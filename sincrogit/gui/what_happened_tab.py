"""What happened tab: the answer to "I was away — did anything move?".

The Log has every fact and is therefore useless for this question: five hundred
lines of `snapshot` bury the two that matter. This tab is the aggregation —
one line per repo (files, ±lines), then the milestones only (commits, marks,
handoffs, conflicts, pushes that failed).

The default period is the LAST ABSENCE, which the tray already computed when
the machine was unlocked (see TrayApp._report_absence); the other periods are
computed on demand. Same threading contract as every other tab: git and the
event history are read on a worker, delivered by a queued signal, guarded by a
generation token.
"""

import datetime
import threading
import time

from PyQt5.QtCore import Qt, pyqtSignal
from PyQt5.QtGui import QColor, QFont
from PyQt5.QtWidgets import (
    QComboBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .busy import BusyBar
from .time_machine_tab import _fmt

# Periods offered, in the order they appear. The value is either the special
# "absence" marker or the number of seconds to look back; "today" is midnight,
# which is a wall-clock question and not a duration.
PERIODS = (("Since you left (last absence)", "absence"),
           ("Today", "today"),
           ("Last 24 hours", 24 * 3600),
           ("Last 7 days", 7 * 86400))

# Milestone accents: the two that need the user to DO something stand out.
_ACTION_COLOR = {"conflict": "#d23f3f", "push": "#c2410c",
                 "handoff": "#1e6fd9", "mark": "#c2410c",
                 "seal": "#2e9e5b", "leave-seal": "#2e9e5b"}
_ACTION_WORD = {"seal": "commit", "leave-seal": "commit (on leaving)",
                "push": "push problem", "mark": "mark",
                "handoff": "handoff", "conflict": "conflict"}


def humanize_span(seconds: float) -> str:
    """A duration as a human says it ("2 h 40 m"). Used by the tray balloon
    too, so both places describe the same absence the same way."""
    secs = max(0, int(seconds or 0))
    if secs < 60:
        return f"{secs} s"
    mins = secs // 60
    if mins < 60:
        return f"{mins} min"
    hrs, mins = divmod(mins, 60)
    if hrs < 48:
        return f"{hrs} h {mins:02d} m"
    return f"{hrs // 24} days"


class WhatHappenedTab(QWidget):
    """Duck-typed controller: last_digest(), digest_since(since, until=None),
    theme."""

    _digest_ready = pyqtSignal(int, object)   # gen, digest

    def __init__(self, controller, parent=None):
        super().__init__(parent)
        self.c = controller
        self._pal = getattr(controller, "theme", None) or {}
        self._gen = 0
        self._loaded_once = False

        v = QVBoxLayout(self)
        v.setContentsMargins(8, 8, 8, 8)

        top = QHBoxLayout()
        top.addWidget(QLabel("Period:"))
        self.cb_period = QComboBox()
        for text, value in PERIODS:
            self.cb_period.addItem(text, value)
        self.cb_period.setToolTip(
            "Which stretch of time to summarize. 'Since you left' is the "
            "absence SincroGit reported when you came back.")
        self.cb_period.currentIndexChanged.connect(self._reload)
        top.addWidget(self.cb_period)
        self.btn_refresh = QPushButton("Refresh")
        self.btn_refresh.clicked.connect(self._reload)
        top.addWidget(self.btn_refresh)
        top.addStretch(1)
        v.addLayout(top)

        self.lbl_head = QLabel("")
        f = QFont()
        f.setBold(True)
        self.lbl_head.setFont(f)
        self.lbl_head.setWordWrap(True)
        v.addWidget(self.lbl_head)
        self.lbl_span = QLabel("")
        self.lbl_span.setProperty("cssClass", "muted")
        v.addWidget(self.lbl_span)

        self.tbl_repos = QTableWidget(0, 5)
        self.tbl_repos.setHorizontalHeaderLabels(
            ["Repo", "Files", "Lines", "Commits", "Marks"])
        self.tbl_repos.setToolTip(
            "One line per repo that moved. 'Files' counts distinct files "
            "touched; 'Lines' is what was added and removed.")
        hdr = self.tbl_repos.horizontalHeader()
        hdr.setSectionResizeMode(0, QHeaderView.Stretch)
        for col in range(1, 5):
            hdr.setSectionResizeMode(col, QHeaderView.ResizeToContents)
        self.tbl_repos.setSelectionBehavior(QTableWidget.SelectRows)
        self.tbl_repos.setEditTriggers(QTableWidget.NoEditTriggers)
        self.tbl_repos.verticalHeader().setVisible(False)
        self.tbl_repos.setShowGrid(False)
        v.addWidget(self.tbl_repos, 1)

        self.lbl_milestones = QLabel("")
        self.lbl_milestones.setProperty("cssClass", "muted")
        v.addWidget(self.lbl_milestones)
        self.tbl_events = QTableWidget(0, 3)
        self.tbl_events.setHorizontalHeaderLabels(["When", "Repo", "What"])
        self.tbl_events.setToolTip(
            "The things worth knowing about — commits, marks, handoffs, "
            "conflicts and pushes that failed. Routine snapshots are left out "
            "on purpose; the Log tab has every one of them.")
        ehdr = self.tbl_events.horizontalHeader()
        ehdr.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        ehdr.setSectionResizeMode(1, QHeaderView.ResizeToContents)
        ehdr.setSectionResizeMode(2, QHeaderView.Stretch)
        self.tbl_events.setEditTriggers(QTableWidget.NoEditTriggers)
        self.tbl_events.verticalHeader().setVisible(False)
        self.tbl_events.setShowGrid(False)
        v.addWidget(self.tbl_events, 1)

        self.busy = BusyBar()
        v.addWidget(self.busy)

        self._digest_ready.connect(self._on_digest_ready)

    # ------------------------------------------------------------- lifecycle
    def showEvent(self, e):
        super().showEvent(e)
        if not self._loaded_once:
            self._reload()

    def notice_event(self, ev):
        """Panel hook: a fresh absence digest landed (the tray computed it on
        unlock). Only that one event refreshes this view — everything else here
        is a period the user chose deliberately."""
        if getattr(ev, "action", "") != "digest":
            return
        if self.cb_period.currentData() == "absence" and self.isVisible():
            self._reload()

    # ---------------------------------------------------------------- loading
    def _window(self):
        """(since, until) for the chosen period, or (None, None) for the last
        absence — which is not a window we compute but a digest we already
        have."""
        value = self.cb_period.currentData()
        if value == "absence":
            return None, None
        now = time.time()
        if value == "today":
            midnight = datetime.datetime.combine(datetime.date.today(),
                                                 datetime.time.min).timestamp()
            return midnight, now
        return now - float(value), now

    def _reload(self):
        self._loaded_once = True
        self._gen += 1
        gen = self._gen
        since, until = self._window()
        self.busy.start("Working out what happened…")

        def work():
            try:
                if since is None:
                    digest = self.c.last_digest()
                else:
                    digest = self.c.digest_since(since, until)
            except Exception:  # noqa: BLE001 — an empty report reads as "nothing"
                digest = None
            try:
                self._digest_ready.emit(gen, digest)
            except RuntimeError:
                pass  # tab destroyed while loading

        threading.Thread(target=work, name="sincrogit-digest-tab",
                         daemon=True).start()

    def _on_digest_ready(self, gen, digest):
        self.busy.stop()
        if gen != self._gen:
            return  # a newer period is on its way
        self.tbl_repos.setRowCount(0)
        self.tbl_events.setRowCount(0)
        self.lbl_milestones.setText("")
        if not digest:
            # Only the "last absence" period can be empty like this: there has
            # been no absence yet in this session.
            self.lbl_head.setText("Nothing to report yet.")
            self.lbl_span.setText(
                "SincroGit works out what happened when you come back to a "
                "locked machine. Pick another period to look at any stretch "
                "of time.")
            return
        self.lbl_head.setText(
            "Nothing changed." if digest.get("trivial") else digest["summary"])
        span = humanize_span(digest["until"] - digest["since"])
        note = (" (as far back as one history walk can see — there may be more)"
                if digest.get("partial") else "")
        self.lbl_span.setText(
            f"{_fmt(digest['since'])} → {_fmt(digest['until'])}  ·  {span}{note}")
        self._fill_repos(digest)
        self._fill_milestones(digest)

    def _fill_repos(self, digest):
        repos = digest.get("repos") or []
        self.tbl_repos.setRowCount(len(repos))
        ok = QColor(self._pal.get("success", "#2e9e5b"))
        muted = QColor(self._pal.get("muted", "#6b7280"))
        for row, r in enumerate(repos):
            lines = QTableWidgetItem(f"+{r['adds']}  −{r['dels']}")
            lines.setForeground(ok if r["adds"] >= r["dels"] else muted)
            cells = (QTableWidgetItem(r["name"]),
                     QTableWidgetItem(str(r["files"])),
                     lines,
                     QTableWidgetItem(str(r["seals"]) if r["seals"] else "—"),
                     QTableWidgetItem(str(r["marks"]) if r["marks"] else "—"))
            for col, item in enumerate(cells):
                if col:
                    item.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
                self.tbl_repos.setItem(row, col, item)

    def _fill_milestones(self, digest):
        items = digest.get("milestones") or []
        self.tbl_events.setRowCount(len(items))
        muted = QColor(self._pal.get("muted", "#6b7280"))
        for row, m in enumerate(items):
            when = QTableWidgetItem(
                datetime.datetime.fromtimestamp(m["epoch"]).strftime("%d %b %H:%M"))
            when.setForeground(muted)
            when.setToolTip(_fmt(m["epoch"]))
            repo = QTableWidgetItem(m["repo"] or "—")
            word = _ACTION_WORD.get(m["action"], m["action"])
            what = QTableWidgetItem(f"{word}: {m['message']}")
            what.setForeground(QColor(_ACTION_COLOR.get(m["action"], "#6b7280")))
            for col, item in enumerate((when, repo, what)):
                self.tbl_events.setItem(row, col, item)
        self.lbl_milestones.setText(
            f"{len(items)} milestone(s) — commits, marks, handoffs and problems"
            if items else "No milestones in this period.")
