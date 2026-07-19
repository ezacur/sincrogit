"""Unified Time machine tab (offscreen): the WHEN rail (timeline / pinned-file
versions), the compare switch (then / vs today), pin flow, search, restores
(file / selected / whole repo), save-a-copy — all async, never on the GUI
thread. Ports the coverage of the retired history & time-machine dialogs."""

import threading
import time

import pytest
from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import QMessageBox

import sincrogit.gui.time_machine_tab as tmt


def _off_gui():
    assert threading.current_thread() is not threading.main_thread(), \
        "git work must NOT run on the GUI thread"


class Ctl:
    theme = {"is_dark": False}

    def __init__(self, tmpdir):
        self.tmpdir = tmpdir
        self.now = time.time()
        self.restored = None
        self.exported = None
        self.fetch_fail = False

    def repo_list(self):
        return [("t", self.tmpdir), ("u", self.tmpdir)]

    def snapshot_timeline(self, name, limit=200):
        _off_gui()
        if name != "t":
            return []
        return [
            {"sha": "s2", "parent": "s1", "epoch": self.now - 60,
             "subject": "sincro: snapshot", "kind": "snapshot",
             "files": [("M", "src/app.py", 3, 1), ("A", "img.png", None, None)]},
            {"sha": "s1", "parent": "s0", "epoch": self.now - 3 * 86400,
             "subject": "feat: sealed window", "kind": "seal",
             "files": [("M", "src/app.py", 10, 2)]},
        ]

    def file_history(self, name, rel):
        _off_gui()
        return [
            {"sha": "s2", "epoch": self.now - 60, "source": "snapshot", "subject": "x"},
            {"sha": "s1", "epoch": self.now - 120, "source": "sealed", "subject": "y"},
            {"sha": "s0", "epoch": self.now - 180, "source": "autosnap", "subject": "z"},
        ]

    def restore_repo_preview(self, name, sha):
        _off_gui()
        return True, {"changes": [("revert", "src/app.py"), ("delete", "new.txt"),
                                  ("revert", "locked.bin")],
                      "risky": ["locked.bin"]}

    def restore_files(self, name, paths, sha):
        self.restored = (name, tuple(paths), sha)
        return True, f"restored {len(paths)} file(s)"

    def restore_file(self, name, rel, sha):
        self.restored = ("file", rel, sha)
        return True, "restored"

    def restore_repo(self, name, sha):
        self.restored = ("repo", name, sha)
        return True, "restored"

    def file_text_at(self, name, rel, sha):
        _off_gui()
        return {"s0": "a\n", "s1": "a\nb\n", "s2": "a\nb\nc\n"}.get(sha, "")

    def current_text(self, name, rel):
        _off_gui()
        return "a\nCURRENT\n"

    def export_file_version(self, n, r, sha, dest):
        self.exported = (r, sha, dest)
        with open(dest, "w") as fh:
            fh.write("old content")
        return True, "saved"

    def search_in_file_versions(self, n, r, text):
        _off_gui()
        return [("s2", 0), ("s1", 1), ("s0", 0)]  # appeared at s1, vanished at s2

    def fetch_autosnaps(self, name):
        _off_gui()
        if self.fetch_fail:
            raise RuntimeError("remote unreachable")
        return [{"host": "other"}]


@pytest.fixture
def tab(qapp, tmp_path, monkeypatch):
    boxes = []
    monkeypatch.setattr(tmt.QMessageBox, "information",
                        staticmethod(lambda *a, **k: boxes.append(("info", a[2]))))
    monkeypatch.setattr(tmt.QMessageBox, "warning",
                        staticmethod(lambda *a, **k: boxes.append(("warn", a[2]))))
    monkeypatch.setattr(tmt.QMessageBox, "critical",
                        staticmethod(lambda *a, **k: boxes.append(("crit", a[2]))))
    monkeypatch.setattr(tmt.QMessageBox, "question",
                        staticmethod(lambda *a, **k: QMessageBox.Yes))
    ctl = Ctl(str(tmp_path))
    t = tmt.TimeMachineTab(ctl)
    t.show()   # fires showEvent -> repo sync + async load
    yield ctl, t, boxes
    t.close()
    t.deleteLater()


def _cards(t):
    return [t.lst.item(i).data(tmt.ROLE_ENTRY) for i in range(t.lst.count())
            if t.lst.item(i).data(tmt.ROLE_ENTRY) is not None]


# ------------------------------------------------------------- WHEN rail
def test_loads_async_with_day_headers(tab, qspin):
    _, t, _ = tab
    assert qspin(lambda: t.lst.count() > 0)
    cards = _cards(t)
    assert [c["sha"] for c in cards] == ["s2", "s1"]
    assert t.lst.count() - len(cards) == 2       # two days -> two captions
    assert "1 snapshot(s) · 1 seal(s)" in t.lbl_count.text()


