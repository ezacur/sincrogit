"""Time machine tab: every view of a repo's past, unified.

One grid with two axes — WHEN (a day-grouped rail of snapshots, seals and
fetched autosnap mirrors) × WHAT (the files of the selected state) — plus a
compare switch that changes the question being answered:

  - "What changed then"  (default): each state's files vs its PARENT — the
    diary the old Timeline tab showed.
  - "vs today": each state's files vs the CURRENT worktree, with checkboxes —
    the restore console the old Time machine dialog was (selective restore,
    whole-repo restore with preview, ⚠ uncapturable files unselectable).

Pinning a FILE (double-click it, or Browse…) flips the WHEN axis to that
file's own versions — the old File history dialog: search across versions,
Save a copy, restore the file, restore hunks.

Threading contract (same as everywhere): every git read/write runs on a worker
and returns via a queued signal guarded by a generation token; the GUI thread
never reads disk, never spawns git, never waits on the engine's locks.
"""

import datetime
import os
import threading
import time

from PyQt5.QtCore import QRectF, QSize, Qt, QTimer, pyqtSignal
from PyQt5.QtGui import QColor, QFont, QFontMetrics, QPainter, QPen
from PyQt5.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFileDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QRadioButton,
    QSplitter,
    QStyle,
    QStyledItemDelegate,
    QTableWidget,
    QTableWidgetItem,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from .busy import BusyBar
from .diff import diff_html, diff_html_sbs

ROLE_ENTRY = Qt.UserRole        # the state/version dict (None on day headers)
ROLE_DAY = Qt.UserRole + 1      # day-header label ("Today — Fri 18 Jul")
ROLE_MARK = Qt.UserRole + 2     # search-transition accent (pinned mode)

_RAIL_X = 26                    # x of the vertical rail inside each card
_TEXT_X = _RAIL_X + 18          # where the card text starts
_CARD_H = 46
_HEADER_H = 26

# Version-type accents and explanations (shared with the machines view).
_TYPE_COLOR = {"sealed": "#2e9e5b", "seal": "#2e9e5b",
               "snapshot": "#6b7280", "autosnap": "#8a63d2"}
_TYPE_TIP = {
    "sealed": "A permanent commit — part of the repo's history (pushed to the remote).",
    "snapshot": "An intra-window snapshot from the reflog (~30 days, this machine only).",
    "autosnap": "Another machine's live mirror (refs/autosnap), fetched from the remote.",
}

_VERB_COLOR = {"revert": "#a87900", "delete": "#d23f3f", "recreate": "#2e9e5b"}
_VERB_TIP = {
    "revert": "Differs — restoring takes it back to this version's content.",
    "delete": "Created since this version — restoring REMOVES it.",
    "recreate": "Deleted since this version — restoring brings it back.",
}


def _fmt(epoch) -> str:
    try:
        return datetime.datetime.fromtimestamp(epoch).strftime("%Y-%m-%d %H:%M:%S")
    except (ValueError, OSError, TypeError):
        return "—"


def _ago(epoch) -> str:
    """Compact relative time ("3 h ago"); the absolute stamp goes in tooltips."""
    try:
        secs = max(0, int(time.time() - epoch))
    except (ValueError, OSError, TypeError):
        return "—"
    if secs < 60:
        return f"{secs} s ago"
    mins = secs // 60
    if mins < 60:
        return f"{mins} min ago"
    hrs, mins = divmod(mins, 60)
    if hrs < 24:
        return f"{hrs} h {mins:02d} m ago"
    days, hrs = divmod(hrs, 24)
    if days < 14:
        return f"{days} d {hrs} h ago"
    return _fmt(epoch)


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


class _RailDelegate(QStyledItemDelegate):
    """Paints the WHEN rail: one dot per state (snapshot = small accent dot,
    seal = larger success dot, autosnap = purple dot), day headers as muted
    captions, per-card summary text, and a search-transition accent (pinned
    mode). Pure painting — all data comes from the item's roles."""

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
        self._auto = QColor(_TYPE_COLOR["autosnap"])
        # Fonts + metrics are CACHED, built once from the first paint's font.
        # Constructing QFont/QFontMetrics on every paint (per visible row, on
        # every hover/scroll/selection) is the classic Qt delegate stall — it
        # made the rail feel sluggish on a real screen. See _ensure_fonts.
        self._fonts_ready = False
        self._bold = None
        self._fm_bold = None
        self._fm = None

    def _ensure_fonts(self, base_font):
        if self._fonts_ready:
            return
        self._bold = QFont(base_font)
        self._bold.setBold(True)
        self._fm_bold = QFontMetrics(self._bold)
        self._fm = QFontMetrics(base_font)
        self._fonts_ready = True

    def sizeHint(self, option, index):
        if index.data(ROLE_ENTRY) is None:
            return QSize(option.rect.width(), _HEADER_H)
        return QSize(option.rect.width(), _CARD_H)

    def _dot(self, kind):
        if kind in ("seal", "sealed"):
            return self._ok, 6
        if kind == "autosnap":
            return self._auto, 6
        return self._accent, 4

    def paint(self, painter: QPainter, option, index):
        self._ensure_fonts(option.font)
        painter.save()
        painter.setRenderHint(QPainter.Antialiasing, True)
        r = option.rect
        entry = index.data(ROLE_ENTRY)

        if entry is None:  # ---- day header ----
            painter.setFont(self._bold)
            painter.setPen(self._muted)
            label = index.data(ROLE_DAY) or ""
            painter.drawText(r.adjusted(8, 0, -8, 0),
                             Qt.AlignVCenter | Qt.AlignLeft, label)
            x0 = 8 + self._fm_bold.horizontalAdvance(label) + 10
            painter.setPen(QPen(self._rail, 1))
            painter.drawLine(x0, r.center().y(), r.right() - 8, r.center().y())
            painter.restore()
            return

        # ---- card: selection wash, rail, dot, texts ----
        if option.state & QStyle.State_Selected:
            painter.fillRect(r, self._sel)
        painter.setPen(QPen(self._rail, 2))
        painter.drawLine(_RAIL_X, r.top(), _RAIL_X, r.bottom() + 1)

        kind = entry.get("kind") or entry.get("source") or "snapshot"
        dot, radius = self._dot(kind)
        painter.setPen(Qt.NoPen)
        painter.setBrush(dot)
        painter.drawEllipse(QRectF(_RAIL_X - radius, r.center().y() - radius,
                                   2 * radius, 2 * radius))

        y1 = r.top() + int(_CARD_H * 0.42)
        painter.setFont(self._bold)
        # A search transition (pinned mode) tints the time text — "the change
        # happened HERE".
        painter.setPen(self._accent if index.data(ROLE_MARK) else self._text)
        time_txt = _fmt_time(entry["epoch"])
        painter.drawText(_TEXT_X, y1, time_txt)
        x = _TEXT_X + self._fm_bold.horizontalAdvance(time_txt) + 10

        painter.setFont(option.font)
        fm = self._fm
        painter.setPen(self._muted)
        kind_txt = {"sealed": "seal"}.get(kind, kind)
        if entry.get("host"):
            kind_txt += f" ({entry['host']})"
        if entry.get("files") is not None:
            files_text, adds, dels = _summary(entry)
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
        else:  # pinned-file version card: no per-state file list
            painter.drawText(x, y1, kind_txt)

        # Second line: the seal's subject / the version's subject / a preview
        # of the files touched.
        if entry.get("files") is not None and kind == "snapshot":
            names = [p.rsplit("/", 1)[-1] for _s, p, _a, _d in entry["files"]]
            line2 = ", ".join(names[:3]) + (", …" if len(names) > 3 else "")
        else:
            line2 = entry.get("subject") or ""
        painter.setPen(self._muted)
        y2 = r.top() + int(_CARD_H * 0.8)
        painter.drawText(_TEXT_X, y2,
                         fm.elidedText(line2, Qt.ElideRight,
                                       r.width() - _TEXT_X - 8))
        painter.restore()


