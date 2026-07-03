"""Dialog to add a repo to SincroGit (existing git repo, local only).

Pick an existing git repository folder; SincroGit adds it live and persists it to
the config file. Remotes are configured later by editing the config / with git.
"""

import os

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
    def __init__(self, controller, parent=None):
        super().__init__(parent)
        self.c = controller
        self.setWindowTitle("⏳g SincroGit — Add repo")
        self.resize(560, 0)

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
        btn_add = QPushButton("Add")
        btn_add.setDefault(True)
        btn_add.clicked.connect(self._add)
        btn_cancel = QPushButton("Cancel")
        btn_cancel.clicked.connect(self.reject)
        buttons.addWidget(btn_add)
        buttons.addWidget(btn_cancel)
        v.addLayout(buttons)

    def _browse(self):
        chosen = QFileDialog.getExistingDirectory(self, "Choose a git repository")
        if chosen:
            self.ed_path.setText(os.path.normpath(chosen))
            self._fill_branch_from_repo()

    def _fill_branch_from_repo(self):
        """Prefill the branch field with the repo's CURRENT branch instead of
        assuming 'main' — otherwise adding a 'master' repo silently starts
        off-branch (autosync waiting). When detection fails, SAY so instead of
        keeping the default quietly."""
        path = self.ed_path.text().strip()
        if not path or not os.path.isdir(path):
            return
        try:
            from ..gitrepo import GitRepo
            branch = GitRepo(path).current_branch()
        except Exception:  # noqa: BLE001 — the hint below covers it
            branch = None
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

    def _add(self):
        path = self.ed_path.text().strip()
        if not path:
            QMessageBox.warning(self, "Add repo", "Please choose a folder.")
            return
        ok, msg = self.c.add_repo(
            path,
            branch=self.ed_branch.text().strip() or "main",
            push=self.cb_push.isChecked(),
            pull=self.cb_pull.isChecked(),
            normalize_eol=self.cb_norm.isChecked(),
        )
        if ok:
            QMessageBox.information(self, "Add repo", "Repo added.")
            self.accept()
        else:
            QMessageBox.critical(self, "Add repo", msg)
