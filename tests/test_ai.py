"""ai.py: model-output parsing and the never-block-the-commit failure contract."""

import urllib.error

from sincrogit import ai
from sincrogit.ai import _split_message, generate_commit_message
from sincrogit.config import AiConfig


# ------------------------------------------------------------- _split_message
def test_split_title_only():
    """A single subject line becomes the title with an empty body."""
    title, body = _split_message("sincro: add the widget")
    assert title == "sincro: add the widget"
    assert body == ""


def test_split_title_and_body():
    """Subject + bullet lines split into (title, body)."""
    title, body = _split_message("feat: add widget\n- detail one\n- detail two")
    assert title == "feat: add widget"
    assert body == "- detail one\n- detail two"


def test_split_strips_code_fences():
    """``` fence marker lines are dropped wherever they appear."""
    title, body = _split_message("```\nfix: repair thing\n- note\n```")
    assert title == "fix: repair thing"
    assert body == "- note"


def test_split_skips_chatty_preamble():
    """A conversational preamble is skipped in favor of the first subject-looking line."""
    text = "Here's the commit message you asked for:\nfeat: implement export\n- adds CSV"
    title, body = _split_message(text)
    assert title == "feat: implement export"
    assert body == "- adds CSV"


def test_split_falls_back_to_first_nonempty_line():
    """With no Conventional-Commits subject, the first non-empty line is the title."""
    title, body = _split_message("\n\njust a plain line\nsecond line")
    assert title == "just a plain line"
    assert body == "second line"


def test_split_empty_returns_none():
    """Empty / whitespace-only input yields (None, '')."""
    assert _split_message("") == (None, "")
    assert _split_message("   \n  ") == (None, "")


# ---------------------------------------------------- generate_commit_message
def test_mode_none_never_hits_the_network(monkeypatch):
    """mode='none' returns None without ever calling urlopen."""
    def _boom(*a, **k):
        raise AssertionError("network must not be touched in mode=none")
    monkeypatch.setattr(ai.urllib.request, "urlopen", _boom)
    cfg = AiConfig(mode="none")
    assert generate_commit_message(cfg, "stat", "diff") is None


def test_mode_local_urlerror_returns_none(monkeypatch):
    """A network failure in local mode returns None (fallback), never propagates."""
    def _fail(*a, **k):
        raise urllib.error.URLError("connection refused")
    monkeypatch.setattr(ai.urllib.request, "urlopen", _fail)
    cfg = AiConfig(mode="local")
    assert generate_commit_message(cfg, "stat", "diff") is None


def test_mode_hybrid_urlerror_returns_none(monkeypatch):
    """Hybrid tries every provider; when all fail it returns None without raising."""
    def _fail(*a, **k):
        raise urllib.error.URLError("no route to host")
    monkeypatch.setattr(ai.urllib.request, "urlopen", _fail)
    # A key is present so the cloud provider is actually attempted (and also fails).
    cfg = AiConfig(mode="hybrid", api_key_env="SINCROGIT_TEST_KEY")
    monkeypatch.setenv("SINCROGIT_TEST_KEY", "super-secret-value")
    assert generate_commit_message(cfg, "stat", "diff") is None


def test_api_key_never_logged(monkeypatch, caplog):
    """The API key is scrubbed from any warning logged when the cloud call fails."""
    secret = "TOP-SECRET-KEY-12345"
    monkeypatch.setenv("SINCROGIT_TEST_KEY", secret)

    # Fail with an error whose message embeds the secret, as a leaky provider might.
    def _fail(*a, **k):
        raise urllib.error.URLError("auth failed for key=" + secret)
    monkeypatch.setattr(ai.urllib.request, "urlopen", _fail)

    cfg = AiConfig(mode="cloud", api_key_env="SINCROGIT_TEST_KEY")
    with caplog.at_level("WARNING", logger="sincrogit.ai"):
        assert generate_commit_message(cfg, "stat", "diff") is None
    joined = "\n".join(r.getMessage() for r in caplog.records)
    assert secret not in joined
    assert "***" in joined  # the scrubbed placeholder made it through


def test_cloud_missing_key_returns_none(monkeypatch):
    """Cloud mode with no API key set returns None (the RuntimeError is swallowed)."""
    monkeypatch.delenv("SINCROGIT_TEST_KEY", raising=False)
    # urlopen must never be reached: the missing key raises before any request.
    def _boom(*a, **k):
        raise AssertionError("must not reach the network without a key")
    monkeypatch.setattr(ai.urllib.request, "urlopen", _boom)
    cfg = AiConfig(mode="cloud", api_key_env="SINCROGIT_TEST_KEY")
    assert generate_commit_message(cfg, "stat", "diff") is None
