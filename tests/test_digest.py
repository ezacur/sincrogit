"""absence_digest: "what happened while you were away".

Two sources, each doing only what it is good at — the per-repo snapshot
timeline for the churn (files, ±lines, commits, marks) and the event log for
what never became a commit (handoffs, conflicts, pushes that failed). The
promise that matters most is the NEGATIVE one: an absence in which nothing
happened must produce nothing, because a balloon that fires on every unlock is
a balloon people turn off.
"""

import os
import subprocess
import time

from sincrogit.events import Event

from conftest import git, write


def _ev(action, repo="t", level="INFO", ts=None, message="x"):
    return Event(ts=ts if ts is not None else time.time(), repo=repo,
                 action=action, level=level, message=message)


def _age(repo, seconds=7200):
    """Back-date the fixture's base commit. A real repo's history predates the
    absence being summarized; the fixture's is seconds old, so without this
    every window would legitimately include the repo's own creation and no test
    could tell "nothing happened" from "the repo was born just now"."""
    when = f"@{int(time.time() - seconds)} +0000"
    subprocess.run(["git", "-C", repo, "commit", "--amend", "--no-edit",
                    "--date", when], check=True, capture_output=True,
                   env={**os.environ, "GIT_COMMITTER_DATE": when})
    return repo


def test_nothing_happened_is_trivial_and_says_so(make_repo, make_engine):
    repo = _age(make_repo({"a.txt": "v1\n"}))
    eng, _ = make_engine(repo)
    d = eng.absence_digest(time.time() - 3600, events=[])
    assert d["trivial"] is True
    assert d["summary"] == "nothing changed"
    assert d["repos"] == [] and d["milestones"] == []
    assert d["files"] == 0 and d["seals"] == 0


def test_work_in_the_window_is_counted_per_repo(make_repo, make_engine):
    repo = _age(make_repo({"a.txt": "one\n"}))
    eng, _ = make_engine(repo)
    since = time.time() - 60
    write(repo, "a.txt", "one\ntwo\nthree\n")
    write(repo, "b.txt", "new file\n")
    eng.snapshot_all_now()

    d = eng.absence_digest(since, events=[])
    assert d["trivial"] is False
    assert [r["name"] for r in d["repos"]] == ["t"]
    r = d["repos"][0]
    assert r["files"] == 2                 # a.txt and b.txt
    assert r["adds"] == 3 and r["dels"] == 0
    assert "1 repo" in d["summary"] and "2 files" in d["summary"]


def test_work_outside_the_window_is_not_counted(make_repo, make_engine):
    repo = _age(make_repo({"a.txt": "one\n"}), seconds=4 * 3600)
    eng, _ = make_engine(repo)
    write(repo, "a.txt", "two\n")
    eng.snapshot_all_now()
    # An absence that ENDED before this work started.
    d = eng.absence_digest(time.time() - 7200, until_epoch=time.time() - 3600,
                           events=[])
    assert d["trivial"] is True


def test_seals_and_marks_come_from_the_timeline(make_repo, make_engine):
    repo = _age(make_repo({"a.txt": "one\n"}))
    eng, _ = make_engine(repo)
    since = time.time() - 60
    write(repo, "a.txt", "two\n")
    eng.mark_now("t", "a milestone")
    write(repo, "a.txt", "three\n")
    assert eng.seal_repo_now("t")[0]

    d = eng.absence_digest(since, events=[])
    assert d["seals"] == 1 and d["marks"] == 1
    assert d["repos"][0]["seals"] == 1 and d["repos"][0]["marks"] == 1
    assert "1 commit" in d["summary"] and "1 mark" in d["summary"]
    # A mark's tree equals its parent's, so it must not inflate the churn.
    assert d["repos"][0]["files"] == 1


