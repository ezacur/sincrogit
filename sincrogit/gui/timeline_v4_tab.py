"""Timeline v4 — the Time machine's machinery under a visibly different skin.

The first v4 draft only added tooltips and folded two buttons — "identical to
v1" was the (fair) verdict. This one keeps the inheritance (every feature —
pin, search, restores, hunks, save-a-copy — is the parent's, so the contest
stays a look-and-feel comparison) but replaces the visual language:

  - COMPACT RAIL: one-line rows with a colored KIND PILL (seal / snap /
    mirror), the time, a preview, and a churn MICRO-BAR (green/red, log
    scale) at the right edge — instead of v1's two-line cards with a rail
    line and dots. Day separators are sunken bands, not text-plus-rule.
  - SEGMENTED mode switch ("What changed then" | "vs today") instead of
    radio buttons — the hidden radios stay the source of truth, so the
    parent's mode logic is untouched.
  - A STATE BANNER above the files: the selected state as one bold line with
    a kind-colored edge — date, kind, files, +/− and subject in one place.
  - Kept from the first draft: a precise tooltip on EVERY control, the rare
    actions folded into "More ▾", the single "All" toggle, airier spacing.
"""

import math

from PyQt5.QtCore import QRectF, QSize, Qt
from PyQt5.QtGui import QColor, QFont, QFontMetrics, QPainter
from PyQt5.QtWidgets import (
    QButtonGroup,
    QLabel,
    QMenu,
    QStyle,
    QStyledItemDelegate,
    QToolButton,
)

from .time_machine_tab import (
    ROLE_DAY,
    ROLE_ENTRY,
    ROLE_MARK,
    TimeMachineTab,
    _TYPE_COLOR,
    _fmt,
    _fmt_time,
    _summary,
)

_ROW_H = 30
_HDR_H = 22
_PILL_W = 48
_BAR_W = 56          # right-edge churn micro-bar zone
_BAR_REF = 400.0     # lines changed that fill the whole bar (log scale)

_KIND_LABEL = {"seal": "seal", "sealed": "seal",
               "autosnap": "mirror", "snapshot": "snap"}


