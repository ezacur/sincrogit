"""Entry point: `python -m sincrogit` / the standalone SincroGit.exe.

Launch model:
  (no arguments)            -> GUI tray app + daemon (single instance)
  --tray [--config X]       -> GUI tray app + daemon (single instance)
  --headless [--config X]   -> daemon without GUI (servers / automation)
  --snapshot-once|--seal-once|--sync-once   -> CLI one-shot and exit
  --history FILE [--pick N] -> browse/restore a file's version history

With no arguments the GUI launches; if an instance is already running, the new
launch just asks the running one to show its panel and exits. Any argument is
treated as a command-line invocation (output goes to the launching terminal).
"""

import argparse
import os
import signal
import sys
from datetime import datetime

from .config import load_config
from .engine import Engine
from .log import setup_logging
from .runtime import (
    acquire_single_instance,
    attach_parent_console,
    ensure_config,
    find_config,
    signal_existing_instance,
)


def _run_tray(explicit_config) -> int:
    """Launch the GUI/daemon as a single instance."""
    lock = acquire_single_instance()
    if lock is None:
        signal_existing_instance()  # ask the running instance to show its panel
        print("SincroGit is already running.", file=sys.stderr)
        return 0
    cfg_path, created = ensure_config(explicit_config)
    try:
        from .gui.app import main as tray_main
    except ImportError as e:
        print(f"The GUI needs PyQt5 (pip install PyQt5): {e}", file=sys.stderr)
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
        kind = "sealed  " if v["source"] == "sealed" else "snapshot"
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


def _run_headless(config, logger) -> int:
    engine = Engine(config)

    def _handle(signum, _frame):
        logger.info("Signal %s received, shutting down...", signum)
        engine.stop()

    signal.signal(signal.SIGINT, _handle)
    try:
        signal.signal(signal.SIGTERM, _handle)
    except (ValueError, AttributeError):
        pass

    engine.run()
    return 0


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
    args = parser.parse_args(argv)

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

    if args.history:
        engine = Engine(config)
        engine.setup(with_watcher=False)
        return _history_command(engine, args.history, args.pick)

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
        return _run_headless(config, logger)

    # Arguments given but no action: don't silently pop a GUI from a terminal.
    parser.print_usage(sys.stderr)
    print("No command given. Use --tray for the GUI, or a command "
          "(--snapshot-once, --history, ...).", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
