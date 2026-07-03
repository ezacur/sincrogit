"""SincroGit tray app (PyQt5).

Threading architecture:
  - Main thread: the Qt loop (tray + windows).
  - Background thread (daemon): engine.run() (the snapshot/seal/sync cycle).
  - The engine emits events via a callback that: (a) stores them in the EventLog
    and (b) emits a Qt signal -> the GUI updates on its thread, without blocking.

Manual actions (Sync/Seal now) run on a separate thread so as not to freeze the
interface; the engine serializes them per repo with each repo's op_lock.
"""

import logging
import os
import re
import sys
import threading
import time

import yaml
from PyQt5.QtCore import QAbstractNativeEventFilter, QObject, QTimer, pyqtSignal
from PyQt5.QtWidgets import QApplication, QMenu, QSystemTrayIcon

from ..config import append_repo, load_config
from ..engine import Engine
from ..events import EventLog
from ..log import setup_logging
from ..runtime import release_instance_mutex, serve_activation
from . import icon as iconmod
from .control_panel import ControlPanel
from .theme import apply_theme

# --- Windows session/power messages (Phase 3: cut cross-machine handoff latency) ---
_WM_WTSSESSION_CHANGE = 0x02B1
_WTS_SESSION_LOCK = 0x7
_WTS_SESSION_UNLOCK = 0x8
_WM_POWERBROADCAST = 0x0218
_PBT_APMSUSPEND = 0x0004
_PBT_APMRESUMESUSPEND = 0x0007
_PBT_APMRESUMEAUTOMATIC = 0x0012
_NOTIFY_FOR_THIS_SESSION = 0


class _WinSessionEventFilter(QAbstractNativeEventFilter):
    """Catches Windows lock/unlock + suspend/resume so SincroGit can flush its WIP to
    the remote when you LEAVE a machine and sync when you ARRIVE — collapsing the
    machine-to-machine handoff latency from minutes to seconds. Windows-only; built
    only when installed, so the module still imports on other platforms."""

    def __init__(self, on_leave, on_arrive):
        super().__init__()
        self._on_leave = on_leave
        self._on_arrive = on_arrive
        import ctypes
        from ctypes import wintypes

        class _MSG(ctypes.Structure):
            _fields_ = [
                ("hwnd", wintypes.HWND), ("message", wintypes.UINT),
                ("wParam", wintypes.WPARAM), ("lParam", wintypes.LPARAM),
                ("time", wintypes.DWORD), ("pt_x", wintypes.LONG), ("pt_y", wintypes.LONG),
            ]

        self._MSG = _MSG

    def nativeEventFilter(self, etype, message):
        try:
            if etype == b"windows_generic_MSG":
                import ctypes
                msg = ctypes.cast(int(message), ctypes.POINTER(self._MSG)).contents
                if msg.message == _WM_WTSSESSION_CHANGE:
                    if msg.wParam == _WTS_SESSION_LOCK:
                        self._on_leave("lock")
                    elif msg.wParam == _WTS_SESSION_UNLOCK:
                        self._on_arrive("unlock")
                elif msg.message == _WM_POWERBROADCAST:
                    if msg.wParam == _PBT_APMSUSPEND:
                        self._on_leave("suspend")
                    elif msg.wParam in (_PBT_APMRESUMEAUTOMATIC, _PBT_APMRESUMESUSPEND):
                        self._on_arrive("resume")
        except Exception:  # noqa: BLE001 — a native event filter must never raise into Qt
            pass
        return False, 0


class _LogBridgeHandler(logging.Handler):
    """Feeds the Python logger's records into the GUI event log, so the Log tab
    shows EVERYTHING the file log shows — including DEBUG detail (filtered files,
    git internals) and warnings raised outside Engine._emit. Records that _emit
    already reported (marked sincro_structured) are skipped to avoid duplicates.
    The handler inherits the configured log level through the logger itself."""

    _REPO_RE = re.compile(r"^\[([^\]]+)\]\s*(.*)$", re.S)

    def __init__(self, sink):
        super().__init__()
        self._sink = sink  # callable(repo, action, message, level)

    def emit(self, record):  # noqa: A003 — logging.Handler API
        if getattr(record, "sincro_structured", False):
            return
        try:
            msg = record.getMessage()
            repo = ""
            m = self._REPO_RE.match(msg)
            if m:
                repo, msg = m.group(1), m.group(2)
            level = record.levelname if record.levelname in (
                "DEBUG", "INFO", "WARNING", "ERROR") else "INFO"
            self._sink(repo, "log", msg, level)
        except Exception:  # noqa: BLE001 — a log handler must never raise
            pass


