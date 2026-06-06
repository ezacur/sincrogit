"""Generate app.ico from the SincroGit vector icon (the "G" + hourglass).

Renders several sizes offscreen with PyQt5 and writes a multi-resolution .ico.
Run:  python tools/make_icon.py [output.ico]
"""

import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt5.QtCore import QBuffer, QIODevice  # noqa: E402
from PyQt5.QtWidgets import QApplication  # noqa: E402

# Make 'sincrogit' importable when run from the repo root.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from sincrogit.gui import icon as iconmod  # noqa: E402

_SIZES = [16, 24, 32, 48, 64, 128, 256]


def main(out_path: str = "app.ico") -> int:
    app = QApplication([])  # noqa: F841 — needed before painting QPixmaps

    try:
        from PIL import Image
    except ImportError:
        print("Pillow is required to write .ico (pip install pillow)", file=sys.stderr)
        return 2

    images = []
    for size in _SIZES:
        pm = iconmod.make_pixmap("running", size)
        buf = QBuffer()
        buf.open(QIODevice.ReadWrite)
        pm.save(buf, "PNG")
        from io import BytesIO
        images.append(Image.open(BytesIO(bytes(buf.data()))).convert("RGBA"))
        buf.close()

    # Pillow writes a multi-size .ico from the largest image + a size list.
    largest = max(images, key=lambda im: im.size[0])
    largest.save(out_path, format="ICO", sizes=[(s, s) for s in _SIZES])
    print(f"Wrote {out_path} ({', '.join(str(s) for s in _SIZES)} px)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else "app.ico"))
