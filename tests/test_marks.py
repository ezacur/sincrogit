"""Named marks: `sincrogit mark "<label>"` and its data model.

The promises under test are the ones a milestone has to keep to be worth
anything: it captures what is on disk AT THAT MOMENT (not the last automatic
snapshot), it SURVIVES every later seal and re-anchor, its readable name
survives the ref-name slug, and it shows up as a point on the time machine's
rail.
"""

from sincrogit.gitrepo import GitRepo

from conftest import git, write


# ------------------------------------------------------------------- the slug
def test_slug_folds_accents_and_punctuation():
    """Ref names stay ASCII on purpose (git stores them as raw bytes); the
    readable label lives in the mark commit's message instead."""
    assert GitRepo.mark_slug("Antes del refactor") == "antes-del-refactor"
    assert GitRepo.mark_slug("Añadí café ☕ (v2)!!") == "anadi-cafe-v2"
    assert GitRepo.mark_slug("  --- weird///name --- ") == "weird-name"


def test_slug_is_bounded_and_never_empty():
    long = GitRepo.mark_slug("x" * 200)
    assert len(long) == GitRepo.MARK_SLUG_MAX
    # A label that folds away entirely still needs a ref name.
    assert GitRepo.mark_slug("日本語") == "mark"
    assert GitRepo.mark_slug("") == "mark"
    assert GitRepo.mark_slug("...") == "mark"


# ------------------------------------------------------------- create / list
def test_mark_captures_the_state_at_that_moment(make_repo, make_engine):
    repo = make_repo({"a.txt": "v1\n"})
    eng, events = make_engine(repo)
    write(repo, "a.txt", "v2 — the state I want to name\n")

    ok, msg = eng.mark_now("t", "before the refactor")
    assert ok and "before the refactor" in msg
    marks = eng.list_marks("t")
    assert len(marks) == 1
    m = marks[0]
    # The mark's own tree holds the edit that was NOT yet snapshotted when the
    # user asked: mark_now snapshots first, always.
    assert git(repo, "show", f"{m['sha']}:a.txt") == "v2 — the state I want to name"
    assert m["label"] == "before the refactor"       # full label, not the slug
    assert m["ref"].startswith("refs/sincro/marks/")
    assert "before-the-refactor" in m["ref"]         # slug, for a readable ref
    assert m["files"] == 0                           # nothing differs from now
    assert ("t", "mark", "INFO", "marked this moment as 'before the refactor'") in events


def test_mark_survives_a_later_seal_and_reanchor(make_repo, make_engine):
    """The reason marks exist: a snapshot's reflog expires and every seal
    re-anchors the chain, so the ONE state you'll want in three months is the
    one the automatic machinery can't promise. A ref can."""
    repo = make_repo({"a.txt": "v1\n"})
    eng, _ = make_engine(repo)
    write(repo, "a.txt", "v2\n")
    eng.mark_now("t", "milestone")
    sha = eng.list_marks("t")[0]["sha"]

    write(repo, "a.txt", "v3\n")
    assert eng.seal_repo_now("t")[0]
    write(repo, "a.txt", "v4\n")
    eng.snapshot_all_now()

    m = eng.list_marks("t")[0]
    assert m["sha"] == sha                                   # same object
    assert git(repo, "show", f"{sha}:a.txt") == "v2"          # same content
    assert m["files"] == 1                                    # a.txt differs now
    # Reachable from a ref, so gc can never take it.
    assert git(repo, "rev-parse", "--verify", m["ref"]) == sha


def test_marks_are_ordered_newest_first_and_names_are_kept(make_repo, make_engine):
    repo = make_repo({"a.txt": "v1\n"})
    eng, _ = make_engine(repo)
    for i, label in enumerate(("first one", "SECOND — with dash", "tercera ✓")):
        write(repo, "a.txt", f"v{i}\n")
        assert eng.mark_now("t", label)[0]
    labels = [m["label"] for m in eng.list_marks("t")]
    assert labels == ["tercera ✓", "SECOND — with dash", "first one"]
    assert [m["epoch"] for m in eng.list_marks("t")] == sorted(
        (m["epoch"] for m in eng.list_marks("t")), reverse=True)


