"""Per-repo Properties dialog (the friendly alternative to editing YAML).

Covers EVERY per-repo option (each inheritable key of RepoConfig), and next to
each field says where its value comes from: "default (X)" when the repo
inherits the global Settings, or "override — default: X" when this repo's
entry pins it. Writes back ONLY the fields you actually change, as explicit
keys of that repo's entry — everything you don't touch keeps inheriting; and
"Use defaults…" drops every override at once, returning the repo to pure
inheritance. Same trades as Add repo / the Settings form: comments inside the
`repos:` section are rewritten, and changes apply on restart.

Talks to the app through the `controller`:
  repo_config_view(name) -> (entry_dict, effective_dict, defaults_dict)
  update_repo_config(name, changes) -> (ok, msg)
  reset_repo_config(name) -> (ok, msg)
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


def _fmt_default(key: str, v) -> str:
    """A default value, human-shaped for the provenance hint: sentinels read
    'off', byte sizes read KB/MB, pattern lists read as a count."""
    if _is_disabled(v):
        return "off"
    if isinstance(v, bool):
        return "on" if v else "off"
    if key == "max_file_bytes":
        return f"{int(v) // 1024} KB"
    if key == "max_include_bytes":
        return f"{int(v) // (1024 * 1024)} MB"
    if isinstance(v, (list, tuple)):
        n = len(v)
        return f"{n} pattern(s)" if n else "none"
    return str(v)


class RepoPropertiesDialog(QDialog):
    def __init__(self, controller, repo_name, parent=None):
        super().__init__(parent)
        self.c = controller
        self.name = repo_name
        self.setWindowTitle(f"⏳g SincroGit — Properties ({repo_name})")
        self.resize(640, 700)

        entry, eff, defaults = self.c.repo_config_view(repo_name)
        self._found = bool(eff)
        self._entry = entry
        self._defaults = defaults
        self._hints = {}   # config key -> the QLabel showing its provenance
        pal = getattr(controller, "theme", None) or {}
        self._hint_override_css = f"color: {pal.get('accent', '#2e7dd1')};"
        self._hint_default_css = f"color: {pal.get('muted', '#6b7280')};"

        outer = QVBoxLayout(self)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.NoFrame)
        body = QWidget()
        v = QVBoxLayout(body)
        v.setSpacing(10)

        def row(key, widget):
            """The field plus its provenance hint ('default (X)' vs
            'override — default: X'), filled by _update_hints."""
            box = QWidget()
            h = QHBoxLayout(box)
            h.setContentsMargins(0, 0, 0, 0)
            h.addWidget(widget)
            lbl = QLabel("")
            lbl.setProperty("cssClass", "muted")
            self._hints[key] = lbl
            h.addWidget(lbl)
            h.addStretch(1)
            return box

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
        f0.addRow(row("track_current_branch", self.ck_track))
        v.addWidget(g0)

        # ------------------------------------------------------------ rhythms
        g1 = QGroupBox("Rhythms")
        f1 = QFormLayout(g1)
        self.sp_snapshot = _spin(QSpinBox())
        self.sp_snapshot.setRange(10, 3600)
        self.sp_snapshot.setSingleStep(10)
        self.sp_snapshot.setSuffix(" s")
        f1.addRow("Snapshot every", row("snapshot_interval_sec", self.sp_snapshot))
        self.sp_debounce = _spin(QSpinBox())
        self.sp_debounce.setRange(2, 600)
        self.sp_debounce.setSuffix(" s")
        self.sp_debounce.setToolTip(
            "Wait for the last change to settle before snapshotting — lower it "
            "for an AI-agent repo (bursts settle fast).")
        f1.addRow("Settle (debounce)", row("debounce_sec", self.sp_debounce))
        self.ck_purist = QCheckBox("Purist mode — never auto-seal; I commit by hand")
        self.ck_purist.toggled.connect(self._purist_toggled)
        f1.addRow(row("seal_interval_min", self.ck_purist))
        self.sp_seal = _spin(QSpinBox())
        self.sp_seal.setRange(5, 2880)
        self.sp_seal.setSingleStep(30)
        self.sp_seal.setSuffix(" min")
        f1.addRow("Auto-seal every", self.sp_seal)
        self.ck_leave = QCheckBox("Leave seal — seal + push after locking and staying away")
        self.ck_leave.setToolTip(
            "Lock (Win+L) and stay away this long → the pending work is sealed "
            "and pushed, so your other machine pulls a fresh branch. Ignored in "
            "purist mode.")
        self.ck_leave.toggled.connect(lambda on: self.sp_leave.setEnabled(on))
        f1.addRow(row("seal_on_leave_min", self.ck_leave))
        self.sp_leave = _spin(QSpinBox())
        self.sp_leave.setRange(1, 240)
        self.sp_leave.setSuffix(" min")
        f1.addRow("Seal after being away for", self.sp_leave)
        v.addWidget(g1)

        # --------------------------------------------------------------- sync
        g2 = QGroupBox("Backup && sync")
        f2 = QFormLayout(g2)
        self.ck_push = QCheckBox("Push sealed commits to the remote")
        f2.addRow(row("push", self.ck_push))
        self.ck_pull = QCheckBox("Pull from the remote periodically")
        self.ck_pull.toggled.connect(lambda on: self.sp_pull.setEnabled(on))
        f2.addRow(row("pull", self.ck_pull))
        self.sp_pull = _spin(QSpinBox())
        self.sp_pull.setRange(1, 240)
        self.sp_pull.setSuffix(" min")
        f2.addRow("Check the remote every", row("pull_interval_min", self.sp_pull))
        self.ck_autosnap = QCheckBox("Mirror the latest state to the remote (autosnap)")
        self.ck_autosnap.toggled.connect(lambda on: self.sp_autosnap.setEnabled(on))
        f2.addRow(row("autosnap", self.ck_autosnap))
        self.sp_autosnap = _spin(QSpinBox())
        self.sp_autosnap.setRange(2, 240)
        self.sp_autosnap.setSuffix(" min")
        f2.addRow("Mirror every", row("autosnap_interval_min", self.sp_autosnap))
        self.cb_handoff = _combo(_HANDOFF)
        f2.addRow("Cross-machine handoff", row("live_handoff", self.cb_handoff))
        self.sp_timeout = _spin(QSpinBox())
        self.sp_timeout.setRange(10, 600)
        self.sp_timeout.setSuffix(" s")
        self.sp_timeout.setToolTip("Limit for network git operations (fetch/push).")
        f2.addRow("Network timeout", row("git_timeout_sec", self.sp_timeout))
        v.addWidget(g2)

        # -------------------------------------------------------------- filters
        g3 = QGroupBox("File filters")
        f3 = QFormLayout(g3)
        self.sp_maxfile = _spin(QSpinBox())
        self.sp_maxfile.setRange(16, 102_400)
        self.sp_maxfile.setSingleStep(64)
        self.sp_maxfile.setSuffix(" KB")
        self.sp_maxfile.setToolTip(
            "Only text files under this size are auto-versioned; bigger ones "
            "are yours to commit by hand.")
        f3.addRow("Max auto-versioned size", row("max_file_bytes", self.sp_maxfile))
        self.ed_excludes = _patterns_edit("**/node_modules/**")
        self.ed_excludes.setToolTip("Never snapshot files matching these patterns "
                                    "(one per line, gitignore syntax).")
        f3.addRow("Exclude", row("extra_excludes", self.ed_excludes))
        self.ed_includes = _patterns_edit("**/*.docx")
        self.ed_includes.setToolTip(
            "Version these even if binary (e.g. **/*.docx gets readable pandoc "
            "diffs). One per line.")
        f3.addRow("Include binaries", row("extra_includes", self.ed_includes))
        self.sp_maxinc = _spin(QSpinBox())
        self.sp_maxinc.setRange(1, 500)
        self.sp_maxinc.setSuffix(" MB")
        self.sp_maxinc.setToolTip("Size cap for the included binaries above.")
        f3.addRow("Max included-binary size", row("max_include_bytes", self.sp_maxinc))
        v.addWidget(g3)

        # -------------------------------------------------------------- notices
        g4 = QGroupBox("Notices")
        f4 = QFormLayout(g4)
        self.ck_suggest_ex = QCheckBox("Suggest excluding high-churn folders (Smart Ignore)")
        f4.addRow(row("suggest_excludes", self.ck_suggest_ex))
        self.ck_suggest_commit = QCheckBox("Remind me to commit when work piles up (purist mode only)")
        f4.addRow(row("suggest_commit", self.ck_suggest_commit))
        v.addWidget(g4)

        note = QLabel(
            "Every value comes from the global defaults (Settings tab) unless this "
            "repo overrides it — the hint next to each field says which, and what "
            "the default is. Only the fields you change are written to this repo's "
            "entry. Changes apply when SincroGit restarts."
        )
        note.setWordWrap(True)
        note.setProperty("cssClass", "muted")
        v.addWidget(note)
        v.addStretch(1)

        scroll.setWidget(body)
        outer.addWidget(scroll, 1)

        row2 = QHBoxLayout()
        self.btn_remove = QPushButton("Remove repo…")
        self.btn_remove.setProperty("cssClass", "danger")
        self.btn_remove.setToolTip("Remove from SincroGit's config only — the git repo on disk is not touched.")
        self.btn_remove.clicked.connect(self._remove)
        row2.addWidget(self.btn_remove)
        self.btn_reset = QPushButton("Use defaults…")
        self.btn_reset.setToolTip(
            "Drop every override this repo pins, so it goes back to inheriting "
            "the global defaults (Settings tab). Branch/remote are kept.")
        self.btn_reset.clicked.connect(self._reset_overrides)
        row2.addWidget(self.btn_reset)
        row2.addStretch(1)
        self.btn_save = QPushButton("Save")
        self.btn_save.clicked.connect(lambda: self._save(restart=False))
        row2.addWidget(self.btn_save)
        self.btn_save_restart = QPushButton("Save and restart")
        self.btn_save_restart.setProperty("cssClass", "primary")
        self.btn_save_restart.clicked.connect(lambda: self._save(restart=True))
        row2.addWidget(self.btn_save_restart)
        btn_cancel = QPushButton("Cancel")
        btn_cancel.clicked.connect(self.reject)
        row2.addWidget(btn_cancel)
        outer.addLayout(row2)

        self._load(eff)
        self._update_hints()
        # The baseline for "what changed": the WIDGET values right after loading.
        # Comparing against these (not the raw config) means an untouched field is
        # never written — e.g. a disabled (inf) interval the spinner can't show
        # stays exactly as it was in the file.
        self._initial = self._values()
        override_count = sum(1 for k in self._entry if k in self._hints)
        self.btn_reset.setEnabled(self._found and override_count > 0)
        for b in (self.btn_save, self.btn_save_restart, self.btn_remove):
            b.setEnabled(self._found)

    def _purist_toggled(self, on):
        self.sp_seal.setEnabled(not on)
        # The leave seal is flat OFF in purist mode — reflect that in the form.
        self.ck_leave.setEnabled(not on)
        self.sp_leave.setEnabled(not on and self.ck_leave.isChecked())

    # ----------------------------------------------------------------- hints
    def _update_hints(self):
        """Say, next to each field, where its value comes from — and what the
        global default is, so overriding is an informed choice."""
        for key, lbl in self._hints.items():
            default = _fmt_default(key, self._defaults.get(key))
            if key in self._entry:
                lbl.setText(f"override — default: {default}")
                lbl.setStyleSheet(self._hint_override_css)
                lbl.setToolTip("This repo pins this value in its own entry. "
                               "'Use defaults…' drops every override.")
            else:
                lbl.setText(f"default ({default})")
                lbl.setStyleSheet(self._hint_default_css)
                lbl.setToolTip("Inherited from the global defaults (Settings "
                               "tab); changing it here pins it for THIS repo.")

    # ------------------------------------------------------------------ load
    def _load(self, eff: dict):
        self.ed_branch.setText(str(eff.get("branch", "main")))
        self.ed_remote.setText(str(eff.get("remote", "origin")))
        self.ck_track.setChecked(bool(eff.get("track_current_branch", False)))

        _load_spin(self.sp_snapshot, eff.get("snapshot_interval_sec", 300), 300)
        _load_spin(self.sp_debounce, eff.get("debounce_sec", 25), 25)
        seal = eff.get("seal_interval_min", 360)
        purist = _is_disabled(seal)
        self.ck_purist.setChecked(purist)
        self.sp_seal.setValue(360 if purist else int(seal))
        leave = eff.get("seal_on_leave_min", 20)
        self.ck_leave.setChecked(not _is_disabled(leave))
        _load_spin(self.sp_leave, leave, 20)
        self._purist_toggled(purist)

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
        _load_spin(self.sp_timeout, eff.get("git_timeout_sec", 60), 60)

        mf = eff.get("max_file_bytes", 1_048_576)
        self.sp_maxfile.setValue(int(mf) // 1024 if not _is_disabled(mf) else 1024)
        mi = eff.get("max_include_bytes", 26_214_400)
        self.sp_maxinc.setValue(int(mi) // (1024 * 1024) if not _is_disabled(mi) else 25)
        self.ed_excludes.setPlainText("\n".join(eff.get("extra_excludes") or []))
        self.ed_includes.setPlainText("\n".join(eff.get("extra_includes") or []))

        self.ck_suggest_ex.setChecked(bool(eff.get("suggest_excludes", True)))
        self.ck_suggest_commit.setChecked(bool(eff.get("suggest_commit", True)))

    def _values(self) -> dict:
        return {
            "branch": self.ed_branch.text().strip() or "main",
            "remote": self.ed_remote.text().strip() or "origin",
            "track_current_branch": self.ck_track.isChecked(),
            "snapshot_interval_sec": self.sp_snapshot.value(),
            "debounce_sec": self.sp_debounce.value(),
            "seal_interval_min": (math.inf if self.ck_purist.isChecked()
                                  else self.sp_seal.value()),
            "seal_on_leave_min": (self.sp_leave.value() if self.ck_leave.isChecked()
                                  else math.inf),
            "push": self.ck_push.isChecked(),
            "pull": self.ck_pull.isChecked(),
            "pull_interval_min": self.sp_pull.value(),
            "autosnap": self.ck_autosnap.isChecked(),
            "autosnap_interval_min": self.sp_autosnap.value(),
            "live_handoff": self.cb_handoff.currentData(),
            "git_timeout_sec": self.sp_timeout.value(),
            "max_file_bytes": self.sp_maxfile.value() * 1024,
            "max_include_bytes": self.sp_maxinc.value() * 1024 * 1024,
            "extra_excludes": _patterns(self.ed_excludes),
            "extra_includes": _patterns(self.ed_includes),
            "suggest_excludes": self.ck_suggest_ex.isChecked(),
            "suggest_commit": self.ck_suggest_commit.isChecked(),
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

    def _reset_overrides(self):
        overrides = sorted(k for k in self._entry if k in self._hints)
        listing = "\n".join("  • " + k for k in overrides)
        if QMessageBox.question(
            self, "Use defaults",
            f"Drop {len(overrides)} override(s) of '{self.name}' and inherit "
            f"the global defaults again?\n\n{listing}\n\nBranch and remote are "
            f"kept. Takes effect on restart.",
            QMessageBox.Yes | QMessageBox.Cancel, QMessageBox.Cancel,
        ) != QMessageBox.Yes:
            return
        ok, msg = self.c.reset_repo_config(self.name)
        if not ok:
            QMessageBox.critical(self, "Use defaults", f"Could not reset: {msg}")
            return
        QMessageBox.information(
            self, "Use defaults",
            f"{msg}. Applies when SincroGit restarts.")
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
