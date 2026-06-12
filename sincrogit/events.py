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
    "push",
    "autosnap",
    "pull",
    "handoff",
    "flush",
    "gc",
    "repair",
    "log",        # bridged from the Python logger (e.g. DEBUG detail) — see gui/app.py
    "conflict",
    "pause",
    "resume",
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
        self._listeners = []  # callables(Event) -> None

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
                    self._jsonl_bytes += len(line.encode("utf-8"))
                    if self._jsonl_bytes > self.MAX_JSONL_BYTES:
                        self._rotate_jsonl()
                except OSError:
                    pass  # don't break the engine over a log write failure
        for cb in list(self._listeners):
            try:
                cb(ev)
            except Exception:  # noqa: BLE001 — a listener must not take down the engine
                pass
        return ev

    def _rotate_jsonl(self):
        """Move the JSONL aside (one .1 backup, replaced) so it never grows
        unbounded. Caller holds self._lock."""
        try:
            os.replace(self._jsonl_path, self._jsonl_path + ".1")
        except OSError:
            return  # e.g. another handle holds the file; retried on a later add
        self._jsonl_bytes = 0

    def add_listener(self, callback) -> None:
        """Register a callback invoked with each new Event (on the thread that
        calls add(), normally the engine's)."""
        self._listeners.append(callback)

    # -------------------------------------------------------------- reading
    def recent(self, limit: int | None = None) -> list:
        with self._lock:
            items = list(self._buf)
        return items[-limit:] if limit else items

    def load_all(self) -> list:
        """Full history from the JSONL file (or whatever is in memory)."""
        if not self._jsonl_path or not os.path.exists(self._jsonl_path):
            return self.recent()
        out = []
        try:
            with open(self._jsonl_path, "r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        out.append(Event(**json.loads(line)))
                    except (json.JSONDecodeError, TypeError):
                        continue
        except OSError:
            return self.recent()
        return out

    def repos_seen(self) -> list:
        with self._lock:
            return sorted({ev.repo for ev in self._buf if ev.repo})
