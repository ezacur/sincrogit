"""Entry point: `python -m sincrogit` / the standalone SincroGit.exe.

Launch model:
  (no arguments)            -> GUI tray app + daemon (single instance)
  --tray [--config X]       -> GUI tray app + daemon (single instance)
  --headless [--config X]   -> daemon without GUI (servers / automation)
  --snapshot-once|--seal-once|--sync-once   -> CLI one-shot and exit
  --history FILE [--pick N] -> browse/restore a file's version history
  --autosnaps               -> fetch & list autosnap recovery points (per machine)
  --commit REPO [-m MSG|-y] -> manual commit of REPO (edit the proposed message, then seal+push)
  --apply-handoff REPO      -> apply your other machine's pending live work to REPO
  --autostart on|off        -> register/unregister start-at-login (per-user Run key)

With no arguments the GUI launches; if an instance is already running, the new
launch just asks the running one to show its panel and exits. Any argument is
treated as a command-line invocation (output goes to the launching terminal).
"""

import argparse
import os
import signal
import subprocess
import sys
import tempfile
import threading
from datetime import datetime

from .config import load_config
from .engine import Engine
from .log import setup_logging
from .runtime import (
    acquire_instance_mutex,
    acquire_single_instance,
    attach_parent_console,
    ensure_config,
    find_config,
    ping_existing_instance,
    serve_activation,
    signal_existing_instance,
)


def _run_tray(explicit_config) -> int:
    """Launch the GUI/daemon as a single instance."""
    # Authoritative single-instance guard (Windows named mutex; no-op elsewhere).
    # It can't be stolen by an app squatting on the lock port, so two instances can
    # never race git on the same repos — the failure Gemini flagged.
    mutex = acquire_instance_mutex()
    if mutex:
        signal_existing_instance()  # best-effort: bring the running panel to front
        print("SincroGit is already running.", file=sys.stderr)
        return 0
    lock = acquire_single_instance()
    if lock is None:
        # The lock port is taken. When WE hold the mutex (False) that is
        # authoritative — no other mutex-aware SincroGit runs — so an ACK from
        # the port can only be an impostor (or a pre-mutex build): exiting on
        # it would let any local process that spoofs the handshake keep
        # SincroGit from ever starting. Only without a mutex (None: non-Windows
        # or ctypes failure) does the port handshake get to decide.
        if mutex is None and signal_existing_instance():
            print("SincroGit is already running.", file=sys.stderr)
            return 0
        if mutex is False:
            print(
                "Lock port held by another process; single-instance is still "
                "enforced (mutex), but the 'show panel' channel is unavailable.",
                file=sys.stderr,
            )
        else:
            print(
                "Lock port held by another app; starting without single-instance "
                "protection.",
                file=sys.stderr,
            )
        # lock stays None -> the activation listener simply won't run.
    cfg_path, created = ensure_config(explicit_config)
    try:
        from .gui.app import main as tray_main
    except ImportError as e:
        print(
            f"The GUI needs PyQt5 (pip install PyQt5): {e}\n"
            f"To run the background daemon without the GUI, use:  "
            f"python -m sincrogit --headless",
            file=sys.stderr,
        )
        return 2
    try:
        return tray_main(cfg_path, lock_socket=lock, open_config=created)
    except (FileNotFoundError, ValueError) as e:
        print(f"Configuration error: {e}", file=sys.stderr)
        return 2


