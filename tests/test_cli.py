"""CLI flag-combination validation (sincrogit.__main__._cli_conflict).

The one-shot flags are independent argparse options, so nonsensical
combinations used to pass silently (one action ran, the rest ignored). These
lock in the up-front rejection.
"""

import argparse

from sincrogit import __version__
from sincrogit.__main__ import _cli_conflict, main
from sincrogit.runtime import version_report


def _args(**kw):
    """An argparse.Namespace with every CLI flag at its default, overridden by kw."""
    d = dict(tray=False, headless=False, history=None, autosnaps=False,
             commit=None, apply_handoff=None, doctor=False, autostart=None,
             status=False, log=False, repo=None, action=None, level=None,
             tail=None, snapshot_once=False, seal_once=False, sync_once=False,
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


def test_status_and_log_are_actions():
    assert _cli_conflict(_args(status=True)) is None
    assert _cli_conflict(_args(log=True, repo="x", action="seal",
                               level="INFO", tail=5)) is None
    msg = _cli_conflict(_args(status=True, log=True))
    assert msg and "--status" in msg and "--log" in msg


def test_view_modifiers_need_their_action():
    assert "--repo" in _cli_conflict(_args(repo="x"))
    assert _cli_conflict(_args(status=True, repo="x")) is None
    msg = _cli_conflict(_args(status=True, tail=5))
    assert msg and "--tail" in msg and "--log" in msg


def test_word_aliases_reach_the_flag_handlers(tmp_path, capsys, monkeypatch):
    """`sincrogit status` / `sincrogit log` are sugar for --status/--log."""
    import logging

    import sincrogit.__main__ as m
    import sincrogit.views as views
    monkeypatch.setattr(views, "ping_existing_instance", lambda: False)
    # main() wires the real file logger before dispatching; leaving that
    # handler on the 'sincrogit' logger poisons every later caplog test (and
    # writes to a tmp file that pytest deletes).
    monkeypatch.setattr(m, "setup_logging",
                        lambda *a, **k: logging.getLogger("sincrogit-cli-test"))
    cfg = tmp_path / "config.yaml"
    cfg.write_text("repos: []\nlog:\n  file: %s\n"
                   % str(tmp_path / "s.log").replace("\\", "/"),
                   encoding="utf-8")
    assert main(["status", "-c", str(cfg)]) == 0
    assert "daemon:" in capsys.readouterr().out
    assert main(["log", "-c", str(cfg), "--tail", "5"]) == 0
    assert "has the daemon run?" in capsys.readouterr().out


# ------------------------------------------------------------------- --version

def test_version_needs_no_config(capsys, tmp_path, monkeypatch):
    """`--version` answers "what did I just copy onto this machine?", so it must
    work on a bare exe with no config anywhere near it — it returns before any
    config resolution."""
    monkeypatch.chdir(tmp_path)          # nothing to find here
    monkeypatch.delenv("APPDATA", raising=False)
    assert main(["--version"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("sincrogit ")
    assert __version__ in out


def test_version_wins_over_other_actions(capsys):
    """Informational like --help: combined with an action it prints and exits,
    instead of being rejected by the conflict check."""
    assert main(["--version", "--doctor"]) == 0
    assert "sincrogit " in capsys.readouterr().out


def test_version_report_fingerprints_the_exe_only_when_frozen(monkeypatch):
    """The POINT of the fingerprint: `__version__` alone can't tell two builds
    apart, so a packaged exe reports its own mtime + SHA-256. From source there
    is no artifact, and it must say so rather than hash python.exe."""
    from sincrogit import runtime

    src = version_report()
    assert "running from source" in src and "sha256" not in src

    fake = "C:/somewhere/SincroGit.exe"
    monkeypatch.setattr(runtime.sys, "frozen", True, raising=False)
    monkeypatch.setattr(runtime.sys, "executable", fake, raising=False)
    monkeypatch.setattr(runtime, "_sha256", lambda p: "a" * 64)
    monkeypatch.setattr(runtime.os.path, "getmtime", lambda p: 1_700_000_000.0)
    frozen = runtime.version_report()
    assert "packaged exe" in frozen
    assert "sha256: " + "a" * 16 in frozen     # a PREFIX, not the whole digest
    assert "running from source" not in frozen


def test_sha256_streams_and_survives_a_missing_file(tmp_path):
    import hashlib

    from sincrogit import runtime

    p = tmp_path / "blob.bin"
    data = b"x" * (3 * (1 << 20) + 7)          # > one 1 MiB read chunk
    p.write_bytes(data)
    assert runtime._sha256(str(p)) == hashlib.sha256(data).hexdigest()
    assert runtime._sha256(str(tmp_path / "nope.bin")) is None


def test_version_label_is_cheap_and_says_where_it_came_from(monkeypatch):
    """The tray builds its identity line from this, so it must not hash the exe
    (~50 MB of SHA-256 on the GUI thread) — only a stat for the build time."""
    from sincrogit import runtime

    hashed = []
    monkeypatch.setattr(runtime, "_sha256", lambda p: hashed.append(p) or "x" * 64)

    label, tip = runtime.version_label()
    assert label == f"SincroGit {__version__} (source)"
    assert "running from source" in tip

    monkeypatch.setattr(runtime.sys, "frozen", True, raising=False)
    monkeypatch.setattr(runtime.sys, "executable", "C:/x/SincroGit.exe", raising=False)
    monkeypatch.setattr(runtime.os.path, "getmtime", lambda p: 1_700_000_000.0)
    label, tip = runtime.version_label()
    assert label == f"SincroGit {__version__}"       # no "(source)" when packaged
    assert "built " in tip and "--version" in tip     # points at where the digest is
    assert hashed == []                                # never hashed anything
