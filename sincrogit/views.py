"""Read-only CLI views: `sincrogit status` and `sincrogit log`.

Both are safe alongside a running daemon — status only READS git (no Engine,
no setup migrations, no locks), and log only reads events.jsonl (whose
corrupt-line guard already tolerates a torn concurrent append). That is why
neither goes through the one-shot "daemon running" refusal in __main__.

`status` answers "is everything being looked after?" in one glance per repo:
branch, last snapshot / last commit ages, how much WIP sits unsealed, and
whether there are edits newer than the last snapshot. `log` prints the same
structured event stream the panel's Log tab shows, filterable by repo,
action type and minimum severity.
"""

import os
import time
from datetime import datetime

from .events import ACTIONS, EventLog
from .gitrepo import GitError, GitRepo
from .runtime import ping_existing_instance

_LEVEL_RANK = {"DEBUG": 0, "INFO": 1, "WARNING": 2, "ERROR": 3}


def _ago(epoch) -> str:
    if not epoch:
        return "never"
    s = max(0.0, time.time() - epoch)
    if s < 90:
        return f"{int(s)}s ago"
    if s < 90 * 60:
        return f"{int(s // 60)}m ago"
    if s < 48 * 3600:
        return f"{s / 3600:.1f}h ago"
    return f"{int(s // 86400)}d ago"


def _repo_row(rc) -> tuple:
    """(state, branch, snapshot_age, commit_age, unsealed, dirty) for one repo.
    `state` doubles as the error text when the repo can't be inspected."""
    if not os.path.isdir(rc.path):
        return ("MISSING PATH", "-", "-", "-", "-", False)
    repo = GitRepo(rc.path)
    try:
        if not repo.is_git_repo():
            return ("NOT A GIT REPO", "-", "-", "-", "-", False)
        current = repo.current_branch()      # None on an unborn branch (no commits)
        branch = current if rc.track_current_branch else rc.branch
        state = "ok"
        if repo.is_busy():
            state = "STALE LOCK" if repo.stale_lock() else "BUSY (merge?)"
        elif current is None:
            state = "no commits yet"
        elif not rc.track_current_branch and current != rc.branch:
            state = f"OFF-BRANCH (on '{current}')"
        # The branch cell must never be None — a bare f-string format of None
        # raises TypeError and would crash the whole table (an unborn repo in
        # track mode has current=None, so `branch` is None too).
        disp = current or (rc.branch if not rc.track_current_branch else "—")
        if not branch:
            return (state, disp, "never", "never", "-", False)
        shadow = repo.shadow_ref(branch)
        tip = repo.shadow_tip(branch)
        snap_age = _ago(repo.ref_time(shadow)) if tip else "never"
        commit_age = _ago(repo.ref_time(branch))
        unsealed = repo.commits_ahead(branch, shadow) if tip else None
        dirty = repo.worktree_differs_from(shadow) if tip else False
        return (state, disp, snap_age, commit_age,
                "-" if unsealed is None else str(unsealed), dirty)
    except GitError as e:
        return (f"ERROR: {e}", "-", "-", "-", "-", False)


def run_status(config, repo_name: str | None = None) -> int:
    """One line per repo. Exit 0 unless a repo can't be inspected at all."""
    daemon = ping_existing_instance()
    print(f"daemon: {'running' if daemon else 'NOT running — nothing is being snapshotted'}")

    repos = [rc for rc in config.repos
             if repo_name is None or rc.name == repo_name]
    if repo_name is not None and not repos:
        names = ", ".join(rc.name for rc in config.repos) or "(none configured)"
        print(f"Repo '{repo_name}' not found. Configured repos: {names}")
        return 1
    if not repos:
        print("No repos configured yet (add one from the GUI: Status -> 'Add repo...').")
        return 0

    header = f"{'REPO':<18} {'BRANCH':<12} {'STATE':<24} {'SNAPSHOT':<10} {'COMMIT':<10} {'UNSEALED':<9} EDITS"
    print()
    print(header)
    print("-" * len(header))
    failed = 0
    for rc in repos:
        state, branch, snap, commit, unsealed, dirty = _repo_row(rc)
        if state.startswith(("MISSING", "NOT A GIT", "ERROR")):
            failed += 1
        marks = []
        if not rc.push:
            marks.append("push off")
        if not rc.pull:
            marks.append("pull off")
        state_txt = state + (f" [{', '.join(marks)}]" if marks else "")
        print(f"{rc.name:<18} {branch:<12} {state_txt:<24} {snap:<10} {commit:<10} "
              f"{unsealed:<9} {'yes (pre-snapshot)' if dirty else 'no'}")
    print()
    print("SNAPSHOT = age of the last time-machine capture; COMMIT = branch tip age;")
    print("UNSEALED = snapshots taken since the last permanent commit; EDITS = work")
    print("newer than the last snapshot (waiting for the next capture).")
    return 1 if failed else 0


def _events_path(config) -> str:
    """events.jsonl lives next to the log file — same derivation as the GUI."""
    log_dir = os.path.dirname(os.path.abspath(config.log.file)) or "."
    return os.path.join(log_dir, "events.jsonl")


def run_log(config, repo: str | None = None, actions: str | None = None,
            level: str | None = None, tail: int = 50) -> int:
    """Print the structured event log, oldest to newest (the last line is the
    most recent). Filters compose; `tail=0` means everything."""
    wanted = None
    if actions:
        wanted = {a.strip() for a in actions.split(",") if a.strip()}
        unknown = wanted - set(ACTIONS)
        if unknown:
            # Free-form actions exist, but a typo silently matching nothing is
            # the likelier story — say what the knowns are and continue.
            print(f"note: unknown action(s) {', '.join(sorted(unknown))} — "
                  f"known: {', '.join(ACTIONS)}")
    min_rank = _LEVEL_RANK[level] if level else 0

    events = EventLog(_events_path(config)).load_all()
    out = []
    for ev in events:
        # Same rule as the panel's Log tab: a repo filter keeps GLOBAL events
        # (repo == "") — lock/suspend/startup lines are context for every repo.
        if repo is not None and ev.repo and ev.repo != repo:
            continue
        if wanted is not None and ev.action not in wanted:
            continue
        if _LEVEL_RANK.get(ev.level, 1) < min_rank:
            continue
        out.append(ev)

    if tail:
        out = out[-tail:]
    if not out:
        print("No events match." if events else
              f"No event log yet at {_events_path(config)} — has the daemon run?")
        return 0
    for ev in out:
        ts = datetime.fromtimestamp(ev.ts).strftime("%Y-%m-%d %H:%M:%S")
        print(f"{ts}  {ev.level:<7} {(ev.repo or '·'):<14} {ev.action:<10} {ev.message}")
    return 0
