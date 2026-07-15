"""Remote onboarding: GitRepo's set_remote/remote_url helpers and the
controller-level configure_remote that verifies reachability + push access."""

import os

import pytest

from sincrogit.gitrepo import GitError, GitRepo

from conftest import git


@pytest.fixture
def repo_and_bare(tmp_path):
    """A working repo with one commit and a separate bare 'remote' to point at.
    No remote configured yet — that's what onboarding does."""
    bare = str(tmp_path / "remote.git")
    git(str(tmp_path), "init", "--bare", "-b", "main", bare)
    repo = str(tmp_path / "work")
    os.makedirs(repo)
    git(repo, "init", "-b", "main")
    git(repo, "config", "user.email", "t@example.com")
    git(repo, "config", "user.name", "T")
    with open(os.path.join(repo, "a.txt"), "w") as fh:
        fh.write("hi\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-m", "base")
    return repo, bare


def test_set_remote_is_idempotent(repo_and_bare):
    repo, bare = repo_and_bare
    r = GitRepo(repo)
    assert not r.has_remote("origin")
    r.set_remote("origin", bare)
    assert r.remote_url("origin") == bare
    r.set_remote("origin", bare)          # again -> update, not a second remote
    assert r.has_remote("origin") and r.remote_url("origin") == bare


def test_set_remote_rejects_a_bad_name(repo_and_bare):
    """Git validates the remote NAME (not the URL) at add time."""
    repo, bare = repo_and_bare
    with pytest.raises(GitError):
        GitRepo(repo).set_remote("bad name", bare)


def _controller(repo):
    """A minimal stand-in exposing just configure_remote's dependency (the
    engine's TrayApp method is thin, so exercise its logic directly here)."""
    from sincrogit.gui.app import TrayApp
    app = TrayApp.__new__(TrayApp)  # no Qt: we only call configure_remote
    return app


def test_configure_remote_verifies_reachable_and_push(repo_and_bare):
    repo, bare = repo_and_bare
    ok, msg = _controller(repo).configure_remote(repo, bare, branch="main")
    assert ok, msg
    assert GitRepo(repo).remote_url("origin") == bare


def test_configure_remote_reports_unreachable(repo_and_bare, tmp_path):
    repo, _bare = repo_and_bare
    missing = str(tmp_path / "nope.git")   # nothing there
    ok, msg = _controller(repo).configure_remote(repo, missing, branch="main")
    assert not ok and "reach" in msg.lower()


def test_configure_remote_needs_a_url(repo_and_bare):
    repo, _bare = repo_and_bare
    ok, msg = _controller(repo).configure_remote(repo, "  ", branch="main")
    assert not ok and "URL" in msg
