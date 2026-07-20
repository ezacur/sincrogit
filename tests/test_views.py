"""`sincrogit status` / `sincrogit log`: the read-only CLI views."""

import os

import pytest

from sincrogit.events import EventLog
from sincrogit.views import run_log, run_status

from conftest import git, write


@pytest.fixture(autouse=True)
def _no_live_daemon(monkeypatch):
    # The dev machine's real daemon must not leak into the "daemon:" line.
    import sincrogit.views as views
    monkeypatch.setattr(views, "ping_existing_instance", lambda: False)


def test_status_one_healthy_repo(make_repo, make_engine, capsys):
    repo = make_repo({"a.txt": "v1\n"})
    eng, _ = make_engine(repo)
    write(repo, "a.txt", "v2\n")
    eng.flush_now(wait=True)              # one snapshot -> shadow exists
    rc = run_status(eng.config)
    out = capsys.readouterr().out
    assert rc == 0
    assert "daemon: NOT running" in out
    line = next(ln for ln in out.splitlines() if ln.startswith("t "))
    assert " main " in line and " ok " in line
    assert "never" not in line            # the snapshot has an age
    assert " 1 " in line                  # one unsealed snapshot
    write(repo, "a.txt", "v3 — newer than the snapshot\n")
    run_status(eng.config)
    line = next(ln for ln in capsys.readouterr().out.splitlines()
                if ln.startswith("t "))
    assert "yes (pre-snapshot)" in line   # edits waiting for the next capture


def test_status_flags_problems(make_repo, make_engine, capsys):
    repo = make_repo()
    eng, _ = make_engine(repo)
    git(repo, "checkout", "-q", "-b", "feature")
    assert run_status(eng.config) == 0    # off-branch is a state, not a failure
    assert "OFF-BRANCH (on 'feature')" in capsys.readouterr().out
    eng.config.repos[0].path = repo + "-gone"
    assert run_status(eng.config) == 1
    assert "MISSING PATH" in capsys.readouterr().out


def test_status_repo_filter(make_repo, make_engine, capsys):
    repo = make_repo()
    eng, _ = make_engine(repo)
    assert run_status(eng.config, repo_name="nope") == 1
    assert "not found" in capsys.readouterr().out
    assert run_status(eng.config, repo_name="t") == 0


def _config_with_events(tmp_path, events):
    """A minimal Config whose events.jsonl (next to the log file) holds `events`."""
    from sincrogit.config import AiConfig, Config, LogConfig
    cfg = Config(repos=[], log=LogConfig(file=str(tmp_path / "sincrogit.log")),
                 ai=AiConfig(mode="none"))
    log = EventLog(os.path.join(str(tmp_path), "events.jsonl"))
    for repo, action, msg, level in events:
        log.add(repo, action, msg, level)
    return cfg


def test_log_filters_compose(tmp_path, capsys):
    cfg = _config_with_events(tmp_path, [
        ("", "startup", "engine up", "INFO"),
        ("alpha", "seal", "sealed A", "INFO"),
        ("beta", "seal", "sealed B", "INFO"),
        ("alpha", "push", "pushed A", "INFO"),
        ("alpha", "error", "boom", "ERROR"),
    ])
    run_log(cfg, repo="alpha")
    out = capsys.readouterr().out
    # Repo filter keeps GLOBAL events (same rule as the panel's Log tab).
    assert "engine up" in out and "sealed A" in out and "boom" in out
    assert "sealed B" not in out
    run_log(cfg, repo="alpha", actions="seal")
    out = capsys.readouterr().out
    assert "sealed A" in out and "pushed A" not in out and "engine up" not in out
    run_log(cfg, level="WARNING")
    out = capsys.readouterr().out
    assert "boom" in out and "sealed A" not in out


def test_log_tail_and_unknown_action_note(tmp_path, capsys):
    cfg = _config_with_events(
        tmp_path, [("r", "snapshot", f"m{i}", "INFO") for i in range(10)])
    run_log(cfg, tail=3)
    lines = [ln for ln in capsys.readouterr().out.splitlines() if "  r " in ln]
    assert len(lines) == 3 and lines[-1].endswith("m9")   # oldest->newest
    run_log(cfg, actions="sael")                          # typo
    out = capsys.readouterr().out
    assert "unknown action(s) sael" in out and "No events match." in out


def test_log_without_any_file(tmp_path, capsys):
    cfg = _config_with_events(tmp_path, [])
    os.remove(os.path.join(str(tmp_path), "events.jsonl")) \
        if os.path.exists(os.path.join(str(tmp_path), "events.jsonl")) else None
    assert run_log(cfg) == 0
    assert "has the daemon run?" in capsys.readouterr().out


def test_engine_status_unsealed_lifecycle(make_repo, make_engine):
    """The GUI's 'Unsealed' column feeds off engine.status(), which must stay
    git-free: the counter is seeded at setup and maintained incrementally."""
    repo = make_repo()
    eng, _ = make_engine(repo)
    assert eng.status()["repos"][0]["unsealed"] == 0
    write(repo, "a.txt", "v2\n")
    eng.flush_now(wait=True)
    assert eng.status()["repos"][0]["unsealed"] == 1
    ok, msg = eng.seal_repo_now("t")
    assert ok and msg == "sealed"
    assert eng.status()["repos"][0]["unsealed"] == 0
    # A fresh engine over the same repo re-derives the count from git.
    write(repo, "a.txt", "v3\n")
    eng.flush_now(wait=True)
    eng2, _ = make_engine(repo)
    assert eng2.status()["repos"][0]["unsealed"] == 1
