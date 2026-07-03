"""Time Machine engine pieces: the repo-wide version timeline, the selective
multi-file restore, version export ("save a copy as") and the history search."""

import os

import pytest

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
    assert hist[0]["source"] == "snapshot"  # newest first: the WIP tip
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
    assert git(repo, "status", "--porcelain") == ""  # captured in the WIP


def test_restore_files_reversible_via_reflog(evolved):
    repo, eng, sha = evolved
    eng.restore_files("t", ["a.txt", "d.txt"], sha)
    ok, msg = eng.restore_files("t", ["a.txt", "d.txt"],
                                git(repo, "rev-parse", "HEAD@{1}"))
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
