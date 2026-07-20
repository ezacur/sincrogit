"""Machines view and per-repo Properties dialog (offscreen)."""

import math
import os
import threading
import time

import pytest

import sincrogit.gui.machines_dialog as md
import sincrogit.gui.repo_properties_dialog as rp


class MachCtl:
    def __init__(self):
        self.fetched = []
        self.now = time.time()

    def repo_list(self):
        return [("t", os.getcwd())]

    def list_autosnaps(self, name):
        rows = [
            {"host": "DESKTOP", "branch": "main", "epoch": self.now - 600, "sha": "a"},
            {"host": "LAPTOP", "branch": "main", "epoch": self.now - 86400 * 5, "sha": "b"},
        ]
        if self.fetched:
            rows.append({"host": "NEWPC", "branch": "main",
                         "epoch": self.now - 60, "sha": "c"})
        return rows

    def fetch_autosnaps(self, name):
        assert threading.current_thread() is not threading.main_thread()
        self.fetched.append(name)
        return []

    def this_host(self):
        return "DESKTOP"


def test_machines_rows_and_freshness(qapp, qspin):
    ctl = MachCtl()
    d = md.MachinesDialog(ctl)
    try:
        # The listing loads on a worker now (one `git for-each-ref` per repo
        # used to freeze the dialog open): spin until it lands.
        assert qspin(lambda: d.tbl.rowCount() == 2)
        assert "DESKTOP" in d.tbl.item(0, 0).text()      # newest first
        assert "(this machine)" in d.tbl.item(0, 0).text()
        fresh = d.tbl.item(0, 3).foreground().color().name().lower()
        stale = d.tbl.item(1, 3).foreground().color().name().lower()
        assert fresh == "#2e9e5b" and stale == "#d23f3f"
    finally:
        d.close()


def test_machines_fetch_async(qapp, qspin):
    ctl = MachCtl()
    d = md.MachinesDialog(ctl)
    try:
        assert qspin(lambda: d.tbl.rowCount() == 2)      # initial async load
        d._fetch()
        assert qspin(lambda: d.btn_fetch.isEnabled())
        assert ctl.fetched == ["t"]
        assert qspin(lambda: d.tbl.rowCount() == 3)      # new mirror after fetch
        assert "refreshed" in d.lbl_info.text()
    finally:
        d.close()


_DEFAULTS = {
    "branch": "main", "remote": "origin", "snapshot_interval_sec": 300,
    "debounce_sec": 25, "seal_interval_min": 360, "seal_on_leave_min": 20,
    "push": True, "pull": True, "pull_interval_min": 10,
    "autosnap": True, "autosnap_interval_min": 30, "live_handoff": "auto",
    "git_timeout_sec": 60, "track_current_branch": False,
    "max_file_bytes": 1_048_576, "max_include_bytes": 26_214_400,
    "extra_excludes": ["**/node_modules/**"], "extra_includes": [],
    "suggest_excludes": True, "suggest_commit": True,
}


class PropsCtl:
    theme = {"is_dark": False}

    def __init__(self, entry=None):
        self.saved = None
        self.reset_called = False
        # `push: False` is this repo's one OVERRIDE (the defaults say True).
        self.entry = entry if entry is not None else {
            "path": "C:/tmp/alpha", "branch": "main", "push": False}

    def repo_config_view(self, name):
        eff = dict(_DEFAULTS)
        eff.update({k: v for k, v in self.entry.items() if k in _DEFAULTS})
        return dict(self.entry), eff, dict(_DEFAULTS)

    def update_repo_config(self, name, changes):
        self.saved = (name, changes)
        return True, "saved"

    def reset_repo_config(self, name):
        self.reset_called = True
        return True, "reset 1 override(s): push"

    def remove_repo_config(self, name):
        return True, "removed"


@pytest.fixture
def props(qapp, monkeypatch):
    infos = []
    monkeypatch.setattr(rp.QMessageBox, "information",
                        staticmethod(lambda *a, **k: infos.append(a[2])))
    monkeypatch.setattr(rp.QMessageBox, "critical",
                        staticmethod(lambda *a, **k: infos.append("CRIT:" + a[2])))
    ctl = PropsCtl()
    d = rp.RepoPropertiesDialog(ctl, "t")
    yield ctl, d, infos
    d.close()


def test_untouched_dialog_writes_nothing(props):
    ctl, d, infos = props
    d._save(restart=False)
    assert ctl.saved is None
    assert infos and "Nothing changed" in infos[-1]


def test_only_changed_keys_are_written(props):
    ctl, d, _ = props
    d.ed_branch.setText("develop")
    d.ck_purist.setChecked(True)  # -> seal_interval_min: inf
    d._save(restart=False)
    name, changes = ctl.saved
    assert name == "t"
    assert set(changes) == {"branch", "seal_interval_min"}
    assert changes["branch"] == "develop"
    assert math.isinf(changes["seal_interval_min"])


def test_every_inheritable_option_has_a_form_field(props):
    """The dialog must cover EVERY per-repo option: each inheritable RepoConfig
    key appears in the values it can save (introspected — a new config field
    without a form field fails here)."""
    from sincrogit.config import _INHERITABLE
    _, d, _ = props
    assert set(_INHERITABLE) <= set(d._values().keys())


def test_hints_show_provenance_and_defaults(props):
    """Next to each field: 'override — default: X' when the repo pins it,
    'default (X)' when it inherits — the default value visible either way."""
    _, d, _ = props
    assert d._hints["push"].text() == "override — default: on"
    assert d._hints["snapshot_interval_sec"].text() == "default (300)"
    assert d._hints["seal_on_leave_min"].text() == "default (20)"
    assert d._hints["max_file_bytes"].text() == "default (1024 KB)"
    assert d._hints["extra_excludes"].text() == "default (1 pattern(s))"


def test_new_fields_round_trip_only_changes(props):
    ctl, d, _ = props
    d.sp_debounce.setValue(5)                 # agent-repo profile
    d.ck_leave.setChecked(False)              # disable the leave seal here
    d.sp_maxfile.setValue(2048)               # 2 MB cap
    d._save(restart=False)
    name, changes = ctl.saved
    assert set(changes) == {"debounce_sec", "seal_on_leave_min", "max_file_bytes"}
    assert changes["debounce_sec"] == 5
    assert math.isinf(changes["seal_on_leave_min"])
    assert changes["max_file_bytes"] == 2048 * 1024


def test_use_defaults_drops_overrides(props, monkeypatch):
    ctl, d, infos = props
    assert d.btn_reset.isEnabled()            # 'push' is overridden
    monkeypatch.setattr(rp.QMessageBox, "question",
                        staticmethod(lambda *a, **k: rp.QMessageBox.Yes))
    d._reset_overrides()
    assert ctl.reset_called
    assert any("reset 1 override" in i for i in infos)


def test_use_defaults_disabled_without_overrides(qapp, monkeypatch):
    infos = []
    monkeypatch.setattr(rp.QMessageBox, "information",
                        staticmethod(lambda *a, **k: infos.append(a[2])))
    ctl = PropsCtl(entry={"path": "C:/tmp/alpha", "branch": "main"})
    d = rp.RepoPropertiesDialog(ctl, "t")
    try:
        assert not d.btn_reset.isEnabled()    # nothing to reset
    finally:
        d.close()
        d.deleteLater()
