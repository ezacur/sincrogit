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
# Activation channel ("show the running panel"). Single-instance safety is the named
# mutex (acquire_instance_mutex), not this port. Deliberately BELOW Windows' ephemeral
# range (49152-65535) so the OS won't randomly hand it to some app's outbound socket.
_LOCK_PORT = 29677
# Tiny handshake so we can tell a real SincroGit from an unrelated app that happens
# to hold the port (still possible, just unlikely now). Only a peer that replies with
# the ACK counts as "us". The "ping" variant is a pure presence probe (no panel pops):
# CLI one-shots use it to detect a running daemon before touching the same repos.
# "flushquit" asks the daemon to flush every repo (snapshot + autosnap push) and then
# exit cleanly — build.ps1 uses it to rebuild the very exe that is running. Localhost
# only; the worst an abuser gains is a clean shutdown of a personal tool.
_HANDSHAKE_REQ = b"SINCROGIT:show"
_HANDSHAKE_PING = b"SINCROGIT:ping"
_HANDSHAKE_FLUSHQUIT = b"SINCROGIT:flushquit"
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
  autosnap: true                  # mirror the shadow tip (latest snapshot, incl. live WIP) to refs/autosnap/<user>/<host>/<branch>
  autosnap_interval_min: 30       # force-push the live mirror every 30 min (only if changed)
  seal_on_leave_min: 20           # seal (+push) 20 min after locking the machine, unless you
                                  # come back first ("off" disables; ignored in purist mode)
  live_handoff: auto              # pick up your other machine's live WIP: auto (fast-forward
                                  # + notify) | ask (one-click apply) | off. Needs autosnap on.
  track_current_branch: false     # false = pause off `branch`; true = follow the current
                                  # branch (feature-branch workflow; pairs with purist mode).
  suggest_excludes: true          # suggest once adding a high-churn folder to extra_excludes
  suggest_commit: true            # purist mode only: remind (once/day) to Smart Commit when work piles up
  extra_excludes:
    - "**/node_modules/**"
    - "**/.venv/**"
    - "**/__pycache__/**"
    - "**/dist/**"
    - "**/build/**"
  # Version these binary files too (opt-in). Word docs diff readably with pandoc
  # (see pandoc_path); PowerPoint with python-pptx (bundled). Uncomment to enable:
  extra_includes: []
  #   - "**/*.docx"
  #   - "**/*.pptx"
  max_include_bytes: 26214400     # 25 MB cap for extra_includes

# Path to pandoc, for readable diffs of .docx and similar (machine-specific).
# "pandoc" if it's on PATH; otherwise a full path, e.g. C:/tools/pandoc.exe
pandoc_path: pandoc

# GUI theme: auto (follow Windows' app theme), light, or dark.
theme: auto

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
  language: en                    # language of the AI commit messages: en | es

log:
  file: sincrogit.log             # relative paths are resolved next to this file
  level: INFO

repos: []
# Example entry (uncomment and adjust — or just use the GUI: Status -> "Add repo..."):
#   - path: C:/work/myproject
#     name: myproject             # display name (defaults to the folder name)
#     branch: main
#     remote: origin
#     # ...plus ANY key from 'defaults:' above to override it for this repo only,
#     # e.g. seal_interval_min: inf   (purist mode just here)
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
_instance_mutex_handle = None  # kept for the process lifetime (never CloseHandle)


