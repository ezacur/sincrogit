"""Settings tab (offscreen): the result-framed permanent-history selector and the
purist commit-reminder toggle round-trip through config.yaml correctly."""

import yaml

import sincrogit.gui.settings_tab as stab


class Ctl:
    def __init__(self, text):
        self._text = text
        self.saved = None

    def config_text(self):
        return self._text

    def save_config(self, text):
        self.saved = text
        self._text = text
        return True, ""

    def restart(self):
        pass


def _defaults(text):
    return yaml.safe_load(text)["defaults"]


def test_history_selector_loads_purist(qapp):
    ctl = Ctl("defaults:\n  seal_interval_min: inf\n  suggest_commit: true\n")
    t = stab.SettingsTab(ctl)
    assert t.cb_history.currentData() == "manual"
    assert not t.sp_seal.isEnabled()      # no interval in manual mode
    assert t.ck_nudge.isEnabled()          # the reminder applies here
    assert t.ck_nudge.isChecked()


def test_history_selector_loads_automatic(qapp):
    ctl = Ctl("defaults:\n  seal_interval_min: 360\n")
    t = stab.SettingsTab(ctl)
    assert t.cb_history.currentData() == "auto"
    assert t.sp_seal.isEnabled() and t.sp_seal.value() == 360
    assert not t.ck_nudge.isEnabled()      # no reminder when auto-seal is on


def _mute_boxes(monkeypatch):
    # _save pops a modal QMessageBox.information on success; offscreen Qt can't run
    # a modal, so stub it (same pattern as the history-dialog tests).
    monkeypatch.setattr(stab.QMessageBox, "information", staticmethod(lambda *a, **k: None))
    monkeypatch.setattr(stab.QMessageBox, "critical", staticmethod(lambda *a, **k: None))


def test_save_manual_mode_writes_inf_and_nudge(qapp, monkeypatch):
    _mute_boxes(monkeypatch)
    ctl = Ctl("defaults:\n  seal_interval_min: 360\n")
    t = stab.SettingsTab(ctl)
    stab._select(t.cb_history, "manual")
    t._history_mode_changed()
    t.ck_nudge.setChecked(False)
    t._save(restart=False)
    d = _defaults(ctl.saved)
    assert d["seal_interval_min"] == "inf"
    assert d["suggest_commit"] is False


def test_save_automatic_mode_writes_interval(qapp, monkeypatch):
    _mute_boxes(monkeypatch)
    ctl = Ctl("defaults:\n  seal_interval_min: inf\n")
    t = stab.SettingsTab(ctl)
    stab._select(t.cb_history, "auto")
    t._history_mode_changed()
    t.sp_seal.setValue(120)
    t._save(restart=False)
    d = _defaults(ctl.saved)
    assert d["seal_interval_min"] == 120