def test_events_supply_what_no_commit_can_show(make_repo, make_engine):
    """A handoff, a conflict and a failing push leave no commit behind — the
    event log is the only place they exist."""
    repo = _age(make_repo())
    eng, _ = make_engine(repo)
    now = time.time()
    # Explicit stamps: two events created in the same clock tick (Windows'
    # time.time() is ~16 ms coarse) would make the ordering assertion a coin toss.
    events = [
        _ev("handoff", ts=now - 300, message="applied laptop's work"),
        _ev("conflict", ts=now - 200, level="WARNING", message="rebase aborted"),
        _ev("push", ts=now - 100, level="ERROR",
            message="push has failed 3 times in a row"),
        _ev("push", ts=now - 90, message="push OK"),   # a good push isn't a milestone
        _ev("snapshot", ts=now - 80, message="noise"),  # never in the milestones
        _ev("handoff", ts=now - 99999, message="last week's, out of the window"),
    ]
    d = eng.absence_digest(now - 3600, events=events)
    assert (d["handoffs"], d["conflicts"], d["push_failed"]) == (1, 1, 1)
    assert [m["action"] for m in d["milestones"]] == ["push", "conflict", "handoff"]
    assert d["trivial"] is False              # nothing moved, but plenty happened
    assert "⚠ 1 conflict(s)" in d["summary"]
    assert "push failure" in d["summary"]


def test_milestones_are_newest_first_and_bounded(make_repo, make_engine):
    repo = _age(make_repo())
    eng, _ = make_engine(repo)
    now = time.time()
    events = [_ev("seal", ts=now - i, message=f"seal {i}") for i in range(120)]
    d = eng.absence_digest(now - 3600, events=events)
    assert len(d["milestones"]) == eng.DIGEST_MILESTONES_MAX
    assert d["milestones"][0]["message"] == "seal 0"      # newest first
    assert d["milestones"][0]["epoch"] >= d["milestones"][-1]["epoch"]


def test_the_injected_reader_is_used_when_no_events_are_passed(make_repo,
                                                               make_engine):
    """The tray injects EventLog.recent so the engine can answer this without
    owning the store; passing `events` explicitly is what the panel does for
    its longer windows."""
    repo = _age(make_repo())
    eng, _ = make_engine(repo)
    # Built up front, not inside the lambda: the window's upper bound is taken
    # when the digest STARTS, so an event stamped mid-computation is (rightly)
    # outside it.
    stored = [_ev("conflict", message="from the store", ts=time.time() - 30)]
    eng._read_events = lambda: stored
    d = eng.absence_digest(time.time() - 60)
    assert d["conflicts"] == 1
    # And with no reader at all it still reports the git side, quietly.
    eng._read_events = None
    assert eng.absence_digest(time.time() - 60)["conflicts"] == 0


def test_summary_reads_like_a_sentence(make_repo, make_engine):
    repo = _age(make_repo({"a.txt": "one\n"}))
    eng, _ = make_engine(repo)
    since = time.time() - 60
    write(repo, "a.txt", "one\ntwo\n")
    eng.snapshot_all_now()
    d = eng.absence_digest(since, events=[_ev("handoff")])
    assert d["summary"].startswith("1 repo · 1 file · +1 −0")
    assert d["summary"].endswith("1 handoff")


def test_a_repo_that_did_not_move_is_left_out(make_repo, make_engine, tmp_path):
    """A digest lists what MOVED — a per-repo line of zeroes is noise."""
    from sincrogit.config import AiConfig, Config, LogConfig, RepoConfig
    from sincrogit.engine import Engine

    busy = _age(make_repo({"a.txt": "one\n"}, name="busy"))
    idle = _age(make_repo({"a.txt": "one\n"}, name="idle"))
    eng = Engine(Config(
        repos=[RepoConfig(path=busy, name="busy", push=False, pull=False,
                          autosnap=False),
               RepoConfig(path=idle, name="idle", push=False, pull=False,
                          autosnap=False)],
        log=LogConfig(file=str(tmp_path / "log.txt")), ai=AiConfig(mode="none")))
    eng.setup(with_watcher=False)
    since = time.time() - 60
    write(busy, "a.txt", "two\n")
    eng.snapshot_all_now()
    d = eng.absence_digest(since, events=[])
    assert [r["name"] for r in d["repos"]] == ["busy"]
    assert git(idle, "for-each-ref", "--format=%(refname)", "refs/sincro/wip/")
