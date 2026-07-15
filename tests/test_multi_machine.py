"""Multi-machine paths against a throwaway BARE remote: seal/push/pull round
trips, autosnap mirrors, cross-machine handoff, and both conflict shapes.
These were the suite's known blind spot — the code the tool's data-loss
promises lean on hardest."""

import os

import pytest

from sincrogit.config import AiConfig, Config, LogConfig, RepoConfig
from sincrogit.engine import Engine

from conftest import git, read, write


@pytest.fixture
def pair(tmp_path):
    """A bare origin + two clones ('machineA'/'machineB') with engines wired
    for push/pull/autosnap over the file transport. Same user.email on both:
    that's the identity handoff matches machines by."""
    origin = str(tmp_path / "origin.git")
    os.makedirs(origin)
    git(origin, "init", "--bare", "-b", "main")

    def make_clone(name):
        path = str(tmp_path / name)
        git(str(tmp_path), "clone", "-q", origin, path)
        git(path, "config", "user.email", "t@example.com")
        git(path, "config", "user.name", "T")
        return path

    a = make_clone("machineA")
    write(a, "f.txt", "line1\nline2\n")
    git(a, "add", "-A")
    git(a, "commit", "-m", "feat: base")
    git(a, "push", "-q", "origin", "main")
    b = make_clone("machineB")

    def make_engine_on(path, host, **kw):
        rc = dict(path=path, name="t", remote="origin", branch="main",
                  push=True, pull=True, autosnap=True)
        rc.update(kw)  # per-test overrides (live_handoff="ask", extra_excludes, ...)
        eng = Engine(Config(
            repos=[RepoConfig(**rc)],
            log=LogConfig(file=str(tmp_path / f"{host}.log")),
            ai=AiConfig(mode="none"),
        ), emit_event=lambda *args: None)
        eng.setup(with_watcher=False)
        eng._autosnap_host = host  # two "machines" on one box
        eng.states[0].user = eng.states[0].repo.sincro_user()
        return eng

    return make_clone, make_engine_on, a, b, make_engine_on(a, "machineA"), \
        make_engine_on(b, "machineB")


def test_seal_push_pull_roundtrip(pair):
    _mk, _me, a, b, eng_a, eng_b = pair
    write(a, "f.txt", "line1-A\nline2\n")
    ok, msg = eng_a.seal_repo_now("t")
    assert ok and msg == "sealed"          # seals AND pushes (push=True)
    ok, msg = eng_b.pull_repo_now("t")
    assert ok, msg
    assert read(b, "f.txt") == "line1-A\nline2\n"
    assert git(a, "rev-parse", "HEAD") == git(b, "rev-parse", "HEAD")


def _mirror_now(eng):
    st = eng.states[0]
    with st.op_lock:
        eng._do_snapshot(st)
        eng._do_autosnap(st)
    return st


def test_autosnap_mirror_recoverable_from_the_other_machine(pair):
    _mk, _me, a, b, eng_a, eng_b = pair
    write(a, "f.txt", "unsealed work\n")
    _mirror_now(eng_a)                      # A mirrors its live snapshot
    states = eng_b.fetch_autosnaps("t")     # B fetches every machine's mirrors
    hosts = {s["host"] for s in states}
    assert "machineA" in hosts
    sha = next(s["sha"] for s in states if s["host"] == "machineA")
    # B can read A's unsealed content — the disk-failure recovery path.
    assert eng_b.file_text_at("t", "f.txt", sha) == "unsealed work\n"


def test_handoff_applies_peer_work_without_touching_head(pair):
    _mk, _me, a, b, eng_a, eng_b = pair
    write(a, "f.txt", "line1-continued\nline2\n")
    _mirror_now(eng_a)
    st_b = eng_b.states[0]
    head_before = git(b, "rev-parse", "HEAD")
    with st_b.op_lock:
        eng_b._maybe_handoff(st_b)          # live_handoff defaults to 'auto'
    assert read(b, "f.txt") == "line1-continued\nline2\n"   # content adopted
    assert git(b, "rev-parse", "HEAD") == head_before        # HEAD untouched
    assert "sincro" not in git(b, "log", "--format=%s")      # log still clean


def test_handoff_divergence_refused_both_sides_intact(pair):
    _mk, _me, a, b, eng_a, eng_b = pair
    write(a, "f.txt", "line1-A\nline2\n")
    _mirror_now(eng_a)
    write(b, "g.txt", "B's own work\n")     # B did its OWN thing meanwhile
    events = []
    eng_b._emit_event = lambda r, act, m, lvl: events.append((act, lvl, m))
    st_b = eng_b.states[0]
    with st_b.op_lock:
        eng_b._maybe_handoff(st_b)
    assert read(b, "f.txt") == "line1\nline2\n"   # nothing was applied
    assert read(b, "g.txt") == "B's own work\n"
    assert any(act == "handoff" and "DIVERGED" in m for act, _l, m in events)
    assert st_b.pending_handoff is None


