"""watcher.py: the event-filtering handler (no real Observer / filesystem)."""

import os

import pytest

from sincrogit.watcher import _make_handler_class


class FakeEvent:
    """A stand-in for a watchdog event (only the attributes the handler reads)."""

    def __init__(self, src_path="", event_type="modified", is_directory=False,
                 dest_path=None):
        self.src_path = src_path
        self.event_type = event_type
        self.is_directory = is_directory
        if dest_path is not None:  # only moves carry a dest_path
            self.dest_path = dest_path


ROOT = os.path.join("C:", os.sep, "proj") if os.name == "nt" else "/proj"


def _handler(ignore=None):
    """Build a handler over ROOT with a counter recording on_change() calls."""
    calls = []
    cls = _make_handler_class()
    h = cls(lambda: calls.append(1), ROOT, ignore=ignore)
    return h, calls


def _p(*parts):
    return os.path.join(ROOT, *parts)


def test_directory_modified_is_dropped():
    """A directory 'modified' event is pure mtime churn and is discarded."""
    h, calls = _handler()
    h.on_any_event(FakeEvent(src_path=_p("sub"), event_type="modified", is_directory=True))
    assert calls == []


def test_directory_create_is_processed():
    """A directory CREATE/DELETE/MOVE is a real change and passes the filter."""
    h, calls = _handler()
    h.on_any_event(FakeEvent(src_path=_p("newdir"), event_type="created", is_directory=True))
    assert calls == [1]


def test_git_internal_path_is_dropped():
    """An event under .git is git's own noise and is discarded."""
    h, calls = _handler()
    h.on_any_event(FakeEvent(src_path=_p(".git", "index"), event_type="modified"))
    assert calls == []


def test_ignored_relpath_is_dropped():
    """A path the `ignore` callback rejects (e.g. node_modules) is discarded."""
    def ignore(rel):
        return rel.replace(os.sep, "/").startswith("node_modules/")
    h, calls = _handler(ignore=ignore)
    h.on_any_event(FakeEvent(src_path=_p("node_modules", "react", "x.js")))
    assert calls == []


def test_normal_event_fires_once():
    """A normal file event triggers on_change exactly once."""
    def ignore(rel):
        return rel.replace(os.sep, "/").startswith("node_modules/")
    h, calls = _handler(ignore=ignore)
    h.on_any_event(FakeEvent(src_path=_p("src", "app.py"), event_type="modified"))
    assert calls == [1]


def test_move_out_of_ignored_folder_into_tracked_fires():
    """A move FROM an ignored folder TO a tracked path is still processed (dst not ignored)."""
    def ignore(rel):
        return rel.replace(os.sep, "/").startswith("node_modules/")
    h, calls = _handler(ignore=ignore)
    h.on_any_event(FakeEvent(
        src_path=_p("node_modules", "pkg", "thing.js"),
        dest_path=_p("src", "thing.js"),
        event_type="moved",
    ))
    assert calls == [1]


def test_move_within_ignored_folder_is_dropped():
    """A move whose src AND dst are both ignored is discarded."""
    def ignore(rel):
        return rel.replace(os.sep, "/").startswith("node_modules/")
    h, calls = _handler(ignore=ignore)
    h.on_any_event(FakeEvent(
        src_path=_p("node_modules", "a", "x.js"),
        dest_path=_p("node_modules", "b", "x.js"),
        event_type="moved",
    ))
    assert calls == []


def test_no_ignore_callback_passes_normal_events():
    """With ignore=None only .git and dir-modified are filtered; the rest pass."""
    h, calls = _handler(ignore=None)
    h.on_any_event(FakeEvent(src_path=_p("deep", "nested", "file.txt")))
    assert calls == [1]
