"""Leave seal: lock + stay away ≥ seal_on_leave_min -> seal with a
'sincro: [leave]' title and push. Armed by the lock, canceled by arrival,
at most once per absence, a silent no-op (no clock moved) when the 6 h seal
got there first, flat OFF in purist mode, and fired early with the
deterministic message when the machine is about to suspend."""

import math
import time

from sincrogit.config import RepoConfig

from conftest import git, write


def _arm_in_the_past(eng, secs_ago):
    eng.arm_leave_seal()
    eng._leave_epoch = time.time() - secs_ago  # the lock happened a while ago


def _spin_until(pred, timeout=10.0):
    deadline = time.time() + timeout
    while not pred() and time.time() < deadline:
        time.sleep(0.05)
    return pred()


def test_leave_seal_fires_after_the_absence(make_repo, make_engine):
    repo = make_repo({"a.txt": "v1\n"})
    eng, events = make_engine(repo)
    st = eng.states[0]
    write(repo, "a.txt", "v2 — work left pending when I locked\n")
    old_clock = st.last_seal_epoch
    _arm_in_the_past(eng, 21 * 60)
    eng.tick()  # dispatches the leave-seal to a worker
    assert _spin_until(lambda: git(repo, "log", "-1", "--format=%s")
                       .startswith("sincro: [leave] "))
    # The ref moves a beat before the worker emits: spin for the event too.
    assert _spin_until(lambda: any(a == "leave-seal" for _r, a, _l, _m in events))
    assert st.last_seal_epoch > old_clock       # a REAL seal resets the 6 h clock
    # Once per absence: further due ticks must not commit again.
    n = git(repo, "rev-list", "--count", "HEAD")
    eng.tick()
    time.sleep(0.4)
    assert git(repo, "rev-list", "--count", "HEAD") == n


def test_leave_seal_noop_when_nothing_pending(make_repo, make_engine):
    """The 6 h seal (or a manual commit) got there first: nothing to publish,
    and — per the design — NO clock is touched by the no-op."""
    repo = make_repo({"a.txt": "v1\n"})      # clean: worktree == HEAD
    eng, events = make_engine(repo)
    st = eng.states[0]
    clock = st.last_seal_epoch
    n = git(repo, "rev-list", "--count", "HEAD")
    _arm_in_the_past(eng, 21 * 60)
    eng.tick()
    assert _spin_until(lambda: "t" in eng._leave_sealed)
    assert git(repo, "rev-list", "--count", "HEAD") == n
    assert st.last_seal_epoch == clock
    assert not any(a == "leave-seal" for _r, a, _l, _m in events)


def test_arrival_cancels_the_countdown(make_repo, make_engine):
    repo = make_repo()
    eng, _ = make_engine(repo)
    write(repo, "a.txt", "edited before leaving\n")
    _arm_in_the_past(eng, 21 * 60)
    eng.disarm_leave_seal()                  # unlocked: I'm back
    eng.tick()
    time.sleep(0.4)
    assert git(repo, "log", "-1", "--format=%s") == "feat: base"


def test_purist_mode_never_leave_seals(make_repo, make_engine):
    """His branch, his commits: the leave seal is flat OFF under
    seal_interval_min: inf, even when seal_on_leave_min is set."""
    repo = make_repo()
    eng, _ = make_engine(repo, seal_interval_min="inf", seal_on_leave_min=20)
    write(repo, "a.txt", "purist work\n")
    _arm_in_the_past(eng, 21 * 60)
    eng.tick()
    time.sleep(0.4)
    assert git(repo, "log", "-1", "--format=%s") == "feat: base"


def test_disabled_by_sentinel(make_repo, make_engine):
    repo = make_repo()
    eng, _ = make_engine(repo, seal_on_leave_min="off")
    write(repo, "a.txt", "edited\n")
    _arm_in_the_past(eng, 21 * 60)
    eng.tick()
    time.sleep(0.4)
    assert git(repo, "log", "-1", "--format=%s") == "feat: base"


def test_suspend_fires_pending_leave_seal_early(make_repo, make_engine):
    """The machine is about to sleep with the countdown running: the timer
    can't tick while asleep, so it fires NOW — deterministic message, no AI."""
    repo = make_repo()
    eng, events = make_engine(repo)
    write(repo, "a.txt", "about to suspend\n")
    eng.arm_leave_seal()                     # locked seconds ago — NOT due yet
    eng.leave_seal_now_if_armed()            # bounded, joins the worker
    assert git(repo, "log", "-1", "--format=%s").startswith("sincro: [leave] ")
    assert any(a == "leave-seal" for _r, a, _l, _m in events)


def test_staged_changes_are_never_absorbed(make_repo, make_engine):
    """Same contract as the auto-seal: a hand-crafted commit in progress is the
    user's — even if they left it staged and went home."""
    repo = make_repo()
    eng, events = make_engine(repo)
    write(repo, "a.txt", "staged by hand\n")
    git(repo, "add", "a.txt")
    _arm_in_the_past(eng, 21 * 60)
    eng.tick()
    assert _spin_until(lambda: "t" in eng._leave_sealed)
    assert git(repo, "log", "-1", "--format=%s") == "feat: base"
    assert any(a == "leave-seal" and "staged" in m for _r, a, _l, m in events)


def test_config_sentinels_and_default(tmp_path):
    rc = RepoConfig(path=str(tmp_path), name="t")
    assert rc.seal_on_leave_min == 20
    off = RepoConfig(path=str(tmp_path), name="t", seal_on_leave_min="off")
    assert math.isinf(off.seal_on_leave_sec)