class TimeMachineTab(QWidget):
    """Duck-typed controller: repo_list(), snapshot_timeline(name, limit),
    file_history(name, rel), restore_repo_preview(name, sha), restore_repo,
    restore_files, restore_file, file_text_at, current_text,
    export_file_version, search_in_file_versions, fetch_autosnaps, theme —
    and the hunk picker's contract (file_hunks / restore_hunks)."""

    MAX_FILE_ROWS = 4000   # QUÉ table cap ("vs today" can differ in 10k+ paths)

    _timeline_loaded = pyqtSignal(int, list)             # gen, entries
    _versions_loaded = pyqtSignal(int, str, list)        # gen, rel, versions
    _today_ready = pyqtSignal(int, bool, object, str)    # gen, ok, payload|msg, sha
    _diff_ready = pyqtSignal(int, str)                   # gen, html
    _search_ready = pyqtSignal(str, str, list)           # rel, term, [(sha, n)]
    _fetch_done = pyqtSignal(bool, int, str, str)        # ok, count, repo, err
    _restore_done = pyqtSignal(bool, str)                # ok, message
    _preview_ready = pyqtSignal(bool, object, str, str)  # ok, payload|msg, sha, when
    _export_done = pyqtSignal(bool, str, str)            # ok, message, dest

    def __init__(self, controller, parent=None):
        super().__init__(parent)
        self.c = controller
        self._pal = getattr(controller, "theme", None) or {}
        self._gen = 0            # WHEN-axis loads (timeline or file versions)
        self._files_gen = 0      # "vs today" file computations
        self._diff_gen = 0       # per-file diff loads
        self._cache = []         # last loaded WHEN entries
        self._files = []         # rows of the QUÉ table: (verb_or_status, path, adds, dels)
        self._risky = set()      # unselectable paths ("vs today")
        self._pinned = None      # relpath of the pinned file, or None
        self._loaded_once = False
        self._stale = True

        v = QVBoxLayout(self)
        v.setContentsMargins(8, 8, 8, 8)

        # ---------------------------------------------------------- toolbar
        top = QHBoxLayout()
        top.addWidget(QLabel("Repo:"))
        self.cb_repo = QComboBox()
        self.cb_repo.currentIndexChanged.connect(self._repo_changed)
        top.addWidget(self.cb_repo)
        self.lbl_pin = QLabel("")
        self.lbl_pin.setProperty("cssClass", "muted")
        top.addWidget(self.lbl_pin)
        self.btn_browse = QPushButton("Pin a file…")
        self.btn_browse.setToolTip(
            "Follow ONE file through time: its versions, a search across them, "
            "per-file restore. Double-clicking a file below pins it too.")
        self.btn_browse.clicked.connect(self._browse_pin)
        top.addWidget(self.btn_browse)
        self.btn_unpin = QPushButton("Unpin ✕")
        self.btn_unpin.clicked.connect(lambda: self._set_pin(None))
        self.btn_unpin.setVisible(False)
        top.addWidget(self.btn_unpin)
        top.addStretch(1)
        self.lbl_count = QLabel("")
        self.lbl_count.setProperty("cssClass", "muted")
        top.addWidget(self.lbl_count)
        self.btn_fetch = QPushButton("Fetch autosnaps")
        self.btn_fetch.setToolTip(
            "Download your other machines' live mirrors so their states appear "
            "on the rail (disaster recovery).")
        self.btn_fetch.clicked.connect(self._fetch_autosnaps)
        top.addWidget(self.btn_fetch)
        v.addLayout(top)

        # --------------------------------------------------------- mode bar
        mode = QHBoxLayout()
        mode.addWidget(QLabel("Compare:"))
        self.rb_then = QRadioButton("what changed then")
        self.rb_then.setToolTip("Each state's files vs its parent — the diary.")
        self.rb_then.setChecked(True)
        self.rb_today = QRadioButton("vs today")
        self.rb_today.setToolTip(
            "Each state's files vs the CURRENT worktree — pick files to "
            "restore, or restore everything.")
        self.rb_then.toggled.connect(self._mode_changed)
        mode.addWidget(self.rb_then)
        mode.addWidget(self.rb_today)
        self.cb_filter = QComboBox()
        self.cb_filter.addItem("All activity", "all")
        self.cb_filter.addItem("Seals only", "seal")
        self.cb_filter.currentIndexChanged.connect(self._render)
        mode.addWidget(self.cb_filter)
        mode.addStretch(1)
        self.ed_search = QLineEdit()
        self.ed_search.setPlaceholderText("find text across versions…")
        self.ed_search.returnPressed.connect(self._find_in_versions)
        self.ed_search.setVisible(False)
        self.ed_search.setMaximumWidth(320)
        mode.addWidget(self.ed_search, 1)
        self.btn_search = QPushButton("Find")
        self.btn_search.setToolTip(
            "Count the text in every version and highlight where it appeared, "
            "changed or vanished.")
        self.btn_search.clicked.connect(self._find_in_versions)
        self.btn_search.setVisible(False)
        mode.addWidget(self.btn_search)
        v.addLayout(mode)

        # ------------------------------------------------------------ panes
        split = QSplitter(Qt.Horizontal)
        self.lst = QListWidget()
        self.lst.setItemDelegate(_RailDelegate(self._pal, self.lst))
        self.lst.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.lst.currentItemChanged.connect(self._on_state_selected)
        split.addWidget(self.lst)

        right = QSplitter(Qt.Vertical)
        files_box = QWidget()
        fv = QVBoxLayout(files_box)
        fv.setContentsMargins(0, 0, 0, 0)
        frow = QHBoxLayout()
        self.lbl_files = QLabel("")
        self.lbl_files.setProperty("cssClass", "muted")
        frow.addWidget(self.lbl_files, 1)
        self.btn_all = QPushButton("Select all")
        self.btn_all.clicked.connect(lambda: self._set_all(Qt.Checked))
        self.btn_none = QPushButton("Select none")
        self.btn_none.clicked.connect(lambda: self._set_all(Qt.Unchecked))
        frow.addWidget(self.btn_all)
        frow.addWidget(self.btn_none)
        fv.addLayout(frow)
        self.tbl_files = QTableWidget(0, 5)
        fhdr = self.tbl_files.horizontalHeader()
        fhdr.setResizeContentsPrecision(64)
        self.tbl_files.setSelectionBehavior(QTableWidget.SelectRows)
        self.tbl_files.setSelectionMode(QTableWidget.SingleSelection)
        self.tbl_files.setEditTriggers(QTableWidget.NoEditTriggers)
        self.tbl_files.setShowGrid(False)
        self.tbl_files.verticalHeader().setVisible(False)
        self.tbl_files.itemSelectionChanged.connect(self._load_diff)
        self.tbl_files.itemChanged.connect(self._sync_actions)
        self.tbl_files.doubleClicked.connect(self._pin_from_row)
        fv.addWidget(self.tbl_files)
        right.addWidget(files_box)
        self._files_box = files_box

        diff_box = QWidget()
        dv = QVBoxLayout(diff_box)
        dv.setContentsMargins(0, 0, 0, 0)
        drow = QHBoxLayout()
        self.lbl_diff = QLabel("")
        self.lbl_diff.setProperty("cssClass", "muted")
        drow.addWidget(self.lbl_diff, 1)
        self.cb_content = QCheckBox("Content only")
        self.cb_content.setToolTip("Show the version's raw text instead of a diff.")
        self.cb_content.stateChanged.connect(self._load_diff)
        self.cb_content.setVisible(False)
        drow.addWidget(self.cb_content)
        self.cb_sbs = QCheckBox("Side-by-side")
        self.cb_sbs.stateChanged.connect(self._load_diff)
        drow.addWidget(self.cb_sbs)
        dv.addLayout(drow)
        self.diff = QTextEdit()
        self.diff.setReadOnly(True)
        self.diff.setFont(QFont("Consolas", 10))
        self.diff.setLineWrapMode(QTextEdit.NoWrap)
        dv.addWidget(self.diff)
        right.addWidget(diff_box)
        right.setSizes([220, 320])

        split.addWidget(right)
        split.setSizes([380, 540])
        split.setStretchFactor(0, 0)
        split.setStretchFactor(1, 1)
        v.addWidget(split, 1)

        # A visible "working…" bar: this tab runs the rail load and a file diff
        # (and more) on workers CONCURRENTLY, so the ref-counted BusyBar is
        # exactly right — it stays up until the last worker of a burst finishes.
        self.busy = BusyBar()
        v.addWidget(self.busy)

        # ------------------------------------------------------ action row
        act = QHBoxLayout()
        self.lbl_info = QLabel("")
        self.lbl_info.setProperty("cssClass", "muted")
        act.addWidget(self.lbl_info, 1)
        self.btn_saveas = QPushButton("Save a copy…")
        self.btn_saveas.setToolTip(
            "Write this file's version to a NEW file — recover it under "
            "another name; nothing is overwritten.")
        self.btn_saveas.clicked.connect(self._save_copy)
        act.addWidget(self.btn_saveas)
        self.btn_restore_file = QPushButton("Restore file")
        self.btn_restore_file.clicked.connect(self._restore_file)
        act.addWidget(self.btn_restore_file)
        self.btn_hunks = QPushButton("Restore hunks…")
        self.btn_hunks.setToolTip(
            "Roll back only SOME of the changed blocks, keeping your other "
            "current edits. Text files only.")
        self.btn_hunks.clicked.connect(self._restore_hunks)
        act.addWidget(self.btn_hunks)
        self.btn_restore_sel = QPushButton("Restore selected")
        self.btn_restore_sel.setProperty("cssClass", "primary")
        self.btn_restore_sel.clicked.connect(self._restore_selected)
        act.addWidget(self.btn_restore_sel)
        self.btn_restore_repo = QPushButton("Restore ENTIRE repo…")
        self.btn_restore_repo.setProperty("cssClass", "danger")
        self.btn_restore_repo.setToolTip(
            "Set every file back to this state (including deletions). "
            "Reversible: it's captured as a new snapshot.")
        self.btn_restore_repo.clicked.connect(self._restore_repo)
        act.addWidget(self.btn_restore_repo)
        v.addLayout(act)

        for s, h in ((self._timeline_loaded, self._on_timeline_loaded),
                     (self._versions_loaded, self._on_versions_loaded),
                     (self._today_ready, self._on_today_ready),
                     (self._diff_ready, self._on_diff_ready),
                     (self._search_ready, self._on_search_ready),
                     (self._fetch_done, self._on_fetch_done),
                     (self._restore_done, self._on_restore_done),
                     (self._preview_ready, self._on_preview_ready),
                     (self._export_done, self._on_export_done)):
            s.connect(h)
        self._debounce = QTimer(self)
        self._debounce.setSingleShot(True)
        self._debounce.setInterval(800)   # collapse a burst of snapshot events
        self._debounce.timeout.connect(self._reload)
        self._apply_mode_ui()

    # ------------------------------------------------------------ public API
    def focus_repo(self, name=None, pin=None):
        """Jump here from Status (button / context menu / double-click):
        select `name` and optionally pin a file."""
        self._sync_repos()
        if name:
            i = self.cb_repo.findText(name)
            if i >= 0 and i != self.cb_repo.currentIndex():
                self.cb_repo.setCurrentIndex(i)  # triggers _repo_changed
        self._set_pin(pin)
        if self._stale or not self._loaded_once:
            self._reload()

    def notice_event(self, ev):
        """Panel hook (GUI thread): refresh only when new states land for the
        repo CURRENTLY SHOWN. An event on another repo used to mark the view
        stale too, so returning to the tab reloaded needlessly — with 5 repos
        and a busy daemon the rail was almost always 'stale' and every visit
        paid a fresh load. Other repos reload on demand when selected anyway."""
        if getattr(ev, "action", "") not in ("snapshot", "seal", "handoff", "pull"):
            return
        if getattr(ev, "repo", "") != self.cb_repo.currentText():
            return  # not the shown repo: switching to it will reload if needed
        self._stale = True
        if self.isVisible():
            self._debounce.start()

    # ------------------------------------------------------------- lifecycle
    def showEvent(self, e):
        super().showEvent(e)
        self._sync_repos()
        if self._stale or not self._loaded_once:
            self._reload()

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
        """Everything on screen belongs to the previous repo: clear and reload."""
        self._pinned = None
        self._apply_mode_ui()
        self._clear_right("")
        self._reload()

    # ------------------------------------------------------------ mode / pin
    def _mode(self) -> str:
        return "today" if self.rb_today.isChecked() else "then"

    def _mode_changed(self, *_):
        self._apply_mode_ui()
        self._on_state_selected()  # recompute the QUÉ pane for the new question

    def _apply_mode_ui(self):
        """Show only the controls that make sense for the current mode/pin.
        Defaults reproduce the old Timeline exactly — extra power stays out of
        the way until asked for."""
        pinned = self._pinned is not None
        today = self._mode() == "today"
        self.btn_unpin.setVisible(pinned)
        self.lbl_pin.setText(f"📌 {self._pinned}" if pinned else "")
        self.ed_search.setVisible(pinned)
        self.btn_search.setVisible(pinned)
        self.cb_content.setVisible(pinned)
        self.cb_filter.setVisible(not pinned)
        self._files_box.setVisible(not pinned)   # pinned: the file IS the QUÉ
        self.btn_all.setVisible(not pinned and today)
        self.btn_none.setVisible(not pinned and today)
        self.btn_restore_sel.setVisible(not pinned and today)
        self.btn_restore_file.setVisible(pinned or not today)
        self.btn_hunks.setVisible(pinned)
        if not pinned:
            self.tbl_files.setColumnCount(5)
            if today:
                self.tbl_files.setHorizontalHeaderLabels(["", "Action", "File", "", ""])
            else:
                self.tbl_files.setHorizontalHeaderLabels(["Δ", "File", "+", "−", ""])
            hdr = self.tbl_files.horizontalHeader()
            hdr.setSectionResizeMode(0, QHeaderView.ResizeToContents)
            stretch_col = 2 if today else 1
            for col in range(1, 5):
                hdr.setSectionResizeMode(
                    col, QHeaderView.Stretch if col == stretch_col
                    else QHeaderView.ResizeToContents)
        self._sync_actions()

    def _set_pin(self, rel):
        rel = rel.replace("\\", "/") if rel else None
        if rel == self._pinned:
            return
        self._pinned = rel
        self._apply_mode_ui()
        self._clear_right("")
        self._reload()

    def _browse_pin(self):
        base = self.cb_repo.currentData() or ""
        chosen, _ = QFileDialog.getOpenFileName(self, "Follow a file through time", base)
        if not chosen:
            return
        try:
            rel = os.path.relpath(os.path.abspath(chosen), os.path.abspath(base))
        except ValueError:
            rel = ""
        if not rel or rel.startswith(".."):
            QMessageBox.warning(self, "Time machine",
                                "The chosen file is not inside the selected repo.")
            return
        self._set_pin(rel)

    def _pin_from_row(self, _index):
        row = self.tbl_files.currentRow()
        if 0 <= row < len(self._files):
            self._set_pin(self._files[row][1])

    # ---------------------------------------------------------- WHEN loading
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
        pinned = self._pinned

        def work():
            try:
                if pinned:
                    versions = self.c.file_history(name, pinned)
                else:
                    versions = self.c.snapshot_timeline(name)
            except Exception:  # noqa: BLE001 — an empty rail reads as "none"
                versions = []
            try:
                if pinned:
                    self._versions_loaded.emit(gen, pinned, versions)
                else:
                    self._timeline_loaded.emit(gen, versions)
            except RuntimeError:
                pass  # tab destroyed while loading

        self.busy.start("Loading the file's versions…" if pinned
                        else "Loading the timeline…")
        threading.Thread(target=work, name="sincrogit-tm-when", daemon=True).start()

    def _on_timeline_loaded(self, gen, entries):
        self.busy.stop()  # unconditional: one dispatch fired exactly one handler
        if gen != self._gen or self._pinned:
            return
        self._cache = entries
        n_snap = sum(1 for e in entries if e["kind"] == "snapshot")
        n_seal = sum(1 for e in entries if e["kind"] == "seal")
        n_auto = len(entries) - n_snap - n_seal
        txt = f"{n_snap} snapshot(s) · {n_seal} seal(s)"
        if n_auto:
            txt += f" · {n_auto} autosnap(s)"
        self.lbl_count.setText(txt)
        self._render()

    def _on_versions_loaded(self, gen, rel, versions):
        self.busy.stop()  # unconditional (see _on_timeline_loaded)
        if gen != self._gen or rel != self._pinned:
            return
        # Normalize to card dicts (kind from source; no per-state file list).
        self._cache = [{"sha": v["sha"], "epoch": v["epoch"],
                        "subject": v["subject"], "kind": v.get("source", "snapshot")}
                       for v in versions]
        self.lbl_count.setText(f"{len(versions)} version(s) of '{rel}'"
                               if versions else f"no history for '{rel}'")
        self._render()

    def _render(self):
        """Rebuild the rail from the cached entries (filters are local)."""
        want = self.cb_filter.currentData() or "all"
        self.lst.blockSignals(True)
        self.lst.clear()
        last_day = None
        for e in self._cache:
            if not self._pinned:
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
            it.setToolTip(f"{_fmt(e['epoch'])} — {_TYPE_TIP.get(e['kind'], '')}")
            self.lst.addItem(it)
        self.lst.blockSignals(False)
        for i in range(self.lst.count()):
            if self.lst.item(i).data(ROLE_ENTRY) is not None:
                self.lst.setCurrentRow(i)
                break
        else:
            self._clear_right("")

    # ------------------------------------------------------------ QUÉ loading
    def _selected_entry(self):
        it = self.lst.currentItem()
        return it.data(ROLE_ENTRY) if it else None

    def _clear_right(self, note):
        self._files, self._risky = [], set()
        self.tbl_files.setRowCount(0)
        self.diff.clear()
        self.lbl_diff.setText("")
        self.lbl_files.setText(note)
        self._sync_actions()

    def _on_state_selected(self, *_):
        e = self._selected_entry()
        if not e:
            self._clear_right("")
            return
        if self._pinned:
            # The file IS the subject: go straight to its diff/content.
            self._files = [("version", self._pinned, None, None)]
            self._load_diff()
            self._sync_actions()
            return
        if self._mode() == "then":
            self._show_then_files(e)
        else:
            self._load_today_files(e)

    def _show_then_files(self, e):
        """QUÉ = what this state captured (local data: no git)."""
        ok = QColor(self._pal.get("success", "#2e9e5b"))
        warn = QColor(self._pal.get("warning", "#a87900"))
        bad = QColor(self._pal.get("danger", "#d23f3f"))
        muted = QColor(self._pal.get("muted", "#6b7280"))
        status_color = {"A": ok, "M": warn, "D": bad}
        self._files = list(e["files"])
        self._risky = set()
        self.lbl_files.setText("files this state captured (vs its parent)")
        self.tbl_files.blockSignals(True)
        self.tbl_files.setUpdatesEnabled(False)  # one repaint, not one per cell
        self.tbl_files.setRowCount(len(self._files))
        for row, (s, path, adds, dels) in enumerate(self._files):
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
            self.tbl_files.setItem(row, 4, QTableWidgetItem(""))
        self.tbl_files.blockSignals(False)
        self.tbl_files.setUpdatesEnabled(True)
        if self._files:
            self.tbl_files.selectRow(0)
        else:
            self.diff.clear()
        self._sync_actions()

    def _load_today_files(self, e):
        """QUÉ = what differs from the current worktree (worker: git diff)."""
        self._files_gen += 1
        gen = self._files_gen
        self._clear_right("Comparing with the current state…")
        name, sha = self.cb_repo.currentText(), e["sha"]

        def work():
            try:
                ok, payload = self.c.restore_repo_preview(name, sha)
            except Exception as ex:  # noqa: BLE001 — surfaced in the pane
                ok, payload = False, str(ex)
            try:
                self._today_ready.emit(gen, ok, payload, sha)
            except RuntimeError:
                pass  # tab destroyed while comparing

        self.busy.start("Comparing with the current state…")
        threading.Thread(target=work, name="sincrogit-tm-today", daemon=True).start()

    def _on_today_ready(self, gen, ok, payload, sha):
        self.busy.stop()
        e = self._selected_entry()
        if gen != self._files_gen or not e or e["sha"] != sha:
            return  # a newer selection superseded this computation
        if not ok:
            self.lbl_files.setText(f"Could not compare: {payload}")
            return
        changes = payload["changes"]
        n = len(changes)
        # Cap the table: an old state can differ in tens of thousands of paths.
        # Announced, never silent; whole-repo restore covers the rest.
        self._files = [(verb, path, None, None)
                       for verb, path in changes[:self.MAX_FILE_ROWS]]
        self._risky = set(payload["risky"])
        extra = f"  (⚠ {len(self._risky)} at risk)" if self._risky else ""
        if n > len(self._files):
            extra += (f"  — showing the first {len(self._files)}; use "
                      f"Restore ENTIRE repo for the rest")
        self.lbl_files.setText(
            f"{n} file(s) differ from the current state{extra}" if n or self._risky
            else "The working tree already matches this state")
        muted = QColor(self._pal.get("muted", "#6b7280"))
        self.tbl_files.blockSignals(True)
        self.tbl_files.setUpdatesEnabled(False)  # one repaint for the whole build
        self.tbl_files.setRowCount(len(self._files))
        for i, (verb, path, _a, _d) in enumerate(self._files):
            chk = QTableWidgetItem("")
            if path in self._risky:
                chk.setFlags(Qt.ItemIsSelectable | Qt.ItemIsEnabled)
                chk.setText("⚠")
                chk.setToolTip(
                    "Current content that snapshots can't capture (excluded, "
                    "over the size limit or binary) — restoring would destroy "
                    "it, so it can't be selected. Copy it somewhere safe first.")
            else:
                chk.setFlags(Qt.ItemIsUserCheckable | Qt.ItemIsSelectable
                             | Qt.ItemIsEnabled)
                chk.setCheckState(Qt.Unchecked)
            self.tbl_files.setItem(i, 0, chk)
            it_verb = QTableWidgetItem(verb)
            it_verb.setForeground(QColor(_VERB_COLOR.get(verb, "#6b7280")))
            it_verb.setToolTip(_VERB_TIP.get(verb, ""))
            self.tbl_files.setItem(i, 1, it_verb)
            self.tbl_files.setItem(i, 2, QTableWidgetItem(path))
            self.tbl_files.setItem(i, 3, QTableWidgetItem(""))
            self.tbl_files.setItem(i, 4, QTableWidgetItem(""))
            _ = muted
        self.tbl_files.blockSignals(False)
        self.tbl_files.setUpdatesEnabled(True)
        if self._files:
            self.tbl_files.selectRow(0)
        self._sync_actions()

    # ------------------------------------------------------------------ diff
    def _row_file(self):
        if self._pinned:
            return ("version", self._pinned)
        row = self.tbl_files.currentRow()
        if 0 <= row < len(self._files):
            verb, path, _a, _d = self._files[row]
            return (verb, path)
        return None

    def _load_diff(self, *_):
        e = self._selected_entry()
        rf = self._row_file()
        if not e or not rf:
            return
        verb, path = rf
        mode = self._mode()
        when = _fmt_time(e["epoch"])
        if self._pinned:
            side = ("content" if self.cb_content.isChecked()
                    else ("vs the previous version" if mode == "then"
                          else "vs the current file"))
            self.lbl_diff.setText(f"{path} @ {when}  —  {side}")
        elif mode == "then":
            self.lbl_diff.setText(f"{path}  —  changes captured at {when}")
        else:
            self.lbl_diff.setText(f"{path}  —  this state vs today")
        if not self._pinned and mode == "then":
            _s, _p, adds, dels = self._files[self.tbl_files.currentRow()]
            if adds is None and dels is None:
                self.diff.setPlainText("(binary file — no text diff)")
                return
        name, sha, parent = self.cb_repo.currentText(), e["sha"], e.get("parent")
        # The pinned "previous version" is the NEXT list entry (newest first).
        prev_sha = None
        if self._pinned and mode == "then":
            idx = next((i for i, v in enumerate(self._cache)
                        if v["sha"] == sha), None)
            if idx is not None and idx + 1 < len(self._cache):
                prev_sha = self._cache[idx + 1]["sha"]
        dark = bool(self._pal.get("is_dark"))
        sbs = self.cb_sbs.isChecked()
        content_only = self._pinned and self.cb_content.isChecked()
        self._diff_gen += 1
        gen = self._diff_gen
        pinned, m = self._pinned, mode
        self.diff.setPlainText("Loading the diff…")  # the pane no longer shows stale content
        self.busy.start("Loading the diff…")

        def work():
            try:
                # "delete" is the ONE verb with nothing to load at `sha` (the
                # file was added after it; a restore would remove it). Every
                # other verb — "recreate" above all — has content there, and
                # showing it is the point: it's what the restore brings back.
                new = (self.c.file_text_at(name, path, sha)
                       if verb != "delete" or pinned else None)
                if content_only:
                    html = None if new is None else (
                        "<pre style=\"font-family:Consolas,monospace;"
                        "font-size:10pt;\">" + _escape(new) + "</pre>")
                else:
                    if pinned:
                        old = (self.c.file_text_at(name, path, prev_sha)
                               if m == "then" and prev_sha else
                               (self.c.current_text(name, path) if m == "today"
                                else ""))
                        # vs current: the DIFF direction is version -> current.
                        a, b = ((old or "", new or "") if m == "then"
                                else (new or "", old or ""))
                        labels = (("previous version", "this version")
                                  if m == "then" else
                                  ("selected version", "current file"))
                    elif m == "then":
                        a = (self.c.file_text_at(name, path, parent) or "") if parent else ""
                        b = new or ""
                        labels = ("before", "this snapshot")
                    else:  # vs today
                        a = new if verb != "delete" else ""
                        b = (self.c.current_text(name, path)
                             if a is not None and verb != "recreate" else "")
                        a = a or ""
                        labels = ("selected version", "current file")
                    if new is None and not pinned and m == "today" and verb != "delete":
                        html = None
                    else:
                        html = (diff_html_sbs(a, b, dark=dark) if sbs else
                                diff_html(a, b, dark=dark,
                                          from_label=labels[0], to_label=labels[1]))
            except Exception as ex:  # noqa: BLE001 — surfaced in the pane
                html = f"<pre>could not load the diff: {_escape(str(ex))}</pre>"
            try:
                self._diff_ready.emit(gen, html if html is not None else "")
            except RuntimeError:
                pass  # tab destroyed while loading

        threading.Thread(target=work, name="sincrogit-tm-diff", daemon=True).start()

    def _on_diff_ready(self, gen, html):
        self.busy.stop()
        if gen != self._diff_gen:
            return  # the user already picked another file/state
        if not html:
            self.diff.setPlainText("(binary or unavailable)")
        else:
            self.diff.setHtml(html)

    # ---------------------------------------------------------------- search
    def _find_in_versions(self):
        term = self.ed_search.text().strip()
        rel = self._pinned
        if not term or not rel or not self._cache:
            return
        self.btn_search.setEnabled(False)
        self.lbl_info.setText(
            f"Searching '{term}' across {len(self._cache)} version(s)…")
        name = self.cb_repo.currentText()

        def work():
            try:
                results = self.c.search_in_file_versions(name, rel, term)
            except Exception:  # noqa: BLE001 — empty reads as "not found"
                results = []
            try:
                self._search_ready.emit(rel, term, results)
            except RuntimeError:
                pass  # tab destroyed while searching

        self.busy.start(f"Searching '{term}' across versions…")
        threading.Thread(target=work, name="sincrogit-tm-search", daemon=True).start()

    def _on_search_ready(self, rel, term, results):
        self.busy.stop()
        self.btn_search.setEnabled(True)
        if rel != self._pinned:
            return  # the user unpinned / switched meanwhile
        counts = dict(results)
        # The rail is newest-first with day headers interleaved: walk the CARD
        # items in order and compare each version against the next older one.
        cards = [self.lst.item(i) for i in range(self.lst.count())
                 if self.lst.item(i).data(ROLE_ENTRY) is not None]
        transitions = 0
        for i, it in enumerate(cards):
            sha = it.data(ROLE_ENTRY)["sha"]
            n = counts.get(sha, 0)
            older = (counts.get(cards[i + 1].data(ROLE_ENTRY)["sha"], 0)
                     if i + 1 < len(cards) else n)
            changed = n != older
            transitions += changed
            it.setData(ROLE_MARK, bool(changed))
            it.setToolTip(f"'{term}': {n} occurrence(s) in this version")
        self.lst.viewport().update()
        self.lbl_info.setText(
            f"'{term}' changes in {transitions} version(s) — highlighted"
            if transitions else f"'{term}': no changes across these versions")

    # ------------------------------------------------------------- autosnaps
    def _fetch_autosnaps(self):
        name = self.cb_repo.currentText()
        if not name:
            return
        self.btn_fetch.setEnabled(False)
        self.lbl_info.setText(f"Fetching autosnaps for '{name}'…")

        def work():
            try:
                states = self.c.fetch_autosnaps(name)
                ok, count, err = True, len(states), ""
            except Exception as e:  # noqa: BLE001 — surfaced below
                ok, count, err = False, 0, str(e)
            try:
                self._fetch_done.emit(ok, count, name, err)
            except RuntimeError:
                pass  # tab destroyed while fetching

        self.busy.start("Fetching autosnaps from the remote…")
        threading.Thread(target=work, name="sincrogit-tm-fetch", daemon=True).start()

    def _on_fetch_done(self, ok, count, name, err):
        self.busy.stop()
        self.btn_fetch.setEnabled(True)
        self.lbl_info.setText("")
        if not ok:
            QMessageBox.warning(self, "Autosnap", f"Fetch failed: {err}")
            return
        self.lbl_info.setText(f"{count} autosnap state(s) available for '{name}'")
        if name == self.cb_repo.currentText():
            self._reload()  # the new states appear on the rail

    # ------------------------------------------------------------- selection
    def _checked_paths(self) -> list:
        out = []
        for i, (_verb, path, _a, _d) in enumerate(self._files):
            item = self.tbl_files.item(i, 0)
            if item is not None and item.checkState() == Qt.Checked:
                out.append(path)
        return out

    def _set_all(self, state):
        self.tbl_files.blockSignals(True)
        for i, (_verb, path, _a, _d) in enumerate(self._files):
            item = self.tbl_files.item(i, 0)
            if item is not None and path not in self._risky:
                item.setCheckState(state)
        self.tbl_files.blockSignals(False)
        self._sync_actions()

    def _sync_actions(self, *_):
        e = self._selected_entry()
        has_state = e is not None
        has_file = self._row_file() is not None
        pinned = self._pinned is not None
        today = self._mode() == "today"
        n = len(self._checked_paths()) if (not pinned and today) else 0
        self.btn_restore_sel.setText(
            f"Restore selected ({n})" if n else "Restore selected")
        self.btn_restore_sel.setEnabled(n > 0)
        self.btn_saveas.setEnabled(has_state and has_file)
        self.btn_restore_file.setEnabled(has_state and has_file)
        self.btn_hunks.setEnabled(pinned and has_state)
        self.btn_restore_repo.setEnabled(has_state)

    # --------------------------------------------------------------- actions
    def _save_copy(self):
        e = self._selected_entry()
        rf = self._row_file()
        if not e or not rf:
            return
        verb, path = rf
        if verb == "delete":
            QMessageBox.information(
                self, "Save a copy",
                "This file doesn't exist in the selected state (it was created "
                "later) — there is nothing to save from there.")
            return
        stem, ext = os.path.splitext(os.path.basename(path))
        try:
            stamp = datetime.datetime.fromtimestamp(
                e["epoch"]).strftime("%Y-%m-%d %H.%M")
        except (ValueError, OSError, TypeError):
            stamp = e["sha"][:8]
        suggested = os.path.join(self.cb_repo.currentData() or "",
                                 os.path.dirname(path), f"{stem} ({stamp}){ext}")
        dest, _ = QFileDialog.getSaveFileName(
            self, "Save a copy of this version", suggested)
        if not dest:
            return
        name, sha = self.cb_repo.currentText(), e["sha"]
        self.btn_saveas.setEnabled(False)
        self.busy.start("Saving a copy…")

        def work():  # git show of a raw blob + write: seconds for a big binary
            try:
                ok, msg = self.c.export_file_version(name, path, sha, dest)
            except Exception as ex:  # noqa: BLE001 — surfaced in the dialog
                ok, msg = False, str(ex)
            try:
                self._export_done.emit(ok, msg, dest)
            except RuntimeError:
                pass  # tab destroyed while exporting

        threading.Thread(target=work, name="sincrogit-tm-export", daemon=True).start()

    def _on_export_done(self, ok, msg, dest):
        self.busy.stop()
        self.btn_saveas.setEnabled(True)
        if ok:
            QMessageBox.information(self, "Save a copy", f"Saved to:\n{dest}")
        else:
            QMessageBox.critical(self, "Save a copy", msg)

    def _restore_file(self):
        e = self._selected_entry()
        rf = self._row_file()
        if not e or not rf:
            return
        _verb, path = rf
        when = _fmt(e["epoch"])
        if QMessageBox.question(
            self, "Restore file",
            f"Restore '{path}' to its state at {when}?\n\nThe current content "
            f"is overwritten in the working tree (and saved as a new snapshot "
            f"first, so this is reversible).",
            QMessageBox.Yes | QMessageBox.Cancel, QMessageBox.Cancel,
        ) != QMessageBox.Yes:
            return
        self._run_restore("file", path, e["sha"],
                          f"Restored '{path}' to {when}.")

    def _restore_hunks(self):
        e = self._selected_entry()
        if not e or not self._pinned:
            return
        from .hunk_dialog import HunkRestoreDialog
        dlg = HunkRestoreDialog(self.c, self.cb_repo.currentText(), self._pinned,
                                e["sha"], _fmt(e["epoch"]), parent=self)
        accepted = dlg.exec_()
        dlg.deleteLater()  # parented dialogs outlive exec_() otherwise
        if accepted:
            self._reload()  # the restore is a new version on the rail

    def _restore_selected(self):
        paths = self._checked_paths()
        e = self._selected_entry()
        if not paths or not e:
            return
        when = _fmt(e["epoch"])
        sample = "\n".join("  • " + p for p in paths[:8])
        if len(paths) > 8:
            sample += f"\n  … and {len(paths) - 8} more"
        if QMessageBox.question(
            self, "Restore selected files",
            f"Restore {len(paths)} file(s) of '{self.cb_repo.currentText()}' to "
            f"their state at {when}?\n\n{sample}\n\nReversible: the restore is "
            f"captured as a new snapshot.",
            QMessageBox.Yes | QMessageBox.Cancel, QMessageBox.Cancel,
        ) != QMessageBox.Yes:
            return
        self._run_restore("selected", paths, e["sha"],
                          f"Restored {len(paths)} file(s) to {when}.")

    def _restore_repo(self):
        """Whole-repo restore, in two steps: a background PREVIEW of what would
        change, then a confirm box showing it — decide on facts, not faith."""
        e = self._selected_entry()
        if not e:
            return
        when = _fmt(e["epoch"])
        name, sha = self.cb_repo.currentText(), e["sha"]
        self.btn_restore_repo.setEnabled(False)
        self.lbl_info.setText("Computing what the restore would change…")
        self.busy.start("Computing what the restore would change…")

        def work():
            try:
                ok, payload = self.c.restore_repo_preview(name, sha)
            except Exception as ex:  # noqa: BLE001 — surfaced in the dialog
                ok, payload = False, str(ex)
            try:
                self._preview_ready.emit(ok, payload, sha, when)
            except RuntimeError:
                pass  # tab destroyed while computing

        threading.Thread(target=work, name="sincrogit-tm-preview", daemon=True).start()

    def _on_preview_ready(self, ok, payload, sha, when):
        self.busy.stop()
        self.btn_restore_repo.setEnabled(True)
        self.lbl_info.setText("")
        if not ok:
            QMessageBox.warning(self, "Restore",
                                f"Could not preview the restore: {payload}")
            return
        changes, risky = payload["changes"], payload["risky"]
        if not changes and not risky:
            QMessageBox.information(
                self, "Restore",
                "Nothing would change — the working tree already matches that state.")
            return
        n_rev = sum(1 for v, _ in changes if v == "revert")
        n_del = sum(1 for v, _ in changes if v == "delete")
        n_rec = sum(1 for v, _ in changes if v == "recreate")
        parts = []
        if n_rev:
            parts.append(f"{n_rev} file(s) revert to their {when} version")
        if n_del:
            parts.append(f"{n_del} file(s) created since then are removed")
        if n_rec:
            parts.append(f"{n_rec} file(s) deleted since then come back")
        box = QMessageBox(self)
        box.setWindowTitle("Restore ENTIRE repo")
        box.setIcon(QMessageBox.Warning)
        box.setText(
            f"Restore the WHOLE repository '{self.cb_repo.currentText()}' to "
            f"{when}?\n\n" + "\n".join("•  " + p for p in parts))
        info = ("Reversible: the restore is captured as a new snapshot, so the "
                "time machine can take you back to right before it. See Details "
                "for the file list.")
        if risky:
            info = (f"⚠ {len(risky)} file(s) have local content snapshots can't "
                    f"capture — the restore will REFUSE while they exist. Copy "
                    f"them somewhere safe first (marked ⚠ in Details).\n\n" + info)
        box.setInformativeText(info)
        detail = "\n".join(f"⚠ can't capture  {p}" for p in risky)
        if changes:
            listing = "\n".join(f"{v:<9} {p}" for v, p in changes)
            detail = (detail + "\n" + listing) if detail else listing
        box.setDetailedText(detail)
        box.setStandardButtons(QMessageBox.Yes | QMessageBox.Cancel)
        box.setDefaultButton(QMessageBox.Cancel)
        confirmed = box.exec_() == QMessageBox.Yes
        box.deleteLater()  # parented boxes outlive exec_() otherwise
        if not confirmed:
            return
        self._run_restore("repo", None, sha, f"Repository restored to {when}.")

    def _run_restore(self, kind, target, sha, success_text):
        """All three restore shapes funnel here: worker + one done-signal.
        restore_* take the repo's op_lock — inline they'd freeze the GUI
        whenever the engine holds it (a push up to the git network timeout)."""
        name = self.cb_repo.currentText()
        for b in (self.btn_restore_file, self.btn_restore_sel,
                  self.btn_restore_repo, self.btn_hunks, self.btn_saveas):
            b.setEnabled(False)
        self.lbl_info.setText("Restoring…")
        self.busy.start("Restoring…")

        def work():
            try:
                if kind == "file":
                    ok, msg = self.c.restore_file(name, target, sha)
                elif kind == "selected":
                    ok, msg = self.c.restore_files(name, target, sha)
                else:
                    ok, msg = self.c.restore_repo(name, sha)
            except Exception as e:  # noqa: BLE001 — reported in the dialog
                ok, msg = False, str(e)
            try:
                self._restore_done.emit(ok, success_text if ok else msg)
            except RuntimeError:
                pass  # tab destroyed while restoring

        threading.Thread(target=work, name="sincrogit-tm-restore", daemon=True).start()

    def _on_restore_done(self, ok, msg):
        self.busy.stop()
        self.lbl_info.setText("")
        self._sync_actions()
        if ok:
            QMessageBox.information(self, "Restore", msg)
            self._reload()  # the restore is itself a new state on the rail
        else:
            QMessageBox.critical(self, "Restore failed", msg)


def _escape(text: str) -> str:
    import html
    return html.escape(text)
