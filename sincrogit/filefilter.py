"""File filter: only TEXT and < 1 MB is versioned automatically.

Everything else (binaries, large files) is handled by hand. The filter lives
here (in the `git add` logic), NOT in .gitignore, so a binary can still be
committed manually. See §5 of DESIGN.md.
"""

import logging
import os

log = logging.getLogger("sincrogit.filter")

# How many bytes to read to decide whether a file is text.
_TEXT_SNIFF_BYTES = 8192

try:
    import pathspec  # type: ignore
except ImportError:  # pragma: no cover
    pathspec = None


class FileFilter:
    """Decides whether a file should be included in the automatic commit."""

    def __init__(self, max_bytes: int, excludes: list):
        self.max_bytes = max_bytes
        self.excludes = excludes or []
        self._spec = None
        if self.excludes:
            if pathspec is not None:
                self._spec = pathspec.PathSpec.from_lines(
                    "gitwildmatch", self.excludes
                )
            else:
                log.warning(
                    "There are 'extra_excludes' in the config but the 'pathspec' "
                    "package is not installed; exclusions will be ignored."
                )

    def accept(self, abspath: str, relpath: str) -> bool:
        return self.reason_to_skip(abspath, relpath) is None

    def reason_to_skip(self, abspath: str, relpath: str) -> str | None:
        """Why this file is NOT auto-versioned, or None if it is accepted.

        Returns a short human-readable reason ("excluded" / "too large (...)" /
        "binary" / "unreadable") so callers can surface why a tracked file stopped
        being snapshotted. `accept()` is just `reason_to_skip(...) is None`.
        """
        rel = relpath.replace(os.sep, "/")

        if self._spec is not None and self._spec.match_file(rel):
            return "excluded"

        try:
            size = os.path.getsize(abspath)
        except OSError:
            return "unreadable"
        if size > self.max_bytes:
            return f"too large ({size} > {self.max_bytes} bytes)"

        if not self._is_text(abspath):
            return "binary"
        return None

    @staticmethod
    def _is_text(abspath: str) -> bool:
        """Standard heuristic (like git): binary if there's a NUL byte near the start."""
        try:
            with open(abspath, "rb") as fh:
                chunk = fh.read(_TEXT_SNIFF_BYTES)
        except OSError:
            return False
        return b"\x00" not in chunk
