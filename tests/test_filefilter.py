"""filefilter.py: the text/binary/size/exclude/include heuristics."""

import os

from sincrogit.filefilter import FileFilter


def _write(tmp_path, relpath, content):
    """Write bytes/str to tmp_path/relpath, return (abspath, relpath)."""
    p = tmp_path / relpath
    p.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(content, bytes):
        p.write_bytes(content)
    else:
        p.write_text(content, encoding="utf-8")
    return str(p), relpath


# ---------------------------------------------------------------- text vs binary
def test_empty_file_is_text(tmp_path):
    """An empty file counts as text (accepted)."""
    f = FileFilter(max_bytes=1_000_000, excludes=[])
    assert f.accept(*_write(tmp_path, "empty.txt", b""))


def test_utf8_accents_and_emoji_is_text(tmp_path):
    """UTF-8 with accents and an emoji is text (high bytes are not control)."""
    f = FileFilter(max_bytes=1_000_000, excludes=[])
    assert f.accept(*_write(tmp_path, "u8.txt", "camión café niño 🚀 日本語\n"))


def test_utf16_le_bom_is_text(tmp_path):
    """UTF-16 LE (with BOM) is text despite embedded NUL bytes."""
    f = FileFilter(max_bytes=1_000_000, excludes=[])
    data = "hola mundo".encode("utf-16-le")
    assert data.count(b"\x00")  # sanity: it really does contain NULs
    assert f.accept(*_write(tmp_path, "u16.txt", b"\xff\xfe" + data))


def test_nul_without_bom_is_binary(tmp_path):
    """A NUL byte without a leading BOM marks the file binary."""
    f = FileFilter(max_bytes=1_000_000, excludes=[])
    assert f.reason_to_skip(*_write(tmp_path, "b.bin", b"abc\x00def")) == "binary"


def test_high_control_ratio_is_binary(tmp_path):
    """More than 10% control bytes (no NUL) is treated as binary."""
    f = FileFilter(max_bytes=1_000_000, excludes=[])
    # 100 bytes: 20 control (0x01) + 80 printable => 20% control, no NUL.
    data = b"\x01" * 20 + b"A" * 80
    assert f.reason_to_skip(*_write(tmp_path, "noise.dat", data)) == "binary"


def test_low_control_ratio_is_text(tmp_path):
    """Below the 10% control-byte threshold stays text."""
    f = FileFilter(max_bytes=1_000_000, excludes=[])
    data = b"\x01" * 5 + b"A" * 95  # 5% control, no NUL
    assert f.accept(*_write(tmp_path, "ok.dat", data))


# ------------------------------------------------------------------- size limits
def test_too_large_text_rejected(tmp_path):
    """A text file above max_file_bytes is skipped with a 'too large' reason."""
    f = FileFilter(max_bytes=10, excludes=[])
    reason = f.reason_to_skip(*_write(tmp_path, "big.txt", "x" * 50))
    assert reason.startswith("too large")


def test_include_accepts_binary_under_include_cap(tmp_path):
    """An included binary (e.g. **/*.docx) is versioned under max_include_bytes."""
    f = FileFilter(max_bytes=10, excludes=[], includes=["**/*.docx"],
                   max_include_bytes=1_000_000)
    # Binary (NUL) and larger than max_bytes, but an included pattern with a big cap.
    abspath, rel = _write(tmp_path, "doc.docx", b"PK\x00\x00binary" + b"x" * 100)
    assert f.accept(abspath, rel)


def test_include_respects_its_own_size_cap(tmp_path):
    """An included file over max_include_bytes is still 'too large'."""
    f = FileFilter(max_bytes=10, excludes=[], includes=["**/*.docx"],
                   max_include_bytes=20)
    reason = f.reason_to_skip(*_write(tmp_path, "doc.docx", b"\x00" * 50))
    assert reason.startswith("too large")


def test_max_include_bytes_zero_is_respected(tmp_path):
    """max_include_bytes=0 is kept (not replaced by max_bytes): everything included is too large."""
    f = FileFilter(max_bytes=1_000_000, excludes=[], includes=["**/*.docx"],
                   max_include_bytes=0)
    assert f.max_include_bytes == 0
    reason = f.reason_to_skip(*_write(tmp_path, "doc.docx", b"\x00\x00"))
    assert reason.startswith("too large")


# -------------------------------------------------------------------- precedence
def test_excluded_beats_include(tmp_path):
    """An included file inside an excluded folder stays excluded (excludes win)."""
    f = FileFilter(max_bytes=1_000_000, excludes=["**/build/**"],
                   includes=["**/*.docx"], max_include_bytes=1_000_000)
    reason = f.reason_to_skip(*_write(tmp_path, "build/doc.docx", b"\x00\x00data"))
    assert reason == "excluded"


def test_reason_to_skip_excluded(tmp_path):
    """A plain excluded path reports 'excluded'."""
    f = FileFilter(max_bytes=1_000_000, excludes=["**/node_modules/**"])
    reason = f.reason_to_skip(*_write(tmp_path, "node_modules/x.js", "code\n"))
    assert reason == "excluded"


def test_reason_to_skip_binary(tmp_path):
    """A binary, non-excluded, small file reports 'binary'."""
    f = FileFilter(max_bytes=1_000_000, excludes=[])
    assert f.reason_to_skip(*_write(tmp_path, "x.bin", b"\x00\x01\x02")) == "binary"


# --------------------------------------------------------------------- is_excluded
def test_is_excluded_cheap_check(tmp_path):
    """is_excluded matches excluded folders without touching the filesystem."""
    f = FileFilter(max_bytes=1_000_000, excludes=["**/node_modules/**"])
    assert f.is_excluded("node_modules/react/index.js")
    assert not f.is_excluded("src/app.py")


def test_is_excluded_windows_separators(tmp_path):
    """is_excluded normalizes Windows backslash separators before matching."""
    f = FileFilter(max_bytes=1_000_000, excludes=["**/dist/**"])
    assert f.is_excluded("a" + os.sep + "dist" + os.sep + "bundle.js")


def test_is_excluded_no_excludes_is_false(tmp_path):
    """With no excludes configured, is_excluded is always False (no spec)."""
    f = FileFilter(max_bytes=1_000_000, excludes=[])
    assert f.is_excluded("anything/at/all.txt") is False
