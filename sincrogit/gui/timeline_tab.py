"""Timeline tab: the snapshots the Log deliberately doesn't list.

The event Log stays readable because the ~5-min snapshots are not logged one
by one — this tab is where they live instead: per repo, a vertical timeline
(rail + dots, grouped by day) of every snapshot and seal, what files each one
captured, and each file's colored diff vs the snapshot's parent.

Threading: every git read (the timeline walk, each file diff) runs on a
worker thread and returns via a queued signal, guarded by a generation token
so a slow result never overwrites a newer selection — the same pattern as the
history/time-machine dialogs. The tab reloads itself when a snapshot/seal
event for the shown repo arrives while visible (debounced), or on next show.
"""

import datetime
import threading

from PyQt5.QtCore import QRectF, QSize, Qt, QTimer, pyqtSignal
from PyQt5.QtGui import QColor, QFont, QFontMetrics, QPainter, QPen
from PyQt5.QtWidgets import (
    QComboBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QSplitter,
    QStyle,
    QStyledItemDelegate,
    QTableWidget,
    QTableWidgetItem,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from .diff import diff_html

ROLE_ENTRY = Qt.UserRole        # the timeline entry dict (None on day headers)
ROLE_DAY = Qt.UserRole + 1      # day-header label ("Today — Wed 16 Jul")

_RAIL_X = 26                    # x of the vertical rail inside each card
_TEXT_X = _RAIL_X + 18          # where the card text starts
_CARD_H = 46
_HEADER_H = 26


def _fmt_time(epoch: float) -> str:
    return datetime.datetime.fromtimestamp(epoch).strftime("%H:%M")


def _day_label(epoch: float) -> str:
    d = datetime.date.fromtimestamp(epoch)
    today = datetime.date.today()
    name = d.strftime("%a %d %b %Y")
    if d == today:
        return f"Today — {name}"
    if d == today - datetime.timedelta(days=1):
        return f"Yesterday — {name}"
    return name


def _summary(entry: dict) -> tuple:
    """(files_text, adds, dels) totals of one timeline entry."""
    files = entry.get("files") or []
    adds = sum(a for _s, _p, a, _d in files if a)
    dels = sum(d for _s, _p, _a, d in files if d)
    n = len(files)
    return (f"{n} file{'s' if n != 1 else ''}", adds, dels)


class _TimelineDelegate(QStyledItemDelegate):
    """Paints the timeline: a vertical rail with one dot per state (snapshot =
    small accent dot, seal = larger success dot), day headers as muted
    captions, and per-card summary text. Pure painting — all data comes from
    the item's roles, so the offscreen tests exercise the same items."""

    def __init__(self, pal: dict, parent=None):
        super().__init__(parent)
        p = pal or {}
        self._rail = QColor(p.get("border", "#d7dce3"))
        self._muted = QColor(p.get("muted", "#6b7280"))
        self._text = QColor(p.get("text", "#1f2430"))
        self._accent = QColor(p.get("accent", "#2e7dd1"))
        self._ok = QColor(p.get("success", "#2e9e5b"))
        self._bad = QColor(p.get("danger", "#d23f3f"))
        self._sel = QColor(p.get("sel_bg", "#dce9f7"))

    def sizeHint(self, option, index):
        if index.data(ROLE_ENTRY) is None:
            return QSize(option.rect.width(), _HEADER_H)
        return QSize(option.rect.width(), _CARD_H)

    def paint(self, painter: QPainter, option, index):
        painter.save()
        painter.setRenderHint(QPainter.Antialiasing, True)
        r = option.rect
        entry = index.data(ROLE_ENTRY)

        if entry is None:  # ---- day header ----
            f = QFont(option.font)
            f.setBold(True)
            painter.setFont(f)
            painter.setPen(self._muted)
            label = index.data(ROLE_DAY) or ""
            painter.drawText(r.adjusted(8, 0, -8, 0),
                             Qt.AlignVCenter | Qt.AlignLeft, label)
            fm = QFontMetrics(f)
            x0 = 8 + fm.horizontalAdvance(label) + 10
            painter.setPen(QPen(self._rail, 1))
            painter.drawLine(x0, r.center().y(), r.right() - 8, r.center().y())
            painter.restore()
            return

        # ---- card: selection wash, rail, dot, texts ----
        if option.state & QStyle.State_Selected:
            painter.fillRect(r, self._sel)
        painter.setPen(QPen(self._rail, 2))
        painter.drawLine(_RAIL_X, r.top(), _RAIL_X, r.bottom() + 1)

        is_seal = entry.get("kind") == "seal"
        dot = self._ok if is_seal else self._accent
        radius = 6 if is_seal else 4
        painter.setPen(Qt.NoPen)
        painter.setBrush(dot)
        painter.drawEllipse(QRectF(_RAIL_X - radius, r.center().y() - radius,
                                   2 * radius, 2 * radius))

        files_text, adds, dels = _summary(entry)
        y1 = r.top() + int(_CARD_H * 0.42)
        f = QFont(option.font)
        f.setBold(True)
        painter.setFont(f)
        painter.setPen(self._text)
        time_txt = _fmt_time(entry["epoch"])
        painter.drawText(_TEXT_X, y1, time_txt)
        x = _TEXT_X + QFontMetrics(f).horizontalAdvance(time_txt) + 10

        painter.setFont(option.font)
        fm = QFontMetrics(option.font)
        painter.setPen(self._muted)
        kind_txt = "seal" if is_seal else "snapshot"
        seg = f"{kind_txt}  ·  {files_text}"
        painter.drawText(x, y1, seg)
        x += fm.horizontalAdvance(seg) + 10
        if adds:
            painter.setPen(self._ok)
            seg = f"+{adds}"
            painter.drawText(x, y1, seg)
            x += fm.horizontalAdvance(seg) + 6
        if dels:
            painter.setPen(self._bad)
            painter.drawText(x, y1, f"−{dels}")

        # Second line: the seal's subject, or a preview of the files touched.
        if is_seal:
            line2 = entry.get("subject") or ""
        else:
            names = [p.rsplit("/", 1)[-1] for _s, p, _a, _d in entry["files"]]
            line2 = ", ".join(names[:3]) + (", …" if len(names) > 3 else "")
        painter.setPen(self._muted)
        y2 = r.top() + int(_CARD_H * 0.8)
        painter.drawText(_TEXT_X, y2,
                         fm.elidedText(line2, Qt.ElideRight,
                                       r.width() - _TEXT_X - 8))
        painter.restore()


class TimelineTab(QWidget):
    """Duck-typed controller: repo_list(), snapshot_timeline(name, limit),
    file_text_at(name, rel, sha), theme (palette dict)."""

    _loaded = pyqtSignal(int, list)      # gen, entries
    _diff_ready = pyqtSignal(int, str)   # gen, html

    def __init__(self, controller, parent=None):
        super().__init__(parent)
        self.c = controller
        self._pal = getattr(controller, "theme", None) or {}
        self._gen = 0            # timeline loads
        self._diff_gen = 0       # per-file diff loads
        self._cache = []         # last loaded entries (for the filter combo)
        self._loaded_once = False
        self._stale = True       # reload on next show

        v = QVBoxLayout(self)
        v.setContentsMargins(8, 8, 8, 8)

        top = QHBoxLayout()
        top.addWidget(QLabel("Repo:"))
        self.cb_repo = QComboBox()
        self.cb_repo.currentIndexChanged.connect(self._reload)
        top.addWidget(self.cb_repo)
        self.cb_filter = QComboBox()
        self.cb_filter.addItem("All activity", "all")
        self.cb_filter.addItem("Seals only", "seal")
        self.cb_filter.currentIndexChanged.connect(self._render)
        top.addWidget(self.cb_filter)
        top.addStretch(1)
        self.lbl_count = QLabel("")
        self.lbl_count.setProperty("cssClass", "muted")
        top.addWidget(self.lbl_count)
        self.btn_refresh = QPushButton("Refresh")
        self.btn_refresh.clicked.connect(self._reload)
        top.addWidget(self.btn_refresh)
        v.addLayout(top)

        split = QSplitter(Qt.Horizontal)

        self.lst = QListWidget()
        self.lst.setItemDelegate(_TimelineDelegate(self._pal, self.lst))
        self.lst.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.lst.currentItemChanged.connect(self._on_state_selected)
        split.addWidget(self.lst)

        right = QSplitter(Qt.Vertical)
        self.tbl_files = QTableWidget(0, 4)
        self.tbl_files.setHorizontalHeaderLabels(["", "File", "+", "−"])
        hdr = self.tbl_files.horizontalHeader()
        hdr.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        hdr.setSectionResizeMode(1, QHeaderView.Stretch)
        hdr.setSectionResizeMode(2, QHeaderView.ResizeToContents)
        hdr.setSectionResizeMode(3, QHeaderView.ResizeToContents)
        self.tbl_files.verticalHeader().setVisible(False)
        self.tbl_files.setSelectionBehavior(QTableWidget.SelectRows)
        self.tbl_files.setSelectionMode(QTableWidget.SingleSelection)
        self.tbl_files.setEditTriggers(QTableWidget.NoEditTriggers)
        self.tbl_files.setShowGrid(False)
        self.tbl_files.itemSelectionChanged.connect(self._on_file_selected)
        right.addWidget(self.tbl_files)

        diff_box = QWidget()
        dv = QVBoxLayout(diff_box)
        dv.setContentsMargins(0, 0, 0, 0)
        self.lbl_diff = QLabel("")
        self.lbl_diff.setProperty("cssClass", "muted")
        dv.addWidget(self.lbl_diff)
        self.diff = QTextEdit()
        self.diff.setReadOnly(True)
        self.diff.setFont(QFont("Consolas", 10))
        self.diff.setLineWrapMode(QTextEdit.NoWrap)
        dv.addWidget(self.diff)
        right.addWidget(diff_box)
        right.setSizes([220, 320])

        split.addWidget(right)
        split.setSizes([380, 520])
        split.setStretchFactor(0, 0)
        split.setStretchFactor(1, 1)
        v.addWidget(split, 1)

        self._loaded.connect(self._on_loaded)
        self._diff_ready.connect(self._on_diff_ready)
        self._debounce = QTimer(self)
        self._debounce.setSingleShot(True)
        self._debounce.setInterval(800)   # collapse a burst of snapshot events
        self._debounce.timeout.connect(self._reload)

    # ------------------------------------------------------------- lifecycle
    def showEvent(self, e):
        super().showEvent(e)
        self._sync_repos()
        if self._stale or not self._loaded_once:
            self._reload()

    def notice_event(self, ev):
        """Panel hook: a structured engine event arrived (GUI thread). New
        snapshots/seals for the shown repo refresh the timeline — debounced
        while visible, deferred to the next show otherwise."""
        if getattr(ev, "action", "") not in ("snapshot", "seal", "handoff", "pull"):
            return
        self._stale = True
        if self.isVisible() and getattr(ev, "repo", "") == self.cb_repo.currentText():
            self._debounce.start()

    def _sync_repos(self):
        names = [n for n, _p in self.c.repo_list()]
        have = [self.cb_repo.itemText(i) for i in range(self.cb_repo.count())]
        if names != have:
            self.cb_repo.blockSignals(True)
            current = self.cb_repo.currentText()
            self.cb_repo.clear()
            self.cb_repo.addItems(names)
            if current in names:
                self.cb_repo.setCurrentText(current)
            self.cb_repo.blockSignals(False)

    # ------------------------------------------------------------------ load
    def _reload(self):
        name = self.cb_repo.currentText()
        if not name:
            self.lst.clear()
            self.lbl_count.setText("no repos configured")
            return
        self._stale = False
        self._loaded_once = True
        self._gen += 1
        gen = self._gen
        self.lbl_count.setText("loading…")

        def work():
            try:
                entries = self.c.snapshot_timeline(name)
            except Exception:  # noqa: BLE001 — surfaced as an empty timeline
                entries = []
            try:
                self._loaded.emit(gen, entries)
            except RuntimeError:
                pass  # tab destroyed while loading

        threading.Thread(target=work, name="sincrogit-timeline", daemon=True).start()

    def _on_loaded(self, gen, entries):
        if gen != self._gen:
            return  # a newer load superseded this one
        self._cache = entries
        n_snap = sum(1 for e in entries if e["kind"] == "snapshot")
        n_seal = len(entries) - n_snap
        self.lbl_count.setText(f"{n_snap} snapshot(s) · {n_seal} seal(s)")
        self._render()

    def _render(self):
        """Rebuild the list from the cached entries (filter is local)."""
        want = self.cb_filter.currentData() or "all"
        self.lst.blockSignals(True)
        self.lst.clear()
        last_day = None
        for e in self._cache:
            if want == "seal" and e["kind"] != "seal":
                continue
            if not e["files"]:
                continue  # anchors / empty states: nothing to show
            day = datetime.date.fromtimestamp(e["epoch"])
            if day != last_day:
                last_day = day
                hdr = QListWidgetItem()
                hdr.setData(ROLE_ENTRY, None)
                hdr.setData(ROLE_DAY, _day_label(e["epoch"]))
                hdr.setFlags(Qt.ItemIsEnabled)  # not selectable
                self.lst.addItem(hdr)
            it = QListWidgetItem()
            it.setData(ROLE_ENTRY, e)
            self.lst.addItem(it)
        self.lst.blockSignals(False)
        # Select the newest state so the right side is never blank.
        for i in range(self.lst.count()):
            if self.lst.item(i).data(ROLE_ENTRY) is not None:
                self.lst.setCurrentRow(i)
                break
        else:
            self.tbl_files.setRowCount(0)
            self.diff.clear()
            self.lbl_diff.setText("")

    # ------------------------------------------------------------- selection
    def _selected_entry(self):
        it = self.lst.currentItem()
        return it.data(ROLE_ENTRY) if it else None

    def _on_state_selected(self, *_):
        e = self._selected_entry()
        self.tbl_files.setRowCount(0)
        self.diff.clear()
        self.lbl_diff.setText("")
        if not e:
            return
        ok = QColor(self._pal.get("success", "#2e9e5b"))
        warn = QColor(self._pal.get("warning", "#a87900"))
        bad = QColor(self._pal.get("danger", "#d23f3f"))
        muted = QColor(self._pal.get("muted", "#6b7280"))
        status_color = {"A": ok, "M": warn, "D": bad}
        self.tbl_files.setRowCount(len(e["files"]))
        for row, (s, path, adds, dels) in enumerate(e["files"]):
            it = QTableWidgetItem(s)
            it.setForeground(status_color.get(s, muted))
            self.tbl_files.setItem(row, 0, it)
            self.tbl_files.setItem(row, 1, QTableWidgetItem(path))
            if adds is None and dels is None:
                bin_it = QTableWidgetItem("bin")
                bin_it.setForeground(muted)
                self.tbl_files.setItem(row, 2, bin_it)
                self.tbl_files.setItem(row, 3, QTableWidgetItem(""))
            else:
                a_it = QTableWidgetItem(f"+{adds or 0}")
                a_it.setForeground(ok)
                d_it = QTableWidgetItem(f"−{dels or 0}")
                d_it.setForeground(bad)
                self.tbl_files.setItem(row, 2, a_it)
                self.tbl_files.setItem(row, 3, d_it)
        if e["files"]:
            self.tbl_files.selectRow(0)

    def _on_file_selected(self):
        e = self._selected_entry()
        row = self.tbl_files.currentRow()
        if not e or row < 0 or row >= len(e["files"]):
            return
        status, path, adds, dels = e["files"][row]
        when = _fmt_time(e["epoch"])
        self.lbl_diff.setText(f"{path}  —  changes captured at {when}")
        if adds is None and dels is None:
            self.diff.setPlainText("(binary file — no text diff)")
            return
        name = self.cb_repo.currentText()
        sha, parent = e["sha"], e["parent"]
        dark = bool(self._pal.get("is_dark"))
        self._diff_gen += 1
        gen = self._diff_gen

        def work():
            try:
                old = (self.c.file_text_at(name, path, parent) or "") if parent else ""
                new = self.c.file_text_at(name, path, sha) or ""
                html = diff_html(old, new, dark=dark,
                                 from_label="before", to_label="this snapshot")
            except Exception as ex:  # noqa: BLE001 — surfaced in the pane
                html = f"<pre>could not load the diff: {ex}</pre>"
            try:
                self._diff_ready.emit(gen, html)
            except RuntimeError:
                pass  # tab destroyed while loading

        threading.Thread(target=work, name="sincrogit-timeline-diff",
                         daemon=True).start()

    def _on_diff_ready(self, gen, html):
        if gen != self._diff_gen:
            return  # user already picked another file
        self.diff.setHtml(html)
