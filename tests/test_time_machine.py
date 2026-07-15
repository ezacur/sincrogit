"""Time Machine engine pieces: the repo-wide version timeline, the selective
multi-file restore, version export ("save a copy as") and the history search."""

import os
import sys

import pytest

from sincrogit.gitrepo import GitRepo

from conftest import git, read, write


@pytest.fixture
def evolved(make_repo, make_engine):
    """Base commit (4 files) + one WIP snapshot that modified a, deleted b and
    created d. Returns (repo, engine, base_sha)."""
    repo = make_repo({"a.txt": "a1\n", "b.txt": "b1\n", "c.txt": "c1\n",
                      "secret.bin": "s1\n"})
    sha = git(repo, "rev-parse", "HEAD")
    write(repo, "a.txt", "a2\n")
    os.remove(os.path.join(repo, "b.txt"))
    write(repo, "d.txt", "d1\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-m", "sincro: WIP autosnapshot")
    eng, _ = make_engine(repo, extra_excludes=["secret.bin"])
    return repo, eng, sha


def test_repo_history_timeline(evolved):
    repo, eng, sha = evolved
    hist = eng.repo_history("t")
    assert len(hist) >= 2
    # The migrated WIP became a shadow snapshot; the base commit stays sealed.
    assert any(h["source"] == "snapshot" for h in hist)
    assert any(h["source"] == "sealed" and h["sha"] == sha for h in hist)
    shas = [h["sha"] for h in hist]
    assert len(shas) == len(set(shas))  # identical trees collapsed


def test_restore_files_selective_and_atomic(evolved):
    repo, eng, sha = evolved
    ok, msg = eng.restore_files("t", ["a.txt", "d.txt"], sha)
    assert ok, msg
    assert read(repo, "a.txt") == "a1\n"      # selected: reverted
    assert read(repo, "d.txt") is None        # selected: created-since -> removed
    assert read(repo, "b.txt") is None        # UNselected deletion: stays deleted
    assert read(repo, "c.txt") == "c1\n"      # untouched
    # Shadow model: the SNAPSHOT chain captured the restore (nothing uncaptured
    # left)…
    assert eng.states[0].repo.shadow_changed_paths() == []
    # …while the USER's git view stays theirs: HEAD didn't move, and status
    # truthfully shows the worktree's divergence from it (b.txt's deletion).
    assert "b.txt" in git(repo, "status", "--porcelain")


def test_restore_files_reversible_via_shadow_reflog(evolved):
    repo, eng, sha = evolved
    eng.restore_files("t", ["a.txt", "d.txt"], sha)
    # The undo point is the SHADOW ref's reflog (HEAD never moves on snapshots).
    prev = git(repo, "rev-parse", "refs/sincro/wip/main@{1}")
    ok, msg = eng.restore_files("t", ["a.txt", "d.txt"], prev)
    assert ok, msg
    assert read(repo, "a.txt") == "a2\n" and read(repo, "d.txt") == "d1\n"


def test_restore_files_noop_and_risky(evolved):
    repo, eng, sha = evolved
    ok, msg = eng.restore_files("t", ["c.txt"], sha)
    assert ok and "already match" in msg
    write(repo, "secret.bin", "EDITED uncapturable\n")
    ok, msg = eng.restore_files("t", ["secret.bin"], sha)
    assert not ok and "can't capture" in msg
    assert read(repo, "secret.bin").startswith("EDITED")
    ok, _ = eng.restore_files("t", ["a.txt"], sha)  # unselected risky doesn't block
    assert ok and read(repo, "a.txt") == "a1\n"


def test_restore_across_a_rename(make_repo, make_engine):
    """A file renamed since the target version must come BACK under its old name.

    Regression: git's default rename detection made diff_trees_name_status report
    a single 'R old new'; the restore then tried to bring back 'new' (absent in
    the target) — which git turns into a DELETE — and never recreated 'old',
    leaving NEITHER file. --no-renames splits it into 'D old' + 'A new', both
    handled correctly.
    """
    content = "".join(f"line {i}\n" for i in range(20))  # enough to be detected R100
    repo = make_repo({"old.txt": content, "keep.txt": "k\n"})
    sha = git(repo, "rev-parse", "HEAD")
    # Rename (identical content -> git sees it as a rename) and capture it as a
    # WIP snapshot the engine migrates onto the shadow chain.
    os.rename(os.path.join(repo, "old.txt"), os.path.join(repo, "new.txt"))
    git(repo, "add", "-A")
    git(repo, "commit", "-m", "sincro: WIP autosnapshot")
    eng, _ = make_engine(repo)

    repo_obj = eng.states[0].repo
    mine = repo_obj.shadow_tip("main")
    # The diff the restore drives must now be delete-old + add-new, not a rename.
    statuses = {p: s for s, p in repo_obj.diff_trees_name_status(sha, mine)}
    assert statuses.get("old.txt") == "D" and statuses.get("new.txt") == "A"

    ok, msg = eng.restore_repo("t", sha)
    assert ok, msg
    assert read(repo, "old.txt") == content   # came back under its old name
    assert read(repo, "new.txt") is None       # the rename target removed
    assert read(repo, "keep.txt") == "k\n"     # untouched

    # Selective restore across the rename works the same: the engine snapshots
    # the worktree rename first, then brings old.txt back and removes new.txt.
    os.rename(os.path.join(repo, "old.txt"), os.path.join(repo, "new.txt"))
    ok, msg = eng.restore_files("t", ["old.txt", "new.txt"], sha)
    assert ok, msg
    assert read(repo, "old.txt") == content and read(repo, "new.txt") is None


