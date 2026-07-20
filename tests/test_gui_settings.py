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


def test_revisiting_a_repo_rebuilds_the_pane_fresh(qapp):
    """_build_pane's contract is 'a FRESH pane per visit': jumping to the SAME
    repo again must re-read entry/effective — setCurrentRow on the current row
    emits nothing, so the stale pane (pre Advanced-YAML edit) survived."""
    class Live(FullCtl):
        branch = "main"

        def repo_config_view(self, name):
            return ({"path": f"C:/tmp/{name}", "branch": self.branch},
                    {"branch": self.branch}, {})

    ctl = Live("defaults: {}\n")
    t = stab.SettingsTab(ctl)
    t.select_repo("beta")
    first = t._pane
    assert first.ed_branch.text() == "main"
    ctl.branch = "develop"            # edited in Advanced (YAML) meanwhile
    t.select_repo("beta")             # same row as before
    assert t._pane is not first       # rebuilt, not the stale leftover
    assert t._pane.ed_branch.text() == "develop"


def test_autostart_checkbox_reflects_and_applies(qapp, monkeypatch):
    """The start-at-login toggle mirrors the registry (via the controller) on
    load and applies IMMEDIATELY on Save — it never touches config.yaml."""
    _mute_boxes(monkeypatch)

    class AutoCtl(Ctl):
        def __init__(self, text):
            super().__init__(text)
            self.autostart = True
            self.applied = None

        def autostart_enabled(self):
            return self.autostart, None

        def set_autostart(self, enabled):
            self.applied = enabled
            return True, "ok"

    ctl = AutoCtl("defaults: {}\n")
    t = stab.SettingsTab(ctl)
    assert t.ck_autostart.isChecked() and t.ck_autostart.isEnabled()
    t.ck_autostart.setChecked(False)
    t._save(restart=False)
    assert ctl.applied is False
    assert "autostart" not in (ctl.saved or "")     # registry, not YAML


def test_autostart_checkbox_disabled_without_support(qapp):
    """Duck-typing: a controller without the autostart API (tests, non-Windows)
    just gets a disabled checkbox — saving skips the registry entirely."""
    t = stab.SettingsTab(Ctl("defaults: {}\n"))
    assert not t.ck_autostart.isEnabled()
