"""Cross-machine config inheritance: a repo publishes its per-repo options to a
single-writer side ref (refs/sincro/config/<user>), and the SAME user's other
machines inherit them when they add the repo. One-time at add — later changes
do NOT auto-propagate (the accepted, simpler semantics)."""

import os

import pytest
import yaml

from sincrogit.config import (inheritable_overrides, load_config,
                               overrides_to_yaml, parse_published_overrides)
from sincrogit.engine import Engine
from sincrogit.gitrepo import GitRepo

from conftest import git, write


# --------------------------------------------------------------- config helpers
def test_inheritable_overrides_excludes_identity():
    entry = {"path": "C:/x", "name": "x", "branch": "dev", "remote": "origin",
             "seal_interval_min": "inf", "snapshot_interval_sec": 120}
    assert inheritable_overrides(entry) == {
        "seal_interval_min": "inf", "snapshot_interval_sec": 120}


def test_overrides_yaml_roundtrip():
    ov = {"seal_interval_min": "inf", "push": False, "snapshot_interval_sec": 120}
    text = overrides_to_yaml(ov)
    assert parse_published_overrides(text) == ov


def test_parse_is_defensive():
    # Unknown keys (incl. identity keys) are dropped; junk yields {}.
    assert parse_published_overrides(
        "path: /etc/passwd\nname: evil\nseal_interval_min: 30\n") == {
        "seal_interval_min": 30}
    assert parse_published_overrides("- not a mapping\n") == {}
    assert parse_published_overrides("this: is: not: valid") == {}
    assert parse_published_overrides("") == {}


# --------------------------------------------------------------- ref transport
@pytest.fixture
def bare_and_clones(tmp_path):
    origin = str(tmp_path / "origin.git")
    os.makedirs(origin)
    git(origin, "init", "--bare", "-b", "main")

    def clone(name):
        path = str(tmp_path / name)
        git(str(tmp_path), "clone", "-q", origin, path)
        git(path, "config", "user.email", "t@example.com")  # SAME identity
        git(path, "config", "user.name", "T")
        return path

    a = clone("office")
    write(a, "a.txt", "hi\n")
    git(a, "add", "-A")
    git(a, "commit", "-m", "base")
    git(a, "push", "-q", "origin", "main")
    return origin, clone, a


def test_publish_then_fetch_roundtrip(bare_and_clones):
    origin, clone, a = bare_and_clones
    ra = GitRepo(a)
    user = ra.sincro_user()
    text = overrides_to_yaml({"seal_interval_min": "inf", "snapshot_interval_sec": 120})
    ok, _ = ra.publish_repo_config("origin", user, text)
    assert ok
    # A second publish of the SAME content is a no-op (no push attempted).
    ok, msg = ra.publish_repo_config("origin", user, text)
    assert ok and msg == "config unchanged"

    b = clone("laptop")
    rb = GitRepo(b)
    assert rb.sincro_user() == user
    fetched = rb.fetch_published_config("origin", user)
    assert parse_published_overrides(fetched) == {
        "seal_interval_min": "inf", "snapshot_interval_sec": 120}


def test_fetch_returns_none_when_nothing_published(bare_and_clones):
    origin, clone, _a = bare_and_clones
    b = clone("laptop")
    assert GitRepo(b).fetch_published_config("origin", "t_example.com") is None


# --------------------------------------------------- engine publish (autosnap)
def _config_file(tmp_path, repo_path, **overrides):
    """A real config.yaml with one repo carrying `overrides`, so the engine has
    a config_path to read explicit overrides from (as production does)."""
    entry = {"path": repo_path.replace("\\", "/"), "name": "t",
             "remote": "origin", "branch": "main", **overrides}
    data = {"defaults": {}, "ai": {"mode": "none"},
            "log": {"file": str(tmp_path / "s.log")}, "repos": [entry]}
    p = str(tmp_path / "config.yaml")
    with open(p, "w", encoding="utf-8") as fh:
        yaml.safe_dump(data, fh)
    return p


def test_engine_publishes_explicit_overrides(bare_and_clones, tmp_path):
    origin, clone, a = bare_and_clones
    cfg_path = _config_file(tmp_path, a, seal_interval_min="inf",
                            snapshot_interval_sec=120)
    config = load_config(cfg_path)
    eng = Engine(config, config_path=cfg_path)
    eng.setup(with_watcher=False)
    st = eng.states[0]
    eng._publish_repo_config(st)               # what _do_autosnap calls on success

    b = clone("laptop")
    fetched = GitRepo(b).fetch_published_config("origin", st.user)
    assert parse_published_overrides(fetched) == {
        "seal_interval_min": "inf", "snapshot_interval_sec": 120}


def test_engine_without_config_path_skips_publishing(make_repo, make_engine):
    """The in-memory Config path (tests, dry runs): no config_path -> no publish,
    no crash."""
    repo = make_repo()
    eng, _ = make_engine(repo)
    assert eng.config_path is None
    eng._publish_repo_config(eng.states[0])    # must be a silent no-op


def test_added_repo_inherits_published_overrides(bare_and_clones, tmp_path):
    """End to end at the config layer: laptop fetches the office machine's
    published options and writes them as the new repo's overrides."""
    origin, clone, a = bare_and_clones
    ra = GitRepo(a)
    user = ra.sincro_user()
    ra.publish_repo_config("origin", user, overrides_to_yaml(
        {"seal_interval_min": "inf", "autosnap": False}))

    b = clone("laptop")
    overrides = parse_published_overrides(
        GitRepo(b).fetch_published_config("origin", user))
    # Simulate the Add-repo write: identity + inherited overrides -> a config
    # that loads into a RepoConfig with those exact effective values.
    entry = {"path": b.replace("\\", "/"), "name": "t", "remote": "origin",
             "branch": "main", **overrides}
    data = {"defaults": {}, "ai": {"mode": "none"},
            "log": {"file": str(tmp_path / "l.log")}, "repos": [entry]}
    p = str(tmp_path / "laptop.yaml")
    with open(p, "w", encoding="utf-8") as fh:
        yaml.safe_dump(data, fh)
    rc = load_config(p).repos[0]
    import math
    assert math.isinf(rc.seal_interval_sec) and rc.autosnap is False
