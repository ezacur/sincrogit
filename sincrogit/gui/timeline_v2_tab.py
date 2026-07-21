"""Timeline v2 — a proposal to sit ALONGSIDE the Time machine tab so the two
can be compared (Ernesto: "pick the best of each").

Where the Time machine tab is a vertical, day-grouped RAIL of cards, this is a
horizontal ACTIVITY BAND: time runs left (oldest) to right (now); every
snapshot is a bar whose height encodes its churn (lines added+deleted), seals
are taller accented bars and autosnaps are diamonds. At a glance you see the
SHAPE of the work — bursts, quiet days, the big commits — which a scrolling
list can't convey. Wheel to zoom the time axis, drag to pan; click a bar to
open that state below (its files + a colored diff), same as the other tab.

Deliberately reuses the existing controller contract (repo_list,
snapshot_timeline, file_text_at, current_text) and the shared diff/format
helpers, so it's a real working tab, not a mockup. Same threading contract:
git reads happen on a worker and return via a generation-guarded signal; the
GUI thread never touches disk.
"""

import math
import threading

from PyQt5.QtCore import QPoint, Qt, QTimer, pyqtSignal
from PyQt5.QtGui import QColor, QFont, QFontMetrics, QPainter, QPen
from PyQt5.QtWidgets import (
    QCheckBox,
    QHBoxLayout,
    QLabel,
    QComboBox,
    QPushButton,
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

# Band geometry.
_BAND_H = 150
_PAD_L = 44          # room for the churn axis labels
_PAD_R = 14
_PAD_TOP = 10
_AXIS_H = 22         # bottom strip for the time labels
_MIN_MARK_H = 4      # even a zero-churn snapshot gets a visible tick
_MARK_W = 3
_HIT_PX = 6          # click tolerance around a mark


def _kind_color(kind: str) -> str:
    return _TYPE_COLOR.get(kind, _TYPE_COLOR["snapshot"])


def _is_seal(kind: str) -> bool:
    return kind in ("seal", "sealed")


class _ActivityBand(QWidget):
    """A painted horizontal timeline. Owns only the VIEW window (which slice of
    time is shown); the entries come from the tab. Emits `selected` with the
    entry index on a click, `hovered` with the index (or -1) as the mouse moves."""

    selected = pyqtSignal(int)
    hovered = pyqtSignal(int)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumHeight(_BAND_H)
        self.setMouseTracking(True)
        self._entries = []          # [{epoch, kind, churn, ...}] oldest-first
        self._t0 = 0.0              # data time bounds
        self._t1 = 1.0
        self._view0 = 0.0          # visible window (a sub-range of [t0, t1])
        self._view1 = 1.0
        self._max_churn = 1
        self._sel = -1
        self._hover = -1
        self._drag_x = None        # press x while a drag/pan is in progress
        self._press_x = None
        self._axis_font = QFont()
        self._axis_font.setPointSize(8)

    # ------------------------------------------------------------- data / view
    def set_entries(self, entries):
        """`entries` are timeline dicts, oldest first. Resets the view to fit."""
        self._entries = entries or []
        if self._entries:
            self._t0 = min(e["epoch"] for e in self._entries)
            self._t1 = max(e["epoch"] for e in self._entries)
            if self._t1 <= self._t0:
                self._t1 = self._t0 + 1.0
            self._max_churn = max(1, max(e.get("churn", 0) for e in self._entries))
        else:
            self._t0, self._t1, self._max_churn = 0.0, 1.0, 1
        self.reset_view()

    def reset_view(self):
        # A hair of padding each side so the first/last marks aren't on the edge.
        span = max(1.0, self._t1 - self._t0)
        self._view0 = self._t0 - span * 0.02
        self._view1 = self._t1 + span * 0.02
        self._sel = -1
        self._hover = -1
        self.update()

    def select_index(self, i: int):
        self._sel = i if 0 <= i < len(self._entries) else -1
        self.update()

    # ------------------------------------------------------------- mapping
    def _plot_w(self) -> int:
        return max(1, self.width() - _PAD_L - _PAD_R)

    def _plot_h(self) -> int:
        return max(1, self.height() - _PAD_TOP - _AXIS_H)

    def _x_for(self, epoch: float) -> float:
        frac = (epoch - self._view0) / max(1e-9, self._view1 - self._view0)
        return _PAD_L + frac * self._plot_w()

    def _epoch_for(self, x: float) -> float:
        frac = (x - _PAD_L) / self._plot_w()
        return self._view0 + frac * (self._view1 - self._view0)

    def _mark_h(self, churn: int) -> float:
        """Log-scaled so one giant commit doesn't flatten everything else."""
        frac = math.log1p(max(0, churn)) / math.log1p(self._max_churn)
        return _MIN_MARK_H + frac * (self._plot_h() - _MIN_MARK_H)

    def _visible_marks(self):
        """[(index, x)] for entries whose time falls inside the view window."""
        out = []
        for i, e in enumerate(self._entries):
            if self._view0 <= e["epoch"] <= self._view1:
                out.append((i, self._x_for(e["epoch"])))
        return out

    def _mark_at(self, x: float):
        """Index of the nearest visible mark within _HIT_PX of x, or None."""
        best, best_d = None, _HIT_PX + 1
        for i, mx in self._visible_marks():
            d = abs(mx - x)
            if d < best_d:
                best, best_d = i, d
        return best

    # ------------------------------------------------------------- interaction
    def wheelEvent(self, ev):
        if not self._entries:
            return
        # Zoom about the cursor: the time under the mouse stays put.
        anchor = self._epoch_for(ev.position().x() if hasattr(ev, "position")
                                 else ev.x())
        factor = 0.8 if ev.angleDelta().y() > 0 else 1.25
        self._zoom(factor, anchor)
        ev.accept()

    def _zoom(self, factor: float, anchor: float):
        left = (anchor - self._view0) * factor
        right = (self._view1 - anchor) * factor
        self._view0, self._view1 = anchor - left, anchor + right
        self._clamp_view()
        self.update()
        self._emit_hover_at(None)

    def _clamp_view(self):
        span_data = max(1.0, self._t1 - self._t0)
        # Don't zoom out past the data (plus a small margin) or in below a minute.
        lo, hi = self._t0 - span_data * 0.05, self._t1 + span_data * 0.05
        if self._view1 - self._view0 > hi - lo:
            self._view0, self._view1 = lo, hi
        if self._view1 - self._view0 < 60:
            mid = (self._view0 + self._view1) / 2
            self._view0, self._view1 = mid - 30, mid + 30
        # Keep the window inside the padded data range.
        if self._view0 < lo:
            self._view1 += lo - self._view0
            self._view0 = lo
        if self._view1 > hi:
            self._view0 -= self._view1 - hi
            self._view1 = hi

    def mousePressEvent(self, ev):
        if ev.button() == Qt.LeftButton:
            self._press_x = ev.x()
            self._drag_x = ev.x()

    def mouseMoveEvent(self, ev):
        if self._drag_x is not None:
            # Pan: translate the view by the dragged pixel delta.
            dt = (ev.x() - self._drag_x) / self._plot_w() * (self._view1 - self._view0)
            self._view0 -= dt
            self._view1 -= dt
            self._clamp_view()
            self._drag_x = ev.x()
            self.update()
        else:
            self._emit_hover_at(ev.x())

    def mouseReleaseEvent(self, ev):
        if ev.button() != Qt.LeftButton:
            return
        moved = self._press_x is not None and abs(ev.x() - self._press_x) > 3
        self._drag_x = None
        self._press_x = None
        if not moved:                       # a click, not a pan: select
            hit = self._mark_at(ev.x())
            if hit is not None:
                self._sel = hit
                self.update()
                self.selected.emit(hit)

    def _emit_hover_at(self, x):
        hit = self._mark_at(x) if x is not None else None
        idx = -1 if hit is None else hit
        if idx != self._hover:
            self._hover = idx
            self.update()
            self.hovered.emit(idx)
        if hit is not None:
            e = self._entries[hit]
            files, adds, dels = _summary(e)
            self.setToolTip(f"{_fmt(e['epoch'])}\n{e.get('kind', 'snapshot')} · "
                            f"{files}  +{adds} −{dels}")
        else:
            self.setToolTip("")

    # ------------------------------------------------------------- painting
    def paintEvent(self, _ev):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, False)
        dark = self.palette().window().color().lightness() < 128
        axis_col = QColor("#666") if not dark else QColor("#999")
        base_y = self.height() - _AXIS_H

        # Baseline + churn axis ticks.
        p.setPen(QPen(axis_col, 1))
        p.drawLine(_PAD_L, base_y, self.width() - _PAD_R, base_y)
        p.setFont(self._axis_font)
        if self._entries:
            p.drawText(2, _PAD_TOP + 8, f"{self._max_churn}")
            p.drawText(2, base_y, "0")

        if not self._entries:
            p.setPen(axis_col)
            p.drawText(self.rect(), Qt.AlignCenter,
                       "No snapshots yet for this repo.")
            p.end()
            return

        self._paint_time_grid(p, base_y, axis_col, dark)

        # Marks.
        for i, x in self._visible_marks():
            e = self._entries[i]
            kind = e.get("kind", "snapshot")
            col = QColor(_kind_color(kind))
            h = self._mark_h(e.get("churn", 0))
            seal = _is_seal(kind)
            auto = kind == "autosnap"
            w = _MARK_W + (2 if seal else 0)
            top = base_y - h
            if i == self._sel:
                p.fillRect(int(x - w), _PAD_TOP, int(2 * w + 1),
                           base_y - _PAD_TOP, QColor(col.red(), col.green(),
                                                     col.blue(), 40))
            p.fillRect(int(x - w / 2), int(top), int(max(1, w)), int(h), col)
            if seal:                          # cap dot marks a permanent commit
                p.setBrush(col)
                p.setPen(Qt.NoPen)
                p.drawEllipse(QPoint(int(x), int(top)), 3, 3)
            elif auto:                        # diamond marks another machine
                p.setBrush(col)
                p.setPen(Qt.NoPen)
                p.drawEllipse(QPoint(int(x), int(top)), 2, 2)
            if i == self._hover and i != self._sel:
                p.setPen(QPen(col, 1))
                p.drawLine(int(x), _PAD_TOP, int(x), base_y)
        p.end()

    def _paint_time_grid(self, p, base_y, axis_col, dark):
        """Day (or hour, when zoomed in) gridlines + labels along the bottom."""
        import datetime
        span = self._view1 - self._view0
        p.setPen(QPen(QColor(axis_col.red(), axis_col.green(), axis_col.blue(), 60), 1))
        grid = QColor(axis_col)
        p.setFont(self._axis_font)
        fm = QFontMetrics(self._axis_font)
        hourly = span < 6 * 3600          # zoomed in: label hours, else days
        step = 3600 if hourly else 86400
        t = math.floor(self._view0 / step) * step
        while t <= self._view1:
            x = self._x_for(t)
            if x >= _PAD_L:
                p.setPen(QPen(QColor(grid.red(), grid.green(), grid.blue(), 45), 1))
                p.drawLine(int(x), _PAD_TOP, int(x), base_y)
                try:
                    d = datetime.datetime.fromtimestamp(t)
                    label = d.strftime("%H:%M" if hourly else "%d %b")
                except (ValueError, OSError):
                    label = ""
                p.setPen(axis_col)
                p.drawText(int(x) - fm.width(label) // 2, base_y + 15, label)
            t += step


class TimelineV2Tab(QWidget):
    """The proposal tab. Public API mirrors TimeMachineTab (focus_repo /
    notice_event / showEvent-reload) so the panel wires it the same way."""

    _timeline_ready = pyqtSignal(int, object)   # gen, entries
    _diff_ready = pyqtSignal(int, str)          # gen, html

    def __init__(self, controller, parent=None):
        super().__init__(parent)
        self.c = controller
        self._gen = 0
        self._diff_gen = 0
        self._entries = []
        self._stale = True
        self._loaded_once = False

        v = QVBoxLayout(self)

        top = QHBoxLayout()
        top.addWidget(QLabel("Repo:"))
        self.cb_repo = QComboBox()
        self.cb_repo.currentIndexChanged.connect(self._repo_changed)
        top.addWidget(self.cb_repo)
        self.ck_snap = QCheckBox("Snapshots")
        self.ck_seal = QCheckBox("Seals")
        self.ck_auto = QCheckBox("Autosnaps")
        for ck in (self.ck_snap, self.ck_seal, self.ck_auto):
            ck.setChecked(True)
            ck.toggled.connect(self._apply_filter)
            top.addWidget(ck)
        top.addStretch(1)
        self.btn_reset = QPushButton("Reset zoom")
        self.btn_reset.clicked.connect(lambda: self.band.reset_view())
        top.addWidget(self.btn_reset)
        v.addLayout(top)

        self.lbl_hint = QLabel("Wheel to zoom · drag to pan · click a bar to open it. "
                               "Bar height = lines changed.")
        self.lbl_hint.setProperty("cssClass", "muted")
        v.addWidget(self.lbl_hint)

        self.band = _ActivityBand()
        self.band.selected.connect(self._on_band_selected)
        v.addWidget(self.band)

        self.lbl_sel = QLabel("Select a bar above to see what changed.")
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

    # ------------------------------------------------------------- repos
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
        self._clear_detail()
        self._reload()

    # ------------------------------------------------------------- loading
    def _reload(self):
        name = self.cb_repo.currentText()
        if not name:
            self.band.set_entries([])
            return
        self._stale = False
        self._loaded_once = True
        self._gen += 1
        gen = self._gen
        self.busy.start("Loading the timeline…")

        def work():
            try:
                entries = self.c.snapshot_timeline(name)
            except Exception:  # noqa: BLE001 — empty reads as "no history"
                entries = []
            try:
                self._timeline_ready.emit(gen, entries)
            except RuntimeError:
                pass  # tab closed mid-load
        threading.Thread(target=work, name="sincrogit-tl2-load", daemon=True).start()

    def _on_timeline_ready(self, gen, entries):
        self.busy.stop()
        if gen != self._gen:
            return
        # Oldest-first for the band (time flows left->right); precompute churn.
        rows = sorted(entries or [], key=lambda e: e["epoch"])
        for e in rows:
            _f, a, d = _summary(e)
            e["churn"] = (a or 0) + (d or 0)
        self._all = rows
        self._apply_filter()

    def _apply_filter(self):
        rows = getattr(self, "_all", [])
        keep = []
        for e in rows:
            kind = e.get("kind", "snapshot")
            if _is_seal(kind):
                ok = self.ck_seal.isChecked()
            elif kind == "autosnap":
                ok = self.ck_auto.isChecked()
            else:
                ok = self.ck_snap.isChecked()
            if ok:
                keep.append(e)
        self._entries = keep
        self.band.set_entries(keep)
        self._clear_detail()
        self.lbl_hint.setText(
            f"{len(keep)} state(s).  Wheel to zoom · drag to pan · click a bar. "
            f"Bar height = lines changed." if keep else "No states to show.")

    # ------------------------------------------------------------- detail
    def _on_band_selected(self, index):
        if not (0 <= index < len(self._entries)):
            return
        e = self._entries[index]
        files, adds, dels = _summary(e)
        self.lbl_sel.setText(
            f"{_fmt(e['epoch'])} · {e.get('kind', 'snapshot')} · {_ago(e['epoch'])}"
            f"   —   {files}  +{adds} −{dels}")
        self._fill_files(e)

    def _fill_files(self, entry):
        rows = entry.get("files") or []
        self.tbl_files.setRowCount(len(rows))
        for i, (status, path, adds, dels) in enumerate(rows):
            self.tbl_files.setItem(i, 0, QTableWidgetItem(status))
            it_path = QTableWidgetItem(path)
            it_path.setData(Qt.UserRole, (entry.get("sha"), entry.get("parent"),
                                          path, adds, dels))
            self.tbl_files.setItem(i, 1, it_path)
            churn = "binary" if adds is None and dels is None else f"+{adds or 0} −{dels or 0}"
            self.tbl_files.setItem(i, 2, QTableWidgetItem(churn))
        if rows:
            self.tbl_files.selectRow(0)
        else:
            self.diff.setPlainText("")

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
        threading.Thread(target=work, name="sincrogit-tl2-diff", daemon=True).start()

    def _on_diff_ready(self, gen, html):
        self.busy.stop()
        if gen != self._diff_gen:
            return
        self.diff.setHtml(html)

    def _clear_detail(self):
        self.tbl_files.setRowCount(0)
        self.diff.setPlainText("")
        self.lbl_sel.setText("Select a bar above to see what changed.")
