"""SincroGit engine: orchestrates snapshots and seals per repo.

- The watcher marks each repo as "dirty" (with a timestamp).
- A tick loop (every few seconds) decides, per repo:
    * SNAPSHOT: if dirty, the debounce and the snapshot interval have elapsed
      -> stage (filtered) + `git commit --amend` over the WIP.
    * SEAL: if 2h have passed since the last sealed commit -> message (AI or
      fallback) + `git commit --amend` (seals) + new WIP + push.
    * PULL: every 10 min, fetch + (if the remote has something) rebase the WIP on top.

See DESIGN.md.
"""

import logging
import os
import threading
import time

from .ai import generate_commit_message
from .gitrepo import GitError, GitRepo
from .filefilter import FileFilter
from .messages import build_fallback_message
from .notify import notify
from .watcher import WatchManager

log = logging.getLogger("sincrogit.engine")


class RepoState:
    def __init__(self, repo: GitRepo, cfg, file_filter: FileFilter):
        self.repo = repo
        self.cfg = cfg
        self.file_filter = file_filter

        self._lock = threading.Lock()
        self.dirty = False
        self.last_event_mono = 0.0
        self.last_snapshot_mono = time.monotonic()
        self.last_seal_epoch = time.time()
        self.last_pull_mono = time.monotonic()
        self.paused = False       # set on rebase conflicts (not cleared on its own)
        self.user_paused = False  # set by the user from the GUI (per repo)

        # For the control panel (wall-clock time, not monotonic):
        self.branch = None
        self.last_snapshot_wall = None
        self.last_action = ""
        self.last_action_ts = 0.0

    def mark_dirty(self):
        with self._lock:
            self.dirty = True
            self.last_event_mono = time.monotonic()

    def read_dirty(self):
        with self._lock:
            return self.dirty, self.last_event_mono

    def clear_dirty_if_unchanged(self, seen_event_mono):
        """Clear 'dirty' only if no new events arrived during the snapshot."""
        with self._lock:
            if self.last_event_mono == seen_event_mono:
                self.dirty = False


