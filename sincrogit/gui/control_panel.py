"""SincroGit control panel (PyQt5).

Tabbed window:
  - Status: repos table + an action bar for the selected repo.
  - Log: events filterable by repo, action, level and text.
  - Settings: friendly form over the global defaults.
  - Advanced (YAML): raw config.yaml editor (save / save and restart).

It talks to the app through a `controller` (duck-typed) that exposes:
  status(), events_all(), pause_all(), resume_all(), sync_now(), seal_now(),
  resume_repo(name), apply_handoff(name), save_config(text)->(ok,msg), restart(),
  config_path, config_text(), app_state(), make_icon(state).
"""

import os
import time
from datetime import datetime

from PyQt5.QtCore import Qt, QTimer
from PyQt5.QtGui import QColor, QFont
from PyQt5.QtWidgets import (
    QComboBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QTabWidget,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .. import __version__
from ..events import ACTIONS
from .add_repo_dialog import AddRepoDialog
from .history_dialog import HistoryDialog
from .machines_dialog import MachinesDialog
from .repo_properties_dialog import RepoPropertiesDialog
from .settings_tab import SettingsTab
from .smart_commit_dialog import SmartCommitDialog
from .time_machine_dialog import TimeMachineDialog

_LEVEL_COLOR = {
    "DEBUG": QColor("#8a929c"),    # muted: high-volume detail (filtered files, ...)
    "WARNING": QColor("#8a6d00"),
    "ERROR": QColor("#b00020"),
}

# Canonical per-repo states (Engine.status()["state"]) -> (label, color).
# The precedence between the underlying flags lives in the engine — this map
# is presentation only. `None` color = inherit the palette's normal text.
_STATE_STYLE = {
    "conflict":   ("Conflict", "#D23F3F"),
    "off-branch": ("Off-branch", "#8a6d00"),
    "paused":     ("Paused", "#8a6d00"),
    "busy":       ("Busy (merge/rebase)", "#8a6d00"),
    "handoff":    ("Handoff ready", "#1E6FD9"),  # label gets the peer host appended
    "active":     ("Active", None),
}


def _humanize_since(epoch) -> str:
    if not epoch:
        return "—"
    secs = max(0, int(time.time() - epoch))
    if secs < 60:
        return f"{secs}s"
    mins = secs // 60
    if mins < 60:
        return f"{mins}m"
    hrs, mins = divmod(mins, 60)
    if hrs < 24:
        return f"{hrs}h {mins}m"
    days, hrs = divmod(hrs, 24)
    return f"{days}d {hrs}h"


def _handoff_tooltip(r: dict) -> str:
    """Explain a pending handoff in plain words (host, age, what Apply does)."""
    host = r.get("pending_handoff") or "another machine"
    epoch = r.get("pending_handoff_epoch")
    age = f" (from {_humanize_since(epoch)} ago)" if epoch else ""
    return (
        f"'{host}' has newer work on this repo{age}.\n"
        f"Applying fast-forwards your files to that state — loss-free: it's "
        f"re-validated on apply, and your current state stays recoverable "
        f"via File history."
    )


def _fmt_time(epoch) -> str:
    if not epoch:
        return "—"
    try:
        return datetime.fromtimestamp(epoch).strftime("%H:%M:%S")
    except (ValueError, OSError):
        return "—"


class ControlPanel(QMainWindow):
    def __init__(self, controller):
        super().__init__()
        self.c = controller
        # In-memory event cache for the Log tab: the JSONL file is read ONCE per
        # open/Refresh (reload_log); filter changes only re-filter this list, so
        # typing in the search box never re-reads the file from disk.
        self._events_cache = []
        self.setWindowTitle(f"⏳g SincroGit v{__version__} — Control panel")
        self.resize(880, 560)
        try:
            self.setWindowIcon(self.c.make_icon(self.c.app_state()))
        except Exception:
            pass

        self.tabs = QTabWidget()
        self.setCentralWidget(self.tabs)
        self.tabs.addTab(self._build_status_tab(), "Status")
        self.tabs.addTab(self._build_log_tab(), "Log")
        self.tabs.addTab(SettingsTab(self.c), "Settings")
        self.tabs.addTab(self._build_config_tab(), "Advanced (YAML)")

        # Periodic status refresh while the window is visible.
        self._timer = QTimer(self)
        self._timer.setInterval(2000)
        self._timer.timeout.connect(self.refresh_status)

        self.refresh_status()
        self.reload_log()

    # =============================================================== STATUS
    _COLS = ["Repo", "Branch", "State", "Since last seal", "Last action"]

    def _build_status_tab(self) -> QWidget:
        w = QWidget()
        v = QVBoxLayout(w)

        top = QHBoxLayout()
        self.lbl_state = QLabel()
        f = QFont()
        f.setBold(True)
        self.lbl_state.setFont(f)
        top.addWidget(self.lbl_state, 1)
        btn_history = QPushButton("File history…")
        btn_history.setToolTip("Pick a FILE and browse its versions")
        btn_history.clicked.connect(self._open_history)
        btn_time = QPushButton("Time machine…")
        btn_time.setToolTip("Pick a VERSION, see every file that differs, and "
                            "restore a selected set")
        btn_time.clicked.connect(self._open_time_machine)
        btn_machines = QPushButton("Machines…")
        btn_machines.setToolTip("Each machine's last autosnap mirror — your "
                                "recovery points, and who's gone stale")
        btn_machines.clicked.connect(self._open_machines)
        btn_add = QPushButton("Add repo…")
        btn_add.clicked.connect(self._open_add_repo)
        self.btn_pause = QPushButton("Pause all")
        self.btn_pause.clicked.connect(self._toggle_pause)
        for b in (btn_history, btn_time, btn_machines, btn_add, self.btn_pause):
            top.addWidget(b)
        v.addLayout(top)

        # Clean table: per-repo ACTIONS live in the bar below and act on the
        # SELECTED row (buttons crammed into cells overflowed the viewport).
        self.tbl_repos = QTableWidget(0, len(self._COLS))
        self.tbl_repos.setHorizontalHeaderLabels(self._COLS)
        hdr = self.tbl_repos.horizontalHeader()
        hdr.setSectionResizeMode(0, QHeaderView.Stretch)        # Repo
        for col in range(1, len(self._COLS)):
            hdr.setSectionResizeMode(col, QHeaderView.ResizeToContents)
        self.tbl_repos.setSelectionBehavior(QTableWidget.SelectRows)
        self.tbl_repos.setSelectionMode(QTableWidget.SingleSelection)
        self.tbl_repos.setEditTriggers(QTableWidget.NoEditTriggers)
        self.tbl_repos.verticalHeader().setVisible(False)
        self.tbl_repos.setAlternatingRowColors(True)
        self.tbl_repos.setShowGrid(False)
        self.tbl_repos.verticalHeader().setDefaultSectionSize(34)  # row breathing room
        self.tbl_repos.itemSelectionChanged.connect(self._sync_action_bar)
        self.tbl_repos.doubleClicked.connect(lambda _i: self._open_history())
        self.tbl_repos.setContextMenuPolicy(Qt.CustomContextMenu)
        self.tbl_repos.customContextMenuRequested.connect(self._repo_context_menu)
        v.addWidget(self.tbl_repos)

        # --- Action bar for the selected repo ---
        bar = QHBoxLayout()
        self.lbl_selected = QLabel("Select a repo")
        self.lbl_selected.setProperty("cssClass", "muted")
        bar.addWidget(self.lbl_selected, 1)
        self.b_handoff = QPushButton("Apply handoff")
        self.b_handoff.setProperty("cssClass", "accent")
        self.b_handoff.setToolTip("Apply newer work waiting from your other machine")
        self.b_handoff.clicked.connect(lambda: self._apply_handoff(self._selected_repo()))
        self.b_handoff.setVisible(False)
        self.b_fix = QPushButton("How to fix…")
        self.b_fix.setProperty("cssClass", "accent")
        self.b_fix.setToolTip("Why this repo is paused on a conflict, and how to resolve it")
        self.b_fix.clicked.connect(lambda: self._show_conflict_help(self._selected_repo()))
        self.b_fix.setVisible(False)
        self.b_props = QPushButton("Properties…")
        self.b_props.setToolTip("This repo's settings (branch, rhythms, sync, filters) "
                                "— a form instead of the YAML")
        self.b_props.clicked.connect(lambda: self._open_repo_properties(self._selected_repo()))
        self.b_pause_repo = QPushButton("Pause")
        self.b_pause_repo.clicked.connect(lambda: self._toggle_repo_pause(self._selected_repo()))
        self.b_commit = QPushButton("Commit…")
        self.b_commit.setToolTip("Manual commit now with an AI-proposed Conventional Commits message")
        self.b_commit.clicked.connect(lambda: self._open_smart_commit(self._selected_repo()))
        self.b_seal = QPushButton("Seal+Push")
        self.b_seal.setToolTip("Turn the current WIP into a permanent commit and push it")
        self.b_seal.clicked.connect(
            lambda: self._start_repo_action(self._selected_repo(), self.c.seal_repo_now))
        self.b_pull = QPushButton("Fetch+Pull")
        self.b_pull.setToolTip("Fetch the remote and rebase the WIP on top now")
        self.b_pull.clicked.connect(
            lambda: self._start_repo_action(self._selected_repo(), self.c.pull_repo_now))
        for b in (self.b_handoff, self.b_fix, self.b_props, self.b_pause_repo,
                  self.b_commit, self.b_seal, self.b_pull):
            bar.addWidget(b)
        v.addLayout(bar)

        # One-line activity digest (today's counts). A full statistics tab would
        # mostly duplicate the Log; the non-redundant part is this aggregation.
        self.lbl_digest = QLabel("")
        self.lbl_digest.setProperty("cssClass", "muted")
        v.addWidget(self.lbl_digest)

        self._shown_names = []   # cached row order
        self._inflight = {}      # repo -> monotonic start of a manual Seal/Pull
        return w

    def _selected_repo(self) -> str:
        row = self.tbl_repos.currentRow()
        item = self.tbl_repos.item(row, 0) if row >= 0 else None
        return item.text() if item else ""

    # Fallback for the in-flight marker if no completion event ever arrives
    # (the git network timeout is 60 s, so 90 s means something went wrong).
    _INFLIGHT_TIMEOUT = 90

    def _action_inflight(self, name: str) -> bool:
        t = self._inflight.get(name)
        if t is None:
            return False
        if time.monotonic() - t > self._INFLIGHT_TIMEOUT:
            self._inflight.pop(name, None)
            return False
        return True

    def _start_repo_action(self, name: str, fn):
        """Launch a manual Seal/Pull and mark it in flight: the buttons disable
        and the bar says 'working…' until its completion event (or a timeout)
        clears the marker — no double-dispatch, no dead-button look."""
        if not name:
            return
        self._inflight[name] = time.monotonic()
        fn(name)
        self._sync_action_bar()

    def _sync_action_bar(self):
        """Point the action bar at the selected repo (enabled state, pause text,
        handoff/conflict visibility, working feedback)."""
        name = self._selected_repo()
        repos = {r["name"]: r for r in self.c.status()["repos"]}
        r = repos.get(name)
        has = r is not None
        for b in (self.b_props, self.b_pause_repo, self.b_commit, self.b_seal, self.b_pull):
            b.setEnabled(has)
        if not has:
            self.lbl_selected.setText("Select a repo")
            self.b_handoff.setVisible(False)
            self.b_fix.setVisible(False)
            return
        paused = r["user_paused"] or r["conflict_paused"]
        self.b_pause_repo.setText("Resume" if paused else "Pause")
        self.b_handoff.setVisible(bool(r.get("pending_handoff")))
        if r.get("pending_handoff"):
            self.b_handoff.setToolTip(_handoff_tooltip(r))
        self.b_fix.setVisible(bool(r["conflict_paused"]))
        # 'working': an engine network worker holds the repo, or a manual
        # Seal/Pull is still in flight.
        working = bool(r.get("net_busy")) or self._action_inflight(name)
        self.b_seal.setEnabled(not working)
        self.b_pull.setEnabled(not working)
        self.lbl_selected.setText(
            f"Selected:  {name}" + ("  —  working…" if working else ""))

    def refresh_status(self):
        st = self.c.status()
        repos = st["repos"]
        global_paused = bool(st.get("paused"))

        if not st.get("running", True):
            self.lbl_state.setText("● Stopped"); self.lbl_state.setStyleSheet("color:#7A7F87;")
        elif global_paused:
            self.lbl_state.setText("● Paused (all)"); self.lbl_state.setStyleSheet("color:#E0A400;")
        elif any(r["conflict_paused"] for r in repos):
            self.lbl_state.setText("● Conflict — a repo needs attention"); self.lbl_state.setStyleSheet("color:#D23F3F;")
        elif not repos:
            self.lbl_state.setText("● No repos yet — use “Add repo…”"); self.lbl_state.setStyleSheet("color:#7A7F87;")
        else:
            self.lbl_state.setText("● Active"); self.lbl_state.setStyleSheet("color:#2E9E5B;")
        self.btn_pause.setText("Resume all" if global_paused else "Pause all")

        names = [r["name"] for r in repos]
        if names != self._shown_names:
            self._rebuild_rows(repos)
            self._shown_names = names
        self._update_rows(repos, global_paused)
        self._update_digest()

    _DIGEST_ACTIONS = ("snapshot", "seal", "push", "pull", "handoff")

    def _update_digest(self):
        """Today's activity in one line, aggregated from the event cache."""
        midnight = datetime.now().replace(hour=0, minute=0, second=0,
                                          microsecond=0).timestamp()
        counts = {}
        for ev in getattr(self, "_events_cache", []):
            if ev.ts >= midnight and ev.action in self._DIGEST_ACTIONS:
                counts[ev.action] = counts.get(ev.action, 0) + 1
        self.lbl_digest.setText(
            "Today:  " + "   ·   ".join(
                f"{counts[a]} {a}{'s' if counts[a] != 1 else ''}"
                for a in self._DIGEST_ACTIONS if a in counts)
            if counts else "")

    def _rebuild_rows(self, repos):
        selected = self._selected_repo()
        self.tbl_repos.setRowCount(len(repos))
        for i, r in enumerate(repos):
            for col in range(len(self._COLS)):
                self.tbl_repos.setItem(i, col, QTableWidgetItem(""))
        # Keep (or establish) a selection so the action bar always has a target.
        names = [r["name"] for r in repos]
        if repos:
            row = names.index(selected) if selected in names else 0
            self.tbl_repos.selectRow(row)

    # Hover explanations for the non-obvious states (the state cell's tooltip).
    _STATE_TIP = {
        "busy": "A manual git operation (merge/rebase) is holding this repo; "
                "snapshots resume when it finishes.",
        "off-branch": "HEAD is on another branch — autosync waits until you switch "
                      "back (or enable track_current_branch for this repo).",
    }

    def _update_rows(self, repos, global_paused):
        for i, r in enumerate(repos):
            s = r.get("state", "active")
            state, color = _STATE_STYLE.get(s, _STATE_STYLE["active"])
            if s == "handoff":
                state = f"{state}: {r['pending_handoff']}"
            if global_paused and s in ("active", "busy", "handoff"):
                # The tray's global pause outranks the non-blocked states; the
                # blocked ones (conflict/off-branch/paused) stay visible — they
                # still need the user's attention after a Resume all.
                state, color = "Paused (all)", "#8a6d00"
            if s == "conflict":
                tip = r.get("conflict_msg") or "Paused on a rebase conflict — see the Log."
            elif s == "handoff":
                tip = _handoff_tooltip(r)
            else:
                tip = self._STATE_TIP.get(s, "")
            cells = [r["name"], r["branch"] or "—", state,
                     _humanize_since(r["last_seal"]), r["last_action"] or "—"]
            for col, text in enumerate(cells):
                item = self.tbl_repos.item(i, col)
                if item is None:
                    continue
                item.setText(text)
                if col == 2:
                    item.setToolTip(tip)
                    if color:
                        item.setForeground(QColor(color))
                    else:
                        # Clear the role: an invalid QColor() would paint black,
                        # wrong under a non-default/dark palette.
                        item.setData(Qt.ForegroundRole, None)
        self._sync_action_bar()

    def _toggle_repo_pause(self, name):
        r = {x["name"]: x for x in self.c.status()["repos"]}.get(name)
        if not r:
            return
        if r["user_paused"] or r["conflict_paused"]:
            self.c.resume_repo(name)
        else:
            self.c.pause_repo(name)
        self.refresh_status()

    def _apply_handoff(self, name):
        r = {x["name"]: x for x in self.c.status()["repos"]}.get(name)
        if not r or not r.get("pending_handoff"):
            return
        host = r["pending_handoff"]
        epoch = r.get("pending_handoff_epoch")
        age = f"from {_humanize_since(epoch)} ago " if epoch else ""
        if QMessageBox.question(
            self, "Apply handoff",
            f"Apply the newer work {age}on '{host}' to '{name}'?\n\n"
            f"Your working tree fast-forwards to that machine's state. It's "
            f"loss-free: the move is re-validated on apply, and your current "
            f"state stays recoverable via File history.",
        ) != QMessageBox.Yes:
            return
        self.c.apply_handoff(name)
        self.refresh_status()

    def _show_conflict_help(self, name):
        """Why the repo is paused on a conflict, and what to do — without making
        the user reconstruct it from the Log."""
        r = {x["name"]: x for x in self.c.status()["repos"]}.get(name)
        if not r:
            return
        box = QMessageBox(self)
        box.setWindowTitle(f"Conflict — {name}")
        box.setIcon(QMessageBox.Warning)
        box.setText(r.get("conflict_msg") or "Paused on a rebase conflict.")
        box.setInformativeText(
            "SincroGit stays paused on this repo (nothing is snapshotted or "
            "synced) until you press Resume."
        )
        b_open = box.addButton("Open folder", QMessageBox.ActionRole)
        box.addButton(QMessageBox.Close)
        box.exec_()
        if box.clickedButton() is b_open:
            try:
                os.startfile(r["path"])
            except OSError:
                pass

    def _toggle_pause(self):
        if self.c.status().get("paused"):
            self.c.resume_all()
        else:
            self.c.pause_all()
        self.refresh_status()

    def _open_add_repo(self):
        dlg = AddRepoDialog(self.c, parent=self)
        if dlg.exec_():
            self.refresh_status()

    def _open_repo_properties(self, name):
        if not name:
            return
        dlg = RepoPropertiesDialog(self.c, name, parent=self)
        dlg.exec_()
        self.refresh_status()

    def _repo_context_menu(self, pos):
        name = self._selected_repo()
        r = {x["name"]: x for x in self.c.status()["repos"]}.get(name)
        if not r:
            return
        menu = QMenu(self)
        act_open = menu.addAction("Open folder")
        act_hist = menu.addAction("File history…")
        act_time = menu.addAction("Time machine…")
        act_props = menu.addAction("Properties…")
        chosen = menu.exec_(self.tbl_repos.viewport().mapToGlobal(pos))
        if chosen is act_open:
            try:
                os.startfile(r["path"])
            except OSError:
                pass
        elif chosen is act_hist:
            self._open_history()
        elif chosen is act_time:
            self._open_time_machine()
        elif chosen is act_props:
            self._open_repo_properties(name)

    def _open_smart_commit(self, name):
        dlg = SmartCommitDialog(self.c, name, parent=self)
        if dlg.exec_():
            self.refresh_status()

    def _open_history(self):
        row = self.tbl_repos.currentRow()
        preselect = None
        if row >= 0 and self.tbl_repos.item(row, 0):
            preselect = self.tbl_repos.item(row, 0).text()
        dlg = HistoryDialog(self.c, parent=self, preselect_repo=preselect)
        dlg.exec_()

    def _open_time_machine(self):
        preselect = self._selected_repo() or None
        dlg = TimeMachineDialog(self.c, parent=self, preselect_repo=preselect)
        dlg.exec_()
        self.refresh_status()

    def _open_machines(self):
        MachinesDialog(self.c, parent=self).exec_()

    # ================================================================== LOG
    def _build_log_tab(self) -> QWidget:
        w = QWidget()
        v = QVBoxLayout(w)

        filt = QHBoxLayout()
        filt.addWidget(QLabel("Repo:"))
        self.cb_repo = QComboBox()
        self.cb_repo.currentIndexChanged.connect(self.refresh_log)
        filt.addWidget(self.cb_repo)

        filt.addWidget(QLabel("Action:"))
        self.cb_action = QComboBox()
        self.cb_action.addItem("(all)")
        self.cb_action.addItems(ACTIONS)
        self.cb_action.currentIndexChanged.connect(self.refresh_log)
        filt.addWidget(self.cb_action)

        filt.addWidget(QLabel("Level:"))
        self.cb_level = QComboBox()
        self.cb_level.addItems(["(all)", "DEBUG", "INFO", "WARNING", "ERROR"])
        self.cb_level.currentIndexChanged.connect(self.refresh_log)
        filt.addWidget(self.cb_level)

        filt.addWidget(QLabel("Text:"))
        self.ed_search = QLineEdit()
        self.ed_search.setPlaceholderText("filter by message…")
        self.ed_search.textChanged.connect(self.refresh_log)
        filt.addWidget(self.ed_search, 1)

        v.addLayout(filt)

        self.tbl_log = QTableWidget(0, 5)
        self.tbl_log.setHorizontalHeaderLabels(["Time", "Repo", "Action", "Level", "Message"])
        hdr = self.tbl_log.horizontalHeader()
        for col in range(4):
            hdr.setSectionResizeMode(col, QHeaderView.ResizeToContents)
        hdr.setSectionResizeMode(4, QHeaderView.Stretch)
        self.tbl_log.setEditTriggers(QTableWidget.NoEditTriggers)
        self.tbl_log.setAlternatingRowColors(True)
        self.tbl_log.setShowGrid(False)
        self.tbl_log.verticalHeader().setVisible(False)
        v.addWidget(self.tbl_log)

        self.lbl_log_count = QLabel()
        v.addWidget(self.lbl_log_count)
        return w

    def _passes_filter(self, ev) -> bool:
        repo_sel = self.cb_repo.currentText()
        if repo_sel not in ("", "(all)") and ev.repo != repo_sel:
            return False
        act = self.cb_action.currentText()
        if act != "(all)" and ev.action != act:
            return False
        lvl = self.cb_level.currentText()
        if lvl != "(all)" and ev.level != lvl:
            return False
        txt = self.ed_search.text().strip().lower()
        if txt and txt not in ev.message.lower():
            return False
        return True

    def reload_log(self):
        """Re-read the full event history from disk (on open). After that the tab
        stays current by itself: every new event arrives live through the Qt
        signal (append_event), so there is no manual Refresh button. Filter
        changes go through refresh_log, which reuses the cache."""
        self._events_cache = list(self.c.events_all())
        self.refresh_log()

    def refresh_log(self):
        events = self._events_cache

        # Repopulate the repo dropdown, preserving the selection.
        repos = sorted({e.repo for e in events if e.repo})
        cur = self.cb_repo.currentText()
        self.cb_repo.blockSignals(True)
        self.cb_repo.clear()
        self.cb_repo.addItem("(all)")
        self.cb_repo.addItems(repos)
        idx = self.cb_repo.findText(cur)
        self.cb_repo.setCurrentIndex(idx if idx >= 0 else 0)
        self.cb_repo.blockSignals(False)

        # Newest first: the latest event is what you came to see.
        filtered = [e for e in reversed(events) if self._passes_filter(e)]
        self.tbl_log.setRowCount(len(filtered))
        for i, ev in enumerate(filtered):
            cells = [_fmt_time(ev.ts), ev.repo or "—", ev.action, ev.level, ev.message]
            for j, val in enumerate(cells):
                item = QTableWidgetItem(str(val))
                color = _LEVEL_COLOR.get(ev.level)
                if color:
                    item.setForeground(color)
                self.tbl_log.setItem(i, j, item)
        self.lbl_log_count.setText(f"{len(filtered)} event(s) shown of {len(events)} total")

    def append_event(self, ev):
        """Append a new event live if it passes the current filter (Qt signal)."""
        # A seal/pull/push/sync event for a repo marks its manual action as done:
        # clear the in-flight marker so the action bar re-enables its buttons.
        if ev.repo in self._inflight and ev.action in ("seal", "push", "pull", "sync"):
            self._inflight.pop(ev.repo, None)
            self._sync_action_bar()
        self._events_cache.append(ev)
        if len(self._events_cache) > 60_000:  # bound a very long session
            del self._events_cache[:20_000]
        # If the repo isn't in the dropdown, add it.
        if ev.repo and self.cb_repo.findText(ev.repo) < 0:
            self.cb_repo.addItem(ev.repo)
        if not self._passes_filter(ev):
            return
        self.tbl_log.insertRow(0)  # newest first
        cells = [_fmt_time(ev.ts), ev.repo or "—", ev.action, ev.level, ev.message]
        for j, val in enumerate(cells):
            item = QTableWidgetItem(str(val))
            color = _LEVEL_COLOR.get(ev.level)
            if color:
                item.setForeground(color)
            self.tbl_log.setItem(0, j, item)

    # =========================================================== CONFIGURATION
    def _build_config_tab(self) -> QWidget:
        w = QWidget()
        v = QVBoxLayout(w)
        v.addWidget(QLabel(f"File: {self.c.config_path}"))

        self.ed_config = QPlainTextEdit()
        self.ed_config.setFont(QFont("Consolas", 10))
        self.ed_config.setPlainText(self.c.config_text())
        v.addWidget(self.ed_config)

        row = QHBoxLayout()
        btn_reload_file = QPushButton("Reload from disk")
        btn_reload_file.clicked.connect(lambda: self.ed_config.setPlainText(self.c.config_text()))
        btn_save = QPushButton("Save")
        btn_save.clicked.connect(self._save_config)
        btn_save_restart = QPushButton("Save and restart")
        btn_save_restart.clicked.connect(self._save_and_restart)
        row.addWidget(btn_reload_file)
        row.addStretch(1)
        row.addWidget(btn_save)
        row.addWidget(btn_save_restart)
        v.addLayout(row)

        v.addWidget(QLabel(
            "Changes take effect when SincroGit restarts ('Save and restart')."
        ))
        return w

    def _save_config(self) -> bool:
        ok, msg = self.c.save_config(self.ed_config.toPlainText())
        if ok:
            QMessageBox.information(self, "Configuration", "Saved. Restart to apply.")
        else:
            QMessageBox.critical(self, "Error saving", msg)
        return ok

    def _save_and_restart(self):
        if self._save_config():
            self.c.restart()

    def select_config_tab(self):
        # First run lands the user on the friendly Settings form, not raw YAML.
        for i in range(self.tabs.count()):
            if self.tabs.tabText(i) == "Settings":
                self.tabs.setCurrentIndex(i)
                return

    # ================================================================ window
    def showEvent(self, e):
        super().showEvent(e)
        self.refresh_status()
        self.reload_log()
        self._timer.start()

    def hideEvent(self, e):
        super().hideEvent(e)
        self._timer.stop()

    def closeEvent(self, e):
        # Closing the window only hides it; the app stays in the tray.
        e.ignore()
        self.hide()
