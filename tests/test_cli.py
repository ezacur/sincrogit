"""CLI flag-combination validation (sincrogit.__main__._cli_conflict).

The one-shot flags are independent argparse options, so nonsensical
combinations used to pass silently (one action ran, the rest ignored). These
lock in the up-front rejection.
"""

import argparse

from sincrogit.__main__ import _cli_conflict, main


def _args(**kw):
    """An argparse.Namespace with every CLI flag at its default, overridden by kw."""
    d = dict(tray=False, headless=False, history=None, autosnaps=False,
             commit=None, apply_handoff=None, doctor=False, autostart=None,
             snapshot_once=False, seal_once=False, sync_once=False,
             pick=None, message=None, yes=False)
    d.update(kw)
    return argparse.Namespace(**d)


def test_no_flags_is_fine():
    """No action at all is coherent (main() then prints the usage hint)."""
    assert _cli_conflict(_args()) is None


def test_single_action_is_fine():
    assert _cli_conflict(_args(doctor=True)) is None
    assert _cli_conflict(_args(commit="repo")) is None


def test_once_flags_combine():
    """--snapshot-once/--seal-once/--sync-once are DELIBERATELY combinable."""
    assert _cli_conflict(_args(snapshot_once=True, seal_once=True, sync_once=True)) is None


def test_two_distinct_actions_rejected():
    msg = _cli_conflict(_args(history="f.py", commit="repo"))
    assert msg and "combined" in msg


def test_once_batch_conflicts_with_other_action():
    msg = _cli_conflict(_args(seal_once=True, commit="repo"))
    assert msg and "combined" in msg


def test_pick_without_history_rejected():
    msg = _cli_conflict(_args(pick=2))
    assert msg and "--pick" in msg


def test_message_without_commit_rejected():
    msg = _cli_conflict(_args(message="hi"))
    assert msg and "--commit" in msg


def test_yes_without_commit_rejected():
    assert _cli_conflict(_args(yes=True)) is not None


def test_message_and_yes_are_mutually_exclusive():
    msg = _cli_conflict(_args(commit="repo", message="hi", yes=True))
    assert msg and "mutually exclusive" in msg


def test_main_rejects_conflict_with_exit_2(capsys):
    """End-to-end: the real parser + validator reject before touching any config."""
    rc = main(["--pick", "3"])  # orphan --pick, no --history
    assert rc == 2
    assert "--pick" in capsys.readouterr().err


def test_autostart_is_an_action_like_any_other():
    assert _cli_conflict(_args(autostart="on")) is None
    msg = _cli_conflict(_args(autostart="on", doctor=True))
    assert msg and "--autostart" in msg and "--doctor" in msg