def test_pull_rebase_conflict_pauses_and_explains(pair):
    _mk, _me, a, b, eng_a, eng_b = pair
    write(a, "f.txt", "line1-A\nline2\n")
    assert eng_a.seal_repo_now("t")[0]                  # A wins the push race
    write(b, "f.txt", "line1-B\nline2\n")
    ok, msg = eng_b.seal_repo_now("t", message="feat: B's take")  # push rejected
    assert ok
    ok, msg = eng_b.pull_repo_now("t")                  # rebase B's seal -> conflict
    st_b = eng_b.states[0]
    assert not ok and st_b.paused
    assert "overlap" in st_b.conflict_msg
    assert read(b, "f.txt") == "line1-B\nline2\n"       # aborted: tree intact


def test_pull_dirty_conflict_pauses_with_markers_and_recovery(pair):
    _mk, _me, a, b, eng_a, eng_b = pair
    write(a, "f.txt", "line1-A\nline2\n")
    assert eng_a.seal_repo_now("t")[0]
    write(b, "f.txt", "line1-B\nline2\n")               # UNCOMMITTED edit on B
    ok, msg = eng_b.pull_repo_now("t")
    st_b = eng_b.states[0]
    assert not ok and st_b.paused
    assert "conflict markers" in st_b.conflict_msg
    assert "<<<<<<<" in read(b, "f.txt")                # git's standard markers
    # The exact pre-pull content is one Time-Machine restore away (we snapshot
    # BEFORE pulling).
    tip = git(b, "rev-parse", "refs/sincro/wip/main")
    assert eng_b.file_text_at("t", "f.txt", tip) == "line1-B\nline2\n"


# The empty tree's well-known sha (SHA-1 repos) — used to build an orphan commit.
_EMPTY_TREE = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"


def test_work_relationship_classification(make_repo):
    """All four verdicts of the handoff's content-based comparison, explicitly —
    plus the 'unrelated histories -> never auto-apply' backstop."""
    r = make_repo({"f.txt": "base\n"})
    base = git(r, "rev-parse", "HEAD")

    def commit_on(branch, files):
        git(r, "checkout", "-q", "-b", branch, base)
        for name, content in files.items():
            write(r, name, content)
        git(r, "add", "-A")
        git(r, "commit", "-qm", branch)
        return git(r, "rev-parse", "HEAD")

    c1 = commit_on("t1", {"one.txt": "X\n"})
    c1_twin = commit_on("t2", {"one.txt": "X\n"})   # same change, sibling commit
    c12 = commit_on("t12", {"one.txt": "X\n", "two.txt": "Y\n"})
    c3 = commit_on("t3", {"three.txt": "Z\n"})
    orphan = git(r, "commit-tree", _EMPTY_TREE, "-m", "orphan")  # no shared history

    from sincrogit.gitrepo import GitRepo
    rel = GitRepo(r).work_relationship
    assert rel(c1, c1) == "equal"                    # literally the same state
    assert rel(c1, c1_twin) == "equal"               # same CONTENT, different commit
    assert rel(base, c1) == "theirs_contains"        # I'm simply behind
    assert rel(c12, c1) == "mine_contains"           # I'm ahead
    assert rel(c1, c3) == "diverged"                 # each has work the other lacks
    assert rel(c1, orphan) == "diverged"             # unrelated: never auto-apply


def test_handoff_ask_mode_records_then_applies(pair):
    _mk, make_engine_on, a, b, eng_a, _eng_b = pair
    eng_b = make_engine_on(b, "machineB", live_handoff="ask")
    write(a, "f.txt", "line1-continued\nline2\n")
    _mirror_now(eng_a)
    st_b = eng_b.states[0]
    with st_b.op_lock:
        eng_b._maybe_handoff(st_b)
    # Recorded, notified — but NOTHING touched until the user clicks Apply.
    assert st_b.pending_handoff and st_b.pending_handoff["host"] == "machineA"
    assert read(b, "f.txt") == "line1\nline2\n"
    ok, msg = eng_b.apply_handoff("t")               # the one-click action
    assert ok and "machineA" in msg
    assert read(b, "f.txt") == "line1-continued\nline2\n"
    assert st_b.pending_handoff is None


def test_handoff_ask_revalidates_when_no_longer_safe(pair):
    """The Apply click re-checks from scratch: work done on B after the
    notification turns the pending fast-forward into a divergence -> refused."""
    _mk, make_engine_on, a, b, eng_a, _eng_b = pair
    eng_b = make_engine_on(b, "machineB", live_handoff="ask")
    write(a, "f.txt", "line1-A\nline2\n")
    _mirror_now(eng_a)
    st_b = eng_b.states[0]
    with st_b.op_lock:
        eng_b._maybe_handoff(st_b)
    assert st_b.pending_handoff
    write(b, "f.txt", "line1-B\nline2\n")            # B works before clicking Apply
    ok, msg = eng_b.apply_handoff("t")
    assert not ok and "no longer" in msg
    assert read(b, "f.txt") == "line1-B\nline2\n"    # B's work intact


