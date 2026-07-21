"""Timeline v3 — the third proposal, redesigned from scratch (kept ALONGSIDE
the Time machine tab and the v2 activity band, so the best of each can win).

Paradigm: drill-down by TIME SCALE, GitHub-contributions style.

  Level 1 — CALENDAR HEATMAP: weeks as columns, weekdays as rows, each day
            cell shaded by how much happened (log-scaled churn + state count);
            a dot marks days that produced a permanent commit (seal). Months
            labeled along the top. One glance answers "when did I work on
            this?" across weeks — the scale the rail (v1) and the band (v2)
            are weakest at.
  Level 2 — DAY STRIP: click a day and its 24 hours unfold as a horizontal
            strip with one chip per state (snapshot / seal / autosnap, the
            shared colors). The work rhythm of THAT day.
  Level 3 — STATE DETAIL: click a chip → its files and a colored diff vs the
            parent (the same detail the other tabs show).

Reuses the controller contract (repo_list, snapshot_timeline, file_text_at)
and the shared helpers, and mirrors the public API of the other timeline tabs
(focus_repo / notice_event / showEvent-reload). Same threading contract:
git reads on a worker, back via a generation-guarded queued signal.
"""

import datetime
import math
import threading

from PyQt5.QtCore import QRect, Qt, QTimer, pyqtSignal
from PyQt5.QtGui import QColor, QFont, QFontMetrics, QPainter, QPen
from PyQt5.QtWidgets import (
    QComboBox,
    QHBoxLayout,
    QLabel,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from .busy import BusyBar
from .diff import diff_html
from .time_machine_tab import _TYPE_COLOR, _ago, _fmt, _summary

# Heatmap geometry.
_CELL = 16
_GAP = 3
_LEFT_PAD = 30      # weekday initials
_TOP_PAD = 18       # month labels
_SEAL_ACCENT = _TYPE_COLOR["seal"]
_HEAT_BASE = "#4a7fb5"          # activity shade (distinct from the kind colors)

# Day strip geometry.
_STRIP_H = 64
_STRIP_PAD = 26     # hour labels gutter on both sides
_CHIP_W = 5
_CHIP_HIT = 8

_RANGES = [("13 weeks", 13), ("4 weeks", 4), ("26 weeks", 26), ("All", None)]


def _aggregate_days(entries) -> dict:
    """{date: {"count", "churn", "seals", "autos", "entries"}} — entries kept
    oldest-first per day. `churn` is the summed +/− of the day (binaries → 0)."""
    days = {}
    for e in sorted(entries or [], key=lambda x: x["epoch"]):
        try:
            d = datetime.date.fromtimestamp(e["epoch"])
        except (ValueError, OSError, TypeError):
            continue
        slot = days.setdefault(d, {"count": 0, "churn": 0, "seals": 0,
                                   "autos": 0, "entries": []})
        _files, adds, dels = _summary(e)
        kind = e.get("kind", "snapshot")
        slot["count"] += 1
        slot["churn"] += (adds or 0) + (dels or 0)
        slot["seals"] += 1 if kind in ("seal", "sealed") else 0
        slot["autos"] += 1 if kind == "autosnap" else 0
        slot["entries"].append(e)
    return days


def _heat_weight(day: dict) -> float:
    """One number per day for the shade: churn dominates, count breaks ties
    (a day of many zero-churn snapshots still reads as activity)."""
    return math.log1p(day["churn"]) + 0.35 * math.log1p(day["count"])


class _CalendarHeatmap(QWidget):
    """Weeks × weekdays grid ending at the CURRENT week. Emits day_selected
    with a datetime.date when a day WITH DATA is clicked."""

    day_selected = pyqtSignal(object)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMouseTracking(True)
        self._days = {}
        self._weeks = 13
        self._max_w = 1.0
        self._sel = None            # selected date
        self._font = QFont()
        self._font.setPointSize(8)
        self._resize_to_grid()

    # --------------------------------------------------------------- data
    def set_days(self, days: dict, weeks: int):
        self._days = days or {}
        self._weeks = max(1, weeks)
        self._max_w = max([1.0] + [_heat_weight(d) for d in self._days.values()])
        self._sel = None
        self._resize_to_grid()
        self.update()

    def select_day(self, d):
        self._sel = d
        self.update()

    def _resize_to_grid(self):
        w = _LEFT_PAD + self._weeks * (_CELL + _GAP) + 8
        h = _TOP_PAD + 7 * (_CELL + _GAP) + 4
        self.setMinimumSize(w, h)
        self.setMaximumHeight(h)

    # ------------------------------------------------------------- mapping
    def _grid_start(self) -> datetime.date:
        """Monday of the LEFTMOST week column (the grid ends at today's week)."""
        today = datetime.date.today()
        this_monday = today - datetime.timedelta(days=today.weekday())
        return this_monday - datetime.timedelta(weeks=self._weeks - 1)

    def _rect_for(self, d: datetime.date) -> QRect | None:
        start = self._grid_start()
        offset = (d - start).days
        if offset < 0:
            return None
        col, row = divmod(offset, 7)
        if col >= self._weeks:
            return None
        return QRect(_LEFT_PAD + col * (_CELL + _GAP),
                     _TOP_PAD + row * (_CELL + _GAP), _CELL, _CELL)

    def _date_at(self, pos) -> datetime.date | None:
        col = (pos.x() - _LEFT_PAD) // (_CELL + _GAP)
        row = (pos.y() - _TOP_PAD) // (_CELL + _GAP)
        if not (0 <= col < self._weeks and 0 <= row < 7):
            return None
        d = self._grid_start() + datetime.timedelta(days=int(col) * 7 + int(row))
        return d if d <= datetime.date.today() else None

    # ---------------------------------------------------------- interaction
    def mousePressEvent(self, ev):
        if ev.button() != Qt.LeftButton:
            return
        d = self._date_at(ev.pos())
        if d is not None and d in self._days:
            self._sel = d
            self.update()
            self.day_selected.emit(d)

    def mouseMoveEvent(self, ev):
        d = self._date_at(ev.pos())
        info = self._days.get(d) if d else None
        if info:
            seals = f" · {info['seals']} seal(s)" if info["seals"] else ""
            self.setToolTip(f"{d.strftime('%a %d %b %Y')}\n"
                            f"{info['count']} state(s), {info['churn']} lines changed{seals}")
        elif d:
            self.setToolTip(d.strftime("%a %d %b %Y") + "\nno activity")
        else:
            self.setToolTip("")

    # ------------------------------------------------------------- painting
    def paintEvent(self, _ev):
        p = QPainter(self)
        dark = self.palette().window().color().lightness() < 128
        axis = QColor("#999" if dark else "#666")
        empty = QColor(255, 255, 255, 18) if dark else QColor(0, 0, 0, 14)
        base = QColor(_HEAT_BASE)
        p.setFont(self._font)
        fm = QFontMetrics(self._font)

        # Weekday initials (Mon/Wed/Fri rows, GitHub-style).
        p.setPen(axis)
        for row, label in ((0, "M"), (2, "W"), (4, "F")):
            y = _TOP_PAD + row * (_CELL + _GAP) + _CELL - 4
            p.drawText(10, y, label)

        start = self._grid_start()
        today = datetime.date.today()
        last_month = None
        for col in range(self._weeks):
            monday = start + datetime.timedelta(weeks=col)
            if monday.month != last_month:      # label each month once
                last_month = monday.month
                p.setPen(axis)
                p.drawText(_LEFT_PAD + col * (_CELL + _GAP), _TOP_PAD - 6,
                           monday.strftime("%b"))
            for row in range(7):
                d = monday + datetime.timedelta(days=row)
                if d > today:
                    continue
                r = QRect(_LEFT_PAD + col * (_CELL + _GAP),
                          _TOP_PAD + row * (_CELL + _GAP), _CELL, _CELL)
                info = self._days.get(d)
                if info:
                    frac = _heat_weight(info) / self._max_w
                    alpha = int(45 + frac * 205)
                    p.fillRect(r, QColor(base.red(), base.green(), base.blue(), alpha))
                    if info["seals"]:
                        p.setBrush(QColor(_SEAL_ACCENT))
                        p.setPen(Qt.NoPen)
                        p.drawEllipse(r.center(), 2, 2)
                else:
                    p.fillRect(r, empty)
                if d == self._sel:
                    p.setPen(QPen(QColor(_SEAL_ACCENT if info and info["seals"]
                                         else base), 2))
                    p.setBrush(Qt.NoBrush)
                    p.drawRect(r.adjusted(0, 0, -1, -1))
                elif d == today:
                    p.setPen(QPen(axis, 1))
                    p.setBrush(Qt.NoBrush)
                    p.drawRect(r.adjusted(0, 0, -1, -1))
        _ = fm
        p.end()


class _DayStrip(QWidget):
    """The 24 hours of ONE day as a horizontal strip: a chip per state at its
    time of day. Emits state_selected(index into the day's entries)."""

    state_selected = pyqtSignal(int)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumHeight(_STRIP_H)
        self.setMaximumHeight(_STRIP_H)
        self.setMouseTracking(True)
        self._entries = []
        self._sel = -1
        self._font = QFont()
        self._font.setPointSize(8)

    def set_entries(self, entries):
        self._entries = entries or []
        self._sel = -1
        self.update()

    def select_index(self, i):
        self._sel = i if 0 <= i < len(self._entries) else -1
        self.update()

    # ------------------------------------------------------------- mapping
    def _x_for(self, epoch: float) -> float:
        t = datetime.datetime.fromtimestamp(epoch)
        hour = t.hour + t.minute / 60 + t.second / 3600
        return _STRIP_PAD + hour / 24 * (self.width() - 2 * _STRIP_PAD)

    def _index_at(self, x: float):
        best, best_d = None, _CHIP_HIT + 1
        for i, e in enumerate(self._entries):
            d = abs(self._x_for(e["epoch"]) - x)
            if d < best_d:
                best, best_d = i, d
        return best

    # ---------------------------------------------------------- interaction
    def mousePressEvent(self, ev):
        if ev.button() != Qt.LeftButton:
            return
        hit = self._index_at(ev.x())
        if hit is not None:
            self._sel = hit
            self.update()
            self.state_selected.emit(hit)

    def mouseMoveEvent(self, ev):
        hit = self._index_at(ev.x())
        if hit is not None:
            e = self._entries[hit]
            files, adds, dels = _summary(e)
            self.setToolTip(f"{_fmt(e['epoch'])} · {e.get('kind', 'snapshot')}\n"
                            f"{files}  +{adds} −{dels}")
        else:
            self.setToolTip("")

    # ------------------------------------------------------------- painting
    def paintEvent(self, _ev):
        p = QPainter(self)
        dark = self.palette().window().color().lightness() < 128
        axis = QColor("#999" if dark else "#666")
        base_y = self.height() - 16
        p.setPen(QPen(axis, 1))
        p.drawLine(_STRIP_PAD, base_y, self.width() - _STRIP_PAD, base_y)
        p.setFont(self._font)
        fm = QFontMetrics(self._font)
        for h in range(0, 25, 3):               # hour ticks every 3 h
            x = _STRIP_PAD + h / 24 * (self.width() - 2 * _STRIP_PAD)
            p.setPen(QPen(QColor(axis.red(), axis.green(), axis.blue(), 60), 1))
            p.drawLine(int(x), 8, int(x), base_y)
            p.setPen(axis)
            label = f"{h:02d}"
            p.drawText(int(x) - fm.width(label) // 2, self.height() - 3, label)
        if not self._entries:
            p.setPen(axis)
            p.drawText(self.rect(), Qt.AlignCenter, "Pick a day above.")
            p.end()
            return
        for i, e in enumerate(self._entries):
            kind = e.get("kind", "snapshot")
            col = QColor(_TYPE_COLOR.get(kind, _TYPE_COLOR["snapshot"]))
            x = self._x_for(e["epoch"])
            seal = kind in ("seal", "sealed")
            top = 10 if seal else 20
            if i == self._sel:
                p.fillRect(int(x) - _CHIP_HIT, 8, 2 * _CHIP_HIT, base_y - 8,
                           QColor(col.red(), col.green(), col.blue(), 40))
            p.fillRect(int(x - _CHIP_W / 2), top, _CHIP_W, base_y - top, col)
        p.end()


class TimelineV3Tab(QWidget):
    """The drill-down proposal. Public API mirrors the other timeline tabs so
    the panel wires it the same way."""

    _timeline_ready = pyqtSignal(int, object)   # gen, entries
    _diff_ready = pyqtSignal(int, str)          # gen, html

    def __init__(self, controller, parent=None):
        super().__init__(parent)
        self.c = controller
        self._gen = 0
        self._diff_gen = 0
        self._all = []
        self._days = {}
        self._day_entries = []
        self._stale = True
        self._loaded_once = False

        v = QVBoxLayout(self)
        top = QHBoxLayout()
        top.addWidget(QLabel("Repo:"))
        self.cb_repo = QComboBox()
        self.cb_repo.currentIndexChanged.connect(self._repo_changed)
        top.addWidget(self.cb_repo)
        top.addWidget(QLabel("Range:"))
        self.cb_range = QComboBox()
        for label, _weeks in _RANGES:
            self.cb_range.addItem(label)
        self.cb_range.currentIndexChanged.connect(self._apply_range)
        top.addWidget(self.cb_range)
        top.addStretch(1)
        self.lbl_legend = QLabel("shade = activity · ● = sealed that day")
        self.lbl_legend.setProperty("cssClass", "muted")
        top.addWidget(self.lbl_legend)
        v.addLayout(top)

        self.heatmap = _CalendarHeatmap()
        self.heatmap.day_selected.connect(self._on_day_selected)
        v.addWidget(self.heatmap)

        self.lbl_day = QLabel("Click a day to unfold its hours.")
        self.lbl_day.setProperty("cssClass", "muted")
        v.addWidget(self.lbl_day)

        self.strip = _DayStrip()
        self.strip.state_selected.connect(self._on_state_selected)
        v.addWidget(self.strip)

        self.lbl_sel = QLabel("")
        self.lbl_sel.setProperty("cssClass", "muted")
        v.addWidget(self.lbl_sel)

        split = QSplitter(Qt.Horizontal)
        self.tbl_files = QTableWidget(0, 3)
        self.tbl_files.setHorizontalHeaderLabels(["Δ", "File", "+/−"])
        self.tbl_files.verticalHeader().setVisible(False)
        self.tbl_files.setSelectionBehavior(QTableWidget.SelectRows)
        self.tbl_files.setEditTriggers(QTableWidget.NoEditTriggers)
        self.tbl_files.currentCellChanged.connect(lambda *_: self._load_diff())
        split.addWidget(self.tbl_files)
        self.diff = QTextEdit()
        self.diff.setReadOnly(True)
        self.diff.setFont(QFont("Consolas", 10))
        split.addWidget(self.diff)
        split.setStretchFactor(0, 1)
        split.setStretchFactor(1, 2)
        v.addWidget(split, 1)

        self.busy = BusyBar()
        v.addWidget(self.busy)

        self._timeline_ready.connect(self._on_timeline_ready)
        self._diff_ready.connect(self._on_diff_ready)
        self._debounce = QTimer(self)
        self._debounce.setSingleShot(True)
        self._debounce.setInterval(800)
        self._debounce.timeout.connect(self._reload)

    # ------------------------------------------------------------ public API
    def focus_repo(self, name=None, pin=None):
        self._sync_repos()
        if name:
            i = self.cb_repo.findText(name)
            if i >= 0 and i != self.cb_repo.currentIndex():
                self.cb_repo.setCurrentIndex(i)
        if self._stale or not self._loaded_once:
            self._reload()

    def notice_event(self, ev):
        if getattr(ev, "action", "") not in ("snapshot", "seal", "handoff", "pull"):
            return
        if getattr(ev, "repo", "") != self.cb_repo.currentText():
            return
        self._stale = True
        if self.isVisible():
            self._debounce.start()

    def showEvent(self, e):
        super().showEvent(e)
        self._sync_repos()
        if self._stale or not self._loaded_once:
            self._reload()

    # --------------------------------------------------------------- repos
    def _sync_repos(self):
        names = [n for n, _p in self.c.repo_list()]
        have = [self.cb_repo.itemText(i) for i in range(self.cb_repo.count())]
        if names != have:
            self.cb_repo.blockSignals(True)
            current = self.cb_repo.currentText()
            self.cb_repo.clear()
            for n, p in self.c.repo_list():
                self.cb_repo.addItem(n, p)
            if current in names:
                self.cb_repo.setCurrentText(current)
            self.cb_repo.blockSignals(False)

    def _repo_changed(self):
        self._clear_detail(full=True)
        self._reload()

    # ------------------------------------------------------------- loading
    def _reload(self):
        name = self.cb_repo.currentText()
        if not name:
            self.heatmap.set_days({}, 13)
            return
        self._stale = False
        self._loaded_once = True
        self._gen += 1
        gen = self._gen
        self.busy.start("Loading the calendar…")

        def work():
            try:
                entries = self.c.snapshot_timeline(name)
            except Exception:  # noqa: BLE001 — empty reads as "no history"
                entries = []
            try:
                self._timeline_ready.emit(gen, entries)
            except RuntimeError:
                pass
        threading.Thread(target=work, name="sincrogit-tl3-load", daemon=True).start()

    def _on_timeline_ready(self, gen, entries):
        self.busy.stop()
        if gen != self._gen:
            return
        self._all = entries or []
        self._apply_range()

    def _weeks(self) -> int:
        weeks = _RANGES[self.cb_range.currentIndex()][1]
        if weeks is not None:
            return weeks
        if not self._days:
            return 13
        oldest = min(self._days)
        span_days = (datetime.date.today() - oldest).days + 1
        return min(53, max(13, math.ceil(span_days / 7)))

    def _apply_range(self):
        self._days = _aggregate_days(self._all)
        weeks = self._weeks()
        cutoff = datetime.date.today() - datetime.timedelta(weeks=weeks)
        self._days = {d: v for d, v in self._days.items() if d > cutoff}
        self.heatmap.set_days(self._days, weeks)
        self._clear_detail(full=False)
        if self._days:
            latest = max(self._days)            # open the most recent day
            self.heatmap.select_day(latest)
            self._on_day_selected(latest)
        else:
            self.lbl_day.setText("No activity in this range.")
            self.strip.set_entries([])

    # -------------------------------------------------------------- drill-down
    def _on_day_selected(self, d):
        info = self._days.get(d)
        self._day_entries = (info or {}).get("entries") or []
        seals = f" · {info['seals']} seal(s)" if info and info["seals"] else ""
        self.lbl_day.setText(
            f"{d.strftime('%A %d %B %Y')} — {len(self._day_entries)} state(s), "
            f"{(info or {}).get('churn', 0)} lines changed{seals}"
            if info else "No activity that day.")
        self.strip.set_entries(self._day_entries)
        self.tbl_files.setRowCount(0)
        self.diff.setPlainText("")
        self.lbl_sel.setText("")
        if self._day_entries:                   # open the day's last state
            last = len(self._day_entries) - 1
            self.strip.select_index(last)
            self._on_state_selected(last)

    def _on_state_selected(self, index):
        if not (0 <= index < len(self._day_entries)):
            return
        e = self._day_entries[index]
        files, adds, dels = _summary(e)
        self.lbl_sel.setText(
            f"{_fmt(e['epoch'])} · {e.get('kind', 'snapshot')} · {_ago(e['epoch'])}"
            f"   —   {files}  +{adds} −{dels}")
        rows = e.get("files") or []
        self.tbl_files.setRowCount(len(rows))
        for i, (status, path, a, d) in enumerate(rows):
            self.tbl_files.setItem(i, 0, QTableWidgetItem(status))
            it = QTableWidgetItem(path)
            it.setData(Qt.UserRole, (e.get("sha"), e.get("parent"), path, a, d))
            self.tbl_files.setItem(i, 1, it)
            churn = "binary" if a is None and d is None else f"+{a or 0} −{d or 0}"
            self.tbl_files.setItem(i, 2, QTableWidgetItem(churn))
        if rows:
            self.tbl_files.selectRow(0)
        else:
            self.diff.setPlainText("")

    # ----------------------------------------------------------------- diff
    def _load_diff(self):
        row = self.tbl_files.currentRow()
        item = self.tbl_files.item(row, 1) if row >= 0 else None
        if item is None:
            return
        sha, parent, path, adds, dels = item.data(Qt.UserRole)
        if adds is None and dels is None:
            self.diff.setPlainText("(binary file — no text diff)")
            return
        name = self.cb_repo.currentText()
        dark = bool(getattr(self.c, "theme", {}).get("is_dark"))
        self._diff_gen += 1
        gen = self._diff_gen
        self.diff.setPlainText("Loading the diff…")
        self.busy.start("Loading the diff…")

        def work():
            try:
                new = self.c.file_text_at(name, path, sha) or ""
                old = (self.c.file_text_at(name, path, parent) or "") if parent else ""
                html = diff_html(old, new, dark, "before", "this state")
            except Exception as e:  # noqa: BLE001
                html = f"<pre>Could not load the diff: {e}</pre>"
            try:
                self._diff_ready.emit(gen, html)
            except RuntimeError:
                pass
        threading.Thread(target=work, name="sincrogit-tl3-diff", daemon=True).start()

    def _on_diff_ready(self, gen, html):
        self.busy.stop()
        if gen != self._diff_gen:
            return
        self.diff.setHtml(html)

    def _clear_detail(self, full: bool):
        self.tbl_files.setRowCount(0)
        self.diff.setPlainText("")
        self.lbl_sel.setText("")
        self.strip.set_entries([])
        if full:
            self.lbl_day.setText("Click a day to unfold its hours.")
