"""SincroGit engine: orchestrates snapshots and seals per repo.

- The watcher marks each repo as "dirty" (with a timestamp).
- A tick loop (every few seconds) decides, per repo:
    * SNAPSHOT: if dirty, the debounce and the snapshot interval have elapsed
      -> stage (filtered) + `git commit --amend` over the WIP.
    * SEAL: if 2h have passed since the last sealed commit -> message (AI or
      fallback) + `git commit --amend` (seals) + new WIP + push.
    * PULL: every 10 min, fetch + (if the remote has something) rebase the WIP on top.

Concurrency model (see §7 of DESIGN.md):
- Each repo has its own `op_lock` that serializes git operations on THAT repo
  (the tick, the manual actions and the network worker can never touch the same
  repo at the same time), so two different repos proceed independently.
- Network operations (fetch/pull/push) run on short-lived background threads, so
  a slow fetch never blocks the tick loop, the local snapshots of other repos, or
  the GUI's manual actions. The tick only ever does fast LOCAL git work, and it
  uses a non-blocking lock acquire: if a repo is busy with a network task it is
  simply skipped until the next tick.

See DESIGN.md.
"""

import logging
import os
import threading
import time

from .ai import generate_commit_message
from .gitrepo import GitError, GitRepo, autosnap_host, resolve_pandoc
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

        self._lock = threading.Lock()      # guards the dirty flag + net_busy
        self.op_lock = threading.RLock()   # serializes git ops on THIS repo
        self.dirty = False
        self.last_event_mono = 0.0
        self.last_snapshot_mono = time.monotonic()
        self.last_seal_epoch = time.time()
        self.last_pull_mono = time.monotonic()
        self.net_busy = False     # a network task (fetch/pull/push/autosnap) is in flight
        self.autosnap_pending = False  # HEAD changed since the last autosnap push
        self.last_autosnap_mono = time.monotonic()
        self.paused = False       # set on rebase conflicts (not cleared on its own)
        self.user_paused = False  # set by the user from the GUI (per repo)
        self.dropped_warned = set()  # files already warned about (no longer snapshotted)
        self.off_branch = False   # HEAD is on a branch other than cfg.branch -> yield
        self._branch_cache = None      # last branch-check result (rate-limited, see below)
        self._branch_checked_mono = 0.0

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
    # The loop sleeps until the *soonest action that could actually fire* across
    # all repos (next snapshot/seal/autosnap/fetch), capped at MAX_TICK_SEC as a
    # safety backstop, and the watcher wakes it instantly on a change. So it does
    # NOT poll every few seconds while the next snapshot is minutes away.
    MAX_TICK_SEC = 60
    _BRANCH_CHECK_TTL = 15  # seconds: cap how often the per-repo branch check runs

    def __init__(self, config, emit_event=None):
        self.config = config
        self.states: list[RepoState] = []
        self.watch = None
        self._watch_ready = False
        self._stop = threading.Event()
        self._paused = threading.Event()  # GLOBAL pause (from the tray)
        # Set by the watcher (a change arrived) or by stop()/resume() to wake the
        # main loop out of its idle sleep immediately (adaptive tick).
        self._wake = threading.Event()
        # Guards the `states` list itself (append from add_repo vs. iteration from
        # the tick / GUI). Git work is NOT serialized here — that's each repo's
        # own op_lock (see the module docstring).
        self._states_lock = threading.Lock()
        # Optional callback (repo, action, message, level) for the structured
        # log / GUI. If None, only the text logger is used.
        self._emit_event = emit_event
        # This machine's name for the per-host autosnap ref (computed once).
        self._autosnap_host = autosnap_host()
        # Resolve pandoc once (for readable .docx diffs via textconv); None if absent.
        self._pandoc = resolve_pandoc(getattr(self.config, "pandoc_path", "pandoc"))
        if self._pandoc:
            log.info("pandoc found (%s): .docx diffs will be readable", self._pandoc)

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

    def _states_snapshot(self) -> list:
        """A stable copy of the repo list to iterate without racing add_repo."""
        with self._states_lock:
            return list(self.states)

    def _dirty_cb(self, st: "RepoState"):
        """Build the watcher callback for a repo: mark it dirty AND wake the main
        loop out of its idle sleep so the change is handled promptly."""
        def cb():
            st.mark_dirty()
            self._wake.set()
        return cb

    def _branch_ok(self, st: "RepoState"):
        """(on_configured_branch, current_branch_name). Fresh, uncached check."""
        current = st.repo.current_branch()
        return (current == st.cfg.branch), current

    def _ensure_on_branch(self, st: "RepoState", now_mono: float) -> bool:
        """Guard for the tick loop: only operate when HEAD is on the configured
        branch. If the user did a manual `git checkout`, SincroGit yields the repo
        instead of snapshotting/sealing the wrong branch and pushing it to the
        configured branch's ref. Logs the transition once each way. See §11 of
        DESIGN.md.

        Rate-limited (the check spawns `git rev-parse`): an overdue seal/sync on a
        wrong branch must not run it every tick. A switch is still noticed within
        _BRANCH_CHECK_TTL seconds.
        """
        if st._branch_cache is not None and now_mono - st._branch_checked_mono < self._BRANCH_CHECK_TTL:
            return st._branch_cache
        st._branch_checked_mono = now_mono
        ok, current = self._branch_ok(st)
        st._branch_cache = ok
        if not ok and not st.off_branch:
            st.off_branch = True
            st.branch = current
            self._emit(
                st.cfg.name, "info",
                f"HEAD on '{current}' != configured '{st.cfg.branch}'; autosync "
                f"paused until you switch back",
                "WARNING",
            )
        elif ok and st.off_branch:
            st.off_branch = False
            st.branch = current
            self._emit(st.cfg.name, "info", f"back on '{current}'; autosync resumed")
        return ok

    def _ensure_wip(self, st: "RepoState"):
        """Ensure HEAD is a WIP. If a WIP had to be created, HEAD was a non-WIP
        commit — typically the user committed manually. We respect that as a
        "manual seal" and reset the 2h clock to count from that commit.
        """
        if st.repo.ensure_wip():
            sealed = st.repo.last_sealed_time()
            st.last_seal_epoch = float(sealed) if sealed else time.time()
            self._emit(st.cfg.name, "info", "external commit detected; seal clock reset")

    def _ensure_docx_attributes(self, st: "RepoState"):
        """If the repo versions .docx (via extra_includes), map it to the pandoc
        diff driver in .gitattributes and keep it out of EOL normalization."""
        if not any("docx" in p.lower() for p in (st.cfg.extra_includes or [])):
            return
        try:
            if st.repo.ensure_gitattributes(["*.docx -text diff=pandoc"]):
                self._emit(st.cfg.name, "info",
                           ".gitattributes: mapped *.docx to the pandoc diff driver")
        except Exception:  # noqa: BLE001 — best-effort convenience
            pass

    def _stage(self, st: "RepoState") -> bool:
        """Stage the filtered changes, warning once about any tracked file that
        dropped out of auto-snapshot (see _note_dropped). Returns True if staged."""
        return st.repo.stage_changes(
            st.file_filter,
            on_drop=lambda rel, reason: self._note_dropped(st, rel, reason),
        )

    def _note_dropped(self, st: "RepoState", relpath: str, reason: str):
        """A previously-tracked file is no longer auto-snapshotted (e.g. it grew
        past the size limit or turned binary). Warn ONCE per file so the user can
        commit it by hand if they still want it versioned. See §5 of DESIGN.md.
        """
        if relpath in st.dropped_warned:
            return
        st.dropped_warned.add(relpath)
        self._emit(
            st.cfg.name, "info",
            f"'{relpath}' is no longer auto-snapshotted ({reason}); "
            f"commit it by hand if you want it versioned",
            "WARNING",
        )

    # ----------------------------------------------- background network task
    def _dispatch_network(self, st: "RepoState", label: str, fn) -> bool:
        """Run a network git op (fetch/pull/push) on a background thread so the
        tick thread never blocks on I/O. At most one network task per repo at a
        time; the task holds the repo's op_lock so it can't race the
        snapshot/seal cycle. Returns False if one is already in flight.
        """
        with st._lock:
            if st.net_busy:
                return False
            st.net_busy = True

        def worker():
            try:
                with st.op_lock:
                    fn()
            except GitError as e:
                log.error("[%s] %s failed: %s", st.cfg.name, label, e)
            except Exception as e:  # noqa: BLE001 — a worker must not die silently
                log.error("[%s] %s crashed: %s", st.cfg.name, label, e)
            finally:
                with st._lock:
                    st.net_busy = False

        threading.Thread(
            target=worker, name=f"sincrogit-{label}-{st.cfg.name}", daemon=True
        ).start()
        return True

    # ------------------------------------------------ control from the tray
    def pause(self):
        if not self._paused.is_set():
            self._paused.set()
            self._emit("", "pause", "SincroGit paused", "WARNING")

    def resume(self):
        if self._paused.is_set():
            self._paused.clear()
            self._wake.set()  # resume ticking immediately
            self._emit("", "resume", "SincroGit resumed")

    def is_paused(self) -> bool:
        return self._paused.is_set()

    def pause_repo(self, name: str) -> bool:
        """Pause sync for a single repo (user action)."""
        for st in self._states_snapshot():
            if st.cfg.name == name and not st.user_paused:
                st.user_paused = True
                self._emit(name, "pause", "repo paused")
                return True
        return False

    def resume_repo(self, name: str) -> bool:
        """Resume a repo (clears both a user pause and a conflict pause)."""
        for st in self._states_snapshot():
            if st.cfg.name == name and (st.user_paused or st.paused):
                st.user_paused = False
                st.paused = False
                self._wake.set()  # re-evaluate this repo without waiting
                self._emit(name, "resume", "repo resumed")
                return True
        return False

    def status(self) -> dict:
        """State snapshot for the panel (cached fields, no git calls)."""
        repos = []
        for st in self._states_snapshot():
            repos.append({
                "name": st.cfg.name,
                "path": st.cfg.path,
                "branch": st.branch,
                "conflict_paused": st.paused,
                "user_paused": st.user_paused,
                "off_branch": st.off_branch,
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
            repo = GitRepo(rc.path, pandoc=self._pandoc)
            if not repo.is_git_repo():
                log.error("Not a git repo (skipping): %s", rc.path)
                continue

            ff = FileFilter(rc.max_file_bytes, rc.extra_excludes,
                            rc.extra_includes, rc.max_include_bytes)
            st = RepoState(repo, rc, ff)

            try:
                if repo.ensure_wip():
                    log.info("[%s] initial WIP created", rc.name)
            except GitError as e:
                log.error("[%s] could not initialize the WIP: %s", rc.name, e)
                continue

            sealed = repo.last_sealed_time()
            st.last_seal_epoch = float(sealed) if sealed else time.time()

            with self._states_lock:
                self.states.append(st)
            if self._watch_ready:
                self.watch.watch(rc.path, self._dirty_cb(st))

            branch = repo.current_branch()
            st.branch = branch
            if branch and branch != rc.branch:
                st.off_branch = True  # the branch guard keeps autosync paused until you switch
                log.warning(
                    "[%s] current branch '%s' != configured '%s'; autosync will wait "
                    "until you switch to it",
                    rc.name, branch, rc.branch,
                )
            self._ensure_docx_attributes(st)
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
                if self._stop.is_set():
                    break
                # Adaptive sleep: short while actively working, long while idle.
                # The watcher (and stop/resume) set _wake to interrupt early, so an
                # idle laptop barely spins; timed actions (fetch/seal/autosnap) still
                # fire because we wake at least every IDLE_TICK_SEC.
                self._wake.wait(self._wait_seconds())
                self._wake.clear()
        finally:
            self.shutdown()

    def _wait_seconds(self) -> float:
        """How long to sleep before the next tick: the time until the soonest
        action that could fire across all repos (snapshot/seal/autosnap/fetch),
        capped at MAX_TICK_SEC (a backstop) and floored to avoid busy-looping.
        The watcher / stop / resume set _wake to interrupt this early, so new
        changes are handled at once. No fixed-rate polling while nothing is due.
        """
        if self._paused.is_set():
            return self.MAX_TICK_SEC
        now_mono = time.monotonic()
        now_epoch = time.time()
        soonest = float(self.MAX_TICK_SEC)
        for st in self._states_snapshot():
            if st.paused or st.user_paused or st.off_branch:
                continue
            cfg = st.cfg
            # next permanent seal
            soonest = min(soonest, st.last_seal_epoch + cfg.seal_interval_sec - now_epoch)
            # next remote sync (fetch/push)
            if cfg.pull or cfg.push:
                soonest = min(soonest, st.last_pull_mono + cfg.pull_interval_sec - now_mono)
            # next snapshot — only if there are pending changes
            if st.dirty:
                snap = max(st.last_event_mono + cfg.debounce_sec,
                           st.last_snapshot_mono + cfg.snapshot_interval_sec) - now_mono
                soonest = min(soonest, snap)
            # next autosnap — only if the live mirror is stale
            if cfg.autosnap and st.autosnap_pending:
                soonest = min(soonest, st.last_autosnap_mono + cfg.autosnap_interval_sec - now_mono)
        return max(1.0, min(soonest, float(self.MAX_TICK_SEC)))

    def stop(self):
        self._stop.set()
        self._wake.set()  # interrupt the idle wait so shutdown is prompt

    def tick(self):
        if self._paused.is_set():
            return  # global pause from the tray: don't touch any repo
        now_mono = time.monotonic()
        now_epoch = time.time()
        for st in self._states_snapshot():
            if st.paused or st.user_paused:
                continue
            try:
                if not self._ensure_on_branch(st, now_mono):
                    continue  # user switched branches (git checkout): yield this repo
                self._maybe_sync(st, now_mono)      # dispatched to a worker; returns at once
                self._maybe_snapshot(st, now_mono)  # local; skipped if a worker holds the repo
                self._maybe_seal(st, now_epoch)     # local seal; push dispatched to a worker
                self._maybe_autosnap(st, now_mono)  # live mirror; dispatched to a worker
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
        # Don't block the tick if a network op holds this repo: skip and retry.
        if not st.op_lock.acquire(blocking=False):
            return
        try:
            if st.repo.is_busy():
                log.info("[%s] repo busy (merge/rebase), snapshot postponed", st.cfg.name)
                return
            if self._do_snapshot(st):
                st.last_snapshot_wall = time.time()
                self._mark_action(st, "snapshot")
                self._emit(st.cfg.name, "snapshot", "snapshot")
            st.last_snapshot_mono = now_mono
            st.clear_dirty_if_unchanged(last_event)
        finally:
            st.op_lock.release()

    def _initial_snapshot(self):
        for st in self._states_snapshot():
            if not self._branch_ok(st)[0]:
                continue  # on another branch: don't snapshot it (see _ensure_on_branch)
            try:
                with st.op_lock:
                    if st.repo.is_busy():
                        continue
                    if self._do_snapshot(st):
                        log.info("[%s] initial snapshot", st.cfg.name)
            except GitError as e:
                log.error("[%s] error in initial snapshot: %s", st.cfg.name, e)

    def _do_snapshot(self, st: RepoState) -> bool:
        """Stage (filtered) and amend the WIP. Returns True if there were changes.

        Assumes the caller holds st.op_lock. Logs nothing: each caller writes its
        own log message according to the context (normal / initial / final).
        """
        self._ensure_wip(st)
        if self._stage(st) and st.repo.has_staged_changes():
            st.repo.amend_keep_message()
            st.autosnap_pending = True  # HEAD moved -> the live mirror is now stale
            return True
        return False

    def _maybe_seal(self, st: RepoState, now_epoch: float):
        if now_epoch - st.last_seal_epoch < st.cfg.seal_interval_sec:
            return
        # Don't block the tick if a network op holds this repo: skip and retry.
        if not st.op_lock.acquire(blocking=False):
            return
        try:
            sealed = self._do_seal(st, now_epoch)
            push_due = sealed and st.cfg.push and st.repo.has_remote(st.cfg.remote)
        finally:
            st.op_lock.release()
        # Push the sealed commit on a background thread so the tick never blocks
        # on the network. If the remote is ahead and rejects it, the sync cycle
        # reconciles (pull + retry).
        if push_due:
            self._dispatch_network(st, "push", lambda: self._do_push(st))

    def _do_seal(self, st: RepoState, now_epoch: float, message: str | None = None) -> bool:
        """Core seal: final snapshot + reword the WIP + new WIP. Returns True if a
        seal happened. Assumes the caller holds st.op_lock. Does NOT push.

        `message` (a developer's manual commit message) overrides the automatic
        AI/fallback message; its first line is the subject, the rest the body.
        """
        if st.repo.is_busy():
            return False

        # Final snapshot to capture the latest changes before sealing.
        self._ensure_wip(st)
        if self._stage(st) and st.repo.has_staged_changes():
            st.repo.amend_keep_message()

        if not st.repo.wip_differs_from_base():
            log.debug("[%s] nothing to seal", st.cfg.name)  # DEBUG: avoids noise over idle days
            st.last_seal_epoch = now_epoch  # reschedule the clock
            return False

        if message is not None:
            title, _, body = message.strip().partition("\n")
            title, body = title.strip(), body.strip()
        else:
            title, body = self._seal_message(st)
        st.repo.seal(title, body)
        st.repo.new_wip()
        st.last_seal_epoch = now_epoch
        st.autosnap_pending = True  # HEAD moved (new WIP) -> refresh the live mirror
        self._mark_action(st, "seal")
        self._emit(st.cfg.name, "seal", title)

        # Maintenance: pack the orphan objects left by the amends.
        st.repo.gc_auto()
        return True

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
        """Push the last sealed commit. Assumes the caller holds st.op_lock
        (it always runs inside a _dispatch_network worker)."""
        repo, cfg = st.repo, st.cfg
        ok, msg = repo.push_sealed(cfg.remote, cfg.branch, timeout=cfg.git_timeout_sec)
        if ok:
            self._mark_action(st, "push")
            self._emit(cfg.name, "push", f"push OK -> {cfg.remote}/{cfg.branch}")
        else:
            # A rejected push (remote ahead) is reconciled in the next sync.
            self._emit(cfg.name, "push", f"push failed (will retry): {msg}", "WARNING")

    # --------------------------------------------------------- autosnap (mirror)
    def _maybe_autosnap(self, st: RepoState, now_mono: float):
        """Mirror HEAD (incl. the WIP) to the remote on a background worker, so a
        total disk failure loses at most ~autosnap_interval. Only when something
        actually changed since the last mirror (autosnap_pending), so a dormant
        repo never pushes. See §12 of DESIGN.md."""
        if not st.cfg.autosnap or not st.autosnap_pending:
            return
        if now_mono - st.last_autosnap_mono < st.cfg.autosnap_interval_sec:
            return
        if not st.repo.has_remote(st.cfg.remote):
            return
        st.last_autosnap_mono = now_mono
        self._dispatch_network(st, "autosnap", lambda: self._do_autosnap(st))

    def _do_autosnap(self, st: RepoState):
        """Force-push HEAD to this host's autosnap ref. Assumes op_lock (runs in a
        _dispatch_network worker). Clears the pending flag only on success."""
        repo, cfg = st.repo, st.cfg
        ok, msg = repo.push_autosnap(
            cfg.remote, cfg.branch, self._autosnap_host, timeout=cfg.git_timeout_sec
        )
        ref = repo.autosnap_ref(self._autosnap_host, cfg.branch)
        if ok:
            st.autosnap_pending = False
            self._mark_action(st, "autosnap")
            self._emit(cfg.name, "autosnap", f"mirror pushed -> {cfg.remote}/{ref}")
        else:
            # Keep pending=True so the next interval retries.
            self._emit(cfg.name, "autosnap", f"mirror push failed (will retry): {msg}", "WARNING")

    def _initial_sync(self):
        # Runs on its own thread at startup. Per-repo op_lock keeps each repo's
        # sync from racing the tick (which simply skips a repo that's busy).
        for st in self._states_snapshot():
            if self._branch_ok(st)[0]:
                try:
                    with st.op_lock:
                        self._do_sync(st)
                except GitError as e:
                    log.error("[%s] error in initial sync: %s", st.cfg.name, e)
            st.last_pull_mono = time.monotonic()

    def _maybe_sync(self, st: RepoState, now_mono: float):
        if not (st.cfg.pull or st.cfg.push):
            return
        if now_mono - st.last_pull_mono < st.cfg.pull_interval_sec:
            return
        # Set the clock before dispatching so we don't re-queue it every tick
        # while the worker is still running.
        st.last_pull_mono = now_mono
        self._dispatch_network(st, "sync", lambda: self._do_sync(st))

    def _do_sync(self, st: RepoState):
        """One network cycle: fetch + pull (rebase) if the remote is ahead, and
        push of pending sealed commits. Shares a single fetch.

        Assumes the caller holds st.op_lock (runs inside a network worker or a
        manual action). Rebase conflict -> abort, pause the repo and notify.
        Never force, never data loss. See §3.4 and §4 of DESIGN.md.
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
        if self._stage(st) and repo.has_staged_changes():
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
        for st in self._states_snapshot():
            try:
                # Wait briefly for any in-flight network op to release the repo;
                # if it doesn't, skip (the process is exiting, workers are daemons).
                if not st.op_lock.acquire(timeout=5):
                    log.warning("[%s] busy at shutdown; final snapshot skipped", st.cfg.name)
                    continue
                try:
                    if st.repo.is_busy():
                        continue
                    if self._do_snapshot(st):
                        log.info("[%s] final snapshot", st.cfg.name)
                finally:
                    st.op_lock.release()
            except GitError as e:
                log.error("[%s] error in final snapshot: %s", st.cfg.name, e)
        log.info("SincroGit stopped.")

    # ------------------------------------------- manual actions / tests
    # (Launched from the tray on another thread -> each guarded by the repo's
    # op_lock so they can't race the tick or a network worker on the same repo.)
    def snapshot_all_now(self):
        for st in self._states_snapshot():
            ok, cur = self._branch_ok(st)
            if not ok:
                log.info("[%s] on '%s' != configured '%s', skipped", st.cfg.name, cur, st.cfg.branch)
                continue
            try:
                with st.op_lock:
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
        for st in self._states_snapshot():
            ok, cur = self._branch_ok(st)
            if not ok:
                log.info("[%s] on '%s' != configured '%s', skipped", st.cfg.name, cur, st.cfg.branch)
                continue
            try:
                # Push synchronously: this is a manual / CLI one-shot action (off
                # the tick thread), so the push must finish before we return —
                # otherwise `--seal-once` would exit before the push completes.
                with st.op_lock:
                    if self._do_seal(st, now) and st.cfg.push and st.repo.has_remote(st.cfg.remote):
                        self._do_push(st)
            except GitError as e:
                log.error("[%s] %s", st.cfg.name, e)

    def sync_all_now(self):
        for st in self._states_snapshot():
            ok, cur = self._branch_ok(st)
            if not ok:
                log.info("[%s] on '%s' != configured '%s', skipped", st.cfg.name, cur, st.cfg.branch)
                continue
            try:
                with st.op_lock:
                    self._do_sync(st)
            except GitError as e:
                log.error("[%s] %s", st.cfg.name, e)

    # ------------------------------------------------- live repo management
    def add_repo(self, rc) -> tuple:
        """Add a repo to the running engine (no restart). `rc` is a RepoConfig
        already merged with defaults. Returns (ok, message)."""
        repo = GitRepo(rc.path, pandoc=self._pandoc)
        if not repo.is_git_repo():
            return False, "not a git repository"
        with self._states_lock:
            if any(os.path.abspath(st.cfg.path) == os.path.abspath(rc.path) for st in self.states):
                return False, "repo already added"
        # The new repo isn't shared yet (not in `states`, no watcher), so this
        # git work needs no lock.
        st = RepoState(repo, rc, FileFilter(rc.max_file_bytes, rc.extra_excludes,
                                            rc.extra_includes, rc.max_include_bytes))
        try:
            repo.ensure_wip()
        except GitError as e:
            return False, str(e)
        sealed = repo.last_sealed_time()
        st.last_seal_epoch = float(sealed) if sealed else time.time()
        st.branch = repo.current_branch()
        with self._states_lock:
            self.states.append(st)
        if self._watch_ready and self.watch is not None:
            try:
                self.watch.watch(rc.path, self._dirty_cb(st))
            except Exception:  # noqa: BLE001 — watching is best-effort
                log.warning("[%s] could not start the watcher", rc.name)
        self._wake.set()  # pick up the new repo without waiting out the idle sleep
        self._ensure_docx_attributes(st)
        self._emit(rc.name, "startup", f"repo added: '{rc.path}' (branch {st.branch})")
        return True, "added"

    def seal_repo_now(self, name: str, message: str | None = None) -> tuple:
        """Force a seal (+push) of a single repo. With `message`, seals with the
        developer's own commit message (a manual "smart commit"). Returns (ok, msg)."""
        st = self.repo_state_by_name(name)
        if not st:
            return False, "repo not found"
        ok, cur = self._branch_ok(st)
        if not ok:
            return False, f"on branch '{cur}', not configured '{st.cfg.branch}'; switch back first"
        with st.op_lock:
            try:
                sealed = self._do_seal(st, time.time(), message=message)
                # Synchronous push (manual action, already off the tick thread).
                if sealed and st.cfg.push and st.repo.has_remote(st.cfg.remote):
                    self._do_push(st)
            except GitError as e:
                return False, str(e)
        return True, ("sealed" if sealed else "nothing to seal")

    def propose_seal_message(self, name: str) -> tuple:
        """Propose a Conventional-Commits message for a manual commit, WITHOUT
        committing. Returns (ok, title, body, files_text).

        The AI sees the cumulative diff since the developer's last manual commit
        (skipping the automatic 'sincro:'/'auto:' seals), so it can summarize the
        whole unit of work; the body notes that scope honestly. The file list is
        what actually enters this commit (the current WIP window). The slow AI call
        runs WITHOUT holding the repo lock.
        """
        st = self.repo_state_by_name(name)
        if not st:
            return False, "", "", "repo not found"
        ok, cur = self._branch_ok(st)
        if not ok:
            return False, "", "", f"on branch '{cur}', not configured '{st.cfg.branch}'; switch back first"

        # Quick git work under the lock: capture the WIP and the diffs.
        with st.op_lock:
            if st.repo.is_busy():
                return False, "", "", "repo busy (merge/rebase in progress)"
            self._ensure_wip(st)
            if self._stage(st) and st.repo.has_staged_changes():
                st.repo.amend_keep_message()
            if not st.repo.wip_differs_from_base():
                return False, "", "", "nothing to commit"
            base = st.repo.last_manual_sha()
            name_status = st.repo.name_status_for_seal()  # files in THIS commit (WIP window)
            stat = st.repo.diff_stat_for_seal(base=base)
            text = st.repo.diff_text_for_seal(self.config.ai.max_diff_chars, base=base)

        # Slow AI call OUTSIDE the lock (it doesn't touch git).
        title, body = build_fallback_message(name_status, prefix="chore")
        if self.config.ai.mode != "none":
            try:
                ai_msg = generate_commit_message(self.config.ai, stat, text, manual=True)
                if ai_msg and ai_msg[0]:
                    title, body = ai_msg
            except Exception as e:  # noqa: BLE001 — never block on the AI
                log.warning("[%s] AI proposal failed, using fallback: %s", name, e)

        # Honest disclosure: the message summarizes work spread across earlier seals.
        if base:
            note = (f"(SincroGit: cumulative summary since {base[:8]}; some of this code "
                    f"is already in earlier sincro: commits)")
            body = f"{body}\n\n{note}" if body else note

        files_text = "\n".join(f"{s}  {p}" for s, p in name_status)
        return True, title, body, files_text

    def pull_repo_now(self, name: str) -> tuple:
        """Force a fetch + pull (rebase) of a single repo, no push. (ok, message)."""
        st = self.repo_state_by_name(name)
        if not st:
            return False, "repo not found"
        ok, cur = self._branch_ok(st)
        if not ok:
            return False, f"on branch '{cur}', not configured '{st.cfg.branch}'; switch back first"
        repo, cfg = st.repo, st.cfg
        with st.op_lock:
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
        for st in self._states_snapshot():
            if st.cfg.name == name:
                return st
        return None

    def locate_file(self, abspath: str):
        """Map an absolute file path to (repo_name, relpath) or (None, None)."""
        abspath = os.path.abspath(abspath)
        for st in self._states_snapshot():
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

    def file_text_at(self, repo_name: str, relpath: str, sha: str):
        """Readable text of a past version (markdown for .docx). For the GUI diff."""
        st = self.repo_state_by_name(repo_name)
        return st.repo.file_text_at(relpath, sha) if st else None

    def worktree_text(self, repo_name: str, relpath: str):
        """Readable text of the current working-tree file (markdown for .docx)."""
        st = self.repo_state_by_name(repo_name)
        return st.repo.worktree_text(relpath) if st else ""

    def restore_file(self, repo_name: str, relpath: str, sha: str):
        """Restore a file to a past version. Returns (ok, message)."""
        st = self.repo_state_by_name(repo_name)
        if not st:
            return False, "repo not found"
        with st.op_lock:  # don't race with the snapshot/seal cycle
            try:
                st.repo.restore_file(relpath, sha)
            except GitError as e:
                return False, str(e)
        self._emit(repo_name, "info", f"restored '{relpath}' from {sha[:8]}")
        return True, "restored"

    # ---------------------------------------------- autosnap recovery (cross-machine)
    def fetch_autosnaps(self, repo_name: str) -> list:
        """Fetch every machine's autosnap refs from the remote and return them
        (newest first). Use this on another machine to recover after a disk
        failure. Each item: ref, host, branch, sha, epoch, subject."""
        st = self.repo_state_by_name(repo_name)
        if not st:
            return []
        with st.op_lock:
            if st.repo.has_remote(st.cfg.remote):
                st.repo.fetch_autosnaps(st.cfg.remote, timeout=st.cfg.git_timeout_sec)
            return st.repo.list_autosnap_refs()

    def list_autosnaps(self, repo_name: str) -> list:
        """Locally-known autosnap states (no network). Call fetch_autosnaps first
        to refresh from the remote."""
        st = self.repo_state_by_name(repo_name)
        return st.repo.list_autosnap_refs() if st else []

    def restore_repo(self, repo_name: str, sha: str):
        """Restore the WHOLE working tree to the state at `sha` (a sealed/snapshot/
        autosnap commit), captured into the WIP so it's versioned and reversible.
        HEAD is not moved. Returns (ok, message)."""
        st = self.repo_state_by_name(repo_name)
        if not st:
            return False, "repo not found"
        with st.op_lock:  # don't race with the snapshot/seal cycle
            try:
                self._ensure_wip(st)
                st.repo.restore_tree(sha)
                if st.repo.has_staged_changes():
                    st.repo.amend_keep_message()
                st.autosnap_pending = True
            except GitError as e:
                return False, str(e)
        self._emit(repo_name, "info", f"restored whole repo to {sha[:8]}")
        return True, "restored"
