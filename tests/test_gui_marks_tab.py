"""Marks tab (offscreen): the named moments, comparing one against the files on
disk, restoring to it, forgetting it, and the jump into the Time machine — all
async, never on the GUI thread."""

import threading
import time

import pytest
from PyQt5.QtWidgets import QMessageBox

import sincrogit.gui.marks_tab as mt


def _off_gui():
    assert threading.current_thread() is not threading.main_thread(), \
        "git work must NOT run on the GUI thread"


class Ctl:
    theme = {"is_dark": False}

    def __init__(self):
        self.now = time.time()
        self.marked = []
        self.restored = None
        self.forgotten = None
        self.preview_ok = True

    def repo_list(self):
        return [("t", "C:/x/t"), ("u", "C:/x/u")]

    def list_marks(self, name):
        _off_gui()
        if name != "t":
            return []
        return [
            {"ref": "refs/sincro/marks/2-later", "label": "demo build",
             "sha": "m2", "epoch": self.now - 3600, "files": 4},
            {"ref": "refs/sincro/marks/1-earlier", "label": "before the refactor",
             "sha": "m1", "epoch": self.now - 4 * 86400, "files": 0},
        ]

    def mark_repo(self, name, label):
        _off_gui()
        self.marked.append((name, label))
        return True, f"marked '{label}'"

    def forget_mark(self, name, ref):
        self.forgotten = (name, ref)
        return True, "forgotten"

    def restore_repo_preview(self, name, sha):
        _off_gui()
        if not self.preview_ok:
            return False, "repo busy (merge/rebase in progress)"
        return True, {"changes": [("revert", "src/app.py"), ("delete", "new.txt"),
                                  ("revert", "locked.bin")],
                      "risky": ["locked.bin"]}

    def restore_repo(self, name, sha):
        _off_gui()
        self.restored = (name, sha)
        return True, "restored"


@pytest.fixture
def tab(qapp, monkeypatch):
    boxes = []
    monkeypatch.setattr(mt.QMessageBox, "information",
                        staticmethod(lambda *a, **k: boxes.append(("info", a[2]))))
    monkeypatch.setattr(mt.QMessageBox, "warning",
                        staticmethod(lambda *a, **k: boxes.append(("warn", a[2]))))
    monkeypatch.setattr(mt.QMessageBox, "critical",
                        staticmethod(lambda *a, **k: boxes.append(("crit", a[2]))))
    monkeypatch.setattr(mt.QMessageBox, "question",
                        staticmethod(lambda *a, **k: QMessageBox.Yes))
    ctl = Ctl()
    opened = []
    t = mt.MarksTab(ctl, on_open_state=lambda n, s: opened.append((n, s)))
    t.show()   # showEvent -> repo sync + async load
    yield ctl, t, boxes, opened
    t.close()
    t.deleteLater()


def _labels(t):
    return [t.tbl_marks.item(r, 0).text() for r in range(t.tbl_marks.rowCount())]


def test_marks_load_off_the_gui_thread_newest_first(tab, qspin):
    _ctl, t, _boxes, _opened = tab
    assert qspin(lambda: t.tbl_marks.rowCount() == 2)
    assert _labels(t) == ["demo build", "before the refactor"]
    assert t.tbl_marks.item(0, 2).text() == "4 file(s)"
    assert t.tbl_marks.item(1, 2).text() == "nothing"   # still matches the mark
    assert "2 mark(s)" in t.lbl_count.text()
    assert t.busy.active is False


def test_a_repo_without_marks_says_so_and_disables_the_actions(tab, qspin):
    _ctl, t, _boxes, _opened = tab
    assert qspin(lambda: t.tbl_marks.rowCount() == 2)
    t.cb_repo.setCurrentText("u")
    assert qspin(lambda: t.tbl_marks.rowCount() == 0)
    assert "no marks yet" in t.lbl_count.text()
    for b in (t.btn_changes, t.btn_open, t.btn_restore, t.btn_forget):
        assert not b.isEnabled()
    assert t.btn_new.isEnabled()   # you can always make the first one


def test_what_changed_since_lists_the_files_and_flags_the_risky(tab, qspin):
    _ctl, t, _boxes, _opened = tab
    assert qspin(lambda: t.tbl_marks.rowCount() == 2)
    t._load_changes()
    assert qspin(lambda: t.tbl_changes.rowCount() == 3)
    paths = [t.tbl_changes.item(r, 1).text() for r in range(3)]
    assert "⚠  locked.bin" in paths     # uncapturable content is called out
    assert "3 file(s) differ from 'demo build'" in t.lbl_changes.text()
    assert t.busy.active is False


def test_a_failed_comparison_is_reported_not_swallowed(tab, qspin):
    ctl, t, _boxes, _opened = tab
    assert qspin(lambda: t.tbl_marks.rowCount() == 2)
    ctl.preview_ok = False
    t._load_changes()
    assert qspin(lambda: "Could not compare" in t.lbl_changes.text())
    assert "merge/rebase" in t.lbl_changes.text()


