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
import subprocess
import sys
import threading
import time

import yaml
from PyQt5.QtCore import QAbstractNativeEventFilter, QObject, QTimer, pyqtSignal
from PyQt5.QtWidgets import QApplication, QMenu, QSystemTrayIcon

from .. import autostart
from ..config import _validate_entry, append_repo, atomic_write_text, load_config
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
# Session end (shutdown / restart / logoff): the last chance to flush.
_WM_QUERYENDSESSION = 0x0011
_WM_ENDSESSION = 0x0016
_ENDSESSION_LOGOFF = 0x80000000


class _WinSessionEventFilter(QAbstractNativeEventFilter):
    """Catches Windows lock/unlock + suspend/resume so SincroGit can flush its WIP to
    the remote when you LEAVE a machine and sync when you ARRIVE — collapsing the
    machine-to-machine handoff latency from minutes to seconds. Windows-only; built
    only when installed, so the module still imports on other platforms."""

    def __init__(self, on_leave, on_arrive, on_ending=None, on_end_canceled=None):
        super().__init__()
        self._on_leave = on_leave
        self._on_arrive = on_arrive
        # Session-END callbacks (shutdown/restart/logoff). on_ending(kind) fires
        # on BOTH WM_QUERYENDSESSION and WM_ENDSESSION(TRUE) — a critical
        # shutdown may skip the former — so the receiver must dedupe.
        self._on_ending = on_ending or (lambda kind: None)
        self._on_end_canceled = on_end_canceled or (lambda: None)
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
                elif msg.message == _WM_QUERYENDSESSION:
                    # The session MAY end: flush now — the earliest (and
                    # longest) time budget we will get before Windows kills us.
                    self._on_ending("logoff" if msg.lParam & _ENDSESSION_LOGOFF
                                    else "shutdown")
                elif msg.message == _WM_ENDSESSION:
                    if msg.wParam:  # the end is now CERTAIN
                        self._on_ending("logoff" if msg.lParam & _ENDSESSION_LOGOFF
                                        else "shutdown")
                    else:          # some app vetoed it: we're staying alive
                        self._on_end_canceled()
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
    refresh_tray = pyqtSignal()  # workers may not touch QSystemTrayIcon/QAction directly
    teardown_done = pyqtSignal()  # the engine thread joined; finish quit/restart (GUI)
    # Self-update, two steps so the GUI thread never waits on the network:
    # (status, release|error) after the check, then (path|None, error) after the
    # download. Both carry a tuple; the GUI decides and only then tears down.
    update_checked = pyqtSignal(object)
    update_fetched = pyqtSignal(object)


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
        self.bridge.refresh_tray.connect(self._refresh_tray)
        self.bridge.teardown_done.connect(self._on_teardown_done)
        self.bridge.update_checked.connect(self._on_update_checked)
        self.bridge.update_fetched.connect(self._on_update_fetched)
        self._teardown_then = None   # GUI-thread continuation after the join
        self._quitting = False       # quit/restart in progress (ignore repeats)
        self._updating = False       # a self-update is in flight (see update_and_relaunch)
        self._pending_update = None  # staged, VERIFIED exe awaiting the swap

        # Mirror the Python logger into the GUI event log (DEBUG detail and
        # warnings that don't go through Engine._emit). The logger's configured
        # level (log.level in the config) decides how much flows through.
        logging.getLogger("sincrogit").addHandler(
            _LogBridgeHandler(self._on_engine_event))

        self.engine = Engine(self.config, emit_event=self._on_engine_event,
                             config_path=self.config_path)
        self._engine_thread = None
        self._last_state = None

        self.panel = ControlPanel(self)
        self._build_tray()

        # A previous "Update and relaunch" parked the old binary next to us; it
        # only becomes deletable once that process is gone, which is now.
        if getattr(sys, "frozen", False):
            from .. import updater
            if updater.cleanup_old(os.path.abspath(sys.executable)):
                self.logger.info("removed the previous build left by an update")

        # Start-at-login self-heal: an entry left pointing at an exe that no
        # longer exists (dist\ moved, old install removed) would silently
        # launch nothing at logon — re-register THIS invocation. Only the
        # stale case; a live entry (even a different install) is respected.
        if autostart.heal(self.config_path):
            self._on_engine_event(
                "", "info",
                "start-at-login pointed at a missing program; re-registered "
                "this one", "WARNING")

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
        self._endsession_flushed = False  # dedupe QUERYENDSESSION + ENDSESSION
        if sys.platform != "win32":
            return
        try:
            import ctypes
            self._session_filter = _WinSessionEventFilter(
                self._on_machine_leave, self._on_machine_arrive,
                self._on_session_ending, self._on_session_end_canceled,
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
        # Leave-seal bookkeeping BEFORE the flush debounce (the debounce may
        # swallow this call entirely): the LOCK arms the countdown (a re-lock
        # restarts it); a SUSPEND with one pending fires it right now — the
        # timer can't tick while the machine sleeps (bounded, deterministic
        # message; see Engine.leave_seal_now_if_armed).
        if reason == "lock":
            self.engine.arm_leave_seal()
        elif reason == "suspend":
            self.engine.leave_seal_now_if_armed()
        # Debounce: lock usually precedes suspend — don't flush twice in a row.
        if time.monotonic() - self._last_leave_mono < 10:
            return
        self._last_leave_mono = time.monotonic()
        self._on_engine_event("", "flush", f"machine {reason}: flushing latest state", "INFO")
        self.engine.flush_now()

    def _on_machine_arrive(self, reason):
        # You're back: a pending leave seal is off — BEFORE the debounce, which
        # may swallow this call (resume usually precedes unlock).
        self.engine.disarm_leave_seal()
        # Debounce: resume usually precedes unlock — don't sync twice in a row.
        if time.monotonic() - self._last_arrive_mono < 10:
            return
        self._last_arrive_mono = time.monotonic()
        self._on_engine_event("", "resume", f"machine {reason}: syncing to catch up", "INFO")
        self.engine.sync_soon()

    def _on_session_ending(self, kind):
        """The Windows session is ending (shutdown / restart / logoff): flush
        every repo to the remote SYNCHRONOUSLY — the process dies when this
        handler returns, so async would silently lose the push. A shutdown
        block reason makes Windows show WHAT we're doing (and wait) instead of
        killing us at its default patience. Deduped across the two messages."""
        if self._endsession_flushed:
            return
        self._endsession_flushed = True
        self._shutdown_block("SincroGit: backing up your latest work to the remote…")
        try:
            # The event is written to events.jsonl synchronously, so the line
            # survives even if the flush itself gets cut short.
            self._on_engine_event(
                "", "flush",
                f"machine {kind}: flushing latest state before the session ends",
                "WARNING")
            self.engine.flush_now(wait=True, wait_timeout=20)
        finally:
            self._shutdown_unblock()

    def _on_session_end_canceled(self):
        """Some app vetoed the shutdown — we're staying alive. Re-arm the hook
        so the NEXT real session end flushes again."""
        if self._endsession_flushed:
            self._endsession_flushed = False
            self._on_engine_event("", "info", "session end canceled; still running", "INFO")

    def _shutdown_block(self, reason: str):
        """Register `reason` on Windows' shutdown screen while we flush (best
        effort; without it the OS kills a GUI process ~5 s after ENDSESSION)."""
        try:
            import ctypes
            hwnd = self._session_hwnd or int(self.panel.winId())
            ctypes.windll.user32.ShutdownBlockReasonCreate(hwnd, reason)
        except Exception:  # noqa: BLE001 — the flush still runs, just unshielded
            pass

    def _shutdown_unblock(self):
        try:
            import ctypes
            hwnd = self._session_hwnd or int(self.panel.winId())
            ctypes.windll.user32.ShutdownBlockReasonDestroy(hwnd)
        except Exception:  # noqa: BLE001
            pass

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

    # Actions that can change the tray's visible state (icon/tooltip) right now;
    # everything else (the flood of snapshot/pull/DEBUG records) is picked up by
    # the 2.5 s timer instead — see below.
    _TRAY_ACTIONS = {"conflict", "pause", "resume", "handoff", "repair",
                     "startup", "error"}

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
        # Refresh the tray IMMEDIATELY only when the event might have changed the
        # visible state (warnings/errors or a state-changing action). Every event
        # calling _refresh_tray meant one engine.status() per record — and with
        # log.level=DEBUG that's every filtered-file line; the 2.5 s timer already
        # keeps the icon fresh for the routine flood.
        if ev.level in ("WARNING", "ERROR") or ev.action in self._TRAY_ACTIONS:
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
        self.act_update = menu.addAction("Update and relaunch…")
        self.act_update.setToolTip(
            "Check GitHub for a newer SincroGit, verify it against its published "
            "SHA-256, then flush every repo and restart into the new build.")
        self.act_update.triggered.connect(self.update_and_relaunch)
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

    def _teardown_engine_async(self, then):
        """Stop the engine and JOIN its thread OFF the GUI thread. The engine's
        shutdown takes each repo's op_lock (up to 5 s apiece behind a slow
        network worker) for the final snapshots — joining that on the GUI
        thread froze the tray for up to 15 s and read as 'SincroGit hung on
        quit'. `then` continues on the GUI thread once the engine is down."""
        self._timer.stop()
        self._remove_session_hooks()
        self._teardown_then = then
        self.engine.stop()

        def work():
            if self._engine_thread:
                self._engine_thread.join(timeout=15)
            try:
                self.bridge.teardown_done.emit()
            except RuntimeError:
                pass  # app object torn down already

        threading.Thread(target=work, name="sincrogit-teardown", daemon=True).start()

    def _on_teardown_done(self):
        then, self._teardown_then = self._teardown_then, None
        if then:
            then()

    def quit(self):
        if self._quitting:
            return
        self._quitting = True
        self.tray.setToolTip("SincroGit — shutting down…")
        self._teardown_engine_async(self._finish_quit)

    def _finish_quit(self):
        self._release_lock()
        self.tray.hide()
        self.qapp.quit()

    def restart(self):
        """Relaunches the process to apply the new config."""
        if self._quitting:
            return
        self._quitting = True
        # Leave a trace in the event log: without it a restart reads as an
        # unexplained gap followed by fresh startup lines.
        self._on_engine_event("", "restart",
                              "restarting to apply the new configuration", "INFO")
        self.tray.setToolTip("SincroGit — restarting…")
        self._teardown_engine_async(self._finish_restart)

    def _finish_restart(self):
        self._release_lock()  # free the single-instance port before re-launching
        # Also release the named mutex NOW: the child starts while this process
        # is still dying — if it still held the mutex, the child would see
        # "already running" and exit, leaving no SincroGit at all.
        release_instance_mutex()
        self.tray.hide()
        if getattr(sys, "frozen", False):
            args = [sys.executable, "--tray", "-c", self.config_path]
        else:
            args = [sys.executable, "-m", "sincrogit", "--tray", "-c", self.config_path]
        # Popen, NOT os.execv: execv on Windows joins the argv with spaces and no
        # quoting, so a path like "C:\Program Files\..." reaches the child split
        # into pieces. Popen quotes each argument properly; then exit this process.
        subprocess.Popen(args, close_fds=True)
        self.qapp.quit()

    # ------------------------------------------------------------ self-update
    def update_and_relaunch(self):
        """Tray action: fetch the newest published build and restart into it.

        Split in two worker hops (check, then download) with a confirmation in
        between, because the GUI thread must never wait on the network and
        because replacing the binary the user is running deserves a yes. The
        swap itself happens only after the engine is DOWN — see _finish_update.
        """
        from .. import updater

        if self._quitting or not self._update_busy_guard():
            return
        if not getattr(sys, "frozen", False):
            QMessageBox.information(
                None, "SincroGit",
                "This is running from source, not a packaged exe — there is no "
                "binary to replace.\n\nUse `git pull` and `build.ps1` instead.")
            self._update_done()
            return

        exe = os.path.abspath(sys.executable)
        self._tray_ack("Checking for updates", "Asking GitHub for the latest release…")

        def work():
            try:
                self.bridge.update_checked.emit(updater.check(exe))
            except updater.UpdateError as e:
                self.bridge.update_checked.emit(("error", str(e)))
            except Exception as e:  # noqa: BLE001 — the tray must never die on this
                self.bridge.update_checked.emit(("error", f"unexpected: {e}"))

        threading.Thread(target=work, name="sincrogit-update-check",
                         daemon=True).start()

    def _update_busy_guard(self) -> bool:
        """One update at a time: the action is disabled while one is in flight."""
        if getattr(self, "_updating", False):
            return False
        self._updating = True
        self.act_update.setEnabled(False)
        return True

    def _update_done(self):
        self._updating = False
        try:
            self.act_update.setEnabled(True)
        except RuntimeError:
            pass  # shutting down

    def _on_update_checked(self, result):
        """GUI thread: report, or ask before downloading ~66 MB."""
        status, info = result
        if status == "error":
            self._on_engine_event("", "error", f"update check failed: {info}", "ERROR")
            QMessageBox.warning(None, "SincroGit — update",
                                f"Could not check for updates:\n\n{info}")
            self._update_done()
            return
        if status == "up-to-date":
            QMessageBox.information(
                None, "SincroGit — update",
                f"You are already running the published build "
                f"({info['tag']}).\n\nIts SHA-256 matches this exe.")
            self._update_done()
            return

        mb = info["size"] / (1024 * 1024)
        unverified = ("\n\nWARNING: that release publishes no SHA-256, so the "
                      "download cannot be verified." if not info["digest"] else "")
        if QMessageBox.question(
                None, "SincroGit — update",
                f"A different build is published: {info['tag']} "
                f"({mb:.1f} MB).\n\nDownload it, verify it, and restart SincroGit "
                f"into it?\n\nYour work is flushed and pushed before the "
                f"restart.{unverified}",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.Yes) != QMessageBox.Yes:
            self._update_done()
            return

        from .. import updater
        exe = os.path.abspath(sys.executable)
        dest = updater.staging_path(exe)
        self._tray_ack("Downloading update", f"{info['tag']} — {mb:.1f} MB…")

        def work():
            try:
                updater.download(info["url"], dest, info["size"], info["digest"])
                self.bridge.update_fetched.emit((dest, None))
            except updater.UpdateError as e:
                self.bridge.update_fetched.emit((None, str(e)))
            except Exception as e:  # noqa: BLE001
                self.bridge.update_fetched.emit((None, f"unexpected: {e}"))

        threading.Thread(target=work, name="sincrogit-update-download",
                         daemon=True).start()

    def _on_update_fetched(self, result):
        """GUI thread: the verified binary is staged — tear the engine down and
        swap. Nothing has touched the installed exe yet at this point."""
        path, err = result
        if err or not path:
            self._on_engine_event("", "error", f"update download failed: {err}", "ERROR")
            QMessageBox.warning(None, "SincroGit — update",
                                f"The update was NOT installed:\n\n{err}\n\n"
                                f"SincroGit keeps running on the current build.")
            self._update_done()
            return
        if self._quitting:
            return
        self._quitting = True
        self._pending_update = path
        self._on_engine_event("", "restart",
                              "installing the downloaded update and restarting", "INFO")
        self.tray.setToolTip("SincroGit — updating…")
        self._teardown_engine_async(self._finish_update)

    def _finish_update(self):
        """Engine is down: park the running exe, put the new one at the same path,
        then take the ordinary restart path (which relaunches sys.executable — now
        the NEW binary, since the path never changed)."""
        from .. import updater

        path, self._pending_update = getattr(self, "_pending_update", None), None
        exe = os.path.abspath(sys.executable)
        try:
            updater.swap_in(exe, path)
        except updater.UpdateError as e:
            # swap_in restores the original on failure, so relaunching is safe —
            # and leaving the user with NO daemon would be far worse than a
            # failed update.
            self.logger.error("update swap failed: %s", e)
            QMessageBox.warning(None, "SincroGit — update",
                                f"Could not install the update:\n\n{e}\n\n"
                                f"Restarting on the current build.")
        self._finish_restart()

    # ============================ 'controller' interface for the panel =======
    def status(self):
        return self.engine.status()

    def events_all(self):
        """FULL history — parses the whole JSONL (megabytes). The panel only
        calls this from a worker thread; never call it on the GUI thread."""
        return self.event_log.load_all()

    def events_recent(self):
        """The in-memory tail (instant, no disk): what the panel seeds its Log
        with so the window appears immediately."""
        return self.event_log.recent()

    def app_state(self) -> str:
        st = self.engine.status()
        if not st.get("running", True):
            return "stopped"
        if st.get("paused"):
            return "paused"
        if any(r["conflict_paused"] for r in st["repos"]):
            return "conflict"
        # The snapshots keep running, but the off-machine copy has stopped
        # advancing: the icon must stop saying "all good". See Engine._do_push.
        if any(r.get("state") == "push-failing" for r in st["repos"]):
            return "attention"
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
        """Runs a network action on a thread so as not to freeze the GUI.

        Outcomes go through _on_engine_event, NEVER event_log.add directly:
        add() only writes the store, so the line would skip the Qt bridge and
        never reach the Log — and the panel's in-flight marker (which disables
        the action buttons) is only cleared by an arriving event."""
        def worker():
            try:
                fn()
            except Exception as e:  # noqa: BLE001
                self._on_engine_event("", "error", f"{label} failed: {e}", "ERROR")
        threading.Thread(target=worker, name=f"sincrogit-{label}", daemon=True).start()

    def sync_now(self):
        # A tray menu action with no visible acknowledgment reads as "nothing
        # happened" — the work runs on a worker and only lands in the Log later.
        # A balloon confirms the click; the per-repo results follow as events.
        self._tray_ack("Syncing all repos", "Fetching and pushing in the background…")
        self._run_async(self.engine.sync_all_now, "sync")

    def seal_now(self):
        self._tray_ack("Sealing all repos", "Committing and pushing in the background…")
        self._run_async(self.engine.seal_all_now, "seal")

    def _tray_ack(self, title, body):
        """Immediate acknowledgment of a tray-menu action (the work itself is
        async and reports via events). Best-effort: a platform without balloon
        support just skips it."""
        try:
            self.tray.showMessage(title, body, QSystemTrayIcon.Information, 3000)
        except Exception:  # noqa: BLE001 — feedback is best-effort
            pass

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
                self._on_engine_event(name, "seal", f"not sealed: {msg}", "WARNING")
            elif msg != "sealed":
                self._on_engine_event(name, "seal", msg, "INFO")  # "nothing to seal"
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
            self._on_engine_event(name, "pull", msg if ok else f"not pulled: {msg}",
                                  "INFO" if ok else "WARNING")
        self._run_async(work, f"pull:{name}")

    def apply_handoff(self, name):
        """Apply a pending cross-machine handoff ('ask' mode, one click). Runs on a
        thread (it fetches + git); success is notified by the engine, failures are
        surfaced in the log."""
        def worker():
            ok, msg = self.engine.apply_handoff(name)
            if not ok:
                self._on_engine_event(name, "handoff", f"apply failed: {msg}", "WARNING")
            self.bridge.refresh_tray.emit()  # queued to the GUI thread
        threading.Thread(target=worker, name=f"sincrogit-handoff-{name}", daemon=True).start()

    def fetch_repo_settings(self, path, remote="origin"):
        """The per-repo OPTIONS this user published for `path` from another
        machine, or None when there are none (no remote, nothing published, or
        an error). Runs off the GUI thread (the Add-repo dialog calls it on its
        worker) — it does a network fetch of a single side ref."""
        from ..gitrepo import GitError, GitRepo
        from ..config import parse_published_overrides
        try:
            repo = GitRepo(os.path.abspath(path))
            if not repo.remote_url(remote):
                return None
            text = repo.fetch_published_config(remote, repo.sincro_user())
            if not text:
                return None
            overrides = parse_published_overrides(text)
            return overrides or None
        except (GitError, OSError):
            return None

    def add_repo(self, path, branch="main", push=True, pull=True,
                 normalize_eol=True, overrides=None):
        """Validate, persist to the config file, and add the repo live. (ok, msg).

        If `normalize_eol` and the repo has no .gitattributes, a '* text=auto' one
        is created so line endings stay consistent across machines. `overrides`
        (inherited from another machine's published config) are written as this
        repo's per-repo option overrides.
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
        # Inherited options from another machine come off a remote-controlled ref
        # (namespaced only by git-email), so sanitize the VALUES before they touch
        # config.yaml — a malformed one would otherwise fail the load for every
        # repo. push/pull are dropped: the dialog's own checkboxes decide those, an
        # inherited value must not silently override the user's explicit choice.
        if overrides:
            from ..config import safe_published_overrides
            clean, dropped = safe_published_overrides(overrides)
            for k in ("push", "pull"):
                clean.pop(k, None)
            entry.update(clean)
            if dropped:
                logging.getLogger("sincrogit").warning(
                    "add_repo: ignored %d unsafe inherited setting(s): %s",
                    len(dropped), ", ".join(sorted(dropped)))
        # Never write an entry that wouldn't load (same guard as update_repo):
        # validate the merged entry first, so a bad value can't brick the config.
        try:
            _validate_entry(entry, {})
        except (ValueError, TypeError) as e:
            return False, f"Invalid repo settings: {e}"
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
        # add_repo now runs on the dialog's worker thread (git on a slow/network
        # drive mustn't freeze Qt), so the tray refresh must hop to the GUI thread.
        self.bridge.refresh_tray.emit()
        return ok, msg

    def detect_branch(self, path):
        """The current branch of the git repo at `path`, or None (detached HEAD
        reports 'HEAD'; not-a-repo / errors report None). Exposed so the Add-repo
        dialog can prefill the branch WITHOUT importing gitrepo, and can run this
        git call off the GUI thread (a network/AV-slow drive would freeze Qt)."""
        from ..gitrepo import GitRepo
        try:
            return GitRepo(os.path.abspath(path)).current_branch()
        except Exception:  # noqa: BLE001 — the dialog's hint covers a failure
            return None

    def detect_remote(self, path, remote="origin"):
        """The URL of `remote` on the repo at `path`, or None. Lets the Add-repo
        dialog pre-fill the field and skip onboarding when a remote already
        exists. Runs off the GUI thread (see detect_branch)."""
        from ..gitrepo import GitRepo
        try:
            return GitRepo(os.path.abspath(path)).remote_url(remote)
        except Exception:  # noqa: BLE001 — treated as "no remote"
            return None

    def configure_remote(self, path, url, branch="main", remote="origin"):
        """Set `remote` to `url` and verify it end to end, WITHOUT adding the
        repo. Returns (ok, msg): read reachability first (ls-remote), then push
        auth (a --dry-run that transfers nothing). The same two checks --doctor
        runs, so 'passes here' means push/pull/autosnap will actually work.
        Runs on the dialog's worker thread (network calls must not freeze Qt)."""
        from ..gitrepo import GitError, GitRepo
        url = (url or "").strip()
        if not url:
            return False, "Enter the remote URL first."
        repo = GitRepo(os.path.abspath(path))
        try:
            repo.set_remote(remote, url)
        except GitError as e:
            return False, f"git rejected that URL: {e}"
        ok, detail = repo.ls_remote_heads(remote, timeout=30)
        if not ok:
            return False, (f"Can't reach the remote (check the URL and your "
                           f"access): {detail}")
        ok, detail = repo.push_dry_run(remote, branch or "main", timeout=30)
        if not ok:
            return False, (f"Reachable, but a test push was rejected (check "
                           f"write access / credentials): {detail}")
        return True, "Remote reachable and push access confirmed."

    # ---- per-repo configuration (Properties dialog) ----
    def repo_config_view(self, name):
        """(entry, effective, defaults) for the Properties dialog: `entry` is
        the repo's RAW config entry (explicit keys only), `effective` the
        values the engine runs with (entry merged over defaults), `defaults`
        what a repo WITHOUT overrides would get — the global `defaults:`
        section resolved through RepoConfig, so sentinels normalize and every
        field carries a value. ({}, {}, {}) if the repo isn't found.

        All three are plain dicts of RepoConfig fields (dataclasses.asdict /
        introspection), so a new field shows up here automatically — no mirror
        list of field names to keep in sync."""
        import dataclasses

        from ..config import _INHERITABLE, RepoConfig, find_repo_entry
        try:
            entry = find_repo_entry(self.config_path, name) or {}
        except (OSError, yaml.YAMLError):
            entry = {}
        effective = self.engine.repo_config_view(name)
        defaults = {}
        try:
            data = yaml.safe_load(self.config_text()) or {}
            d = data.get("defaults") or {} if isinstance(data, dict) else {}
            rc = RepoConfig(path="", name="", **{
                k: v for k, v in d.items() if k in _INHERITABLE})
            defaults = dataclasses.asdict(rc)
        except Exception:  # noqa: BLE001 — no defaults just means no hints
            pass
        return entry, (effective or {}), defaults

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

    def reset_repo_config(self, name):
        """Drop every inheritable override from the repo's entry — back to pure
        inheritance of the global defaults. (ok, msg). Applies on restart."""
        from ..config import reset_repo_overrides
        try:
            return reset_repo_overrides(self.config_path, name)
        except (OSError, yaml.YAMLError) as e:
            return False, str(e)

    # ---- start at login (registry, per machine — NOT part of config.yaml) ----
    def autostart_enabled(self):
        """(enabled, why_not): why_not is None when the toggle is usable, else
        the reason it's disabled (non-Windows)."""
        if not autostart.supported():
            return False, "Start at login is only available on Windows."
        return autostart.is_enabled(), None

    def set_autostart(self, enabled: bool):
        """(ok, msg). Applies immediately — no restart involved."""
        return autostart.set_autostart(enabled, self.config_path)

    # ---- file history / restore ----
    def repo_list(self):
        return [(r["name"], r["path"]) for r in self.engine.status()["repos"]]

    def file_history(self, name, relpath):
        return self.engine.file_history(name, relpath)

    def repo_history(self, name, limit=200):
        """The repo's whole-tree version timeline (Time Machine). Blocking (git
        log/reflog): the dialog runs it off the GUI thread."""
        return self.engine.repo_history(name, limit)

    def snapshot_timeline(self, name, limit=200):
        """Per-snapshot change lists for the Timeline tab. Blocking (git log
        walks): the tab runs it off the GUI thread."""
        return self.engine.snapshot_timeline(name, limit)

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
        """This machine's name as used in its autosnap refs. Via the engine so
        the GUI never imports gitrepo internals."""
        return self.engine.host_name()

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

    def file_hunks(self, name, relpath, sha):
        """Changed blocks between a file's version at `sha` and the working
        tree, for a partial restore. Blocking (git): the dialog runs it off
        the GUI thread."""
        return self.engine.file_hunks(name, relpath, sha)

    def restore_hunks(self, name, relpath, sha, selected, base):
        """Restore only the selected hunks of a file to its state at `sha`.
        Blocking: the dialog runs it off the GUI thread."""
        return self.engine.restore_hunks(name, relpath, sha, selected, base)

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
            data = yaml.safe_load(text)  # validate it's valid YAML before writing
        except yaml.YAMLError as e:
            return False, f"Invalid YAML: {e}"
        if data is not None and not isinstance(data, dict):
            return False, "Invalid config: the top level must be a mapping (key: value)"
        try:
            atomic_write_text(self.config_path, text)
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