def test_seals_only_filter(tab, qspin):
    _, t, _ = tab
    assert qspin(lambda: t.lst.count() > 0)
    t.cb_filter.setCurrentIndex(1)
    assert [c["kind"] for c in _cards(t)] == ["seal"]


def test_repo_switch_clears_everything(tab, qspin):
    _, t, _ = tab
    assert qspin(lambda: t.lst.count() > 0)
    t.cb_repo.setCurrentText("u")                # empty repo
    assert qspin(lambda: "0 snapshot(s)" in t.lbl_count.text())
    assert _cards(t) == [] and t.tbl_files.rowCount() == 0


# ------------------------------------------------- "what changed then" mode
def test_then_mode_files_and_diff(tab, qspin):
    _, t, _ = tab
    assert qspin(lambda: t.tbl_files.rowCount() == 2)   # newest auto-selects
    assert t.tbl_files.item(0, 1).text() == "src/app.py"
    assert t.tbl_files.item(0, 2).text() == "+3"
    assert t.tbl_files.item(1, 2).text() == "bin"
    assert qspin(lambda: "this snapshot" in t.diff.toPlainText())


def test_binary_file_shows_placeholder(tab, qspin):
    _, t, _ = tab
    assert qspin(lambda: t.tbl_files.rowCount() == 2)
    t.tbl_files.selectRow(1)                     # img.png (binary)
    assert qspin(lambda: "binary file" in t.diff.toPlainText())


# ------------------------------------------------------------ "vs today" mode
def test_today_mode_lists_differing_files_with_risky(tab, qspin):
    _, t, _ = tab
    assert qspin(lambda: t.lst.count() > 0)
    t.rb_today.setChecked(True)
    assert qspin(lambda: t.tbl_files.rowCount() == 3)
    assert "3 file(s) differ" in t.lbl_files.text()
    risky_row = next(i for i in range(3)
                     if t.tbl_files.item(i, 2).text() == "locked.bin")
    chk = t.tbl_files.item(risky_row, 0)
    assert chk.text() == "⚠" and not (chk.flags() & Qt.ItemIsUserCheckable)
    assert qspin(lambda: "current file" in t.diff.toPlainText())  # diff vs today


def test_selective_restore_roundtrip(tab, qspin):
    ctl, t, boxes = tab
    assert qspin(lambda: t.lst.count() > 0)
    t.rb_today.setChecked(True)
    assert qspin(lambda: t.tbl_files.rowCount() == 3)
    t._set_all(Qt.Checked)                       # risky stays unchecked
    assert "Restore selected (2)" in t.btn_restore_sel.text()
    t._restore_selected()                        # question auto-answers Yes
    assert qspin(lambda: ctl.restored is not None)
    assert ctl.restored == ("t", ("src/app.py", "new.txt"), "s2")
    assert qspin(lambda: boxes and boxes[-1][0] == "info")


def test_restore_whole_repo_after_preview_confirm(tab, qspin, monkeypatch):
    ctl, t, _ = tab
    assert qspin(lambda: t.lst.count() > 0)
    monkeypatch.setattr(tmt.QMessageBox, "exec_", lambda self: QMessageBox.Yes)
    t._restore_repo()
    assert qspin(lambda: ctl.restored == ("repo", "t", "s2"))


def test_today_files_table_caps_and_announces(tab, qspin):
    _, t, _ = tab
    assert qspin(lambda: t.lst.count() > 0)
    payload = {"changes": [("revert", f"f{i}.txt")
                           for i in range(t.MAX_FILE_ROWS + 50)], "risky": []}
    t._files_gen += 1
    t._on_today_ready(t._files_gen, True, payload, "s2")
    assert t.tbl_files.rowCount() == t.MAX_FILE_ROWS
    assert "showing the first" in t.lbl_files.text()
    assert str(t.MAX_FILE_ROWS + 50) in t.lbl_files.text()


# ------------------------------------------------------------- pinned file
def test_pin_via_double_click_shows_versions(tab, qspin):
    _, t, _ = tab
    assert qspin(lambda: t.tbl_files.rowCount() == 2)
    t.tbl_files.selectRow(0)
    t._pin_from_row(None)                        # pin src/app.py
    assert "src/app.py" in t.lbl_pin.text()
    assert qspin(lambda: len(_cards(t)) == 3)    # its versions, incl. autosnap
    assert not t._files_box.isVisible()          # the file IS the QUÉ
    assert t.ed_search.isVisible() and t.btn_hunks.isVisible()
    assert qspin(lambda: "previous version" in t.diff.toPlainText())


