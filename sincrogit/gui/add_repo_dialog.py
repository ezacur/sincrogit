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
  detect_remote(path, remote='origin') -> str | None
  configure_remote(path, url, branch, remote='origin') -> (ok, msg)
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

from .busy import BusyBar


class AddRepoDialog(QDialog):
    # Emitted from background threads; delivered on the GUI thread (queued).
    _added = pyqtSignal(bool, str)          # ok, message
    _branch_ready = pyqtSignal(int, object)  # gen, branch|None
    _remote_detected = pyqtSignal(int, object)   # gen, url|None
    _remote_verified = pyqtSignal(bool, str)     # ok, message
    _settings_ready = pyqtSignal(int, object)    # gen, overrides|None

    def __init__(self, controller, parent=None):
        super().__init__(parent)
        self.c = controller
        self.setWindowTitle("⏳g SincroGit — Add repo")
        self.resize(560, 0)
        # Monotonic token so a slow branch detection whose result arrives after
        # the user changed the path is discarded, not written into the field.
        self._branch_gen = 0
        self._remote_ok = False  # a verify passed for the URL currently shown
        self._inherited = None   # options published by another of your machines

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

        # --- Remote onboarding. Without a remote, push/pull/autosnap and the
        # cross-machine handoff all sit idle — and today the only sign is a
        # --doctor warning. Let the user paste a URL and verify it here.
        v.addWidget(QLabel("Remote (for push / pull / cross-machine sync):"))
        row3 = QHBoxLayout()
        self.ed_remote = QLineEdit()
        self.ed_remote.setPlaceholderText("https://github.com/you/repo.git  (leave blank for local-only)")
        self.ed_remote.textChanged.connect(self._on_remote_changed)
        row3.addWidget(self.ed_remote, 1)
        self.btn_verify = QPushButton("Verify")
        self.btn_verify.clicked.connect(self._verify_remote)
        row3.addWidget(self.btn_verify)
        v.addLayout(row3)

        self.lbl_remote = QLabel("")
        self.lbl_remote.setProperty("cssClass", "muted")
        self.lbl_remote.setWordWrap(True)
        self.lbl_remote.setVisible(False)
        v.addWidget(self.lbl_remote)

        # Cross-machine inheritance: if THIS user published options for this repo
        # from another machine (a side ref), offer to adopt them here. Hidden
        # until the async detection finds some.
        self.ck_inherit = QCheckBox("Use the settings saved from your other machine")
        self.ck_inherit.setChecked(True)
        self.ck_inherit.setVisible(False)
        v.addWidget(self.ck_inherit)

        self.cb_norm = QCheckBox("Normalize line endings (add .gitattributes if missing)")
        self.cb_norm.setChecked(True)
        self.cb_norm.setToolTip(
            "Adds '* text=auto' so a CRLF/LF-only change is never treated as an edit "
            "and machines don't fight over line endings. Recommended for sync."
        )
        v.addWidget(self.cb_norm)

        v.addWidget(QLabel(
            "Only existing git repos are accepted. Without a remote the repo is "
            "versioned locally only — no push/pull and no cross-machine sync."
        ))

        self.busy = BusyBar()
        v.addWidget(self.busy)

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
        self._remote_detected.connect(self._on_remote_detected)
        self._remote_verified.connect(self._on_remote_verified)
        self._settings_ready.connect(self._on_settings_ready)

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
            remote = self.c.detect_remote(path)
        except Exception:  # noqa: BLE001 — treated as "no remote"
            remote = None
        # Saved settings from another machine (network fetch of one side ref;
        # returns None fast when there's no remote). Optional on the controller
        # so older/duck-typed controllers simply skip the offer.
        settings = None
        fetch = getattr(self.c, "fetch_repo_settings", None)
        if fetch is not None:
            try:
                settings = fetch(path)
            except Exception:  # noqa: BLE001 — just means "no offer"
                settings = None
        try:
            self._branch_ready.emit(gen, branch)
            self._remote_detected.emit(gen, remote)
            self._settings_ready.emit(gen, settings)
        except RuntimeError:
            pass  # dialog closed while detecting

    def _on_remote_detected(self, gen, url):
        if gen != self._branch_gen:
            return  # the path changed after this detection was kicked off
        if url and not self.ed_remote.text().strip():
            # Pre-fill the repo's existing remote; it's already configured, so
            # treat it as verified (the user can re-verify if unsure).
            self.ed_remote.setText(url)
            self._remote_ok = True
            self._remote_msg(f"Using the repo's configured remote: {url}", ok=True)

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

    def _on_settings_ready(self, gen, overrides):
        if gen != self._branch_gen:
            return  # the path changed after this detection was kicked off
        self._inherited = overrides or None
        if not self._inherited:
            self.ck_inherit.setVisible(False)
            return
        n = len(self._inherited)
        self.ck_inherit.setText(
            f"Use the {n} setting{'s' if n != 1 else ''} saved from your other machine")
        self.ck_inherit.setToolTip(
            "This repo has options you set on another machine (inherited via the "
            "remote):\n  " + "\n  ".join(f"{k}: {v}" for k, v in
                                         sorted(self._inherited.items())) +
            "\n\nUnchecked, the repo just inherits this machine's global defaults.")
        self.ck_inherit.setChecked(True)
        self.ck_inherit.setVisible(True)

    def _hint(self, text: str):
        self.lbl_hint.setText(text)
        self.lbl_hint.setVisible(bool(text))

    # -------------------------------------------------- remote onboarding (async)
    def _remote_msg(self, text: str, ok: bool = False):
        self.lbl_remote.setText(("✓ " if ok else "") + text)
        self.lbl_remote.setVisible(bool(text))

    def _on_remote_changed(self, _text):
        # Any edit invalidates a prior verify — a stale ✓ on a changed URL is
        # worse than no mark at all.
        self._remote_ok = False
        self._remote_msg("")

    def _verify_remote(self):
        url = self.ed_remote.text().strip()
        if not url:
            self._remote_msg("Enter a remote URL first (or leave it blank for "
                             "local-only).")
            return
        path = self.ed_path.text().strip()
        if not path or not os.path.isdir(path):
            self._remote_msg("Choose the repo folder first.")
            return
        self.btn_verify.setEnabled(False)
        self._remote_msg("Checking reachability and push access…")
        self.busy.start("Checking the remote (reachability + push access)…")
        threading.Thread(
            target=self._do_verify_remote,
            args=(path, url, self.ed_branch.text().strip() or "main"),
            name="sincrogit-verify-remote", daemon=True,
        ).start()

    def _do_verify_remote(self, path, url, branch):
        try:
            ok, msg = self.c.configure_remote(path, url, branch=branch)
        except Exception as e:  # noqa: BLE001 — surfaced in the label
            ok, msg = False, str(e)
        try:
            self._remote_verified.emit(ok, msg)
        except RuntimeError:
            pass  # dialog closed while verifying

    def _on_remote_verified(self, ok, msg):
        self.busy.stop()
        self.btn_verify.setEnabled(True)
        self._remote_ok = ok
        self._remote_msg(msg, ok=ok)

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
        self.busy.start("Adding the repo…")
        overrides = (self._inherited if (self._inherited
                     and self.ck_inherit.isChecked()) else None)
        threading.Thread(
            target=self._do_add,
            args=(path, self.ed_branch.text().strip() or "main",
                  self.cb_push.isChecked(), self.cb_pull.isChecked(),
                  self.cb_norm.isChecked(), self.ed_remote.text().strip(),
                  overrides),
            name="sincrogit-add-repo", daemon=True,
        ).start()

    def _do_add(self, path, branch, push, pull, normalize_eol, remote_url,
                overrides=None):
        try:
            # A URL typed but never verified is still configured on the repo
            # here (idempotent), so 'Add' without clicking Verify does the right
            # thing. A failure blocks the add with the reason, rather than
            # silently adding a repo whose remote doesn't work.
            if remote_url and not self._remote_ok:
                ok, msg = self.c.configure_remote(path, remote_url, branch=branch)
                if not ok:
                    self._added.emit(False, f"Remote not configured: {msg}")
                    return
            ok, msg = self.c.add_repo(path, branch=branch, push=push, pull=pull,
                                      normalize_eol=normalize_eol,
                                      overrides=overrides)
        except Exception as e:  # noqa: BLE001 — surfaced in the dialog
            ok, msg = False, str(e)
        try:
            self._added.emit(ok, msg)
        except RuntimeError:
            pass  # dialog closed while adding

    def _on_added(self, ok, msg):
        self.busy.stop()
        self.btn_add.setEnabled(True)
        if ok:
            QMessageBox.information(self, "Add repo", "Repo added.")
            self.accept()
        else:
            QMessageBox.critical(self, "Add repo", msg)
