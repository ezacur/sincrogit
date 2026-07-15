"""File filter: only TEXT and < 1 MB is versioned automatically.

Everything else (binaries, large files) is handled by hand. The filter lives
here (in the `git add` logic), NOT in .gitignore, so a binary can still be
committed manually. See §5 of DESIGN.md.
"""

import logging
import os

log = logging.getLogger("sincrogit.filter")

# How many bytes to inspect to decide text vs binary. Generous on purpose: the
# size filter already guaranteed the file is <= max_bytes by the time we get
# here, so we can afford to scan a large prefix (1 MiB) instead of a tiny sample
# — a binary whose first NUL byte appears late is no longer mistaken for text.
_TEXT_SNIFF_BYTES = 1 << 20            # 1 MiB
_RATIO_SAMPLE = 1 << 16                # cap the per-byte control-ratio scan at 64 KiB
_MAX_CONTROL_RATIO = 0.10              # >10% control bytes (non-UTF-8) => treat as binary

# UTF-16/UTF-32 text starts with a BOM and *does* contain NUL bytes, so it must be
# detected before the NUL test or it would be misread as binary.
_TEXT_BOMS = (
    b"\x00\x00\xfe\xff",  # UTF-32 BE
    b"\xff\xfe\x00\x00",  # UTF-32 LE
    b"\xfe\xff",          # UTF-16 BE
    b"\xff\xfe",          # UTF-16 LE
    b"\xef\xbb\xbf",      # UTF-8 with BOM
)

# "Text" bytes: the usual whitespace + printable ASCII + every high byte (legacy
# 8-bit text and UTF-8 multibyte sequences). Everything else (other control bytes
# and DEL 0x7F) counts as "control". Used with bytes.translate() to count control
# bytes at C speed (a Python per-byte loop is too slow on a big initial scan).
_TEXT_BYTES = bytes(
    [0x09, 0x0A, 0x0B, 0x0C, 0x0D]      # tab, LF, VT, FF, CR
    + list(range(0x20, 0x7F))           # printable ASCII (0x20..0x7E)
    + list(range(0x80, 0x100))          # high bytes (Latin-1 / UTF-8 multibyte)
)

try:
    import pathspec  # type: ignore
except ImportError:  # pragma: no cover
    pathspec = None


class FileFilter:
    """Decides whether a file should be included in the automatic commit."""

    def __init__(self, max_bytes: int, excludes: list,
                 includes: list | None = None, max_include_bytes: int | None = None):
        self.max_bytes = max_bytes
        self.excludes = excludes or []
        self.includes = includes or []
        # `is None` (not truthiness): a legitimate 0 ("include no oversized file")
        # must be kept, not silently replaced by max_bytes.
        self.max_include_bytes = max_include_bytes if max_include_bytes is not None else max_bytes
        self._spec = self._compile(self.excludes, "extra_excludes")
        self._include_spec = self._compile(self.includes, "extra_includes")

    @staticmethod
    def _compile(patterns: list, label: str):
        if not patterns:
            return None
        if pathspec is None:
            log.warning(
                "There are '%s' in the config but the 'pathspec' package is not "
                "installed; they will be ignored.", label,
            )
            return None
        return pathspec.PathSpec.from_lines("gitwildmatch", patterns)

    def accept(self, abspath: str, relpath: str) -> bool:
        return self.reason_to_skip(abspath, relpath) is None

    def is_excluded(self, relpath: str) -> bool:
        """Cheap exclude-only check (no size/binary I/O). Used by the watcher to
        drop filesystem events under excluded folders (e.g. node_modules during an
        `npm install`) before they ever wake the engine — see §5/§11 of DESIGN.md."""
        if self._spec is None:
            return False
        return self._spec.match_file(relpath.replace(os.sep, "/"))

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

        # Explicitly-included patterns (e.g. "**/*.docx") are versioned even if
        # binary, under a separate (larger) size cap. Excludes still win (checked
        # above), so an included file inside an excluded folder stays excluded.
        if self._include_spec is not None and self._include_spec.match_file(rel):
            if size > self.max_include_bytes:
                return f"too large ({size} > {self.max_include_bytes} bytes)"
            return None

        if size > self.max_bytes:
            return f"too large ({size} > {self.max_bytes} bytes)"

        if not self._is_text(abspath):
            return "binary"
        return None

    @staticmethod
    def _is_text(abspath: str) -> bool:
        """Heuristic for "a human reading this flat would find it makes sense".

        Layered, from strongest signal to weakest (see §5 of DESIGN.md):
          - empty file                -> text
          - starts with a Unicode BOM -> text (UTF-8/16/32)
          - contains a NUL byte       -> binary
          - else: decide by the proportion of control bytes — real text (ASCII,
            UTF-8, Latin-1, ...) has very few; binary/garbage has many. UTF-8
            multibyte chars are bytes >= 0x80, not control bytes, so this keeps
            accented/emoji/CJK text while rejecting control-byte spam.
        """
        try:
            with open(abspath, "rb") as fh:
                chunk = fh.read(_TEXT_SNIFF_BYTES)
        except OSError:
            return False

        if not chunk:
            return True
        if chunk.startswith(_TEXT_BOMS):
            return True
        if b"\x00" in chunk:
            return False
        sample = chunk[:_RATIO_SAMPLE]
        # Count control bytes at C speed: delete all "text" bytes, what's left is
        # the control bytes.
        control = len(sample.translate(None, _TEXT_BYTES))
        return control / len(sample) <= _MAX_CONTROL_RATIO
