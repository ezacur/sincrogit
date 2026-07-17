"""snapshot_timeline: the Timeline tab's data source (files + line counts
per snapshot/seal, newest first)."""

from conftest import git, write


def test_timeline_lists_files_with_status_and_counts(make_repo, make_engine):
    repo = make_repo({"a.txt": "l1\nl2\n"})
    eng, _ = make_engine(repo)
    write(repo, "a.txt", "l1 EDIT\nl2\nl3\n")   # modified: +2 −1
    write(repo, "b.txt", "new\n")               # added: +1 −0
    eng.snapshot_all_now()

    tl = eng.snapshot_timeline("t")
    assert tl, "the snapshot must appear in the timeline"
    snap = tl[0]
    assert snap["kind"] == "snapshot"
    assert snap["parent"], "snapshot chains to a parent (the anchor)"
    files = {p: (s, a, d) for s, p, a, d in snap["files"]}
    assert files["a.txt"] == ("M", 2, 1)
    assert files["b.txt"] == ("A", 1, 0)


def test_timeline_labels_seals_and_deletions(make_repo, make_engine):
    repo = make_repo({"a.txt": "l1\n", "gone.txt": "bye\n"})
    eng, _ = make_engine(repo)
    write(repo, "a.txt", "l1\nl2\n")
    eng.snapshot_all_now()
    ok, msg = eng.seal_repo_now("t", message="feat: sealed window")
    assert ok, msg

    import os
    os.remove(repo + "/gone.txt")               # deletion in the NEXT snapshot
    eng.snapshot_all_now()

    tl = eng.snapshot_timeline("t")
    kinds = [(e["kind"], e["subject"]) for e in tl]
    assert ("seal", "feat: sealed window") in kinds
    newest = tl[0]
    assert newest["kind"] == "snapshot"
    files = {p: s for s, p, _a, _d in newest["files"]}
    assert files.get("gone.txt") == "D"
    # The seal's file list is the whole sealed window (diff vs previous tip).
    seal = next(e for e in tl if e["kind"] == "seal")
    assert any(p == "a.txt" for _s, p, _a, _d in seal["files"])


def test_timeline_binary_files_have_no_counts(make_repo, make_engine):
    repo = make_repo({"a.txt": "l1\n"})
    eng, _ = make_engine(repo, extra_includes=["**/*.bin"], max_file_bytes=10_000)
    write(repo, "blob.bin", b"\x00\x01\x02\x03")
    eng.snapshot_all_now()
    tl = eng.snapshot_timeline("t")
    files = {p: (s, a, d) for s, p, a, d in tl[0]["files"]}
    assert files["blob.bin"] == ("A", None, None)   # numstat says binary


def test_timeline_unknown_repo_is_empty(make_repo, make_engine):
    eng, _ = make_engine(make_repo())
    assert eng.snapshot_timeline("nope") == []
