"""Shared fixtures: throwaway git repos, engines wired to them, offscreen Qt.

The suite grew out of the manual smoke scripts that verified each feature as it
landed; the fixtures keep that style — real git repos in tmp_path, a real
Engine over them (watcher off, AI off, network off), and a session-wide
offscreen QApplication for the GUI tests.
"""

import os
import subprocess
import sys
import time

# Must be set BEFORE PyQt5 is imported anywhere (the GUI tests are headless).
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

# The package lives at the repo root, one level up from tests/.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from sincrogit.config import AiConfig, Config, LogConfig, RepoConfig
from sincrogit.engine import Engine


def git(repo: str, *args: str) -> str:
    """Run git in `repo`, return stripped stdout. Raises on failure."""
    return subprocess.run(["git", "-C", repo, *args], check=True,
                          capture_output=True, text=True).stdout.strip()


def write(repo: str, relpath: str, content) -> None:
    path = os.path.join(repo, relpath)
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    if isinstance(content, bytes):
        with open(path, "wb") as fh:
            fh.write(content)
    else:  # explicit utf-8: Windows' locale default would mangle non-ASCII
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(content)


def read(repo: str, relpath: str):
    """File content, or None if it doesn't exist (post-restore assertions)."""
    path = os.path.join(repo, relpath)
    if not os.path.exists(path):
        return None
    with open(path, encoding="utf-8") as fh:
        return fh.read()


@pytest.fixture
def make_repo(tmp_path):
    """Factory: an initialized git repo (branch main, identity set) with the
    given {relpath: content} committed as 'feat: base'."""
    def _make(files=None, name="repo"):
        repo = str(tmp_path / name)
        os.makedirs(repo)
        git(repo, "init", "-b", "main")
        git(repo, "config", "user.email", "t@example.com")
        git(repo, "config", "user.name", "T")
        for fn, content in (files or {"a.txt": "a1\n"}).items():
            write(repo, fn, content)
        git(repo, "add", "-A")
        git(repo, "commit", "-m", "feat: base")
        return repo
    return _make


@pytest.fixture
def make_engine(tmp_path):
    """Factory: (engine, events) over ONE repo — watcher off, AI off, push/pull/
    autosnap off. `events` collects the structured (repo, action, level, msg)."""
    def _make(repo, **overrides):
        events = []
        kw = dict(path=repo, name="t", push=False, pull=False, autosnap=False)
        kw.update(overrides)
        eng = Engine(
            Config(repos=[RepoConfig(**kw)],
                   log=LogConfig(file=str(tmp_path / "log.txt")),
                   ai=AiConfig(mode="none")),
            emit_event=lambda r, a, m, lvl: events.append((r, a, lvl, m)),
        )
        eng.setup(with_watcher=False)
        return eng, events
    return _make


@pytest.fixture(scope="session")
def qapp():
    from PyQt5.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


@pytest.fixture
def qspin(qapp):
    """Spin the Qt event loop until `pred()` holds (or 5 s pass); returns it.
    The dialogs deliver background-thread results via queued signals."""
    def _spin(pred, timeout=5.0):
        deadline = time.time() + timeout
        while not pred() and time.time() < deadline:
            qapp.processEvents()
            time.sleep(0.01)
        return pred()
    return _spin
