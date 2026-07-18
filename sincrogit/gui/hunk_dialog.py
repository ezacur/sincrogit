"""Hunk-level restore dialog (part of the "time machine").

Restore only SOME of the changed blocks of a file to a past version, leaving
your other current edits in place — the fine-grained counterpart to "Restore
this file". Both git-touching steps run OFF the GUI thread and return via
queued Qt signals: loading the hunks (git show + a diff) and applying the
selection (takes the repo's op_lock).

Talks to the app through the `controller`:
  file_hunks(name, relpath, sha) -> (ok, {"base": str, "hunks": [ ... ]})
  restore_hunks(name, relpath, sha, selected, base) -> (ok, message)
"""

import threading

from PyQt5.QtCore import Qt, pyqtSignal
from PyQt5.QtGui import QFont
from PyQt5.QtWidgets import (
    QCheckBox,
    QDialog,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from .busy import BusyBar

_MONO = QFont("Consolas", 9)
_PREVIEW_LINES = 12  # per hunk, so a huge block doesn't blow up the dialog


class HunkRestoreDialog(QDialog):
    # Max hunk widgets built (see _on_loaded); the rest is announced, not shown.
    MAX_HUNKS = 200
    # Emitted from background threads; delivered on the GUI thread (queued).
    _loaded = pyqtSignal(bool, object)   # ok, payload|error-str
    _done = pyqtSignal(bool, str)        # ok, message

    def __init__(self, controller, repo_name, relpath, sha, when, parent=None):
        super().__init__(parent)
        self.c = controller
        self.name = repo_name
        self.relpath = relpath
        self.sha = sha
        self._base = ""
        self._checks = []  # (index, QCheckBox)
        self.setWindowTitle(f"⏳g SincroGit — Restore hunks ({relpath})")
        self.resize(720, 560)

        v = QVBoxLayout(self)
        v.addWidget(QLabel(
            f"Tick the blocks to roll back to the version from {when}. "
            f"Lines marked <b>−</b> are what you have now; <b>+</b> is what "
            f"comes back. Everything unticked stays as it is."))

        self.area = QScrollArea()
        self.area.setWidgetResizable(True)
        self._holder = QWidget()
        self._holder_v = QVBoxLayout(self._holder)
        self._holder_v.addWidget(QLabel("Loading changed blocks…"))
        self._holder_v.addStretch(1)
        self.area.setWidget(self._holder)
        v.addWidget(self.area, 1)

        self.busy = BusyBar()
        v.addWidget(self.busy)

        row = QHBoxLayout()
        self.lbl = QLabel("")
        self.lbl.setProperty("cssClass", "muted")
        row.addWidget(self.lbl, 1)
        self.btn_all = QPushButton("Select all")
        self.btn_all.clicked.connect(lambda: self._set_all(True))
        self.btn_all.setEnabled(False)
        row.addWidget(self.btn_all)
        self.btn_none = QPushButton("Select none")
        self.btn_none.clicked.connect(lambda: self._set_all(False))
        self.btn_none.setEnabled(False)
        row.addWidget(self.btn_none)
        self.btn_restore = QPushButton("Restore selected hunks")
        self.btn_restore.setProperty("cssClass", "primary")
        self.btn_restore.setEnabled(False)
        self.btn_restore.clicked.connect(self._restore)
        row.addWidget(self.btn_restore)
        btn_cancel = QPushButton("Cancel")
        btn_cancel.clicked.connect(self.reject)
        row.addWidget(btn_cancel)
        v.addLayout(row)

        self._loaded.connect(self._on_loaded)
        self._done.connect(self._on_done)
        self.busy.start("Loading the changed blocks…")
        threading.Thread(target=self._do_load, name="sincrogit-hunks",
                         daemon=True).start()

    # -------------------------------------------------------- load (background)
    def _do_load(self):
        try:
            ok, payload = self.c.file_hunks(self.name, self.relpath, self.sha)
        except Exception as e:  # noqa: BLE001 — surfaced in the dialog
            ok, payload = False, str(e)
        try:
            self._loaded.emit(ok, payload)
        except RuntimeError:
            pass  # dialog closed while loading

    def _on_loaded(self, ok, payload):
        self.busy.stop()
        # Clear the placeholder.
        while self._holder_v.count():
            item = self._holder_v.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        if not ok:
            self._holder_v.addWidget(QLabel(str(payload)))
            self._holder_v.addStretch(1)
            self.lbl.setText("Nothing to restore.")
            return
        self._base = payload["base"]
        hunks = payload["hunks"]
        if not hunks:
            self._holder_v.addWidget(QLabel(
                "This file already matches that version — nothing to restore."))
            self._holder_v.addStretch(1)
            return
        # Cap the widgets: each hunk is a QWidget+QCheckBox+QLabel, and a
        # heavily-rewritten file can have thousands — seconds of layout on the
        # GUI thread. Announced, never silent; past the cap, picking blocks one
        # by one stops making sense anyway (whole-file restore covers it).
        shown = hunks[:self.MAX_HUNKS]
        for h in shown:
            self._holder_v.addWidget(self._hunk_widget(h))
        if len(hunks) > len(shown):
            more = QLabel(
                f"… {len(hunks) - len(shown)} more block(s) not shown — with this "
                f"much change, 'Restore this file' (the whole version) is the tool.")
            more.setProperty("cssClass", "muted")
            self._holder_v.addWidget(more)
        self._holder_v.addStretch(1)
        self.btn_all.setEnabled(True)
        self.btn_none.setEnabled(True)
        self.btn_restore.setEnabled(True)
        self.lbl.setText(f"{len(hunks)} changed block(s)."
                         + (f"  Showing the first {len(shown)}."
                            if len(hunks) > len(shown) else ""))

    def _hunk_widget(self, h) -> QWidget:
        box = QWidget()
        bv = QVBoxLayout(box)
        bv.setContentsMargins(4, 4, 4, 4)
        cb = QCheckBox(h["header"])
        cb.setChecked(True)
        self._checks.append((h["index"], cb))
        bv.addWidget(cb)
        body = QLabel(self._preview_html(h))
        body.setFont(_MONO)
        body.setTextFormat(Qt.RichText)
        bv.addWidget(body)
        return box

    def _preview_html(self, h) -> str:
        import html
        lines = []
        # Current lines go away (−); target lines come back (+). Cap the block.
        for ln in h["new"][:_PREVIEW_LINES]:
            lines.append(f'<span style="color:#cf222e;">−&nbsp;'
                         f'{html.escape(ln.rstrip(chr(10)))}</span>')
        for ln in h["old"][:_PREVIEW_LINES]:
            lines.append(f'<span style="color:#1a7f37;">+&nbsp;'
                         f'{html.escape(ln.rstrip(chr(10)))}</span>')
        extra = max(len(h["new"]), len(h["old"])) - _PREVIEW_LINES
        if extra > 0:
            lines.append(f'<span style="color:#8a929c;">… {extra} more line(s)</span>')
        return "<br>".join(lines) or "(empty block)"

    def _set_all(self, checked: bool):
        for _i, cb in self._checks:
            cb.setChecked(checked)

    # ----------------------------------------------------- restore (background)
    def _restore(self):
        selected = [i for i, cb in self._checks if cb.isChecked()]
        if not selected:
            QMessageBox.information(self, "Restore hunks",
                                    "Tick at least one block to restore.")
            return
        self.btn_restore.setEnabled(False)
        self.btn_all.setEnabled(False)
        self.btn_none.setEnabled(False)
        self.lbl.setText("Restoring…")
        self.busy.start("Restoring the selected blocks…")
        threading.Thread(
            target=self._do_restore, args=(selected, self._base),
            name="sincrogit-restore-hunks", daemon=True).start()

    def _do_restore(self, selected, base):
        try:
            ok, msg = self.c.restore_hunks(self.name, self.relpath, self.sha,
                                           selected, base)
        except Exception as e:  # noqa: BLE001 — surfaced in the dialog
            ok, msg = False, str(e)
        try:
            self._done.emit(ok, msg)
        except RuntimeError:
            pass  # dialog closed while restoring

    def _on_done(self, ok, msg):
        self.busy.stop()
        if ok:
            QMessageBox.information(self, "Restore hunks", msg)
            self.accept()
            return
        QMessageBox.critical(self, "Restore hunks failed", msg)
        self.btn_restore.setEnabled(True)
        self.btn_all.setEnabled(True)
        self.btn_none.setEnabled(True)
        self.lbl.setText("")
