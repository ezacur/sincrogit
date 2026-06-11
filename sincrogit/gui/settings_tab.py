"""Friendly Settings form (the lazy person's alternative to raw YAML).

Edits the GLOBAL `defaults:` (plus ai/theme/log/pandoc) of config.yaml through
spinners and toggles, with the headline modes (purist, autosnap, handoff) as
plain checkboxes/combos. Per-repo overrides intentionally stay in the Advanced
(YAML) tab — they're the power-user path.

Saving rewrites the file structurally (comments are not preserved — that's the
Advanced tab's trade); changes take effect on restart, same as the YAML editor.
"""

import math

import yaml
from PyQt5.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

# Combo entries: (stored value, human label)
_HANDOFF = [
    ("auto", "Automatic (fast-forward + notify)"),
    ("ask", "Ask me (notify + one-click Apply)"),
    ("off", "Off (manual only)"),
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


def _combo(pairs) -> QComboBox:
    cb = QComboBox()
    for value, label in pairs:
        cb.addItem(label, value)
    return cb


def _select(cb: QComboBox, value):
    i = cb.findData(value)
    cb.setCurrentIndex(i if i >= 0 else 0)


def _is_disabled(value) -> bool:
    """Is an interval stored as a disable sentinel (inf/off/none/never/...)?"""
    if value is None or value is False:
        return True
    if isinstance(value, float) and math.isinf(value):
        return True
    return isinstance(value, str) and value.strip().lower() in (
        "inf", "infinity", "none", "never", "off", "false", "disabled")


class SettingsTab(QWidget):
    """Form over config.yaml's global keys. Duck-typed controller: config_text(),
    save_config(text) -> (ok, msg), restart()."""

    def __init__(self, controller, parent=None):
        super().__init__(parent)
        self.c = controller

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
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
        self.sp_snapshot = QSpinBox()
        self.sp_snapshot.setRange(10, 3600)
        self.sp_snapshot.setSingleStep(10)
        self.sp_snapshot.setSuffix(" s")
        self.sp_snapshot.setToolTip("Time-machine granularity: how often the WIP is amended.")
        f1.addRow("Snapshot every", self.sp_snapshot)

        self.ck_purist = QCheckBox("Purist mode — never auto-seal; I commit by hand (Smart Commit)")
        self.ck_purist.toggled.connect(lambda on: self.sp_seal.setEnabled(not on))
        f1.addRow(self.ck_purist)
        self.sp_seal = QSpinBox()
        self.sp_seal.setRange(5, 2880)
        self.sp_seal.setSingleStep(30)
        self.sp_seal.setSuffix(" min")
        self.sp_seal.setToolTip("How often the WIP becomes a permanent commit (and is pushed).")
        f1.addRow("Auto-seal every", self.sp_seal)
        v.addWidget(g1)

        # --------------------------------------------------------------- sync
        g2 = QGroupBox("Backup && sync")
        f2 = QFormLayout(g2)
        self.ck_autosnap = QCheckBox("Mirror the latest state to the remote (autosnap)")
        self.ck_autosnap.setToolTip("Disk-failure recovery + cross-machine handoff substrate.")
        self.ck_autosnap.toggled.connect(lambda on: self.sp_autosnap.setEnabled(on))
        f2.addRow(self.ck_autosnap)
        self.sp_autosnap = QSpinBox()
        self.sp_autosnap.setRange(2, 240)
        self.sp_autosnap.setSuffix(" min")
        f2.addRow("Mirror every", self.sp_autosnap)

        self.ck_push = QCheckBox("Push sealed commits to the remote")
        f2.addRow(self.ck_push)
        self.ck_pull = QCheckBox("Pull from the remote periodically")
        self.ck_pull.toggled.connect(lambda on: self.sp_pull.setEnabled(on))
        f2.addRow(self.ck_pull)
        self.sp_pull = QSpinBox()
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
            "These are the GLOBAL defaults — every repo inherits them. Per-repo overrides "
            "(a \"hot\" repo, another branch…) live in the Advanced (YAML) tab. Saving from "
            "here rewrites the file, so YAML comments are kept only when editing there."
        )
        note.setWordWrap(True)
        note.setProperty("cssClass", "muted")
        v.addWidget(note)
        v.addStretch(1)

        scroll.setWidget(body)
        outer.addWidget(scroll, 1)

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
        outer.addLayout(row)

        self.load_values()

    # ------------------------------------------------------------------ load
    def _raw(self) -> dict:
        try:
            return yaml.safe_load(self.c.config_text()) or {}
        except yaml.YAMLError:
            return {}

    def load_values(self):
        raw = self._raw()
        d = raw.get("defaults") or {}
        ai = raw.get("ai") or {}
        logc = raw.get("log") or {}

        snap = d.get("snapshot_interval_sec", 300)
        self.sp_snapshot.setValue(int(snap) if not _is_disabled(snap) else 300)

        seal = d.get("seal_interval_min", 360)
        purist = _is_disabled(seal)
        self.ck_purist.setChecked(purist)
        self.sp_seal.setValue(360 if purist else int(seal))
        self.sp_seal.setEnabled(not purist)

        auto = d.get("autosnap", True)
        self.ck_autosnap.setChecked(bool(auto))
        a_int = d.get("autosnap_interval_min", 30)
        self.sp_autosnap.setValue(int(a_int) if not _is_disabled(a_int) else 30)
        self.sp_autosnap.setEnabled(bool(auto))

        self.ck_push.setChecked(bool(d.get("push", True)))
        pull = bool(d.get("pull", True))
        self.ck_pull.setChecked(pull)
        p_int = d.get("pull_interval_min", 10)
        self.sp_pull.setValue(int(p_int) if not _is_disabled(p_int) else 10)
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
        d = raw.setdefault("defaults", {})
        d["snapshot_interval_sec"] = self.sp_snapshot.value()
        d["seal_interval_min"] = "inf" if self.ck_purist.isChecked() else self.sp_seal.value()
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
