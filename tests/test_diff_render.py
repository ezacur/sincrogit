"""Diff HTML rendering (pure functions): unified, side-by-side, intra-line."""

from sincrogit.gui.diff import DIFF_LIGHT, diff_html, diff_html_sbs, mark_intraline


def test_mark_intraline_highlights_only_the_change():
    old, new = mark_intraline("return 1", "return 2", DIFF_LIGHT)
    assert old.startswith("return ") and new.startswith("return ")
    assert DIFF_LIGHT["del_hl"] in old and ">1<" in old
    assert DIFF_LIGHT["add_hl"] in new and ">2<" in new


def test_unified_diff_embeds_intraline_spans():
    out = diff_html("a\nreturn 1\nb\n", "a\nreturn 2\nb\n")
    assert DIFF_LIGHT["del_hl"] in out and DIFF_LIGHT["add_hl"] in out
    assert "@@" in out  # hunk header preserved


def test_unified_diff_escapes_html():
    out = diff_html("x = 1\n", "x = <b>1</b>\n")
    assert "<b>" not in out and "&lt;b&gt;" in out


def test_unified_diff_no_differences():
    assert "no differences" in diff_html("same\n", "same\n")


def test_sbs_two_columns_and_intraline():
    out = diff_html_sbs("a\nreturn 1\nb\n", "a\nreturn 2\nb\n")
    assert "selected version" in out and "current file" in out
    assert "<table" in out
    assert DIFF_LIGHT["del_hl"] in out and DIFF_LIGHT["add_hl"] in out


def test_sbs_no_differences():
    assert "no differences" in diff_html_sbs("same\n", "same\n")
