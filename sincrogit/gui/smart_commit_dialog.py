"""Smart Commit dialog: a manual, curated commit with an AI-proposed message.

Flow: on open it asks the engine to PROPOSE a Conventional-Commits message (the
slow AI call runs on a background thread so the GUI never freezes); the developer
edits it and confirms; the engine seals the current WIP with that message and
resets the seal timer. Talks to the app through the `controller`:
  propose_seal_message(name) -> (ok, title, body, files_or_error)
  smart_commit(name, message) -> (ok, message)
"""

import threading

from PyQt5.QtCore import pyqtSignal
from PyQt5.QtGui import QFont
from PyQt5.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
)


class SmartCommitDialog(QDialog):
    # Emitted from background threads; delivered on the GUI thread (queued).
    _proposed = pyqtSignal(bool, str, str, str)   # ok, title, body, files-or-error
    _committed = pyqtSignal(bool, str)            # ok, message

    def __init__(self, controller, repo_name, parent=None):
        super().__init__(parent)
        self.c = controller
        self.name = repo_name
        self._msg = ""
        self.setWindowTitle(f"SincroGit — Smart Commit ({repo_name})")
        self.resize(640, 480)

        v = QVBoxLayout(self)
        v.addWidget(QLabel("Proposed commit message (edit freely):"))
        self.ed_msg = QPlainTextEdit()
        self.ed_msg.setFont(QFont("Consolas", 10))
        self.ed_msg.setPlainText("Generating proposal…")
        self.ed_msg.setEnabled(False)
        v.addWidget(self.ed_msg, 1)

        v.addWidget(QLabel("Files in this commit (current snapshot window):"))
        self.files_view = QPlainTextEdit()
        self.files_view.setReadOnly(True)
        self.files_view.setFont(QFont("Consolas", 9))
        self.files_view.setMaximumHeight(140)
        v.addWidget(self.files_view)

        row = QHBoxLayout()
        self.lbl = QLabel("Proposing a message…")
        row.addWidget(self.lbl, 1)
        self.btn_commit = QPushButton("Commit")
        self.btn_commit.setDefault(True)
        self.btn_commit.setEnabled(False)
        self.btn_commit.clicked.connect(self._commit)
        self.btn_cancel = QPushButton("Cancel")
        self.btn_cancel.clicked.connect(self.reject)
        row.addWidget(self.btn_commit)
        row.addWidget(self.btn_cancel)
        v.addLayout(row)

        self._proposed.connect(self._on_proposed)
        self._committed.connect(self._on_committed)
        threading.Thread(target=self._do_propose, name="sincrogit-propose", daemon=True).start()

    # ----------------------------------------------------- propose (background)
    def _do_propose(self):
        try:
            ok, title, body, files = self.c.propose_seal_message(self.name)
        except Exception as e:  # noqa: BLE001
            ok, title, body, files = False, "", "", str(e)
        self._proposed.emit(ok, title, body, files)

    def _on_proposed(self, ok, title, body, files):
        if not ok:
            QMessageBox.information(self, "Smart Commit", files or "Nothing to commit.")
            self.reject()
            return
        self.ed_msg.setPlainText(title if not body else f"{title}\n\n{body}")
        self.ed_msg.setEnabled(True)
        self.files_view.setPlainText(files or "(no files)")
        self.lbl.setText("Edit the message and press Commit.")
        self.btn_commit.setEnabled(True)
        self.ed_msg.setFocus()

    # ------------------------------------------------------ commit (background)
    def _commit(self):
        msg = self.ed_msg.toPlainText().strip()
        if not msg:
            QMessageBox.warning(self, "Smart Commit", "The commit message can't be empty.")
            return
        self._msg = msg
        self.btn_commit.setEnabled(False)
        self.btn_cancel.setEnabled(False)
        self.ed_msg.setEnabled(False)
        self.lbl.setText("Committing…")
        threading.Thread(target=self._do_commit, name="sincrogit-smartcommit", daemon=True).start()

    def _do_commit(self):
        try:
            ok, m = self.c.smart_commit(self.name, self._msg)
        except Exception as e:  # noqa: BLE001
            ok, m = False, str(e)
        self._committed.emit(ok, m)

    def _on_committed(self, ok, m):
        if ok:
            self.accept()
            return
        QMessageBox.critical(self, "Smart Commit failed", m)
        self.btn_commit.setEnabled(True)
        self.btn_cancel.setEnabled(True)
        self.ed_msg.setEnabled(True)
        self.lbl.setText("Edit the message and press Commit.")