def test_two_marks_in_the_same_second_do_not_collide(make_repo, make_engine):
    """A scripted hook (an agent marking per step) is exactly the caller that
    can ask twice within one second with the same label."""
    repo = make_repo({"a.txt": "v1\n"})
    eng, _ = make_engine(repo)
    write(repo, "a.txt", "v2\n")
    assert eng.mark_now("t", "step")[0]
    write(repo, "a.txt", "v3\n")
    assert eng.mark_now("t", "step")[0]
    refs = {m["ref"] for m in eng.list_marks("t")}
    assert len(refs) == 2


def test_mark_needs_a_name_and_a_known_repo(make_repo, make_engine):
    repo = make_repo()
    eng, _ = make_engine(repo)
    assert eng.mark_now("t", "   ") == (False, "a mark needs a name")
    assert eng.mark_now("nope", "x") == (False, "repo not found")
    assert eng.list_marks("nope") == []


def test_long_labels_are_bounded(make_repo, make_engine):
    repo = make_repo()
    eng, _ = make_engine(repo)
    assert eng.mark_now("t", "L" * 500)[0]
    assert len(eng.list_marks("t")[0]["label"]) == eng.MARK_LABEL_MAX


def test_mark_all_now_covers_every_repo(make_repo, make_engine):
    repo = make_repo()
    eng, _ = make_engine(repo)
    results = eng.mark_all_now("everything at once")
    assert [(n, ok) for n, ok, _m in results] == [("t", True)]
    assert eng.list_marks("t")[0]["label"] == "everything at once"


def test_forget_mark_drops_the_name_only(make_repo, make_engine):
    repo = make_repo({"a.txt": "v1\n"})
    eng, _ = make_engine(repo)
    write(repo, "a.txt", "v2\n")
    eng.mark_now("t", "temporary")
    ref = eng.list_marks("t")[0]["ref"]
    assert eng.forget_mark("t", ref) == (True, "forgotten")
    assert eng.list_marks("t") == []
    # A ref outside the mark namespace is never deleted through this door.
    assert eng.forget_mark("t", "refs/heads/main")[0] is False
    assert git(repo, "rev-parse", "--verify", "refs/heads/main")


# ------------------------------------------------------- on the time machine
def test_marks_appear_on_the_timeline_as_their_own_point(make_repo, make_engine):
    """"Go to that moment" has to work forever, so the mark's OWN sha is on the
    rail — the snapshot it was taken from eventually leaves the reflog window."""
    repo = make_repo({"a.txt": "v1\n"})
    eng, _ = make_engine(repo)
    write(repo, "a.txt", "v2\n")
    eng.mark_now("t", "the moment")
    entries = eng.snapshot_timeline("t")
    marks = [e for e in entries if e["kind"] == "mark"]
    assert len(marks) == 1
    assert marks[0]["subject"] == "the moment"
    assert marks[0]["sha"] == eng.list_marks("t")[0]["sha"]
    assert marks[0]["files"] == ()   # a mark's tree equals its parent's


def test_restoring_to_a_mark_brings_that_state_back(make_repo, make_engine):
    repo = make_repo({"a.txt": "v1\n"})
    eng, _ = make_engine(repo)
    write(repo, "a.txt", "the good version\n")
    eng.mark_now("t", "known good")
    sha = eng.list_marks("t")[0]["sha"]

    write(repo, "a.txt", "broken\n")
    write(repo, "b.txt", "added later\n")
    ok, payload = eng.restore_repo_preview("t", sha)
    assert ok and sorted(payload["changes"]) == [("delete", "b.txt"),
                                                 ("revert", "a.txt")]
    assert eng.restore_repo("t", sha)[0]
    from conftest import read
    assert read(repo, "a.txt") == "the good version\n"
    assert read(repo, "b.txt") is None
