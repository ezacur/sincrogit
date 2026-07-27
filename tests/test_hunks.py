"""Hunk-level restore: the pure diff/reconstruct functions (hunks.py) and the
engine's restore_hunks — partial restore, refusals, and the TOCTOU guard."""

import os

from sincrogit import hunks

from conftest import git, read, write


def _lines(text):
    return text.splitlines(keepends=True)


def test_compute_hunks_one_block_per_change():
    old = _lines("a\nb\nc\nd\ne\n")
    new = _lines("a\nB\nc\nd\nE\n")   # two separate changes
    hs = hunks.compute_hunks(old, new)
    assert len(hs) == 2
    assert hs[0]["new"] == ["B\n"] and hs[0]["old"] == ["b\n"]
    assert hs[1]["new"] == ["E\n"] and hs[1]["old"] == ["e\n"]


def test_apply_selected_reverts_only_chosen_blocks():
    old = _lines("a\nb\nc\nd\ne\n")
    new = _lines("a\nB\nc\nd\nE\n")
    # Restore only the first block: b comes back, E stays.
    assert "".join(hunks.apply_selected(old, new, {0})) == "a\nb\nc\nd\nE\n"
    # None selected -> current unchanged; all selected -> exactly the target.
    assert "".join(hunks.apply_selected(old, new, set())) == "a\nB\nc\nd\nE\n"
    assert "".join(hunks.apply_selected(old, new, {0, 1})) == "a\nb\nc\nd\ne\n"


def test_hunks_handle_insertions_and_deletions():
    old = _lines("keep\ngone\n")
    new = _lines("keep\nadded\n")
    hs = hunks.compute_hunks(old, new)
    assert len(hs) == 1
    assert "".join(hunks.apply_selected(old, new, {0})) == "keep\ngone\n"


def test_apply_preserves_crlf_and_missing_final_newline():
    old = _lines("x\r\ny\r\nz")       # CRLF, no trailing newline
    new = _lines("x\r\nY\r\nz")
    assert "".join(hunks.apply_selected(old, new, {0})) == "x\r\ny\r\nz"


# --------------------------------------------------------------- engine level

def _evolved(make_repo, make_engine):
    """A repo whose file gained two independent edits since `sha`."""
    repo = make_repo({"code.py": "a\nb\nc\nd\ne\n"})
    sha = git(repo, "rev-parse", "HEAD")
    write(repo, "code.py", "a\nB\nc\nd\nE\n")   # user's current edits
    eng, _ = make_engine(repo)
    return repo, eng, sha


def test_restore_hunks_partial(make_repo, make_engine):
    repo, eng, sha = _evolved(make_repo, make_engine)
    ok, payload = eng.file_hunks("t", "code.py", sha)
    assert ok and len(payload["hunks"]) == 2
    # Roll back only the first block (b) and keep the second edit (E).
    ok, msg = eng.restore_hunks("t", "code.py", sha, [0], payload["base"])
    assert ok, msg
    assert read(repo, "code.py") == "a\nb\nc\nd\nE\n"
    # It's versioned like any restore (nothing left uncaptured).
    assert eng.states[0].repo.shadow_changed_paths() == []


def test_restore_hunks_refuses_stale_base(make_repo, make_engine):
    """If the file changed since file_hunks read it, the picked indices no
    longer line up — refuse rather than apply to the wrong lines."""
    repo, eng, sha = _evolved(make_repo, make_engine)
    ok, payload = eng.file_hunks("t", "code.py", sha)
    assert ok
    write(repo, "code.py", "a\nB\nc\nd\nE\nf\n")   # user types more meanwhile
    ok, msg = eng.restore_hunks("t", "code.py", sha, [0], payload["base"])
    assert not ok and "changed since" in msg
    assert read(repo, "code.py") == "a\nB\nc\nd\nE\nf\n"   # untouched


def test_restore_hunks_refuses_binary(make_repo, make_engine):
    repo = make_repo({"blob.dat": "text-for-now\n"})
    sha = git(repo, "rev-parse", "HEAD")
    write(repo, "blob.dat", "now\x00binary\n")
    eng, _ = make_engine(repo)
    ok, msg = eng.file_hunks("t", "blob.dat", sha)
    assert not ok and "binary" in msg


def test_restore_hunks_refuses_non_utf8(make_repo, make_engine):
    """The filter counts every high byte as text (filefilter._TEXT_BYTES), so a
    cp1252/Latin-1 file IS snapshotted and DOES reach the hunk path. A hunk
    restore rebuilds the WHOLE file, so a lenient decode would replace every
    accented byte with U+FFFD — including in the hunks the user left alone.
    Refuse instead, and leave the bytes exactly as they were."""
    latin1 = "café — piñón\nsegunda línea\n".encode("cp1252")
    repo = make_repo({"legacy.txt": latin1})
    sha = git(repo, "rev-parse", "HEAD")
    mutated = "café — piñón\nsegunda línea EDITADA\n".encode("cp1252")
    write(repo, "legacy.txt", mutated)
    eng, _ = make_engine(repo)
    # It is genuinely accepted as text (otherwise the scenario is moot).
    assert eng.states[0].file_filter.reason_to_skip(
        os.path.join(repo, "legacy.txt"), "legacy.txt") is None
    ok, msg = eng.file_hunks("t", "legacy.txt", sha)
    assert not ok and "UTF-8" in msg
    ok, msg = eng.restore_hunks("t", "legacy.txt", sha, [0], "whatever")
    assert not ok and "UTF-8" in msg
    with open(os.path.join(repo, "legacy.txt"), "rb") as fh:
        assert fh.read() == mutated          # not one byte rewritten
    # And a UTF-8 file with the same accents still works normally.
    write(repo, "modern.txt", "café\nlínea\n")
    git(repo, "add", "modern.txt")
    git(repo, "commit", "-m", "feat: utf-8 file")
    sha2 = git(repo, "rev-parse", "HEAD")
    write(repo, "modern.txt", "café\nlínea EDITADA\n")
    ok, payload = eng.file_hunks("t", "modern.txt", sha2)
    assert ok and payload["hunks"]


def test_restore_hunks_refuses_uncapturable(make_repo, make_engine):
    """An excluded file's current content lives nowhere in git; a hunk restore
    would still overwrite it, so it's refused like whole-file restore."""
    repo = make_repo({"secret.env": "k=1\n"})
    sha = git(repo, "rev-parse", "HEAD")
    write(repo, "secret.env", "k=2\n")
    eng, _ = make_engine(repo, extra_excludes=["secret.env"])
    ok, payload = eng.file_hunks("t", "secret.env", sha)
    assert ok  # reading is fine
    ok, msg = eng.restore_hunks("t", "secret.env", sha, [0], payload["base"])
    assert not ok and "capture" in msg
    assert read(repo, "secret.env") == "k=2\n"   # nothing destroyed


def test_restore_hunks_no_selection(make_repo, make_engine):
    repo, eng, sha = _evolved(make_repo, make_engine)
    ok, msg = eng.restore_hunks("t", "code.py", sha, [], "whatever")
    assert not ok and "no hunks" in msg