def _history_command(engine, file_arg: str, pick) -> int:
    repo_name, rel = engine.locate_file(file_arg)
    if not repo_name:
        print(f"'{file_arg}' is not inside any configured repo.", file=sys.stderr)
        return 1

    versions = engine.file_history(repo_name, rel)
    if not versions:
        print(f"No history found for '{rel}' (repo {repo_name}).")
        return 0

    print(f"History of '{rel}' (repo {repo_name}):")
    for i, v in enumerate(versions, 1):
        ts = datetime.fromtimestamp(v["epoch"]).strftime("%Y-%m-%d %H:%M:%S")
        # Distinguish all three sources the history returns (a fetched autosnap
        # state used to be mislabeled 'snapshot'). Padded to align the column.
        kind = {"sealed": "sealed  ", "snapshot": "snapshot",
                "autosnap": "autosnap"}.get(v["source"], "snapshot")
        print(f"  [{i}] {ts}  {kind}  {v['subject']}")

    if pick is None:
        try:
            raw = input("Pick a version to restore (number, Enter to cancel): ").strip()
        except EOFError:
            raw = ""
        if not raw:
            print("Cancelled.")
            return 0
        try:
            pick = int(raw)
        except ValueError:
            print("Invalid selection.", file=sys.stderr)
            return 1

    if not (1 <= pick <= len(versions)):
        print(f"Selection out of range (1-{len(versions)}).", file=sys.stderr)
        return 1

    chosen = versions[pick - 1]
    ok, msg = engine.restore_file(repo_name, rel, chosen["sha"])
    if ok:
        ts = datetime.fromtimestamp(chosen["epoch"]).strftime("%Y-%m-%d %H:%M:%S")
        print(f"Restored '{rel}' to its version from {ts}.")
        return 0
    print(f"Restore failed: {msg}", file=sys.stderr)
    return 1


def _autosnaps_command(engine) -> int:
    """Fetch and list the autosnap recovery points for every configured repo.

    Useful on another machine after a disk failure: it shows each machine's last
    mirrored state. Afterwards `--history FILE` includes those states too (the
    refs are now local), so you can pull individual files from them.
    """
    found = False
    for st in engine.states:
        states = engine.fetch_autosnaps(st.cfg.name)
        if not states:
            continue
        found = True
        print(f"Autosnap states for repo '{st.cfg.name}':")
        for s in states:
            ts = (datetime.fromtimestamp(s["epoch"]).strftime("%Y-%m-%d %H:%M:%S")
                  if s["epoch"] else "?")
            print(f"  {ts}  {s['host']}/{s['branch']}  {s['sha'][:10]}")
    if not found:
        print("No autosnap states found on the remote(s).")
    return 0


def _apply_handoff_command(engine, repo_name: str) -> int:
    """Apply a pending cross-machine handoff for one repo (the 'ask'-mode action
    from the CLI): fast-forward this checkout to your other machine's newer work,
    if that's still safe. Re-validates before touching anything."""
    ok, msg = engine.apply_handoff(repo_name)
    print(("Handoff applied: " if ok else "Nothing applied: ") + msg)
    return 0 if ok else 1


def _resolve_editor() -> str:
    """The editor command string, resolved git-style: GIT_EDITOR / VISUAL /
    EDITOR / git's core.editor, else a platform default. It may include arguments
    (e.g. 'code --wait'); we run it through the shell, like git does."""
    for var in ("GIT_EDITOR", "VISUAL", "EDITOR"):
        val = os.environ.get(var)
        if val:
            return val
    try:
        res = subprocess.run(["git", "config", "--get", "core.editor"],
                             capture_output=True, text=True)
        if res.returncode == 0 and res.stdout.strip():
            return res.stdout.strip()
    except OSError:
        pass
    return "notepad" if os.name == "nt" else "vi"


def _edit_message_in_editor(initial: str, files_text: str):
    """Open the user's editor with the proposed message (git-commit style).
    Returns the edited text with '#' comment lines stripped, or None if aborted
    or the editor could not be opened.
    """
    comment = "\n".join(f"#   {ln}" for ln in (files_text or "").splitlines())
    template = (
        f"{initial}\n\n"
        "# Edit the commit message above. Lines starting with '#' are ignored;\n"
        "# an empty message aborts the commit.\n"
        "#\n"
        "# Files in this commit:\n"
        f"{comment}\n"
    )
    fd, path = tempfile.mkstemp(prefix="SINCROGIT_COMMIT_", suffix=".txt", text=True)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(template)
        editor = _resolve_editor()
        # Run via the shell so editor strings with args/quoted paths (e.g.
        # 'code --wait', '"C:\\Program Files\\...\\app.exe"') parse like in git.
        try:
            subprocess.run(f'{editor} "{path}"', shell=True, check=True)
        except (OSError, subprocess.CalledProcessError) as e:
            print(f"Could not open the editor ({editor}): {e}\n"
                  f"Set $EDITOR or use --message.", file=sys.stderr)
            return None
        with open(path, "r", encoding="utf-8") as fh:
            raw = fh.read()
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass
    lines = [ln for ln in raw.splitlines() if not ln.lstrip().startswith("#")]
    return "\n".join(lines).strip()


