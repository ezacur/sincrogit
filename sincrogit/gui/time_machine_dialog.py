"""Time Machine explorer: browse the repo BY VERSION and restore selected files.

The inverse navigation of the File history dialog (file -> its versions): pick a
point in the repo's timeline (sealed commits, intra-window snapshots, fetched
autosnap mirrors) and see every file that differs from the present, each with a
checkbox — restore the checked set in ONE atomic step (a single WIP capture).
The diff of the clicked file shows unified or side-by-side.

Heavy git work (timeline, per-version diff, the restore itself) runs on
background threads; results come back through queued Qt signals.

Talks to the app through the `controller`:
  repo_list() -> [(name, path), ...]
  repo_history(name, limit=200) -> [ {sha, epoch, source, subject}, ... ]
  restore_repo_preview(name, sha) -> (ok, {"changes": [(verb, path)], "risky": [...]})
  file_text_at(name, relpath, sha) -> str | None
  current_text(name, relpath) -> str
  restore_files(name, relpaths, sha) -> (ok, message)
"""

import os
import threading
from datetime import datetime

from PyQt5.QtCore import Qt, pyqtSignal
from PyQt5.QtGui import QColor, QFont
from PyQt5.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QFileDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMessageBox,
    QPushButton,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from .diff import diff_html, diff_html_sbs
from .history_dialog import _TYPE_COLOR, _TYPE_TIP, _ago, _fmt

# What the restore would DO to each differing file (colors match the intent).
_VERB_COLOR = {"revert": "#8a6d00", "delete": "#cf222e", "recreate": "#1a7f37"}
_VERB_TIP = {
    "revert": "Differs from that version — restoring reverts it to the version's content.",
    "delete": "Created after that version — restoring REMOVES it.",
    "recreate": "Deleted after that version — restoring brings it back.",
}


