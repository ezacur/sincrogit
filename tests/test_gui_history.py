"""File-history dialog (offscreen): async autosnap fetch, version search
annotations, and Save-a-copy — all without ever blocking the GUI thread."""

import os
import threading
import time

import pytest

import sincrogit.gui.history_dialog as hd


class Ctl:
    theme = {"is_dark": False}

    def __init__(self, tmpdir):
        self.tmpdir = tmpdir
        self.exported = None
        self.fetch_fail = False

    def repo_list(self):
        return [("t", self.tmpdir)]

    def file_history(self, n, r):
        now = time.time()
        return [
            {"sha": "s3", "epoch": now - 10, "source": "snapshot", "subject": "x"},
            {"sha": "s2", "epoch": now - 20, "source": "sealed", "subject": "y"},
            {"sha": "s1", "epoch": now - 30, "source": "sealed", "subject": "z"},
        ]

    def file_text_at(self, n, r, sha):
        return ""

    def current_text(self, n, r):
        return ""

    def fetch_autosnaps(self, name):
        assert threading.current_thread() is not threading.main_thread(), \
            "fetch must NOT run on the GUI thread"
        time.sleep(0.1)
        if self.fetch_fail:
            raise RuntimeError("remote unreachable")
        return [{"host": "other"}, {"host": "other2"}]

    def search_in_file_versions(self, n, r, text):
        assert threading.current_thread() is not threading.main_thread()
        return [("s3", 0), ("s2", 1), ("s1", 0)]  # appeared at s2, vanished at s3

    def export_file_version(self, n, r, sha, dest):
        self.exported = (r, sha, dest)
        with open(dest, "w") as fh:
            fh.write("old content")
        return True, "saved"


@pytest.fixture
def dlg(qapp, tmp_path, monkeypatch):
    boxes = []
    monkeypatch.setattr(hd.QMessageBox, "information",
                        staticmethod(lambda *a, **k: boxes.append(("info", a[2]))))
    monkeypatch.setattr(hd.QMessageBox, "warning",
                        staticmethod(lambda *a, **k: boxes.append(("warn", a[2]))))
    monkeypatch.setattr(hd.QMessageBox, "critical",
                        staticmethod(lambda *a, **k: boxes.append(("crit", a[2]))))
    ctl = Ctl(str(tmp_path))
    d = hd.HistoryDialog(ctl)
    yield ctl, d, boxes
    d.close()


def test_fetch_autosnaps_async(dlg, qspin):
    ctl, d, boxes = dlg
    d._fetch_autosnaps()
    assert not d.btn_fetch.isEnabled()      # disabled while fetching
    assert not boxes                        # GUI thread returned before the result
    assert qspin(lambda: bool(boxes))
    assert boxes[0][0] == "info" and "2 autosnap state(s)" in boxes[0][1]
    assert d.btn_fetch.isEnabled()


def test_fetch_autosnaps_error_surfaced(dlg, qspin):
    ctl, d, boxes = dlg
    ctl.fetch_fail = True
    d._fetch_autosnaps()
    assert qspin(lambda: bool(boxes))
    assert boxes[0][0] == "warn" and "remote unreachable" in boxes[0][1]
    assert d.btn_fetch.isEnabled()


def test_search_annotates_transitions(dlg, qspin):
    _, d, _ = dlg
    d.ed_file.setText("code.py")
    d.show_history()
    d.ed_search.setText("def bar")
    d._find_in_versions()
    assert qspin(lambda: "changes in" in d.lbl_info.text())
    assert "2 version(s)" in d.lbl_info.text()  # appeared + vanished
    assert "1 occurrence" in d.tbl.item(1, 3).toolTip()


def test_save_a_copy(dlg, monkeypatch, tmp_path):
    ctl, d, boxes = dlg
    d.ed_file.setText("code.py")
    d.show_history()
    assert d.btn_saveas.isEnabled()
    dest = str(tmp_path / "code (old).py")
    monkeypatch.setattr(hd.QFileDialog, "getSaveFileName",
                        staticmethod(lambda *a, **k: (dest, "")))
    d._save_copy()
    assert ctl.exported is not None and ctl.exported[2] == dest
    assert boxes and "Saved to" in boxes[-1][1]
    assert os.path.exists(dest)
