"""SincroGit tray app (PyQt5).

Threading architecture:
  - Main thread: the Qt loop (tray + windows).
  - Background thread (daemon): engine.run() (the snapshot/seal/sync cycle).
  - The engine emits events via a callback that: (a) stores them in the EventLog
    and (b) emits a Qt signal -> the GUI updates on its thread, without blocking.

Manual actions (Sync/Seal now) run on a separate thread so as not to freeze the
interface; the engine serializes them with its _oplock.
"""

import os
import sys
import threading

import yaml
from PyQt5.QtCore import QObject, QTimer, pyqtSignal
from PyQt5.QtWidgets import QApplication, QMenu, QSystemTrayIcon

from ..config import append_repo, load_config
from ..engine import Engine
from ..events import EventLog
from ..log import setup_logging
from . import icon as iconmod
from .control_panel import ControlPanel


class _Bridge(QObject):
    """Thread-safe bridge: background threads emit; the GUI receives."""
    event_added = pyqtSignal(object)
    activate = pyqtSignal()  # a second launch asks us to show the panel


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

        self.bridge = _Bridge()
        self.bridge.event_added.connect(self._on_event_gui)
        self.bridge.activate.connect(self.show_panel)

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

        # First run (config just created): open the panel on the Config tab.
        if open_config:
            self.show_panel()
            self.panel.select_config_tab()

    def _start_activation_listener(self):
        if not self._lock_socket:
            return

        def loop():
            while True:
                try:
                    conn, _ = self._lock_socket.accept()
                except OSError:
                    break  # socket closed on quit
                try:
                    conn.recv(16)
                    conn.close()
                except OSError:
                    pass
                self.bridge.activate.emit()

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
        self.tray.setToolTip("SincroGit")
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
        self.engine.stop()
        if self._engine_thread:
            self._engine_thread.join(timeout=15)
        self._release_lock()
        self.tray.hide()
        self.qapp.quit()

    def restart(self):
        """Relaunches the process to apply the new config."""
        self.engine.stop()
        if self._engine_thread:
            self._engine_thread.join(timeout=15)
        self._release_lock()  # free the single-instance port before re-launching
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

    def resume_repo(self, name) -> bool:
        ok = self.engine.resume_repo(name)
        self._refresh_tray()
        return ok

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
        self._run_async(lambda: self.engine.seal_repo_now(name), f"seal:{name}")

    def pull_repo_now(self, name):
        self._run_async(lambda: self.engine.pull_repo_now(name), f"pull:{name}")

    def add_repo(self, path, branch="main", push=True, pull=True):
        """Validate, persist to the config file, and add the repo live. (ok, msg)."""
        from ..gitrepo import GitRepo
        path = os.path.abspath(path)
        if not GitRepo(path).is_git_repo():
            return False, "The selected folder is not a git repository."
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
        self._refresh_tray()
        return ok, msg

    # ---- file history / restore ----
    def repo_list(self):
        return [(r["name"], r["path"]) for r in self.engine.status()["repos"]]

    def file_history(self, name, relpath):
        return self.engine.file_history(name, relpath)

    def file_content_at(self, name, relpath, sha):
        return self.engine.file_content_at(name, relpath, sha)

    def restore_file(self, name, relpath, sha):
        return self.engine.restore_file(name, relpath, sha)

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
    app = TrayApp(config_path, lock_socket=lock_socket, open_config=open_config)
    if not QSystemTrayIcon.isSystemTrayAvailable():
        app.logger.warning("No system tray available.")
    return app.run()
