"""What happened tab (offscreen): the period selector, the per-repo lines, the
milestone list, and the "no absence yet" state. Plus the tray side of it — the
digest is computed on arrival, shown only when something actually happened, and
never twice for one absence."""

import threading
import time

import pytest

import sincrogit.gui.app as appmod
import sincrogit.gui.what_happened_tab as wht


def _off_gui():
    assert threading.current_thread() is not threading.main_thread(), \
        "the digest must NOT be computed on the GUI thread"


def _digest(now, trivial=False, repos=None, milestones=None, **kw):
    d = {"since": now - 3600, "until": now, "summary": "1 repo · 3 files",
         "repos": repos if repos is not None else
         [{"name": "t", "files": 3, "adds": 40, "dels": 5, "seals": 1, "marks": 2}],
         "milestones": milestones if milestones is not None else
         [{"epoch": now - 100, "repo": "t", "action": "seal", "level": "INFO",
           "message": "feat: the thing"},
          {"epoch": now - 200, "repo": "t", "action": "push", "level": "ERROR",
           "message": "push has failed 3 times in a row"}],
         "files": 3, "adds": 40, "dels": 5, "seals": 1, "marks": 2,
         "handoffs": 0, "conflicts": 0, "push_failed": 1, "partial": False,
         "trivial": trivial}
    d.update(kw)
    return d


class Ctl:
    theme = {"is_dark": False}

    def __init__(self):
        self.now = time.time()
        self.asked = []
        self.absence = _digest(self.now)

    def last_digest(self):
        _off_gui()
        return self.absence

    def digest_since(self, since, until=None):
        _off_gui()
        self.asked.append((since, until))
        return _digest(self.now, summary="computed on demand")


@pytest.fixture
def tab(qapp):
    ctl = Ctl()
    t = wht.WhatHappenedTab(ctl)
    t.show()
    yield ctl, t
    t.close()
    t.deleteLater()


def test_the_last_absence_is_the_default_view(tab, qspin):
    ctl, t = tab
    assert qspin(lambda: t.tbl_repos.rowCount() == 1)
    assert t.lbl_head.text() == "1 repo · 3 files"
    assert t.tbl_repos.item(0, 0).text() == "t"
    assert t.tbl_repos.item(0, 1).text() == "3"
    assert t.tbl_repos.item(0, 2).text() == "+40  −5"
    assert t.tbl_repos.item(0, 3).text() == "1"      # commits
    assert t.tbl_repos.item(0, 4).text() == "2"      # marks
    assert t.tbl_events.rowCount() == 2
    assert "commit: feat: the thing" in t.tbl_events.item(0, 2).text()
    assert "push problem" in t.tbl_events.item(1, 2).text()
    assert ctl.asked == []       # the absence digest is already computed
    assert t.busy.active is False


def test_other_periods_are_computed_on_demand(tab, qspin):
    ctl, t = tab
    assert qspin(lambda: t.tbl_repos.rowCount() == 1)
    t.cb_period.setCurrentIndex(2)               # last 24 h
    assert qspin(lambda: t.lbl_head.text() == "computed on demand")
    since, until = ctl.asked[-1]
    assert 23.9 * 3600 < until - since < 24.1 * 3600

    t.cb_period.setCurrentIndex(1)               # today -> since midnight
    assert qspin(lambda: len(ctl.asked) == 2)
    since, until = ctl.asked[-1]
    assert time.localtime(since).tm_hour == 0 and time.localtime(since).tm_min == 0


def test_nothing_to_report_yet_explains_itself(tab, qspin):
    ctl, t = tab
    assert qspin(lambda: t.tbl_repos.rowCount() == 1)
    ctl.absence = None
    t._reload()
    assert qspin(lambda: t.lbl_head.text() == "Nothing to report yet.")
    assert "when you come back" in t.lbl_span.text()
    assert t.tbl_repos.rowCount() == 0 and t.tbl_events.rowCount() == 0


def test_a_trivial_absence_says_nothing_changed(tab, qspin):
    ctl, t = tab
    ctl.absence = _digest(ctl.now, trivial=True, repos=[], milestones=[])
    t._reload()
    assert qspin(lambda: t.lbl_head.text() == "Nothing changed.")
    assert "No milestones" in t.lbl_milestones.text()


def test_a_partial_window_admits_it(tab, qspin):
    ctl, t = tab
    ctl.absence = _digest(ctl.now, partial=True)
    t._reload()
    assert qspin(lambda: "there may be more" in t.lbl_span.text())


