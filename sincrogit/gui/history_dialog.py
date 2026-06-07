"""File history dialog (the "time machine").

Pick a repo and a file, browse its versions (sealed commits, intra-window
snapshots from the reflog, and — after "Fetch autosnaps" — other machines' live
mirrors), see a colored diff against the current file, and recover: either just
that file or the whole repository, to the chosen version.

Talks to the app through the `controller`:
  repo_list() -> [(name, path), ...]
  file_history(name, relpath) -> [ {sha, epoch, source, subject}, ... ]
  file_content_at(name, relpath, sha) -> str | None
  restore_file(name, relpath, sha) -> (ok, message)
  restore_repo(name, sha) -> (ok, message)
  fetch_autosnaps(name) -> [ {host, branch, epoch, sha, ...}, ... ]
"""

import difflib
import html
import os
from datetime import datetime

from PyQt5.QtCore import Qt
from PyQt5.QtGui import QFont
from PyQt5.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QFileDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)


def _fmt(epoch) -> str:
    try:
        return datetime.fromtimestamp(epoch).strftime("%Y-%m-%d %H:%M:%S")
    except (ValueError, OSError, TypeError):
        return "—"


def _diff_html(old_text: str, current_text: str) -> str:
    """Unified diff (old version -> current file) as colored HTML."""
    diff = difflib.unified_diff(
        old_text.splitlines(), current_text.splitlines(),
        fromfile="selected version", tofile="current file", lineterm="",
    )
    rows = []
    for ln in diff:
        esc = html.escape(ln)
        if ln.startswith(("+++", "---")):
            color = "#888888"
        elif ln.startswith("@@"):
            color = "#2b6cb0"
        elif ln.startswith("+"):
            color = "#1a7f37"   # green: present in the current file
        elif ln.startswith("-"):
            color = "#cf222e"   # red: only in the old version
        else:
            color = "#444444"
        rows.append(f'<span style="color:{color};">{esc or "&nbsp;"}</span>')
    if not rows:
        return '<pre style="color:#777;">(no differences vs the current file)</pre>'
    body = "\n".join(rows)
    return f'<pre style="font-family:Consolas,monospace;font-size:10pt;margin:0;">{body}</pre>'


