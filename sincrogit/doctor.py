"""Health check (`--doctor`): verify the whole chain SincroGit depends on.

One command that answers "why isn't it syncing?" without reading logs: git on
PATH, config valid, watchdog importable, each repo's path/branch/remote (read
reachability via ls-remote and PUSH auth via a --dry-run that transfers
nothing), pandoc when some repo versions .docx, the AI backends the configured
mode needs, and whether the daemon is running.

Output is one [ OK ]/[WARN]/[FAIL] line per check. Exit code: 0 when nothing
FAILED (warnings are informational), 1 otherwise.
"""

import importlib
import os
import shutil
import urllib.request

from .gitrepo import GitError, GitRepo, resolve_pandoc
from .runtime import ping_existing_instance

OK, WARN, FAIL = "OK", "WARN", "FAIL"


class _Report:
    def __init__(self):
        self.failed = 0

    def add(self, status: str, label: str, detail: str = ""):
        if status == FAIL:
            self.failed += 1
        mark = {OK: "[ OK ]", WARN: "[WARN]", FAIL: "[FAIL]"}[status]
        print(f"{mark} {label}" + (f" — {detail}" if detail else ""))


def _check_git(rep: _Report) -> bool:
    git = shutil.which("git")
    if not git:
        rep.add(FAIL, "git", "not found on PATH — install Git for Windows")
        return False
    try:
        import subprocess
        out = subprocess.run(["git", "--version"], capture_output=True, text=True,
                             timeout=10).stdout.strip()
        rep.add(OK, "git", out or git)
        return True
    except Exception as e:  # noqa: BLE001 — a broken git IS the finding
        rep.add(FAIL, "git", f"present but not runnable: {e}")
        return False


def _check_repo(rep: _Report, rc) -> None:
    name = f"repo '{rc.name}'"
    if not os.path.isdir(rc.path):
        rep.add(FAIL, name, f"path does not exist: {rc.path}")
        return
    repo = GitRepo(rc.path)
    try:
        if not repo.is_git_repo():
            rep.add(FAIL, name, f"not a git repository: {rc.path}")
            return
    except GitError as e:
        rep.add(FAIL, name, str(e))
        return
    cur = repo.current_branch()
    if rc.track_current_branch:
        rep.add(OK, name, f"follows the current branch (now '{cur}')")
    elif cur != rc.branch:
        rep.add(WARN, name, f"HEAD is on '{cur}', configured '{rc.branch}' — "
                            f"autosync waits until you switch back")
    else:
        rep.add(OK, name, f"on '{rc.branch}'")
    if repo.is_busy():
        stale = repo.stale_lock()
        if stale:
            rep.add(WARN, name,
                    f"stale git lock (untouched for over an hour): {stale} — "
                    f"if no git command is running, delete that file to let "
                    f"syncing resume")
        else:
            rep.add(WARN, name, "a manual merge/rebase is in progress — the "
                                "daemon yields until it finishes")

    # Remote: configured -> reachable (read) -> push auth (--dry-run, no data).
    if not repo.has_remote(rc.remote):
        state = WARN if (rc.push or rc.pull or rc.autosnap) else OK
        rep.add(state, name, f"no remote '{rc.remote}' — push/pull/autosnap wait "
                             f"until you add one")
        return
    reachable, detail = repo.ls_remote_heads(rc.remote, timeout=rc.git_timeout_sec)
    if not reachable:
        rep.add(FAIL, name, f"remote '{rc.remote}' unreachable: {detail}")
        return
    rep.add(OK, name, f"remote '{rc.remote}' reachable")
    # Write auth is needed by push AND by autosnap (which force-pushes the mirror
    # to refs/autosnap/... regardless of `push`). Verify it whenever either is on,
    # so a `push: false, autosnap: true` repo isn't reported healthy while every
    # mirror push silently fails. The dry-run transfers nothing.
    if rc.push or rc.autosnap:
        what = "push" if rc.push else "autosnap"
        ok, detail = repo.push_dry_run(rc.remote, rc.branch, timeout=rc.git_timeout_sec)
        if ok:
            rep.add(OK, name, f"{what} credentials verified (dry-run)")
        else:
            rep.add(FAIL, name, f"{what} --dry-run failed: {detail}")


def _check_ai(rep: _Report, ai) -> None:
    if ai.mode == "none":
        rep.add(OK, "AI", "off (deterministic messages)")
        return
    if ai.mode in ("hybrid", "local"):
        try:
            # rstrip like ai.py does: a trailing slash in ollama_url would probe
            # '//api/tags' -> 404 -> a false "not reachable" while generation works.
            with urllib.request.urlopen(f"{ai.ollama_url.rstrip('/')}/api/tags", timeout=3):
                rep.add(OK, "AI: Ollama", f"reachable at {ai.ollama_url}")
        except Exception:  # noqa: BLE001 — unreachable IS the finding
            state = WARN if ai.mode == "hybrid" else FAIL
            rep.add(state, "AI: Ollama", f"not reachable at {ai.ollama_url}"
                    + (" (hybrid falls back to the cloud)" if ai.mode == "hybrid" else ""))
    if ai.mode in ("hybrid", "cloud"):
        if os.environ.get(ai.api_key_env):
            rep.add(OK, "AI: cloud key", f"{ai.api_key_env} is set")
        else:
            state = WARN if ai.mode == "hybrid" else FAIL
            rep.add(state, "AI: cloud key",
                    f"{ai.api_key_env} not set"
                    + (" (hybrid falls back to deterministic messages)"
                       if ai.mode == "hybrid" else ""))


def run_doctor(config) -> int:
    """All checks against `config` (an already-loaded Config). Returns the
    process exit code: 0 = healthy (warnings allowed), 1 = something FAILED."""
    rep = _Report()
    if not _check_git(rep):
        return 1  # nothing else is meaningful without git

    try:
        importlib.import_module("watchdog")
        rep.add(OK, "watchdog", "installed (automatic change detection)")
    except Exception:  # noqa: BLE001
        rep.add(WARN, "watchdog", "not importable — automatic snapshots are OFF")

    if not config.repos:
        rep.add(WARN, "config", "no repos configured yet (add one from the GUI)")
    for rc in config.repos:
        _check_repo(rep, rc)

    if any(any(".docx" in p.lower() for p in rc.extra_includes)
           for rc in config.repos):
        pandoc = resolve_pandoc(config.pandoc_path)
        if pandoc:
            rep.add(OK, "pandoc", f"resolved: {pandoc} (readable .docx diffs)")
        else:
            rep.add(WARN, "pandoc",
                    f"'{config.pandoc_path}' not found — .docx versioned as opaque blobs")

    if any(any(".pptx" in p.lower() for p in rc.extra_includes)
           for rc in config.repos):
        from .convert import pptx_available
        if pptx_available():
            rep.add(OK, "python-pptx", "installed (readable .pptx previews/diffs)")
        else:
            rep.add(WARN, "python-pptx",
                    "not installed — .pptx versioned as opaque blobs "
                    "(pip install python-pptx)")

    _check_ai(rep, config.ai)

    running = ping_existing_instance()
    rep.add(OK if running else WARN, "daemon",
            "running" if running
            else "not running — snapshots only happen while SincroGit is open")

    print()
    print("Everything looks healthy." if rep.failed == 0
          else f"{rep.failed} check(s) FAILED — fix them and re-run --doctor.")
    return 0 if rep.failed == 0 else 1