class _Bridge(QObject):
    """Thread-safe bridge: background threads emit; the GUI receives."""
    event_added = pyqtSignal(object)
    activate = pyqtSignal()  # a second launch asks us to show the panel
    quit_requested = pyqtSignal()  # flushquit command: exit cleanly on the GUI thread


class TrayApp:
    def __init__(self, config_path: str, lock_socket=None, open_config: bool = False):
        self.config_path = os.path.abspath(config_path)
        self.config = load_config(self.config_path)
        self.logger = setup_logging(self.config.log.file, self.config.log.level)
        self._lock_socket = lock_socket  # single-instance lock (kept alive)

        # EventLog next to the log file (events.jsonl).
        log_dir = os.path.dirname(os.path.abspath(self.config.log.file)) or "."
        self.event_log = EventLog(os.path.join(log_dir, "events.jsonl"))

        self.qapp = QApplication.instance() or QApplication(sys.argv)
        self.qapp.setQuitOnLastWindowClosed(False)  # live in the tray
        # Visual theme (light/dark/auto from the config); the returned palette is
        # shared with dialogs that color custom elements (diff HTML, state labels).
        self.theme = apply_theme(self.qapp, getattr(self.config, "theme", "auto"))

        self.bridge = _Bridge()
        self.bridge.event_added.connect(self._on_event_gui)
        self.bridge.activate.connect(self.show_panel)
        self.bridge.quit_requested.connect(self.quit)  # flushquit -> clean exit (GUI thread)

        # Mirror the Python logger into the GUI event log (DEBUG detail and
        # warnings that don't go through Engine._emit). The logger's configured
        # level (log.level in the config) decides how much flows through.
        logging.getLogger("sincrogit").addHandler(
            _LogBridgeHandler(self._on_engine_event))

        self.engine = Engine(self.config, emit_event=self._on_engine_event)
        self._engine_thread = None
        self._last_state = None

        self.panel = ControlPanel(self)
        self._build_tray()

        # Refresh of the icon/tooltip state.
        self._timer = QTimer()
        self._timer.setInterval(2500)
        self._timer.timeout.connect(self._refresh_tray)
        self._timer.start()

        # Listen for "show panel" requests from a second launch.
        self._start_activation_listener()

        # OS session/power hooks: flush on leave (lock/suspend), sync on arrive
        # (unlock/resume), so the cross-machine handoff is prompt. Windows-only.
        self._install_session_hooks()

        # First run (config just created): open the panel on the Config tab.
        if open_config:
            self.show_panel()
            self.panel.select_config_tab()

    # ----------------------------------------- OS session/power hooks (Phase 3)
    def _install_session_hooks(self):
        """Windows: flush the WIP to the remote on lock/suspend (you're LEAVING) and
        sync on unlock/resume (you've ARRIVED), so machine-to-machine handoff drops
        from minutes to seconds. No-op off Windows; failures are non-fatal (the
        periodic autosnap/pull intervals remain the fallback)."""
        self._session_filter = None
        self._session_hwnd = None
        self._last_leave_mono = 0.0
        self._last_arrive_mono = 0.0
        if sys.platform != "win32":
            return
        try:
            import ctypes
            self._session_filter = _WinSessionEventFilter(
                self._on_machine_leave, self._on_machine_arrive
            )
            self.qapp.installNativeEventFilter(self._session_filter)
            hwnd = int(self.panel.winId())  # forces native window creation (stable HWND)
            ctypes.windll.wtsapi32.WTSRegisterSessionNotification(hwnd, _NOTIFY_FOR_THIS_SESSION)
            self._session_hwnd = hwnd
        except Exception as e:  # noqa: BLE001 — non-fatal; the intervals still cover us
            self._session_filter = None
            self._on_engine_event("", "info", f"OS session hooks unavailable: {e}", "WARNING")

    def _remove_session_hooks(self):
        if sys.platform == "win32" and self._session_hwnd is not None:
            try:
                import ctypes
                ctypes.windll.wtsapi32.WTSUnRegisterSessionNotification(self._session_hwnd)
            except Exception:  # noqa: BLE001
                pass
            self._session_hwnd = None

    def _on_machine_leave(self, reason):
        # Debounce: lock usually precedes suspend — don't flush twice in a row.
        if time.monotonic() - self._last_leave_mono < 10:
            return
        self._last_leave_mono = time.monotonic()
        self._on_engine_event("", "flush", f"machine {reason}: flushing latest state", "INFO")
        self.engine.flush_now()

    def _on_machine_arrive(self, reason):
        # Debounce: resume usually precedes unlock — don't sync twice in a row.
        if time.monotonic() - self._last_arrive_mono < 10:
            return
        self._last_arrive_mono = time.monotonic()
        self._on_engine_event("", "resume", f"machine {reason}: syncing to catch up", "INFO")
        self.engine.sync_soon()

    def _start_activation_listener(self):
        if not self._lock_socket:
            return

        def loop():
            while True:
                try:
                    conn, _ = self._lock_socket.accept()
                except OSError:
                    break  # socket closed on quit
                # Only react to a valid SincroGit handshake (serve_activation
                # answers the ACK); ignore anything else that hit the port.
                verdict = serve_activation(conn)
                if verdict == "show":
                    self.bridge.activate.emit()
                elif verdict == "flushquit":
                    # build.ps1 is about to rebuild this very exe: flush every repo
                    # (snapshot + autosnap push, synchronous — don't die mid-push)
                    # and exit cleanly via the GUI thread. The listener thread may
                    # block here; that's fine, we're quitting anyway.
                    self._on_engine_event(
                        "", "flush", "rebuild requested: flushing all repos, then exiting", "WARNING")
                    self.engine.flush_now(wait=True)
                    self.bridge.quit_requested.emit()
                    break

        threading.Thread(target=loop, name="sincrogit-activation", daemon=True).start()

    # --------------------------------------------------- engine <-> GUI (signal)
    def _on_engine_event(self, repo, action, message, level):
        """Called FROM THE ENGINE THREAD. Stores and forwards to the GUI via signal."""
        ev = self.event_log.add(repo, action, message, level)
        self.bridge.event_added.emit(ev)

    def _on_event_gui(self, ev):
        """On the GUI thread: refresh the panel and warn about conflicts."""
        try:
            self.panel.append_event(ev)
        except Exception:
            pass
        if ev.action == "conflict" or ev.level == "ERROR":
            self.tray.showMessage(
                "SincroGit",
                f"[{ev.repo}] {ev.message}",
                QSystemTrayIcon.Warning,
                8000,
            )
        self._refresh_tray()

    # ------------------------------------------------------------- tray
    def _build_tray(self):
        self.tray = QSystemTrayIcon(iconmod.make_icon("running"))
        self.tray.setToolTip("⏳g SincroGit")
        menu = QMenu()

        self.act_panel = menu.addAction("Open control panel")
        self.act_panel.triggered.connect(self.show_panel)
        menu.addSeparator()
        self.act_pause = menu.addAction("Pause")
        self.act_pause.triggered.connect(self._toggle_pause)
        self.act_sync = menu.addAction("Sync now")
        self.act_sync.triggered.connect(self.sync_now)
        self.act_seal = menu.addAction("Seal now")
        self.act_seal.triggered.connect(self.seal_now)
        menu.addSeparator()
        self.act_quit = menu.addAction("Quit")
        self.act_quit.triggered.connect(self.quit)

        self.tray.setContextMenu(menu)
        self.tray.activated.connect(self._on_tray_activated)
        self.tray.show()

    def _on_tray_activated(self, reason):
        if reason in (QSystemTrayIcon.DoubleClick, QSystemTrayIcon.Trigger):
            self.show_panel()

    def _refresh_tray(self):
        state = self.app_state()
        self.act_pause.setText("Resume" if state == "paused" else "Pause")
        if state != self._last_state:
            self._last_state = state
            self.tray.setIcon(iconmod.make_icon(state))
            self.tray.setToolTip(iconmod.STATE_TOOLTIP.get(state, "SincroGit"))

    # --------------------------------------------------------------- lifecycle
    def run(self) -> int:
        self._engine_thread = threading.Thread(
            target=self.engine.run, name="sincrogit-engine", daemon=True
        )
        self._engine_thread.start()
        return self.qapp.exec_()

    def _release_lock(self):
        if self._lock_socket is not None:
            try:
                self._lock_socket.close()
            except OSError:
                pass
            self._lock_socket = None

    def quit(self):
        self._timer.stop()
        self._remove_session_hooks()
        self.engine.stop()
        if self._engine_thread:
            self._engine_thread.join(timeout=15)
        self._release_lock()
        self.tray.hide()
        self.qapp.quit()

    def restart(self):
        """Relaunches the process to apply the new config."""
        self._remove_session_hooks()
        self.engine.stop()
        if self._engine_thread:
            self._engine_thread.join(timeout=15)
        self._release_lock()  # free the single-instance port before re-launching
        # Also release the named mutex NOW: os.execv spawns the child while this
        # process is still dying — if it still held the mutex, the child would
        # see "already running" and exit, leaving no SincroGit at all.
        release_instance_mutex()
        self.tray.hide()
        if getattr(sys, "frozen", False):
            args = [sys.executable, "--tray", "-c", self.config_path]
        else:
            args = [sys.executable, "-m", "sincrogit", "--tray", "-c", self.config_path]
        os.execv(sys.executable, args)

    # ============================ 'controller' interface for the panel =======
    def status(self):
        return self.engine.status()

    def events_all(self):
        return self.event_log.load_all()

    def app_state(self) -> str:
        st = self.engine.status()
        if not st.get("running", True):
            return "stopped"
        if st.get("paused"):
            return "paused"
        if any(r["conflict_paused"] for r in st["repos"]):
            return "conflict"
        return "running"

    def pause_all(self):
        self.engine.pause()
        self._refresh_tray()

    def resume_all(self):
        self.engine.resume()
        self._refresh_tray()

    def _toggle_pause(self):
        if self.engine.is_paused():
            self.resume_all()
        else:
            self.pause_all()

    def _run_async(self, fn, label):
        """Runs a network action on a thread so as not to freeze the GUI."""
        def worker():
            try:
                fn()
            except Exception as e:  # noqa: BLE001
                self.event_log.add("", "error", f"{label} failed: {e}", "ERROR")
        threading.Thread(target=worker, name=f"sincrogit-{label}", daemon=True).start()

    def sync_now(self):
        self._run_async(self.engine.sync_all_now, "sync")

    def seal_now(self):
        self._run_async(self.engine.seal_all_now, "seal")

    def show_panel(self):
        self.panel.show()
        self.panel.raise_()
        self.panel.activateWindow()

    def make_icon(self, state):
        return iconmod.make_icon(state)

    # ---- per-repo actions ----
    def pause_repo(self, name):
        ok = self.engine.pause_repo(name)
        self._refresh_tray()
        return ok

    def resume_repo(self, name) -> bool:
        ok = self.engine.resume_repo(name)
        self._refresh_tray()
        return ok

    def seal_repo_now(self, name):
        # Surface the outcome as an event — silently dropping a refusal (busy,
        # off-branch…) or a no-op left the panel button looking dead. A real
        # seal already emits its own "seal"/"push" events from the engine.
        def work():
            ok, msg = self.engine.seal_repo_now(name)
            if not ok:
                self.event_log.add(name, "seal", f"not sealed: {msg}", "WARNING")
            elif msg != "sealed":
                self.event_log.add(name, "seal", msg, "INFO")  # "nothing to seal"
        self._run_async(work, f"seal:{name}")

    def propose_seal_message(self, name):
        """(ok, title, body, files_text) — proposed manual-commit message. May be
        slow (AI); callers run it off the GUI thread."""
        return self.engine.propose_seal_message(name)

    def smart_commit(self, name, message):
        """Manual commit: seal the current WIP with the developer's own message."""
        return self.engine.seal_repo_now(name, message=message)

    def pull_repo_now(self, name):
        def work():
            ok, msg = self.engine.pull_repo_now(name)
            self.event_log.add(name, "pull", msg if ok else f"not pulled: {msg}",
                               "INFO" if ok else "WARNING")
        self._run_async(work, f"pull:{name}")

    def apply_handoff(self, name):
        """Apply a pending cross-machine handoff ('ask' mode, one click). Runs on a
        thread (it fetches + git); success is notified by the engine, failures are
        surfaced in the log."""
        def worker():
            ok, msg = self.engine.apply_handoff(name)
            if not ok:
                self.event_log.add(name, "handoff", f"apply failed: {msg}", "WARNING")
            self._refresh_tray()
        threading.Thread(target=worker, name=f"sincrogit-handoff-{name}", daemon=True).start()

    def add_repo(self, path, branch="main", push=True, pull=True, normalize_eol=True):
        """Validate, persist to the config file, and add the repo live. (ok, msg).

        If `normalize_eol` and the repo has no .gitattributes, a '* text=auto' one
        is created so line endings stay consistent across machines.
        """
        from ..gitrepo import GitError, GitRepo
        path = os.path.abspath(path)
        try:
            if not GitRepo(path).is_git_repo():
                return False, "The selected folder is not a git repository."
        except GitError as e:
            return False, str(e)
        if any(os.path.abspath(r["path"]) == path for r in self.engine.status()["repos"]):
            return False, "That repo is already added."
        entry = {
            "path": path.replace("\\", "/"),
            "remote": "origin",
            "branch": branch or "main",
            "push": bool(push),
            "pull": bool(pull),
        }
        try:
            append_repo(self.config_path, entry)
        except OSError as e:
            return False, f"Could not write the config: {e}"
        # Reload to apply 'defaults' to the new entry, then add it live.
        try:
            reloaded = load_config(self.config_path)
        except (FileNotFoundError, ValueError) as e:
            return False, f"Config reload failed: {e}"
        self.config = reloaded
        rc = next((r for r in reloaded.repos if os.path.abspath(r.path) == path), None)
        if rc is None:
            return False, "Repo was written but not found on reload."
        ok, msg = self.engine.add_repo(rc)
        if ok and normalize_eol:
            try:
                if GitRepo(path).ensure_gitattributes():
                    self.event_log.add(
                        rc.name, "info",
                        ".gitattributes created (line-ending normalization)",
                    )
            except Exception:  # noqa: BLE001 — best-effort convenience
                pass
        self._refresh_tray()
        return ok, msg

    # ---- per-repo configuration (Properties dialog) ----
    _REPO_CFG_FIELDS = (
        "branch", "remote", "snapshot_interval_sec", "seal_interval_min",
        "push", "pull", "pull_interval_min", "autosnap", "autosnap_interval_min",
        "live_handoff", "track_current_branch", "extra_excludes", "extra_includes",
    )

    def repo_config_view(self, name):
        """(entry, effective) for the Properties dialog: `entry` is the repo's RAW
        config entry (explicit keys only), `effective` the values the engine runs
        with (entry merged over defaults). ({}, {}) if the repo isn't found."""
        from ..config import find_repo_entry
        try:
            entry = find_repo_entry(self.config_path, name) or {}
        except (OSError, yaml.YAMLError):
            entry = {}
        st = self.engine.repo_state_by_name(name)
        if not st:
            return entry, {}
        effective = {f: getattr(st.cfg, f) for f in self._REPO_CFG_FIELDS}
        return entry, effective

    def update_repo_config(self, name, changes):
        """Persist per-repo overrides to the config file. (ok, msg). Applies on
        restart, like every config edit."""
        from ..config import update_repo
        try:
            return update_repo(self.config_path, name, changes)
        except (OSError, yaml.YAMLError) as e:
            return False, str(e)

    def remove_repo_config(self, name):
        """Remove the repo's entry from the config file (the git repo on disk is
        untouched). (ok, msg). Applies on restart."""
        from ..config import remove_repo
        try:
            return remove_repo(self.config_path, name)
        except (OSError, yaml.YAMLError) as e:
            return False, str(e)

    # ---- file history / restore ----
    def repo_list(self):
        return [(r["name"], r["path"]) for r in self.engine.status()["repos"]]

    def file_history(self, name, relpath):
        return self.engine.file_history(name, relpath)

    def repo_history(self, name, limit=200):
        """The repo's whole-tree version timeline (Time Machine). Blocking (git
        log/reflog): the dialog runs it off the GUI thread."""
        return self.engine.repo_history(name, limit)

    def export_file_version(self, name, relpath, sha, dest_path):
        """Save a copy of a version to `dest_path` (nothing in the repo changes)."""
        return self.engine.export_file_version(name, relpath, sha, dest_path)

    def search_in_file_versions(self, name, relpath, text):
        """[(sha, count)] of `text` across the file's versions. Blocking (one git
        show per version): the dialog runs it off the GUI thread."""
        return self.engine.search_in_file_versions(name, relpath, text)

    def list_autosnaps(self, name):
        """Locally-known autosnap mirrors of every machine (no network)."""
        return self.engine.list_autosnaps(name)

    def this_host(self):
        """This machine's name as used in its autosnap refs."""
        from ..gitrepo import autosnap_host
        return autosnap_host()

    def restore_files(self, name, relpaths, sha):
        """Selectively restore several files to their state at `sha` (one atomic
        WIP capture). Blocking: the dialog runs it off the GUI thread."""
        return self.engine.restore_files(name, relpaths, sha)

    def file_content_at(self, name, relpath, sha):
        return self.engine.file_content_at(name, relpath, sha)

    def file_text_at(self, name, relpath, sha):
        """Readable text of a version (markdown for .docx) — for the history diff."""
        return self.engine.file_text_at(name, relpath, sha)

    def current_text(self, name, relpath):
        """Readable text of the current working-tree file (markdown for .docx)."""
        return self.engine.worktree_text(name, relpath)

    def restore_file(self, name, relpath, sha):
        return self.engine.restore_file(name, relpath, sha)

    def fetch_autosnaps(self, name):
        """Fetch + list other machines' autosnap recovery points. Blocking
        (network + the repo's op_lock): callers on the GUI thread must run it
        on a background thread — the history dialog does."""
        return self.engine.fetch_autosnaps(name)

    def restore_repo_preview(self, name, sha):
        """(ok, payload) — what a whole-repo restore would change. Blocking (git
        diff + the repo's op_lock): the history dialog runs it off the GUI
        thread."""
        return self.engine.restore_repo_preview(name, sha)

    def restore_repo(self, name, sha):
        return self.engine.restore_repo(name, sha)

    # ---- configuration ----
    def config_text(self) -> str:
        try:
            with open(self.config_path, "r", encoding="utf-8") as fh:
                return fh.read()
        except OSError as e:
            return f"# Could not read {self.config_path}: {e}"

    def save_config(self, text: str):
        try:
            yaml.safe_load(text)  # validate it's valid YAML before writing
        except yaml.YAMLError as e:
            return False, f"Invalid YAML: {e}"
        try:
            with open(self.config_path, "w", encoding="utf-8") as fh:
                fh.write(text)
        except OSError as e:
            return False, f"Could not write: {e}"
        return True, "saved"


