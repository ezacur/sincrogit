"""Timeline v4 proposal (offscreen): a refined TimeMachineTab — same machinery
by inheritance, so these tests check exactly the three v4 promises: a precise
tooltip on EVERY control, the rare actions folded into "More ▾", and the
select-all/none pair collapsed into one toggle. Plus the theme fix that makes
combos stop looking like buttons."""

import pytest
from PyQt5.QtWidgets import QCheckBox, QComboBox, QPushButton, QRadioButton

import sincrogit.gui.timeline_v4_tab as tv4
from test_gui_time_machine_tab import Ctl


@pytest.fixture
def tab(qapp, tmp_path):
    ctl = Ctl(str(tmp_path))
    t = tv4.TimelineV4Tab(ctl)
    t.show()
    yield ctl, t
    t.close()
    t.deleteLater()


def test_every_control_has_a_tooltip(tab):
    _ctl, t = tab
    bare = [w.text() or w.objectName() or type(w).__name__
            for cls in (QPushButton, QCheckBox, QComboBox, QRadioButton)
            for w in t.findChildren(cls)
            if not w.toolTip().strip()]
    assert bare == [], f"controls without a tooltip: {bare}"
    assert t.lst.toolTip() and t.tbl_files.toolTip()


def test_rare_actions_fold_into_the_more_menu(tab, qspin, monkeypatch):
    _ctl, t = tab
    assert qspin(lambda: t.lst.count() > 0)
    assert t.btn_fetch.text() == "More ▾" and t.btn_fetch.menu() is not None
    labels = [a.text() for a in t.btn_fetch.menu().actions()]
    assert labels == ["Fetch autosnaps", "Restore ENTIRE repo…"]
    assert not t.btn_restore_repo.isVisible()    # demoted, not removed
    called = []
    monkeypatch.setattr(t, "_restore_repo", lambda: called.append("repo"))
    # The action stays wired to the ORIGINAL slot machinery... the menu action
    # was bound at construction, so trigger the real one instead:
    t._act_fetch.trigger()                        # runs the inherited worker
    assert qspin(lambda: not t.busy.active)       # fetch completed
    # Enabled-state mirrors the hidden button (needs a selected state).
    assert t._act_restore_repo.isEnabled() == t.btn_restore_repo.isEnabled()


def test_folded_buttons_stay_hidden_across_mode_changes(tab, qspin):
    _ctl, t = tab
    assert qspin(lambda: t.lst.count() > 0)
    t.rb_today.setChecked(True)                   # parent re-shows per mode...
    assert not t.btn_none.isVisible()             # ...v4 keeps them folded
    assert not t.btn_restore_repo.isVisible()
    assert t.btn_all.isVisible()                  # the single toggle survives
    t.rb_then.setChecked(True)
    assert not t.btn_none.isVisible()


def test_all_toggle_checks_and_unchecks(tab, qspin):
    ctl, t = tab
    assert qspin(lambda: t.lst.count() > 0)
    t.rb_today.setChecked(True)
    assert qspin(lambda: t.tbl_files.rowCount() == 3)
    t.btn_all.setChecked(True)                    # tick all (risky stays out)
    assert "Restore selected (2)" in t.btn_restore_sel.text()
    t.btn_all.setChecked(False)                   # same button unticks
    assert t.btn_restore_sel.text() == "Restore selected"
    assert not t.btn_restore_sel.isEnabled()


def test_inherited_machinery_still_works(tab, qspin):
    """v4 is v1 underneath: the rail loads, selecting a state fills the files,
    and pinning via double-click still flips to the file's versions."""
    _ctl, t = tab
    assert qspin(lambda: t.tbl_files.rowCount() == 2)
    t.tbl_files.selectRow(0)
    t._pin_from_row(None)
    assert "src/app.py" in t.lbl_pin.text()
    assert qspin(lambda: t.lst.count() > 0)


def test_theme_distinguishes_combos_from_buttons():
    from sincrogit.gui.theme import stylesheet
    s = stylesheet("light")
    assert "QComboBox::down-arrow" in s           # a visible list-opener arrow
    assert "border-left: 1px solid" in s.split("QComboBox::drop-down")[1] \
        .split("}")[0]                            # the separated arrow well
