"""Time Machine dialog (offscreen): async timeline, checkbox selection with
unselectable risky rows, diff views, and the selective restore round trip."""

import os
import threading
import time

import pytest
from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import QMessageBox

import sincrogit.gui.time_machine_dialog as tm


class Ctl:
    theme = {"is_dark": False}

    def __init__(self):
        self.restored = None

    def repo_list(self):
        return [("t", os.getcwd())]

    def repo_history(self, name, limit=200):
        assert threading.current_thread() is not threading.main_thread()
        now = time.time()
        return [
            {"sha": "wip111", "epoch": now - 60, "source": "snapshot",
             "subject": "(auto-snapshot)"},
            {"sha": "seal22", "epoch": now - 7200, "source": "sealed",
             "subject": "feat: base"},
        ]

    def restore_repo_preview(self, name, sha):
        assert threading.current_thread() is not threading.main_thread()
        if self.restored:  # after the restore everything matches
            return True, {"changes": [], "risky": []}
        return True, {"changes": [("revert", "a.txt"), ("delete", "d.txt"),
                                  ("revert", "secret.bin")],
                      "risky": ["secret.bin"]}

    def file_text_at(self, name, rel, sha):
        return "line1\nold2\nline3\n"

    def current_text(self, name, rel):
        return "line1\nnew2\nline3\nextra\n"

    def restore_files(self, name, paths, sha):
        assert threading.current_thread() is not threading.main_thread()
        self.restored = (name, tuple(paths), sha)
        return True, f"restored {len(paths)} file(s)"


@pytest.fixture
def dlg(qapp, qspin, monkeypatch):
    monkeypatch.setattr(tm.QMessageBox, "question",
                        staticmethod(lambda *a, **k: QMessageBox.Yes))
    boxes = []
    monkeypatch.setattr(tm.QMessageBox, "information",
                        staticmethod(lambda *a, **k: boxes.append(("info", a[2]))))
    monkeypatch.setattr(tm.QMessageBox, "critical",
                        staticmethod(lambda *a, **k: boxes.append(("crit", a[2]))))
    ctl = Ctl()
    d = tm.TimeMachineDialog(ctl)
    d.show()
    assert qspin(lambda: d.tbl_ver.rowCount() == 2)     # timeline loaded async
    assert qspin(lambda: d.tbl_files.rowCount() == 3)   # newest auto-selected
    yield ctl, d, boxes
    d.close()


def test_files_listed_with_risk_note(dlg):
    _, d, _ = dlg
    assert "3 file(s) differ" in d.lbl_files.text()
    assert "1 at risk" in d.lbl_files.text()


def test_risky_row_not_checkable(dlg):
    _, d, _ = dlg
    item = d.tbl_files.item(2, 0)
    assert not (item.flags() & Qt.ItemIsUserCheckable)
    assert item.text() == "⚠"


def test_select_all_counts_only_safe_rows(dlg):
    _, d, _ = dlg
    assert not d.btn_restore.isEnabled()
    d._set_all(Qt.Checked)
    assert d.btn_restore.isEnabled() and "(2)" in d.btn_restore.text()


def test_diff_views(dlg, qspin):
    _, d, _ = dlg
    d.tbl_files.selectRow(0)
    # The diff loads on a worker ("Loading…" in between): spin until it lands —
    # a single processEvents() pass is a race the suite's load can lose.
    assert qspin(lambda: "old2" in d.preview.toPlainText())
    assert "new2" in d.preview.toPlainText()
    d.cb_sbs.setChecked(True)
    assert qspin(lambda: "selected version" in d.preview.toHtml())
    assert "current file" in d.preview.toHtml()


def test_selective_restore_roundtrip(dlg, qspin):
    ctl, d, boxes = dlg
    d._set_all(Qt.Checked)
    d._restore_selected()  # confirm auto-answers Yes
    assert qspin(lambda: ctl.restored is not None)
    assert ctl.restored == ("t", ("a.txt", "d.txt"), "wip111")
    assert qspin(lambda: "already matches" in d.lbl_files.text())  # recomputed
    assert boxes and boxes[-1][0] == "info"


def test_files_table_caps_and_announces(dlg):
    """A version differing in tens of thousands of paths froze the GUI on every
    click; the table now caps the rows and says so — never a silent cut."""
    _, d, _ = dlg
    d._sha = "capsha"
    payload = {"changes": [("revert", f"f{i}.txt")
                           for i in range(d.MAX_FILE_ROWS + 50)],
               "risky": []}
    d._on_files_ready(True, payload, "capsha")
    assert d.tbl_files.rowCount() == d.MAX_FILE_ROWS
    assert "showing the first" in d.lbl_files.text()
    assert str(d.MAX_FILE_ROWS + 50) in d.lbl_files.text()  # the true total stays visible
