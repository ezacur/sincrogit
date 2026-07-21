"""Power-cut self-healing for .git/config — the corruption class that hit all
five watched repos at once on 2026-07-21: a crash mid-boot left every config
the right SIZE but full of NUL bytes, git said "not a git repository", and the
engine silently skipped them all (an empty panel). The repair rebuilds the
file (core + origin recovered from FETCH_HEAD + branch tracking), keeps the
corrupt original as a .bak, and runs BEFORE the is_git_repo gate."""

import os

from sincrogit.gitrepo import GitRepo

from conftest import git


def _zero_config(repo):
    cfg = os.path.join(repo, ".git", "config")
    size = os.path.getsize(cfg)
    with open(cfg, "wb") as fh:
        fh.write(b"\0" * size)
    return cfg


def _fake_fetch_head(repo, url):
    with open(os.path.join(repo, ".git", "FETCH_HEAD"), "w",
              encoding="utf-8", newline="\n") as fh:
        fh.write(f"{'1' * 40}\t\tbranch 'main' of {url}\n")


def test_zeroed_config_is_rebuilt_with_the_remote(make_repo):
    repo = make_repo()
    git(repo, "remote", "add", "origin", "https://example.com/me/proj.git")
    _fake_fetch_head(repo, "https://example.com/me/proj.git")
    cfg = _zero_config(repo)
    r = GitRepo(repo)
    assert not r.is_git_repo()                    # what the crash looks like

    repairs = r.repair_zeroed_config("main")
    assert repairs and "rebuilt" in repairs[0]
    assert "https://example.com/me/proj.git" in repairs[0]
    assert r.is_git_repo()                        # the repo is BACK
    assert r.remote_url("origin") == "https://example.com/me/proj.git"
    assert git(repo, "config", "branch.main.remote") == "origin"
    assert git(repo, "log", "-1", "--format=%s") == "feat: base"
    with open(cfg + ".corrupt.bak", "rb") as fh:  # forensics kept
        assert fh.read().strip(b"\0") == b""


def test_partially_zeroed_config_also_repairs(make_repo):
    """The real incident's variant: the [core] block survived and the NUL run
    started mid-file (git still refuses: 'bad config line')."""
    repo = make_repo()
    _fake_fetch_head(repo, "git@github.com:me/proj.git")
    cfg = os.path.join(repo, ".git", "config")
    with open(cfg, "rb") as fh:
        raw = fh.read()
    with open(cfg, "wb") as fh:
        fh.write(raw[: len(raw) // 2] + b"\0" * 200)
    r = GitRepo(repo)
    assert r.repair_zeroed_config("main")
    assert r.is_git_repo()
    assert r.remote_url("origin") == "git@github.com:me/proj.git"


def test_healthy_config_is_never_touched(make_repo):
    repo = make_repo()
    git(repo, "remote", "add", "origin", "https://example.com/x.git")
    cfg = os.path.join(repo, ".git", "config")
    before = open(cfg, "rb").read()
    assert GitRepo(repo).repair_zeroed_config("main") == []
    assert open(cfg, "rb").read() == before
    assert not os.path.exists(cfg + ".corrupt.bak")


def test_no_fetch_head_rebuilds_without_remote_and_says_so(make_repo):
    repo = make_repo()
    _zero_config(repo)
    r = GitRepo(repo)
    repairs = r.repair_zeroed_config("main")
    assert repairs and "WITHOUT a remote" in repairs[0]
    assert r.is_git_repo() and not r.has_remote("origin")


def test_engine_setup_heals_and_still_adds_the_repo(make_repo, make_engine):
    """End to end: the zeroed repo would have been 'not a git repo (skipping)'
    — with the repair running BEFORE that gate, setup emits a repair event and
    the repo is watched again (the empty-panel symptom, fixed)."""
    repo = make_repo()
    _fake_fetch_head(repo, "https://example.com/me/proj.git")
    _zero_config(repo)
    eng, events = make_engine(repo)
    assert len(eng.states) == 1                   # added, not skipped
    assert any(a == "repair" and "config" in m for _r, a, _l, m in events)
    assert eng.states[0].repo.remote_url("origin") == "https://example.com/me/proj.git"


def test_reflog_setting_writes_config_only_once(make_repo):
    """Prevention: `git config` rewrites the whole file, so the steady state
    must not touch it — the write window in front of every boot is exactly
    what zeroed five configs at once."""
    repo = make_repo()
    r = GitRepo(repo)
    r._ensure_reflog_enabled()                    # first call: writes
    cfg = os.path.join(repo, ".git", "config")
    stamp = os.path.getmtime(cfg)
    content = open(cfg, "rb").read()
    for _ in range(3):
        r._ensure_reflog_enabled()                # already 'always': no write
        r.ensure_shadow("main")
    assert os.path.getmtime(cfg) == stamp
    assert open(cfg, "rb").read() == content


def test_zeroed_tracking_ref_is_removed_or_restored(make_repo, tmp_path):
    """The remote-tracking ref is the same tiny-file class. git itself cannot
    delete a ref it can't resolve, so the repair does — it's only a cache, and
    the next fetch rebuilds it."""
    bare = str(tmp_path / "origin.git")
    git(str(tmp_path), "init", "--bare", "-b", "main", bare)
    repo = make_repo()
    git(repo, "remote", "add", "origin", bare)
    git(repo, "push", "-q", "-u", "origin", "main")

    loose = os.path.join(repo, ".git", "refs", "remotes", "origin", "main")
    assert os.path.exists(loose)
    with open(loose, "wb") as fh:
        fh.write(b"\0" * 41)
    r = GitRepo(repo)
    repairs = r.repair_corrupt_refs("main")
    assert any("refs/remotes/origin/main" in m for m in repairs)
    # Either restored from its reflog or removed — in both cases the ref no
    # longer BLOCKS git, and a fetch brings it back in sync.
    git(repo, "fetch", "origin")
    assert git(repo, "rev-parse", "refs/remotes/origin/main") == \
        git(repo, "rev-parse", "main")
