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


def test_machines_rows_and_freshness(qapp):
    ctl = MachCtl()
    d = md.MachinesDialog(ctl)
    try:
        assert d.tbl.rowCount() == 2
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
        d._fetch()
        assert qspin(lambda: d.btn_fetch.isEnabled())
        assert ctl.fetched == ["t"]
        assert d.tbl.rowCount() == 3  # the new mirror appears after the fetch
    finally:
        d.close()


class PropsCtl:
    def __init__(self):
        self.saved = None

    def repo_config_view(self, name):
        entry = {"path": "C:/tmp/alpha", "branch": "main"}
        eff = {"branch": "main", "remote": "origin", "snapshot_interval_sec": 300,
               "seal_interval_min": 360, "push": True, "pull": True,
               "pull_interval_min": 10, "autosnap": True, "autosnap_interval_min": 30,
               "live_handoff": "auto", "track_current_branch": False,
               "extra_excludes": ["**/node_modules/**"], "extra_includes": []}
        return entry, eff

    def update_repo_config(self, name, changes):
        self.saved = (name, changes)
        return True, "saved"

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
