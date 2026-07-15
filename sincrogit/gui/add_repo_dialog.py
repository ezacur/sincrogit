"""Dialog to add a repo to SincroGit (existing git repo, local only).

Pick an existing git repository folder; SincroGit adds it live and persists it to
the config file. Remotes are configured later by editing the config / with git.

Both git-touching steps run OFF the GUI thread and come back via queued Qt
signals: adding a repo (git validation + .gitattributes) and detecting the
branch (git). On a network drive or with an aggressive antivirus those calls
can take seconds — running them inline would freeze the whole interface.

Talks to the app through the `controller`:
  add_repo(path, branch, push, pull, normalize_eol) -> (ok, msg)
  detect_branch(path) -> str | None   ('HEAD' on a detached HEAD)
"""

import os
import threading

from PyQt5.QtCore import pyqtSignal
from PyQt5.QtWidgets import (
    QCheckBox,
    QDialog,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
)


class AddRepoDialog(QDialog):
    # Emitted from background threads; delivered on the GUI thread (queued).
    _added = pyqtSignal(bool, str)          # ok, message
    _branch_ready = pyqtSignal(int, object)  # gen, branch|None

    def __init__(self, controller, parent=None):
        super().__init__(parent)
        self.c = controller
        self.setWindowTitle("⏳g SincroGit — Add repo")
        self.resize(560, 0)
        # Monotonic token so a slow branch detection whose result arrives after
        # the user changed the path is discarded, not written into the field.
        self._branch_gen = 0

        v = QVBoxLayout(self)

        v.addWidget(QLabel("Folder (an existing git repository):"))
        row = QHBoxLayout()
        self.ed_path = QLineEdit()
        self.ed_path.setPlaceholderText(r"C:\path\to\your\repo")
        self.ed_path.editingFinished.connect(self._fill_branch_from_repo)
        row.addWidget(self.ed_path, 1)
        btn_browse = QPushButton("Browse…")
        btn_browse.clicked.connect(self._browse)
        row.addWidget(btn_browse)
        v.addLayout(row)

        row2 = QHBoxLayout()
        row2.addWidget(QLabel("Branch:"))
        self.ed_branch = QLineEdit("main")
        self.ed_branch.setMaximumWidth(160)
        row2.addWidget(self.ed_branch)
        self.cb_push = QCheckBox("push")
        self.cb_push.setChecked(True)
        self.cb_pull = QCheckBox("pull")
        self.cb_pull.setChecked(True)
        row2.addWidget(self.cb_push)
        row2.addWidget(self.cb_pull)
        row2.addStretch(1)
        v.addLayout(row2)

        # Inline feedback for the branch autodetection (silence looked like it
        # worked, then the repo started off-branch with autosync waiting).
        self.lbl_hint = QLabel("")
        self.lbl_hint.setProperty("cssClass", "muted")
        self.lbl_hint.setWordWrap(True)
        self.lbl_hint.setVisible(False)
        v.addWidget(self.lbl_hint)

        self.cb_norm = QCheckBox("Normalize line endings (add .gitattributes if missing)")
        self.cb_norm.setChecked(True)
        self.cb_norm.setToolTip(
            "Adds '* text=auto' so a CRLF/LF-only change is never treated as an edit "
            "and machines don't fight over line endings. Recommended for sync."
        )
        v.addWidget(self.cb_norm)

        v.addWidget(QLabel(
            "Only existing git repos are accepted. Push/pull are skipped until a "
            "remote is configured for the repo."
        ))

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        self.btn_add = QPushButton("Add")
        self.btn_add.setDefault(True)
        self.btn_add.clicked.connect(self._add)
        btn_cancel = QPushButton("Cancel")
        btn_cancel.clicked.connect(self.reject)
        buttons.addWidget(self.btn_add)
        buttons.addWidget(btn_cancel)
        v.addLayout(buttons)

        self._added.connect(self._on_added)
        self._branch_ready.connect(self._on_branch_ready)

    def _browse(self):
        chosen = QFileDialog.getExistingDirectory(self, "Choose a git repository")
        if chosen:
            self.ed_path.setText(os.path.normpath(chosen))
            self._fill_branch_from_repo()

    # ------------------------------------------------- branch autodetect (async)
    def _fill_branch_from_repo(self):
        """Prefill the branch field with the repo's CURRENT branch instead of
        assuming 'main' — otherwise adding a 'master' repo silently starts
        off-branch (autosync waiting). The git call runs on a worker (a slow
        network/AV drive would freeze Qt); a generation token discards a stale
        result if the user changed the path meanwhile."""
        path = self.ed_path.text().strip()
        if not path or not os.path.isdir(path):
            return
        self._branch_gen += 1
        gen = self._branch_gen
        threading.Thread(
            target=self._do_detect_branch, args=(gen, path),
            name="sincrogit-detect-branch", daemon=True,
        ).start()

    def _do_detect_branch(self, gen, path):
        try:
            branch = self.c.detect_branch(path)
        except Exception:  # noqa: BLE001 — the hint below covers it
            branch = None
        try:
            self._branch_ready.emit(gen, branch)
        except RuntimeError:
            pass  # dialog closed while detecting

    def _on_branch_ready(self, gen, branch):
        if gen != self._branch_gen:
            return  # the path changed after this detection was kicked off
        if branch and branch != "HEAD":
            self.ed_branch.setText(branch)
            self._hint("")
        elif branch == "HEAD":
            self._hint("This repo is on a detached HEAD — type the branch "
                       "SincroGit should track.")
        else:
            self._hint(f"Couldn't detect the repo's branch — check that "
                       f"'{self.ed_branch.text().strip() or 'main'}' is right.")

    def _hint(self, text: str):
        self.lbl_hint.setText(text)
        self.lbl_hint.setVisible(bool(text))

    # -------------------------------------------------------- add repo (async)
    def _add(self):
        path = self.ed_path.text().strip()
        if not path:
            QMessageBox.warning(self, "Add repo", "Please choose a folder.")
            return
        # Disable Add while the worker runs: add_repo does git (validate, add
        # live, .gitattributes) which on a slow drive would otherwise let the
        # user click Add twice.
        self.btn_add.setEnabled(False)
        threading.Thread(
            target=self._do_add,
            args=(path, self.ed_branch.text().strip() or "main",
                  self.cb_push.isChecked(), self.cb_pull.isChecked(),
                  self.cb_norm.isChecked()),
            name="sincrogit-add-repo", daemon=True,
        ).start()

    def _do_add(self, path, branch, push, pull, normalize_eol):
        try:
            ok, msg = self.c.add_repo(path, branch=branch, push=push, pull=pull,
                                      normalize_eol=normalize_eol)
        except Exception as e:  # noqa: BLE001 — surfaced in the dialog
            ok, msg = False, str(e)
        try:
            self._added.emit(ok, msg)
        except RuntimeError:
            pass  # dialog closed while adding

    def _on_added(self, ok, msg):
        self.btn_add.setEnabled(True)
        if ok:
            QMessageBox.information(self, "Add repo", "Repo added.")
            self.accept()
        else:
            QMessageBox.critical(self, "Add repo", msg)
