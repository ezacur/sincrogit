"""SincroGit icon: the ⏳g brand mark — an hourglass and a lowercase git "g".

The product mark is pure unicode ("⏳g", typable anywhere); this draws the same
two glyphs with QPainter (vectorial, crisp at any size) so the icon needs no
font or image asset. The background color changes with the state, so a glance
at the tray tells you whether it is active, paused, in conflict or stopped.

Requires a QApplication to exist (gui/app.py creates it before painting).
"""

from PyQt5.QtCore import QPointF, QRectF, Qt
from PyQt5.QtGui import (
    QBrush,
    QColor,
    QIcon,
    QLinearGradient,
    QPainter,
    QPainterPath,
    QPen,
    QPixmap,
    QPolygonF,
)

# Background color per state.
STATE_COLORS = {
    "running": "#2E9E5B",   # green: working
    "paused": "#E0A400",    # amber: paused by the user
    # Orange-red, deliberately distinct from `conflict`: the snapshots ARE
    # running (nothing is paused), but the copy that leaves the machine is not
    # landing — a green icon there would be a lie.
    "attention": "#C2410C",
    "conflict": "#D23F3F",  # red: conflict, needs attention
    "stopped": "#7A7F87",   # gray: stopped / idle
}

STATE_TOOLTIP = {
    "running": "⏳g SincroGit: active",
    "paused": "⏳g SincroGit: paused",
    "attention": "⏳g SincroGit: your work is NOT reaching the remote (open the panel)",
    "conflict": "⏳g SincroGit: conflict (needs attention)",
    "stopped": "⏳g SincroGit: stopped",
}


def _draw(painter: QPainter, size: int, color_hex: str):
    painter.setRenderHint(QPainter.Antialiasing, True)
    painter.setRenderHint(QPainter.TextAntialiasing, True)

    base = QColor(color_hex)
    light = base.lighter(118)
    dark = base.darker(118)

    # --- Rounded background with a slight vertical gradient ---
    margin = size * 0.06
    rect = QRectF(margin, margin, size - 2 * margin, size - 2 * margin)
    radius = size * 0.22
    grad = QLinearGradient(0, rect.top(), 0, rect.bottom())
    grad.setColorAt(0.0, light)
    grad.setColorAt(1.0, dark)
    path = QPainterPath()
    path.addRoundedRect(rect, radius, radius)
    painter.fillPath(path, QBrush(grad))
    painter.setPen(QPen(dark.darker(120), max(1.0, size * 0.02)))
    painter.drawPath(path)

    # --- The mark, side by side like the text: hourglass ⏳ then lowercase g ---
    _draw_hourglass(painter, size)
    _draw_g(painter, size)


def _draw_hourglass(painter: QPainter, size: int):
    """The ⏳ half of the mark (left). What makes the glyph read "hourglass" (and
    not "X") is the OUTLINED glass with the sand inside: stroked bulbs with a real
    neck, sand left in the top bulb, a falling stream, and the pile below."""
    cx = size * 0.335
    cy = size * 0.50
    gw = size * 0.26   # glass (bulb) width
    hh = size * 0.47   # glass height
    neck = gw * 0.18   # waist half-gap
    left = cx - gw / 2
    right = cx + gw / 2
    top = cy - hh / 2
    bot = cy + hh / 2

    white = QColor("#FFFFFF")

    # Glass outline (stroked, NOT filled): two edges narrowing to a neck.
    sw = max(1.2, size * 0.034)
    painter.setBrush(QColor(0, 0, 0, 0))
    painter.setPen(QPen(white, sw, Qt.SolidLine, Qt.FlatCap, Qt.RoundJoin))
    for sgn in (-1, 1):  # left edge, right edge
        path = QPainterPath(QPointF(cx + sgn * gw / 2, top))
        path.lineTo(QPointF(cx + sgn * neck, cy))
        path.lineTo(QPointF(cx + sgn * gw / 2, bot))
        painter.drawPath(path)

    # Sand: hanging in the top bulb, the falling stream, the pile at the bottom.
    painter.setPen(QPen(white, max(1.0, size * 0.004), Qt.SolidLine, Qt.FlatCap, Qt.MiterJoin))
    painter.setBrush(QBrush(white))
    inset = gw * 0.16
    painter.drawPolygon(QPolygonF([          # top bulb: sand draining
        QPointF(left + inset, top + hh * 0.12),
        QPointF(right - inset, top + hh * 0.12),
        QPointF(cx, cy - hh * 0.06),
    ]))
    painter.drawPolygon(QPolygonF([          # bottom: the pile
        QPointF(cx, cy + hh * 0.16),
        QPointF(left + inset * 0.8, bot - hh * 0.06),
        QPointF(right - inset * 0.8, bot - hh * 0.06),
    ]))
    painter.setPen(QPen(white, max(1.0, size * 0.022), Qt.SolidLine, Qt.RoundCap))
    painter.drawLine(QPointF(cx, cy), QPointF(cx, cy + hh * 0.18))  # the stream

    # Caps wider than the glass — the hourglass frame.
    capw = gw * 1.35
    cap = max(1.8, size * 0.05)
    painter.setPen(QPen(white, cap, Qt.SolidLine, Qt.RoundCap))
    painter.drawLine(QPointF(cx - capw / 2, top), QPointF(cx + capw / 2, top))
    painter.drawLine(QPointF(cx - capw / 2, bot), QPointF(cx + capw / 2, bot))


