"""Control panel (offscreen): canonical states in the action bar and table,
handoff confirmation, working feedback for manual actions, activity digest."""

import os
import time
import types

import pytest
from PyQt5.QtWidgets import QMessageBox

import sincrogit.gui.control_panel as cp


class PanelCtl:
    config_path = "x.yaml"

    def __init__(self):
        self.repo = {
            "name": "t", "path": os.getcwd(), "branch": "main", "state": "active",
            "conflict_paused": False, "conflict_msg": "", "user_paused": False,
            "off_branch": False, "net_busy": False, "pending_handoff": None,
            "pending_handoff_epoch": None, "last_snapshot": None, "last_seal": None,
            "last_action": "", "last_action_ts": 0.0, "push": True, "pull": True,
        }
        self.calls = []
        self.history_reads = 0

    def status(self):
        return {"paused": False, "running": True, "repos": [dict(self.repo)]}

    def events_recent(self):
        return [types.SimpleNamespace(ts=time.time(), repo="t", action="startup",
                                      level="INFO", message="live tail")]

    def events_all(self):
        self.history_reads += 1
        return []

    def config_text(self):
        return "defaults: {}\nrepos: []\n"

    def app_state(self):
        return "running"

    def make_icon(self, state):
        from PyQt5.QtGui import QIcon
        return QIcon()

    def seal_repo_now(self, name):
        self.calls.append(("seal", name))

    def pull_repo_now(self, name):
        self.calls.append(("pull", name))

    def apply_handoff(self, name):
        self.calls.append(("handoff", name))


@pytest.fixture
def panel(qapp):
    ctl = PanelCtl()
    p = cp.ControlPanel(ctl)
    p.show()  # offscreen: child isVisible() must reflect setVisible()
    p.refresh_status()
    yield ctl, p
    p.close()


def test_active_state(panel):
    _, p = panel
    assert not p.b_fix.isVisible() and p.b_seal.isEnabled()


def test_conflict_state_explained(panel):
    ctl, p = panel
    ctl.repo.update(state="conflict", conflict_paused=True,
                    conflict_msg="Your local changes overlap…")
    p.refresh_status()
    item = p.tbl_repos.item(0, 2)
    assert item.text() == "Conflict" and "overlap" in item.toolTip()
    assert p.b_fix.isVisible()


def test_handoff_tooltip_and_confirmation(panel, monkeypatch):
    ctl, p = panel
    ctl.repo.update(state="handoff", pending_handoff="laptop",
                    pending_handoff_epoch=time.time() - 300)
    p.refresh_status()
    item = p.tbl_repos.item(0, 2)
    assert "laptop" in item.text() and "laptop" in item.toolTip()
    assert "5m" in item.toolTip()  # the mirror's age
    monkeypatch.setattr(cp.QMessageBox, "question",
                        staticmethod(lambda *a, **k: QMessageBox.No))
    p._apply_handoff("t")
    assert ("handoff", "t") not in ctl.calls  # declined -> not applied
    monkeypatch.setattr(cp.QMessageBox, "question",
                        staticmethod(lambda *a, **k: QMessageBox.Yes))
    p._apply_handoff("t")
    assert ("handoff", "t") in ctl.calls


def test_net_busy_disables_actions(panel):
    ctl, p = panel
    ctl.repo.update(net_busy=True)
    p.refresh_status()
    assert not p.b_seal.isEnabled() and not p.b_pull.isEnabled()
    assert "working" in p.lbl_selected.text()


def test_manual_action_inflight_lifecycle(panel):
    ctl, p = panel
    p._start_repo_action("t", ctl.seal_repo_now)
    assert ("seal", "t") in ctl.calls and not p.b_seal.isEnabled()
    p.append_event(types.SimpleNamespace(ts=time.time(), repo="t", action="seal",
                                         level="INFO", message="sealed"))
    assert p.b_seal.isEnabled()  # the completion event re-enables


def test_activity_digest(panel):
    _, p = panel
    now = time.time()
    p.append_event(types.SimpleNamespace(ts=now, repo="t", action="snapshot",
                                         level="INFO", message="snapshot"))
    p.append_event(types.SimpleNamespace(ts=now, repo="t", action="seal",
                                         level="INFO", message="sealed"))
    p.refresh_status()
    assert "1 snapshot" in p.lbl_digest.text() and "1 seal" in p.lbl_digest.text()


def _goto_log_tab(p):
    for i in range(p.tabs.count()):
        if p.tabs.tabText(i) == "Log":
            p.tabs.setCurrentIndex(i)
            return


def test_log_history_loads_in_background_and_only_once(panel, qspin):
    """Opening the panel must NOT re-read the JSONL from disk: the history
    loads once (worker) at construction and the cache stays live afterwards —
    the synchronous re-read on every show is what froze the tray click."""
    ctl, p = panel
    assert qspin(lambda: ctl.history_reads == 1)
    p.hide(); p.show()
    p.hide(); p.show()
    assert ctl.history_reads == 1


def test_log_history_merges_under_live_tail_without_dupes(qapp, qspin):
    live = types.SimpleNamespace(ts=1000.0, repo="t", action="seal",
                                 level="INFO", message="dup")
    old = types.SimpleNamespace(ts=1.0, repo="t", action="snapshot",
                                level="INFO", message="old")
    ctl = PanelCtl()
    ctl.events_recent = lambda: [live]
    ctl.events_all = lambda: [old, live]   # the disk list contains the live one too
    p = cp.ControlPanel(ctl)
    try:
        assert qspin(lambda: len(p._events_cache) == 2)
        assert p._events_cache[0] is old   # history merges UNDER the live tail
        assert p._events_cache[1] is live
    finally:
        p.close()
        p.deleteLater()


def test_search_filter_is_debounced(panel, qspin):
    _, p = panel
    _goto_log_tab(p)
    p.ed_search.setText("zzz-matches-nothing")
    assert p._search_debounce.isActive()   # not rebuilt on the keystroke itself
    assert qspin(lambda: "0 event(s) match" in p.lbl_log_count.text())


def test_global_events_pass_any_repo_filter(panel, qspin):
    """Session lock/unlock, flushes and engine errors carry repo == "" — they
    must stay visible even when the Log is filtered to one repo (hiding them
    made the OS-event flushes look like they never fired)."""
    _, p = panel
    _goto_log_tab(p)
    p.cb_repo.addItem("t")
    p.cb_repo.setCurrentText("t")              # filter to one repo
    ev = types.SimpleNamespace(ts=time.time(), repo="", action="flush",
                               level="INFO", message="machine lock: flushing")
    assert p._passes_filter(ev)                # global event survives the filter
    p.append_event(ev)
    assert p.tbl_log.item(0, 4).text().startswith("machine lock")
