"""The docs-parity guard itself (tools/check_docs_parity.py): heading
structure, fact extraction from inline code spans, and the
config.example.yaml key check — plus the live repo docs passing all three."""

import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_ROOT, "tools"))

import check_docs_parity as cdp


def _write(tmp_path, name, text):
    (tmp_path / name).write_text(text, encoding="utf-8")


def test_repo_docs_pass_every_check():
    """The real docs: structure, in-span facts and config example in sync."""
    vocab = cdp._code_vocabulary(_ROOT)
    for en, es in cdp.PAIRS:
        assert cdp.check_pair(_ROOT, en, es, vocab) == [], (en, es)
    assert cdp.check_config_example(_ROOT) == []


def test_fact_drift_catches_a_diverged_default(tmp_path):
    vocab = {"seal_interval_min"}
    _write(tmp_path, "EN.md", "## A\nDefault is `seal_interval_min: 360`.\n")
    _write(tmp_path, "ES.md", "## A\nPor defecto `seal_interval_min: 999`.\n")
    problems = cdp.check_pair(str(tmp_path), "EN.md", "ES.md", vocab)
    assert any("360" in p for p in problems)
    assert any("999" in p for p in problems)


def test_translated_placeholders_and_examples_are_not_drift(tmp_path):
    """`<branch>` -> `<rama>`, FILE -> FICHERO, sample names: legitimate
    translation inside spans must not read as factual drift."""
    vocab = {"branch", "file", "name"}
    _write(tmp_path, "EN.md",
           "## A\nSee `refs/x/<branch>`, `--history FILE` and "
           "`name (stamp).ext`.\n")
    _write(tmp_path, "ES.md",
           "## A\nVer `refs/x/<rama>`, `--history FICHERO` y "
           "`nombre (fecha).ext`.\n")
    assert cdp.check_pair(str(tmp_path), "EN.md", "ES.md", vocab) == []


def test_missing_cli_flag_is_drift(tmp_path):
    _write(tmp_path, "EN.md", "## A\nRun `--doctor` first.\n")
    _write(tmp_path, "ES.md", "## A\nEjecuta el chequeo primero.\n")
    problems = cdp.check_pair(str(tmp_path), "EN.md", "ES.md", set())
    assert any("--doctor" in p for p in problems)


def test_heading_drift_still_detected(tmp_path):
    _write(tmp_path, "EN.md", "## A\n## B\n")
    _write(tmp_path, "ES.md", "## A\n### B\n")
    problems = cdp.check_pair(str(tmp_path), "EN.md", "ES.md", set())
    assert any("heading structure drift" in p for p in problems)


def test_unknown_config_key_is_flagged(tmp_path):
    _write(tmp_path, "config.example.yaml",
           "defaults:\n  no_such_key: 1\nai:\n  mode: none\n")
    problems = cdp.check_config_example(str(tmp_path))
    assert any("no_such_key" in p for p in problems)


def test_repo_docs_mention_only_live_ui():
    """Every UI label the real docs point at exists in sincrogit/gui/*.py."""
    assert cdp.check_ui_names(_ROOT) == []


def test_retired_ui_mention_is_flagged(tmp_path):
    """A doc pointing at a button that no longer exists in the GUI source must
    be flagged — bold labels aren't code-spans, so the fact check can't see
    them (exactly how 'File history…' survived a redesign in one language)."""
    gui = tmp_path / "sincrogit" / "gui"
    gui.mkdir(parents=True)
    (gui / "panel.py").write_text(
        'btn = QPushButton("Time machine…")\n', encoding="utf-8")
    for name in ("README.md", "DESIGN.md", "MANUAL.md", "GUIDE.md"):
        _write(tmp_path, name, "## A\nOpen **Time machine…** from Status.\n")
    for name in ("LEAME.md", "DISENO.md", "MANUAL_ES.md", "GUIA.md"):
        _write(tmp_path, name, "## A\nAbre **File history…** desde Estado.\n")
    problems = cdp.check_ui_names(str(tmp_path))
    assert problems and all("File history…" in p for p in problems)
    assert not any("Time machine…" in p for p in problems)  # live label passes