class _CompactRailDelegate(QStyledItemDelegate):
    """v4's rail row: [pill] time  preview………………  ▮churn. Same item roles as
    the parent's delegate (pure painting; fonts cached once, like v1 learned
    the hard way)."""

    def __init__(self, pal: dict, parent=None):
        super().__init__(parent)
        p = pal or {}
        self._muted = QColor(p.get("muted", "#6b7280"))
        self._text = QColor(p.get("text", "#1f2430"))
        self._accent = QColor(p.get("accent", "#2e7dd1"))
        self._ok = QColor(p.get("success", "#2e9e5b"))
        self._bad = QColor(p.get("danger", "#d23f3f"))
        self._sel = QColor(p.get("sel_bg", "#dce9f7"))
        self._band = QColor(p.get("sunken", "#eceff3"))
        self._fonts_ready = False
        self._bold = self._small = self._fm = self._fm_bold = None

    def _ensure_fonts(self, base):
        if self._fonts_ready:
            return
        self._bold = QFont(base)
        self._bold.setBold(True)
        self._small = QFont(base)
        self._small.setPointSize(max(7, base.pointSize() - 2))
        self._fm = QFontMetrics(base)
        self._fm_bold = QFontMetrics(self._bold)
        self._fonts_ready = True

    def sizeHint(self, option, index):
        return QSize(option.rect.width(),
                     _HDR_H if index.data(ROLE_ENTRY) is None else _ROW_H)

    def paint(self, painter: QPainter, option, index):
        self._ensure_fonts(option.font)
        painter.save()
        painter.setRenderHint(QPainter.Antialiasing, True)
        r = option.rect
        entry = index.data(ROLE_ENTRY)

        if entry is None:  # ---- day separator: a sunken band ----
            painter.fillRect(r.adjusted(0, 2, 0, -2), self._band)
            painter.setFont(self._small)
            painter.setPen(self._muted)
            painter.drawText(r.adjusted(10, 0, -10, 0),
                             Qt.AlignVCenter | Qt.AlignLeft,
                             (index.data(ROLE_DAY) or "").upper())
            painter.restore()
            return

        if option.state & QStyle.State_Selected:
            painter.fillRect(r, self._sel)
            painter.fillRect(r.left(), r.top(), 3, r.height(), self._accent)

        kind = entry.get("kind") or entry.get("source") or "snapshot"
        col = QColor(_TYPE_COLOR.get(kind, _TYPE_COLOR["snapshot"]))
        y = r.center().y()

        # Kind pill.
        pill = QRectF(r.left() + 8, y - 8, _PILL_W - 10, 16)
        bg = QColor(col)
        bg.setAlpha(36)
        painter.setPen(Qt.NoPen)
        painter.setBrush(bg)
        painter.drawRoundedRect(pill, 8, 8)
        painter.setFont(self._small)
        painter.setPen(col)
        painter.drawText(pill, Qt.AlignCenter, _KIND_LABEL.get(kind, kind))

        # Time (accented when it's a search transition in pinned mode).
        painter.setFont(self._bold)
        painter.setPen(self._accent if index.data(ROLE_MARK) else self._text)
        x = r.left() + 8 + _PILL_W
        time_txt = _fmt_time(entry["epoch"])
        painter.drawText(x, y + self._fm_bold.ascent() // 2 - 1, time_txt)
        x += self._fm_bold.horizontalAdvance(time_txt) + 8

        # One-line preview: the subject (seals/versions) or the touched files.
        files = entry.get("files")
        if files is not None and kind == "snapshot":
            names = [p.rsplit("/", 1)[-1] for _s, p, _a, _d in files]
            preview = ", ".join(names[:3]) + (", …" if len(names) > 3 else "")
        else:
            preview = entry.get("subject") or ""
        if entry.get("host"):
            preview = f"[{entry['host']}]  {preview}"
        painter.setFont(option.font)
        painter.setPen(self._muted)
        avail = r.right() - _BAR_W - 12 - x
        painter.drawText(x, y + self._fm.ascent() // 2 - 1,
                         self._fm.elidedText(preview, Qt.ElideRight, avail))

        # Churn micro-bar (green adds | red dels, log scale). Data v1 only
        # showed as "+a −d" text; here it's readable at scroll speed.
        if files is not None:
            _t, adds, dels = _summary(entry)
            total = (adds or 0) + (dels or 0)
            if total:
                frac = min(1.0, math.log1p(total) / math.log1p(_BAR_REF))
                w = max(4, int(frac * (_BAR_W - 8)))
                gw = int(round(w * (adds or 0) / total))
                x0 = r.right() - 8 - w
                painter.setPen(Qt.NoPen)
                if gw:
                    painter.setBrush(self._ok)
                    painter.drawRect(x0, y - 3, gw, 6)
                if w - gw:
                    painter.setBrush(self._bad)
                    painter.drawRect(x0 + gw, y - 3, w - gw, 6)
        painter.restore()


class TimelineV4Tab(TimeMachineTab):

    def __init__(self, controller, parent=None):
        super().__init__(controller, parent)
        self.lst.setItemDelegate(_CompactRailDelegate(self._pal, self.lst))
        self._build_segmented_mode()
        self._build_banner()
        self._build_more_menu()
        self._collapse_select_buttons()
        self._precise_tooltips()
        lay = self.layout()
        lay.setContentsMargins(12, 12, 12, 8)
        lay.setSpacing(9)
        self._apply_mode_ui()   # re-run with the v4 visibility overrides
        self._update_banner()

    # -------------------------------------------------------- segmented mode
    def _build_segmented_mode(self):
        """Two joined toggle buttons replace the radios VISUALLY; the hidden
        radios stay the source of truth so the parent's mode logic (and any
        programmatic rb_today.setChecked) keeps working unchanged."""
        mode_lay = self.layout().itemAt(1).layout()
        label = mode_lay.itemAt(0).widget()
        if isinstance(label, QLabel):
            label.setVisible(False)
        self.rb_then.setVisible(False)
        self.rb_today.setVisible(False)

        self.seg_then = QToolButton()
        self.seg_then.setText("What changed then")
        self.seg_today = QToolButton()
        self.seg_today.setText("vs today")
        group = QButtonGroup(self)
        group.setExclusive(True)
        for btn, pos in ((self.seg_then, "first"), (self.seg_today, "last")):
            btn.setCheckable(True)
            btn.setProperty("cssClass", "seg")
            btn.setProperty("segPos", pos)
            group.addButton(btn)
        self.seg_then.setChecked(True)
        self.seg_then.clicked.connect(lambda: self.rb_then.setChecked(True))
        self.seg_today.clicked.connect(lambda: self.rb_today.setChecked(True))
        self.rb_then.toggled.connect(self._sync_seg)
        mode_lay.insertWidget(1, self.seg_then)
        mode_lay.insertWidget(2, self.seg_today)

    def _sync_seg(self, *_):
        then = self.rb_then.isChecked()
        for btn, on in ((self.seg_then, then), (self.seg_today, not then)):
            btn.blockSignals(True)
            btn.setChecked(on)
            btn.blockSignals(False)

    # ---------------------------------------------------------- state banner
    def _build_banner(self):
        self.banner = QLabel("")
        self.banner.setTextFormat(Qt.RichText)
        self.banner.setToolTip("The state currently open — everything below "
                               "(files, diff, restores) refers to it.")
        self.layout().insertWidget(2, self.banner)

    def _update_banner(self):
        e = self._selected_entry()
        sunken = self._pal.get("sunken", "#eceff3")
        muted = self._pal.get("muted", "#6b7280")
        if not e:
            self.banner.setStyleSheet(
                f"QLabel{{border-left:4px solid {muted}; background:{sunken};"
                f"border-radius:4px; padding:5px 10px; color:{muted};}}")
            self.banner.setText("Pick a state on the left rail.")
            return
        kind = e.get("kind") or e.get("source") or "snapshot"
        col = _TYPE_COLOR.get(kind, _TYPE_COLOR["snapshot"])
        self.banner.setStyleSheet(
            f"QLabel{{border-left:4px solid {col}; background:{sunken};"
            f"border-radius:4px; padding:5px 10px;}}")
        bits = [f"<b>{_fmt(e['epoch'])}</b>",
                f"<span style='color:{col};'>{_KIND_LABEL.get(kind, kind)}"
                + (f" · {e['host']}" if e.get("host") else "") + "</span>"]
        if e.get("files") is not None:
            files_text, adds, dels = _summary(e)
            churn = (f"{files_text}"
                     + (f" · <span style='color:#2e9e5b;'>+{adds}</span>" if adds else "")
                     + (f" <span style='color:#d23f3f;'>−{dels}</span>" if dels else ""))
            bits.append(churn)
        if e.get("subject"):
            bits.append(f"<span style='color:{muted};'>{e['subject']}</span>")
        self.banner.setText(" &nbsp;·&nbsp; ".join(bits))

    def _on_state_selected(self, *args):
        super()._on_state_selected(*args)
        self._update_banner()

    # ------------------------------------------------------------ regrouping
    def _build_more_menu(self):
        """Fold the two rare actions into one menu: btn_fetch becomes the
        trigger ("More ▾"), btn_restore_repo hides — both keep their slots and
        enabled-state logic; the menu actions just delegate."""
        self.btn_fetch.clicked.disconnect()
        self.btn_fetch.setText("More ▾")
        menu = QMenu(self)
        self._act_fetch = menu.addAction("Fetch autosnaps",
                                         self._fetch_autosnaps)
        self._act_fetch.setToolTip(
            "Download your other machines' live mirrors from the remote so "
            "their states appear on the rail (disaster recovery / handoff).")
        self._act_restore_repo = menu.addAction("Restore ENTIRE repo…",
                                                self._restore_repo)
        self._act_restore_repo.setToolTip(
            "Set every file back to the selected state, after a preview of "
            "what would change. Undoable: the pre-restore state is captured "
            "as a snapshot first.")
        menu.setToolTipsVisible(True)
        self.btn_fetch.setMenu(menu)
        self.btn_restore_repo.setVisible(False)

    def _collapse_select_buttons(self):
        """One 'All' toggle instead of the Select all / Select none pair."""
        self.btn_all.clicked.disconnect()
        self.btn_all.setText("All")
        self.btn_all.setCheckable(True)
        self.btn_all.toggled.connect(
            lambda on: self._set_all(Qt.Checked if on else Qt.Unchecked))
        self.btn_none.setVisible(False)

    def _apply_mode_ui(self):
        super()._apply_mode_ui()
        # The parent re-shows these per mode; v4 keeps them folded/replaced.
        if hasattr(self, "_act_restore_repo"):   # (parent __init__ calls this early)
            self.btn_none.setVisible(False)
            self.btn_restore_repo.setVisible(False)
            self.rb_then.setVisible(False)
            self.rb_today.setVisible(False)

    def _sync_actions(self, *_):
        super()._sync_actions()
        if hasattr(self, "_act_restore_repo"):
            self._act_restore_repo.setEnabled(self.btn_restore_repo.isEnabled())

    # -------------------------------------------------------------- tooltips
    def _precise_tooltips(self):
        """Say what each control DOES — precisely. Overrides the inherited
        ones too, so the whole tab speaks with one voice."""
        tips = {
            "cb_repo": "Which repo's history this tab shows.",
            "btn_browse": "Follow ONE file through time: the rail switches to "
                          "that file's versions, with search, per-file restore "
                          "and hunk restore. Double-clicking a file in the "
                          "list below pins it too.",
            "btn_unpin": "Stop following the pinned file and return to the "
                         "whole-repo timeline.",
            "btn_fetch": "The rare actions: fetch your other machines' "
                         "mirrors, or roll the whole repo back to a state.",
            "seg_then": "Answer 'what changed AT that moment': each state's "
                        "files compared against its parent — the work diary.",
            "seg_today": "Answer 'what is different FROM today': each state's "
                         "files compared against the current worktree, with "
                         "checkboxes to restore exactly what you pick.",
            "rb_then": "(replaced by the segmented switch above)",
            "rb_today": "(replaced by the segmented switch above)",
            "cb_filter": "What the rail lists: every capture (~5-min "
                         "snapshots, seals, mirrors) or only the permanent "
                         "commits.",
            "ed_search": "Type a text and press Enter: every version of the "
                         "pinned file is searched, and the rail highlights "
                         "where the text appeared, changed count, or "
                         "vanished.",
            "btn_search": "Search the text in every version of the pinned "
                          "file (same as pressing Enter).",
            "btn_all": "Tick / untick every restorable file at once "
                       "(⚠ uncapturable files always stay out).",
            "btn_none": "Untick every file. (Hidden in this proposal — the "
                        "'All' toggle covers both directions.)",
            "cb_content": "Show the selected version's raw text instead of a "
                          "diff.",
            "cb_sbs": "Two-column diff (before | after) instead of the "
                      "unified single-column view.",
            "btn_saveas": "Write the selected version to a NEW file you name "
                          "— recover old content without overwriting "
                          "anything.",
            "btn_restore_file": "Overwrite the current file with the selected "
                                "version's content. Undoable: the pre-restore "
                                "state is captured as a snapshot first.",
            "btn_hunks": "Roll back only SOME of the changed blocks of the "
                         "pinned file, keeping your other current edits "
                         "intact. Text files only.",
            "btn_restore_sel": "Set the TICKED files back to this state's "
                               "content — restoring creates, overwrites or "
                               "deletes as needed. Undoable: the pre-restore "
                               "state is captured as a snapshot first.",
            "lst": "The timeline, newest first: the pill says what each state "
                   "is (snap / seal / mirror), the bar on the right shows how "
                   "big the change was (green added, red removed). Click a "
                   "state to open it.",
            "tbl_files": "The selected state's files. Double-click one to "
                         "follow it through time (pin).",
        }
        for attr, tip in tips.items():
            getattr(self, attr).setToolTip(tip)
