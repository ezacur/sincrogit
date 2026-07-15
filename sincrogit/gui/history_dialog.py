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
  file_hunks(name, relpath, sha) -> (ok, {"base": str, "hunks": [...]})
  restore_hunks(name, relpath, sha, selected, base) -> (ok, message)
  restore_repo(name, sha) -> (ok, message)
  fetch_autosnaps(name) -> [ {host, branch, epoch, sha, ...}, ... ]
      (network: the dialog calls it from a background thread — it can take up to
      the repo's git timeout, and even wait on the engine's per-repo lock)
"""

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

from .diff import diff_html

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
    _history_ready = pyqtSignal(int, str, list)            # gen, relpath, versions
    _file_preview_ready = pyqtSignal(int, object, object)  # gen, content|None, current
    _restore_done = pyqtSignal(bool, str)                  # ok, message

    def __init__(self, controller, parent=None, preselect_repo=None):
        super().__init__(parent)
        self.c = controller
        self.setWindowTitle("⏳g SincroGit — File history")
        self.resize(860, 600)
        self._versions = []
        # Monotonic tokens so a slow git call whose result arrives after the user
        # moved on (picked another file / version) is discarded, not shown stale.
        self._history_gen = 0
        self._preview_gen = 0

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
        self._history_ready.connect(self._on_history_ready)
        self._file_preview_ready.connect(self._on_file_preview_ready)
        self._restore_done.connect(self._on_restore_done)
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
        self.cb_repo.currentIndexChanged.connect(self._repo_changed)
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
        self.btn_hunks = QPushButton("Restore hunks…")
        self.btn_hunks.setToolTip(
            "Roll back only SOME of the changed blocks to this version, keeping "
            "your other current edits. Text files only."
        )
        self.btn_hunks.clicked.connect(self._restore_hunks)
        self.btn_hunks.setEnabled(False)
        row.addWidget(self.btn_hunks)
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
    def _repo_changed(self):
        """Switching repo invalidates everything on screen: the listed versions'
        shas belong to the OLD repo (a Restore would hand one to the new repo's
        engine), and the typed path likely doesn't exist there. Clear it all,
        then reroot the tree."""
        self._history_gen += 1   # discard any in-flight history/preview result
        self._preview_gen += 1
        self._versions = []
        self.tbl.setRowCount(0)
        self.preview.clear()
        self.ed_file.clear()
        self.lbl_info.setText("")
        for b in (self.btn_restore, self.btn_hunks, self.btn_restore_repo,
                  self.btn_saveas):
            b.setEnabled(False)
        self._reroot_tree()

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
        """Load the file's versions on a background thread: file_history runs
        git log + a cat-file batch (and pandoc/python-pptx for documents), which
        would freeze the window inline on a big file or a slow converter."""
        rel = self._relpath()
        if not rel:
            return
        self._history_gen += 1
        gen = self._history_gen
        self.preview.clear()
        self.btn_restore.setEnabled(False)
        self.btn_hunks.setEnabled(False)
        self.btn_restore_repo.setEnabled(False)
        self.btn_saveas.setEnabled(False)
        self.lbl_info.setText(f"Loading history of '{rel}'…")
        threading.Thread(
            target=self._do_load_history, args=(gen, self._repo_name(), rel),
            name="sincrogit-history", daemon=True,
        ).start()

    def _do_load_history(self, gen, name, rel):
        try:
            versions = self.c.file_history(name, rel)
        except Exception:  # noqa: BLE001 — surfaced as "no history" below
            versions = []
        try:
            self._history_ready.emit(gen, rel, versions)
        except RuntimeError:
            pass  # dialog closed while loading

    def _on_history_ready(self, gen, rel, versions):
        if gen != self._history_gen:
            return  # a newer file/history request superseded this one
        self._versions = versions
        self.tbl.setRowCount(len(versions))
        for i, ver in enumerate(versions):
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
        if versions:
            self.lbl_info.setText(f"{len(versions)} version(s) of '{rel}'")
            self.tbl.selectRow(0)  # triggers _load_preview
        else:
            self.lbl_info.setText(f"No history for '{rel}'")

    def _selected_version(self):
        row = self.tbl.currentRow()
        if 0 <= row < len(self._versions):
            return self._versions[row]
        return None

    def _load_preview(self):
        """Fetch the selected version's text (and the current file, for the diff)
        on a background thread: file_text_at / current_text run `git show` plus,
        for .docx/.pptx, pandoc/python-pptx — up to tens of seconds inline."""
        ver = self._selected_version()
        self.btn_restore.setEnabled(ver is not None)
        self.btn_hunks.setEnabled(ver is not None)
        self.btn_restore_repo.setEnabled(ver is not None)
        self.btn_saveas.setEnabled(ver is not None)
        if not ver:
            self.preview.clear()
            return
        self._preview_gen += 1
        gen = self._preview_gen
        want_diff = self.cb_diff.isChecked()
        self.preview.setPlainText("Loading…")
        threading.Thread(
            target=self._do_load_preview,
            args=(gen, self._repo_name(), self._relpath(), ver["sha"], want_diff),
            name="sincrogit-preview", daemon=True,
        ).start()

    def _do_load_preview(self, gen, name, rel, sha, want_diff):
        try:
            content = self.c.file_text_at(name, rel, sha)
            current = (self.c.current_text(name, rel)
                       if want_diff and content is not None else "")
        except Exception:  # noqa: BLE001 — shown as "unavailable" below
            content, current = None, ""
        try:
            self._file_preview_ready.emit(gen, content, current)
        except RuntimeError:
            pass  # dialog closed while loading

    def _on_file_preview_ready(self, gen, content, current):
        if gen != self._preview_gen:
            return  # a newer selection superseded this preview
        if content is None:
            self.preview.setPlainText("(binary or unavailable)")
            return
        if self.cb_diff.isChecked():
            # Dark diff colors when the app palette is dark (TrayApp.theme).
            pal = getattr(self.c, "theme", None) or {}
            self.preview.setHtml(diff_html(content, current, dark=bool(pal.get("is_dark"))))
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
        # Default to Cancel: an accidental Enter on this destructive confirmation
        # shouldn't overwrite the working tree.
        if QMessageBox.question(
            self, "Restore file",
            f"Restore '{rel}' to its version from {when} ({ver.get('source','')})?\n\n"
            f"The current content will be overwritten in the working tree "
            f"(and saved as a new snapshot).",
            QMessageBox.Yes | QMessageBox.Cancel, QMessageBox.Cancel,
        ) != QMessageBox.Yes:
            return
        # Background thread: restore_file takes the repo's op_lock and does several
        # git ops — inline it would freeze the GUI whenever the engine holds that
        # lock (e.g. a push/pull up to the git network timeout).
        self._begin_restore(f"Restoring '{rel}'…")
        threading.Thread(
            target=self._do_restore,
            args=("file", self._repo_name(), rel, ver["sha"], f"Restored '{rel}' to {when}."),
            name="sincrogit-restore", daemon=True,
        ).start()

    def _begin_restore(self, note: str):
        """Disable the restore/save buttons and show a note while a restore runs
        on a worker (re-enabled by _on_restore_done)."""
        self.btn_restore.setEnabled(False)
        self.btn_hunks.setEnabled(False)
        self.btn_restore_repo.setEnabled(False)
        self.btn_saveas.setEnabled(False)
        self.lbl_info.setText(note)

    def _do_restore(self, kind, name, rel, sha, success_text):
        try:
            ok, msg = (self.c.restore_file(name, rel, sha) if kind == "file"
                       else self.c.restore_repo(name, sha))
        except Exception as e:  # noqa: BLE001 — reported in the dialog
            ok, msg = False, str(e)
        try:
            self._restore_done.emit(ok, success_text if ok else msg)
        except RuntimeError:
            pass  # dialog closed while restoring

    def _on_restore_done(self, ok, msg):
        ver = self._selected_version()
        self.btn_restore.setEnabled(ver is not None)
        self.btn_hunks.setEnabled(ver is not None)
        self.btn_restore_repo.setEnabled(ver is not None)
        self.btn_saveas.setEnabled(ver is not None)
        self.lbl_info.setText("")
        if ok:
            QMessageBox.information(self, "Restore", msg)
            self.show_history()  # reload (threaded); the restore is a new version
        else:
            QMessageBox.critical(self, "Restore failed", msg)

    def _restore_hunks(self):
        """Open the hunk picker for the selected version. It applies the
        restore itself; on success reload history (the restore is a new
        version, just like a whole-file one)."""
        from .hunk_dialog import HunkRestoreDialog
        ver = self._selected_version()
        rel = self._relpath()
        if not ver or not rel:
            return
        dlg = HunkRestoreDialog(self.c, self._repo_name(), rel, ver["sha"],
                                _fmt(ver["epoch"]), parent=self)
        accepted = dlg.exec_() == QDialog.Accepted
        dlg.deleteLater()  # parented dialogs outlive exec_() otherwise
        if accepted:
            self.show_history()

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
        confirmed = box.exec_() == QMessageBox.Yes
        box.deleteLater()  # parented boxes outlive exec_() otherwise
        if not confirmed:
            return
        # Background thread (same reason as _restore): restore_repo holds the
        # repo's op_lock and rewrites the whole worktree.
        self._begin_restore("Restoring the whole repository…")
        threading.Thread(
            target=self._do_restore,
            args=("repo", self._repo_name(), None, sha, f"Repository restored to {when}."),
            name="sincrogit-restore-repo", daemon=True,
        ).start()

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
