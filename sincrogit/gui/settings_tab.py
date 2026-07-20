"""Friendly Settings tab (the lazy person's alternative to raw YAML).

Master-detail, everything in ONE screen (no dialogs — Ernesto's call): the
list on the left holds **Global defaults** plus every repo; picking an item
edits it inline on the right. The global page edits `defaults:` (plus
ai/theme/log/pandoc) through spinners and toggles; a repo page is a
RepoSettingsPane — every per-repo option, each field carrying a hint of
whether it inherits the default (and which) or overrides it.

Saving rewrites the file structurally (comments are not preserved — that's the
Advanced tab's trade); changes take effect on restart, same as the YAML editor.
"""

import yaml
from PyQt5.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

# Widget idioms shared with the per-repo settings pane (kept re-exported here
# so the tests that reach in via settings_tab._select still work).
from .formwidgets import _HANDOFF, _combo, _is_disabled, _load_spin, _select, _spin
from .repo_settings_pane import RepoSettingsPane

# Combo entries: (stored value, human label)
# Permanent-history mode, framed by RESULT — not the "purist/pragmatic" jargon,
# which means nothing to a GUI-first user. "auto" = auto-seal on an interval;
# "manual" = seal_interval_min: inf (only the user's own Smart Commits land).
_HISTORY_MODES = [
    ("auto", "Automatic checkpoints (recommended)"),
    ("manual", "Only my own commits (I seal by hand)"),
]
_AI_MODES = [
    ("hybrid", "Hybrid (local Ollama, cloud fallback)"),
    ("local", "Local only (Ollama)"),
    ("cloud", "Cloud only (Gemini)"),
    ("none", "Off (deterministic messages)"),
]
_LANGS = [("en", "English"), ("es", "Español")]
_THEMES = [("auto", "Auto (follow Windows)"), ("light", "Light"), ("dark", "Dark")]
_LOG_LEVELS = ["DEBUG", "INFO", "WARNING", "ERROR"]


