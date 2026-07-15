"""messages.py: the deterministic fallback commit message."""

from sincrogit.messages import build_fallback_message


def test_counts_and_labels_added_modified_deleted():
    """A/M/D map to new/modified/deleted with per-status counts in status order."""
    name_status = [
        ("A", "new1.py"), ("A", "new2.py"),
        ("M", "changed.py"),
        ("D", "gone.py"), ("D", "gone2.py"), ("D", "gone3.py"),
    ]
    title, body = build_fallback_message(name_status)
    assert title == "sincro: 6 file(s) (2 new, 1 modified, 3 deleted)"
    # Body lists each file with its status.
    assert "A  new1.py" in body
    assert "D  gone3.py" in body


def test_single_file_no_special_pluralization():
    """The counts are raw numbers; the label text itself is not pluralized."""
    title, _ = build_fallback_message([("M", "only.py")])
    assert title == "sincro: 1 file(s) (1 modified)"


def test_custom_prefix():
    """A custom prefix (e.g. 'chore' for a manual-commit fallback) replaces 'sincro'."""
    title, _ = build_fallback_message([("A", "x.py")], prefix="chore")
    assert title.startswith("chore: ")
    assert "1 new" in title


def test_empty_name_status_reports_no_changes():
    """With no entries the summary is 'no changes' and the count is zero."""
    title, body = build_fallback_message([])
    assert title == "sincro: 0 file(s) (no changes)"
    assert body == ""


def test_body_truncates_to_ten_with_more_line():
    """Only the first 10 files are listed; the rest are summarized as '... and N more'."""
    name_status = [("M", "f%02d.py" % i) for i in range(13)]
    title, body = build_fallback_message(name_status)
    assert title == "sincro: 13 file(s) (13 modified)"
    lines = body.splitlines()
    assert len(lines) == 11  # 10 files + the "... and N more" line
    assert lines[-1] == "... and 3 more"


def test_unknown_status_falls_back_to_raw_letter():
    """A status not in the known set uses the raw letter as its label."""
    title, _ = build_fallback_message([("X", "weird.py")])
    assert "1 X" in title
