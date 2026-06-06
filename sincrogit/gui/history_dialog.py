"""File history dialog (the "time machine").

Pick a repo and a file, browse its versions (sealed commits + intra-window
snapshots from the reflog), preview any version and restore the chosen one.

Talks to the app through the `controller`:
  repo_list() -> [(name, path), ...]
  file_history(name, relpath) -> [ {sha, epoch, source, subject}, ... ]
  file_content_at(name, relpath, sha) -> str | None
  restore_file(name, relpath, sha) -> (ok, message)
"""

import os
from datetime import datetime

from PyQt5.QtCore import Qt
from PyQt5.QtGui import QFont
from PyQt5.QtWidgets import (
    QComboBox,
    QDialog,
    QFileDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)


def _fmt(epoch) -> str:
    try:
        return datetime.fromtimestamp(epoch).strftime("%Y-%m-%d %H:%M:%S")
    except (ValueError, OSError, TypeError):
        return "—"


class HistoryDialog(QDialog):
    def __init__(self, controller, parent=None, preselect_repo=None):
        super().__init__(parent)
        self.c = controller
        self.setWindowTitle("SincroGit — File history")
        self.resize(820, 560)
        self._versions = []

        v = QVBoxLayout(self)

        # --- Top row: repo + file + browse ---
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

        # --- Splitter: versions table (top) + preview (bottom) ---
        splitter = QSplitter(Qt.Vertical)

        self.tbl = QTableWidget(0, 4)
        self.tbl.setHorizontalHeaderLabels(["#", "Time", "Type", "Message"])
        hdr = self.tbl.horizontalHeader()
        hdr.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        hdr.setSectionResizeMode(1, QHeaderView.ResizeToContents)
        hdr.setSectionResizeMode(2, QHeaderView.ResizeToContents)
        hdr.setSectionResizeMode(3, QHeaderView.Stretch)
        self.tbl.setSelectionBehavior(QTableWidget.SelectRows)
        self.tbl.setSelectionMode(QTableWidget.SingleSelection)
        self.tbl.setEditTriggers(QTableWidget.NoEditTriggers)
        self.tbl.itemSelectionChanged.connect(self._load_preview)
        splitter.addWidget(self.tbl)

        prev_box = QWidget()
        pv = QVBoxLayout(prev_box)
        pv.setContentsMargins(0, 0, 0, 0)
        pv.addWidget(QLabel("Preview:"))
        self.preview = QPlainTextEdit()
        self.preview.setReadOnly(True)
        self.preview.setFont(QFont("Consolas", 10))
        pv.addWidget(self.preview)
        splitter.addWidget(prev_box)
        splitter.setSizes([260, 240])
        v.addWidget(splitter, 1)

        # --- Bottom buttons ---
        row = QHBoxLayout()
        self.lbl_info = QLabel()
        row.addWidget(self.lbl_info, 1)
        self.btn_restore = QPushButton("Restore selected version")
        self.btn_restore.clicked.connect(self._restore)
        self.btn_restore.setEnabled(False)
        row.addWidget(self.btn_restore)
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
        self.tbl.setRowCount(len(self._versions))
        for i, ver in enumerate(self._versions):
            kind = "snapshot" if ver["source"] == "snapshot" else "sealed"
            cells = [str(i + 1), _fmt(ver["epoch"]), kind, ver["subject"]]
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
        if not ver:
            return
        content = self.c.file_content_at(self._repo_name(), self._relpath(), ver["sha"])
        self.preview.setPlainText(content if content is not None else "(binary or unavailable)")

    def _restore(self):
        ver = self._selected_version()
        if not ver:
            return
        rel = self._relpath()
        when = _fmt(ver["epoch"])
        if QMessageBox.question(
            self, "Restore",
            f"Restore '{rel}' to its version from {when}?\n\n"
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
