"""Structured log of SincroGit actions.

Unlike the text log, here each action is an event with fields (timestamp, repo,
action, level, message) so the panel can filter by repo and by action. It is
kept in memory (for recent items) and appended to a JSONL file (for the full
history).

Thread-safe: the engine (background thread) writes; the GUI (main thread) reads.
"""

import json
import os
import threading
import time
from collections import deque
from dataclasses import asdict, dataclass

# Known actions (to populate the panel filter). Any other string is also valid;
# this is just for the dropdown list.
ACTIONS = [
    "startup",
    "snapshot",
    "seal",
    "leave-seal",  # the seal fired ~20 min after locking the machine (left for real)
    "push",
    "autosnap",
    "pull",
    "handoff",
    "flush",
    "gc",
    "repair",
    "log",        # bridged from the Python logger (e.g. DEBUG detail) — see gui/app.py
    "conflict",
    "busy",       # a manual merge/rebase is holding a repo (long-busy warning)
    "pause",
    "resume",
    "restart",    # SincroGit relaunching itself (Save and restart)
    "info",
    "error",
]


@dataclass
class Event:
    ts: float          # epoch (seconds)
    repo: str          # repo name, or "" for global events
    action: str        # one of ACTIONS (or free-form)
    level: str         # INFO | WARNING | ERROR
    message: str

    def as_dict(self) -> dict:
        return asdict(self)


class EventLog:
    # Rotate the JSONL once it grows past this (one .1 backup is kept). Keeps the
    # on-disk history bounded, so the GUI's full reload stays fast forever.
    MAX_JSONL_BYTES = 5_000_000

    def __init__(self, jsonl_path: str | None = None, capacity: int = 2000):
        self._buf: deque[Event] = deque(maxlen=capacity)
        self._lock = threading.Lock()
        self._jsonl_path = jsonl_path
        self._jsonl_bytes = 0  # tracked in-process to avoid a stat per event

        if jsonl_path:
            d = os.path.dirname(os.path.abspath(jsonl_path))
            if d:
                os.makedirs(d, exist_ok=True)
            try:
                self._jsonl_bytes = os.path.getsize(jsonl_path)
            except OSError:
                pass

    # ------------------------------------------------------------- writing
    def add(self, repo: str, action: str, message: str, level: str = "INFO") -> Event:
        ev = Event(ts=time.time(), repo=repo or "", action=action, level=level, message=message)
        with self._lock:
            self._buf.append(ev)
            if self._jsonl_path:
                try:
                    line = json.dumps(ev.as_dict(), ensure_ascii=False) + "\n"
                    with open(self._jsonl_path, "a", encoding="utf-8") as fh:
                        fh.write(line)
                        # One line, one write, then push it out of Python's buffer.
                        # Without this an unclean exit (the update restart, a power
                        # cut) leaves NTFS to zero-fill the tail of the file — which
                        # is how this log ended up with NUL bytes in it. No fsync:
                        # that would be a disk round-trip per event.
                        fh.flush()
                    self._jsonl_bytes += len(line.encode("utf-8"))
                    if self._jsonl_bytes > self.MAX_JSONL_BYTES:
                        self._rotate_jsonl()
                except OSError:
                    pass  # don't break the engine over a log write failure
        return ev

    def _rotate_jsonl(self):
        """Move the JSONL aside (one .1 backup, replaced) so it never grows
        unbounded. Caller holds self._lock."""
        try:
            os.replace(self._jsonl_path, self._jsonl_path + ".1")
        except OSError:
            return  # e.g. another handle holds the file; retried on a later add
        self._jsonl_bytes = 0

    # -------------------------------------------------------------- reading
    def recent(self, limit: int | None = None) -> list:
        with self._lock:
            items = list(self._buf)
        return items[-limit:] if limit else items

    def load_all(self) -> list:
        """Full history from the JSONL file (or whatever is in memory).

        Includes the rotated `.1` backup first (when present): after a rotation
        the current file starts empty, and without the backup the GUI's "full
        history" would silently lose everything older than the rotation point.
        """
        # Read whichever of the two files exist: right after a rotation the
        # current file is gone (os.replace moved it to .1) and ALL history lives
        # in the backup, so guarding on the current file alone would drop it.
        if not self._jsonl_path:
            return self.recent()  # in-memory only, as the docstring promises
        paths = [p for p in (self._jsonl_path + ".1", self._jsonl_path)
                 if os.path.exists(p)]
        if not paths:
            return self.recent()
        out = []
        for path in paths:
            try:
                with open(path, "r", encoding="utf-8") as fh:
                    for line in fh:
                        # NUL bytes appear when an unclean exit leaves NTFS to
                        # zero-fill the tail; strip them so one damaged record
                        # can't swallow the readable ones around it.
                        line = line.replace("\x00", "").strip()
                        if not line:
                            continue
                        try:
                            out.append(Event(**json.loads(line)))
                        except (json.JSONDecodeError, TypeError):
                            continue
            except OSError:
                continue  # no backup yet / current file unreadable: keep what we have
        return out or self.recent()

    def repos_seen(self) -> list:
        with self._lock:
            return sorted({ev.repo for ev in self._buf if ev.repo})