def acquire_instance_mutex(name: str = "Local\\SincroGit-tray-instance") -> bool | None:
    """Authoritative single-instance guard on Windows: a named mutex. Unlike a
    lockfile it has NO stale-lock problem (the OS releases it when the process
    dies), and unlike the port lock it can't be stolen by an unrelated app
    squatting on the port. Tri-state:

      True  — ANOTHER instance already holds it (authoritative: back off).
      False — WE hold it now (authoritative: no other mutex-aware SincroGit
              runs — callers must NOT let a port ACK override this, or any
              local process spoofing the handshake could block startup).
      None  — the mutex isn't available (non-Windows, or ctypes failed): the
              port handshake remains the only signal there.

    The handle is kept in a module global for the process lifetime; we never
    CloseHandle it, so the mutex is held until we exit.
    """
    global _instance_mutex_handle
    if sys.platform != "win32":
        return None
    try:
        import ctypes
        from ctypes import wintypes
        # use_last_error=True + get_last_error(): reading GetLastError through
        # windll is officially unreliable (ctypes' own interposed Win32 calls
        # can clobber the thread's last error in between).
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateMutexW.restype = wintypes.HANDLE
        kernel32.CreateMutexW.argtypes = (wintypes.LPVOID, wintypes.BOOL,
                                          wintypes.LPCWSTR)
        handle = kernel32.CreateMutexW(None, False, name)
        already = (ctypes.get_last_error() == 183)  # ERROR_ALREADY_EXISTS
        if not handle:
            return already if already else None  # creation failed: nothing held
        if _instance_mutex_handle is None:
            _instance_mutex_handle = handle  # keep alive (the ONE per-process handle)
        else:
            # Repeat call in the same process: don't leak/overwrite — extra live
            # handles would keep the mutex alive past release_instance_mutex().
            kernel32.CloseHandle(handle)
        return already
    except Exception:  # noqa: BLE001 — if the mutex can't be created, fall back to the port
        return None


def release_instance_mutex() -> None:
    """Release the named mutex explicitly (Windows; no-op elsewhere/if unheld).

    Needed right before a self-restart (os.execv): the dying parent could
    otherwise still hold the mutex when the child checks it, making the child
    conclude another instance runs and exit — leaving no SincroGit at all.
    Normal shutdown doesn't need this (the OS releases it on process death)."""
    global _instance_mutex_handle
    if sys.platform != "win32" or _instance_mutex_handle is None:
        return
    try:
        import ctypes
        ctypes.windll.kernel32.CloseHandle(_instance_mutex_handle)
    except Exception:  # noqa: BLE001 — releasing is best-effort
        pass
    _instance_mutex_handle = None


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


def ping_existing_instance(port: int = _LOCK_PORT) -> bool:
    """Is a real SincroGit holding the lock port? Like signal_existing_instance,
    but a pure presence probe — it does NOT ask the daemon to show its panel.
    Used by CLI one-shots to detect a running daemon before racing its git work."""
    try:
        with socket.create_connection((_LOCK_HOST, port), timeout=2) as c:
            c.sendall(_HANDSHAKE_PING)
            c.settimeout(3)
            reply = c.recv(64)
        return reply.startswith(_HANDSHAKE_ACK)
    except OSError:
        return False


def serve_activation(conn):
    """Handle one inbound connection on the lock socket (server side).

    Returns the command verdict: "show" (bring the panel to front), "flushquit"
    (flush all repos then exit cleanly — the build script's rebuild cycle), or
    None for anything else that hit the port — including a presence ping, which
    is ACKed but demands no action. Always closes the connection.
    """
    try:
        # A client that connects but never sends (a localhost port scanner, a
        # health probe, a stalled second launch) must not wedge the accept loop
        # forever: the listen socket is blocking, so without this recv() would
        # never return and every later "show panel" / "flushquit" would hang.
        conn.settimeout(3)
        data = conn.recv(64)
        # NOTE: "flushquit" must be tested BEFORE bare prefixes ever overlap; the
        # current commands share no prefix, but keep the most specific first.
        if data.startswith(_HANDSHAKE_FLUSHQUIT):
            conn.sendall(_HANDSHAKE_ACK)  # ACK receipt; the flush+quit follows
            return "flushquit"
        if data.startswith(_HANDSHAKE_PING):
            conn.sendall(_HANDSHAKE_ACK)  # presence probe: acknowledge, no action
            return None
        if data.startswith(_HANDSHAKE_REQ):
            conn.sendall(_HANDSHAKE_ACK)
            return "show"
        return None
    except OSError:
        return None
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
