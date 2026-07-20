"""events.py: in-memory ring, JSONL persistence, rotation, corrupt-line skip."""

from sincrogit.events import Event, EventLog


def test_add_and_recent_respects_capacity():
    """recent() returns the newest events; the ring drops the oldest at capacity."""
    log = EventLog(capacity=3)
    for i in range(5):
        log.add("r", "snapshot", "msg %d" % i)
    msgs = [e.message for e in log.recent()]
    assert msgs == ["msg 2", "msg 3", "msg 4"]
    assert [e.message for e in log.recent(limit=2)] == ["msg 3", "msg 4"]


def test_add_returns_event_with_fields():
    """add() returns the Event with the fields it stored."""
    log = EventLog()
    ev = log.add("repoA", "seal", "done", level="WARNING")
    assert isinstance(ev, Event)
    assert (ev.repo, ev.action, ev.message, ev.level) == ("repoA", "seal", "done", "WARNING")
    assert ev.ts > 0


def test_jsonl_persistence_and_load_all(tmp_path):
    """Events are appended to the JSONL and load_all reads them back in order."""
    path = str(tmp_path / "events.jsonl")
    log = EventLog(jsonl_path=path)
    log.add("r1", "snapshot", "one")
    log.add("r2", "push", "two")
    # A fresh EventLog over the same file loads the full on-disk history.
    reloaded = EventLog(jsonl_path=path)
    events = reloaded.load_all()
    assert [e.message for e in events] == ["one", "two"]
    assert [e.repo for e in events] == ["r1", "r2"]


def test_load_all_skips_corrupt_lines(tmp_path):
    """Malformed / non-JSON lines are skipped, not fatal."""
    path = str(tmp_path / "events.jsonl")
    log = EventLog(jsonl_path=path)
    log.add("r1", "snapshot", "good1")
    log.add("r1", "snapshot", "good2")
    # Inject junk: a blank line, non-JSON, and a JSON object missing fields.
    with open(path, "a", encoding="utf-8") as fh:
        fh.write("\n")
        fh.write("this is not json\n")
        fh.write('{"unexpected": "keys"}\n')
    events = EventLog(jsonl_path=path).load_all()
    assert [e.message for e in events] == ["good1", "good2"]


def test_load_all_without_a_jsonl_path():
    """Memory-only log (tests, dry runs): load_all falls back to the buffer
    instead of TypeError-ing on `None + ".1"`."""
    log = EventLog(None)
    log.add("r", "seal", "hello")
    assert [e.message for e in log.load_all()] == ["hello"]


def test_repos_seen_sorted_unique():
    """repos_seen returns the distinct non-empty repo names, sorted."""
    log = EventLog()
    log.add("beta", "snapshot", "x")
    log.add("alpha", "snapshot", "x")
    log.add("beta", "push", "x")
    log.add("", "startup", "global")  # empty repo excluded
    assert log.repos_seen() == ["alpha", "beta"]


def test_rotation_reads_backup_then_current_in_order(tmp_path):
    """After ONE rotation, load_all returns the .1 backup first, then the fresh
    current file, so no history is lost across a single rotation.

    NOTE: sized for EXACTLY one rotation on purpose (cap 250 B ~= 3 lines, then 2
    more written). Forcing many rotations here would trip TWO source limitations
    (see the module report): (1) only one '.1' backup is kept, so os.replace
    clobbers the middle history; (2) a rotation that lands on the final write
    leaves no current file, and load_all()'s existence guard then ignores the
    surviving '.1' entirely. This test deliberately stays inside the safe case."""
    path = str(tmp_path / "events.jsonl")
    log = EventLog(jsonl_path=path)
    log.MAX_JSONL_BYTES = 250  # ~2.5 lines: rotate once, keep the current file non-empty

    total = 5
    for i in range(total):
        log.add("r", "snapshot", "m%02d" % i)

    import os
    assert os.path.exists(path + ".1"), "a rotation should have produced a .1 backup"
    assert os.path.exists(path), "the current file should be non-empty after the rotation"

    events = EventLog(jsonl_path=path).load_all()
    assert [e.message for e in events] == ["m%02d" % i for i in range(total)]
