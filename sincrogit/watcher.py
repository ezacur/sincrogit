"""Filesystem watching (watchdog).

The watcher ONLY marks the repo as "dirty"; all git work is done by the Engine's
main thread. This avoids concurrent access to git. See §7 of DESIGN.md.

`watchdog` is imported lazily so the rest of the package (config, git, filter)
can be used/tested without watchdog installed.
"""

import logging
import os

log = logging.getLogger("sincrogit.watcher")


def _is_git_internal(path: str) -> bool:
    """Is the path inside a .git directory? (events to ignore)."""
    parts = path.replace("\\", "/").split("/")
    return ".git" in parts


def _make_handler_class():
    from watchdog.events import FileSystemEventHandler

    class _RepoEventHandler(FileSystemEventHandler):
        def __init__(self, on_change, root, ignore=None):
            self._on_change = on_change
            self._root = root
            self._ignore = ignore  # callable(relpath) -> bool, or None

        def _ignored(self, path: str) -> bool:
            if not path:
                return False
            if _is_git_internal(path):
                return True
            if self._ignore is None:
                return False
            try:
                rel = os.path.relpath(path, self._root)
            except ValueError:  # different drive on Windows
                return False
            return self._ignore(rel)

        def on_any_event(self, event):
            # A plain directory *modification* is pure mtime churn — it fires on
            # the parent of every file write and adds nothing over that file's own
            # event, so drop it. But a directory MOVE / CREATE / DELETE (e.g.
            # renaming a folder full of tracked files) is a real change watchdog
            # may not emit per-child events for, so let those through the filter.
            if event.is_directory and getattr(event, "event_type", "") == "modified":
                return
            src = getattr(event, "src_path", "") or ""
            dst = getattr(event, "dest_path", "") or ""
            # Drop git's internal noise AND churn under excluded folders (e.g. the
            # tens of thousands of events an `npm install` fires under node_modules)
            # before it ever wakes the engine. A move OUT of an ignored folder into a
            # tracked one is still processed (its dst isn't ignored).
            if self._ignored(src) and (not dst or self._ignored(dst)):
                return
            self._on_change()

    return _RepoEventHandler


class WatchManager:
    def __init__(self):
        from watchdog.observers import Observer

        self.observer = Observer()
        self._handler_cls = _make_handler_class()
        self._started = False

    def watch(self, path: str, on_change, ignore=None):
        handler = self._handler_cls(on_change, path, ignore)
        self.observer.schedule(handler, path, recursive=True)

    def start(self):
        self.observer.start()
        self._started = True

    def stop(self):
        if not self._started:
            return
        self.observer.stop()
        self.observer.join(timeout=5)
        self._started = False