def _draw_g(painter: QPainter, size: int):
    """The lowercase git "g" of the mark (right), drawn as vectors (bowl + stem
    + descender hook) so it always renders without depending on installed fonts."""
    white = QColor("#FFFFFF")
    sw = size * 0.082  # stroke width
    painter.setBrush(QColor(0, 0, 0, 0))
    painter.setPen(QPen(white, sw, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))

    # Bowl: a circle in the x-height band of the right half.
    r = size * 0.13
    bx = size * 0.66           # bowl center x
    by = size * 0.45           # bowl center y
    painter.drawEllipse(QPointF(bx, by), r, r)

    # Stem: down the bowl's right edge, into the descender...
    sx = bx + r
    painter.drawLine(QPointF(sx, by - r), QPointF(sx, by + r * 1.9))
    # ...ending in an open hook swinging left under the bowl.
    hook = QRectF(sx - 2 * r * 1.05, by + r * 0.55, 2 * r * 1.05, 2 * r * 1.35)
    painter.drawArc(hook, int(-15 * 16), int(-150 * 16))


def make_pixmap(state: str = "running", size: int = 64) -> QPixmap:
    color = STATE_COLORS.get(state, STATE_COLORS["stopped"])
    pm = QPixmap(size, size)
    pm.fill(Qt.transparent)
    painter = QPainter(pm)
    try:
        _draw(painter, size, color)
    finally:
        painter.end()
    return pm


def make_icon(state: str = "running") -> QIcon:
    """Multi-resolution QIcon for the tray and windows."""
    icon = QIcon()
    for s in (16, 24, 32, 48, 64, 128, 256):
        icon.addPixmap(make_pixmap(state, s))
    return icon


# The mark goes grey while an update runs, with a ring drawn over it. Grey and
# not one of the state colours ON PURPOSE: during an update the state signal is
# meaningless (the engine is stopping, then the process is replaced), and a green
# "all good" icon while the daemon is about to vanish for ~40 s is exactly the
# lie that made an update look like a hang.
_PROGRESS_BASE = "#7A7F87"
_PROGRESS_ARC = "#E8EAED"


def make_progress_pixmap(fraction, size: int = 64) -> QPixmap:
    """The icon in grey with a progress ring over it.

    `fraction` is 0.0-1.0, or None for "working, no percentage yet" — which
    draws the track alone. An indeterminate phase must still look different from
    a finished one, so None is a dim ring and 1.0 is a full bright one.
    """
    pm = QPixmap(size, size)
    pm.fill(Qt.transparent)
    painter = QPainter(pm)
    try:
        _draw(painter, size, _PROGRESS_BASE)
        painter.setRenderHint(QPainter.Antialiasing, True)
        # Inset so the ring sits over the mark instead of being clipped by the
        # rounded background; the pen is thick enough to read at 16 px, where
        # most people will actually see this.
        pen_w = max(1.5, size * 0.11)
        inset = pen_w / 2 + size * 0.08
        box = QRectF(inset, inset, size - 2 * inset, size - 2 * inset)

        track = QColor(_PROGRESS_ARC)
        track.setAlpha(90)
        painter.setPen(QPen(track, pen_w, Qt.SolidLine, Qt.RoundCap))
        painter.drawEllipse(box)

        if fraction is not None:
            # A known 0% must still look STARTED: with a zero-length span the
            # ring renders identically to the indeterminate one, so "downloading"
            # and "still asking GitHub" would be the same picture.
            done = max(0.03, min(1.0, float(fraction)))
            painter.setPen(QPen(QColor(_PROGRESS_ARC), pen_w, Qt.SolidLine,
                                Qt.RoundCap))
            # Qt angles are in 1/16th of a degree; 90*16 starts at twelve
            # o'clock and a NEGATIVE span sweeps clockwise, like every other
            # progress ring a user has ever seen.
            painter.drawArc(box, 90 * 16, -int(360 * 16 * done))
    finally:
        painter.end()
    return pm


def make_dot_icon(color_hex: str, size: int = 16) -> QIcon:
    """A small filled dot, for marking a menu entry that wants attention.

    Qt cannot colour the TEXT of a QAction: a menu item is painted by the style,
    and the only ways round it are a custom delegate or a QWidgetAction, both of
    which cost the native hover/keyboard behaviour of a real menu. An icon is
    what menus are actually designed to carry, so the red lives there and the
    emphasis lives in a bold font.
    """
    pm = QPixmap(size, size)
    pm.fill(Qt.transparent)
    painter = QPainter(pm)
    try:
        painter.setRenderHint(QPainter.Antialiasing, True)
        painter.setPen(QPen(QColor(color_hex).darker(130), max(1.0, size * 0.06)))
        painter.setBrush(QBrush(QColor(color_hex)))
        m = size * 0.28
        painter.drawEllipse(QRectF(m, m, size - 2 * m, size - 2 * m))
    finally:
        painter.end()
    return QIcon(pm)


def make_progress_icon(fraction) -> QIcon:
    icon = QIcon()
    for s in (16, 24, 32, 48, 64, 128, 256):
        icon.addPixmap(make_progress_pixmap(fraction, s))
    return icon
