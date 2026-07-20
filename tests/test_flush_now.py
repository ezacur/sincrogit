"""flush_now: the 'leaving this machine' sweep. One repo's held op_lock must
never stall it past the bounded acquire — the session-end handler has ~20 s
for ALL repos before Windows kills the process."""

import time

from sincrogit.engine import Engine

from conftest import write


def test_flush_skips_a_held_repo_and_finishes(make_repo, make_engine, monkeypatch):
    repo = make_repo()
    eng, events = make_engine(repo)
    st = eng.states[0]
    monkeypatch.setattr(Engine, "FLUSH_LOCK_TIMEOUT", 0.2)
    write(repo, "a.txt", "unsaved work\n")
    st.op_lock.acquire()          # a network worker is busy with this repo
    try:
        t0 = time.monotonic()
        eng.flush_now(wait=True, wait_timeout=10)
        elapsed = time.monotonic() - t0
    finally:
        st.op_lock.release()
    assert elapsed < 5            # bounded: it did NOT wait out the worker
    assert not any(a == "snapshot" for _r, a, _l, _m in events)
    # Once the lock frees up, the next flush captures the pending edit.
    eng.flush_now(wait=True, wait_timeout=10)
    assert any(a == "snapshot" for _r, a, _l, _m in events)