def test_restore_refuses_when_an_edit_lands_mid_flight(evolved, monkeypatch):
    """An edit saved AFTER the risky-content check but BEFORE the worktree
    writes exists nowhere in git — applying the stale plan would destroy it.
    (Edits landing EARLIER in the window already trip _risky_paths, which
    compares actual content.) The apply must capture it, refuse, and succeed
    cleanly on the retry."""
    from sincrogit.engine import Engine
    repo, eng, sha = evolved
    orig = Engine._risky_paths

    def sneaky(self, st, target_sha, touched):
        res = orig(self, st, target_sha, touched)
        # Simulates the user hitting Save in the window between the last
        # validation step and the apply.
        write(repo, "a.txt", "typed at the last second\n")
        return res

    monkeypatch.setattr(Engine, "_risky_paths", sneaky)
    ok, msg = eng.restore_repo("t", sha)
    assert not ok and "try again" in msg
    assert read(repo, "a.txt") == "typed at the last second\n"  # untouched
    # The last-second edit is already versioned (the guard snapshotted it)…
    assert eng.states[0].repo.shadow_changed_paths() == []
    # …and the retry works: the same write is a no-op now, the plan is fresh.
    ok, msg = eng.restore_repo("t", sha)
    assert ok, msg
    assert read(repo, "a.txt") == "a1\n"
    # The mid-flight edit stays one reflog step away (recoverable, not lost).
    assert (git(repo, "show", "refs/sincro/wip/main@{1}:a.txt")
            == "typed at the last second")


@pytest.mark.skipif(sys.platform != "win32",
                    reason="deleting an open file only fails on Windows")
def test_restore_reports_files_it_could_not_delete(make_repo, make_engine):
    """A file the restore should remove but can't (open in another program)
    must be REPORTED, not silently left behind in a half-applied worktree."""
    repo = make_repo({"a.txt": "a1\n"})
    sha = git(repo, "rev-parse", "HEAD")
    write(repo, "d.txt", "d1\n")  # created since `sha` -> the restore removes it
    git(repo, "add", "-A")
    git(repo, "commit", "-m", "sincro: WIP autosnapshot")
    eng, events = make_engine(repo)
    with open(os.path.join(repo, "d.txt")):  # held open: os.remove will fail
        ok, msg = eng.restore_repo("t", sha)
        assert ok, msg                          # the restore itself proceeds
        assert read(repo, "d.txt") == "d1\n"    # the locked file survived
        warnings = [m for _r, _a, lvl, m in events
                    if lvl == "WARNING" and "could not be deleted" in m]
        assert warnings and "d.txt" in warnings[0]
    os.remove(os.path.join(repo, "d.txt"))      # released: cleanup works again


def test_path_chunks_bound_count_and_bytes():
    """Chunks respect BOTH limits (Windows caps a command line at ~32k, so
    count alone is not enough with deep paths), lose nothing, keep order."""
    many = [f"src/module_{i:03}.py" for i in range(250)]
    chunks = list(GitRepo._path_chunks(many))
    assert [p for c in chunks for p in c] == many
    assert all(len(c) <= GitRepo._PATH_CHUNK for c in chunks)
    deep = ["deep/" * 400 + f"f{i}.txt" for i in range(50)]  # ~2 kB per path
    chunks = list(GitRepo._path_chunks(deep))
    assert [p for c in chunks for p in c] == deep
    assert all(sum(len(p) + 3 for p in c) <= GitRepo._CHUNK_BYTES
               for c in chunks)
    oversized = ["x" * (2 * GitRepo._CHUNK_BYTES)]
    assert list(GitRepo._path_chunks(oversized)) == [oversized]  # travels alone


def test_export_file_version(make_repo, make_engine, tmp_path):
    repo = make_repo({"code.py": "def foo():\n    return 1\n",
                      "logo.bin": b"\x00\x01BINARY\xff"})
    sha = git(repo, "rev-parse", "HEAD")
    write(repo, "code.py", "def foo():\n    return 2\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-m", "feat: v2")
    eng, _ = make_engine(repo)

    dest = os.path.join(repo, "code (v1).py")  # inside the repo, ANOTHER name
    ok, msg = eng.export_file_version("t", "code.py", sha, dest)
    assert ok, msg
    assert read(repo, "code (v1).py") == "def foo():\n    return 1\n"
    assert read(repo, "code.py") == "def foo():\n    return 2\n"  # untouched

    dest2 = str(tmp_path / "logo_old.bin")     # outside the repo, byte-exact
    ok, _ = eng.export_file_version("t", "logo.bin", sha, dest2)
    assert ok and open(dest2, "rb").read() == b"\x00\x01BINARY\xff"

    ok, msg = eng.export_file_version("t", "nope.txt", sha, str(tmp_path / "x"))
    assert not ok and "doesn't exist" in msg


def test_search_in_file_versions(make_repo, make_engine):
    repo = make_repo({"code.py": "def foo():\n    return 1\n"})
    write(repo, "code.py", "def foo():\n    return 1\n\ndef bar():\n    return 2\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-m", "feat: v2 adds bar")
    write(repo, "code.py", "def foo():\n    return 1\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-m", "fix: v3 drops bar")
    eng, _ = make_engine(repo)
    counts = [n for _sha, n in eng.search_in_file_versions("t", "code.py", "def bar")]
    assert counts == [0, 1, 0]  # newest first: vanished, present, absent
