"""The portable promise: a bare exe in a folder writes a default config THERE,
and that generated file must show EVERY option (Ernesto's "con todas las
opciones!"). These tests introspect the dataclasses, so any future option that
isn't added to the template fails the suite — the guarantee can't rot."""

from dataclasses import fields

import yaml

from sincrogit.config import AiConfig, LogConfig, _INHERITABLE, load_config
from sincrogit.runtime import DEFAULT_CONFIG_TEMPLATE, ensure_config


def test_template_lists_every_option():
    raw = yaml.safe_load(DEFAULT_CONFIG_TEMPLATE)
    assert {"defaults", "repos", "ai", "log", "theme", "pandoc_path"} <= set(raw)
    # Exact equality BOTH ways: a key missing from the template hides an option
    # from the user; a key missing from _INHERITABLE is a template typo.
    assert set(raw["defaults"]) == set(_INHERITABLE)
    assert set(raw["ai"]) == {f.name for f in fields(AiConfig)}
    assert set(raw["log"]) == {f.name for f in fields(LogConfig)}
    assert raw["repos"] == []


def test_template_values_are_the_dataclass_defaults(tmp_path):
    """The generated file must LOAD, and loading it must equal the built-in
    defaults — writing a template that silently diverges from the dataclasses
    would pin surprising values on every fresh install."""
    p = tmp_path / "sincrogit.config.yaml"
    p.write_text(DEFAULT_CONFIG_TEMPLATE, encoding="utf-8")
    cfg = load_config(str(p))
    ai_defaults = AiConfig()
    for f in fields(AiConfig):
        assert getattr(cfg.ai, f.name) == getattr(ai_defaults, f.name), f.name
    assert cfg.repos == []
    assert cfg.theme == "auto" and cfg.pandoc_path == "pandoc"


def test_first_run_creates_the_config_where_the_exe_lives(tmp_path, monkeypatch):
    """Portable behavior: no config anywhere -> one is generated next to the
    exe (cwd when running from source) with the full template."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("APPDATA", raising=False)
    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))
    path, created = ensure_config(None)
    assert created and path == str(tmp_path / "sincrogit.config.yaml")
    # Idempotent: the second resolution FINDS it instead of recreating.
    path2, created2 = ensure_config(None)
    assert path2 == path and not created2
    load_config(path)  # and it parses/validates