def _commit_command(engine, repo_name: str, message, assume_yes: bool) -> int:
    """Manual "smart commit" of one repo: propose a Conventional-Commits message,
    let the user edit it in their editor, then seal+push. With --message, commit
    that message directly; with --yes, accept the AI proposal without editing.
    """
    if not engine.repo_state_by_name(repo_name):
        names = ", ".join(s.cfg.name for s in engine.states) or "(none configured)"
        print(f"Repo '{repo_name}' not found. Configured repos: {names}", file=sys.stderr)
        return 1

    if message:
        ok, msg = engine.seal_repo_now(repo_name, message=message)
        print(msg if ok else f"Commit failed: {msg}", file=(sys.stdout if ok else sys.stderr))
        return 0 if ok else 1

    ok, title, body, files = engine.propose_seal_message(repo_name)
    if not ok:
        # On failure, `files` carries the reason (e.g. "nothing to commit").
        print(files or "Nothing to commit.", file=sys.stderr)
        return 1

    proposed = title if not body else f"{title}\n\n{body}"
    if assume_yes:
        final = proposed
    else:
        final = _edit_message_in_editor(proposed, files)
    if not final or not final.strip():
        print("Aborted: empty commit message.", file=sys.stderr)
        return 1

    ok, msg = engine.seal_repo_now(repo_name, message=final)
    if not ok:
        print(f"Commit failed: {msg}", file=sys.stderr)
        return 1
    print(f"Committed '{repo_name}': {final.splitlines()[0]}")
    return 0


def _daemon_running() -> bool:
    """Is a SincroGit daemon (tray or headless) already running? Side-effect-free
    detection for CLI one-shots: the Windows named mutex is authoritative; the
    presence ping covers other platforms (and an app squatting the port answers
    nothing, so it never false-positives). If no daemon runs, we end up HOLDING
    the mutex for the rest of this short-lived process — which conveniently also
    keeps a daemon from starting mid-command."""
    mutex = acquire_instance_mutex()
    if mutex:
        return True
    if mutex is False:
        # We hold the authoritative mutex: no daemon runs. Don't consult the
        # ping — a spoofed ACK would make every one-shot refuse to run.
        return False
    return ping_existing_instance()


def _serve_activation_pings(lock, get_engine=None) -> None:
    """Answer the single-instance handshake on the lock socket. A headless daemon
    has no panel to show, but replying the ACK is what tells a second launch
    (tray or headless) that a real SincroGit already runs, so it backs off.
    A "flushquit" command (build.ps1's rebuild cycle) flushes every repo and
    stops the engine — `get_engine` returns the live Engine (or None during the
    brief startup window, in which case the command is ignored and the caller's
    forced-kill fallback covers it)."""
    def loop():
        while True:
            try:
                conn, _ = lock.accept()
            except OSError:
                break  # socket closed at exit
            verdict = serve_activation(conn)
            if verdict == "flushquit" and get_engine is not None:
                eng = get_engine()
                if eng is not None:
                    eng.flush_now(wait=True)
                    eng.stop()  # run() returns -> the process exits cleanly
                    break

    threading.Thread(target=loop, name="sincrogit-activation", daemon=True).start()


def _acquire_headless_instance(get_engine=None):
    """Single-instance guard for --headless, same scheme as the tray: the named
    mutex is authoritative on Windows; the lock port covers other platforms and
    doubles as the handshake channel. Returns (ok, lock_socket): ok=False means
    another SincroGit (tray or headless) already runs — two daemons amending the
    same repos' WIPs would race each other's git work."""
    mutex = acquire_instance_mutex()
    if mutex:
        return False, None
    lock = acquire_single_instance()
    if lock is None:
        # Port taken: when we hold the mutex it is authoritative (see
        # _run_tray) — a spoofed ACK must not stop the daemon. Without one
        # (non-Windows), a real SincroGit answering the handshake means back off.
        if mutex is None and signal_existing_instance():
            return False, None
        print("Lock port held by another app; continuing.", file=sys.stderr)
        return True, None
    _serve_activation_pings(lock, get_engine)
    return True, lock


