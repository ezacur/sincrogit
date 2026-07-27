"""Marks tab: the moments you named, and what to do with them.

A snapshot every few minutes is a safety net; a mark is a MEMORY — "before the
refactor", "the demo build that worked". The time machine can show marks on its
rail, but finding one there means scrolling a day-by-day list of automatic
captures, which is the wrong shape for "take me back to the state I named".
This tab is that shape: the named states, newest first, and the three things
anyone actually wants from one — see what changed since, roll back to it, or
open it on the full timeline.

Threading contract (same as every other tab): all git work runs on a worker and
comes back through a queued signal guarded by a generation token; the GUI thread
never spawns git and never waits on the engine's locks.
"""

import threading

from PyQt5.QtCore import Qt, QTimer, pyqtSignal
from PyQt5.QtGui import QColor
from PyQt5.QtWidgets import (
    QComboBox,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QMessageBox,
    QPushButton,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .busy import BusyBar
from .time_machine_tab import _VERB_COLOR, _VERB_TIP, _ago, _fmt

# The label the user types when creating a mark. Kept in one place because the
# tray menu, the Status context menu and this tab must all say the same thing —
# and because the docs' UI-name check reads these literals.
ASK_TITLE = "Mark this moment…"
ASK_PROMPT = ("Name this moment — you'll find it by this name later:\n"
              "(e.g. \"before the refactor\", \"demo build that worked\")")


def ask_label(parent=None, repo: str = "") -> str | None:
    """The one dialog that creates a mark, shared by every entry point. None
    when the user cancels or types nothing (an unnamed mark is unfindable,
    which defeats the entire feature)."""
    where = f" in '{repo}'" if repo else ""
    text, ok = QInputDialog.getText(parent, ASK_TITLE, ASK_PROMPT + where)
    text = " ".join((text or "").split())
    return text if (ok and text) else None


class MarksTab(QWidget):
    """Duck-typed controller: repo_list(), list_marks(name), mark_repo(name,
    label), forget_mark(name, ref), restore_repo_preview(name, sha),
    restore_repo(name, sha), theme. `on_open_state(name, sha)` is the panel's
    jump into the Time machine tab."""

    _marks_loaded = pyqtSignal(int, list)             # gen, marks
    # One signal for BOTH comparisons — the button's and the restore's — because
    # they run the identical query; `ctx["prompt"]` decides whether the confirm
    # box follows. gen guards against a stale worker overwriting a newer answer.
    _preview_ready = pyqtSignal(int, bool, object, object)  # gen, ok, payload|msg, ctx
    _mark_done = pyqtSignal(bool, str)                # ok, message
    _restore_done = pyqtSignal(bool, str)             # ok, message

    def __init__(self, controller, on_open_state=None, parent=None):
        super().__init__(parent)
        self.c = controller
        self._pal = getattr(controller, "theme", None) or {}
        self._open_state = on_open_state
        self._gen = 0            # mark-list loads
        self._changes_gen = 0    # "what changed since" computations
        self._marks = []
        self._loaded_once = False
        self._stale = True

        v = QVBoxLayout(self)
        v.setContentsMargins(8, 8, 8, 8)

        top = QHBoxLayout()
        top.addWidget(QLabel("Repo:"))
        self.cb_repo = QComboBox()
        self.cb_repo.setToolTip("Which repo's marks this tab lists.")
        self.cb_repo.currentIndexChanged.connect(self._repo_changed)
        top.addWidget(self.cb_repo)
        self.btn_new = QPushButton(ASK_TITLE)
        self.btn_new.setProperty("cssClass", "primary")
        self.btn_new.setToolTip(
            "Snapshot this repo right now and give that state a name. It "
            "survives every later commit — unlike an ordinary snapshot, whose "
            "reflog expires in about a month.")
        self.btn_new.clicked.connect(self._new_mark)
        top.addWidget(self.btn_new)
        top.addStretch(1)
        self.lbl_count = QLabel("")
        self.lbl_count.setProperty("cssClass", "muted")
        top.addWidget(self.lbl_count)
        v.addLayout(top)

        split = QSplitter(Qt.Horizontal)
        self.tbl_marks = QTableWidget(0, 3)
        self.tbl_marks.setHorizontalHeaderLabels(["Mark", "When", "Differs"])
        self.tbl_marks.setToolTip(
            "Your named moments, newest first. 'Differs' is how many files "
            "have changed since — how far back this mark is.")
        hdr = self.tbl_marks.horizontalHeader()
        hdr.setSectionResizeMode(0, QHeaderView.Stretch)
        hdr.setSectionResizeMode(1, QHeaderView.ResizeToContents)
        hdr.setSectionResizeMode(2, QHeaderView.ResizeToContents)
        self.tbl_marks.setSelectionBehavior(QTableWidget.SelectRows)
        self.tbl_marks.setSelectionMode(QTableWidget.SingleSelection)
        self.tbl_marks.setEditTriggers(QTableWidget.NoEditTriggers)
        self.tbl_marks.verticalHeader().setVisible(False)
        self.tbl_marks.setShowGrid(False)
        self.tbl_marks.itemSelectionChanged.connect(self._on_mark_selected)
        self.tbl_marks.doubleClicked.connect(lambda _i: self._open_in_time_machine())
        split.addWidget(self.tbl_marks)

        right = QWidget()
        rv = QVBoxLayout(right)
        rv.setContentsMargins(0, 0, 0, 0)
        self.lbl_changes = QLabel("")
        self.lbl_changes.setProperty("cssClass", "muted")
        rv.addWidget(self.lbl_changes)
        self.tbl_changes = QTableWidget(0, 2)
        self.tbl_changes.setHorizontalHeaderLabels(["Action", "File"])
        self.tbl_changes.setToolTip(
            "What restoring this mark would do to each file — the difference "
            "between the marked state and what's on disk right now.")
        chdr = self.tbl_changes.horizontalHeader()
        chdr.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        chdr.setSectionResizeMode(1, QHeaderView.Stretch)
        self.tbl_changes.setSelectionBehavior(QTableWidget.SelectRows)
        self.tbl_changes.setEditTriggers(QTableWidget.NoEditTriggers)
        self.tbl_changes.verticalHeader().setVisible(False)
        self.tbl_changes.setShowGrid(False)
        rv.addWidget(self.tbl_changes)
        split.addWidget(right)
        split.setSizes([420, 500])
        v.addWidget(split, 1)

        self.busy = BusyBar()
        v.addWidget(self.busy)

        act = QHBoxLayout()
        self.lbl_info = QLabel("")
        self.lbl_info.setProperty("cssClass", "muted")
        act.addWidget(self.lbl_info, 1)
        self.btn_changes = QPushButton("What changed since")
        self.btn_changes.setToolTip(
            "List every file that differs between the marked state and now.")
        self.btn_changes.clicked.connect(self._load_changes)
        act.addWidget(self.btn_changes)
        self.btn_open = QPushButton("Open in Time machine")
        self.btn_open.setToolTip(
            "Show this moment on the full timeline — per-file diffs, partial "
            "restores, everything around it.")
        self.btn_open.clicked.connect(self._open_in_time_machine)
        act.addWidget(self.btn_open)
        self.btn_forget = QPushButton("Forget this mark")
        self.btn_forget.setToolTip(
            "Drop the NAME. The state itself stays wherever the snapshot "
            "history still holds it — this is not a delete of your work.")
        self.btn_forget.clicked.connect(self._forget_mark)
        act.addWidget(self.btn_forget)
        self.btn_restore = QPushButton("Restore to this mark…")
        self.btn_restore.setProperty("cssClass", "danger")
        self.btn_restore.setToolTip(
            "Put every file back the way it was at this mark, after showing "
            "you exactly what would change. Reversible: the current state is "
            "captured as a snapshot first.")
        self.btn_restore.clicked.connect(self._restore)
        act.addWidget(self.btn_restore)
        v.addLayout(act)

        for s, h in ((self._marks_loaded, self._on_marks_loaded),
                     (self._preview_ready, self._on_preview_ready),
                     (self._mark_done, self._on_mark_done),
                     (self._restore_done, self._on_restore_done)):
            s.connect(h)
        # A burst of marks (a scripted hook marking each step) must not reload
        # the list once per event.
        self._debounce = QTimer(self)
        self._debounce.setSingleShot(True)
        self._debounce.setInterval(800)
        self._debounce.timeout.connect(self._reload)
        self._sync_actions()

    # ------------------------------------------------------------ public API
    def focus_repo(self, name=None):
        """Jump here from elsewhere in the panel, showing `name`'s marks."""
        self._sync_repos()
        if name:
            i = self.cb_repo.findText(name)
            if i >= 0 and i != self.cb_repo.currentIndex():
                self.cb_repo.setCurrentIndex(i)  # triggers _repo_changed
        if self._stale or not self._loaded_once:
            self._reload()

    def notice_event(self, ev):
        """Panel hook (GUI thread): a mark landed for the repo being shown —
        including one made from the tray, the CLI or an agent's hook, which is
        the whole reason this listens to the event stream at all."""
        if getattr(ev, "action", "") != "mark":
            return
        if getattr(ev, "repo", "") != self.cb_repo.currentText():
            return
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
            self.cb_repo.addItems(names)
            if current in names:
                self.cb_repo.setCurrentText(current)
            self.cb_repo.blockSignals(False)

    def _repo_changed(self):
        self._clear_changes("")
        self._reload()

    # ---------------------------------------------------------- loading
    def _reload(self):
        name = self.cb_repo.currentText()
        if not name:
            self.tbl_marks.setRowCount(0)
            self.lbl_count.setText("no repos configured")
            self._sync_actions()
            return
        self._stale = False
        self._loaded_once = True
        self._gen += 1
        gen = self._gen

        def work():
            try:
                marks = self.c.list_marks(name)
            except Exception:  # noqa: BLE001 — an empty list reads as "none yet"
                marks = []
            try:
                self._marks_loaded.emit(gen, marks)
            except RuntimeError:
                pass  # tab destroyed while loading

        self.busy.start("Loading marks…")
        threading.Thread(target=work, name="sincrogit-marks-load",
                         daemon=True).start()

    def _on_marks_loaded(self, gen, marks):
        self.busy.stop()
        if gen != self._gen:
            return
        self._marks = marks
        self.tbl_marks.setRowCount(len(marks))
        muted = QColor(self._pal.get("muted", "#6b7280"))
        for row, m in enumerate(marks):
            label = QTableWidgetItem(m.get("label") or "(unnamed)")
            label.setToolTip(f"{_fmt(m.get('epoch'))}  ·  {m.get('sha', '')[:10]}")
            when = QTableWidgetItem(_ago(m.get("epoch")))
            when.setForeground(muted)
            when.setToolTip(_fmt(m.get("epoch")))
            n = m.get("files")
            differs = QTableWidgetItem("—" if n is None else
                                       ("nothing" if n == 0 else f"{n} file(s)"))
            differs.setForeground(muted)
            differs.setToolTip(
                "How many files have changed since this mark — 'nothing' means "
                "your working tree still matches it.")
            for col, item in enumerate((label, when, differs)):
                self.tbl_marks.setItem(row, col, item)
        self.lbl_count.setText(
            f"{len(marks)} mark(s)" if marks else
            "no marks yet — name a moment and it stays findable forever")
        if marks:
            self.tbl_marks.selectRow(0)
        else:
            self._clear_changes("")
        self._sync_actions()

    # ------------------------------------------------------------- selection
    def _selected(self):
        row = self.tbl_marks.currentRow()
        return self._marks[row] if 0 <= row < len(self._marks) else None

    def _on_mark_selected(self):
        m = self._selected()
        self._clear_changes("" if not m else
                            "Press 'What changed since' to compare this mark "
                            "with your files right now.")
        self._sync_actions()

    def _clear_changes(self, note):
        self.tbl_changes.setRowCount(0)
        self.lbl_changes.setText(note)

    def _sync_actions(self):
        has = self._selected() is not None
        for b in (self.btn_changes, self.btn_open, self.btn_restore,
                  self.btn_forget):
            b.setEnabled(has)
        self.btn_new.setEnabled(bool(self.cb_repo.currentText()))

    # --------------------------------------------------------------- actions
    def _new_mark(self):
        name = self.cb_repo.currentText()
        if not name:
            return
        label = ask_label(self, name)
        if not label:
            return
        self.btn_new.setEnabled(False)
        self.busy.start("Marking this moment…")

        def work():  # snapshot + ref: takes the repo's op_lock
            try:
                ok, msg = self.c.mark_repo(name, label)
            except Exception as e:  # noqa: BLE001 — surfaced in the dialog
                ok, msg = False, str(e)
            try:
                self._mark_done.emit(ok, msg)
            except RuntimeError:
                pass  # tab destroyed while marking

        threading.Thread(target=work, name="sincrogit-mark", daemon=True).start()

    def _on_mark_done(self, ok, msg):
        self.busy.stop()
        self.btn_new.setEnabled(True)
        if ok:
            self._reload()
        else:
            QMessageBox.warning(self, "Mark", f"Nothing was marked: {msg}")

    def _load_changes(self):
        self._compare(prompt=False)

    def _compare(self, prompt: bool):
        """Ask what differs between the selected mark and the files on disk.
        The restore path runs the SAME query (`prompt=True` just adds the
        confirm box after it) — a restore must never be offered on a preview
        the user can't see."""
        m = self._selected()
        if not m:
            return
        ctx = {"name": self.cb_repo.currentText(), "sha": m["sha"],
               "label": m.get("label", ""), "when": _fmt(m.get("epoch")),
               "prompt": prompt}
        self._changes_gen += 1
        gen = self._changes_gen
        caption = ("Computing what the restore would change…" if prompt
                   else "Comparing with your files right now…")
        self.lbl_changes.setText(caption)
        if prompt:
            self.btn_restore.setEnabled(False)
        self.busy.start(caption)

        def work():
            # The same call the time machine's "vs today" uses: it snapshots
            # first, so the comparison includes edits saved seconds ago.
            try:
                ok, payload = self.c.restore_repo_preview(ctx["name"], ctx["sha"])
            except Exception as e:  # noqa: BLE001
                ok, payload = False, str(e)
            try:
                self._preview_ready.emit(gen, ok, payload, ctx)
            except RuntimeError:
                pass  # tab destroyed while comparing

        threading.Thread(target=work, name="sincrogit-marks-diff",
                         daemon=True).start()

    def _on_preview_ready(self, gen, ok, payload, ctx):
        self.busy.stop()
        if ctx.get("prompt"):
            self.btn_restore.setEnabled(True)
        if gen != self._changes_gen:
            return  # a newer comparison is on its way
        if not ok:
            self._clear_changes(f"Could not compare: {payload}")
            if ctx.get("prompt"):
                QMessageBox.warning(self, "Restore",
                                    f"Could not preview the restore: {payload}")
            return
        self._fill_changes(payload, ctx["label"])
        if ctx.get("prompt"):
            self._confirm_restore(payload, ctx)

    def _fill_changes(self, payload, label):
        changes, risky = payload["changes"], set(payload["risky"])
        self.tbl_changes.setRowCount(len(changes))
        for row, (verb, path) in enumerate(sorted(changes, key=lambda c: c[1])):
            v = QTableWidgetItem(verb)
            v.setForeground(QColor(_VERB_COLOR.get(verb, "#6b7280")))
            v.setToolTip(_VERB_TIP.get(verb, ""))
            p = QTableWidgetItem(("⚠  " if path in risky else "") + path)
            if path in risky:
                p.setToolTip(
                    "This file's CURRENT content is something snapshots can't "
                    "capture, so a restore would destroy it — it refuses while "
                    "the file is there. Copy it somewhere safe first.")
            self.tbl_changes.setItem(row, 0, v)
            self.tbl_changes.setItem(row, 1, p)
        self.lbl_changes.setText(
            f"{len(changes)} file(s) differ from '{label}'" if changes else
            f"Nothing differs — your files still match '{label}'.")

    def _open_in_time_machine(self):
        m = self._selected()
        if m and self._open_state:
            self._open_state(self.cb_repo.currentText(), m["sha"])

    def _forget_mark(self):
        m = self._selected()
        if not m:
            return
        name = self.cb_repo.currentText()
        if QMessageBox.question(
                self, "Forget this mark",
                f"Forget the mark '{m.get('label')}'?\n\nOnly the NAME goes: "
                f"the state stays in the repo's history for as long as the "
                f"snapshots hold it.",
                QMessageBox.Yes | QMessageBox.Cancel,
                QMessageBox.Cancel) != QMessageBox.Yes:
            return
        ok, msg = self.c.forget_mark(name, m["ref"])  # one ref delete: instant
        if not ok:
            QMessageBox.warning(self, "Forget this mark", msg)
        self._reload()

    def _restore(self):
        """Roll back to a mark, in two steps like every other whole-repo
        restore: a background preview of what would change, then a confirm box
        that shows it — decide on facts, not faith."""
        self._compare(prompt=True)

    def _confirm_restore(self, payload, ctx):
        """GUI thread: the preview is on screen; ask, then do it."""
        name, sha = ctx["name"], ctx["sha"]
        label, when = ctx["label"], ctx["when"]
        changes, risky = payload["changes"], payload["risky"]
        if not changes and not risky:
            QMessageBox.information(
                self, "Restore",
                f"Nothing would change — your files already match '{label}'.")
            return
        n_rev = sum(1 for v, _ in changes if v == "revert")
        n_del = sum(1 for v, _ in changes if v == "delete")
        n_rec = sum(1 for v, _ in changes if v == "recreate")
        parts = []
        if n_rev:
            parts.append(f"{n_rev} file(s) go back to their '{label}' version")
        if n_del:
            parts.append(f"{n_del} file(s) created since then are removed")
        if n_rec:
            parts.append(f"{n_rec} file(s) deleted since then come back")
        box = QMessageBox(self)
        box.setWindowTitle("Restore to this mark")
        box.setIcon(QMessageBox.Warning)
        box.setText(f"Put '{name}' back to '{label}' ({when})?\n\n"
                    + "\n".join("•  " + p for p in parts))
        info = ("Reversible: the current state is captured as a snapshot first, "
                "so you can come back to this exact moment. See Details for the "
                "file list.")
        if risky:
            info = (f"⚠ {len(risky)} file(s) hold local content snapshots can't "
                    f"capture — the restore will REFUSE while they exist. Copy "
                    f"them somewhere safe first (⚠ in Details).\n\n" + info)
        box.setInformativeText(info)
        detail = "\n".join(f"⚠ can't capture  {p}" for p in risky)
        listing = "\n".join(f"{v:<9} {p}" for v, p in changes)
        box.setDetailedText((detail + "\n" + listing) if detail else listing)
        box.setStandardButtons(QMessageBox.Yes | QMessageBox.Cancel)
        box.setDefaultButton(QMessageBox.Cancel)
        confirmed = box.exec_() == QMessageBox.Yes
        box.deleteLater()  # parented boxes outlive exec_() otherwise
        if not confirmed:
            return
        self.btn_restore.setEnabled(False)
        self.busy.start("Restoring…")

        def work():
            try:
                ok2, msg = self.c.restore_repo(name, sha)
            except Exception as e:  # noqa: BLE001
                ok2, msg = False, str(e)
            try:
                # `name`, not the combo: a worker must never read a widget.
                self._restore_done.emit(
                    ok2, f"'{name}' is back to '{label}'." if ok2 else msg)
            except RuntimeError:
                pass  # tab destroyed while restoring

        threading.Thread(target=work, name="sincrogit-marks-restore",
                         daemon=True).start()

    def _on_restore_done(self, ok, msg):
        self.busy.stop()
        self.btn_restore.setEnabled(True)
        if ok:
            QMessageBox.information(self, "Restore", msg)
            self._reload()  # the restore is itself a new state
        else:
            QMessageBox.critical(self, "Restore failed", msg)