class Engine:
    TICK_SEC = 3

    def __init__(self, config, emit_event=None):
        self.config = config
        self.states: list[RepoState] = []
        self.watch = None
        self._watch_ready = False
        self._stop = threading.Event()
        self._paused = threading.Event()  # GLOBAL pause (from the tray)
        # Serializes git work: the automatic tick and the manual actions
        # (Sync/Seal now from the tray, on another thread) must not touch the
        # same repo at the same time.
        self._oplock = threading.RLock()
        # Optional callback (repo, action, message, level) for the structured
        # log / GUI. If None, only the text logger is used.
        self._emit_event = emit_event

    # --------------------------------------------------------------- events
    _LEVELS = {"INFO": logging.INFO, "WARNING": logging.WARNING, "ERROR": logging.ERROR}

    def _emit(self, repo: str, action: str, message: str, level: str = "INFO"):
        """Record an action in the text log and the structured sink.

        In the text log the action is prepended (unless the message already
        starts with it) so the line is self-explanatory; the structured sink
        receives the raw message (the GUI already shows the action in its column).
        """
        prefix = f"[{repo}] " if repo else ""
        text = message if message.startswith(action) else f"{action}: {message}"
        log.log(self._LEVELS.get(level, logging.INFO), "%s%s", prefix, text)
        if self._emit_event is not None:
            try:
                self._emit_event(repo, action, message, level)
            except Exception:  # noqa: BLE001 — the GUI must not take down the engine
                pass

    def _mark_action(self, st: "RepoState", action: str):
        st.last_action = action
        st.last_action_ts = time.time()

    def _ensure_wip(self, st: "RepoState"):
        """Ensure HEAD is a WIP. If a WIP had to be created, HEAD was a non-WIP
        commit — typically the user committed manually. We respect that as a
        "manual seal" and reset the 2h clock to count from that commit.
        """
        if st.repo.ensure_wip():
            sealed = st.repo.last_sealed_time()
            st.last_seal_epoch = float(sealed) if sealed else time.time()
            self._emit(st.cfg.name, "info", "external commit detected; seal clock reset")

    # ------------------------------------------------ control from the tray
    def pause(self):
        if not self._paused.is_set():
            self._paused.set()
            self._emit("", "pause", "SincroGit paused", "WARNING")

    def resume(self):
        if self._paused.is_set():
            self._paused.clear()
            self._emit("", "resume", "SincroGit resumed")

    def is_paused(self) -> bool:
        return self._paused.is_set()

    def pause_repo(self, name: str) -> bool:
        """Pause sync for a single repo (user action)."""
        for st in self.states:
            if st.cfg.name == name and not st.user_paused:
                st.user_paused = True
                self._emit(name, "pause", "repo paused")
                return True
        return False

    def resume_repo(self, name: str) -> bool:
        """Resume a repo (clears both a user pause and a conflict pause)."""
        for st in self.states:
            if st.cfg.name == name and (st.user_paused or st.paused):
                st.user_paused = False
                st.paused = False
                self._emit(name, "resume", "repo resumed")
                return True
        return False

    def status(self) -> dict:
        """State snapshot for the panel (cached fields, no git calls)."""
        repos = []
        for st in self.states:
            repos.append({
                "name": st.cfg.name,
                "path": st.cfg.path,
                "branch": st.branch,
                "conflict_paused": st.paused,
                "user_paused": st.user_paused,
                "last_snapshot": st.last_snapshot_wall,
                "last_seal": st.last_seal_epoch,
                "last_action": st.last_action,
                "last_action_ts": st.last_action_ts,
                "push": st.cfg.push,
                "pull": st.cfg.pull,
            })
        return {
            "paused": self.is_paused(),
            "running": not self._stop.is_set(),
            "repos": repos,
        }

    # ------------------------------------------------------------- startup
    def setup(self, with_watcher: bool = False):
        if with_watcher:
            self.watch = WatchManager()
            self._watch_ready = True

        for rc in self.config.repos:
            repo = GitRepo(rc.path)
            if not repo.is_git_repo():
                log.error("Not a git repo (skipping): %s", rc.path)
                continue

            ff = FileFilter(rc.max_file_bytes, rc.extra_excludes)
            st = RepoState(repo, rc, ff)

            try:
                if repo.ensure_wip():
                    log.info("[%s] initial WIP created", rc.name)
            except GitError as e:
                log.error("[%s] could not initialize the WIP: %s", rc.name, e)
                continue

            sealed = repo.last_sealed_time()
            st.last_seal_epoch = float(sealed) if sealed else time.time()

            self.states.append(st)
            if self._watch_ready:
                self.watch.watch(rc.path, st.mark_dirty)

            branch = repo.current_branch()
            st.branch = branch
            if branch and branch != rc.branch:
                log.warning(
                    "[%s] current branch '%s' != configured '%s' (operating on the current one)",
                    rc.name, branch, rc.branch,
                )
            self._emit(rc.name, "startup", f"watching '{rc.path}' (branch {branch})")

    # ---------------------------------------------------------- loop / life
    def run(self):
        self.setup(with_watcher=True)
        # Keep running even with 0 repos: repos can be added later from the GUI.
        if not self.states:
            log.warning("No valid repos yet. Waiting (add some from the GUI).")

        self.watch.start()
        log.info("SincroGit running (%d repo[s]).", len(self.states))
        # Initial snapshot: captures pre-existing unsaved changes (e.g. after a
        # reboot) without waiting for a watcher event.
        self._initial_snapshot()
        # Initial sync on a separate thread: if the network hangs, the fetch has a
        # timeout but we don't want to block the loop's startup. The local
        # snapshots are already saved above, so nothing is lost if it's slow.
        threading.Thread(
            target=self._initial_sync, name="sincrogit-initsync", daemon=True
        ).start()
        try:
            while not self._stop.is_set():
                self.tick()
                self._stop.wait(self.TICK_SEC)
        finally:
            self.shutdown()

    def stop(self):
        self._stop.set()

    def tick(self):
        if self._paused.is_set():
            return  # global pause from the tray: don't touch any repo
        now_mono = time.monotonic()
        now_epoch = time.time()
        with self._oplock:
            for st in self.states:
                if st.paused or st.user_paused:
                    continue
                try:
                    self._maybe_sync(st, now_mono)
                    if st.paused:  # the sync may have paused it due to a conflict
                        continue
                    self._maybe_snapshot(st, now_mono)
                    self._maybe_seal(st, now_epoch)
                except GitError as e:
                    log.error("[%s] error in the cycle: %s", st.cfg.name, e)

    # ----------------------------------------------------------- operations
    def _maybe_snapshot(self, st: RepoState, now_mono: float):
        dirty, last_event = st.read_dirty()
        if not dirty:
            return
        if now_mono - last_event < st.cfg.debounce_sec:
            return
        if now_mono - st.last_snapshot_mono < st.cfg.snapshot_interval_sec:
            return
        if st.repo.is_busy():
            log.info("[%s] repo busy (merge/rebase), snapshot postponed", st.cfg.name)
            return

        if self._do_snapshot(st):
            st.last_snapshot_wall = time.time()
            self._mark_action(st, "snapshot")
            self._emit(st.cfg.name, "snapshot", "snapshot")
        st.last_snapshot_mono = now_mono
        st.clear_dirty_if_unchanged(last_event)

    def _initial_snapshot(self):
        for st in self.states:
            try:
                if st.repo.is_busy():
                    continue
                if self._do_snapshot(st):
                    log.info("[%s] initial snapshot", st.cfg.name)
            except GitError as e:
                log.error("[%s] error in initial snapshot: %s", st.cfg.name, e)

    def _do_snapshot(self, st: RepoState) -> bool:
        """Stage (filtered) and amend the WIP. Returns True if there were changes.

        Logs nothing: each caller writes its own log message according to the
        context (normal / initial / final snapshot).
        """
        self._ensure_wip(st)
        if st.repo.stage_changes(st.file_filter) and st.repo.has_staged_changes():
            st.repo.amend_keep_message()
            return True
        return False

    def _maybe_seal(self, st: RepoState, now_epoch: float):
        if now_epoch - st.last_seal_epoch < st.cfg.seal_interval_sec:
            return
        if st.repo.is_busy():
            return

        # Final snapshot to capture the latest changes before sealing.
        self._ensure_wip(st)
        if st.repo.stage_changes(st.file_filter) and st.repo.has_staged_changes():
            st.repo.amend_keep_message()

        if not st.repo.wip_differs_from_base():
            log.debug("[%s] nothing to seal", st.cfg.name)  # DEBUG: avoids noise over idle days
            st.last_seal_epoch = now_epoch  # reschedule the clock
            return

        title, body = self._seal_message(st)
        st.repo.seal(title, body)
        st.repo.new_wip()
        st.last_seal_epoch = now_epoch
        self._mark_action(st, "seal")
        self._emit(st.cfg.name, "seal", title)

        # Maintenance: pack the orphan objects left by the amends.
        st.repo.gc_auto()

        # Immediate push of the sealed commit (low latency). If the remote is
        # ahead and rejects it, the sync cycle reconciles (pull + retry).
        if st.cfg.push and st.repo.has_remote(st.cfg.remote):
            self._do_push(st)

    def _seal_message(self, st: RepoState):
        """Seal message: AI if possible, otherwise the deterministic fallback."""
        name_status = st.repo.name_status_for_seal()
        title, body = build_fallback_message(name_status)
        if self.config.ai.mode == "none":
            return title, body
        try:
            stat = st.repo.diff_stat_for_seal()
            text = st.repo.diff_text_for_seal(self.config.ai.max_diff_chars)
            ai_msg = generate_commit_message(self.config.ai, stat, text)
            if ai_msg and ai_msg[0]:
                return ai_msg
        except Exception as e:  # noqa: BLE001 — never block the seal because of the AI
            log.warning("[%s] AI failed, using fallback: %s", st.cfg.name, e)
        return title, body

    # ------------------------------------------------------------- sync (network)
    def _do_push(self, st: RepoState):
        repo, cfg = st.repo, st.cfg
        ok, msg = repo.push_sealed(cfg.remote, cfg.branch, timeout=cfg.git_timeout_sec)
        if ok:
            self._mark_action(st, "push")
            self._emit(cfg.name, "push", f"push OK -> {cfg.remote}/{cfg.branch}")
        else:
            # A rejected push (remote ahead) is reconciled in the next sync.
            self._emit(cfg.name, "push", f"push failed (will retry): {msg}", "WARNING")

    def _initial_sync(self):
        # _oplock: this method runs on its own thread and must not touch git at
        # the same time as the main loop's tick().
        with self._oplock:
            for st in self.states:
                try:
                    self._do_sync(st)
                except GitError as e:
                    log.error("[%s] error in initial sync: %s", st.cfg.name, e)
                st.last_pull_mono = time.monotonic()

    def _maybe_sync(self, st: RepoState, now_mono: float):
        if not (st.cfg.pull or st.cfg.push):
            return
        if now_mono - st.last_pull_mono < st.cfg.pull_interval_sec:
            return
        st.last_pull_mono = now_mono
        self._do_sync(st)

    def _do_sync(self, st: RepoState):
        """One network cycle: fetch + pull (rebase) if the remote is ahead, and
        push of pending sealed commits. Shares a single fetch.

        Rebase conflict -> abort, pause the repo and notify. Never force, never
        data loss. See §3.4 and §4 of DESIGN.md.
        """
        repo, cfg = st.repo, st.cfg
        if repo.is_busy() or not repo.has_remote(cfg.remote):
            return
        if not (cfg.pull or cfg.push):
            return

        if not repo.fetch(cfg.remote, timeout=cfg.git_timeout_sec):
            return  # already warned in the log
        remote_exists = repo.remote_branch_exists(cfg.remote, cfg.branch)

        # --- PULL: rebase the WIP onto the new remote commits ---
        if not self._pull_after_fetch(st, remote_exists):
            return  # conflict -> repo paused

        # --- PUSH: upload pending sealed commits (first push or retries) ---
        if cfg.push:
            if not remote_exists or repo.has_unpushed_sealed(cfg.remote, cfg.branch):
                self._do_push(st)

    def _pull_after_fetch(self, st: RepoState, remote_exists: bool) -> bool:
        """Rebase the WIP onto new remote commits (assumes fetch already ran).
        Returns False if a conflict occurred (and the repo was paused)."""
        repo, cfg = st.repo, st.cfg
        behind = repo.commits_behind(cfg.remote, cfg.branch) if (cfg.pull and remote_exists) else 0
        if behind <= 0:
            return True
        self._ensure_wip(st)
        if repo.stage_changes(st.file_filter) and repo.has_staged_changes():
            repo.amend_keep_message()
        if repo.rebase_onto_remote(cfg.remote, cfg.branch):
            self._mark_action(st, "pull")
            self._emit(cfg.name, "pull", f"integrated {behind} commit(s) from the remote")
            return True
        st.paused = True
        notify(
            "SincroGit: conflict",
            f"Autosync PAUSED on '{cfg.name}'. Resolve the rebase by hand.",
        )
        self._mark_action(st, "conflict")
        self._emit(cfg.name, "conflict", "rebase conflict; repo PAUSED", "ERROR")
        return False

    # ----------------------------------------------------------- shutdown
    def shutdown(self):
        log.info("Stopping SincroGit...")
        if self._watch_ready:
            self.watch.stop()
        # Final local snapshot (does NOT seal or push): ensures the latest state on disk.
        for st in self.states:
            try:
                if st.repo.is_busy():
                    continue
                if self._do_snapshot(st):
                    log.info("[%s] final snapshot", st.cfg.name)
            except GitError as e:
                log.error("[%s] error in final snapshot: %s", st.cfg.name, e)
        log.info("SincroGit stopped.")

    # ------------------------------------------- manual actions / tests
    # (May be launched from the tray on another thread -> guarded by _oplock.)
    def snapshot_all_now(self):
        with self._oplock:
            for st in self.states:
                try:
                    if st.repo.is_busy():
                        log.info("[%s] repo busy, skipped", st.cfg.name)
                        continue
                    if self._do_snapshot(st):
                        st.last_snapshot_wall = time.time()
                        self._mark_action(st, "snapshot")
                        self._emit(st.cfg.name, "snapshot", "snapshot (manual)")
                    else:
                        log.info("[%s] no changes to save", st.cfg.name)
                except GitError as e:
                    log.error("[%s] %s", st.cfg.name, e)

    def seal_all_now(self):
        now = time.time()
        with self._oplock:
            for st in self.states:
                st.last_seal_epoch = 0.0  # force the seal
                try:
                    self._maybe_seal(st, now)
                except GitError as e:
                    log.error("[%s] %s", st.cfg.name, e)

    def sync_all_now(self):
        with self._oplock:
            for st in self.states:
                try:
                    self._do_sync(st)
                except GitError as e:
                    log.error("[%s] %s", st.cfg.name, e)

    # ------------------------------------------------- live repo management
    def add_repo(self, rc) -> tuple:
        """Add a repo to the running engine (no restart). `rc` is a RepoConfig
        already merged with defaults. Returns (ok, message)."""
        repo = GitRepo(rc.path)
        if not repo.is_git_repo():
            return False, "not a git repository"
        if any(os.path.abspath(st.cfg.path) == os.path.abspath(rc.path) for st in self.states):
            return False, "repo already added"
        st = RepoState(repo, rc, FileFilter(rc.max_file_bytes, rc.extra_excludes))
        with self._oplock:
            try:
                repo.ensure_wip()
            except GitError as e:
                return False, str(e)
            sealed = repo.last_sealed_time()
            st.last_seal_epoch = float(sealed) if sealed else time.time()
            st.branch = repo.current_branch()
            self.states.append(st)
        if self._watch_ready and self.watch is not None:
            try:
                self.watch.watch(rc.path, st.mark_dirty)
            except Exception:  # noqa: BLE001 — watching is best-effort
                log.warning("[%s] could not start the watcher", rc.name)
        self._emit(rc.name, "startup", f"repo added: '{rc.path}' (branch {st.branch})")
        return True, "added"

    def seal_repo_now(self, name: str) -> tuple:
        """Force a seal (+push) of a single repo. Returns (ok, message)."""
        st = self.repo_state_by_name(name)
        if not st:
            return False, "repo not found"
        with self._oplock:
            st.last_seal_epoch = 0.0  # force the seal
            try:
                self._maybe_seal(st, time.time())
            except GitError as e:
                return False, str(e)
        return True, "sealed"

    def pull_repo_now(self, name: str) -> tuple:
        """Force a fetch + pull (rebase) of a single repo, no push. (ok, message)."""
        st = self.repo_state_by_name(name)
        if not st:
            return False, "repo not found"
        repo, cfg = st.repo, st.cfg
        with self._oplock:
            if repo.is_busy():
                return False, "repo busy"
            if not repo.has_remote(cfg.remote):
                return False, "no remote configured"
            if not repo.fetch(cfg.remote, timeout=cfg.git_timeout_sec):
                return False, "fetch failed"
            remote_exists = repo.remote_branch_exists(cfg.remote, cfg.branch)
            ok = self._pull_after_fetch(st, remote_exists)
        return (ok, "pulled" if ok else "conflict; repo paused")

    # ------------------------------------------------- history / restore
    def repo_state_by_name(self, name: str):
        for st in self.states:
            if st.cfg.name == name:
                return st
        return None

    def locate_file(self, abspath: str):
        """Map an absolute file path to (repo_name, relpath) or (None, None)."""
        abspath = os.path.abspath(abspath)
        for st in self.states:
            base = os.path.abspath(st.cfg.path)
            try:
                rel = os.path.relpath(abspath, base)
            except ValueError:  # different drive on Windows
                continue
            if rel != ".." and not rel.startswith(".." + os.sep):
                return st.cfg.name, rel.replace(os.sep, "/")
        return None, None

    def file_history(self, repo_name: str, relpath: str, limit: int = 50) -> list:
        st = self.repo_state_by_name(repo_name)
        if not st:
            return []
        try:
            return st.repo.file_history(relpath, limit)
        except GitError as e:
            log.error("[%s] history failed: %s", repo_name, e)
            return []

    def file_content_at(self, repo_name: str, relpath: str, sha: str):
        st = self.repo_state_by_name(repo_name)
        if not st:
            return None
        return st.repo.file_content_at(relpath, sha)

    def restore_file(self, repo_name: str, relpath: str, sha: str):
        """Restore a file to a past version. Returns (ok, message)."""
        st = self.repo_state_by_name(repo_name)
        if not st:
            return False, "repo not found"
        with self._oplock:  # don't race with the snapshot/seal cycle
            try:
                st.repo.restore_file(relpath, sha)
            except GitError as e:
                return False, str(e)
        self._emit(repo_name, "info", f"restored '{relpath}' from {sha[:8]}")
        return True, "restored"
