"""BusyBar: the reusable "working…" indicator — ref-counted so overlapping
workers don't hide it early, and safe to over-stop."""

from sincrogit.gui.busy import BusyBar


def test_hidden_when_idle(qapp):
    b = BusyBar()
    assert not b.active and not b.isVisible()


def test_start_shows_stop_hides(qapp):
    b = BusyBar()
    b.start("Loading…")
    assert b.active and b.isVisible() and b._label.text() == "Loading…"
    b.stop()
    assert not b.active and not b.isVisible()


def test_ref_counted_across_overlapping_workers(qapp):
    b = BusyBar()
    b.start("history")
    b.start("diff")
    assert b._label.text() == "diff"          # newest caption wins
    b.stop()                                   # one worker done, one still running
    assert b.active and b.isVisible()
    assert b._label.text() == "history"        # caption falls back, never lies "idle"
    b.stop()
    assert not b.active and not b.isVisible()


def test_over_stop_is_safe(qapp):
    b = BusyBar()
    b.start("x")
    b.stop()
    b.stop()                                   # extra stop must not go negative
    assert b._count == 0 and not b.isVisible()


def test_reset_forces_idle(qapp):
    b = BusyBar()
    b.start("a")
    b.start("b")
    b.reset()
    assert not b.active and not b.isVisible() and b._count == 0
