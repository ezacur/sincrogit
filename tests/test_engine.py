"""Engine safety guards: restore refusals, long-busy warning, canonical repo
state, conflict explanations, and the GitError stdout fallback."""

import os
import time

import pytest

from sincrogit.engine import Engine
from sincrogit.gitrepo import GitError

from conftest import git, read, write


@pytest.fixture
def guarded(make_repo, make_engine):
    """A repo with one normal file and one EXCLUDED tracked file, plus its engine."""
    repo = make_repo({"a.txt": "v1\n", "secret.bin": "s1\n"})
    eng, events = make_engine(repo, extra_excludes=["secret.bin"])
    return repo, eng, events, git(repo, "rev-parse", "HEAD")


def test_restore_file_refuses_uncapturable_edit(guarded):
    repo, eng, _, sha = guarded
    write(repo, "secret.bin", "EDITED — capturable by NO snapshot\n")
    ok, msg = eng.restore_file("t", "secret.bin", sha)
    assert not ok and "can't capture" in msg
    assert read(repo, "secret.bin").startswith("EDITED")  # nothing was destroyed


def test_restore_repo_refuses_uncapturable_edit(guarded):
    repo, eng, _, sha = guarded
    write(repo, "secret.bin", "EDITED\n")
    ok, msg = eng.restore_repo("t", sha)
    assert not ok and "can't capture" in msg and "secret.bin" in msg
    assert read(repo, "secret.bin") == "EDITED\n"


def test_restore_file_still_works_for_capturable(guarded):
    repo, eng, _, sha = guarded
    write(repo, "a.txt", "v2\n")
    ok, msg = eng.restore_file("t", "a.txt", sha)
    assert ok, msg
    assert read(repo, "a.txt") == "v1\n"


def test_long_busy_warns_once_then_resumes(guarded):
    repo, eng, events, sha = guarded
    st = eng.states[0]
    gd = os.path.join(repo, ".git")
    open(os.path.join(gd, "MERGE_HEAD"), "w").write(sha)
    now = time.monotonic()
    eng._track_busy(st, now)
    assert st.busy_since_mono is not None and not st.busy_warned
    eng._track_busy(st, now + Engine.BUSY_WARN_SEC + 1)
    assert st.busy_warned
    warnings = [e for e in events if e[1] == "busy" and e[2] == "WARNING"]
    assert len(warnings) == 1
    eng._track_busy(st, now + Engine.BUSY_WARN_SEC + 100)  # still busy: no re-warn
    assert len([e for e in events if e[1] == "busy" and e[2] == "WARNING"]) == 1
    assert eng.status()["repos"][0]["state"] == "busy"
    os.remove(os.path.join(gd, "MERGE_HEAD"))
    eng._track_busy(st, now + Engine.BUSY_WARN_SEC + 200)
    assert st.busy_since_mono is None and not st.busy_warned
    assert any(e[1] == "info" and "snapshots resume" in e[3] for e in events)


def test_busy_warning_fires_mid_rebase(guarded, monkeypatch):
    """A manual rebase detaches HEAD: the branch guard yields, but the busy
    tracking must run BEFORE it so the warning still fires."""
    repo, eng, events, sha = guarded
    git(repo, "checkout", "--detach", "HEAD")
    os.makedirs(os.path.join(repo, ".git", "rebase-merge"))
    monkeypatch.setattr(Engine, "BUSY_WARN_SEC", 0)
    eng.tick()  # arms busy_since_mono (and trips the branch guard)
    eng.tick()  # past the (zeroed) threshold
    assert any(e[1] == "busy" and e[2] == "WARNING" for e in events)
    assert eng.status()["repos"][0]["state"] == "busy"  # outranks off-branch


def test_state_precedence(guarded):
    _, eng, _, _ = guarded
    st = eng.states[0]

    def state():
        return eng.status()["repos"][0]["state"]

    assert state() == "active"
    st.pending_handoff = {"sha": "x", "host": "other"}
    assert state() == "handoff"
    st.user_paused = True
    assert state() == "paused"
    st.off_branch = True
    assert state() == "off-branch"
    st.busy_since_mono = 1.0
    assert state() == "busy"
    st.paused = True
    assert state() == "conflict"


def test_status_fields_and_conflict_clear(guarded):
    _, eng, _, _ = guarded
    st = eng.states[0]
    r = eng.status()["repos"][0]
    assert r["net_busy"] is False and r["conflict_msg"] == ""
    assert "pending_handoff_epoch" in r
    st.paused, st.conflict_msg = True, "Your local changes overlap …"
    st.pending_handoff = {"sha": "x", "host": "laptop", "epoch": 1234.0}
    r = eng.status()["repos"][0]
    assert r["conflict_msg"].startswith("Your local")
    assert r["pending_handoff"] == "laptop" and r["pending_handoff_epoch"] == 1234.0
    assert eng.resume_repo("t")
    assert eng.states[0].conflict_msg == "" and not eng.states[0].paused


def test_restore_repo_preview(make_repo, make_engine):
    repo = make_repo({"changed.txt": "v1\n", "gone.txt": "bye\n", "secret.bin": "s1\n"})
    eng, _ = make_engine(repo, extra_excludes=["secret.bin"])
    sha = git(repo, "rev-parse", "HEAD")
    write(repo, "changed.txt", "v2\n")            # modified -> revert
    os.remove(os.path.join(repo, "gone.txt"))     # deleted  -> recreate
    write(repo, "created.txt", "new\n")           # new      -> delete
    git(repo, "add", "-A")
    git(repo, "commit", "-m", "sincro: WIP autosnapshot")
    ok, payload = eng.restore_repo_preview("t", sha)
    assert ok, payload
    verbs = {p: v for v, p in payload["changes"]}
    assert verbs["changed.txt"] == "revert"
    assert verbs["gone.txt"] == "recreate"
    assert verbs["created.txt"] == "delete"
    assert payload["risky"] == []
    write(repo, "secret.bin", "EDITED uncapturable\n")
    ok, payload = eng.restore_repo_preview("t", sha)
    assert ok and payload["risky"] == ["secret.bin"]
    assert read(repo, "secret.bin").startswith("EDITED")  # preview is read-only


def test_giterror_falls_back_to_stdout(guarded):
    _, eng, _, _ = guarded
    with pytest.raises(GitError, match="No stash entries found"):
        eng.states[0].repo._run(["stash", "drop"])  # errors on stdout, not stderr
