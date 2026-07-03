"""File history dialog (the "time machine").

Pick a repo and a file, browse its versions (sealed commits, intra-window
snapshots from the reflog, and — after "Fetch autosnaps" — other machines' live
mirrors), see a colored diff against the current file, and recover: either just
that file or the whole repository, to the chosen version.

Talks to the app through the `controller`:
  repo_list() -> [(name, path), ...]
  file_history(name, relpath) -> [ {sha, epoch, source, subject}, ... ]
  file_text_at(name, relpath, sha) -> str | None   (markdown for .docx)
  current_text(name, relpath) -> str               (working-tree text; md for .docx)
  restore_file(name, relpath, sha) -> (ok, message)
  restore_repo(name, sha) -> (ok, message)
  fetch_autosnaps(name) -> [ {host, branch, epoch, sha, ...}, ... ]
      (network: the dialog calls it from a background thread — it can take up to
      the repo's git timeout, and even wait on the engine's per-repo lock)
"""

import difflib
import html
import os
import threading
import time
from datetime import datetime

from PyQt5.QtCore import QModelIndex, QSortFilterProxyModel, Qt, pyqtSignal
from PyQt5.QtGui import QColor, QFont
from PyQt5.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QFileDialog,
    QFileSystemModel,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QTextEdit,
    QTreeView,
    QVBoxLayout,
    QWidget,
)

# Diff colors per theme flavor (keyed off the panel palette's background).
# *_hl are the stronger intra-line backgrounds: WHAT changed inside the line.
_DIFF_LIGHT = {"meta": "#8a929c", "hunk": "#2b6cb0", "add": "#1a7f37", "add_bg": "#e6f4eb",
               "del": "#cf222e", "del_bg": "#fbebed", "ctx": "#444444",
               "add_hl": "#9fdcb4", "del_hl": "#f4b6bd"}
_DIFF_DARK = {"meta": "#9aa3af", "hunk": "#6cb0f0", "add": "#4cc07a", "add_bg": "#203428",
              "del": "#ec7272", "del_bg": "#3a2628", "ctx": "#c8cdd4",
              "add_hl": "#2f5c3f", "del_hl": "#6e3a3f"}

# Version-type accents (foreground of the "Type" cell).
_TYPE_COLOR = {"sealed": "#2e9e5b", "snapshot": "#6b7280", "autosnap": "#8a63d2"}

# What each version type IS (the "Type" cell's tooltip).
_TYPE_TIP = {
    "sealed": "A permanent commit — part of the repo's history (pushed to the remote).",
    "snapshot": "An intra-window snapshot from the reflog (~30 days, this machine only).",
    "autosnap": "Another machine's live mirror (refs/autosnap), fetched from the remote.",
}


def _fmt(epoch) -> str:
    try:
        return datetime.fromtimestamp(epoch).strftime("%Y-%m-%d %H:%M:%S")
    except (ValueError, OSError, TypeError):
        return "—"


def _ago(epoch) -> str:
    """Compact relative time ("3 h ago"); the absolute stamp goes in the tooltip."""
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


def _mark_intraline(old_line: str, new_line: str, c: dict) -> tuple:
    """(old_html, new_html) of a modified line pair, with the spans that actually
    changed wrapped in a stronger background — you see WHAT changed inside the
    line, not just that the line changed."""
    o, n = [], []
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(
            None, old_line, new_line).get_opcodes():
        oseg, nseg = html.escape(old_line[i1:i2]), html.escape(new_line[j1:j2])
        if tag == "equal":
            o.append(oseg)
            n.append(nseg)
        else:
            if oseg:
                o.append(f'<span style="background:{c["del_hl"]};">{oseg}</span>')
            if nseg:
                n.append(f'<span style="background:{c["add_hl"]};">{nseg}</span>')
    return "".join(o), "".join(n)