def main(config_path: str, lock_socket=None, open_config: bool = False) -> int:
    # HiDPI must be configured BEFORE the QApplication exists: crisp scaling on
    # high-resolution displays instead of blurry bitmap stretching.
    from PyQt5.QtCore import Qt
    QApplication.setAttribute(Qt.AA_EnableHighDpiScaling, True)
    QApplication.setAttribute(Qt.AA_UseHighDpiPixmaps, True)
    try:
        app = TrayApp(config_path, lock_socket=lock_socket, open_config=open_config)
    except (FileNotFoundError, ValueError) as e:
        # A broken config (e.g. a TAB snuck into the YAML while hand-editing) must
        # show a friendly dialog: a windowed (--noconsole) exe has no stderr the
        # user can see, so printing — or worse, an uncaught exception — surfaces as
        # PyInstaller's "unhandled exception" crash box.
        from PyQt5.QtWidgets import QMessageBox
        QApplication.instance() or QApplication(sys.argv)
        QMessageBox.critical(
            None, "SincroGit — configuration error",
            f"SincroGit could not start because the configuration file is invalid:"
            f"\n\n{e}\n\nFix the file (file and line above) and launch SincroGit again.",
        )
        return 2
    if not QSystemTrayIcon.isSystemTrayAvailable():
        app.logger.warning("No system tray available.")
    return app.run()