class TimeMachineDialog(QDialog):
    # Emitted from background threads; delivered on the GUI thread (queued).
    _history_ready = pyqtSignal(str, list)          # repo, versions
    _files_ready = pyqtSignal(bool, object, str)    # ok, payload|msg, sha
    _restore_done = pyqtSignal(bool, str)           # ok, message
    _diff_ready = pyqtSignal(int, object, object, bool)  # gen, old|None, new, sbs
    _export_done = pyqtSignal(bool, str, str)            # ok, message, dest path

    def __init__(self, controller, parent=None, preselect_repo=None):
        super().__init__(parent)
        self.c = controller
        self.setWindowTitle("⏳g SincroGit — Time machine")
        self.resize(980, 640)
        self._versions = []
        self._files = []      # [(verb, path)] of the selected version
        self._risky = set()
        self._sha = None      # the version the files table currently shows
        self._diff_gen = 0    # discards a diff whose result arrives after the
                              # user clicked another file/version

        v = QVBoxLayout(self)

        top = QHBoxLayout()
        top.addWidget(QLabel("Repo:"))
        self.cb_repo = QComboBox()
        for name, path in self.c.repo_list():
            self.cb_repo.addItem(name, path)
        if preselect_repo:
            i = self.cb_repo.findText(preselect_repo)
            if i >= 0:
                self.cb_repo.setCurrentIndex(i)
        self.cb_repo.currentIndexChanged.connect(self._load_history)
        top.addWidget(self.cb_repo)
        top.addStretch(1)
        self.cb_sbs = QCheckBox("Side-by-side diff")
        self.cb_sbs.stateChanged.connect(self._load_diff)
        top.addWidget(self.cb_sbs)
        v.addLayout(top)

        outer = QSplitter(Qt.Horizontal)

        # --- Left: the repo's version timeline ---
        self.tbl_ver = QTableWidget(0, 3)
        self.tbl_ver.setHorizontalHeaderLabels(["When", "Type", "Message"])
        hdr = self.tbl_ver.horizontalHeader()
        hdr.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        hdr.setSectionResizeMode(1, QHeaderView.ResizeToContents)
        hdr.setSectionResizeMode(2, QHeaderView.Stretch)
        self.tbl_ver.setSelectionBehavior(QTableWidget.SelectRows)
        self.tbl_ver.setSelectionMode(QTableWidget.SingleSelection)
        self.tbl_ver.setEditTriggers(QTableWidget.NoEditTriggers)
        self.tbl_ver.setAlternatingRowColors(True)
        self.tbl_ver.setShowGrid(False)
        self.tbl_ver.verticalHeader().setVisible(False)
        self.tbl_ver.itemSelectionChanged.connect(self._version_selected)
        outer.addWidget(self.tbl_ver)

        # --- Right: differing files (checkboxes) over the diff preview ---
        right = QSplitter(Qt.Vertical)
        files_box = QWidget()
        fv = QVBoxLayout(files_box)
        fv.setContentsMargins(0, 0, 0, 0)
        frow = QHBoxLayout()
        self.lbl_files = QLabel("Pick a version on the left")
        self.lbl_files.setProperty("cssClass", "muted")
        frow.addWidget(self.lbl_files, 1)
        b_all = QPushButton("Select all")
        b_all.clicked.connect(lambda: self._set_all(Qt.Checked))
        b_none = QPushButton("Select none")
        b_none.clicked.connect(lambda: self._set_all(Qt.Unchecked))
        b_save = QPushButton("Save a copy…")
        b_save.setToolTip(
            "Write the clicked file's version to a NEW file — recover it under "
            "another name; nothing is overwritten."
        )
        b_save.clicked.connect(self._save_copy)
        frow.addWidget(b_all)
        frow.addWidget(b_none)
        frow.addWidget(b_save)
        fv.addLayout(frow)
        self.tbl_files = QTableWidget(0, 3)
        self.tbl_files.setHorizontalHeaderLabels(["", "Action", "File"])
        fhdr = self.tbl_files.horizontalHeader()
        fhdr.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        fhdr.setSectionResizeMode(1, QHeaderView.ResizeToContents)
        fhdr.setSectionResizeMode(2, QHeaderView.Stretch)
        self.tbl_files.setSelectionBehavior(QTableWidget.SelectRows)
        self.tbl_files.setSelectionMode(QTableWidget.SingleSelection)
        self.tbl_files.setEditTriggers(QTableWidget.NoEditTriggers)
        self.tbl_files.setAlternatingRowColors(True)
        self.tbl_files.setShowGrid(False)
        self.tbl_files.verticalHeader().setVisible(False)
        self.tbl_files.itemSelectionChanged.connect(self._load_diff)
        self.tbl_files.itemChanged.connect(self._sync_restore_button)
        fv.addWidget(self.tbl_files)
        right.addWidget(files_box)

        self.preview = QTextEdit()
        self.preview.setReadOnly(True)
        self.preview.setFont(QFont("Consolas", 10))
        self.preview.setLineWrapMode(QTextEdit.NoWrap)
        right.addWidget(self.preview)
        right.setSizes([280, 260])
        outer.addWidget(right)
        outer.setSizes([340, 640])
        outer.setStretchFactor(1, 1)
        v.addWidget(outer, 1)

        row = QHBoxLayout()
        self.lbl_info = QLabel()
        self.lbl_info.setProperty("cssClass", "muted")
        row.addWidget(self.lbl_info, 1)
        self.btn_restore = QPushButton("Restore selected")
        self.btn_restore.setProperty("cssClass", "primary")
        self.btn_restore.setEnabled(False)
        self.btn_restore.clicked.connect(self._restore_selected)
        row.addWidget(self.btn_restore)
        btn_close = QPushButton("Close")
        btn_close.clicked.connect(self.accept)
        row.addWidget(btn_close)
        v.addLayout(row)

        self._history_ready.connect(self._on_history_ready)
        self._files_ready.connect(self._on_files_ready)
        self._restore_done.connect(self._on_restore_done)
        self._diff_ready.connect(self._on_diff_ready)
        self._export_done.connect(self._on_export_done)
        self._load_history()

    # ------------------------------------------------------------- timeline
    def _repo_name(self) -> str:
        return self.cb_repo.currentText()

    def _load_history(self):
        name = self._repo_name()
        if not name:
            return
        self.lbl_info.setText(f"Loading versions of '{name}'…")
        self.tbl_ver.setRowCount(0)
        self._clear_files("Pick a version on the left")
        threading.Thread(target=self._do_load_history, args=(name,),
                         name="sincrogit-tm-history", daemon=True).start()

    def _do_load_history(self, name):
        try:
            versions = self.c.repo_history(name)
        except Exception:  # noqa: BLE001 — an empty timeline reads as "none"
            versions = []
        try:
            self._history_ready.emit(name, versions)
        except RuntimeError:
            pass  # dialog closed meanwhile

    def _on_history_ready(self, name, versions):
        if name != self._repo_name():
            return  # the user switched repos while this one was loading
        self.lbl_info.setText("")
        self._versions = versions
        self.tbl_ver.setRowCount(len(versions))
        for i, ver in enumerate(versions):
            source = ver.get("source", "")
            cells = [_ago(ver["epoch"]), source, ver["subject"]]
            for j, val in enumerate(cells):
                item = QTableWidgetItem(val)
                if j == 0:
                    item.setToolTip(_fmt(ver["epoch"]))
                if j == 1:
                    item.setToolTip(_TYPE_TIP.get(source, ""))
                    if source in _TYPE_COLOR:
                        item.setForeground(QColor(_TYPE_COLOR[source]))
                self.tbl_ver.setItem(i, j, item)
        if versions:
            self.tbl_ver.selectRow(0)
        else:
            self.lbl_files.setText("No versions found")

    # ------------------------------------------------------- differing files
    def _selected_version(self):
        row = self.tbl_ver.currentRow()
        if 0 <= row < len(self._versions):
            return self._versions[row]
        return None

    def _clear_files(self, note: str):
        self._files, self._risky, self._sha = [], set(), None
        self.tbl_files.setRowCount(0)
        self.preview.clear()
        self.lbl_files.setText(note)
        self._sync_restore_button()

    def _version_selected(self):
        ver = self._selected_version()
        if not ver:
            return
        self._clear_files("Comparing with the current state…")
        self._sha = ver["sha"]
        threading.Thread(
            target=self._do_load_files, args=(self._repo_name(), ver["sha"]),
            name="sincrogit-tm-files", daemon=True,
        ).start()

    def _do_load_files(self, name, sha):
        try:
            ok, payload = self.c.restore_repo_preview(name, sha)
        except Exception as e:  # noqa: BLE001 — surfaced in the dialog
            ok, payload = False, str(e)
        try:
            self._files_ready.emit(ok, payload, sha)
        except RuntimeError:
            pass  # dialog closed meanwhile

    # Max rows the per-version file table builds (see _on_files_ready).
    MAX_FILE_ROWS = 4000

    def _on_files_ready(self, ok, payload, sha):
        if sha != self._sha:
            return  # a newer selection superseded this computation
        if not ok:
            self.lbl_files.setText(f"Could not compare: {payload}")
            return
        changes = payload["changes"]
        n = len(changes)
        # Cap the table: an old version can differ in tens of thousands of
        # paths and 3 items × row froze the GUI on every version click. The
        # cut is announced, never silent; a whole-repo restore (History tab)
        # covers anything beyond the selectable window.
        self._files = changes[:self.MAX_FILE_ROWS]
        self._risky = set(payload["risky"])
        extra = f"  (⚠ {len(self._risky)} at risk)" if self._risky else ""
        if n > len(self._files):
            extra += (f"  — showing the first {len(self._files)}; use a "
                      f"whole-repo restore for the rest")
        self.lbl_files.setText(
            f"{n} file(s) differ from the current state{extra}" if n or self._risky
            else "The working tree already matches this version")
        self.tbl_files.blockSignals(True)  # itemChanged fires per cell otherwise
        self.tbl_files.setRowCount(len(self._files))
        for i, (verb, path) in enumerate(self._files):
            chk = QTableWidgetItem("")
            if path in self._risky:
                chk.setFlags(Qt.ItemIsSelectable | Qt.ItemIsEnabled)
                chk.setText("⚠")
                chk.setToolTip(
                    "Current content that snapshots can't capture (excluded, over "
                    "the size limit or binary) — restoring would destroy it, so it "
                    "can't be selected. Copy it somewhere safe first.")
            else:
                chk.setFlags(Qt.ItemIsUserCheckable | Qt.ItemIsSelectable | Qt.ItemIsEnabled)
                chk.setCheckState(Qt.Unchecked)
            self.tbl_files.setItem(i, 0, chk)
            it_verb = QTableWidgetItem(verb)
            it_verb.setForeground(QColor(_VERB_COLOR.get(verb, "#6b7280")))
            it_verb.setToolTip(_VERB_TIP.get(verb, ""))
            self.tbl_files.setItem(i, 1, it_verb)
            self.tbl_files.setItem(i, 2, QTableWidgetItem(path))
        self.tbl_files.blockSignals(False)
        self._sync_restore_button()

    # ------------------------------------------------------------------ diff
    def _load_diff(self):
        """Load the clicked file's old/new text on a worker: file_text_at /
        current_text run `git show` + pandoc/python-pptx for documents, which
        would freeze the window inline."""
        row = self.tbl_files.currentRow()
        if not (0 <= row < len(self._files)) or not self._sha:
            return
        verb, path = self._files[row]
        self._diff_gen += 1
        gen = self._diff_gen
        self.preview.setPlainText("Loading…")
        threading.Thread(
            target=self._do_load_diff,
            args=(gen, self._repo_name(), path, self._sha, verb, self.cb_sbs.isChecked()),
            name="sincrogit-tm-diff", daemon=True,
        ).start()

    def _do_load_diff(self, gen, name, path, sha, verb, sbs):
        try:
            old = self.c.file_text_at(name, path, sha) if verb != "delete" else ""
            new = (self.c.current_text(name, path)
                   if old is not None and verb != "recreate" else "")
        except Exception:  # noqa: BLE001 — shown as "unavailable" below
            old, new = None, ""
        try:
            self._diff_ready.emit(gen, old, new, sbs)
        except RuntimeError:
            pass  # dialog closed while loading

    def _on_diff_ready(self, gen, old, new, sbs):
        if gen != self._diff_gen:
            return  # a newer selection superseded this diff
        if old is None:
            self.preview.setPlainText("(binary or unavailable)")
            return
        pal = getattr(self.c, "theme", None) or {}
        dark = bool(pal.get("is_dark"))
        if sbs:
            self.preview.setHtml(diff_html_sbs(old, new, dark=dark))
        else:
            self.preview.setHtml(diff_html(old, new, dark=dark))

    def _save_copy(self):
        """Recover the clicked file's version WITHOUT overwriting anything."""
        row = self.tbl_files.currentRow()
        ver = self._selected_version()
        if not (0 <= row < len(self._files)) or not ver:
            return
        verb, path = self._files[row]
        if verb == "delete":
            QMessageBox.information(
                self, "Save a copy",
                "This file doesn't exist in the selected version (it was created "
                "later) — there is nothing to save from there.")
            return
        stem, ext = os.path.splitext(os.path.basename(path))
        try:
            stamp = datetime.fromtimestamp(ver["epoch"]).strftime("%Y-%m-%d %H.%M")
        except (ValueError, OSError, TypeError):
            stamp = ver["sha"][:8]
        suggested = os.path.join(self.cb_repo.currentData() or "",
                                 os.path.dirname(path), f"{stem} ({stamp}){ext}")
        dest, _ = QFileDialog.getSaveFileName(
            self, "Save a copy of this version", suggested)
        if not dest:
            return
        # Worker: `git show` of a raw blob + write — seconds for a big binary,
        # and it froze the dialog inline (same fix as the history dialog).
        threading.Thread(
            target=self._do_export,
            args=(self._repo_name(), path, ver["sha"], dest),
            name="sincrogit-tm-export", daemon=True).start()

    def _do_export(self, name, path, sha, dest):
        try:
            ok, msg = self.c.export_file_version(name, path, sha, dest)
        except Exception as e:  # noqa: BLE001 — surfaced in the dialog
            ok, msg = False, str(e)
        try:
            self._export_done.emit(ok, msg, dest)
        except RuntimeError:
            pass  # dialog closed while exporting

    def _on_export_done(self, ok, msg, dest):
        if ok:
            QMessageBox.information(self, "Save a copy", f"Saved to:\n{dest}")
        else:
            QMessageBox.critical(self, "Save a copy", msg)

    # --------------------------------------------------------------- restore
    def _checked_paths(self) -> list:
        out = []
        for i, (_verb, path) in enumerate(self._files):
            item = self.tbl_files.item(i, 0)
            if item is not None and item.checkState() == Qt.Checked:
                out.append(path)
        return out

    def _set_all(self, state):
        self.tbl_files.blockSignals(True)
        for i, (_verb, path) in enumerate(self._files):
            item = self.tbl_files.item(i, 0)
            if item is not None and path not in self._risky:
                item.setCheckState(state)
        self.tbl_files.blockSignals(False)
        self._sync_restore_button()

    def _sync_restore_button(self):
        n = len(self._checked_paths())
        self.btn_restore.setText(f"Restore selected ({n})" if n else "Restore selected")
        self.btn_restore.setEnabled(n > 0)

    def _restore_selected(self):
        paths = self._checked_paths()
        ver = self._selected_version()
        if not paths or not ver:
            return
        when = _fmt(ver["epoch"])
        sample = "\n".join("  • " + p for p in paths[:8])
        if len(paths) > 8:
            sample += f"\n  … and {len(paths) - 8} more"
        # Default to Cancel: a destructive restore shouldn't fire on a stray Enter.
        if QMessageBox.question(
            self, "Restore selected files",
            f"Restore {len(paths)} file(s) of '{self._repo_name()}' to their state "
            f"at {when}?\n\n{sample}\n\nReversible: the restore is captured as a "
            f"new snapshot.",
            QMessageBox.Yes | QMessageBox.Cancel, QMessageBox.Cancel,
        ) != QMessageBox.Yes:
            return
        self.btn_restore.setEnabled(False)
        self.lbl_info.setText(f"Restoring {len(paths)} file(s)…")
        threading.Thread(
            target=self._do_restore, args=(self._repo_name(), paths, ver["sha"]),
            name="sincrogit-tm-restore", daemon=True,
        ).start()

    def _do_restore(self, name, paths, sha):
        try:
            ok, msg = self.c.restore_files(name, paths, sha)
        except Exception as e:  # noqa: BLE001 — surfaced in the dialog
            ok, msg = False, str(e)
        try:
            self._restore_done.emit(ok, msg)
        except RuntimeError:
            pass  # dialog closed meanwhile

    def _on_restore_done(self, ok, msg):
        self.lbl_info.setText("")
        if ok:
            QMessageBox.information(self, "Restore", msg)
            self._version_selected()  # recompute: restored files leave the list
        else:
            QMessageBox.critical(self, "Restore failed", msg)
            self._sync_restore_button()
