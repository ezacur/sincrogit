"""A push that keeps failing must become VISIBLE, not just a WARNING line.

The failure mode this guards against: an expired credential (or a deleted /
protected remote) makes every push fail. The local history stays perfectly safe,
so nothing pauses and the tray icon used to stay green — the off-machine copy
the user is counting on silently stops advancing, and they find out when the disk
dies. See Engine.PUSH_FAIL_ALERT / _do_push / _repo_state.
"""

from sincrogit.engine import Engine

AUTH_FAIL = "fatal: Authentication failed for 'https://example.invalid/r.git'"


def _fail_pushes(st, monkeypatch, ok=False, msg=AUTH_FAIL):
    monkeypatch.setattr(st.repo, "push_sealed",
                        lambda *a, **k: (ok, "pushed" if ok else msg))


def _levels(events, level):
    return [e for e in events if e[2] == level]


def test_a_blip_stays_quiet(make_repo, make_engine, monkeypatch):
    """One or two failures are normal weather (a remote that moved ahead is
    reconciled by the next sync): WARNING only, no state change, no balloon."""
    eng, events = make_engine(make_repo(), push=True)
    st = eng.states[0]
    _fail_pushes(st, monkeypatch)
    for _ in range(Engine.PUSH_FAIL_ALERT - 1):
        eng._do_push(st)
    assert st.push_fail_streak == Engine.PUSH_FAIL_ALERT - 1
    assert eng._repo_state(st) == "active"
    assert _levels(events, "ERROR") == []
    assert _levels(events, "WARNING")


def test_a_streak_becomes_a_state_and_an_error(make_repo, make_engine, monkeypatch):
    eng, events = make_engine(make_repo(), push=True)
    st = eng.states[0]
    _fail_pushes(st, monkeypatch)
    for _ in range(Engine.PUSH_FAIL_ALERT):
        eng._do_push(st)

    assert eng._repo_state(st) == "push-failing"
    row = eng.status()["repos"][0]
    assert row["state"] == "push-failing"
    assert row["push_fail_streak"] == Engine.PUSH_FAIL_ALERT
    assert row["push_fail_msg"] == AUTH_FAIL
    assert row["push_fail_since"] is not None

    # Exactly ONE ERROR (that's what raises the tray balloon), and it says both
    # halves of the truth: the work is safe, the remote is not getting it.
    errors = _levels(events, "ERROR")
    assert len(errors) == 1
    assert "safe locally" in errors[0][3] and "NOT reaching" in errors[0][3]
    assert AUTH_FAIL in errors[0][3]


def test_further_failures_do_not_flood_the_log(make_repo, make_engine, monkeypatch):
    """A dead remote is retried on every sync — the alert must fire once per
    streak, or the Log becomes unreadable exactly when it matters."""
    eng, events = make_engine(make_repo(), push=True)
    st = eng.states[0]
    _fail_pushes(st, monkeypatch)
    for _ in range(Engine.PUSH_FAIL_ALERT + 5):
        eng._do_push(st)
    assert len(_levels(events, "ERROR")) == 1
    assert eng._repo_state(st) == "push-failing"


def test_recovery_clears_the_state_and_says_so(make_repo, make_engine, monkeypatch):
    eng, events = make_engine(make_repo(), push=True)
    st = eng.states[0]
    _fail_pushes(st, monkeypatch)
    for _ in range(Engine.PUSH_FAIL_ALERT):
        eng._do_push(st)
    assert eng._repo_state(st) == "push-failing"

    _fail_pushes(st, monkeypatch, ok=True)
    eng._do_push(st)
    assert st.push_fail_streak == 0
    assert st.push_fail_msg == "" and st.push_fail_since is None
    assert eng._repo_state(st) == "active"
    assert any("recovered" in e[3] for e in events)


def test_recovery_is_silent_when_nothing_was_wrong(make_repo, make_engine, monkeypatch):
    """Below the threshold the user was never alarmed, so there is nothing to
    reassure them about — no 'recovered' line for an ordinary retry."""
    eng, events = make_engine(make_repo(), push=True)
    st = eng.states[0]
    _fail_pushes(st, monkeypatch)
    eng._do_push(st)
    _fail_pushes(st, monkeypatch, ok=True)
    eng._do_push(st)
    assert st.push_fail_streak == 0
    assert not any("recovered" in e[3] for e in events)


def test_conflict_still_outranks_a_failing_push(make_repo, make_engine, monkeypatch):
    """Precedence guard: the canonical state lives in one place, and a rebase
    conflict is the more urgent truth."""
    eng, _ = make_engine(make_repo(), push=True)
    st = eng.states[0]
    _fail_pushes(st, monkeypatch)
    for _ in range(Engine.PUSH_FAIL_ALERT):
        eng._do_push(st)
    st.paused = True
    assert eng._repo_state(st) == "conflict"
    st.paused = False
    st.pending_handoff = {"sha": "deadbeef", "host": "laptop"}
    # ...but a fault the user must repair outranks an offer they can accept later.
    assert eng._repo_state(st) == "push-failing"