def test_a_stale_period_never_overwrites_a_newer_one(tab, qspin):
    _ctl, t = tab
    assert qspin(lambda: t.tbl_repos.rowCount() == 1)
    t._gen = 9
    t._on_digest_ready(4, _digest(time.time(), summary="stale"))
    assert t.lbl_head.text() != "stale"


def test_humanize_span_reads_like_a_person():
    assert wht.humanize_span(0) == "0 s"
    assert wht.humanize_span(45) == "45 s"
    assert wht.humanize_span(90) == "1 min"
    assert wht.humanize_span(3 * 3600 + 5 * 60) == "3 h 05 m"
    assert wht.humanize_span(5 * 86400) == "5 days"


# ------------------------------------------------------------- the tray side
class FakeTray:
    """Enough TrayApp surface to drive the arrival handlers unbound (building a
    real one needs a config, an Engine and the whole panel — see test_updater)."""

    def __init__(self, digest):
        self.balloons = []
        self.events = []
        self.logger = type("L", (), {"warning": lambda *a, **k: None})()
        self._last_digest = None
        self.bridge = type("B", (), {})()
        self.bridge.digest_ready = type(
            "S", (), {"emit": lambda _s, d: self._deliver(d)})()
        self.engine = type("E", (), {})()
        self.engine.absence_digest = lambda since: digest

    def _deliver(self, digest):
        appmod.TrayApp._on_digest_ready(self, digest)

    def _tray_ack(self, title, body):
        self.balloons.append((title, body))

    def _on_engine_event(self, repo, action, message, level="INFO"):
        self.events.append((repo, action, message, level))


def test_arrival_reports_an_absence_that_mattered():
    now = time.time()
    app = FakeTray(_digest(now, summary="2 repos · 9 files"))
    appmod.TrayApp._report_absence(app, now - 3600)
    deadline = time.time() + 5
    while not app.balloons and time.time() < deadline:
        time.sleep(0.01)
    assert app.balloons and "While you were away" in app.balloons[0][0]
    assert app.balloons[0][1] == "2 repos · 9 files"
    assert app.events[0][1] == "digest"
    assert app._last_digest is not None


def test_a_trivial_absence_never_interrupts():
    """Unlocking your machine to be told "nothing happened" is how a helpful
    feature becomes one people turn off."""
    now = time.time()
    app = FakeTray(_digest(now, trivial=True, repos=[], milestones=[]))
    appmod.TrayApp._on_digest_ready(app, app.engine.absence_digest(now))
    assert app.balloons == [] and app.events == []
    assert app._last_digest is not None   # the panel can still show it


class FakeArrive:
    """The arrival path itself: TrayApp._on_machine_arrive, unbound."""

    def __init__(self, since):
        self.since = since
        self.reported = []
        self.events = []
        self._last_arrive_mono = 0.0
        self.engine = type("E", (), {})()
        self.engine.absence_since = lambda: self.since
        self.engine.disarm_leave_seal = self._disarm
        self.engine.sync_soon = lambda: None

    def _disarm(self):
        self.since = None            # the real engine clears it too

    def _report_absence(self, since):
        self.reported.append(since)

    def _on_engine_event(self, repo, action, message, level="INFO"):
        self.events.append(action)


def test_the_absence_is_reported_once_per_arrival():
    """Windows sends resume AND unlock: reading the epoch before disarming is
    what makes the second one a no-op, with no extra bookkeeping."""
    started = time.time() - 1800
    app = FakeArrive(started)
    appmod.TrayApp._on_machine_arrive(app, "resume")
    appmod.TrayApp._on_machine_arrive(app, "unlock")
    assert app.reported == [started]


def test_no_known_absence_reports_nothing():
    """A resume with no lock behind it (the leave seal was never armed) is not
    an absence we can describe, so we don't invent one."""
    app = FakeArrive(None)
    appmod.TrayApp._on_machine_arrive(app, "resume")
    assert app.reported == []
    assert app.events == ["resume"]   # the ordinary catch-up still happens


def test_arrival_survives_a_digest_that_blows_up():
    app = FakeTray(None)

    def boom(_since):
        raise RuntimeError("git went missing")

    app.engine.absence_digest = boom
    appmod.TrayApp._report_absence(app, time.time() - 60)
    time.sleep(0.3)
    assert app.balloons == []      # reported to the log, never to a crash