def _diff_html(old_text: str, current_text: str, dark: bool = False) -> str:
    """Unified diff (old version -> current file) as colored HTML, theme-aware,
    with intra-line highlighting on paired -/+ lines."""
    c = _DIFF_DARK if dark else _DIFF_LIGHT
    diff = list(difflib.unified_diff(
        old_text.splitlines(), current_text.splitlines(),
        fromfile="selected version", tofile="current file", lineterm="",
    ))
    rows = []

    def emit(kind: str, body_html: str):
        rows.append(f'<span style="color:{c[kind]};background:{c[kind + "_bg"]};'
                    f'display:block;">{body_html or "&nbsp;"}</span>')

    in_hunk = False  # the ---/+++ headers only appear before the first @@
    i = 0
    while i < len(diff):
        ln = diff[i]
        if not in_hunk and ln.startswith(("+++", "---")):
            rows.append(f'<span style="color:{c["meta"]};">{html.escape(ln)}</span>')
            i += 1
        elif ln.startswith("@@"):
            in_hunk = True
            rows.append(f'<span style="color:{c["hunk"]};font-weight:bold;">'
                        f'{html.escape(ln)}</span>')
            i += 1
        elif ln.startswith("-"):
            # A run of removals followed by a run of additions is a MODIFICATION:
            # pair them index-wise and highlight what changed inside each line.
            dels = []
            while i < len(diff) and diff[i].startswith("-"):
                dels.append(diff[i][1:])
                i += 1
            adds = []
            while i < len(diff) and diff[i].startswith("+"):
                adds.append(diff[i][1:])
                i += 1
            paired = min(len(dels), len(adds))
            marked = [_mark_intraline(dels[k], adds[k], c) for k in range(paired)]
            for k, d in enumerate(dels):
                emit("del", "-" + (marked[k][0] if k < paired else html.escape(d)))
            for k, a in enumerate(adds):
                emit("add", "+" + (marked[k][1] if k < paired else html.escape(a)))
        elif ln.startswith("+"):
            emit("add", html.escape(ln))
            i += 1
        else:
            rows.append(f'<span style="color:{c["ctx"]};">{html.escape(ln) or "&nbsp;"}</span>')
            i += 1
    if not rows:
        return (f'<pre style="color:{c["meta"]};font-family:Consolas,monospace;'
                f'padding:8px;">(no differences vs the current file)</pre>')
    body = "\n".join(rows)
    return (f'<pre style="font-family:Consolas,monospace;font-size:10pt;'
            f'margin:0;padding:6px;line-height:1.35;">{body}</pre>')


class _NoGitProxy(QSortFilterProxyModel):
    """Hides .git (and git's lock litter) from the repo file tree."""

    def filterAcceptsRow(self, row: int, parent: QModelIndex) -> bool:  # noqa: N802
        idx = self.sourceModel().index(row, 0, parent)
        name = self.sourceModel().fileName(idx)
        return name != ".git"