def test_pinned_search_marks_transitions(tab, qspin):
    _, t, _ = tab
    assert qspin(lambda: t.tbl_files.rowCount() == 2)
    t._set_pin("src/app.py")
    assert qspin(lambda: len(_cards(t)) == 3)
    t.ed_search.setText("def foo")
    t._find_in_versions()
    assert qspin(lambda: "changes in 2 version(s)" in t.lbl_info.text())
    marked = [t.lst.item(i).data(tmt.ROLE_MARK) for i in range(t.lst.count())
              if t.lst.item(i).data(tmt.ROLE_ENTRY) is not None]
    assert marked.count(True) == 2               # appeared + vanished


def test_pinned_restore_file(tab, qspin):
    ctl, t, _ = tab
    assert qspin(lambda: t.lst.count() > 0)
    t._set_pin("src/app.py")
    assert qspin(lambda: len(_cards(t)) == 3)
    t._restore_file()                            # question auto-answers Yes
    assert qspin(lambda: ctl.restored == ("file", "src/app.py", "s2"))


def test_unpin_returns_to_the_timeline(tab, qspin):
    _, t, _ = tab
    assert qspin(lambda: t.lst.count() > 0)
    t._set_pin("src/app.py")
    assert qspin(lambda: len(_cards(t)) == 3)
    t._set_pin(None)
    assert qspin(lambda: len(_cards(t)) == 2)
    assert t._files_box.isVisible() and not t.ed_search.isVisible()


def test_focus_repo_preselects_and_pins(tab, qspin):
    _, t, _ = tab
    t.focus_repo("t", pin="src/app.py")
    assert "src/app.py" in t.lbl_pin.text()
    assert qspin(lambda: len(_cards(t)) == 3)


# ----------------------------------------------------------- shared actions
def test_save_a_copy_async(tab, qspin, monkeypatch, tmp_path):
    ctl, t, boxes = tab
    assert qspin(lambda: t.tbl_files.rowCount() == 2)
    dest = str(tmp_path / "app (old).py")
    monkeypatch.setattr(tmt.QFileDialog, "getSaveFileName",
                        staticmethod(lambda *a, **k: (dest, "")))
    t._save_copy()
    assert qspin(lambda: ctl.exported is not None)
    assert ctl.exported == ("src/app.py", "s2", dest)
    assert qspin(lambda: boxes and "Saved to" in boxes[-1][1])
    assert t.btn_saveas.isEnabled()


def test_fetch_autosnaps_async_ok_and_error(tab, qspin):
    ctl, t, boxes = tab
    assert qspin(lambda: t.lst.count() > 0)
    t._fetch_autosnaps()
    assert not t.btn_fetch.isEnabled()
    assert qspin(lambda: t.btn_fetch.isEnabled())
    assert "1 autosnap state(s)" in t.lbl_info.text()
    ctl.fetch_fail = True
    t._fetch_autosnaps()
    assert qspin(lambda: boxes and boxes[-1][0] == "warn"
                 and "remote unreachable" in boxes[-1][1])


def test_notice_event_refreshes_only_matching_repo(tab, qspin):
    _, t, _ = tab
    assert qspin(lambda: t.lst.count() > 0)

    class Ev:
        def __init__(self, repo, action):
            self.repo, self.action = repo, action

    t._debounce.stop()
    t._stale = False
    t.notice_event(Ev("other", "snapshot"))
    # An event on ANOTHER repo must NOT stale the view (revisiting the tab
    # would reload needlessly) nor schedule a reload.
    assert not t._stale and not t._debounce.isActive()
    t.notice_event(Ev("t", "snapshot"))       # the shown repo -> reload
    assert t._stale and t._debounce.isActive()


# ------------------------------------------------------- busy indicator wiring
def test_busy_bar_shows_during_load_and_hides_after(tab, qspin):
    """The rail load runs on a worker: the bar must be visible while it runs
    (start() is synchronous on dispatch) and gone once the result lands."""
    _, t, _ = tab
    t._set_pin(None)                 # trigger a fresh reload
    assert t.busy.active             # start() ran synchronously before the thread
    assert qspin(lambda: not t.busy.active)   # stop() ran when the worker finished
    assert not t.busy.isVisible()


def test_busy_bar_covers_overlapping_workers(tab, qspin):
    """Rail + diff can run at once: the ref count keeps the bar up until BOTH
    finish, never flickering to idle between them."""
    _, t, _ = tab
    assert qspin(lambda: t.tbl_files.rowCount() == 2)   # initial load settled
    t._set_pin("src/app.py")         # reload (rail) …
    t._load_diff()                   # … and a diff, overlapping
    assert t.busy.active
    assert qspin(lambda: not t.busy.active)
