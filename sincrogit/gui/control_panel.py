"""SincroGit control panel (PyQt5).

Tabbed window:
  - Status: repos table + actions (pause/resume/sync/seal).
  - Log: events filterable by repo, action, level and text.
  - Configuration: config.yaml editor (save / save and restart).
  - About.

It talks to the app through a `controller` (duck-typed) that exposes:
  status(), events_all(), pause_all(), resume_all(), sync_now(), seal_now(),
  resume_repo(name), save_config(text)->(ok,msg), restart(), config_path,
  config_text(), app_state(), make_icon(state).
"""

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
from .smart_commit_dialog import SmartCommitDialog

_LEVEL_COLOR = {
    "WARNING": QColor("#8a6d00"),
    "ERROR": QColor("#b00020"),
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
        self.setWindowTitle("SincroGit — Control panel")
        self.resize(880, 560)
        try:
            self.setWindowIcon(self.c.make_icon(self.c.app_state()))
        except Exception:
            pass

        self.tabs = QTabWidget()
        self.setCentralWidget(self.tabs)
        self.tabs.addTab(self._build_status_tab(), "Status")
        self.tabs.addTab(self._build_log_tab(), "Log")
        self.tabs.addTab(self._build_config_tab(), "Configuration")
        self.tabs.addTab(self._build_about_tab(), "About")

        # Periodic status refresh while the window is visible.
        self._timer = QTimer(self)
        self._timer.setInterval(2000)
        self._timer.timeout.connect(self.refresh_status)

        self.refresh_status()
        self.refresh_log()

    # =============================================================== STATUS
    _COLS = ["Repo", "Branch", "State", "Since last seal", "Last action", "Actions"]

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
        btn_history.clicked.connect(self._open_history)
        btn_add = QPushButton("Add repo…")
        btn_add.clicked.connect(self._open_add_repo)
        self.btn_pause = QPushButton("Pause all")
        self.btn_pause.clicked.connect(self._toggle_pause)
        for b in (btn_history, btn_add, self.btn_pause):
            top.addWidget(b)
        v.addLayout(top)

        self.tbl_repos = QTableWidget(0, len(self._COLS))
        self.tbl_repos.setHorizontalHeaderLabels(self._COLS)
        hdr = self.tbl_repos.horizontalHeader()
        hdr.setSectionResizeMode(0, QHeaderView.Stretch)        # Repo
        for col in range(1, len(self._COLS)):
            hdr.setSectionResizeMode(col, QHeaderView.ResizeToContents)
        self.tbl_repos.setSelectionBehavior(QTableWidget.SelectRows)
        self.tbl_repos.setEditTriggers(QTableWidget.NoEditTriggers)
        self.tbl_repos.verticalHeader().setVisible(False)
        v.addWidget(self.tbl_repos)

        self._row_widgets = {}   # repo name -> {"pause": QPushButton}
        self._shown_names = []   # cached row order
        return w

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

    def _rebuild_rows(self, repos):
        self._row_widgets = {}
        self.tbl_repos.setRowCount(len(repos))
        for i, r in enumerate(repos):
            for col in range(5):
                self.tbl_repos.setItem(i, col, QTableWidgetItem(""))
            name = r["name"]
            cell = QWidget()
            h = QHBoxLayout(cell)
            h.setContentsMargins(2, 1, 2, 1)
            h.setSpacing(4)
            b_pause = QPushButton("Pause")
            b_pause.clicked.connect(lambda _, n=name: self._toggle_repo_pause(n))
            b_commit = QPushButton("Commit…")
            b_commit.setToolTip("Manual commit now with an AI-proposed Conventional Commits message")
            b_commit.clicked.connect(lambda _, n=name: self._open_smart_commit(n))
            b_seal = QPushButton("Seal+Push")
            b_seal.clicked.connect(lambda _, n=name: self.c.seal_repo_now(n))
            b_pull = QPushButton("Fetch+Pull")
            b_pull.clicked.connect(lambda _, n=name: self.c.pull_repo_now(n))
            for b in (b_pause, b_commit, b_seal, b_pull):
                b.setFixedHeight(24)
                h.addWidget(b)
            self.tbl_repos.setCellWidget(i, 5, cell)
            self._row_widgets[name] = {"pause": b_pause}

    def _update_rows(self, repos, global_paused):
        for i, r in enumerate(repos):
            if r["conflict_paused"]:
                state, color = "Conflict", "#D23F3F"
            elif r.get("off_branch"):
                state, color = "Off-branch", "#8a6d00"
            elif r["user_paused"]:
                state, color = "Paused", "#8a6d00"
            elif global_paused:
                state, color = "Paused (all)", "#8a6d00"
            else:
                state, color = "Active", None
            cells = [r["name"], r["branch"] or "—", state,
                     _humanize_since(r["last_seal"]), r["last_action"] or "—"]
            for col, text in enumerate(cells):
                item = self.tbl_repos.item(i, col)
                if item is None:
                    continue
                item.setText(text)
                if col == 2:
                    item.setForeground(QColor(color) if color else QColor())
            w = self._row_widgets.get(r["name"])
            if w:
                paused = r["user_paused"] or r["conflict_paused"]
                w["pause"].setText("Resume" if paused else "Pause")

    def _toggle_repo_pause(self, name):
        r = {x["name"]: x for x in self.c.status()["repos"]}.get(name)
        if not r:
            return
        if r["user_paused"] or r["conflict_paused"]:
            self.c.resume_repo(name)
        else:
            self.c.pause_repo(name)
        self.refresh_status()

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
        self.cb_level.addItems(["(all)", "INFO", "WARNING", "ERROR"])
        self.cb_level.currentIndexChanged.connect(self.refresh_log)
        filt.addWidget(self.cb_level)

        filt.addWidget(QLabel("Text:"))
        self.ed_search = QLineEdit()
        self.ed_search.setPlaceholderText("filter by message…")
        self.ed_search.textChanged.connect(self.refresh_log)
        filt.addWidget(self.ed_search, 1)

        btn_refresh = QPushButton("Refresh")
        btn_refresh.clicked.connect(self.refresh_log)
        filt.addWidget(btn_refresh)
        v.addLayout(filt)

        self.tbl_log = QTableWidget(0, 5)
        self.tbl_log.setHorizontalHeaderLabels(["Time", "Repo", "Action", "Level", "Message"])
        hdr = self.tbl_log.horizontalHeader()
        for col in range(4):
            hdr.setSectionResizeMode(col, QHeaderView.ResizeToContents)
        hdr.setSectionResizeMode(4, QHeaderView.Stretch)
        self.tbl_log.setEditTriggers(QTableWidget.NoEditTriggers)
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

    def refresh_log(self):
        events = self.c.events_all()

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

        filtered = [e for e in events if self._passes_filter(e)]
        self.tbl_log.setRowCount(len(filtered))
        for i, ev in enumerate(filtered):
            cells = [_fmt_time(ev.ts), ev.repo or "—", ev.action, ev.level, ev.message]
            for j, val in enumerate(cells):
                item = QTableWidgetItem(str(val))
                color = _LEVEL_COLOR.get(ev.level)
                if color:
                    item.setForeground(color)
                self.tbl_log.setItem(i, j, item)
        self.tbl_log.scrollToBottom()
        self.lbl_log_count.setText(f"{len(filtered)} event(s) shown of {len(events)} total")

    def append_event(self, ev):
        """Append a new event live if it passes the current filter (Qt signal)."""
        # If the repo isn't in the dropdown, add it.
        if ev.repo and self.cb_repo.findText(ev.repo) < 0:
            self.cb_repo.addItem(ev.repo)
        if not self._passes_filter(ev):
            return
        r = self.tbl_log.rowCount()
        self.tbl_log.insertRow(r)
        cells = [_fmt_time(ev.ts), ev.repo or "—", ev.action, ev.level, ev.message]
        for j, val in enumerate(cells):
            item = QTableWidgetItem(str(val))
            color = _LEVEL_COLOR.get(ev.level)
            if color:
                item.setForeground(color)
            self.tbl_log.setItem(r, j, item)
        self.tbl_log.scrollToBottom()

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

    # ================================================================= ABOUT
    def _build_about_tab(self) -> QWidget:
        w = QWidget()
        v = QVBoxLayout(w)
        v.addStretch(1)
        icon_lbl = QLabel()
        try:
            icon_lbl.setPixmap(self.c.make_icon(self.c.app_state()).pixmap(96, 96))
        except Exception:
            pass
        icon_lbl.setAlignment(Qt.AlignCenter)
        v.addWidget(icon_lbl)

        title = QLabel(f"SincroGit v{__version__}")
        tf = QFont()
        tf.setPointSize(16)
        tf.setBold(True)
        title.setFont(tf)
        title.setAlignment(Qt.AlignCenter)
        v.addWidget(title)

        sub = QLabel("Automatic synchronization with robust Git versioning.")
        sub.setAlignment(Qt.AlignCenter)
        v.addWidget(sub)
        v.addStretch(2)
        return w

    def select_config_tab(self):
        for i in range(self.tabs.count()):
            if self.tabs.tabText(i) == "Configuration":
                self.tabs.setCurrentIndex(i)
                return

    # ================================================================ window
    def showEvent(self, e):
        super().showEvent(e)
        self.refresh_status()
        self.refresh_log()
        self._timer.start()

    def hideEvent(self, e):
        super().hideEvent(e)
        self._timer.stop()

    def closeEvent(self, e):
        # Closing the window only hides it; the app stays in the tray.
        e.ignore()
        self.hide()