class HistoryDialog(QDialog):
    # Emitted from background threads; delivered on the GUI thread (queued).
    _autosnaps_fetched = pyqtSignal(bool, int, str, str)   # ok, count, repo, error
    _preview_ready = pyqtSignal(bool, object, str, str)    # ok, payload|msg, sha, when
    _search_ready = pyqtSignal(str, str, list)             # relpath, term, [(sha, count)]

    def __init__(self, controller, parent=None, preselect_repo=None):
        super().__init__(parent)
        self.c = controller
        self.setWindowTitle("⏳g SincroGit — File history")
        self.resize(860, 600)
        self._versions = []

        v = QVBoxLayout(self)

        # --- Top row: repo + file + browse + show ---
        top = QHBoxLayout()
        top.addWidget(QLabel("Repo:"))
        self.cb_repo = QComboBox()
        for name, path in self.c.repo_list():
            self.cb_repo.addItem(name, path)
        if preselect_repo:
            i = self.cb_repo.findText(preselect_repo)
            if i >= 0:
                self.cb_repo.setCurrentIndex(i)
        top.addWidget(self.cb_repo)

        top.addWidget(QLabel("File:"))
        self.ed_file = QLineEdit()
        self.ed_file.setPlaceholderText("path relative to the repo, e.g. src/app.py")
        self.ed_file.returnPressed.connect(self.show_history)
        top.addWidget(self.ed_file, 1)

        btn_browse = QPushButton("Browse…")
        btn_browse.clicked.connect(self._browse)
        top.addWidget(btn_browse)
        btn_show = QPushButton("Show history")
        btn_show.clicked.connect(self.show_history)
        top.addWidget(btn_show)
        v.addLayout(top)

        # --- Second row: recovery-from-other-machines + diff toggle ---
        row2 = QHBoxLayout()
        self.btn_fetch = QPushButton("Fetch autosnaps (other machines)…")
        self.btn_fetch.setToolTip(
            "Download the live mirrors pushed by your other machines so their "
            "latest states show up in the history below (disaster recovery)."
        )
        self.btn_fetch.clicked.connect(self._fetch_autosnaps)
        self._autosnaps_fetched.connect(self._on_autosnaps_fetched)
        self._preview_ready.connect(self._on_preview_ready)
        self._search_ready.connect(self._on_search_ready)
        row2.addWidget(self.btn_fetch)
        self.ed_search = QLineEdit()
        self.ed_search.setPlaceholderText("find text across versions (e.g. a function name)…")
        self.ed_search.returnPressed.connect(self._find_in_versions)
        row2.addWidget(self.ed_search, 1)
        self.btn_search = QPushButton("Find")
        self.btn_search.setToolTip(
            "Count the text in every listed version and highlight the versions "
            "where it appeared, changed or vanished."
        )
        self.btn_search.clicked.connect(self._find_in_versions)
        row2.addWidget(self.btn_search)
        row2.addStretch(1)
        self.cb_diff = QCheckBox("Show diff vs current file")
        self.cb_diff.setChecked(True)
        self.cb_diff.stateChanged.connect(self._load_preview)
        row2.addWidget(self.cb_diff)
        v.addLayout(row2)

        # --- Main area: repo file tree (left) | versions + preview (right) ---
        outer = QSplitter(Qt.Horizontal)

        self.fs_model = QFileSystemModel(self)
        self.fs_proxy = _NoGitProxy(self)
        self.fs_proxy.setSourceModel(self.fs_model)
        self.tree = QTreeView()
        self.tree.setModel(self.fs_proxy)
        self.tree.setHeaderHidden(True)
        for col in range(1, 4):  # hide size/type/date — only names matter here
            self.tree.hideColumn(col)
        self.tree.setAlternatingRowColors(False)
        self.tree.clicked.connect(self._tree_clicked)
        outer.addWidget(self.tree)
        self.cb_repo.currentIndexChanged.connect(self._reroot_tree)
        self._reroot_tree()

        right = QSplitter(Qt.Vertical)

        self.tbl = QTableWidget(0, 4)
        self.tbl.setHorizontalHeaderLabels(["#", "When", "Type", "Message"])
        hdr = self.tbl.horizontalHeader()
        for col in range(3):
            hdr.setSectionResizeMode(col, QHeaderView.ResizeToContents)
        hdr.setSectionResizeMode(3, QHeaderView.Stretch)
        self.tbl.setSelectionBehavior(QTableWidget.SelectRows)
        self.tbl.setSelectionMode(QTableWidget.SingleSelection)
        self.tbl.setEditTriggers(QTableWidget.NoEditTriggers)
        self.tbl.setAlternatingRowColors(True)
        self.tbl.setShowGrid(False)
        self.tbl.verticalHeader().setVisible(False)
        self.tbl.itemSelectionChanged.connect(self._load_preview)
        right.addWidget(self.tbl)

        prev_box = QWidget()
        pv = QVBoxLayout(prev_box)
        pv.setContentsMargins(0, 0, 0, 0)
        pv.addWidget(QLabel("Preview / diff:"))
        self.preview = QTextEdit()
        self.preview.setReadOnly(True)
        self.preview.setFont(QFont("Consolas", 10))
        self.preview.setLineWrapMode(QTextEdit.NoWrap)
        pv.addWidget(self.preview)
        right.addWidget(prev_box)
        right.setSizes([260, 280])

        outer.addWidget(right)
        outer.setSizes([230, 630])
        outer.setStretchFactor(0, 0)
        outer.setStretchFactor(1, 1)
        v.addWidget(outer, 1)

        # --- Bottom buttons ---
        row = QHBoxLayout()
        self.lbl_info = QLabel()
        self.lbl_info.setProperty("cssClass", "muted")
        row.addWidget(self.lbl_info, 1)
        self.btn_saveas = QPushButton("Save a copy…")
        self.btn_saveas.setToolTip(
            "Write this version to a NEW file — recover it under another name; "
            "nothing is overwritten."
        )
        self.btn_saveas.clicked.connect(self._save_copy)
        self.btn_saveas.setEnabled(False)
        row.addWidget(self.btn_saveas)
        self.btn_restore = QPushButton("Restore this file")
        self.btn_restore.setProperty("cssClass", "primary")
        self.btn_restore.clicked.connect(self._restore)
        self.btn_restore.setEnabled(False)
        row.addWidget(self.btn_restore)
        self.btn_restore_repo = QPushButton("Restore ENTIRE repo…")
        self.btn_restore_repo.setProperty("cssClass", "danger")
        self.btn_restore_repo.setToolTip(
            "Set every tracked file back to this version (including deletions). "
            "Reversible: it's captured as a new snapshot."
        )
        self.btn_restore_repo.clicked.connect(self._restore_repo)
        self.btn_restore_repo.setEnabled(False)
        row.addWidget(self.btn_restore_repo)
        btn_close = QPushButton("Close")
        btn_close.clicked.connect(self.accept)
        row.addWidget(btn_close)
        v.addLayout(row)

    # ----------------------------------------------------------- file tree
    def _reroot_tree(self):
        """Point the tree at the selected repo's working tree."""
        base = self._repo_path()
        if not base or not os.path.isdir(base):
            return
        self.fs_model.setRootPath(base)
        src_root = self.fs_model.index(base)
        self.tree.setRootIndex(self.fs_proxy.mapFromSource(src_root))

    def _tree_clicked(self, proxy_idx):
        """Click on a file -> load its history right away (folders just expand)."""
        idx = self.fs_proxy.mapToSource(proxy_idx)
        if self.fs_model.isDir(idx):
            return
        full = self.fs_model.filePath(idx)
        try:
            rel = os.path.relpath(full, self._repo_path())
        except ValueError:
            return
        self.ed_file.setText(rel.replace(os.sep, "/"))
        self.show_history()

    # ----------------------------------------------------------- helpers
    def _repo_name(self) -> str:
        return self.cb_repo.currentText()

    def _repo_path(self) -> str:
        return self.cb_repo.currentData() or ""

    def _relpath(self) -> str:
        return self.ed_file.text().strip().replace("\\", "/")

    def _current_content(self) -> str:
        """The file's current content as readable text (markdown for .docx), or ''
        if missing. Routed through the controller so .docx is converted via pandoc."""
        rel = self._relpath()
        if not rel:
            return ""
        return self.c.current_text(self._repo_name(), rel)

    def _browse(self):
        base = self._repo_path()
        chosen, _ = QFileDialog.getOpenFileName(self, "Choose a file", base)
        if not chosen:
            return
        try:
            rel = os.path.relpath(os.path.abspath(chosen), os.path.abspath(base))
        except ValueError:
            rel = ""
        if not rel or rel.startswith(".."):
            QMessageBox.warning(
                self, "File history",
                "The chosen file is not inside the selected repo.",
            )
            return
        self.ed_file.setText(rel.replace(os.sep, "/"))
        self.show_history()

    # ----------------------------------------------------------- actions
    def show_history(self):
        rel = self._relpath()
        if not rel:
            return
        self._versions = self.c.file_history(self._repo_name(), rel)
        self.preview.clear()
        self.btn_restore.setEnabled(False)
        self.btn_restore_repo.setEnabled(False)
        self.btn_saveas.setEnabled(False)
        self.tbl.setRowCount(len(self._versions))
        for i, ver in enumerate(self._versions):
            source = ver.get("source", "")
            cells = [str(i + 1), _ago(ver["epoch"]), source, ver["subject"]]
            for j, val in enumerate(cells):
                item = QTableWidgetItem(val)
                if j == 1:
                    item.setToolTip(_fmt(ver["epoch"]))  # exact stamp on hover
                if j == 2:
                    item.setToolTip(_TYPE_TIP.get(source, ""))
                    if source in _TYPE_COLOR:
                        item.setForeground(QColor(_TYPE_COLOR[source]))
                self.tbl.setItem(i, j, item)
        if self._versions:
            self.lbl_info.setText(f"{len(self._versions)} version(s) of '{rel}'")
            self.tbl.selectRow(0)
        else:
            self.lbl_info.setText(f"No history for '{rel}'")

    def _selected_version(self):
        row = self.tbl.currentRow()
        if 0 <= row < len(self._versions):
            return self._versions[row]
        return None

    def _load_preview(self):
        ver = self._selected_version()
        self.btn_restore.setEnabled(ver is not None)
        self.btn_restore_repo.setEnabled(ver is not None)
        self.btn_saveas.setEnabled(ver is not None)
        if not ver:
            self.preview.clear()
            return
        content = self.c.file_text_at(self._repo_name(), self._relpath(), ver["sha"])
        if content is None:
            self.preview.setPlainText("(binary or unavailable)")
            return
        if self.cb_diff.isChecked():
            # Dark diff colors when the app palette is dark (TrayApp.theme).
            pal = getattr(self.c, "theme", None) or {}
            self.preview.setHtml(_diff_html(
                content, self._current_content(), dark=bool(pal.get("is_dark"))))
        else:
            self.preview.setPlainText(content)

    def _save_copy(self):
        """Recover a version WITHOUT overwriting anything: write it to a new
        file (suggested '<name> (<version stamp>)<ext>' next to the original)."""
        ver = self._selected_version()
        rel = self._relpath()
        if not ver or not rel:
            return
        stem, ext = os.path.splitext(os.path.basename(rel))
        try:
            stamp = datetime.fromtimestamp(ver["epoch"]).strftime("%Y-%m-%d %H.%M")
        except (ValueError, OSError, TypeError):
            stamp = ver["sha"][:8]
        suggested = os.path.join(
            self._repo_path(), os.path.dirname(rel), f"{stem} ({stamp}){ext}")
        dest, _ = QFileDialog.getSaveFileName(
            self, "Save a copy of this version", suggested)
        if not dest:
            return
        ok, msg = self.c.export_file_version(self._repo_name(), rel, ver["sha"], dest)
        if ok:
            QMessageBox.information(self, "Save a copy", f"Saved to:\n{dest}")
        else:
            QMessageBox.critical(self, "Save a copy", msg)

    def _find_in_versions(self):
        """Where did this text appear / change / vanish? Counts it in every
        listed version (background: one git show per version) and highlights
        the transitions in the table."""
        term = self.ed_search.text().strip()
        rel = self._relpath()
        if not term or not rel or not self._versions:
            return
        self.btn_search.setEnabled(False)
        self.lbl_info.setText(f"Searching '{term}' across {len(self._versions)} version(s)…")
        threading.Thread(
            target=self._do_search, args=(self._repo_name(), rel, term),
            name="sincrogit-history-search", daemon=True,
        ).start()

    def _do_search(self, name, rel, term):
        try:
            results = self.c.search_in_file_versions(name, rel, term)
        except Exception:  # noqa: BLE001 — an empty result reads as "not found"
            results = []
        try:
            self._search_ready.emit(rel, term, results)
        except RuntimeError:
            pass  # dialog closed while searching

    def _on_search_ready(self, rel, term, results):
        self.btn_search.setEnabled(True)
        if rel != self._relpath():
            return  # the user moved to another file meanwhile
        counts = dict(results)
        accent = QColor("#1E6FD9")
        transitions = 0
        for i, ver in enumerate(self._versions):
            n = counts.get(ver["sha"], 0)
            older = (counts.get(self._versions[i + 1]["sha"], 0)
                     if i + 1 < len(self._versions) else n)
            changed = n != older
            transitions += changed
            for j in range(self.tbl.columnCount()):
                item = self.tbl.item(i, j)
                if item is None:
                    continue
                item.setToolTip(f"'{term}': {n} occurrence(s) in this version")
                if changed:
                    item.setForeground(accent)
                elif j != 2:  # keep the Type cell's own color
                    item.setData(Qt.ForegroundRole, None)
        self.lbl_info.setText(
            f"'{term}' changes in {transitions} version(s) — highlighted in blue"
            if transitions else f"'{term}': no changes across these versions")

    def _restore(self):
        ver = self._selected_version()
        if not ver:
            return
        rel = self._relpath()
        when = _fmt(ver["epoch"])
        if QMessageBox.question(
            self, "Restore file",
            f"Restore '{rel}' to its version from {when} ({ver.get('source','')})?\n\n"
            f"The current content will be overwritten in the working tree "
            f"(and saved as a new snapshot).",
        ) != QMessageBox.Yes:
            return
        ok, msg = self.c.restore_file(self._repo_name(), rel, ver["sha"])
        if ok:
            QMessageBox.information(self, "Restore", f"Restored '{rel}' to {when}.")
            self.show_history()
        else:
            QMessageBox.critical(self, "Restore failed", msg)

    def _restore_repo(self):
        """Whole-repo restore, in two steps: first a background PREVIEW of what
        would change (git diff on a big repo can take a moment), then a confirm
        dialog that shows it — the user decides on facts, not on faith."""
        ver = self._selected_version()
        if not ver:
            return
        when = _fmt(ver["epoch"])
        self.btn_restore_repo.setEnabled(False)
        self.lbl_info.setText("Computing what the restore would change…")
        threading.Thread(
            target=self._do_restore_preview, args=(self._repo_name(), ver["sha"], when),
            name="sincrogit-restore-preview", daemon=True,
        ).start()

    def _do_restore_preview(self, name, sha, when):
        try:
            ok, payload = self.c.restore_repo_preview(name, sha)
        except Exception as e:  # noqa: BLE001 — surfaced in the dialog
            ok, payload = False, str(e)
        try:
            self._preview_ready.emit(ok, payload, sha, when)
        except RuntimeError:
            pass  # dialog closed while computing; nothing left to update

    def _on_preview_ready(self, ok, payload, sha, when):
        self.btn_restore_repo.setEnabled(True)
        self.lbl_info.setText("")
        if not ok:
            QMessageBox.warning(self, "Restore", f"Could not preview the restore: {payload}")
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
            f"Restore the WHOLE repository '{self._repo_name()}' to {when}?\n\n"
            + "\n".join("•  " + p for p in parts))
        info = ("Reversible: the restore is captured as a new snapshot, so File "
                "history can take you back to right before it. See Details for "
                "the file list.")
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
        if box.exec_() != QMessageBox.Yes:
            return
        ok2, msg = self.c.restore_repo(self._repo_name(), sha)
        if ok2:
            QMessageBox.information(self, "Restore", f"Repository restored to {when}.")
            self.show_history()
        else:
            QMessageBox.critical(self, "Restore failed", msg)

    def _fetch_autosnaps(self):
        """Kick off the fetch on a background thread: it's a network operation
        (up to the repo's git timeout) that may also wait on the engine's
        per-repo lock, so running it inline would freeze the whole GUI."""
        name = self._repo_name()
        self.btn_fetch.setEnabled(False)
        self.lbl_info.setText(f"Fetching autosnaps for '{name}'…")
        threading.Thread(
            target=self._do_fetch_autosnaps, args=(name,),
            name="sincrogit-fetch-autosnaps", daemon=True,
        ).start()

    def _do_fetch_autosnaps(self, name):
        try:
            states = self.c.fetch_autosnaps(name)
            ok, count, err = True, len(states), ""
        except Exception as e:  # noqa: BLE001 — surfaced in the dialog
            ok, count, err = False, 0, str(e)
        try:
            self._autosnaps_fetched.emit(ok, count, name, err)
        except RuntimeError:
            pass  # dialog closed while fetching; nothing left to update

    def _on_autosnaps_fetched(self, ok, count, name, err):
        self.btn_fetch.setEnabled(True)
        self.lbl_info.setText("")
        if not ok:
            QMessageBox.warning(self, "Autosnap", f"Fetch failed: {err}")
            return
        extra = "\nThey now appear in the history of any file." if count else ""
        QMessageBox.information(
            self, "Autosnap",
            f"{count} autosnap state(s) available for '{name}'.{extra}",
        )
        if self._relpath():
            self.show_history()
