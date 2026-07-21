"""Timeline v4 proposal (offscreen): the Time machine's machinery under a
VISIBLY different skin — compact pill rail with churn micro-bars, segmented
mode switch, state banner — plus the first draft's promises (a precise tooltip
on every control, rare actions folded into "More ▾", one "All" toggle). The
machinery itself is inherited from TimeMachineTab, so these tests focus on
what v4 changes; plus the theme fix that distinguishes combos from buttons."""

import pytest
from PyQt5.QtWidgets import (QCheckBox, QComboBox, QPushButton, QRadioButton,
                             QToolButton)

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
            for cls in (QPushButton, QCheckBox, QComboBox, QRadioButton,
                        QToolButton)
            for w in t.findChildren(cls)
            if not w.toolTip().strip()]
    assert bare == [], f"controls without a tooltip: {bare}"
    assert t.lst.toolTip() and t.tbl_files.toolTip() and t.banner.toolTip()


def test_rail_uses_the_compact_delegate(tab, qspin):
    """The visual centerpiece: v4's rail rows are the compact pill+micro-bar
    kind, not v1's two-line cards."""
    _ctl, t = tab
    assert isinstance(t.lst.itemDelegate(), tv4._CompactRailDelegate)
    assert qspin(lambda: t.lst.count() > 0)
    # Compact rows really are compact: an entry row's height is v4's, not v1's.
    from sincrogit.gui.time_machine_tab import _CARD_H
    entry_rows = [i for i in range(t.lst.count())
                  if t.lst.item(i).data(tv4.ROLE_ENTRY) is not None]
    h = t.lst.sizeHintForRow(entry_rows[0])
    assert h == tv4._ROW_H < _CARD_H


def test_segmented_switch_drives_the_hidden_radios(tab, qspin):
    _ctl, t = tab
    assert qspin(lambda: t.lst.count() > 0)
    assert not t.rb_then.isVisible() and not t.rb_today.isVisible()
    assert t.seg_then.isChecked() and not t.seg_today.isChecked()
    t.seg_today.click()
    assert t.rb_today.isChecked()                 # radios = source of truth
    assert qspin(lambda: t.tbl_files.rowCount() == 3)   # today mode loaded
    t.rb_then.setChecked(True)                    # programmatic path syncs back
    assert t.seg_then.isChecked() and not t.seg_today.isChecked()


def test_banner_reflects_the_selected_state(tab, qspin):
    _ctl, t = tab
    assert qspin(lambda: t.tbl_files.rowCount() == 2)   # newest state opened
    txt = t.banner.text()
    assert "snap" in txt and "2 files" in txt and "+3" in txt
    assert "border-left" in t.banner.styleSheet()  # kind-colored edge
    # Selecting the seal updates it.
    rows = [i for i in range(t.lst.count())
            if (t.lst.item(i).data(tv4.ROLE_ENTRY) or {}).get("kind") == "seal"]
    t.lst.setCurrentRow(rows[0])
    assert qspin(lambda: "seal" in t.banner.text())
    assert "sealed window" in t.banner.text()      # the subject rides along


def test_rare_actions_fold_into_the_more_menu(tab, qspin):
    _ctl, t = tab
    assert qspin(lambda: t.lst.count() > 0)
    assert t.btn_fetch.text() == "More ▾" and t.btn_fetch.menu() is not None
    labels = [a.text() for a in t.btn_fetch.menu().actions()]
    assert labels == ["Fetch autosnaps", "Restore ENTIRE repo…"]
    assert not t.btn_restore_repo.isVisible()
    t._act_fetch.trigger()
    assert qspin(lambda: not t.busy.active)
    assert t._act_restore_repo.isEnabled() == t.btn_restore_repo.isEnabled()


def test_folded_buttons_stay_hidden_across_mode_changes(tab, qspin):
    _ctl, t = tab
    assert qspin(lambda: t.lst.count() > 0)
    t.seg_today.click()                            # parent re-shows per mode…
    assert not t.btn_none.isVisible()              # …v4 keeps them folded
    assert not t.btn_restore_repo.isVisible()
    assert not t.rb_today.isVisible()
    assert t.btn_all.isVisible()
    t.seg_then.click()
    assert not t.btn_none.isVisible()


def test_all_toggle_checks_and_unchecks(tab, qspin):
    _ctl, t = tab
    assert qspin(lambda: t.lst.count() > 0)
    t.seg_today.click()
    assert qspin(lambda: t.tbl_files.rowCount() == 3)
    t.btn_all.setChecked(True)                     # tick all (risky stays out)
    assert "Restore selected (2)" in t.btn_restore_sel.text()
    t.btn_all.setChecked(False)
    assert t.btn_restore_sel.text() == "Restore selected"
    assert not t.btn_restore_sel.isEnabled()


def test_inherited_machinery_still_works(tab, qspin):
    """v4 is v1 underneath: pinning via double-click still flips the rail to
    the file's versions."""
    _ctl, t = tab
    assert qspin(lambda: t.tbl_files.rowCount() == 2)
    t.tbl_files.selectRow(0)
    t._pin_from_row(None)
    assert "src/app.py" in t.lbl_pin.text()
    assert qspin(lambda: t.lst.count() > 0)


def test_theme_distinguishes_combos_and_styles_the_segments():
    from sincrogit.gui.theme import stylesheet
    s = stylesheet("light")
    assert "QComboBox::down-arrow" in s            # a visible list-opener arrow
    assert "border-left: 1px solid" in s.split("QComboBox::drop-down")[1] \
        .split("}")[0]                             # the separated arrow well
    assert 'QToolButton[cssClass="seg"]:checked' in s
