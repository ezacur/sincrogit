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


class FullCtl(Ctl):
    """A controller with repos, for the master-detail behavior."""
    theme = {"is_dark": False}

    def repo_list(self):
        return [("alpha", "C:/tmp/alpha"), ("beta", "C:/tmp/beta")]

    def repo_config_view(self, name):
        return ({"path": f"C:/tmp/{name}", "branch": "main"},
                {"branch": "main"}, {})

    def update_repo_config(self, name, changes):
        return True, "saved"

    def reset_repo_config(self, name):
        return True, "reset"

    def remove_repo_config(self, name):
        return True, "removed"


def test_master_list_and_inline_repo_pane(qapp):
    """Settings is master-detail now: Global defaults + one entry per repo,
    and picking a repo edits it INLINE (stack page), never in a window."""
    t = stab.SettingsTab(FullCtl("defaults: {}\n"))
    items = [t.lst.item(i).text() for i in range(t.lst.count())]
    assert items == ["Global defaults", "alpha", "beta"]
    assert t.stack.currentIndex() == 0             # the global form first
    t.select_repo("beta")
    assert t.stack.currentIndex() == 1             # the repo pane, same screen
    assert t._pane is not None and t._pane.name == "beta"
    t.lst.setCurrentRow(0)
    assert t.stack.currentIndex() == 0             # back to the global form


def test_removing_a_repo_returns_to_the_global_page(qapp, monkeypatch):
    import sincrogit.gui.repo_settings_pane as rp
    monkeypatch.setattr(rp.QMessageBox, "warning",
                        staticmethod(lambda *a, **k: rp.QMessageBox.Yes))
    monkeypatch.setattr(rp.QMessageBox, "information",
                        staticmethod(lambda *a, **k: None))
    t = stab.SettingsTab(FullCtl("defaults: {}\n"))
    t.select_repo("alpha")
    t._pane._remove()
    assert t.stack.currentIndex() == 0             # dropped back to Global


def test_controllers_without_repos_get_the_global_page_alone(qapp):
    t = stab.SettingsTab(Ctl("defaults: {}\n"))    # no repo_list on this duck
    assert [t.lst.item(i).text() for i in range(t.lst.count())] == ["Global defaults"]
    assert t.stack.currentIndex() == 0
