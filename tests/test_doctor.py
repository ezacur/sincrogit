"""--doctor: right verdicts and exit codes on healthy and broken setups."""

import os
import time

import pytest

from sincrogit.config import AiConfig, Config, LogConfig, RepoConfig
from sincrogit.doctor import run_doctor


@pytest.fixture(autouse=True)
def _no_live_daemon(monkeypatch):
    """The daemon probe is a LIVE localhost ping: when a real SincroGit runs on
    the developer's machine, 'daemon — running' replaces the [WARN] these
    outputs assert on. Pin it to 'not running' so the suite is deterministic."""
    monkeypatch.setattr("sincrogit.doctor.ping_existing_instance", lambda: False)


def _config(tmp_path, repos):
    return Config(repos=repos, log=LogConfig(file=str(tmp_path / "log.txt")),
                  ai=AiConfig(mode="none"))


def test_doctor_healthy_repo_without_remote(make_repo, tmp_path, capsys):
    repo = make_repo()
    cfg = _config(tmp_path, [RepoConfig(path=repo, name="t", push=False,
                                        pull=False, autosnap=False)])
    rc = run_doctor(cfg)
    out = capsys.readouterr().out
    assert rc == 0                       # warnings are not failures
    assert "[ OK ] git" in out
    assert "repo 't'" in out
    assert "[ OK ] AI" in out and "daemon" in out
    assert "[WARN]" in out and "no remote" in out


def test_doctor_missing_repo_path_fails(tmp_path, capsys):
    cfg = _config(tmp_path, [RepoConfig(path=str(tmp_path / "gone"), name="gone")])
    rc = run_doctor(cfg)
    out = capsys.readouterr().out
    assert rc == 1
    assert "[FAIL] repo 'gone'" in out and "does not exist" in out


def test_doctor_non_git_folder_fails(tmp_path, capsys):
    plain = tmp_path / "plain"
    plain.mkdir()
    cfg = _config(tmp_path, [RepoConfig(path=str(plain), name="plain")])
    rc = run_doctor(cfg)
    assert rc == 1
    assert "not a git repository" in capsys.readouterr().out


def test_doctor_no_repos_is_a_warning(tmp_path, capsys):
    rc = run_doctor(_config(tmp_path, []))
    out = capsys.readouterr().out
    assert rc == 0
    assert "no repos configured" in out


def test_doctor_flags_a_stale_git_lock(make_repo, tmp_path, capsys):
    """An hour-old index.lock is a crash leftover, not a running git command:
    doctor must name the file and say deleting it unblocks syncing."""
    repo = make_repo()
    lock = os.path.join(repo, ".git", "index.lock")
    open(lock, "w").close()
    old = time.time() - 7200
    os.utime(lock, (old, old))
    cfg = _config(tmp_path, [RepoConfig(path=repo, name="t", push=False,
                                        pull=False, autosnap=False)])
    rc = run_doctor(cfg)
    out = capsys.readouterr().out
    assert rc == 0                       # a warning: doctor never deletes it
    assert "stale git lock" in out and "index.lock" in out


def test_doctor_fresh_lock_reads_as_a_manual_operation(make_repo, tmp_path,
                                                       capsys):
    repo = make_repo()
    open(os.path.join(repo, ".git", "index.lock"), "w").close()  # just created
    cfg = _config(tmp_path, [RepoConfig(path=repo, name="t", push=False,
                                        pull=False, autosnap=False)])
    rc = run_doctor(cfg)
    out = capsys.readouterr().out
    assert rc == 0
    assert "merge/rebase is in progress" in out
    assert "stale git lock" not in out


def test_doctor_cloud_mode_without_key_fails(make_repo, tmp_path, capsys,
                                             monkeypatch):
    monkeypatch.delenv("SINCROGIT_TEST_MISSING_KEY", raising=False)
    repo = make_repo()
    cfg = Config(
        repos=[RepoConfig(path=repo, name="t", push=False, pull=False,
                          autosnap=False)],
        log=LogConfig(file=str(tmp_path / "log.txt")),
        ai=AiConfig(mode="cloud", api_key_env="SINCROGIT_TEST_MISSING_KEY"),
    )
    rc = run_doctor(cfg)
    out = capsys.readouterr().out
    assert rc == 1
    assert "[FAIL] AI: cloud key" in out
