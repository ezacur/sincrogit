"""Timeline v3 proposal tab (offscreen): the day aggregation, the calendar's
date<->cell mapping, the day strip's hour mapping + hit-test, and the
drill-down flow (day -> strip -> state -> files -> diff). Painting itself is
left to manual comparison (offscreen doesn't paint), but everything it reads
is exercised."""

import datetime
import threading

import pytest

import sincrogit.gui.timeline_v3_tab as tv3


def _off_gui():
    assert threading.current_thread() is not threading.main_thread(), \
        "git work must NOT run on the GUI thread"


def _at(day_offset, hour):
    """Epoch for `day_offset` days ago at `hour` o'clock local time."""
    d = datetime.date.today() - datetime.timedelta(days=day_offset)
    return datetime.datetime.combine(d, datetime.time(hour)).timestamp()


class Ctl:
    theme = {"is_dark": False}

    def repo_list(self):
        return [("t", "/tmp/t"), ("u", "/tmp/u")]

    def snapshot_timeline(self, name, limit=200):
        _off_gui()
        if name != "t":
            return []
        return [
            {"sha": "s4", "parent": "s3", "epoch": _at(0, 15),
             "subject": "sincro: snapshot", "kind": "snapshot",
             "files": [("M", "app.py", 3, 1)]},
            {"sha": "s3", "parent": "s2", "epoch": _at(0, 9),
             "subject": "feat: sealed", "kind": "seal",
             "files": [("M", "app.py", 2, 1), ("A", "img.png", None, None)]},
            {"sha": "s2", "parent": "s1", "epoch": _at(6, 11),
             "subject": "sincro: snapshot", "kind": "snapshot",
             "files": [("M", "app.py", 40, 5)]},
            {"sha": "s1", "parent": "s0", "epoch": _at(6, 10),
             "subject": "sincro: snapshot", "kind": "snapshot",
             "files": [("M", "b.txt", 1, 0)]},
        ]

    def file_text_at(self, name, rel, sha):
        _off_gui()
        return {"s3": "a\nb\n", "s4": "a\nb\nc\n"}.get(sha, "")

    def current_text(self, name, rel):
        _off_gui()
        return "a\nCURRENT\n"


@pytest.fixture
def tab(qapp):
    ctl = Ctl()
    t = tv3.TimelineV3Tab(ctl)
    t.strip.resize(600, 64)
    t.show()
    yield ctl, t
    t.close()
    t.deleteLater()


def test_aggregation_groups_by_day():
    today = datetime.date.today()
    old = today - datetime.timedelta(days=6)
    # The fake asserts off-GUI-thread, so gather its entries on a worker.
    holder = {}

    def work():
        holder["days"] = tv3._aggregate_days(Ctl().snapshot_timeline("t"))
    th = threading.Thread(target=work)
    th.start()
    th.join()
    days = holder["days"]
    assert set(days) == {today, old}
    assert days[today]["count"] == 2 and days[today]["seals"] == 1
    assert days[today]["churn"] == 7            # 3+1 and 2+1; binary counts 0
    assert days[old]["count"] == 2 and days[old]["churn"] == 46
    eps = [e["epoch"] for e in days[today]["entries"]]
    assert eps == sorted(eps)                    # oldest-first within the day


def test_calendar_maps_dates_to_cells_and_back(tab, qspin):
    _ctl, t = tab
    assert qspin(lambda: len(t._days) == 2)
    hm = t.heatmap
    for d in list(t._days) + [datetime.date.today() - datetime.timedelta(days=2)]:
        r = hm._rect_for(d)
        assert r is not None
        assert hm._date_at(r.center()) == d      # roundtrip through pixels
    # Before the grid or in the future -> None.
    assert hm._rect_for(datetime.date.today() - datetime.timedelta(weeks=60)) is None
    tomorrow_rect = hm._rect_for(datetime.date.today())
    below = tomorrow_rect.center()
    below.setY(below.y() + 700)
    assert hm._date_at(below) is None


def test_loads_and_opens_the_most_recent_day(tab, qspin):
    _ctl, t = tab
    assert qspin(lambda: len(t._days) == 2)
    # The latest day auto-opens: its 2 states in the strip, last one selected.
    assert len(t.strip._entries) == 2
    assert "2 state(s)" in t.lbl_day.text()
    assert "seal" in t.lbl_day.text()            # the day label counts seals
    # The last state of the day (15:00 snapshot) is open with its file.
    assert t.tbl_files.rowCount() == 1
    assert t.tbl_files.item(0, 1).text() == "app.py"
    assert qspin(lambda: "this state" in t.diff.toPlainText())
    assert "c" in t.diff.toPlainText()


def test_day_click_drills_into_that_day(tab, qspin):
    _ctl, t = tab
    assert qspin(lambda: len(t._days) == 2)
    old = datetime.date.today() - datetime.timedelta(days=6)
    t.heatmap.day_selected.emit(old)
    assert len(t.strip._entries) == 2
    assert "46 lines changed" in t.lbl_day.text()
    # Strip auto-opens the day's LAST state (11:00, the 40+5 one).
    assert "+40 −5" in t.tbl_files.item(0, 2).text()


def test_strip_hour_mapping_and_hit_test(tab, qspin):
    _ctl, t = tab
    assert qspin(lambda: len(t._days) == 2)
    strip = t.strip
    x9 = strip._x_for(_at(0, 9))
    x15 = strip._x_for(_at(0, 15))
    assert x9 < x15                              # hours grow left -> right
    assert strip._index_at(x9) == 0 and strip._index_at(x15) == 1
    assert strip._index_at((x9 + x15) / 2) is None   # far from both


def test_binary_and_empty_paths(tab, qspin):
    _ctl, t = tab
    assert qspin(lambda: len(t._days) == 2)
    t.strip.state_selected.emit(0)               # the seal: app.py + img.png
    assert t.tbl_files.rowCount() == 2
    binary_row = next(i for i in range(2)
                      if t.tbl_files.item(i, 1).text() == "img.png")
    t.tbl_files.selectRow(binary_row)
    assert qspin(lambda: "binary" in t.diff.toPlainText())
    t.cb_repo.setCurrentText("u")                # a repo with no history
    assert qspin(lambda: not t._days)
    assert "No activity" in t.lbl_day.text()
