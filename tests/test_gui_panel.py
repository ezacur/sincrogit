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

    def status(self):
        return {"paused": False, "running": True, "repos": [dict(self.repo)]}

    def events_all(self):
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
