"""The shadow model's core promises: `git log` stays clean, the user's index
and status are never touched, seals are single real commits, and legacy WIP
tips migrate correctly."""

from conftest import git, read, write


def test_git_log_stays_clean_and_status_truthful(make_repo, make_engine):
    repo = make_repo({"a.txt": "a1\n"})
    eng, _ = make_engine(repo)
    write(repo, "a.txt", "a2\n")
    eng.snapshot_all_now()
    # The branch's history has NO machine commits — the whole point.
    assert "sincro" not in git(repo, "log", "--format=%s")
    # The snapshot exists, on the side ref.
    tip = git(repo, "rev-parse", "refs/sincro/wip/main")
    assert git(repo, "show", f"{tip}:a.txt") == "a2"
    # And the user's status still tells the truth about their worktree.
    assert "a.txt" in git(repo, "status", "--porcelain")


def test_user_staging_is_never_touched(make_repo, make_engine):
    repo = make_repo({"a.txt": "a1\n", "b.txt": "b1\n"})
    eng, _ = make_engine(repo)
    write(repo, "a.txt", "a2\n")
    write(repo, "b.txt", "b2\n")
    git(repo, "add", "a.txt")  # the user hand-crafts a commit
    eng.snapshot_all_now()
    staged = git(repo, "diff", "--cached", "--name-only")
    assert staged == "a.txt"   # exactly what the user staged, nothing more


def test_snapshot_gating_no_new_commit_without_changes(make_repo, make_engine):
    repo = make_repo()
    eng, _ = make_engine(repo)
    write(repo, "a.txt", "a2\n")
    eng.snapshot_all_now()
    tip1 = git(repo, "rev-parse", "refs/sincro/wip/main")
    eng.snapshot_all_now()  # nothing changed since
    assert git(repo, "rev-parse", "refs/sincro/wip/main") == tip1


def test_seal_is_one_real_commit_and_reanchors(make_repo, make_engine):
    repo = make_repo({"a.txt": "a1\n"})
    eng, _ = make_engine(repo)
    write(repo, "a.txt", "a2\n")
    ok, msg = eng.seal_repo_now("t")
    assert ok and msg == "sealed"
    subjects = git(repo, "log", "--format=%s").splitlines()
    assert len(subjects) == 2                      # base + ONE seal
    assert not subjects[0].startswith("sincro: snapshot")
    assert git(repo, "status", "--porcelain") == ""  # worktree == HEAD now
    # The shadow chain re-anchored at the seal.
    assert (git(repo, "rev-parse", "refs/sincro/wip/main")
            == git(repo, "rev-parse", "HEAD"))


def test_auto_seal_postponed_while_user_has_staged(make_repo, make_engine):
    repo = make_repo({"a.txt": "a1\n"})
    eng, _ = make_engine(repo)
    write(repo, "a.txt", "a2\n")
    git(repo, "add", "a.txt")   # a manual commit in progress
    ok, msg = eng.seal_repo_now("t")          # auto message -> must yield
    assert ok and msg == "nothing to seal"
    assert git(repo, "diff", "--cached", "--name-only") == "a.txt"  # intact
    ok, msg = eng.seal_repo_now("t", message="feat: mine")  # explicit -> seals
    assert ok and msg == "sealed"
    assert git(repo, "log", "-1", "--format=%s") == "feat: mine"


def test_migration_moves_legacy_wip_off_the_tip(make_repo, make_engine):
    repo = make_repo({"a.txt": "a1\n"})
    base = git(repo, "rev-parse", "HEAD")
    write(repo, "a.txt", "a2\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-m", "sincro: WIP autosnapshot")  # the old model's tip
    wip = git(repo, "rev-parse", "HEAD")
    make_engine(repo)  # setup migrates
    assert git(repo, "rev-parse", "HEAD") == base            # branch given back
    assert git(repo, "rev-parse", "refs/sincro/wip/main") == wip  # WIP -> shadow
    assert read(repo, "a.txt") == "a2\n"                     # worktree untouched
    assert "a.txt" in git(repo, "status", "--porcelain")     # edits now visible


def test_seal_survives_a_failing_index_refresh(make_repo, make_engine, caplog,
                                               monkeypatch):
    """If the post-seal `git reset` fails (a transient lock), the seal itself
    must stand AND the failure must be said out loud: a silently stale index
    makes has_staged_changes() postpone every future auto-seal."""
    from sincrogit.gitrepo import GitRepo
    repo = make_repo({"a.txt": "a1\n"})
    eng, _ = make_engine(repo)
    write(repo, "a.txt", "a2\n")
    orig = GitRepo._run

    def failing_reset(self, args, **kw):
        if args and args[0] == "reset":
            class R:
                returncode, stdout, stderr = 1, "", "simulated: index.lock held"
            return R()
        return orig(self, args, **kw)

    monkeypatch.setattr(GitRepo, "_run", failing_reset)
    with caplog.at_level("WARNING", logger="sincrogit.git"):
        ok, msg = eng.seal_repo_now("t", message="feat: sealed anyway")
    assert ok and msg == "sealed"
    assert git(repo, "log", "-1", "--format=%s") == "feat: sealed anyway"
    assert any("could not refresh the index" in r.message
               for r in caplog.records)


def test_shadow_ref_survives_zeroing(make_repo, make_engine):
    """The power-cut self-healing covers the shadow ref too."""
    import os
    repo = make_repo()
    eng, _ = make_engine(repo)
    write(repo, "a.txt", "a2\n")
    eng.snapshot_all_now()
    tip = git(repo, "rev-parse", "refs/sincro/wip/main")
    ref_path = os.path.join(repo, ".git", "refs", "sincro", "wip", "main")
    with open(ref_path, "wb") as fh:
        fh.write(b"\x00" * 41)  # what NTFS leaves after a power cut
    repairs = eng.states[0].repo.repair_corrupt_refs("main")
    assert any("refs/sincro/wip/main" in r for r in repairs)
    assert git(repo, "rev-parse", "refs/sincro/wip/main") == tip
