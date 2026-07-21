"""Timeline v2 proposal tab (offscreen): the activity-band data model, the
kind filter, the time/pixel mapping + hit-testing + zoom, and the
select-bar -> files -> diff flow. Painting isn't exercised (offscreen doesn't
paint), but everything the paint reads is."""

import threading
import time

import pytest

import sincrogit.gui.timeline_v2_tab as tv2


def _off_gui():
    assert threading.current_thread() is not threading.main_thread(), \
        "git work must NOT run on the GUI thread"


class Ctl:
    theme = {"is_dark": False}

    def __init__(self):
        self.now = time.time()

    def repo_list(self):
        return [("t", "/tmp/t"), ("u", "/tmp/u")]

    def snapshot_timeline(self, name, limit=200):
        _off_gui()
        if name != "t":
            return []
        return [
            {"sha": "s3", "parent": "s2", "epoch": self.now - 30,
             "subject": "sincro: snapshot", "kind": "snapshot",
             "files": [("M", "app.py", 40, 5)]},                 # big churn
            {"sha": "s2", "parent": "s1", "epoch": self.now - 3600,
             "subject": "feat: sealed", "kind": "seal",
             "files": [("M", "app.py", 2, 1)]},
            {"sha": "s1", "parent": "s0", "epoch": self.now - 3 * 86400,
             "subject": "sincro: snapshot", "kind": "snapshot",
             "files": [("A", "img.png", None, None)]},           # binary
            {"sha": "a1", "parent": None, "epoch": self.now - 2 * 86400,
             "subject": "mirror", "kind": "autosnap",
             "files": [("M", "app.py", 7, 0)]},
        ]

    def file_text_at(self, name, rel, sha):
        _off_gui()
        return {"s2": "a\nb\n", "s3": "a\nb\nc\n"}.get(sha, "")

    def current_text(self, name, rel):
        _off_gui()
        return "a\nCURRENT\n"


@pytest.fixture
def tab(qapp):
    ctl = Ctl()
    t = tv2.TimelineV2Tab(ctl)
    t.band.resize(600, 150)   # give the band a width so the mapping is real
    t.show()
    yield ctl, t
    t.close()
    t.deleteLater()


def test_loads_oldest_first_with_churn(tab, qspin):
    _ctl, t = tab
    assert qspin(lambda: len(t.band._entries) == 4)
    eps = [e["epoch"] for e in t.band._entries]
    assert eps == sorted(eps)                       # oldest -> newest for the band
    churn = {e["sha"]: e["churn"] for e in t.band._entries}
    assert churn["s3"] == 45 and churn["s2"] == 3   # adds+dels
    assert churn["s1"] == 0                          # binary (None) -> 0


def test_kind_filter_drops_categories(tab, qspin):
    _ctl, t = tab
    assert qspin(lambda: len(t.band._entries) == 4)
    t.ck_snap.setChecked(False)
    assert {e["kind"] for e in t.band._entries} == {"seal", "autosnap"}
    t.ck_auto.setChecked(False)
    assert {e["kind"] for e in t.band._entries} == {"seal"}
    t.ck_snap.setChecked(True)
    t.ck_seal.setChecked(False)
    t.ck_auto.setChecked(True)
    assert {e["kind"] for e in t.band._entries} == {"snapshot", "autosnap"}


def test_time_pixel_mapping_and_hit_test(tab, qspin):
    _ctl, t = tab
    assert qspin(lambda: len(t.band._entries) == 4)
    band = t.band
    # Round-trip: pixel <-> epoch is a linear bijection over the view window.
    for e in band._entries:
        x = band._x_for(e["epoch"])
        assert abs(band._epoch_for(x) - e["epoch"]) < 1.0
    # A click at a mark's x resolves to that mark.
    idx = 2
    x = band._x_for(band._entries[idx]["epoch"])
    assert band._mark_at(x) == idx
    # Far from any mark -> nothing.
    assert band._mark_at(band._x_for(band._entries[idx]["epoch"]) + 40) is None


def test_zoom_shrinks_window_and_clamps(tab, qspin):
    _ctl, t = tab
    assert qspin(lambda: len(t.band._entries) == 4)
    band = t.band
    span0 = band._view1 - band._view0
    anchor = (band._view0 + band._view1) / 2
    band._zoom(0.5, anchor)
    assert (band._view1 - band._view0) < span0          # zoomed in
    for _ in range(60):                                 # zoom in hard
        band._zoom(0.5, anchor)
    assert (band._view1 - band._view0) >= 60            # never below one minute
    band.reset_view()
    assert abs((band._view1 - band._view0) - span0) < 1.0


def test_select_bar_fills_files_and_loads_diff(tab, qspin):
    _ctl, t = tab
    assert qspin(lambda: len(t.band._entries) == 4)
    # s3 is the newest (last) entry; selecting it lists its one file.
    idx = next(i for i, e in enumerate(t.band._entries) if e["sha"] == "s3")
    t.band.selected.emit(idx)
    assert t.tbl_files.rowCount() == 1
    assert t.tbl_files.item(0, 1).text() == "app.py"
    assert "1 file" in t.lbl_sel.text()
    # Row 0 auto-selects -> a diff loads (s2 -> s3 adds a line). Wait for the
    # rendered diff (its +++ header names the 'this state' side), not the
    # transient "Loading…" placeholder.
    assert qspin(lambda: "this state" in t.diff.toPlainText())
    assert "c" in t.diff.toPlainText()          # the added line
    assert "CURRENT" not in t.diff.toPlainText()  # it's vs the PARENT, not today


def test_binary_file_says_no_text_diff(tab, qspin):
    _ctl, t = tab
    assert qspin(lambda: len(t.band._entries) == 4)
    idx = next(i for i, e in enumerate(t.band._entries) if e["sha"] == "s1")
    t.band.selected.emit(idx)
    assert qspin(lambda: "binary" in t.diff.toPlainText())


def test_empty_repo_is_graceful(tab, qspin):
    _ctl, t = tab
    assert qspin(lambda: len(t.band._entries) == 4)
    t.cb_repo.setCurrentText("u")                       # no history
    assert qspin(lambda: len(t.band._entries) == 0)
    assert "No states" in t.lbl_hint.text()
    assert t.band._mark_at(100) is None                 # no marks, no crash