def test_handoff_refuses_to_clobber_uncaptured_content(pair):
    """The data-loss guard: the peer's tree tracks a file whose LOCAL content
    snapshots can't hold (excluded on this machine) -> the whole apply is
    refused and nothing — not even the safe files — is touched."""
    _mk, make_engine_on, a, b, eng_a, _eng_b = pair
    eng_b = make_engine_on(b, "machineB", extra_excludes=["secret.bin"])
    write(a, "secret.bin", "A's version\n")          # A versions it (no exclude there)
    write(a, "f.txt", "line1-A\nline2\n")
    _mirror_now(eng_a)
    write(b, "secret.bin", "B-local-secret\n")       # exists NOWHERE in B's git
    events = []
    eng_b._emit_event = lambda r, act, m, lvl: events.append((act, m))
    st_b = eng_b.states[0]
    with st_b.op_lock:
        eng_b._maybe_handoff(st_b)
    assert read(b, "secret.bin") == "B-local-secret\n"   # the uncapturable survives
    assert read(b, "f.txt") == "line1\nline2\n"          # and nothing else applied
    assert any(act == "handoff" and "NOT applied" in m for act, m in events)
    assert st_b.pending_handoff is None


def test_handoff_across_a_rename(pair):
    """Cross-machine regression for the --no-renames fix: A renames a file, B's
    handoff must end up with the NEW name only (rename detection used to make
    the apply delete both names)."""
    _mk, _me, a, b, eng_a, eng_b = pair
    os.rename(os.path.join(a, "f.txt"), os.path.join(a, "g.txt"))
    _mirror_now(eng_a)
    st_b = eng_b.states[0]
    with st_b.op_lock:
        eng_b._maybe_handoff(st_b)
    assert read(b, "g.txt") == "line1\nline2\n"      # the new name arrived
    assert read(b, "f.txt") is None                  # the old name is gone


def test_rejected_push_reconciles_on_next_sync(pair):
    """The retry loop the README promises: B's push is rejected (remote ahead),
    the next sync rebases B's seal onto the remote and the push then lands —
    both machines converge with no losses and no duplicates."""
    _mk, _me, a, b, eng_a, eng_b = pair
    write(a, "f.txt", "line1-A\nline2\n")
    assert eng_a.seal_repo_now("t")[0]               # A wins the race
    write(b, "g.txt", "B's own thing\n")             # DISJOINT file: clean rebase
    assert eng_b.seal_repo_now("t", message="feat: B's own thing")[0]
    # B's push was rejected (remote ahead): its seal is NOT on the remote yet.
    assert git(b, "ls-remote", "origin", "main").split()[0] != git(b, "rev-parse", "HEAD")
    eng_b.sync_all_now()                             # fetch -> rebase -> push OK
    assert git(b, "rev-list", "--count", "main") == "3"     # base + A + B
    remote_tip = git(b, "ls-remote", "origin", "main").split()[0]
    assert remote_tip == git(b, "rev-parse", "HEAD")        # B's seal landed
    assert eng_a.pull_repo_now("t")[0]
    assert read(a, "g.txt") == "B's own thing\n"            # A converged too


def test_sync_is_idempotent_when_up_to_date(pair):
    _mk, _me, a, b, eng_a, eng_b = pair
    write(a, "f.txt", "line1-A\nline2\n")
    assert eng_a.seal_repo_now("t")[0]
    tip = git(a, "ls-remote", "origin", "main").split()[0]
    count = git(a, "rev-list", "--count", "main")
    eng_a.sync_all_now()                             # nothing to do
    eng_a.sync_all_now()
    assert git(a, "ls-remote", "origin", "main").split()[0] == tip
    assert git(a, "rev-list", "--count", "main") == count
    ok, msg = eng_a.seal_repo_now("t")               # and a no-op seal stays a no-op
    assert ok and msg == "nothing to seal"


def test_prune_autosnap_refs_only_own_stale_branches(pair):
    """The only other destructive remote op: pruning deletes ONLY this host's
    refs for branches that no longer exist locally — never a peer machine's."""
    _mk, _me, a, b, eng_a, _eng_b = pair
    st = eng_a.states[0]
    sha = git(a, "rev-parse", "HEAD")
    user = st.user
    git(a, "push", "-q", "origin", f"{sha}:refs/autosnap/{user}/machineA/deadbranch")
    git(a, "push", "-q", "origin", f"{sha}:refs/autosnap/{user}/machineB/deadbranch")
    removed = st.repo.prune_autosnap_refs("origin", user, "machineA", min_age_sec=0)
    assert removed == ["deadbranch"]
    left = git(a, "ls-remote", "origin", "refs/autosnap/*")
    assert "machineB/deadbranch" in left             # the peer's state survives
    assert "machineA/deadbranch" not in left