def test_restore_previews_first_then_restores(tab, qspin, monkeypatch):
    ctl, t, boxes, _opened = tab
    assert qspin(lambda: t.tbl_marks.rowCount() == 2)
    # The confirm box is a real QMessageBox (it carries a Details list): answer
    # it without a human, the way the Time machine's own test does.
    monkeypatch.setattr(mt.QMessageBox, "exec_", lambda self: QMessageBox.Yes)
    t._restore()
    assert qspin(lambda: ctl.restored is not None)
    assert ctl.restored == ("t", "m2")
    assert qspin(lambda: any(k == "info" for k, _m in boxes))
    assert t.tbl_changes.rowCount() == 3   # the preview stayed on screen


def test_restore_is_abandoned_when_the_user_says_no(tab, qspin, monkeypatch):
    ctl, t, _boxes, _opened = tab
    assert qspin(lambda: t.tbl_marks.rowCount() == 2)
    monkeypatch.setattr(mt.QMessageBox, "exec_", lambda self: QMessageBox.Cancel)
    t._restore()
    assert qspin(lambda: t.tbl_changes.rowCount() == 3)
    time.sleep(0.2)
    assert ctl.restored is None
    assert t.btn_restore.isEnabled()   # the button comes back either way


def test_making_a_mark_asks_for_a_name_and_reloads(tab, qspin, monkeypatch):
    ctl, t, _boxes, _opened = tab
    assert qspin(lambda: t.tbl_marks.rowCount() == 2)
    monkeypatch.setattr(mt, "ask_label", lambda *a, **k: "  a new   moment ")
    t._new_mark()
    assert qspin(lambda: ctl.marked == [("t", "  a new   moment ")])
    assert qspin(lambda: t.btn_new.isEnabled())


def test_cancelling_the_name_dialog_marks_nothing(tab, qspin, monkeypatch):
    ctl, t, _boxes, _opened = tab
    assert qspin(lambda: t.tbl_marks.rowCount() == 2)
    monkeypatch.setattr(mt, "ask_label", lambda *a, **k: None)
    t._new_mark()
    time.sleep(0.1)
    assert ctl.marked == []


def test_ask_label_folds_whitespace_and_refuses_an_empty_name(qapp, monkeypatch):
    """An unnamed mark is unfindable, which defeats the whole feature."""
    monkeypatch.setattr(mt.QInputDialog, "getText",
                        staticmethod(lambda *a, **k: ("  two   words \n", True)))
    assert mt.ask_label() == "two words"
    monkeypatch.setattr(mt.QInputDialog, "getText",
                        staticmethod(lambda *a, **k: ("   ", True)))
    assert mt.ask_label() is None
    monkeypatch.setattr(mt.QInputDialog, "getText",
                        staticmethod(lambda *a, **k: ("real name", False)))
    assert mt.ask_label() is None      # cancelled


def test_forget_drops_the_name_after_confirming(tab, qspin):
    ctl, t, _boxes, _opened = tab
    assert qspin(lambda: t.tbl_marks.rowCount() == 2)
    t._forget_mark()
    assert ctl.forgotten == ("t", "refs/sincro/marks/2-later")


def test_open_in_time_machine_hands_over_the_state(tab, qspin):
    _ctl, t, _boxes, opened = tab
    assert qspin(lambda: t.tbl_marks.rowCount() == 2)
    t.tbl_marks.selectRow(1)
    t._open_in_time_machine()
    assert opened == [("t", "m1")]


def test_a_mark_event_for_the_shown_repo_refreshes_it(tab, qspin):
    """A mark made from the tray, the CLI or an agent's hook must appear here
    without the user reloading anything."""
    _ctl, t, _boxes, _opened = tab
    assert qspin(lambda: t.tbl_marks.rowCount() == 2)

    class Ev:
        action, repo = "mark", "t"

    t._stale = False
    t.notice_event(Ev())
    assert t._stale is True
    Ev.repo = "other"
    t._stale = False
    t.notice_event(Ev())
    assert t._stale is False    # another repo's mark is not our problem
    Ev.repo, Ev.action = "t", "snapshot"
    t.notice_event(Ev())
    assert t._stale is False    # nor is an ordinary snapshot


def test_a_stale_comparison_never_overwrites_a_newer_one(tab, qspin):
    """Generation token: two comparisons in flight, the older answer is dropped."""
    _ctl, t, _boxes, _opened = tab
    assert qspin(lambda: t.tbl_marks.rowCount() == 2)
    t._changes_gen = 7
    t._on_preview_ready(3, True, {"changes": [("revert", "stale.py")],
                                  "risky": []}, {"label": "old"})
    assert t.tbl_changes.rowCount() == 0
