"""The README's skeptic-section promises, enforced by CODE — not by prompts.

1. 'A manual `git add photo.jpg` and done': content the user committed by hand
   but the filter refuses (binary, oversize) lives in HEAD's tree and never in
   the shadow's — sealing the raw shadow tree used to record it as *deleted*
   (and the next pull removed it from every other machine). The seal now
   grafts those entries back (gitrepo.graft_uncaptured).
2. 'Every automatic seal carries the sincro: prefix': the AI prompt asks for
   it, but a prompt is not a guarantee — the engine enforces it.
"""

import os

import sincrogit.engine as engmod

from conftest import git, write

BINARY = b"\x00\x01\x02PNGish\x00binary\x00" * 4


def _manual_commit(repo, relpath, content, msg):
    write(repo, relpath, content)
    git(repo, "add", relpath)
    git(repo, "commit", "-m", msg)


def test_manual_binary_survives_the_seal(make_repo, make_engine):
    repo = make_repo({"a.txt": "v1\n"})
    eng, _ = make_engine(repo)
    _manual_commit(repo, "photo.bin", BINARY, "docs: add the photo")
    write(repo, "a.txt", "v2 — normal text work\n")
    ok, msg = eng.seal_repo_now("t")
    assert ok and msg == "sealed"
    assert git(repo, "log", "-1", "--format=%s").startswith("sincro:")
    # The seal's tree keeps the hand-committed binary, byte for byte.
    files = git(repo, "ls-tree", "-r", "--name-only", "HEAD").splitlines()
    assert "photo.bin" in files and "a.txt" in files
    blob = git(repo, "rev-parse", "HEAD:photo.bin")
    assert blob == git(repo, "rev-parse", "HEAD~1:photo.bin")


def test_manual_binary_alone_is_not_a_seal(make_repo, make_engine):
    """The inverse symptom: with no other edits, the trees used to differ BY
    the binary alone — triggering a pointless seal whose only content was
    deleting it."""
    repo = make_repo({"a.txt": "v1\n"})
    eng, _ = make_engine(repo)
    _manual_commit(repo, "photo.bin", BINARY, "docs: add the photo")
    n = git(repo, "rev-list", "--count", "HEAD")
    ok, msg = eng.seal_repo_now("t")
    assert ok and msg != "sealed"
    assert git(repo, "rev-list", "--count", "HEAD") == n
    assert git(repo, "log", "-1", "--format=%s") == "docs: add the photo"


def test_deleted_binary_stays_deleted(make_repo, make_engine):
    """The worktree check keeps deletions honest: a hand-committed binary the
    user then REMOVES from disk drops out of the seal like any deletion."""
    repo = make_repo({"a.txt": "v1\n"})
    eng, _ = make_engine(repo)
    _manual_commit(repo, "photo.bin", BINARY, "docs: add the photo")
    os.remove(os.path.join(repo, "photo.bin"))
    write(repo, "a.txt", "v2\n")
    ok, msg = eng.seal_repo_now("t")
    assert ok and msg == "sealed"
    files = git(repo, "ls-tree", "-r", "--name-only", "HEAD").splitlines()
    assert "photo.bin" not in files


def test_ai_title_without_the_prefix_gets_it(make_repo, make_engine, monkeypatch):
    """A model that disobeys its prompt must not break the machine-commit
    promise — the prefix is stamped in _compose_seal_message, exactly like
    the leave seal already does."""
    repo = make_repo()
    eng, _ = make_engine(repo)
    st = eng.states[0]
    monkeypatch.setattr(engmod, "generate_commit_message",
                        lambda cfg, stat, text: ("feat: did things", "a body"))
    title, body = eng._compose_seal_message(st, ([("M", "a.txt")], ("stat", "diff")))
    assert title == "sincro: feat: did things" and body == "a body"
    # And one that obeys is left alone (no double prefix).
    monkeypatch.setattr(engmod, "generate_commit_message",
                        lambda cfg, stat, text: ("sincro: tidy things", ""))
    title, _ = eng._compose_seal_message(st, ([("M", "a.txt")], ("stat", "diff")))
    assert title == "sincro: tidy things"
