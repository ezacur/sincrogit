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


# ---------------------------------------------------- load-time validation
def _write(tmp_path, text, name="c.yaml"):
    p = tmp_path / name
    p.write_text(text, encoding="utf-8")
    return str(p)


def test_load_rejects_non_mapping_sections(tmp_path):
    """A section of the wrong TYPE must fail as ValueError (the error every
    caller shows as a clean config error), never as AttributeError/TypeError
    (PyInstaller's crash box in the windowed exe)."""
    import pytest
    for i, bad in enumerate((
        "- the top level\n- as a list\n",   # root not a mapping
        "log: [1]\nrepos: []\n",            # log: not a mapping
        'ai: "x"\nrepos: []\n',             # ai: not a mapping
        "defaults: [1]\nrepos: []\n",       # defaults: not a mapping
        "repos: {a: 1}\n",                  # repos: not a list
    )):
        with pytest.raises(ValueError):
            load_config(_write(tmp_path, bad, f"bad{i}.yaml"))


def test_load_rejects_mistyped_ai_mode(tmp_path):
    """'hibrid' used to silently register NO AI providers (and --doctor printed
    no AI line at all); now it fails at load like every other bad field."""
    import pytest
    with pytest.raises(ValueError, match="ai.mode"):
        load_config(_write(tmp_path, "ai:\n  mode: hibrid\nrepos: []\n"))


def test_ai_mode_is_case_normalized(tmp_path):
    cfg = load_config(_write(tmp_path, "ai:\n  mode: 'Local'\nrepos: []\n"))
    assert cfg.ai.mode == "local"


def test_load_rejects_string_pattern_list(tmp_path):
    """extra_excludes as a bare string used to pass load and then blow up inside
    pathspec at engine setup — skipping the whole repo with a cryptic error."""
    import pytest
    with pytest.raises(ValueError, match="extra_excludes"):
        load_config(_write(
            tmp_path,
            'defaults:\n  extra_excludes: "**/build/**"\n'
            "repos:\n  - path: C:/tmp/x\n"))


def test_reset_repo_overrides(tmp_path):
    """Dropping a repo's overrides returns it to pure inheritance: identity
    keys survive, inheritable keys go, the other repo is untouched, and the
    file still parses (with its comments)."""
    path = _cfg(tmp_path)
    ok, msg = cfgmod.reset_repo_overrides(path, "custom-beta")  # has push: false
    assert ok and "push" in msg
    loaded = load_config(path)
    beta = next(r for r in loaded.repos if r.name == "custom-beta")
    assert beta.push is True                     # back to the default
    assert beta.branch == "main"                 # identity kept... (no branch key: default)
    alpha = next(r for r in loaded.repos if r.name == "alpha")
    assert alpha.branch == "main"                # the OTHER repo untouched
    assert "# Top comment MUST survive edits." in open(path, encoding="utf-8").read()
    ok, msg = cfgmod.reset_repo_overrides(path, "custom-beta")
    assert ok and "no overrides" in msg          # idempotent
    ok, _ = cfgmod.reset_repo_overrides(path, "nope")
    assert not ok


def test_live_handoff_typo_is_an_error(tmp_path):
    """A typo like 'aks' used to fail OPEN to 'auto' — flipping the machine
    into applying remote trees on its own, the one mode the user did NOT pick."""
    import pytest

    from sincrogit.config import RepoConfig
    with pytest.raises(ValueError, match="live_handoff"):
        RepoConfig(path=str(tmp_path), name="t", live_handoff="aks")


def test_live_handoff_accepted_spellings(tmp_path):
    from sincrogit.config import RepoConfig
    for raw, want in [(True, "auto"), (False, "off"), (None, "off"),
                      ("on", "auto"), ("always", "auto"), ("prompt", "ask"),
                      ("confirm", "ask"), ("never", "off"), ("0", "off")]:
        rc = RepoConfig(path=str(tmp_path), name="t", live_handoff=raw)
        assert rc.live_handoff == want, (raw, rc.live_handoff)
