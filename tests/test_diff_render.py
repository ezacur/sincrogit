"""Diff HTML rendering (pure functions): unified, side-by-side, intra-line."""

from sincrogit.gui.history_dialog import _DIFF_LIGHT, _diff_html, _mark_intraline
from sincrogit.gui.time_machine_dialog import _diff_html_sbs


def test_mark_intraline_highlights_only_the_change():
    old, new = _mark_intraline("return 1", "return 2", _DIFF_LIGHT)
    assert old.startswith("return ") and new.startswith("return ")
    assert _DIFF_LIGHT["del_hl"] in old and ">1<" in old
    assert _DIFF_LIGHT["add_hl"] in new and ">2<" in new


def test_unified_diff_embeds_intraline_spans():
    out = _diff_html("a\nreturn 1\nb\n", "a\nreturn 2\nb\n")
    assert _DIFF_LIGHT["del_hl"] in out and _DIFF_LIGHT["add_hl"] in out
    assert "@@" in out  # hunk header preserved


def test_unified_diff_escapes_html():
    out = _diff_html("x = 1\n", "x = <b>1</b>\n")
    assert "<b>" not in out and "&lt;b&gt;" in out


def test_unified_diff_no_differences():
    assert "no differences" in _diff_html("same\n", "same\n")


def test_sbs_two_columns_and_intraline():
    out = _diff_html_sbs("a\nreturn 1\nb\n", "a\nreturn 2\nb\n")
    assert "selected version" in out and "current file" in out
    assert "<table" in out
    assert _DIFF_LIGHT["del_hl"] in out and _DIFF_LIGHT["add_hl"] in out


def test_sbs_no_differences():
    assert "no differences" in _diff_html_sbs("same\n", "same\n")