class HistoryDialog(QDialog):
    def __init__(self, controller, parent=None, preselect_repo=None):
        super().__init__(parent)
        self.c = controller
        self.setWindowTitle("SincroGit — File history")
        self.resize(860, 600)
        self._versions = []

        v = QVBoxLayout(self)

        # --- Top row: repo + file + browse + show ---
        top = QHBoxLayout()
        top.addWidget(QLabel("Repo:"))
        self.cb_repo = QComboBox()
        for name, path in self.c.repo_list():
            self.cb_repo.addItem(name, path)
        if preselect_repo:
            i = self.cb_repo.findText(preselect_repo)
            if i >= 0:
                self.cb_repo.setCurrentIndex(i)
        top.addWidget(self.cb_repo)

        top.addWidget(QLabel("File:"))
        self.ed_file = QLineEdit()
        self.ed_file.setPlaceholderText("path relative to the repo, e.g. src/app.py")
        self.ed_file.returnPressed.connect(self.show_history)
        top.addWidget(self.ed_file, 1)

        btn_browse = QPushButton("Browse…")
        btn_browse.clicked.connect(self._browse)
        top.addWidget(btn_browse)
        btn_show = QPushButton("Show history")
        btn_show.clicked.connect(self.show_history)
        top.addWidget(btn_show)
        v.addLayout(top)

        # --- Second row: recovery-from-other-machines + diff toggle ---
        row2 = QHBoxLayout()
        btn_fetch = QPushButton("Fetch autosnaps (other machines)…")
        btn_fetch.setToolTip(
            "Download the live mirrors pushed by your other machines so their "
            "latest states show up in the history below (disaster recovery)."
        )
        btn_fetch.clicked.connect(self._fetch_autosnaps)
        row2.addWidget(btn_fetch)
        row2.addStretch(1)
        self.cb_diff = QCheckBox("Show diff vs current file")
        self.cb_diff.setChecked(True)
        self.cb_diff.stateChanged.connect(self._load_preview)
        row2.addWidget(self.cb_diff)
        v.addLayout(row2)

        # --- Splitter: versions table (top) + preview/diff (bottom) ---
        splitter = QSplitter(Qt.Vertical)

        self.tbl = QTableWidget(0, 4)
        self.tbl.setHorizontalHeaderLabels(["#", "Time", "Type", "Message"])
        hdr = self.tbl.horizontalHeader()
        for col in range(3):
            hdr.setSectionResizeMode(col, QHeaderView.ResizeToContents)
        hdr.setSectionResizeMode(3, QHeaderView.Stretch)
        self.tbl.setSelectionBehavior(QTableWidget.SelectRows)
        self.tbl.setSelectionMode(QTableWidget.SingleSelection)
        self.tbl.setEditTriggers(QTableWidget.NoEditTriggers)
        self.tbl.itemSelectionChanged.connect(self._load_preview)
        splitter.addWidget(self.tbl)

        prev_box = QWidget()
        pv = QVBoxLayout(prev_box)
        pv.setContentsMargins(0, 0, 0, 0)
        pv.addWidget(QLabel("Preview / diff:"))
        self.preview = QTextEdit()
        self.preview.setReadOnly(True)
        self.preview.setFont(QFont("Consolas", 10))
        self.preview.setLineWrapMode(QTextEdit.NoWrap)
        pv.addWidget(self.preview)
        splitter.addWidget(prev_box)
        splitter.setSizes([260, 280])
        v.addWidget(splitter, 1)

        # --- Bottom buttons ---
        row = QHBoxLayout()
        self.lbl_info = QLabel()
        row.addWidget(self.lbl_info, 1)
        self.btn_restore = QPushButton("Restore this file")
        self.btn_restore.clicked.connect(self._restore)
        self.btn_restore.setEnabled(False)
        row.addWidget(self.btn_restore)
        self.btn_restore_repo = QPushButton("Restore ENTIRE repo…")
        self.btn_restore_repo.setToolTip(
            "Set every tracked file back to this version (including deletions). "
            "Reversible: it's captured as a new snapshot."
        )
        self.btn_restore_repo.clicked.connect(self._restore_repo)
        self.btn_restore_repo.setEnabled(False)
        row.addWidget(self.btn_restore_repo)
        btn_close = QPushButton("Close")
        btn_close.clicked.connect(self.accept)
        row.addWidget(btn_close)
        v.addLayout(row)

    # ----------------------------------------------------------- helpers
    def _repo_name(self) -> str:
        return self.cb_repo.currentText()

    def _repo_path(self) -> str:
        return self.cb_repo.currentData() or ""

    def _relpath(self) -> str:
        return self.ed_file.text().strip().replace("\\", "/")

    def _current_content(self) -> str:
        """The file's current content on disk (working tree), or '' if missing."""
        base, rel = self._repo_path(), self._relpath()
        if not base or not rel:
            return ""
        full = os.path.join(base, rel.replace("/", os.sep))
        try:
            with open(full, "r", encoding="utf-8", errors="replace") as fh:
                return fh.read(400_000)
        except (OSError, ValueError):
            return ""  # missing/unreadable -> treat as empty (e.g. file was deleted)

    def _browse(self):
        base = self._repo_path()
        chosen, _ = QFileDialog.getOpenFileName(self, "Choose a file", base)
        if not chosen:
            return
        try:
            rel = os.path.relpath(os.path.abspath(chosen), os.path.abspath(base))
        except ValueError:
            rel = ""
        if not rel or rel.startswith(".."):
            QMessageBox.warning(
                self, "File history",
                "The chosen file is not inside the selected repo.",
            )
            return
        self.ed_file.setText(rel.replace(os.sep, "/"))
        self.show_history()

    # ----------------------------------------------------------- actions
    def show_history(self):
        rel = self._relpath()
        if not rel:
            return
        self._versions = self.c.file_history(self._repo_name(), rel)
        self.preview.clear()
        self.btn_restore.setEnabled(False)
        self.btn_restore_repo.setEnabled(False)
        self.tbl.setRowCount(len(self._versions))
        for i, ver in enumerate(self._versions):
            cells = [str(i + 1), _fmt(ver["epoch"]), ver.get("source", ""), ver["subject"]]
            for j, val in enumerate(cells):
                self.tbl.setItem(i, j, QTableWidgetItem(val))
        if self._versions:
            self.lbl_info.setText(f"{len(self._versions)} version(s) of '{rel}'")
            self.tbl.selectRow(0)
        else:
            self.lbl_info.setText(f"No history for '{rel}'")

    def _selected_version(self):
        row = self.tbl.currentRow()
        if 0 <= row < len(self._versions):
            return self._versions[row]
        return None

    def _load_preview(self):
        ver = self._selected_version()
        self.btn_restore.setEnabled(ver is not None)
        self.btn_restore_repo.setEnabled(ver is not None)
        if not ver:
            self.preview.clear()
            return
        content = self.c.file_content_at(self._repo_name(), self._relpath(), ver["sha"])
        if content is None:
            self.preview.setPlainText("(binary or unavailable)")
            return
        if self.cb_diff.isChecked():
            self.preview.setHtml(_diff_html(content, self._current_content()))
        else:
            self.preview.setPlainText(content)

    def _restore(self):
        ver = self._selected_version()
        if not ver:
            return
        rel = self._relpath()
        when = _fmt(ver["epoch"])
        if QMessageBox.question(
            self, "Restore file",
            f"Restore '{rel}' to its version from {when} ({ver.get('source','')})?\n\n"
            f"The current content will be overwritten in the working tree "
            f"(and saved as a new snapshot).",
        ) != QMessageBox.Yes:
            return
        ok, msg = self.c.restore_file(self._repo_name(), rel, ver["sha"])
        if ok:
            QMessageBox.information(self, "Restore", f"Restored '{rel}' to {when}.")
            self.show_history()
        else:
            QMessageBox.critical(self, "Restore failed", msg)

    def _restore_repo(self):
        ver = self._selected_version()
        if not ver:
            return
        when = _fmt(ver["epoch"])
        if QMessageBox.warning(
            self, "Restore ENTIRE repo",
            f"Restore the WHOLE repository '{self._repo_name()}' to its state at "
            f"{when} ({ver.get('source','')})?\n\n"
            f"Every tracked file is set back to that version (including deletions). "
            f"This is reversible — it's captured as a new snapshot — but your current "
            f"working tree will change.",
            QMessageBox.Yes | QMessageBox.Cancel, QMessageBox.Cancel,
        ) != QMessageBox.Yes:
            return
        ok, msg = self.c.restore_repo(self._repo_name(), ver["sha"])
        if ok:
            QMessageBox.information(self, "Restore", f"Repository restored to {when}.")
            self.show_history()
        else:
            QMessageBox.critical(self, "Restore failed", msg)

    def _fetch_autosnaps(self):
        name = self._repo_name()
        QApplication.setOverrideCursor(Qt.WaitCursor)
        try:
            states = self.c.fetch_autosnaps(name)
        finally:
            QApplication.restoreOverrideCursor()
        extra = "\nThey now appear in the history of any file." if states else ""
        QMessageBox.information(
            self, "Autosnap",
            f"{len(states)} autosnap state(s) available for '{name}'.{extra}",
        )
        if self._relpath():
            self.show_history()
