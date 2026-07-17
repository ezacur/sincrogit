"""Timeline tab (offscreen): async load, day grouping, file table, per-file
diff on a worker, seal filter, and the event-driven refresh hook."""

import threading
import time

import pytest

import sincrogit.gui.timeline_tab as tt


class Ctl:
    theme = {"is_dark": False}

    def repo_list(self):
        return [("t", "X:/t"), ("u", "X:/u")]

    def snapshot_timeline(self, name, limit=200):
        assert threading.current_thread() is not threading.main_thread(), \
            "the timeline walk must NOT run on the GUI thread"
        if name != "t":
            return []
        now = time.time()
        return [
            {"sha": "s2", "parent": "s1", "epoch": now - 60,
             "subject": "sincro: snapshot", "kind": "snapshot",
             "files": [("M", "src/app.py", 3, 1), ("A", "img.png", None, None)]},
            {"sha": "s1", "parent": "s0", "epoch": now - 3 * 86400,
             "subject": "feat: sealed window", "kind": "seal",
             "files": [("M", "src/app.py", 10, 2)]},
        ]

    def file_text_at(self, name, rel, sha):
        assert threading.current_thread() is not threading.main_thread(), \
            "the diff read must NOT run on the GUI thread"
        return {"s0": "a\nb\n", "s1": "a\nb2\n", "s2": "a\nb3\n"}.get(sha, "")


@pytest.fixture
def tab(qapp):
    t = tt.TimelineTab(Ctl())
    t.show()   # fires showEvent -> repo sync + async load
    yield t
    t.close()
    t.deleteLater()


def _cards(t):
    return [t.lst.item(i).data(tt.ROLE_ENTRY) for i in range(t.lst.count())
            if t.lst.item(i).data(tt.ROLE_ENTRY) is not None]


def test_loads_async_with_day_headers(tab, qspin):
    assert qspin(lambda: tab.lst.count() > 0)
    cards = _cards(tab)
    assert [c["sha"] for c in cards] == ["s2", "s1"]
    headers = tab.lst.count() - len(cards)
    assert headers == 2                      # two different days -> two captions
    assert "1 snapshot(s) · 1 seal(s)" in tab.lbl_count.text()


def test_selecting_state_lists_files_and_diffs_them(tab, qspin):
    assert qspin(lambda: tab.lst.count() > 0)
    # The newest state auto-selects and its files fill the table.
    assert tab.tbl_files.rowCount() == 2
    assert tab.tbl_files.item(0, 1).text() == "src/app.py"
    assert tab.tbl_files.item(0, 2).text() == "+3"
    assert tab.tbl_files.item(1, 2).text() == "bin"   # binary: no counts
    # The first file auto-selects and its diff arrives from the worker.
    assert qspin(lambda: "this snapshot" in tab.diff.toPlainText())
    assert "src/app.py" in tab.lbl_diff.text()


def test_binary_file_shows_placeholder_not_diff(tab, qspin):
    assert qspin(lambda: tab.tbl_files.rowCount() == 2)
    tab.tbl_files.selectRow(1)               # img.png (binary)
    assert qspin(lambda: "binary file" in tab.diff.toPlainText())


def test_seals_only_filter(tab, qspin):
    assert qspin(lambda: tab.lst.count() > 0)
    tab.cb_filter.setCurrentIndex(1)         # "Seals only" re-renders locally
    cards = _cards(tab)
    assert [c["kind"] for c in cards] == ["seal"]


def test_notice_event_refreshes_only_matching_repo(tab, qspin):
    assert qspin(lambda: tab.lst.count() > 0)

    class Ev:
        def __init__(self, repo, action):
            self.repo, self.action = repo, action

    tab._debounce.stop()
    tab.notice_event(Ev("other-repo", "snapshot"))
    assert tab._stale and not tab._debounce.isActive()   # stale, but no reload burst
    tab.notice_event(Ev("t", "snapshot"))
    assert tab._debounce.isActive()                      # visible + same repo -> debounced reload
    tab.notice_event(Ev("t", "log"))                     # unrelated actions never trigger
