"""'My machines' view: every machine's last autosnap mirror, per repo.

Answers at a glance the question that is otherwise invisible until you NEED the
mirror: "is my other machine still backing itself up?". Rows come from the
locally-known autosnap refs (no network); "Fetch latest" refreshes them from
the remote on a background thread. Freshness is color-coded by age.

Talks to the app through the `controller`:
  repo_list() -> [(name, path), ...]
  list_autosnaps(name) -> [ {host, branch, epoch, sha, ...}, ... ]   (local)
  fetch_autosnaps(name) -> same, after fetching from the remote      (network)
  this_host() -> str
"""

import threading
import time

from PyQt5.QtCore import pyqtSignal
from PyQt5.QtGui import QColor
from PyQt5.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
)

from .time_machine_tab import _ago, _fmt

# Freshness buckets by mirror age (tooltips carry the exact stamp).
_FRESH_H, _STALE_H = 2, 48
_COLOR_FRESH, _COLOR_AGING, _COLOR_STALE = "#2E9E5B", "#E0A400", "#D23F3F"


class MachinesDialog(QDialog):
    # Emitted from worker threads; delivered on the GUI thread (queued).
    _fetch_done = pyqtSignal(int, int)   # repos fetched OK, repos total
    _rows_ready = pyqtSignal(int, list)  # gen, [(host, repo, branch, epoch)]

    def __init__(self, controller, parent=None):
        super().__init__(parent)
        self.c = controller
        self.setWindowTitle("⏳g SincroGit — My machines")
        self.resize(680, 380)

        v = QVBoxLayout(self)
        head = QLabel(
            "Each machine's last live mirror (autosnap) per repo — your recovery "
            "points if a disk dies. A machine that stops mirroring goes stale here."
        )
        head.setWordWrap(True)
        head.setProperty("cssClass", "muted")
        v.addWidget(head)

        self.tbl = QTableWidget(0, 4)
        self.tbl.setHorizontalHeaderLabels(["Machine", "Repo", "Branch", "Last mirror"])
        hdr = self.tbl.horizontalHeader()
        for col in (0, 2, 3):
            hdr.setSectionResizeMode(col, QHeaderView.ResizeToContents)
        hdr.setSectionResizeMode(1, QHeaderView.Stretch)
        self.tbl.setSelectionBehavior(QTableWidget.SelectRows)
        self.tbl.setEditTriggers(QTableWidget.NoEditTriggers)
        self.tbl.setAlternatingRowColors(True)
        self.tbl.setShowGrid(False)
        self.tbl.verticalHeader().setVisible(False)
        v.addWidget(self.tbl, 1)

        row = QHBoxLayout()
        self.lbl_info = QLabel()
        self.lbl_info.setProperty("cssClass", "muted")
        row.addWidget(self.lbl_info, 1)
        self.btn_fetch = QPushButton("Fetch latest (network)")
        self.btn_fetch.setToolTip(
            "Download every machine's current mirrors from each repo's remote."
        )
        self.btn_fetch.clicked.connect(self._fetch)
        row.addWidget(self.btn_fetch)
        btn_close = QPushButton("Close")
        btn_close.clicked.connect(self.accept)
        row.addWidget(btn_close)
        v.addLayout(row)

        self._fetch_done.connect(self._on_fetch_done)
        self._rows_ready.connect(self._on_rows_ready)
        self._gen = 0      # discard a stale listing if a newer reload started
        self._note = ""    # suffix for lbl_info (set by a finished fetch)
        self._reload()

    def _reload(self):
        """Gather the rows on a worker: list_autosnaps spawns one `git
        for-each-ref` subprocess PER REPO — no network, but N × subprocess
        latency froze the dialog open for seconds with several repos."""
        self._gen += 1
        gen = self._gen
        names = [name for name, _ in self.c.repo_list()]
        self.lbl_info.setText("Reading local mirrors…")

        def work():
            rows = []
            for name in names:
                try:
                    for r in self.c.list_autosnaps(name):
                        rows.append((r["host"], name, r["branch"], r["epoch"]))
                except Exception:  # noqa: BLE001 — a broken repo just lists nothing
                    pass
            try:
                self._rows_ready.emit(gen, rows)
            except RuntimeError:
                pass  # dialog closed while listing

        threading.Thread(target=work, name="sincrogit-machines-list",
                         daemon=True).start()

    def _on_rows_ready(self, gen, rows):
        if gen != self._gen:
            return  # a newer reload superseded this one
        me = self.c.this_host()
        rows.sort(key=lambda r: r[3], reverse=True)
        now = time.time()
        self.tbl.setRowCount(len(rows))
        for i, (host, repo, branch, epoch) in enumerate(rows):
            age_h = (now - epoch) / 3600 if epoch else float("inf")
            color = (_COLOR_FRESH if age_h <= _FRESH_H
                     else _COLOR_AGING if age_h <= _STALE_H else _COLOR_STALE)
            label = host + ("  (this machine)" if host == me else "")
            cells = [label, repo, branch, _ago(epoch)]
            for j, val in enumerate(cells):
                item = QTableWidgetItem(val)
                if j == 3:
                    item.setToolTip(_fmt(epoch))
                    item.setForeground(QColor(color))
                self.tbl.setItem(i, j, item)
        if not rows:
            self.lbl_info.setText(
                "No mirrors known yet — Fetch latest, or enable autosnap." + self._note)
        else:
            self.lbl_info.setText(f"{len(rows)} mirror(s) known locally" + self._note)
        self._note = ""

    # ------------------------------------------------------------- fetching
    def _fetch(self):
        self.btn_fetch.setEnabled(False)
        self.lbl_info.setText("Fetching mirrors from the remotes…")
        names = [name for name, _ in self.c.repo_list()]
        threading.Thread(target=self._do_fetch, args=(names,),
                         name="sincrogit-machines-fetch", daemon=True).start()

    def _do_fetch(self, names):
        ok = 0
        for name in names:
            try:
                self.c.fetch_autosnaps(name)
                ok += 1
            except Exception:  # noqa: BLE001 — a dead remote just stays stale
                pass
        try:
            self._fetch_done.emit(ok, len(names))
        except RuntimeError:
            pass  # dialog closed while fetching

    def _on_fetch_done(self, ok, total):
        self.btn_fetch.setEnabled(True)
        extra = "" if ok == total else f"  ({total - ok} repo(s) unreachable)"
        self._note = f" — refreshed{extra}"  # appended when the async reload lands
        self._reload()
