"""Filesystem watching (watchdog).

The watcher ONLY marks the repo as "dirty"; all git work is done by the Engine's
main thread. This avoids concurrent access to git. See §7 of DESIGN.md.

`watchdog` is imported lazily so the rest of the package (config, git, filter)
can be used/tested without watchdog installed.
"""

import logging

log = logging.getLogger("sincrogit.watcher")


def _is_git_internal(path: str) -> bool:
    """Is the path inside a .git directory? (events to ignore)."""
    parts = path.replace("\\", "/").split("/")
    return ".git" in parts


def _make_handler_class():
    from watchdog.events import FileSystemEventHandler

    class _RepoEventHandler(FileSystemEventHandler):
        def __init__(self, on_change):
            self._on_change = on_change

        def on_any_event(self, event):
            if event.is_directory:
                return
            src = getattr(event, "src_path", "") or ""
            dst = getattr(event, "dest_path", "") or ""
            # Ignore git's internal noise (index, objects, locks...).
            if _is_git_internal(src) and (not dst or _is_git_internal(dst)):
                return
            self._on_change()

    return _RepoEventHandler


class WatchManager:
    def __init__(self):
        from watchdog.observers import Observer

        self.observer = Observer()
        self._handler_cls = _make_handler_class()
        self._started = False

    def watch(self, path: str, on_change):
        handler = self._handler_cls(on_change)
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
