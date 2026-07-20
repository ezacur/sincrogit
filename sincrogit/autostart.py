"""Start-at-login via the per-user Run registry key (Windows).

Why the Run key and not the scheduled task DESIGN §9 originally sketched, nor
a Startup-folder shortcut: it needs no elevation (creating an at-logon task
often does), winreg is stdlib (a .lnk needs COM), it is trivially idempotent,
and Windows surfaces it in Task Manager → Startup apps, where the user can
disable it like any other app. The daemon needs none of a scheduled task's
extras: the single-instance mutex already dedupes double launches, and the
engine tolerates starting before the network is up (pull retries on its
intervals).

Deliberately NOT part of config.yaml: auto-start is a property of THIS
machine (the registered command embeds this machine's exe and config paths),
while config.yaml may travel between machines. The registry is the single
source of truth; the GUI checkbox just reflects it.
"""

import os
import sys

# Module-level so tests can point them at a throwaway subkey.
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
VALUE_NAME = "SincroGit"


def supported() -> bool:
    return sys.platform == "win32"


def autostart_command(config_path: str) -> str:
    """The command the Run key should hold for THIS installation: the frozen
    exe when we are one, else `pythonw -m sincrogit` for a source checkout
    (pythonw so no console flashes at logon). `--tray` and the config path are
    explicit — logon must start THIS setup, not whatever a bare launch would
    resolve to."""
    cfg = os.path.abspath(config_path)
    if getattr(sys, "frozen", False):
        return f'"{sys.executable}" --tray -c "{cfg}"'
    pyw = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
    py = pyw if os.path.exists(pyw) else sys.executable
    return f'"{py}" -m sincrogit --tray -c "{cfg}"'


def get_autostart() -> str | None:
    """The registered command, or None when absent (or not on Windows)."""
    if not supported():
        return None
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            value, _kind = winreg.QueryValueEx(key, VALUE_NAME)
            return str(value)
    except OSError:
        return None


def is_enabled() -> bool:
    return get_autostart() is not None


def target_of(cmd: str) -> str | None:
    """The executable path inside a registered command (quoted or bare)."""
    cmd = cmd.strip()
    if cmd.startswith('"'):
        end = cmd.find('"', 1)
        return cmd[1:end] if end > 0 else None
    return cmd.split(" ", 1)[0] or None


def is_stale() -> bool:
    """True when an entry exists but its target executable doesn't (moved
    exe, deleted checkout): Windows would silently launch nothing at logon."""
    cmd = get_autostart()
    if cmd is None:
        return False
    exe = target_of(cmd)
    return not (exe and os.path.exists(exe))


def set_autostart(enabled: bool, config_path: str) -> tuple:
    """(ok, msg). Register/remove the start-at-login entry for the current
    user. Removing a value that isn't there counts as success (idempotent)."""
    if not supported():
        return False, "start at login is only supported on Windows"
    import winreg
    try:
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            if enabled:
                winreg.SetValueEx(key, VALUE_NAME, 0, winreg.REG_SZ,
                                  autostart_command(config_path))
                return True, "start at login enabled"
            try:
                winreg.DeleteValue(key, VALUE_NAME)
            except FileNotFoundError:
                pass
            return True, "start at login disabled"
    except OSError as e:
        return False, f"could not update the Run key: {e}"


def heal(config_path: str) -> bool:
    """Tray startup hook: if auto-start is enabled but points at an exe that
    NO LONGER EXISTS, re-register the current invocation (a rebuild moved
    dist\\, the exe was relocated…). Only the stale case is healed — a dev
    launch must never hijack an entry that points at a live installed exe.
    Returns True when the entry was rewritten."""
    if not supported() or not is_stale():
        return False
    ok, _ = set_autostart(True, config_path)
    return ok