class SettingsTab(QWidget):
    """Master-detail settings. Duck-typed controller: config_text(),
    save_config(text) -> (ok, msg), restart() — and, for the per-repo pages,
    repo_list() plus the RepoSettingsPane contract (repo_config_view,
    update/reset/remove_repo_config). Controllers without repo_list (tests)
    simply get the global page alone."""

    def __init__(self, controller, parent=None):
        super().__init__(parent)
        self.c = controller

        outer = QHBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(8)
        # Master list: the global defaults + one entry per repo. Selecting a
        # repo edits ITS settings right here, inline — no window ever opens.
        self.lst = QListWidget()
        self.lst.setMaximumWidth(190)
        self.lst.currentRowChanged.connect(self._on_pick)
        outer.addWidget(self.lst)
        self.stack = QStackedWidget()
        outer.addWidget(self.stack, 1)

        # ---------------- page 0: the global defaults form (as always) -------
        page = QWidget()
        pv = QVBoxLayout(page)
        pv.setContentsMargins(0, 0, 0, 0)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.NoFrame)
        body = QWidget()
        v = QVBoxLayout(body)
        v.setContentsMargins(12, 12, 12, 12)
        v.setSpacing(10)

        # ------------------------------------------------------------ rhythms
        g1 = QGroupBox("Rhythms (how often things happen)")
        f1 = QFormLayout(g1)
        self.sp_snapshot = _spin(QSpinBox())
        self.sp_snapshot.setRange(10, 3600)
        self.sp_snapshot.setSingleStep(10)
        self.sp_snapshot.setSuffix(" s")
        self.sp_snapshot.setToolTip("Time-machine granularity: how often the WIP is amended.")
        f1.addRow("Snapshot every", self.sp_snapshot)

        self.cb_history = _combo(_HISTORY_MODES)
        self.cb_history.setToolTip("Who writes the permanent commits on your branch.")
        self.cb_history.currentIndexChanged.connect(self._history_mode_changed)
        f1.addRow("Permanent history", self.cb_history)

        self.sp_seal = _spin(QSpinBox())
        self.sp_seal.setRange(5, 2880)
        self.sp_seal.setSingleStep(30)
        self.sp_seal.setSuffix(" min")
        self.sp_seal.setToolTip("How often SincroGit adds an automatic checkpoint commit (and pushes).")
        f1.addRow("Automatic checkpoint every", self.sp_seal)

        self.ck_leave = QCheckBox("Seal when I lock the machine and LEAVE (leave seal)")
        self.ck_leave.setToolTip(
            "Lock (Win+L) and stay away this long → the pending work is sealed and "
            "pushed, so your other machine pulls a fresh branch. Coming back earlier "
            "cancels it; if the machine is about to sleep, it seals just before. "
            "Ignored in 'Only my own commits' (purist) mode.")
        self.ck_leave.toggled.connect(lambda on: self.sp_leave.setEnabled(on))
        f1.addRow(self.ck_leave)
        self.sp_leave = _spin(QSpinBox())
        self.sp_leave.setRange(1, 240)
        self.sp_leave.setSuffix(" min")
        f1.addRow("Seal after being away for", self.sp_leave)

        self.ck_nudge = QCheckBox("Remind me to commit when work piles up (a quiet moment, once a day at most)")
        self.ck_nudge.setToolTip(
            "Only for 'Only my own commits': if un-sealed work sits on a stagnant "
            "branch, SincroGit nudges you once to Smart Commit. Your work is backed up "
            "regardless — this only keeps your branch history current.")
        f1.addRow(self.ck_nudge)

        self.lbl_history = QLabel(
            "Automatic: a permanent commit lands on your branch periodically — a real "
            "history with zero effort (recommended). Only my own: the branch stays exactly "
            "as you commit it (Smart Commit); the automatic saves keep running underneath "
            "(time machine, backup, cross-machine handoff), just not on the branch.")
        self.lbl_history.setWordWrap(True)
        self.lbl_history.setProperty("cssClass", "muted")
        f1.addRow(self.lbl_history)
        v.addWidget(g1)

        # --------------------------------------------------------------- sync
        g2 = QGroupBox("Backup && sync")
        f2 = QFormLayout(g2)
        self.ck_autosnap = QCheckBox("Mirror the latest state to the remote (autosnap)")
        self.ck_autosnap.setToolTip("Disk-failure recovery + cross-machine handoff substrate.")
        self.ck_autosnap.toggled.connect(lambda on: self.sp_autosnap.setEnabled(on))
        f2.addRow(self.ck_autosnap)
        self.sp_autosnap = _spin(QSpinBox())
        self.sp_autosnap.setRange(2, 240)
        self.sp_autosnap.setSuffix(" min")
        f2.addRow("Mirror every", self.sp_autosnap)

        self.ck_push = QCheckBox("Push sealed commits to the remote")
        f2.addRow(self.ck_push)
        self.ck_pull = QCheckBox("Pull from the remote periodically")
        self.ck_pull.toggled.connect(lambda on: self.sp_pull.setEnabled(on))
        f2.addRow(self.ck_pull)
        self.sp_pull = _spin(QSpinBox())
        self.sp_pull.setRange(1, 240)
        self.sp_pull.setSuffix(" min")
        f2.addRow("Check the remote every", self.sp_pull)

        self.cb_handoff = _combo(_HANDOFF)
        self.cb_handoff.setToolTip("Pick up your OTHER machine's live work when it's loss-free.")
        f2.addRow("Cross-machine handoff", self.cb_handoff)
        self.ck_track = QCheckBox("Follow the current branch (feature-branch workflow)")
        self.ck_track.setToolTip("Off = pause when you checkout another branch (the safe default).")
        f2.addRow(self.ck_track)
        self.ck_suggest = QCheckBox("Suggest excluding high-churn folders (Smart Ignore)")
        f2.addRow(self.ck_suggest)
        v.addWidget(g2)

        # ----------------------------------------------------------------- AI
        g3 = QGroupBox("AI commit messages")
        f3 = QFormLayout(g3)
        self.cb_ai = _combo(_AI_MODES)
        f3.addRow("Mode", self.cb_ai)
        self.cb_lang = _combo(_LANGS)
        f3.addRow("Message language", self.cb_lang)
        self.ck_content = QCheckBox("Allow sending diff CONTENT to the cloud (off = names + stats only)")
        f3.addRow(self.ck_content)
        v.addWidget(g3)

        # ----------------------------------------------------------- app/misc
        g4 = QGroupBox("Appearance && system")
        f4 = QFormLayout(g4)
        self.cb_theme = _combo(_THEMES)
        f4.addRow("Theme", self.cb_theme)
        self.ed_pandoc = QLineEdit()
        self.ed_pandoc.setPlaceholderText("pandoc  (or a full path, e.g. C:/tools/pandoc.exe)")
        f4.addRow("Pandoc path (.docx diffs)", self.ed_pandoc)
        self.cb_log = QComboBox()
        self.cb_log.addItems(_LOG_LEVELS)
        f4.addRow("Log level", self.cb_log)
        v.addWidget(g4)

        note = QLabel(
            "These are the GLOBAL defaults — every repo inherits them unless it "
            "overrides a field (pick a repo on the left to see and edit ITS "
            "settings, with each override marked). Saving from here rewrites the "
            "file, so YAML comments are kept only when editing the Advanced tab."
        )
        note.setWordWrap(True)
        note.setProperty("cssClass", "muted")
        v.addWidget(note)
        v.addStretch(1)

        scroll.setWidget(body)
        pv.addWidget(scroll, 1)

        row = QHBoxLayout()
        row.addStretch(1)
        self.btn_reload = QPushButton("Reload")
        self.btn_reload.clicked.connect(self.load_values)
        row.addWidget(self.btn_reload)
        self.btn_save = QPushButton("Save")
        self.btn_save.clicked.connect(lambda: self._save(restart=False))
        row.addWidget(self.btn_save)
        self.btn_save_restart = QPushButton("Save and restart")
        self.btn_save_restart.setProperty("cssClass", "primary")
        self.btn_save_restart.clicked.connect(lambda: self._save(restart=True))
        row.addWidget(self.btn_save_restart)
        pv.addLayout(row)
        self.stack.addWidget(page)

        # -------- page 1: the selected repo's pane (rebuilt fresh per pick) --
        self._repo_host = QWidget()
        self._repo_v = QVBoxLayout(self._repo_host)
        self._repo_v.setContentsMargins(0, 0, 0, 0)
        self._pane = None
        self.stack.addWidget(self._repo_host)

        self._sync_repos()
        self.load_values()

    # ------------------------------------------------------ repo master list
    def showEvent(self, e):
        super().showEvent(e)
        # Re-entering the tab is a fresh "visit": the shown repo's pane must
        # re-read entry/effective/defaults (edits from Advanced YAML or a
        # restart happened while we weren't looking). _sync_repos only rebuilds
        # when the repo LIST changed, so force the re-pick otherwise.
        if not self._sync_repos() and self.lst.currentRow() > 0:
            self._on_pick(self.lst.currentRow())

    def _sync_repos(self) -> bool:
        """Refresh the master list; True if it changed (and re-picked)."""
        lister = getattr(self.c, "repo_list", None)
        names = [n for n, _p in lister()] if lister else []
        want = ["Global defaults"] + names
        have = [self.lst.item(i).text() for i in range(self.lst.count())]
        if want == have:
            return False
        cur = self.lst.currentItem().text() if self.lst.currentItem() else ""
        self.lst.blockSignals(True)
        self.lst.clear()
        self.lst.addItems(want)
        self.lst.setCurrentRow(want.index(cur) if cur in want else 0)
        self.lst.blockSignals(False)
        self._on_pick(self.lst.currentRow())
        return True

    def _on_pick(self, row):
        if row <= 0:
            self.stack.setCurrentIndex(0)
            return
        self._build_pane(self.lst.item(row).text())
        self.stack.setCurrentIndex(1)

    def _build_pane(self, name):
        """A FRESH pane per visit: it re-reads entry/effective/defaults, so
        edits made meanwhile (global form, Advanced YAML, a reset) show up."""
        if self._pane is not None:
            self._repo_v.removeWidget(self._pane)
            self._pane.deleteLater()
        self._pane = RepoSettingsPane(self.c, name)
        self._pane.removed.connect(lambda: self.lst.setCurrentRow(0))
        self._pane.reset_done.connect(lambda n=name: self._build_pane(n))
        self._repo_v.addWidget(self._pane)

    def select_repo(self, name):
        """Jump straight to one repo's settings (Status's Properties… and the
        context menu land here — inline, never a window)."""
        self._sync_repos()
        for i in range(self.lst.count()):
            if self.lst.item(i).text() == name:
                if self.lst.currentRow() == i:
                    # setCurrentRow on the already-current row emits nothing —
                    # rebuild explicitly so the pane isn't a stale leftover.
                    self._on_pick(i)
                else:
                    self.lst.setCurrentRow(i)
                return

    def _history_mode_changed(self):
        """The checkpoint interval only applies to automatic mode; the commit
        reminder only applies to 'only my own commits'."""
        auto = self.cb_history.currentData() == "auto"
        self.sp_seal.setEnabled(auto)
        self.ck_nudge.setEnabled(not auto)

    # ------------------------------------------------------------------ load
    def _raw(self):
        """config.yaml parsed as a dict, or None when it can't be trusted
        (unreadable file, invalid YAML, or a non-mapping root). _save refuses
        on None: rebuilding from an empty dict would silently write a config
        WITHOUT the `repos:` section."""
        try:
            raw = yaml.safe_load(self.c.config_text())
        except yaml.YAMLError:
            return None
        return raw if isinstance(raw, dict) else None

    def load_values(self):
        raw = self._raw() or {}
        d = raw.get("defaults") or {}
        ai = raw.get("ai") or {}
        logc = raw.get("log") or {}

        _load_spin(self.sp_snapshot, d.get("snapshot_interval_sec", 300), 300)

        seal = d.get("seal_interval_min", 360)
        purist = _is_disabled(seal)
        _select(self.cb_history, "manual" if purist else "auto")
        self.sp_seal.setValue(360 if purist else int(seal))
        leave = d.get("seal_on_leave_min", 20)
        self.ck_leave.setChecked(not _is_disabled(leave))
        self.sp_leave.setValue(int(leave) if not _is_disabled(leave) else 20)
        self.sp_leave.setEnabled(not _is_disabled(leave))

        self.ck_nudge.setChecked(bool(d.get("suggest_commit", True)))
        self._history_mode_changed()  # sync enable/disable to the selected mode

        auto = d.get("autosnap", True)
        self.ck_autosnap.setChecked(bool(auto))
        _load_spin(self.sp_autosnap, d.get("autosnap_interval_min", 30), 30)
        self.sp_autosnap.setEnabled(bool(auto))

        self.ck_push.setChecked(bool(d.get("push", True)))
        pull = bool(d.get("pull", True))
        self.ck_pull.setChecked(pull)
        _load_spin(self.sp_pull, d.get("pull_interval_min", 10), 10)
        self.sp_pull.setEnabled(pull)

        handoff = d.get("live_handoff", "auto")
        handoff = "auto" if handoff is True else ("off" if handoff in (False, None) else str(handoff))
        _select(self.cb_handoff, handoff if handoff in ("auto", "ask", "off") else "auto")
        self.ck_track.setChecked(bool(d.get("track_current_branch", False)))
        self.ck_suggest.setChecked(bool(d.get("suggest_excludes", True)))

        _select(self.cb_ai, str(ai.get("mode", "hybrid")))
        _select(self.cb_lang, str(ai.get("language", "en")))
        self.ck_content.setChecked(bool(ai.get("cloud_send_content", False)))

        _select(self.cb_theme, str(raw.get("theme", "auto")))
        self.ed_pandoc.setText(str(raw.get("pandoc_path", "pandoc")))
        lvl = str(logc.get("level", "INFO")).upper()
        self.cb_log.setCurrentText(lvl if lvl in _LOG_LEVELS else "INFO")

    # ------------------------------------------------------------------ save
    def _save(self, restart: bool):
        raw = self._raw()
        if raw is None:
            QMessageBox.critical(
                self, "Settings",
                "config.yaml could not be read or parsed, so saving this form "
                "would rewrite it without your repos. Fix the file in the "
                "Advanced (YAML) tab first, then save here.")
            return
        d = raw.setdefault("defaults", {})
        d["snapshot_interval_sec"] = self.sp_snapshot.value()
        manual = self.cb_history.currentData() == "manual"
        d["seal_interval_min"] = "inf" if manual else self.sp_seal.value()
        d["seal_on_leave_min"] = (self.sp_leave.value()
                                  if self.ck_leave.isChecked() else "off")
        d["suggest_commit"] = self.ck_nudge.isChecked()
        d["autosnap"] = self.ck_autosnap.isChecked()
        d["autosnap_interval_min"] = self.sp_autosnap.value()
        d["push"] = self.ck_push.isChecked()
        d["pull"] = self.ck_pull.isChecked()
        d["pull_interval_min"] = self.sp_pull.value()
        d["live_handoff"] = self.cb_handoff.currentData()
        d["track_current_branch"] = self.ck_track.isChecked()
        d["suggest_excludes"] = self.ck_suggest.isChecked()

        ai = raw.setdefault("ai", {})
        ai["mode"] = self.cb_ai.currentData()
        ai["language"] = self.cb_lang.currentData()
        ai["cloud_send_content"] = self.ck_content.isChecked()

        raw["theme"] = self.cb_theme.currentData()
        raw["pandoc_path"] = self.ed_pandoc.text().strip() or "pandoc"
        raw.setdefault("log", {})["level"] = self.cb_log.currentText()

        text = yaml.safe_dump(raw, sort_keys=False, allow_unicode=True, default_flow_style=False)
        ok, msg = self.c.save_config(text)
        if not ok:
            QMessageBox.critical(self, "Settings", f"Could not save: {msg}")
            return
        if restart:
            self.c.restart()
        else:
            QMessageBox.information(self, "Settings", "Saved. Restart to apply ('Save and restart').")
