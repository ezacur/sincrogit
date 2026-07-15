"""SincroGit engine: orchestrates snapshots and seals per repo (shadow model).

- The watcher marks each repo as "dirty" (with a timestamp).
- A tick loop (every few seconds) decides, per repo:
    * SNAPSHOT: if dirty, the debounce and the snapshot interval have elapsed
      -> capture the worktree (filtered) through a PRIVATE index into a commit
      on refs/sincro/wip/<branch>. The user's HEAD/index/status are untouched.
    * SEAL: if the seal interval (default 6h) has passed since the last sealed
      commit -> message (AI or fallback) + the snapshot tree becomes ONE real
      commit on the branch (the shadow chain re-anchors there) + push.
    * AUTOSNAP: every ~30 min, force-push the shadow tip to a per-host side ref
      (disaster backup + cross-machine handoff substrate).
    * PULL: every 10 min, fetch + (if the remote has something) snapshot first,
      then rebase the local branch with --autostash (the user's edits live
      uncommitted in the worktree).

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

import dataclasses
import logging
import math
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
    """Per-repo mutable state shared by the engine loop, the workers and the GUI.

    The pause-like conditions are separate flags on purpose — each has its own
    owner and lifetime (`paused`: rebase conflict, cleared by resume_repo;
    `user_paused`: the GUI's per-repo pause; `off_branch`: HEAD left cfg.branch,
    clears itself when the user returns; `pending_handoff`: a safe fast-forward
    awaiting the user; `busy_since_mono`: a manual merge/rebase is holding the
    repo). The CANONICAL precedence when they must collapse into one user-facing
    state lives in Engine._repo_state (the status() "state" field) — extend it
    there, not in each GUI.
    """

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
        self.has_sealed = False  # a sealed (non-WIP) commit exists; if not, the
                                 # panel shows "—" instead of time-since-startup
        self.last_pull_mono = time.monotonic()
        self.net_busy = False     # a network task (fetch/pull/push/autosnap) is in flight
        self.autosnap_pending = False  # HEAD changed since the last autosnap push
        self.last_autosnap_mono = time.monotonic()
        self.last_gc_mono = time.monotonic()  # last `git gc --auto` (decoupled from sealing)
        self.paused = False       # set on rebase conflicts (not cleared on its own)
        self.conflict_msg = ""    # human explanation of WHY paused=True (for the GUI)
        self.user_paused = False  # set by the user from the GUI (per repo)
        self.busy_since_mono = None  # first tick that saw a manual merge/rebase
        self.busy_warned = False     # long-busy warning already emitted (once)
        self.dropped_warned = set()  # files already warned about (no longer snapshotted)
        self.off_branch = False   # HEAD is on a branch other than cfg.branch -> yield
        self._branch_cache = None      # last branch-check result (rate-limited, see below)
        self._branch_checked_mono = 0.0
        self.active_branch = cfg.branch  # branch currently operated on (== cfg.branch, or
                                         # the current branch when track_current_branch)
        self._skip_counts = {}    # top-level dir -> set of filtered-out rel paths (noise)
        self._noise_warned = set()  # dirs already suggested for extra_excludes (once each)
        self.user = ""            # "same person across machines" id for handoff (git email)
        self._handoff_warned_sha = None  # last diverging peer WIP we warned about (throttle)
        self.pending_handoff = None  # {sha, host} a safe FF awaiting the user (ask mode)
        self._started_mono = time.monotonic()  # for the post-startup grace of the commit nudge
        self._commit_nudge_mono = 0.0  # last purist "time to Smart Commit?" nudge (throttle)

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
    # `git gc --auto` runs on this cadence, INDEPENDENT of sealing: the amend loop
    # creates loose objects continuously, and with auto-seal disabled (purist mode)
    # the seal — the old gc trigger — never fires, so a long-lived WIP would bloat
    # the repo. Once a day is plenty (gc --auto only packs past its own threshold).
    GC_INTERVAL_SEC = 86400
    # Distinct filtered-out files in ONE top-level folder before we suggest excluding
    # it (Smart Ignore). High enough that a normal refactor never trips it.
    NOISE_SUGGEST_THRESHOLD = 50
    # Wall-clock seconds BEYOND the intended sleep that mark a suspend/resume (so the
    # idle loop reacts to a laptop waking up). Large enough to ignore scheduling jitter.
    RESUME_GAP_SEC = 90
    # The debounce waits for an event burst to settle — but a source that NEVER
    # settles (a long build, a log writer inside the repo) must not starve the
    # snapshot forever: past this many snapshot intervals since the last snapshot,
    # one is taken anyway, debounce or not. A disabled (inf) debounce or interval
    # keeps its "never fire" meaning (inf flows through the arithmetic untouched).
    SNAPSHOT_STARVATION_FACTOR = 2
    # Purist-mode commit nudge (see _maybe_nudge_commit). Fires only when ALL hold:
    # auto-seal is off, un-sealed work exists, the repo has been QUIET this long (the
    # "you paused / finished something" proxy) AND running at least this long since
    # startup, the last permanent commit is older than STALE, and we haven't nudged
    # within THROTTLE. Tuned so it reads as "you seem done and it's been a while",
    # never as a clock alarm.
    COMMIT_NUDGE_QUIET_SEC = 20 * 60        # settle window + post-startup grace (20 min)
    COMMIT_NUDGE_STALE_SEC = 24 * 3600      # last permanent commit older than ~1 day
    COMMIT_NUDGE_THROTTLE_SEC = 24 * 3600   # at most one nudge per repo per ~day
    # While a manual merge/rebase is in progress the daemon yields (see is_busy),
    # so edits made during it are NOT being snapshotted. That's invisible from the
    # editor, so past this long we tell the user once. High enough that a normal
    # merge — or the transient index.lock of any git command — never trips it.
    BUSY_WARN_SEC = 600

    def __init__(self, config, emit_event=None):
        self.config = config
        self.states: list[RepoState] = []
        self.watch = None
        self._watch_ready = False
        self.crashed = False  # the loop died on an unexpected error (see run)
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
        # pandoc is resolved lazily, only the first time a .docx is actually
        # staged or previewed (each .docx repo gets _pandoc_cmd as its resolver).
        # So a config without .docx — or a .docx repo where no .docx ever shows
        # up — never even runs `pandoc --version`, and non-.docx repos never get
        # the textconv `-c` on their git commands. See _pandoc_cmd / GitRepo.
        self._pandoc = None
        self._pandoc_resolved = False

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
        # The marker keeps the GUI's logging bridge from re-reporting this record
        # (it already reaches the event sink, structured, right below).
        log.log(self._LEVELS.get(level, logging.INFO), "%s%s", prefix, text,
                extra={"sincro_structured": True})
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

    @staticmethod
    def _repo_versions_docx(rc) -> bool:
        """Does this repo's config opt into versioning .docx (extra_includes)?
        Only then do we need pandoc / the textconv diff driver for it."""
        return any("docx" in p.lower() for p in (rc.extra_includes or []))

    @staticmethod
    def _repo_versions_pptx(rc) -> bool:
        """Does this repo's config opt into versioning .pptx (extra_includes)?
        Only then do the .gitattributes/doctor care about python-pptx."""
        return any("pptx" in p.lower() for p in (rc.extra_includes or []))

    def _pandoc_cmd(self) -> str | None:
        """Resolve pandoc once, on first need. Called only for repos that version
        .docx, so a config without .docx never runs `pandoc --version`."""
        if not self._pandoc_resolved:
            self._pandoc = resolve_pandoc(getattr(self.config, "pandoc_path", "pandoc"))
            self._pandoc_resolved = True
            if self._pandoc:
                log.info("pandoc found (%s): .docx diffs will be readable", self._pandoc)
        return self._pandoc

    def _dirty_cb(self, st: "RepoState"):
        """Build the watcher callback for a repo: mark it dirty AND wake the main
        loop out of its idle sleep so the change is handled promptly."""
        def cb():
            st.mark_dirty()
            self._wake.set()
        return cb

    def _branch_ok(self, st: "RepoState"):
        """(should_operate, current_branch_name). Fresh, uncached. Side effect: when
        should_operate, sets st.active_branch to the branch to operate on.

        - track_current_branch: FOLLOW whatever branch HEAD is on (operate unless
          detached / no branch).
        - otherwise: the branch guard — operate only when HEAD is on cfg.branch.
        """
        current = st.repo.current_branch()
        if st.cfg.track_current_branch:
            ok = bool(current) and current != "HEAD"  # yield only on detached HEAD
            if ok:
                st.active_branch = current
            return ok, current
        ok = (current == st.cfg.branch)
        if ok:
            st.active_branch = st.cfg.branch
        return ok, current

    def _branch_block_msg(self, st: "RepoState", current) -> str:
        """Why a manual action can't run on the current HEAD (mode-aware)."""
        if st.cfg.track_current_branch:
            return f"HEAD is detached ('{current}'); check out a branch first"
        return f"on branch '{current}', not configured '{st.cfg.branch}'; switch back first"

    def _ensure_on_branch(self, st: "RepoState", now_mono: float) -> bool:
        """Guard for the tick loop. Returns whether to operate on this repo this
        cycle, keeping st.active_branch / st.branch / st.off_branch current. See §11.

        Default: only operate when HEAD is on the configured branch (a manual
        `git checkout` makes SincroGit yield instead of snapshotting/pushing the
        wrong branch). This path is rate-limited (the check spawns `git rev-parse`).

        With track_current_branch it instead FOLLOWS the current branch — operating
        on whatever you're on, never pausing — and logs each switch once.
        """
        if st.cfg.track_current_branch:
            ok, current = self._branch_ok(st)  # fresh: following needs accuracy
            if current != st.branch:
                st.branch = current
                if ok:
                    self._emit(st.cfg.name, "info", f"now following branch '{current}'")
            st.off_branch = False
            return ok

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

    def _shadow_snapshot(self, st: "RepoState") -> bool:
        """Capture the worktree into the SHADOW chain (see gitrepo's shadow
        section): the user's HEAD, index and `git status` are never touched.
        Files are filtered exactly like the old staging was (same dropped-file
        warnings, same Smart Ignore feed). Returns True if a NEW snapshot
        commit was created. Assumes the caller holds st.op_lock.
        """
        repo, branch = st.repo, st.active_branch
        repo.ensure_shadow(branch)
        tip = repo.shadow_tip(branch)
        tip_tree = repo.sync_shadow_index(branch)

        to_stage, dropped = [], []
        for rel in repo.shadow_changed_paths():
            full = os.path.join(st.cfg.path, rel)
            if os.path.exists(full):
                reason = st.file_filter.reason_to_skip(full, rel)
                if reason is None:
                    to_stage.append(rel)
                else:
                    log.debug("filtered out (%s): %s", reason, rel)
                    dropped.append((rel, reason))
                    if reason != "excluded":
                        self._note_noise(st, rel, reason)
            else:
                to_stage.append(rel)  # deletion of something snapshotted

        # Warn (once per file) about SNAPSHOTTED files that dropped out of the
        # auto-snapshot — not for explicit excludes, and not for files we never
        # captured.
        reportable = [(r, why) for r, why in dropped if why != "excluded"]
        if reportable:
            tracked = repo.list_tracked([r for r, _ in reportable])
            for rel, why in reportable:
                if rel in tracked:
                    self._note_dropped(st, rel, why)

        if not to_stage:
            return False
        repo.shadow_stage(to_stage)
        tree = repo.shadow_write_tree()
        if repo.trees_match(tip_tree, tree):
            return False  # e.g. a .docx resave whose markdown didn't change
        repo.commit_shadow(branch, tree, tip)
        st.autosnap_pending = True  # the live mirror is now stale
        return True

    def _uncaptured(self, st: "RepoState") -> list:
        """Paths whose CURRENT worktree content the snapshots do NOT hold —
        i.e. what the filter refused (excluded / over the size limit / binary).
        Only meaningful right after a _shadow_snapshot pass; the restore and
        handoff guards refuse to overwrite these (they exist nowhere in git)."""
        return st.repo.shadow_changed_paths()

    def _ensure_docx_attributes(self, st: "RepoState"):
        """Map the binary documents this repo versions in .gitattributes: .docx
        to the pandoc diff driver, .pptx just out of EOL normalization (its
        readable previews are in-process — python-pptx — so no diff driver)."""
        lines = []
        if self._repo_versions_docx(st.cfg):
            lines.append("*.docx -text diff=pandoc")
        if self._repo_versions_pptx(st.cfg):
            lines.append("*.pptx -text")
        if not lines:
            return
        try:
            if st.repo.ensure_gitattributes(lines):
                self._emit(st.cfg.name, "info",
                           f".gitattributes: mapped {', '.join(ln.split()[0] for ln in lines)}")
        except Exception:  # noqa: BLE001 — best-effort convenience
            pass

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

    def _note_noise(self, st: "RepoState", relpath: str, reason: str):
        """A filtered-out file (binary / too large, not a user exclude). When a
        single top-level folder accumulates many of them, it's almost always build
        output / a cache — suggest excluding it (ONCE per folder, a notification +
        log; never auto-edits the config). 'Smart Ignore'. See §5 of DESIGN.md.
        """
        if not st.cfg.suggest_excludes or "/" not in relpath:
            return  # files at the repo root have no folder to suggest
        top = relpath.split("/", 1)[0]
        if top in st._noise_warned:
            return
        bucket = st._skip_counts.setdefault(top, set())
        bucket.add(relpath)
        if len(bucket) >= self.NOISE_SUGGEST_THRESHOLD:
            st._noise_warned.add(top)
            st._skip_counts.pop(top, None)  # free the set; we've warned
            pattern = f"**/{top}/**"
            notify(
                "SincroGit: noisy folder",
                f"'{st.cfg.name}': '{top}/' is churning {len(bucket)}+ unversioned files. "
                f"Add '{pattern}' to extra_excludes to keep the engine light.",
            )
            self._emit(
                st.cfg.name, "info",
                f"folder '{top}/' has {len(bucket)}+ filtered-out files; consider adding "
                f"'{pattern}' to extra_excludes (or set suggest_excludes: false)",
                "WARNING",
            )

    # ----------------------------------------------- background network task
    def _dispatch_network(self, st: "RepoState", label: str, fn, hold_lock: bool = True) -> bool:
        """Run a git op (fetch/pull/push/seal) on a background thread so the tick
        thread never blocks on I/O. At most one such task per repo at a time.
        Returns False if one is already in flight.

        By default the worker holds the repo's op_lock for the whole `fn`, so it
        can't race the snapshot/seal cycle. With hold_lock=False the worker runs
        `fn` WITHOUT the lock and `fn` manages its own — needed by the automatic
        seal, which deliberately releases the lock around its slow AI call.
        """
        with st._lock:
            if st.net_busy:
                return False
            st.net_busy = True

        def worker():
            try:
                if hold_lock:
                    with st.op_lock:
                        fn()
                else:
                    fn()  # fn owns its locking (releases op_lock around slow work)
            except GitError as e:
                log.error("[%s] %s failed: %s", st.cfg.name, label, e)
            except Exception as e:  # noqa: BLE001 — a worker must not die silently
                log.error("[%s] %s crashed: %s", st.cfg.name, label, e)
            finally:
                with st._lock:
                    st.net_busy = False
                # The repo is free again; wake the loop so any action that was
                # waiting on this worker (a due sync/seal/snapshot) runs now.
                self._wake.set()

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
                st.conflict_msg = ""
                self._wake.set()  # re-evaluate this repo without waiting
                self._emit(name, "resume", "repo resumed")
                return True
        return False

    @staticmethod
    def _repo_state(st: RepoState) -> str:
        """Collapse the pause-like flags into ONE canonical state for the UIs.

        Precedence: conflict > busy > off-branch > paused > handoff > active.
        This is the single source of truth — the GUIs map these keys to
        labels/colors but must not re-derive the precedence. `busy` outranks
        `off-branch` because a manual rebase detaches HEAD, setting both — and
        "a merge/rebase is running" is the truthful one. It comes from the
        tick's tracking (no git call here), so it can lag by up to MAX_TICK_SEC.
        """
        if st.paused:
            return "conflict"
        if st.busy_since_mono is not None:
            return "busy"
        if st.off_branch:
            return "off-branch"
        if st.user_paused:
            return "paused"
        if st.pending_handoff:
            return "handoff"
        return "active"

    def status(self) -> dict:
        """State snapshot for the panel (cached fields, no git calls)."""
        repos = []
        for st in self._states_snapshot():
            repos.append({
                "name": st.cfg.name,
                "path": st.cfg.path,
                "branch": st.branch,
                "state": self._repo_state(st),
                "conflict_paused": st.paused,
                "conflict_msg": st.conflict_msg,
                "user_paused": st.user_paused,
                "off_branch": st.off_branch,
                "net_busy": st.net_busy,
                "pending_handoff": (st.pending_handoff or {}).get("host"),
                "pending_handoff_epoch": (st.pending_handoff or {}).get("epoch"),
                "last_snapshot": st.last_snapshot_wall,
                "last_seal": st.last_seal_epoch if st.has_sealed else None,
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
            # Degrade gracefully if watchdog isn't installed: keep running (the GUI,
            # manual snapshot/commit, sync, the time machine all still work) instead
            # of crashing — just without automatic change detection.
            try:
                self.watch = WatchManager()
                self._watch_ready = True
            except Exception as e:  # noqa: BLE001 — watchdog missing/broken
                self.watch = None
                self._watch_ready = False
                self._emit(
                    "", "startup",
                    f"file watcher unavailable ({e}); automatic snapshots are OFF — "
                    f"install 'watchdog' for change detection. Manual commits still work.",
                    "WARNING",
                )

        for rc in self.config.repos:
            try:
                self._setup_repo(rc)
            except GitError as e:
                # E.g. the folder is gone (unplugged drive, moved cloud folder):
                # skip this repo and keep the engine alive for the others.
                self._emit(rc.name, "startup", f"setup failed, repo skipped: {e}", "ERROR")
            except Exception as e:  # noqa: BLE001 — one bad repo must not stop the rest
                log.exception("[%s] unexpected setup failure; repo skipped", rc.name)
                self._emit(rc.name, "startup", f"setup failed, repo skipped: {e}", "ERROR")

    def _setup_repo(self, rc):
        """Initialize and register ONE repo (setup's per-repo body). Raises
        GitError if its git work fails — e.g. the folder no longer exists."""
        # Hand the repo a lazy pandoc resolver (only for .docx repos): it stays
        # unresolved until a .docx actually shows up, so a repo that never sees
        # a .docx never runs `pandoc --version`. See GitRepo._ensure_pandoc.
        provider = self._pandoc_cmd if self._repo_versions_docx(rc) else None
        repo = GitRepo(rc.path, pandoc_provider=provider)
        if not repo.is_git_repo():
            log.error("Not a git repo (skipping): %s", rc.path)
            return

        # Power-cut self-healing: a crash can zero out .git/HEAD or the branch's
        # ref file (right size, NUL content) leaving the repo "broken" while the
        # reflog — append-only — still knows the last state. Repair before any
        # git work so the repo comes back by itself instead of yielding forever.
        for msg in repo.repair_corrupt_refs(rc.branch):
            self._emit(rc.name, "repair", msg, "WARNING")

        ff = FileFilter(rc.max_file_bytes, rc.extra_excludes,
                        rc.extra_includes, rc.max_include_bytes)
        st = RepoState(repo, rc, ff)

        try:
            if repo.migrate_wip_tip(rc.branch):
                self._emit(rc.name, "startup",
                           "migrated to the shadow model — your unsealed edits "
                           "now show as ordinary uncommitted changes", "WARNING")
            repo.ensure_shadow(rc.branch)
        except GitError as e:
            log.error("[%s] could not initialize the snapshot chain: %s", rc.name, e)
            return

        sealed = repo.last_sealed_time()
        st.last_seal_epoch = float(sealed) if sealed else time.time()
        st.has_sealed = sealed is not None
        st.user = repo.sincro_user()

        with self._states_lock:
            self.states.append(st)
        if self._watch_ready:
            try:
                self.watch.watch(rc.path, self._dirty_cb(st), ignore=st.file_filter.is_excluded)
            except Exception:  # noqa: BLE001 — watching is best-effort (matches add_repo)
                log.warning("[%s] could not start the watcher; automatic change "
                            "detection is OFF for this repo", rc.name)

        branch = repo.current_branch()
        st.branch = branch
        if rc.track_current_branch:
            # Follow mode: operate on whatever branch we start on (no pausing).
            if branch and branch != "HEAD":
                st.active_branch = branch
        elif branch and branch != rc.branch:
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
        try:
            self._run()
        except Exception as e:  # noqa: BLE001 — never die silently under the GUI
            # An unexpected (non-GitError) failure must be VISIBLE: log it, emit an
            # ERROR event (the tray shows a balloon) and toast — and the finally
            # below sets _stop, so status() reports not-running (gray tray icon)
            # instead of pretending the autosync is still alive.
            self.crashed = True
            log.exception("engine crashed")
            self._emit("", "error", f"engine stopped unexpectedly: {e}; autosync is "
                                    f"OFF until SincroGit restarts", "ERROR")
            notify("SincroGit: engine stopped",
                   f"Unexpected error: {e}. Autosync is OFF until you restart SincroGit.",
                   level="error")
        finally:
            self._stop.set()  # status() must reflect that the loop is gone
            self.shutdown()

    def _run(self):
        self.setup(with_watcher=True)
        # Keep running even with 0 repos: repos can be added later from the GUI.
        if not self.states:
            log.warning("No valid repos yet. Waiting (add some from the GUI).")

        if self._watch_ready:
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
        while not self._stop.is_set():
            self.tick()
            if self._stop.is_set():
                break
            # Sleep until the next action is actually due (see _wait_seconds),
            # capped at MAX_TICK_SEC. The watcher (and stop/resume/worker
            # completion) set _wake to interrupt early, so an idle laptop barely
            # spins while changes are still handled at once.
            wait = self._wait_seconds()
            before_wall = time.time()
            self._wake.wait(wait)
            self._wake.clear()
            # Wall-clock-gap resume detector (dependency-free, cross-platform):
            # if far more WALL time passed than we meant to sleep, the machine was
            # suspended (laptop lid) — and monotonic clocks may have frozen, so the
            # snapshot/pull deadlines won't notice. Force a sync to pick up the
            # other machine's work right away (the "arrived" half of the handoff).
            if time.time() - before_wall > wait + self.RESUME_GAP_SEC:
                self._on_resume()

    def _on_resume(self):
        """Woke from a long suspend (or the OS told us we unlocked/resumed): make a
        fetch/pull/handoff due now for every repo so we catch up to the other machine
        promptly instead of waiting out the pull interval. See sync_soon."""
        self._emit("", "resume", "resumed; syncing to catch up")
        self.sync_soon()

    def sync_soon(self):
        """Make a fetch/pull/handoff due on the NEXT tick for every repo and wake the
        loop now (non-blocking). Used on an 'arrived at this machine' OS event
        (unlock/resume) and by the resume detector."""
        for st in self._states_snapshot():
            st.last_pull_mono = 0.0  # monotonic in the distant past -> sync is due
        self._wake.set()

    def flush_now(self, wait: bool = False):
        """Force a snapshot + autosnap push of every (on-branch) repo NOW, ignoring
        the intervals, so the remote mirror is fresh — the 'leaving this machine' OS
        event (lock/suspend). Runs on a background thread (never blocks the caller)
        unless `wait` is True — then it blocks (bounded) until the flush finishes,
        for callers about to QUIT the daemon (the build script's flush-quit-rebuild
        cycle must not kill the process mid-push). Best-effort: a suspend may cut
        the network before the push finishes; the normal autosnap interval is the
        backstop. See §4.2/§11 of DESIGN.md."""
        if self._paused.is_set():
            return

        def worker():
            did = 0  # snapshots + mirror pushes actually performed
            for st in self._states_snapshot():
                if st.paused or st.user_paused:
                    continue
                try:
                    ok, _ = self._branch_ok(st)  # sets active_branch; yields off-branch/detached
                    if not ok:
                        continue
                    with st.op_lock:
                        if st.repo.is_busy():
                            continue
                        if self._do_snapshot(st):
                            st.last_snapshot_wall = time.time()
                            self._mark_action(st, "snapshot")
                            did += 1
                        if (st.cfg.autosnap and st.autosnap_pending
                                and st.repo.has_remote(st.cfg.remote)):
                            self._do_autosnap(st)  # synchronous push (already off-thread)
                            st.last_autosnap_mono = time.monotonic()
                            if not st.autosnap_pending:  # cleared only on a SUCCESSFUL push
                                did += 1
                except GitError as e:
                    log.error("[%s] flush failed: %s", st.cfg.name, e)
            # Only claim a flush when something actually moved (a no-op flush on
            # every lock/suspend would just be log noise).
            if did:
                self._emit("", "flush", "flushed latest state to the remote (leaving machine)")

        t = threading.Thread(target=worker, name="sincrogit-flush", daemon=True)
        t.start()
        if wait:
            # Bounded: a hung remote must not wedge the quit path forever (each
            # repo's push already has its own git_timeout_sec).
            t.join(timeout=180)

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
            # net_busy: a network worker holds this repo, so nothing else can run
            # on it until it finishes — and it sets _wake on completion. No point
            # waking for it (avoids busy-polling while a fetch/push is in flight).
            if st.paused or st.user_paused or st.off_branch or st.net_busy:
                continue
            # A busy repo (manual merge/rebase in progress) can't act on its
            # deadlines either — without this, an overdue seal would spin the
            # loop at 1 Hz for the whole merge. Cheap: is_busy is os.path.exists
            # once the git dir is cached. MAX_TICK_SEC is the backstop that
            # notices when the repo frees up.
            try:
                if st.repo.is_busy():
                    continue
            except GitError:
                continue  # repo folder vanished; the tick path will log it
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
                if math.isfinite(cfg.debounce_sec):
                    # anti-starvation deadline (see _maybe_snapshot)
                    snap = min(snap, st.last_snapshot_mono
                               + self.SNAPSHOT_STARVATION_FACTOR * cfg.snapshot_interval_sec
                               - now_mono)
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
                # Busy tracking runs BEFORE the branch guard: a manual rebase
                # detaches HEAD, so the guard would yield first and a dragging
                # rebase — the case the warning exists for — would never warn.
                self._track_busy(st, now_mono)      # warn once if a merge/rebase drags on
                if not self._ensure_on_branch(st, now_mono):
                    continue  # user switched branches (git checkout): yield this repo
                self._maybe_sync(st, now_mono)      # dispatched to a worker; returns at once
                self._maybe_snapshot(st, now_mono)  # local; skipped if a worker holds the repo
                self._dispatch_seal(st, now_epoch)  # off-thread: the AI message must not block the tick
                self._maybe_autosnap(st, now_mono)  # live mirror; dispatched to a worker
                self._maybe_gc(st, now_mono)        # daily repo packing; background worker
                self._maybe_nudge_commit(st, now_mono, now_epoch)  # purist "time to commit?"
            except GitError as e:
                log.error("[%s] error in the cycle: %s", st.cfg.name, e)
            except Exception:  # noqa: BLE001 — one bad repo must not stop the others
                log.exception("[%s] unexpected error in the cycle", st.cfg.name)

    def _track_busy(self, st: RepoState, now_mono: float):
        """Watch how long a manual git operation (merge/rebase/…) has held the
        repo. While it lasts every _maybe_* step yields, so edits saved during it
        are NOT being snapshotted — invisible from the editor. Past BUSY_WARN_SEC
        we say so ONCE (log + toast), and note when snapshots resume. MAX_TICK_SEC
        bounds how stale this check can get while the loop sleeps.
        """
        try:
            busy = st.repo.is_busy()
        except GitError:
            return  # repo folder vanished; the tick's own error handling logs it
        if not busy:
            if st.busy_warned:
                self._emit(st.cfg.name, "info",
                           "merge/rebase finished — snapshots resume")
            st.busy_since_mono = None
            st.busy_warned = False
            return
        if st.busy_since_mono is None:
            st.busy_since_mono = now_mono
        elif (not st.busy_warned
                and now_mono - st.busy_since_mono >= self.BUSY_WARN_SEC):
            st.busy_warned = True
            mins = int((now_mono - st.busy_since_mono) // 60)
            # If what's "busy" is just an old index.lock, no merge is coming to
            # free it — a crash stranded the lock and syncing is stuck until the
            # user deletes it. Say THAT, not a misleading "merge in progress".
            stale = st.repo.stale_lock(self.BUSY_WARN_SEC)
            if stale:
                self._emit(st.cfg.name, "busy",
                           f"the repo looks busy, but it may just be a git lock "
                           f"left behind by a crash: {stale}. If no git command "
                           f"is running, delete that file (see --doctor); "
                           f"snapshots resume then.", "WARNING")
                notify("SincroGit: snapshots stuck",
                       f"'{st.cfg.name}': a leftover git lock is blocking "
                       f"snapshots. Run --doctor for instructions.")
                return
            self._emit(st.cfg.name, "busy",
                       f"a manual merge/rebase has been in progress for {mins}+ min; "
                       f"snapshots are postponed until it finishes (edits saved "
                       f"meanwhile are not yet captured)", "WARNING")
            notify("SincroGit: snapshots postponed",
                   f"'{st.cfg.name}': a manual merge/rebase has been in progress for "
                   f"{mins}+ min. Snapshots resume when it finishes.")

    # ----------------------------------------------------------- operations
    def _maybe_snapshot(self, st: RepoState, now_mono: float):
        dirty, last_event = st.read_dirty()
        if not dirty:
            return
        # Anti-starvation: continuous events keep resetting the debounce, so past
        # SNAPSHOT_STARVATION_FACTOR x the interval, snapshot regardless. Only
        # with a *finite* debounce — `debounce_sec: inf` means "never fire" and
        # must stay that way. See the class constant.
        overdue = (math.isfinite(st.cfg.debounce_sec)
                   and now_mono - st.last_snapshot_mono
                   >= self.SNAPSHOT_STARVATION_FACTOR * st.cfg.snapshot_interval_sec)
        if not overdue:
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
            try:
                if not self._branch_ok(st)[0]:
                    continue  # on another branch: don't snapshot it (see _ensure_on_branch)
                with st.op_lock:
                    if st.repo.is_busy():
                        continue
                    if self._do_snapshot(st):
                        log.info("[%s] initial snapshot", st.cfg.name)
            except GitError as e:
                log.error("[%s] error in initial snapshot: %s", st.cfg.name, e)

    def _do_snapshot(self, st: RepoState) -> bool:
        """One snapshot pass (shadow model). Returns True if there were changes.

        Assumes the caller holds st.op_lock. Logs nothing: each caller writes its
        own log message according to the context (normal / initial / final).
        """
        return self._shadow_snapshot(st)

    def _dispatch_seal(self, st: RepoState, now_epoch: float):
        """Tick entry to the automatic seal: dispatch it to a worker (only when a
        seal is due) so generating the message — which may call a ~30 s AI model —
        never blocks the tick thread. The worker (`_maybe_seal`) manages its own
        op_lock, releasing it around the AI, so the dispatch must NOT hold it."""
        if now_epoch - st.last_seal_epoch < st.cfg.seal_interval_sec:
            return
        self._dispatch_network(st, "seal",
                               lambda e=now_epoch: self._maybe_seal(st, e),
                               hold_lock=False)

    def _maybe_seal(self, st: RepoState, now_epoch: float):
        """Automatic seal. Runs off the tick (the tick dispatches it to a worker)
        and generates the message WITHOUT holding op_lock, so a slow AI model
        freezes neither the tick nor this repo's snapshots — the same split
        propose_seal_message already uses for Smart Commit. Three phases:
          1) under the lock: honor the user's own commits, snapshot, gather the diff;
          2) no lock: compose the message (the AI call, the slow part);
          3) under the lock: re-validate, commit + push.
        """
        if now_epoch - st.last_seal_epoch < st.cfg.seal_interval_sec:
            return
        # --- Phase 1: decide + gather, under the lock. Don't block if a network
        # op holds this repo: skip and retry next tick.
        if not st.op_lock.acquire(blocking=False):
            return
        try:
            # Respect the user's own commits (ported from v0.1's _ensure_wip): a
            # manual `git commit` in a terminal — or commits a pull integrated —
            # never passes through the seal, so the clock wouldn't know about it
            # and an auto-checkpoint could land right on its heels. If a
            # permanent (non-WIP) commit is newer than the clock's baseline,
            # restart the window from it. Only checked when a seal is DUE, so
            # the extra git reads cost nothing in steady state.
            external = st.repo.last_sealed_time()
            if external and external > st.last_seal_epoch:
                st.last_seal_epoch = float(external)
                st.has_sealed = True
                if now_epoch - st.last_seal_epoch < st.cfg.seal_interval_sec:
                    self._emit(st.cfg.name, "info",
                               "found your own commit; the auto-seal clock "
                               "restarts from it")
                    return
                # (An old external commit — or our own seal's sub-second clock
                # skew — just refreshes the baseline; the due seal proceeds.)
            payload = self._prepare_auto_seal(st, now_epoch)
        finally:
            st.op_lock.release()
        if payload is None:
            return
        # --- Phase 2: the message (AI or fallback), WITHOUT the lock (the slow part).
        title, body = self._compose_seal_message(st, payload)
        # --- Phase 3: commit + push, under the lock again. We're off the tick here
        # (a worker, or a direct call from a test), so a synchronous push is fine.
        with st.op_lock:
            sealed = self._commit_seal(st, now_epoch, title, body)
            if sealed and st.cfg.push and st.repo.has_remote(st.cfg.remote):
                self._do_push(st)

    def _prepare_auto_seal(self, st: RepoState, now_epoch: float):
        """Phase 1 of the automatic seal, UNDER op_lock: take the final snapshot
        and gather what the message will summarize. Returns that payload (see
        _seal_message_inputs), or None when there's nothing to seal (and
        reschedules the clock). The AI call itself is deferred to phase 2, unlocked.
        """
        repo, branch = st.repo, st.active_branch
        if repo.is_busy():
            return None
        # The user's own staging area is THEIRS: an auto-seal must not absorb a
        # hand-crafted commit in progress.
        if repo.has_staged_changes():
            log.info("[%s] auto-seal postponed: you have changes staged for a "
                     "manual commit", st.cfg.name)
            st.last_seal_epoch = now_epoch  # reschedule the clock
            return None
        self._shadow_snapshot(st)  # final snapshot: capture the latest edits
        tree = repo.sync_shadow_index(branch)
        head = repo.head_sha()
        base_tree = repo.tree_of(head) if head else repo._empty_tree()
        if repo.trees_match(base_tree, tree):
            log.debug("[%s] nothing to seal", st.cfg.name)
            st.last_seal_epoch = now_epoch
            return None
        return self._seal_message_inputs(st, base_tree, tree)

    def _commit_seal(self, st: RepoState, now_epoch: float, title: str, body: str) -> bool:
        """Phase 3 of the automatic seal, UNDER op_lock: re-snapshot and, if there
        is still something to seal, commit the accumulated shadow tree as ONE real
        commit + re-anchor. Returns True if a seal happened.

        Re-validates because edits (and snapshots) may have landed while the AI
        message was generated with the lock released. The message describes the
        tree as of phase 1; a few extra edits folded in is the same staleness
        Smart Commit already accepts.
        """
        repo, branch = st.repo, st.active_branch
        if repo.is_busy():
            return False
        self._shadow_snapshot(st)
        tree = repo.sync_shadow_index(branch)
        head = repo.head_sha()
        base_tree = repo.tree_of(head) if head else repo._empty_tree()
        if repo.trees_match(base_tree, tree):
            log.debug("[%s] nothing to seal", st.cfg.name)
            st.last_seal_epoch = now_epoch
            return False
        new = repo.seal_from_shadow(branch, tree, title, body)
        repo.reanchor_shadow(branch, new)
        st.last_seal_epoch = now_epoch
        st.has_sealed = True
        st.autosnap_pending = True
        self._mark_action(st, "seal")
        self._emit(st.cfg.name, "seal", title)
        st.repo.gc_auto()
        return True

    def _do_seal(self, st: RepoState, now_epoch: float, message: str | None = None) -> bool:
        """Core seal: final snapshot, then commit the accumulated shadow tree as
        ONE real commit on the branch and re-anchor the shadow chain to it.
        Returns True if a seal happened. Assumes the caller holds st.op_lock.
        Does NOT push.

        `message` (a developer's manual commit message) overrides the automatic
        AI/fallback message; its first line is the subject, the rest the body.
        """
        repo, branch = st.repo, st.active_branch
        if repo.is_busy():
            return False
        # The user's own staging area is THEIRS: an auto-seal must not absorb a
        # hand-crafted commit in progress. (A manual Smart Commit — message
        # given — proceeds: the user asked for exactly that.)
        if message is None and repo.has_staged_changes():
            log.info("[%s] auto-seal postponed: you have changes staged for a "
                     "manual commit", st.cfg.name)
            st.last_seal_epoch = now_epoch  # reschedule the clock
            return False

        self._shadow_snapshot(st)  # final snapshot: capture the latest edits
        tree = repo.sync_shadow_index(branch)
        head = repo.head_sha()
        base_tree = repo.tree_of(head) if head else repo._empty_tree()
        if repo.trees_match(base_tree, tree):
            log.debug("[%s] nothing to seal", st.cfg.name)  # DEBUG: avoids noise over idle days
            st.last_seal_epoch = now_epoch  # reschedule the clock
            return False

        if message is not None:
            title, _, body = message.strip().partition("\n")
            title, body = title.strip(), body.strip()
        else:
            title, body = self._seal_message(st, base_tree, tree)
        new = repo.seal_from_shadow(branch, tree, title, body)
        repo.reanchor_shadow(branch, new)
        st.last_seal_epoch = now_epoch
        st.has_sealed = True
        st.autosnap_pending = True  # the mirror should now track the sealed tip
        self._mark_action(st, "seal")
        self._emit(st.cfg.name, "seal", title)

        # Maintenance: pack the loose objects left by the snapshot chain.
        st.repo.gc_auto()
        return True

    def _seal_message(self, st: RepoState, base_tree: str, tree: str):
        """Seal message for the manual/CLI path (caller holds op_lock): gather the
        diff and compose in one call. The window is base_tree (HEAD's tree) ->
        tree (the latest snapshot). The AUTOMATIC path splits these two steps so
        the AI runs off the lock — see _prepare_auto_seal / _compose_seal_message.
        """
        return self._compose_seal_message(st, self._seal_message_inputs(st, base_tree, tree))

    def _seal_message_inputs(self, st: RepoState, base_tree: str, tree: str):
        """The git reads a seal message needs — name-status (for the fallback) and,
        when AI is on, the --stat + truncated diff — as (name_status, ai_inputs)
        where ai_inputs is None if AI is off. This is the part that MUST run under
        op_lock; kept separate so the automatic seal can gather here and call the
        (slow, lock-free) AI afterwards. See _compose_seal_message."""
        repo = st.repo
        name_status = repo.name_status_for_seal(base_tree, tree)
        ai_inputs = None
        if self.config.ai.mode != "none":
            ai_inputs = (
                repo.diff_stat_for_seal(base=base_tree, target=tree),
                repo.diff_text_for_seal(self.config.ai.max_diff_chars,
                                        base=base_tree, target=tree),
            )
        return name_status, ai_inputs

    def _compose_seal_message(self, st: RepoState, payload) -> tuple:
        """Turn the gathered diff into a message — AI if available, else the
        deterministic fallback. No git and no lock (the AI call is the slow part we
        deliberately keep off op_lock). `payload` is what _seal_message_inputs
        returned: (name_status, ai_inputs | None)."""
        name_status, ai_inputs = payload
        title, body = build_fallback_message(name_status)
        if ai_inputs is None:
            return title, body
        stat, text = ai_inputs
        try:
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
        ok, msg = repo.push_sealed(cfg.remote, st.active_branch, timeout=cfg.git_timeout_sec)
        if ok:
            self._mark_action(st, "push")
            self._emit(cfg.name, "push", f"push OK -> {cfg.remote}/{st.active_branch}")
        else:
            # A rejected push (remote ahead) is reconciled in the next sync.
            self._emit(cfg.name, "push", f"push failed (will retry): {msg}", "WARNING")

    # --------------------------------------------------------- autosnap (mirror)
    def _maybe_autosnap(self, st: RepoState, now_mono: float):
        """Mirror the shadow tip (the latest snapshot: sealed history + the live
        WIP) to the remote on a background worker, so a total disk failure loses
        at most ~autosnap_interval. Only when something
        actually changed since the last mirror (autosnap_pending), so a dormant
        repo never pushes. See §12 of DESIGN.md."""
        if not st.cfg.autosnap or not st.autosnap_pending:
            return
        if now_mono - st.last_autosnap_mono < st.cfg.autosnap_interval_sec:
            return
        if not st.repo.has_remote(st.cfg.remote):
            return
        # Advance only on a real dispatch (see _maybe_sync); pending stays set.
        if self._dispatch_network(st, "autosnap", lambda: self._do_autosnap(st)):
            st.last_autosnap_mono = now_mono

    def _do_autosnap(self, st: RepoState):
        """Force-push the shadow tip to this host's autosnap ref. Assumes op_lock
        (runs in a _dispatch_network worker). Clears the pending flag only on
        success."""
        repo, cfg = st.repo, st.cfg
        ok, msg = repo.push_autosnap(
            cfg.remote, st.active_branch, st.user, self._autosnap_host, timeout=cfg.git_timeout_sec
        )
        ref = repo.autosnap_ref(st.user, self._autosnap_host, st.active_branch)
        if ok:
            st.autosnap_pending = False
            self._mark_action(st, "autosnap")
            self._emit(cfg.name, "autosnap", f"mirror pushed -> {cfg.remote}/{ref}")
        else:
            # Keep pending=True so the next interval retries.
            self._emit(cfg.name, "autosnap", f"mirror push failed (will retry): {msg}", "WARNING")

    # --------------------------------------------------------------- maintenance
    def _maybe_gc(self, st: RepoState, now_mono: float):
        """Pack loose objects ~once a day, INDEPENDENT of sealing (GC_INTERVAL_SEC).
        The amend loop creates loose objects continuously; in purist mode the seal
        never fires, so without this a long-lived WIP would bloat the repo. Runs on
        a background worker (under op_lock) so a pack never blocks the tick; skips
        while a merge/rebase is in progress."""
        if now_mono - st.last_gc_mono < self.GC_INTERVAL_SEC:
            return

        def _gc():
            if st.repo.is_busy():
                return
            st.repo.gc_auto()
            # Housekeeping: delete THIS machine's remote autosnap refs for branches
            # deleted locally — single-writer refs, so it's race-free, and an age
            # guard keeps a freshly re-cloned repo from pruning states it hasn't
            # recovered yet. See GitRepo.prune_autosnap_refs.
            if st.cfg.autosnap and st.repo.has_remote(st.cfg.remote):
                removed = st.repo.prune_autosnap_refs(
                    st.cfg.remote, st.user, self._autosnap_host,
                    timeout=st.cfg.git_timeout_sec)
                if removed:
                    self._emit(st.cfg.name, "gc",
                               f"pruned stale autosnap ref(s): {', '.join(removed)}")

        # Advance only on a real dispatch (a busy repo retries next interval).
        if self._dispatch_network(st, "gc", _gc):
            st.last_gc_mono = now_mono

    def _maybe_nudge_commit(self, st: RepoState, now_mono: float, now_epoch: float):
        """Purist mode's safety net for its one footgun: the permanent branch can
        silently stagnate if the user never Smart Commits (the work is safe in the
        WIP + autosnap, just not ON the branch). When un-sealed work has piled up
        AND the repo has *settled* (a quiet moment = the "you finished something"
        proxy) AND it's been a while since the last permanent commit, remind ONCE
        (throttled) to Smart Commit. No-op outside purist mode (auto-seal keeps the
        branch advancing there) or when suggest_commit is off. See the README.
        """
        cfg = st.cfg
        # Cheap gates first (no git): only in purist mode, only if enabled.
        if not cfg.suggest_commit or not math.isinf(cfg.seal_interval_sec):
            return
        # Don't nag right after launch, and only at a quiet moment (settled work).
        if now_mono - st._started_mono < self.COMMIT_NUDGE_QUIET_SEC:
            return
        if now_mono - st.last_event_mono < self.COMMIT_NUDGE_QUIET_SEC:
            return
        if now_mono - st._commit_nudge_mono < self.COMMIT_NUDGE_THROTTLE_SEC:
            return
        # Staleness is measured off the last permanent (sealed/user) commit, which
        # in purist mode is HEAD. last_seal_epoch tracks it (a Smart Commit refreshes
        # it via _do_seal), so committing makes this gate close by itself.
        if now_epoch - st.last_seal_epoch < self.COMMIT_NUDGE_STALE_SEC:
            return
        # Only now (rare: once/day for a stale purist repo) spend a git call to
        # confirm there's actually un-sealed work. Non-blocking: skip if a worker
        # holds the repo (we'll re-evaluate next tick).
        if not st.op_lock.acquire(blocking=False):
            return
        try:
            repo, branch = st.repo, st.active_branch
            # A manual `git commit` in a terminal never passes through _do_seal,
            # so refresh the staleness baseline from git first — the user who
            # committed by hand yesterday must not be nagged today.
            external = repo.last_sealed_time()
            if external and external > st.last_seal_epoch:
                st.last_seal_epoch = float(external)
                st.has_sealed = True
                if now_epoch - st.last_seal_epoch < self.COMMIT_NUDGE_STALE_SEC:
                    return
            head = repo.head_sha()
            head_tree = repo.tree_of(head) if head else repo._empty_tree()
            tip = repo.shadow_tip(branch)
            tip_tree = repo.tree_of(tip) if tip else None
            if not tip_tree or repo.trees_match(head_tree, tip_tree):
                return  # nothing un-sealed: the branch is already current
            n = len(repo.name_status_for_seal(head_tree, tip_tree))
        except GitError:
            return
        finally:
            st.op_lock.release()
        st._commit_nudge_mono = now_mono
        days = int((now_epoch - st.last_seal_epoch) // 86400)
        since = f"{days} day(s)" if days >= 1 else "a while"
        self._emit(
            cfg.name, "info",
            f"{n} file(s) of work aren't on your branch yet and there's been no "
            f"permanent commit in {since}; consider a Smart Commit (it's already "
            f"backed up — this is about your history)", "WARNING")
        notify(
            "SincroGit: time for a commit?",
            f"'{cfg.name}': {n} file(s) of work aren't on your branch yet. A Smart "
            f"Commit seals them as a permanent commit. (Already backed up; this is "
            f"about keeping your history current.)")

    def _initial_sync(self):
        # Runs on its own thread at startup. Per-repo op_lock keeps each repo's
        # sync from racing the tick (which simply skips a repo that's busy).
        for st in self._states_snapshot():
            try:
                if self._branch_ok(st)[0]:
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
        # Advance the clock only if we actually kicked off the sync. If a worker
        # was already busy on this repo, leave it due so it runs as soon as the
        # worker frees (it sets _wake) instead of waiting a whole interval.
        if self._dispatch_network(st, "sync", lambda: self._do_sync(st)):
            st.last_pull_mono = now_mono

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
        remote_exists = repo.remote_branch_exists(cfg.remote, st.active_branch)

        # --- PULL: rebase the WIP onto the new remote commits ---
        if not self._pull_after_fetch(st, remote_exists):
            return  # conflict -> repo paused

        # --- PUSH: upload pending sealed commits (first push or retries) ---
        if cfg.push:
            if not remote_exists or repo.has_unpushed_sealed(cfg.remote, st.active_branch):
                self._do_push(st)

        # --- HANDOFF: pick up this user's newer live WIP from another machine ---
        if cfg.live_handoff != "off":
            self._maybe_handoff(st)

    def _maybe_handoff(self, st: RepoState):
        """Cross-machine handoff (levels a + b). Pick up the latest live WIP that YOU
        pushed from ANOTHER machine. When it's a safe fast-forward (the peer matches
        your content on every path you changed, and has more — so a reset loses
        nothing of yours, only an empty WIP), then:
          - live_handoff == 'auto': apply it now (and notify so it's never silent);
          - live_handoff == 'ask' : record it and notify, so you Apply with one click.
        It is refused (notify) if it would clobber an untracked file. On divergence it
        does NOT auto-merge (by design): it notifies once and leaves both states intact
        for you to resolve by hand. See the README's handoff section.

        Assumes st.op_lock (runs inside the sync worker). Needs autosnap on the other
        machine to be discoverable. Reversible via the reflog.
        """
        repo, cfg = st.repo, st.cfg
        if not repo.has_remote(cfg.remote):
            return
        # Refresh only MY machines' mirrors (cheap; ignores teammates').
        repo.fetch_autosnaps(cfg.remote, user=st.user, timeout=cfg.git_timeout_sec)
        peer = repo.peer_wip(st.user, self._autosnap_host, st.active_branch)
        if not peer:
            st.pending_handoff = None
            return

        # Capture pending local edits first, so the comparison is honest: only then
        # can we tell "I'm simply behind" from "I have my own new work".
        self._shadow_snapshot(st)
        mine = repo.shadow_tip(st.active_branch)
        if not mine or mine == peer["sha"]:
            st.pending_handoff = None
            return  # in sync (or nothing local yet)

        # Compare actual WORK content (not commit ancestry — the snapshot chains
        # of two machines are siblings; see GitRepo.work_relationship).
        rel = repo.work_relationship(mine, peer["sha"])
        if rel == "theirs_contains":
            # SAFE to adopt: their state matches my content on every path I
            # changed. Refuse if applying would still clobber something that
            # exists nowhere in git: an untracked file their tree contains, or
            # an uncaptured (filtered) local edit on a path being brought over.
            risky = self._handoff_risky(st, mine, peer["sha"])
            if risky:
                st.pending_handoff = None
                sample = ", ".join(risky[:3]) + ("…" if len(risky) > 3 else "")
                self._warn_handoff_once(
                    st, peer["sha"],
                    f"newer work on '{peer['host']}' NOT applied: it would "
                    f"overwrite local content snapshots don't hold ({sample}). "
                    f"Move or commit those files first, then sync.",
                    notify_user=True)
                return
            if cfg.live_handoff == "ask":
                # Don't touch the working tree; record it + notify once for one-click Apply.
                if not st.pending_handoff or st.pending_handoff.get("sha") != peer["sha"]:
                    st.pending_handoff = {"sha": peer["sha"], "host": peer["host"],
                                          "epoch": peer.get("epoch")}
                    notify("SincroGit: newer work available",
                           f"'{cfg.name}': '{peer['host']}' has newer work ready. Open the "
                           f"panel and click Apply (or run --apply-handoff).")
                    self._emit(cfg.name, "handoff",
                               f"newer work from '{peer['host']}' ready to apply (ask mode)")
                return
            self._apply_handoff(st, peer["sha"], peer["host"])  # auto
        elif rel in ("equal", "mine_contains"):
            st.pending_handoff = None  # in sync, or I'm ahead — nothing to adopt
        else:  # "diverged"
            st.pending_handoff = None
            self._warn_handoff_once(
                st, peer["sha"],
                f"your work here and on '{peer['host']}' have DIVERGED (neither contains "
                f"the other). Not auto-merged: seal one side with Smart Commit, then sync. "
                f"See the README's handoff section.", notify_user=True)

    def _handoff_risky(self, st: RepoState, mine: str, peer_sha: str) -> list:
        """Paths applying the peer's tree would destroy beyond recovery: local
        content the snapshots don't hold (post-snapshot leftovers = what the
        filter refused, plus genuinely-untracked files) on paths the apply
        would touch. Assumes a _shadow_snapshot just ran."""
        touched = [p for _s, p in st.repo.diff_trees_name_status(peer_sha, mine)]
        return self._risky_paths(st, peer_sha, touched)

    def _apply_handoff(self, st: RepoState, sha: str, host: str) -> bool:
        """Adopt a peer's state: make the WORKTREE match its snapshot tree and
        record that as MY next snapshot. Content-first — the user's HEAD and
        branch are never moved (sealed history reconciles via the normal pull).
        Caller MUST hold op_lock and have verified work_relationship ==
        'theirs_contains' and _handoff_risky() == []. False = a local save
        landed mid-flight (captured, nothing touched); the next cycle
        re-validates against it."""
        repo, branch = st.repo, st.active_branch
        mine = repo.shadow_tip(branch)
        # target -> current: 'A' = exists here only (remove); rest = take theirs.
        changes = repo.diff_trees_name_status(sha, mine)
        if not self._apply_tree_to_worktree(st, sha, changes):
            self._emit(st.cfg.name, "handoff",
                       f"you saved an edit while '{host}' was being applied; "
                       f"nothing was touched — re-checking on the next sync")
            return False
        st.autosnap_pending = True       # my own mirror should track the new state
        st._handoff_warned_sha = None
        st.pending_handoff = None
        self._mark_action(st, "handoff")
        self._emit(st.cfg.name, "handoff", f"applied newer work from '{host}' — you're up to date")
        notify("SincroGit: caught up",
               f"'{st.cfg.name}': applied newer work from '{host}'. You're up to date.")
        return True

    def apply_handoff(self, name: str) -> tuple:
        """Apply a pending handoff (the 'ask'-mode one-click action / CLI). Re-validates
        from scratch under the lock (the peer may have moved since the notification), so
        it only ever fast-forwards when it's still provably safe. Returns (ok, msg)."""
        st = self.repo_state_by_name(name)
        if not st:
            return False, "repo not found"
        err = self._check_operable(st)
        if err:
            return False, err
        repo = st.repo
        try:
            with st.op_lock:
                if repo.is_busy():
                    return False, "repo busy (merge/rebase in progress)"
                if not repo.has_remote(st.cfg.remote):
                    return False, "no remote configured"
                repo.fetch_autosnaps(st.cfg.remote, user=st.user, timeout=st.cfg.git_timeout_sec)
                peer = repo.peer_wip(st.user, self._autosnap_host, st.active_branch)
                if not peer:
                    st.pending_handoff = None
                    return False, "the other machine's work is no longer available"
                # Snapshot local edits, then re-check the relationship from scratch.
                self._shadow_snapshot(st)
                mine = repo.shadow_tip(st.active_branch)
                if mine == peer["sha"]:
                    st.pending_handoff = None
                    return True, "already up to date"
                rel = repo.work_relationship(mine, peer["sha"])
                if rel != "theirs_contains":
                    st.pending_handoff = None
                    return False, f"can no longer apply safely (now '{rel}'); resolve by hand"
                risky = self._handoff_risky(st, mine, peer["sha"])
                if risky:
                    return False, (f"local content snapshots don't hold in "
                                   f"{len(risky)} file(s) (e.g. '{risky[0]}'); "
                                   f"move or commit them first")
                if not self._apply_handoff(st, peer["sha"], peer["host"]):
                    return False, ("you saved an edit while it was being applied; "
                                   "nothing was touched (the edit is snapshotted) "
                                   "— try again")
        except GitError as e:
            return False, str(e)
        return True, f"applied '{peer['host']}'"

    def _warn_handoff_once(self, st: RepoState, peer_sha: str, message: str,
                           notify_user: bool = False):
        """Emit a handoff warning at most once per distinct peer state (so a stuck
        divergence doesn't spam every sync cycle)."""
        if st._handoff_warned_sha == peer_sha:
            return
        st._handoff_warned_sha = peer_sha
        if notify_user:
            notify("SincroGit: machines diverged", f"'{st.cfg.name}': {message}")
        self._emit(st.cfg.name, "handoff", message, "WARNING")

    def _pull_after_fetch(self, st: RepoState, remote_exists: bool) -> bool:
        """Rebase the local branch onto new remote commits (assumes fetch ran).
        Returns False if a conflict occurred (and the repo was paused).

        The user's edits live UNCOMMITTED in the worktree (shadow model), so the
        rebase autostashes them. Two conflict shapes, both leaving the repo
        paused with an explanation:
          - the REBASE conflicts -> aborted, tree intact (like always);
          - the rebase succeeds but RE-APPLYING the dirty edits conflicts ->
            git leaves conflict markers + a stash entry (phase-0 spike). We
            snapshot BEFORE pulling, so the exact pre-pull content is one
            Time-Machine restore away either way.
        """
        repo, cfg = st.repo, st.cfg
        behind = repo.commits_behind(cfg.remote, st.active_branch) if (cfg.pull and remote_exists) else 0
        if behind <= 0:
            return True
        self._shadow_snapshot(st)  # the recovery guarantee for everything below
        ok, dirty_conflict = repo.rebase_onto_remote(cfg.remote, st.active_branch)
        if ok and not dirty_conflict:
            self._mark_action(st, "pull")
            self._emit(cfg.name, "pull", f"integrated {behind} commit(s) from the remote")
            # A never-sealed repo may have just pulled sealed commits: reflect it
            # in the panel ("since last seal") and base the seal clock on the real
            # one (the extra git call only happens until the first seal is seen).
            if not st.has_sealed:
                sealed = repo.last_sealed_time()
                if sealed is not None:
                    st.has_sealed = True
                    st.last_seal_epoch = float(sealed)
            return True
        st.paused = True
        # Keep the explanation on the state: the GUI shows it next to "Conflict"
        # instead of making the user dig through the Log.
        if dirty_conflict:
            st.conflict_msg = (
                f"The remote's commits were integrated, but re-applying your "
                f"uncommitted edits conflicted: conflict markers were left in "
                f"the affected file(s). Resolve them (your exact pre-pull state "
                f"is in the time machine), then press Resume."
            )
            detail = "pull left conflict markers; repo PAUSED"
        else:
            st.conflict_msg = (
                f"Your local changes overlap commits on '{cfg.remote}/{st.active_branch}'. "
                f"The rebase was aborted — your files are intact. Reconcile by hand "
                f"(e.g. `git pull --rebase` in a terminal and resolve, or move your "
                f"conflicting edits aside), then press Resume."
            )
            detail = "rebase conflict; repo PAUSED"
        notify(
            "SincroGit: conflict",
            f"Autosync PAUSED on '{cfg.name}'. Resolve the conflict by hand.",
        )
        self._mark_action(st, "conflict")
        self._emit(cfg.name, "conflict", detail, "ERROR")
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
            try:
                ok, cur = self._branch_ok(st)
                if not ok:
                    log.info("[%s] skipped: %s", st.cfg.name, self._branch_block_msg(st, cur))
                    continue
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
            try:
                ok, cur = self._branch_ok(st)
                if not ok:
                    log.info("[%s] skipped: %s", st.cfg.name, self._branch_block_msg(st, cur))
                    continue
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
            try:
                ok, cur = self._branch_ok(st)
                if not ok:
                    log.info("[%s] skipped: %s", st.cfg.name, self._branch_block_msg(st, cur))
                    continue
                with st.op_lock:
                    self._do_sync(st)
            except GitError as e:
                log.error("[%s] %s", st.cfg.name, e)

    # ------------------------------------------------- live repo management
    def add_repo(self, rc) -> tuple:
        """Add a repo to the running engine (no restart). `rc` is a RepoConfig
        already merged with defaults. Returns (ok, message)."""
        provider = self._pandoc_cmd if self._repo_versions_docx(rc) else None
        repo = GitRepo(rc.path, pandoc_provider=provider)
        try:
            if not repo.is_git_repo():
                return False, "not a git repository"
        except GitError as e:
            return False, str(e)
        with self._states_lock:
            if any(os.path.abspath(st.cfg.path) == os.path.abspath(rc.path) for st in self.states):
                return False, "repo already added"
        # The new repo isn't shared yet (not in `states`, no watcher), so this
        # git work needs no lock.
        st = RepoState(repo, rc, FileFilter(rc.max_file_bytes, rc.extra_excludes,
                                            rc.extra_includes, rc.max_include_bytes))
        try:
            repo.migrate_wip_tip(rc.branch)
            repo.ensure_shadow(rc.branch)
            sealed = repo.last_sealed_time()
            st.last_seal_epoch = float(sealed) if sealed else time.time()
            st.has_sealed = sealed is not None
            st.branch = repo.current_branch()
            st.user = repo.sincro_user()
        except GitError as e:
            return False, str(e)
        with self._states_lock:
            self.states.append(st)
        if self._watch_ready and self.watch is not None:
            try:
                self.watch.watch(rc.path, self._dirty_cb(st), ignore=st.file_filter.is_excluded)
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
        err = self._check_operable(st)
        if err:
            return False, err
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
        err = self._check_operable(st)
        if err:
            return False, "", "", err

        # Quick git work under the lock: capture a snapshot and take the diffs.
        with st.op_lock:
            if st.repo.is_busy():
                return False, "", "", "repo busy (merge/rebase in progress)"
            self._shadow_snapshot(st)
            repo, branch = st.repo, st.active_branch
            tree = repo.sync_shadow_index(branch)
            head = repo.head_sha()
            head_tree = repo.tree_of(head) if head else repo._empty_tree()
            if repo.trees_match(head_tree, tree):
                return False, "", "", "nothing to commit"
            base = repo.last_manual_sha()
            # Files in THIS commit = the window HEAD -> latest snapshot; the AI
            # context is the cumulative diff since the last MANUAL commit.
            name_status = repo.name_status_for_seal(head_tree, tree)
            stat = repo.diff_stat_for_seal(base=base or head_tree, target=tree)
            text = repo.diff_text_for_seal(self.config.ai.max_diff_chars,
                                           base=base or head_tree, target=tree)

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
        err = self._check_operable(st)
        if err:
            return False, err
        repo, cfg = st.repo, st.cfg
        with st.op_lock:
            if repo.is_busy():
                return False, "repo busy"
            if not repo.has_remote(cfg.remote):
                return False, "no remote configured"
            if not repo.fetch(cfg.remote, timeout=cfg.git_timeout_sec):
                return False, "fetch failed"
            remote_exists = repo.remote_branch_exists(cfg.remote, st.active_branch)
            ok = self._pull_after_fetch(st, remote_exists)
        return (ok, "pulled" if ok else "conflict; repo paused")

    # ------------------------------------------------- history / restore
    def repo_state_by_name(self, name: str):
        for st in self._states_snapshot():
            if st.cfg.name == name:
                return st
        return None

    def repo_config_view(self, name: str) -> dict | None:
        """The repo's EFFECTIVE config (defaults merged) as a plain dict, for
        the GUI's properties dialog. Built with dataclasses.asdict so a new
        RepoConfig field shows up here automatically — no mirror list to keep
        in sync on the GUI side."""
        st = self.repo_state_by_name(name)
        return dataclasses.asdict(st.cfg) if st else None

    def host_name(self) -> str:
        """This machine's autosnap host name (the ref-path-safe identity used
        in refs/autosnap/<user>/<host>/<branch>). Exposed so the GUI never
        imports gitrepo internals."""
        return self._autosnap_host

    # ------------------------------------------- shared manual-action helpers
    def _check_operable(self, st: RepoState) -> str | None:
        """The branch-guard preamble every manual action shares: None if the repo
        can be operated on right now, else a human-readable reason (off the
        configured branch / detached HEAD / the git call itself failed). Sets
        st.active_branch as a side effect (via _branch_ok)."""
        try:
            ok, cur = self._branch_ok(st)
        except GitError as e:
            return str(e)
        return None if ok else self._branch_block_msg(st, cur)

    def _risky_paths(self, st: RepoState, sha: str, touched) -> list:
        """Of `touched` paths, those whose CURRENT worktree content the snapshots
        do NOT hold — the filter refused it (excluded / oversize / binary), or
        it's untracked while `sha` tracks it. Applying `sha` there would destroy
        content that exists nowhere in git, so every restore/handoff refuses on a
        non-empty result. Assumes a _shadow_snapshot just ran."""
        return sorted(set(touched) & (set(self._uncaptured(st))
                                      | set(st.repo.untracked_collisions(sha))))

    def _apply_tree_to_worktree(self, st: RepoState, sha: str, changes: list) -> bool:
        """Make the WORKTREE match `sha` on the given (status, path) name-status
        (target->current): restore differing files to `sha`'s content, delete the
        ones that exist only now ('A'). Worktree-only — the user's index stays
        theirs — and a closing snapshot records the restore so it's itself
        versioned. The shared tail of restore_files / restore_repo / handoff.

        Returns False — WITHOUT touching the worktree — when a save landed
        after the caller computed `changes`: validation snapshots first, but
        the git reads that follow leave a small window, and an edit saved in
        it exists nowhere else. The last-second snapshot here captures it (so
        nothing is lost either way); the stale plan is refused and the caller
        reports/retries against the new content."""
        if self._shadow_snapshot(st):
            return False
        failed: list = []
        try:
            st.repo.restore_paths_worktree(sha, [p for s, p in changes if s != "A"])
            failed = st.repo.delete_paths_worktree([p for s, p in changes if s == "A"])
        finally:
            self._shadow_snapshot(st)  # record what WAS applied, even mid-error
        if failed:
            sample = ", ".join(failed[:5]) + (", …" if len(failed) > 5 else "")
            self._emit(st.cfg.name, "info",
                       f"{len(failed)} file(s) slated for removal could not be "
                       f"deleted (open in another program?): {sample}. Close "
                       f"whatever holds them and run the restore again.",
                       "WARNING")
        return True

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
            return st.repo.file_history(relpath, limit, branch=st.active_branch)
        except GitError as e:
            log.error("[%s] history failed: %s", repo_name, e)
            return []

    def repo_history(self, repo_name: str, limit: int = 200) -> list:
        """The repo's distinct whole-tree states, newest first (sealed commits +
        reflog snapshots + fetched autosnap refs) — the Time Machine timeline.
        Each item: sha, epoch, subject, source."""
        st = self.repo_state_by_name(repo_name)
        if not st:
            return []
        try:
            return st.repo.repo_history(limit, branch=st.active_branch)
        except GitError as e:
            log.error("[%s] repo history failed: %s", repo_name, e)
            return []

    def export_file_version(self, repo_name: str, relpath: str, sha: str,
                            dest_path: str):
        """Write a file's version at `sha` to `dest_path` — recover an old
        version WITHOUT overwriting the current one (e.g. under another name).
        Nothing in the repo changes; if `dest_path` lands inside the repo, the
        new file is simply picked up by the next snapshot like any other.
        Returns (ok, message). Byte-exact (works for binaries too)."""
        st = self.repo_state_by_name(repo_name)
        if not st:
            return False, "repo not found"
        try:
            data = st.repo.file_bytes_at(relpath, sha)
        except GitError as e:
            return False, str(e)
        if data is None:
            return False, f"'{relpath}' doesn't exist in that version"
        try:
            with open(dest_path, "wb") as fh:
                fh.write(data)
        except OSError as e:
            return False, str(e)
        self._emit(repo_name, "info",
                   f"saved a copy of '{relpath}' @ {sha[:8]} as '{dest_path}'")
        return True, "saved"

    def search_in_file_versions(self, repo_name: str, relpath: str, text: str,
                                limit: int = 50) -> list:
        """[(sha, count)] — occurrences of `text` in each version of the file
        (the same versions file_history lists, newest first). The GUI marks the
        transitions ("this is where it appeared / vanished"). One `git show`
        per version: callers run it off the GUI thread."""
        st = self.repo_state_by_name(repo_name)
        if not st or not text:
            return []
        out = []
        for ver in self.file_history(repo_name, relpath, limit):
            try:
                content = st.repo.file_text_at(relpath, ver["sha"]) or ""
            except GitError:
                content = ""
            out.append((ver["sha"], content.count(text)))
        return out

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
        """Restore a file to a past version. Returns (ok, message).

        Refuses if the file's CURRENT content is something snapshots can't
        capture (excluded, over the size limit, binary): that content exists
        nowhere in git, so overwriting it would destroy it beyond recovery —
        the same policy the handoff fast-forward applies.
        """
        st = self.repo_state_by_name(repo_name)
        if not st:
            return False, "repo not found"
        err = self._check_operable(st)  # off-branch: a capture would snapshot the wrong branch
        if err:
            return False, err
        with st.op_lock:  # don't race with the snapshot/seal cycle
            if st.repo.is_busy():
                return False, "repo busy (merge/rebase in progress)"
            try:
                # Snapshot pending edits BEFORE overwriting: an edit saved since
                # the last snapshot exists nowhere else — without this, the
                # restore would destroy it beyond even the reflog's reach.
                self._shadow_snapshot(st)
                # Whatever that pass could NOT capture (content the filter
                # refused, or an untracked-but-filtered file that `sha` tracks)
                # would be destroyed by the restore. Refuse instead.
                if self._risky_paths(st, sha, [relpath]):
                    return False, (
                        f"'{relpath}' has local content that snapshots can't "
                        f"capture (excluded, over the size limit or binary); "
                        f"copy it somewhere safe first, then restore"
                    )
                # Worktree-only write: the user's index stays theirs, so the
                # restore shows up in their `git status` as a plain edit.
                # ("M" = take `sha`'s content; the shared tail also closes the
                # validate->write window and versions the restore itself.)
                if not self._apply_tree_to_worktree(st, sha, [("M", relpath)]):
                    return False, ("you saved an edit while the restore was "
                                   "being prepared; nothing was touched (the "
                                   "edit is snapshotted) — try again")
            except GitError as e:
                return False, str(e)
        self._emit(repo_name, "info", f"restored '{relpath}' from {sha[:8]}")
        return True, "restored"

    def restore_files(self, repo_name: str, relpaths: list, sha: str):
        """Selectively restore SEVERAL files to their state at `sha`, atomically
        captured into one WIP amend. Returns (ok, message).

        Per file, "its state at `sha`" means: its content there (checkout), or
        its REMOVAL if `sha` doesn't have it. Same protections as restore_file:
        branch guard, busy check, pending edits snapshotted first, and a refusal
        if any SELECTED file's current content is something snapshots can't
        capture (excluded, over the size limit, binary).
        """
        relpaths = [p.replace("\\", "/") for p in relpaths]
        st = self.repo_state_by_name(repo_name)
        if not st:
            return False, "repo not found"
        if not relpaths:
            return False, "no files selected"
        err = self._check_operable(st)  # off-branch: a capture would snapshot the wrong branch
        if err:
            return False, err
        with st.op_lock:  # don't race with the snapshot/seal cycle
            if st.repo.is_busy():
                return False, "repo busy (merge/rebase in progress)"
            try:
                # Snapshot pending edits into the WIP BEFORE overwriting (see
                # restore_file), then refuse if a SELECTED file still has content
                # that pass couldn't capture — it exists nowhere in git.
                self._shadow_snapshot(st)
                risky = self._risky_paths(st, sha, relpaths)  # only the SELECTED paths
                if risky:
                    sample = ", ".join(risky[:5]) + (", …" if len(risky) > 5 else "")
                    return False, (
                        f"{len(risky)} selected file(s) have local content that "
                        f"snapshots can't capture (excluded, over the size limit "
                        f"or binary): {sample}. Copy them somewhere safe first, "
                        f"then restore"
                    )
                # Restrict the tree-vs-tree diff to the SELECTED paths: 'A'
                # (created since `sha`) -> remove; any other difference -> take
                # `sha`'s version; files that don't differ are already there.
                # (Tree-vs-tree so files the user's HEAD doesn't track still count.)
                mine = st.repo.shadow_tip(st.active_branch)
                diff = {p: s for s, p in st.repo.diff_trees_name_status(sha, mine)}
                changes = [(diff[p], p) for p in relpaths if p in diff]
                if not changes:
                    return True, "nothing to restore (files already match)"
                # One atomic capture; refused if a save landed since the plan.
                if not self._apply_tree_to_worktree(st, sha, changes):
                    return False, ("you saved an edit while the restore was "
                                   "being prepared; nothing was touched (the "
                                   "edit is snapshotted) — try again")
            except GitError as e:
                return False, str(e)
        n = len(changes)
        self._emit(repo_name, "info", f"restored {n} file(s) from {sha[:8]}")
        return True, f"restored {n} file(s)"

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

    def restore_repo_preview(self, repo_name: str, sha: str):
        """What restoring the WHOLE repo to `sha` would do, WITHOUT touching
        anything. Returns (ok, payload_or_msg); payload = {"changes", "risky"}.

        `changes` items are (verb, path): 'revert' (differs; goes back to `sha`'s
        content), 'delete' (created since `sha`; the restore removes it),
        'recreate' (deleted since `sha`; it comes back). `risky` lists files the
        restore would touch whose CURRENT content snapshots can't capture —
        restore_repo refuses while they exist.

        Takes a fresh snapshot first (invisible: it only moves the shadow ref),
        so the comparison is exact and the preview doubles as a recovery point.
        Callers run it off the GUI thread (git work on a big repo takes a moment).
        """
        st = self.repo_state_by_name(repo_name)
        if not st:
            return False, "repo not found"
        verb = {"M": "revert", "T": "revert", "A": "delete", "D": "recreate"}
        with st.op_lock:
            if st.repo.is_busy():
                return False, "repo busy (merge/rebase in progress)"
            try:
                self._shadow_snapshot(st)
                mine = st.repo.shadow_tip(st.active_branch)
                raw = st.repo.diff_trees_name_status(sha, mine)
                # Risky = what restore_repo's guard would refuse on: uncaptured
                # local content on a path the restore would touch.
                risky = self._risky_paths(st, sha, [p for _s, p in raw])
            except GitError as e:
                return False, str(e)
        changes = [(verb.get(s, "revert"), p) for s, p in raw]
        return True, {"changes": changes, "risky": risky}

    def restore_repo(self, repo_name: str, sha: str):
        """Restore the WHOLE working tree to the state at `sha` (a sealed/snapshot/
        autosnap commit), captured into the WIP so it's versioned and reversible.
        HEAD is not moved. Returns (ok, message).

        Refuses if any file's CURRENT content is something snapshots can't
        capture (excluded, over the size limit, binary): that content exists
        nowhere in git, so the restore would destroy it beyond recovery — the
        same policy the handoff fast-forward applies.
        """
        st = self.repo_state_by_name(repo_name)
        if not st:
            return False, "repo not found"
        err = self._check_operable(st)  # off-branch: a capture would snapshot the wrong branch
        if err:
            return False, err
        with st.op_lock:  # don't race with the snapshot/seal cycle
            if st.repo.is_busy():
                return False, "repo busy (merge/rebase in progress)"
            try:
                # Snapshot pending edits BEFORE overwriting: anything saved
                # since the last snapshot exists nowhere else — without this,
                # the restore would destroy it beyond even the reflog's reach.
                self._shadow_snapshot(st)
                mine = st.repo.shadow_tip(st.active_branch)
                changes = st.repo.diff_trees_name_status(sha, mine)
                # Whatever that pass could NOT capture (content the filter
                # refused, or untracked-but-filtered files that `sha`'s tree
                # tracks) would be destroyed where the restore touches it.
                risky = self._risky_paths(st, sha, [p for _s, p in changes])
                if risky:
                    sample = ", ".join(risky[:5]) + (", …" if len(risky) > 5 else "")
                    return False, (
                        f"{len(risky)} file(s) have local content that snapshots "
                        f"can't capture (excluded, over the size limit or binary) "
                        f"and the restore would destroy it: {sample}. Copy them "
                        f"somewhere safe first, then restore"
                    )
                # Worktree-only application of `sha`'s tree; the closing snapshot
                # records the restored state (the user's index stays theirs).
                if not self._apply_tree_to_worktree(st, sha, changes):
                    return False, ("you saved an edit while the restore was "
                                   "being prepared; nothing was touched (the "
                                   "edit is snapshotted) — try again")
            except GitError as e:
                return False, str(e)
        self._emit(repo_name, "info", f"restored whole repo to {sha[:8]}")
        return True, "restored"