def _run_headless(config, logger, engine_ref=None) -> int:
    engine = Engine(config)
    if engine_ref is not None:
        engine_ref["engine"] = engine  # expose to the activation listener (flushquit)

    def _handle(signum, _frame):
        logger.info("Signal %s received, shutting down...", signum)
        engine.stop()

    signal.signal(signal.SIGINT, _handle)
    try:
        signal.signal(signal.SIGTERM, _handle)
    except (ValueError, AttributeError):
        pass

    engine.run()
    return 1 if engine.crashed else 0


def _cli_conflict(args) -> str | None:
    """Reject nonsensical flag combinations up front — argparse can't express
    "these three compose but are exclusive with those seven". Returns an error
    string (caller prints usage + exits 2) or None when the flags are coherent.

    Without this, a slip like `--history F --commit R` runs whichever branch
    main() checks first and silently ignores the rest; an orphan `--pick`/
    `--message`/`--yes` (its parent action missing) also passes unnoticed.
    """
    actions = []
    if args.tray:
        actions.append("--tray")
    if args.headless:
        actions.append("--headless")
    if args.history is not None:
        actions.append("--history")
    if args.autosnaps:
        actions.append("--autosnaps")
    if args.commit is not None:
        actions.append("--commit")
    if args.apply_handoff is not None:
        actions.append("--apply-handoff")
    if args.doctor:
        actions.append("--doctor")
    if args.autostart is not None:
        actions.append("--autostart")
    # --snapshot-once/--seal-once/--sync-once DELIBERATELY combine (one batch pass),
    # so they count as a single action category here.
    once = [n for n, v in (("--snapshot-once", args.snapshot_once),
                            ("--seal-once", args.seal_once),
                            ("--sync-once", args.sync_once)) if v]
    if once:
        actions.append("+".join(once))
    if len(actions) > 1:
        return f"these actions can't be combined: {', '.join(actions)}"
    # Modifiers that only mean something with their parent action.
    if args.pick is not None and args.history is None:
        return "--pick only applies with --history"
    if (args.message is not None or args.yes) and args.commit is None:
        return "--message/--yes only apply with --commit"
    if args.message is not None and args.yes:
        return "--message and --yes are mutually exclusive (one gives the message, the other accepts the AI's)"
    return None


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv

    # No arguments -> GUI tray app (single instance).
    if not argv:
        return _run_tray(None)

    # Any argument -> command-line invocation. Make output visible if we're a
    # windowed (--noconsole) frozen exe launched from a terminal.
    attach_parent_console()

    parser = argparse.ArgumentParser(
        prog="sincrogit",
        description="Automatic synchronization with robust Git versioning.",
    )
    parser.add_argument("--config", "-c", default=None, help="Path to config.yaml")
    parser.add_argument("--tray", action="store_true", help="Launch the GUI tray app.")
    parser.add_argument("--headless", action="store_true", help="Run the daemon without GUI.")
    parser.add_argument("--snapshot-once", action="store_true",
                        help="Run one snapshot pass on all repos and exit.")
    parser.add_argument("--seal-once", action="store_true",
                        help="Force a seal (+push) on all repos and exit.")
    parser.add_argument("--sync-once", action="store_true",
                        help="Run one sync pass (pull+push) on all repos and exit.")
    parser.add_argument("--history", metavar="FILE",
                        help="Show FILE's version history and restore a chosen version.")
    parser.add_argument("--pick", type=int, metavar="N",
                        help="With --history: restore version N non-interactively.")
    parser.add_argument("--autosnaps", action="store_true",
                        help="Fetch and list the autosnap recovery points (per machine) "
                             "for every configured repo.")
    parser.add_argument("--commit", metavar="REPO",
                        help="Manual commit of REPO now: propose a Conventional Commits "
                             "message, edit it in your editor, then seal + push.")
    parser.add_argument("--message", "-m", metavar="MSG",
                        help="With --commit: use MSG directly (skip the AI proposal/editor).")
    parser.add_argument("--yes", "-y", action="store_true",
                        help="With --commit: accept the AI proposal without editing.")
    parser.add_argument("--apply-handoff", metavar="REPO",
                        help="Apply your other machine's pending live work to REPO (handoff).")
    parser.add_argument("--force", action="store_true",
                        help="Run a one-shot even if a SincroGit daemon is already running "
                             "(risk of racing its git work on the same repos).")
    parser.add_argument("--doctor", action="store_true",
                        help="Health check: git, config, each repo's branch/remote/"
                             "credentials, pandoc, AI backends, daemon. Exit 0 = healthy.")
    parser.add_argument("--autostart", choices=["on", "off"],
                        help="Register (on) or unregister (off) start-at-login for "
                             "the current user (Windows Run key) and exit. The GUI "
                             "checkbox in Settings does the same.")
    args = parser.parse_args(argv)

    conflict = _cli_conflict(args)
    if conflict:
        parser.print_usage(sys.stderr)
        print(f"sincrogit: error: {conflict}", file=sys.stderr)
        return 2

    if args.tray:
        return _run_tray(args.config)

    # Every other mode needs a config.
    cfg_path = os.path.abspath(args.config) if args.config else find_config()
    if cfg_path is None:
        print(
            "No config.yaml found. Launch SincroGit once (GUI) to create one, "
            "or pass --config PATH.",
            file=sys.stderr,
        )
        return 2
    try:
        config = load_config(cfg_path)
    except (FileNotFoundError, ValueError) as e:
        print(f"Configuration error: {e}", file=sys.stderr)
        return 2

    logger = setup_logging(config.log.file, config.log.level)

    if args.doctor:
        # Read-only diagnostics (ls-remote + push --dry-run transfer nothing),
        # so it's safe alongside a running daemon — no _daemon_running guard.
        from .doctor import run_doctor
        return run_doctor(config)

    if args.autostart is not None:
        # Registry-only: safe alongside a running daemon. The config was loaded
        # above on purpose — the registered command embeds cfg_path, and
        # registering a broken config for every future logon deserves the
        # up-front error instead.
        from . import autostart
        ok, msg = autostart.set_autostart(args.autostart == "on", cfg_path)
        print(msg if ok else f"Failed: {msg}",
              file=sys.stdout if ok else sys.stderr)
        return 0 if ok else 1

    # One-shots run their own Engine on the same repos: against a live daemon the
    # two processes would race git (amend vs. amend, TOCTOU over index.lock).
    # Refuse by default; --force overrides (e.g. you know the repos are paused).
    one_shot = bool(args.history or args.autosnaps or args.commit or args.apply_handoff
                    or args.snapshot_once or args.seal_once or args.sync_once)
    if one_shot and not args.force and _daemon_running():
        print(
            "A SincroGit daemon is already running; this one-shot would race its git "
            "work on the same repos. Use the control panel / tray actions instead, "
            "quit or pause the daemon, or re-run with --force.",
            file=sys.stderr,
        )
        return 2

    if args.history:
        engine = Engine(config)
        engine.setup(with_watcher=False)
        return _history_command(engine, args.history, args.pick)

    if args.autosnaps:
        engine = Engine(config)
        engine.setup(with_watcher=False)
        return _autosnaps_command(engine)

    if args.commit:
        engine = Engine(config)
        engine.setup(with_watcher=False)
        return _commit_command(engine, args.commit, args.message, args.yes)

    if args.apply_handoff:
        engine = Engine(config)
        engine.setup(with_watcher=False)
        return _apply_handoff_command(engine, args.apply_handoff)

    if args.snapshot_once or args.seal_once or args.sync_once:
        engine = Engine(config)
        engine.setup(with_watcher=False)
        if not engine.states:
            logger.error("No valid repos.")
            return 1
        if args.snapshot_once:
            engine.snapshot_all_now()
        if args.seal_once:
            engine.seal_all_now()
        if args.sync_once:
            engine.sync_all_now()
        return 0

    if args.headless:
        engine_ref = {"engine": None}  # filled by _run_headless; read by the listener
        ok, lock = _acquire_headless_instance(lambda: engine_ref["engine"])
        if not ok:
            print(
                "SincroGit is already running (tray or headless); not starting a "
                "second instance over the same repos.",
                file=sys.stderr,
            )
            return 2
        try:
            return _run_headless(config, logger, engine_ref)
        finally:
            if lock is not None:
                try:
                    lock.close()
                except OSError:
                    pass

    # Arguments given but no action: don't silently pop a GUI from a terminal.
    parser.print_usage(sys.stderr)
    print("No command given. Use --tray for the GUI, or a command "
          "(--snapshot-once, --history, ...).", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
