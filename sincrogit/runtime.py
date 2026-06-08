"""Runtime helpers for the standalone executable.

- Config resolution (next to the .exe -> %APPDATA%\\SincroGit -> cwd), creating a
  default one on first run.
- Single-instance lock via a localhost socket (no stale-lock problem: the OS frees
  the port when the process dies) + a tiny activation channel.
- Attaching to the parent console so a windowed (--noconsole) exe can still print
  CLI output to the terminal that launched it.
"""

import os
import socket
import sys

APP_NAME = "SincroGit"
CONFIG_NAME = "sincrogit.config.yaml"
_LOCK_HOST = "127.0.0.1"
_LOCK_PORT = 49677  # high port used as the single-instance lock + activation channel
# Tiny handshake so we can tell a real SincroGit from an unrelated app that
# happens to hold the port (it sits in Windows' ephemeral range, so a transient
# squatter is possible). Only a peer that replies with the ACK counts as "us".
_HANDSHAKE_REQ = b"SINCROGIT:show"
_HANDSHAKE_ACK = b"SINCROGIT:ok"

# Template written on first run when no config is found anywhere.
# 'repos' starts empty: add them from the GUI (Status -> Add repo...).
DEFAULT_CONFIG_TEMPLATE = """\
# SincroGit configuration.
# Add repos from the GUI (Status -> "Add repo..."), or list them under 'repos:'.
# Tip: any interval/size below can be disabled with `inf` (or off/none/never) -> never
# fires / no limit. `seal_interval_min: inf` = purist mode (commit by hand). Beware
# `max_file_bytes: inf`: it drops the size guard and may commit huge files forever.

defaults:
  snapshot_interval_sec: 300      # how often the WIP is amended (5 min)
  debounce_sec: 25                # wait after the last change before a snapshot
  seal_interval_min: 360          # "real" permanent commit + push every 6h ("inf" = purist)
  pull_interval_min: 10           # fetch every 10 min; pull only if there's something new
  max_file_bytes: 1048576         # 1 MB: only text below this size is versioned
  push: true
  pull: true
  git_timeout_sec: 60
  autosnap: true                  # mirror HEAD (incl. WIP) to refs/autosnap/<user>/<host>/<branch>
  autosnap_interval_min: 30       # force-push the live mirror every 30 min (only if changed)
  live_handoff: auto              # pick up your other machine's live WIP: auto (fast-forward
                                  # + notify) | ask (one-click apply) | off. Needs autosnap on.
  extra_excludes:
    - "**/node_modules/**"
    - "**/.venv/**"
    - "**/__pycache__/**"
    - "**/dist/**"
    - "**/build/**"
  # Version these binary files too (opt-in). With pandoc (see pandoc_path) their
  # diffs are shown readably (e.g. Word docs). Uncomment to enable:
  extra_includes: []
  #   - "**/*.docx"
  max_include_bytes: 26214400     # 25 MB cap for extra_includes

# Path to pandoc, for readable diffs of .docx and similar (machine-specific).
# "pandoc" if it's on PATH; otherwise a full path, e.g. C:/tools/pandoc.exe
pandoc_path: pandoc

ai:
  mode: hybrid                    # hybrid | local | cloud | none
  cloud_provider: gemini
  cloud_model: gemini-2.5-flash-lite
  cloud_send_content: false       # if false, only names + --stat go to the cloud
  api_key_env: SINCROGIT_GEMINI_KEY
  ollama_url: http://localhost:11434
  ollama_model: llama3.2
  timeout_sec: 30
  max_diff_chars: 6000

log:
  file: sincrogit.log             # relative paths are resolved next to this file
  level: INFO

repos: []
"""


# --------------------------------------------------------------- paths / config
def exe_dir() -> str:
    """Directory of the running executable (or cwd when run as a script)."""
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.getcwd()


def appdata_dir() -> str:
    base = os.environ.get("APPDATA") or os.path.expanduser("~")
    return os.path.join(base, APP_NAME)


