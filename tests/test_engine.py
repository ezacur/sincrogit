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


def _force_nudge_gates(st):
    """Open the cheap non-git gates of the purist commit nudge (quiet, past the
    startup grace, throttle clear). Staleness is NOT faked here: the nudge
    re-reads the last permanent commit's real timestamp from git, so tests make
    that commit genuinely old (see _backdated_commit)."""
    # -inf, not 0.0: monotonic time starts at boot, so on a recently-booted
    # machine `now - 0.0` is SMALLER than these gates' windows and they'd stay
    # closed (the throttle one for a whole day of uptime).
    st._started_mono = float("-inf")               # long uptime
    st.last_event_mono = float("-inf")             # quiet (no recent edits)
    st._commit_nudge_mono = float("-inf")          # not throttled


def test_commit_nudge_fires_in_purist_mode(make_repo, make_engine):
    repo = make_repo({"a.txt": "a1\n"})
    write(repo, "a.txt", "a2\n")
    _backdated_commit(repo, "feat: old manual work", hours_ago=48)  # genuinely stale
    eng, events = make_engine(repo, seal_interval_min="inf")  # purist
    st = eng.states[0]
    write(repo, "a.txt", "a3\n")                    # un-sealed work on top
    eng.snapshot_all_now()                          # capture it into the WIP
    _force_nudge_gates(st)
    events.clear()
    eng._maybe_nudge_commit(st, time.monotonic(), time.time())
    assert any(a == "info" and "Smart Commit" in m for _r, a, _l, m in events)
    # Throttled: a second immediate call must NOT nudge again.
    events.clear()
    eng._maybe_nudge_commit(st, time.monotonic(), time.time())
    assert not any("Smart Commit" in m for _r, _a, _l, m in events)


def test_commit_nudge_silent_when_auto_seal_on(make_repo, make_engine):
    repo = make_repo({"a.txt": "a1\n"})
    write(repo, "a.txt", "a2\n")
    eng, events = make_engine(repo)                 # default: auto-seal on (pragmatic)
    eng.snapshot_all_now()
    st = eng.states[0]
    _force_nudge_gates(st)
    events.clear()
    eng._maybe_nudge_commit(st, time.monotonic(), time.time())
    assert not any("Smart Commit" in m for _r, _a, _l, m in events)


def test_commit_nudge_silent_when_nothing_unsealed(make_repo, make_engine):
    repo = make_repo({"a.txt": "a1\n"})
    write(repo, "a.txt", "a2\n")
    _backdated_commit(repo, "feat: old manual work", hours_ago=48)  # stale for real
    eng, events = make_engine(repo, seal_interval_min="inf")
    eng.snapshot_all_now()                          # no edits since: branch is current
    st = eng.states[0]
    _force_nudge_gates(st)
    events.clear()
    eng._maybe_nudge_commit(st, time.monotonic(), time.time())
    assert not any("Smart Commit" in m for _r, _a, _l, m in events)


def _backdated_commit(repo, message, hours_ago):
    """A commit whose COMMITTER date (what %ct / last_sealed_time reads) lies
    `hours_ago` in the past — for tests that need an old permanent commit."""
    import subprocess
    stamp = f"{int(time.time()) - hours_ago * 3600} +0000"
    subprocess.run(
        ["git", "-C", repo, "commit", "-am", message], check=True, capture_output=True,
        env={**os.environ, "GIT_COMMITTER_DATE": stamp, "GIT_AUTHOR_DATE": stamp},
    )


def test_external_commit_resets_seal_clock(make_repo, make_engine):
    """Ported from v0.1: a manual `git commit` made in a terminal restarts the
    auto-seal window, so no `sincro:` checkpoint lands right on its heels."""
    repo = make_repo({"a.txt": "a1\n"})
    eng, events = make_engine(repo)
    st = eng.states[0]
    write(repo, "a.txt", "a2\n")
    git(repo, "commit", "-am", "feat: my own manual commit")  # external, just now
    st.last_seal_epoch = time.time() - 7 * 3600               # clock says a seal is due
    events.clear()
    eng._maybe_seal(st, time.time())
    assert git(repo, "log", "-1", "--format=%s") == "feat: my own manual commit"
    assert any("restarts from it" in m for _r, _a, _l, m in events)
    assert time.time() - st.last_seal_epoch < 60  # clock now counts from the commit


def test_old_external_commit_does_not_block_a_due_seal(make_repo, make_engine):
    repo = make_repo({"a.txt": "a1\n"})
    write(repo, "a.txt", "a2\n")
    _backdated_commit(repo, "feat: old manual work", hours_ago=8)  # permanent, 8h old
    eng, _ = make_engine(repo)
    st = eng.states[0]
    write(repo, "a.txt", "a3\n")                              # fresh un-sealed work
    eng.snapshot_all_now()
    st.last_seal_epoch = time.time() - 7 * 3600               # due; external is OLDER
    eng._maybe_seal(st, time.time())
    assert git(repo, "log", "-1", "--format=%s").startswith("sincro:")  # sealed


def test_restore_repo_refuses_touched_uncapturable_edit(make_repo, make_engine):
    """The restore target has a DIFFERENT version of the excluded file, so the
    apply would touch (and destroy) the uncaptured edit -> refuse."""
    repo = make_repo({"a.txt": "v1\n", "secret.bin": "s1\n"})
    sha = git(repo, "rev-parse", "HEAD")
    write(repo, "secret.bin", "s2\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-m", "feat: secret v2")
    eng, _ = make_engine(repo, extra_excludes=["secret.bin"])
    write(repo, "secret.bin", "EDITED uncapturable\n")
    ok, msg = eng.restore_repo("t", sha)  # target holds s1 != snapshot's s2
    assert not ok and "can't capture" in msg and "secret.bin" in msg
    assert read(repo, "secret.bin") == "EDITED uncapturable\n"


