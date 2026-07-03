"""config.py surgery: find/update/remove a repo entry without wrecking the file."""

import math

from sincrogit import config as cfgmod
from sincrogit.config import load_config

BASE = (
    "# Top comment MUST survive edits.\n"
    "defaults:\n"
    "  snapshot_interval_sec: 300   # inline comment survives too\n"
    "\n"
    "repos:\n"
    "  - path: C:/tmp/alpha\n"
    "    branch: main\n"
    "  - path: C:/tmp/beta\n"
    "    name: custom-beta\n"
    "    push: false\n"
)


def _cfg(tmp_path):
    p = tmp_path / "config.yaml"
    p.write_text(BASE, encoding="utf-8")
    return str(p)


def test_find_repo_entry(tmp_path):
    path = _cfg(tmp_path)
    assert cfgmod.find_repo_entry(path, "alpha")["path"] == "C:/tmp/alpha"  # by basename
    assert cfgmod.find_repo_entry(path, "custom-beta")["push"] is False     # explicit name
    assert cfgmod.find_repo_entry(path, "nope") is None


def test_update_repo_roundtrip(tmp_path):
    path = _cfg(tmp_path)
    ok, msg = cfgmod.update_repo(path, "alpha", {
        "branch": "develop", "seal_interval_min": math.inf, "push": False,
        "extra_excludes": ["**/logs/**"]})
    assert ok, msg
    text = open(path, encoding="utf-8").read()
    assert "# Top comment MUST survive edits." in text
    assert "# inline comment survives too" in text
    assert "seal_interval_min: inf" in text  # the documented token, not .inf
    loaded = load_config(path)
    alpha = next(r for r in loaded.repos if r.name == "alpha")
    beta = next(r for r in loaded.repos if r.name == "custom-beta")
    assert alpha.branch == "develop" and alpha.push is False
    assert math.isinf(alpha.seal_interval_min)
    assert alpha.extra_excludes == ["**/logs/**"]
    assert beta.push is False and beta.branch == "main"  # the OTHER repo untouched


def test_update_repo_validates_before_writing(tmp_path):
    path = _cfg(tmp_path)
    ok, msg = cfgmod.update_repo(path, "alpha", {"snapshot_interval_sec": "garbage"})
    assert not ok and "snapshot_interval_sec" in msg
    load_config(path)  # the file must still parse (nothing was written)


def test_update_repo_unknown_name(tmp_path):
    ok, _ = cfgmod.update_repo(_cfg(tmp_path), "nope", {"push": True})
    assert not ok


def test_remove_repo(tmp_path):
    path = _cfg(tmp_path)
    ok, _ = cfgmod.remove_repo(path, "custom-beta")
    assert ok
    loaded = load_config(path)
    assert [r.name for r in loaded.repos] == ["alpha"]
    assert "# Top comment MUST survive edits." in open(path, encoding="utf-8").read()