def candidate_config_paths() -> list:
    return [
        os.path.join(exe_dir(), CONFIG_NAME),
        os.path.join(appdata_dir(), CONFIG_NAME),
        os.path.join(os.getcwd(), CONFIG_NAME),
    ]


def find_config() -> str | None:
    for p in candidate_config_paths():
        if os.path.exists(p):
            return os.path.abspath(p)
    return None


def ensure_config(explicit: str | None) -> tuple:
    """Resolve the config path, creating a default if none exists.

    Returns (path, created). The default is written next to the executable; if
    that folder isn't writable (e.g. Program Files), it falls back to
    %APPDATA%\\SincroGit, then the cwd. So first run always works.
    """
    if explicit:
        return os.path.abspath(explicit), False
    found = find_config()
    if found:
        return found, False
    for path in (os.path.join(exe_dir(), CONFIG_NAME),
                 os.path.join(appdata_dir(), CONFIG_NAME),
                 os.path.join(os.getcwd(), CONFIG_NAME)):
        try:
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(DEFAULT_CONFIG_TEMPLATE)
            return os.path.abspath(path), True
        except OSError:
            continue
    raise OSError("Could not create a default config in any location.")


# ---------------------------------------------------------------- single instance
def acquire_single_instance(port: int = _LOCK_PORT):
    """Bind a localhost port as a lock. Returns the socket (keep it alive) or
    None if another instance already holds it."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind((_LOCK_HOST, port))
        s.listen(1)
        return s
    except OSError:
        s.close()
        return None


def signal_existing_instance(port: int = _LOCK_PORT) -> bool:
    """Ask whoever holds the lock port to show SincroGit's panel.

    Returns True ONLY if a real SincroGit answered our handshake. If the port is
    held by an unrelated application (no valid reply), returns False so the caller
    can start normally instead of silently refusing to run.
    """
    try:
        with socket.create_connection((_LOCK_HOST, port), timeout=2) as c:
            c.sendall(_HANDSHAKE_REQ)
            c.settimeout(3)  # give the peer time to answer once its listener is up
            reply = c.recv(64)
        return reply.startswith(_HANDSHAKE_ACK)
    except OSError:
        return False


def serve_activation(conn) -> bool:
    """Handle one inbound connection on the lock socket (server side).

    Returns True if it was a valid SincroGit activation request (the caller should
    then show the panel); False for anything else that hit the port. Always closes
    the connection.
    """
    try:
        data = conn.recv(64)
        if data.startswith(_HANDSHAKE_REQ):
            conn.sendall(_HANDSHAKE_ACK)
            return True
        return False
    except OSError:
        return False
    finally:
        try:
            conn.close()
        except OSError:
            pass


# ----------------------------------------------------------------- console (CLI)
def attach_parent_console() -> None:
    """Attach a windowed (--noconsole) exe to the terminal that launched it, so
    CLI output is visible. No-op when not frozen or not on Windows.

    A windowed build has sys.stdout/stderr == None; we always leave them as
    writable streams (the console if available, otherwise os.devnull) so that
    print() never crashes even when launched without a terminal.
    """
    if os.name != "nt" or not getattr(sys, "frozen", False):
        return
    try:
        import ctypes

        ATTACH_PARENT_PROCESS = -1
        if ctypes.windll.kernel32.AttachConsole(ATTACH_PARENT_PROCESS):
            sys.stdout = open("CONOUT$", "w", encoding="utf-8", buffering=1)
            sys.stderr = open("CONOUT$", "w", encoding="utf-8", buffering=1)
            try:
                sys.stdin = open("CONIN$", "r", encoding="utf-8")
            except OSError:
                pass
    except Exception:  # noqa: BLE001 — console attach is best-effort
        pass
    # Never leave the std streams as None (windowed builds do).
    if sys.stdout is None:
        sys.stdout = open(os.devnull, "w", encoding="utf-8")
    if sys.stderr is None:
        sys.stderr = open(os.devnull, "w", encoding="utf-8")