def test_restore_repo_ignores_untouched_uncapturable_edit(guarded):
    """Finer than the old model: an uncaptured edit on a path the restore does
    NOT touch is not at risk — the restore proceeds and the edit survives."""
    repo, eng, _, sha = guarded
    write(repo, "a.txt", "v2\n")           # capturable change to roll back
    write(repo, "secret.bin", "EDITED\n")  # uncaptured, but target agrees on it
    ok, msg = eng.restore_repo("t", sha)
    assert ok, msg
    assert read(repo, "a.txt") == "v1\n"
    assert read(repo, "secret.bin") == "EDITED\n"  # survived, untouched


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


def test_long_busy_warning_points_at_a_stale_lock(guarded):
    """When the only 'busy' marker is an old index.lock, no merge is coming to
    free it: the warning must say 'leftover lock, delete it', not 'merge in
    progress' — the difference between self-service and a repo stuck forever."""
    repo, eng, events, _sha = guarded
    st = eng.states[0]
    lock = os.path.join(repo, ".git", "index.lock")
    open(lock, "w").close()
    old = time.time() - 2 * Engine.BUSY_WARN_SEC
    os.utime(lock, (old, old))
    now = time.monotonic()
    eng._track_busy(st, now)
    eng._track_busy(st, now + Engine.BUSY_WARN_SEC + 1)
    warnings = [e for e in events if e[1] == "busy" and e[2] == "WARNING"]
    assert len(warnings) == 1
    assert "index.lock" in warnings[0][3] and "crash" in warnings[0][3]


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


def test_going_off_branch_drops_a_pending_handoff(guarded):
    """A handoff offer recorded on the configured branch must not linger once
    the user checks out another branch (nothing can be applied there)."""
    repo, eng, _events, _sha = guarded
    st = eng.states[0]
    st.pending_handoff = {"sha": "x", "host": "laptop"}
    git(repo, "checkout", "-q", "-b", "feature")
    eng._ensure_on_branch(st, time.monotonic())
    assert st.off_branch and st.pending_handoff is None


def test_following_a_new_branch_drops_a_pending_handoff(make_repo, make_engine):
    """With track_current_branch, switching branches re-scopes sync — the old
    branch's handoff offer must be cleared (the next sync re-detects per branch)."""
    repo = make_repo({"a.txt": "v1\n"})
    eng, _ = make_engine(repo, track_current_branch=True)
    st = eng.states[0]
    eng._ensure_on_branch(st, time.monotonic())     # settle st.branch = 'main'
    st.pending_handoff = {"sha": "x", "host": "laptop"}
    git(repo, "checkout", "-q", "-b", "feature")
    eng._ensure_on_branch(st, time.monotonic())
    assert st.branch == "feature" and st.pending_handoff is None


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
    # An uncaptured edit is only at risk where the restore would TOUCH it —
    # here the target and the snapshots agree on secret.bin, so it's safe.
    write(repo, "secret.bin", "EDITED uncapturable\n")
    ok, payload = eng.restore_repo_preview("t", sha)
    assert ok and payload["risky"] == []
    assert read(repo, "secret.bin").startswith("EDITED")  # preview didn't touch it


def test_giterror_falls_back_to_stdout(guarded):
    _, eng, _, _ = guarded
    with pytest.raises(GitError, match="No stash entries found"):
        eng.states[0].repo._run(["stash", "drop"])  # errors on stdout, not stderr


# ------------------------------------------------------------- sync_soon
def test_sync_soon_makes_pull_due_even_on_young_monotonic_clock(make_repo, make_engine):
    """sync_soon used to set last_pull_mono = 0.0 as 'the distant past' — but the
    monotonic clock starts at boot, so within the first pull_interval of uptime
    an unlock/resume event silently did NOT make the sync due."""
    eng, _ = make_engine(make_repo(), pull=True, pull_interval_min=10)
    st = eng.states[0]
    st.last_pull_mono = time.monotonic()   # just synced
    eng.sync_soon()
    # Due NOW regardless of how small time.monotonic() is on this machine.
    assert time.monotonic() - st.last_pull_mono >= st.cfg.pull_interval_sec


def test_sync_soon_keeps_disabled_interval_disabled(make_repo, make_engine):
    """pull_interval_min: inf means 'never' — sync_soon must not turn it into
    NaN arithmetic (now - inf) inside the idle-wait computation."""
    eng, _ = make_engine(make_repo(), pull=True, pull_interval_min="inf")
    st = eng.states[0]
    before = st.last_pull_mono
    eng.sync_soon()
    assert st.last_pull_mono == before


def test_flush_now_emits_per_repo_snapshot_event(make_repo, make_engine):
    """The lock/suspend flush snapshots silently before this: the Log showed no
    'snapshot' line for the very event the OS hook exists to guarantee."""
    repo = make_repo()
    eng, events = make_engine(repo)
    write(repo, "a.txt", "edited before locking\n")
    eng.flush_now(wait=True)
    assert any(r == "t" and a == "snapshot" and "leaving machine" in m
               for r, a, _l, m in events)
