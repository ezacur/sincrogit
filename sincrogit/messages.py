"""Commit message generation when sealing.

Phase 1: deterministic fallback message only (from name-status).
Phase 2: adds the AI generator (Ollama / Gemini), which ALWAYS falls back to this
when it fails. See §6 of DESIGN.md.
"""

_STATUS_LABEL = {
    "A": "new",
    "M": "modified",
    "D": "deleted",
    "R": "renamed",
    "C": "copied",
    "T": "type-changed",
}
_STATUS_ORDER = ["A", "M", "D", "R", "C", "T"]


def build_fallback_message(name_status: list, prefix: str = "sincro") -> tuple:
    """Return (title, body) from [(status, path), ...].

    `prefix` is the commit-type prefix: 'sincro' for automatic seals (so machine
    commits are easy to tell apart from human ones), or e.g. 'chore' for the
    fallback of a manual commit (which must NOT look like a machine commit).
    """
    counts = {}
    for status, _ in name_status:
        counts[status] = counts.get(status, 0) + 1

    total = len(name_status)
    parts = [
        f"{counts[s]} {_STATUS_LABEL.get(s, s)}"
        for s in _STATUS_ORDER
        if counts.get(s)
    ]
    # Any status not covered above.
    for s, n in counts.items():
        if s not in _STATUS_ORDER:
            parts.append(f"{n} {_STATUS_LABEL.get(s, s)}")

    summary = ", ".join(parts) if parts else "no changes"
    title = f"{prefix}: {total} file(s) ({summary})"

    sample = name_status[:10]
    body_lines = [f"{status}  {path}" for status, path in sample]
    if total > len(sample):
        body_lines.append(f"... and {total - len(sample)} more")
    body = "\n".join(body_lines)

    return title, body
