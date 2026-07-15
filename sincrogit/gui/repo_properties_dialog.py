"""Per-repo Properties dialog (the friendly alternative to editing YAML).

Shows the repo's EFFECTIVE settings (its explicit overrides merged over the
global defaults) and writes back ONLY the fields you actually change, as
explicit keys of that repo's entry — everything you don't touch keeps
inheriting the defaults. Same trades as Add repo / the Settings form: comments
inside the `repos:` section are rewritten, and changes apply on restart
("Save and restart").

Talks to the app through the `controller`:
  repo_config_view(name) -> (entry_dict, effective_dict)
  update_repo_config(name, changes) -> (ok, msg)
  remove_repo_config(name) -> (ok, msg)
  restart()
"""

import math

from PyQt5.QtWidgets import (
    QCheckBox,
    QDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

# Same widget idioms as the global Settings form, so the two look alike.
from .formwidgets import _HANDOFF, _combo, _is_disabled, _load_spin, _select, _spin


def _patterns_edit(placeholder: str) -> QPlainTextEdit:
    ed = QPlainTextEdit()
    ed.setPlaceholderText(placeholder)
    ed.setMaximumHeight(72)
    return ed


def _patterns(ed: QPlainTextEdit) -> list:
    return [ln.strip() for ln in ed.toPlainText().splitlines() if ln.strip()]


class RepoPropertiesDialog(QDialog):
    def __init__(self, controller, repo_name, parent=None):
        super().__init__(parent)
        self.c = controller
        self.name = repo_name
        self.setWindowTitle(f"⏳g SincroGit — Properties ({repo_name})")
        self.resize(560, 620)

        entry, eff = self.c.repo_config_view(repo_name)
        self._found = bool(eff)

        outer = QVBoxLayout(self)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.NoFrame)
        body = QWidget()
        v = QVBoxLayout(body)
        v.setSpacing(10)

        # ------------------------------------------------------------ identity
        g0 = QGroupBox("Repository")
        f0 = QFormLayout(g0)
        lbl_path = QLabel(str(entry.get("path", "")) or "—")
        lbl_path.setProperty("cssClass", "muted")
        lbl_path.setWordWrap(True)
        f0.addRow("Path", lbl_path)
        self.ed_branch = QLineEdit()
        self.ed_branch.setMaximumWidth(200)
        self.ed_branch.setToolTip("The branch SincroGit operates on (the branch guard).")
        f0.addRow("Branch", self.ed_branch)
        self.ed_remote = QLineEdit()
        self.ed_remote.setMaximumWidth(200)
        f0.addRow("Remote", self.ed_remote)
        self.ck_track = QCheckBox("Follow the current branch (feature-branch workflow)")
        self.ck_track.setToolTip("Off = pause when you checkout another branch (the safe default).")
        f0.addRow(self.ck_track)
        v.addWidget(g0)

        # ------------------------------------------------------------ rhythms
        g1 = QGroupBox("Rhythms")
        f1 = QFormLayout(g1)
        self.sp_snapshot = _spin(QSpinBox())
        self.sp_snapshot.setRange(10, 3600)
        self.sp_snapshot.setSingleStep(10)
        self.sp_snapshot.setSuffix(" s")
        f1.addRow("Snapshot every", self.sp_snapshot)
        self.ck_purist = QCheckBox("Purist mode — never auto-seal; I commit by hand")
        self.ck_purist.toggled.connect(lambda on: self.sp_seal.setEnabled(not on))
        f1.addRow(self.ck_purist)
        self.sp_seal = _spin(QSpinBox())
        self.sp_seal.setRange(5, 2880)
        self.sp_seal.setSingleStep(30)
        self.sp_seal.setSuffix(" min")
        f1.addRow("Auto-seal every", self.sp_seal)
        v.addWidget(g1)

        # --------------------------------------------------------------- sync
        g2 = QGroupBox("Backup && sync")
        f2 = QFormLayout(g2)
        self.ck_push = QCheckBox("Push sealed commits to the remote")
        f2.addRow(self.ck_push)
        self.ck_pull = QCheckBox("Pull from the remote periodically")
        self.ck_pull.toggled.connect(lambda on: self.sp_pull.setEnabled(on))
        f2.addRow(self.ck_pull)
        self.sp_pull = _spin(QSpinBox())
        self.sp_pull.setRange(1, 240)
        self.sp_pull.setSuffix(" min")
        f2.addRow("Check the remote every", self.sp_pull)
        self.ck_autosnap = QCheckBox("Mirror the latest state to the remote (autosnap)")
        self.ck_autosnap.toggled.connect(lambda on: self.sp_autosnap.setEnabled(on))
        f2.addRow(self.ck_autosnap)
        self.sp_autosnap = _spin(QSpinBox())
        self.sp_autosnap.setRange(2, 240)
        self.sp_autosnap.setSuffix(" min")
        f2.addRow("Mirror every", self.sp_autosnap)
        self.cb_handoff = _combo(_HANDOFF)
        f2.addRow("Cross-machine handoff", self.cb_handoff)
        v.addWidget(g2)

        # -------------------------------------------------------------- filters
        g3 = QGroupBox("File filters (one pattern per line, gitignore syntax)")
        f3 = QFormLayout(g3)
        self.ed_excludes = _patterns_edit("**/node_modules/**")
        self.ed_excludes.setToolTip("Never snapshot files matching these patterns.")
        f3.addRow("Exclude", self.ed_excludes)
        self.ed_includes = _patterns_edit("**/*.docx")
        self.ed_includes.setToolTip(
            "Version these even if binary (e.g. **/*.docx gets readable pandoc diffs)."
        )
        f3.addRow("Include binaries", self.ed_includes)
        v.addWidget(g3)

        note = QLabel(
            "Only the fields you change are written to this repo's entry — the rest "
            "keep inheriting the global defaults (Settings tab). Changes apply when "
            "SincroGit restarts."
        )
        note.setWordWrap(True)
        note.setProperty("cssClass", "muted")
        v.addWidget(note)
        v.addStretch(1)

        scroll.setWidget(body)
        outer.addWidget(scroll, 1)

        row = QHBoxLayout()
        self.btn_remove = QPushButton("Remove repo…")
        self.btn_remove.setProperty("cssClass", "danger")
        self.btn_remove.setToolTip("Remove from SincroGit's config only — the git repo on disk is not touched.")
        self.btn_remove.clicked.connect(self._remove)
        row.addWidget(self.btn_remove)
        row.addStretch(1)
        self.btn_save = QPushButton("Save")
        self.btn_save.clicked.connect(lambda: self._save(restart=False))
        row.addWidget(self.btn_save)
        self.btn_save_restart = QPushButton("Save and restart")
        self.btn_save_restart.setProperty("cssClass", "primary")
        self.btn_save_restart.clicked.connect(lambda: self._save(restart=True))
        row.addWidget(self.btn_save_restart)
        btn_cancel = QPushButton("Cancel")
        btn_cancel.clicked.connect(self.reject)
        row.addWidget(btn_cancel)
        outer.addLayout(row)

        self._load(eff)
        # The baseline for "what changed": the WIDGET values right after loading.
        # Comparing against these (not the raw config) means an untouched field is
        # never written — e.g. a disabled (inf) interval the spinner can't show
        # stays exactly as it was in the file.
        self._initial = self._values()
        for b in (self.btn_save, self.btn_save_restart, self.btn_remove):
            b.setEnabled(self._found)

    # ------------------------------------------------------------------ load
    def _load(self, eff: dict):
        self.ed_branch.setText(str(eff.get("branch", "main")))
        self.ed_remote.setText(str(eff.get("remote", "origin")))
        self.ck_track.setChecked(bool(eff.get("track_current_branch", False)))

        _load_spin(self.sp_snapshot, eff.get("snapshot_interval_sec", 300), 300)
        seal = eff.get("seal_interval_min", 360)
        purist = _is_disabled(seal)
        self.ck_purist.setChecked(purist)
        self.sp_seal.setValue(360 if purist else int(seal))
        self.sp_seal.setEnabled(not purist)

        self.ck_push.setChecked(bool(eff.get("push", True)))
        pull = bool(eff.get("pull", True))
        self.ck_pull.setChecked(pull)
        _load_spin(self.sp_pull, eff.get("pull_interval_min", 10), 10)
        self.sp_pull.setEnabled(pull)
        auto = bool(eff.get("autosnap", True))
        self.ck_autosnap.setChecked(auto)
        _load_spin(self.sp_autosnap, eff.get("autosnap_interval_min", 30), 30)
        self.sp_autosnap.setEnabled(auto)
        _select(self.cb_handoff, str(eff.get("live_handoff", "auto")))

        self.ed_excludes.setPlainText("\n".join(eff.get("extra_excludes") or []))
        self.ed_includes.setPlainText("\n".join(eff.get("extra_includes") or []))

    def _values(self) -> dict:
        return {
            "branch": self.ed_branch.text().strip() or "main",
            "remote": self.ed_remote.text().strip() or "origin",
            "track_current_branch": self.ck_track.isChecked(),
            "snapshot_interval_sec": self.sp_snapshot.value(),
            "seal_interval_min": (math.inf if self.ck_purist.isChecked()
                                  else self.sp_seal.value()),
            "push": self.ck_push.isChecked(),
            "pull": self.ck_pull.isChecked(),
            "pull_interval_min": self.sp_pull.value(),
            "autosnap": self.ck_autosnap.isChecked(),
            "autosnap_interval_min": self.sp_autosnap.value(),
            "live_handoff": self.cb_handoff.currentData(),
            "extra_excludes": _patterns(self.ed_excludes),
            "extra_includes": _patterns(self.ed_includes),
        }

    # ------------------------------------------------------------------ save
    def _save(self, restart: bool):
        current = self._values()
        changes = {k: v for k, v in current.items() if v != self._initial.get(k)}
        if not changes:
            QMessageBox.information(self, "Repo properties", "Nothing changed.")
            return
        ok, msg = self.c.update_repo_config(self.name, changes)
        if not ok:
            QMessageBox.critical(
                self, "Repo properties",
                f"Could not save: {msg}\n\nYou can still edit this repo's entry "
                f"in the Advanced (YAML) tab.",
            )
            return
        if restart:
            self.c.restart()
        else:
            QMessageBox.information(
                self, "Repo properties",
                "Saved. Changes apply when SincroGit restarts ('Save and restart').",
            )
            self.accept()

    def _remove(self):
        if QMessageBox.warning(
            self, "Remove repo",
            f"Remove '{self.name}' from SincroGit?\n\nOnly the config entry is "
            f"removed — the git repository on disk is NOT touched. Takes effect "
            f"on restart.",
            QMessageBox.Yes | QMessageBox.Cancel, QMessageBox.Cancel,
        ) != QMessageBox.Yes:
            return
        ok, msg = self.c.remove_repo_config(self.name)
        if not ok:
            QMessageBox.critical(self, "Remove repo", f"Could not remove: {msg}")
            return
        QMessageBox.information(
            self, "Remove repo",
            "Removed from the config. It disappears when SincroGit restarts.",
        )
        self.accept()
