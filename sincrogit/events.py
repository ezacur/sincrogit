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
    "pull",
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
    def __init__(self, jsonl_path: str | None = None, capacity: int = 2000):
        self._buf: deque[Event] = deque(maxlen=capacity)
        self._lock = threading.Lock()
        self._jsonl_path = jsonl_path
        self._listeners = []  # callables(Event) -> None

        if jsonl_path:
            d = os.path.dirname(os.path.abspath(jsonl_path))
            if d:
                os.makedirs(d, exist_ok=True)

    # ------------------------------------------------------------- writing
    def add(self, repo: str, action: str, message: str, level: str = "INFO") -> Event:
        ev = Event(ts=time.time(), repo=repo or "", action=action, level=level, message=message)
        with self._lock:
            self._buf.append(ev)
            if self._jsonl_path:
                try:
                    with open(self._jsonl_path, "a", encoding="utf-8") as fh:
                        fh.write(json.dumps(ev.as_dict(), ensure_ascii=False) + "\n")
                except OSError:
                    pass  # don't break the engine over a log write failure
        for cb in list(self._listeners):
            try:
                cb(ev)
            except Exception:  # noqa: BLE001 — a listener must not take down the engine
                pass
        return ev

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
