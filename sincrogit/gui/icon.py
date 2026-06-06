"""SincroGit icon: a "G" with an hourglass.

It is drawn with QPainter (vectorial, crisp at any size) and the background color
changes with the state, so a glance at the tray tells you whether it is active,
paused, in conflict or stopped.

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
    "conflict": "#D23F3F",  # red: conflict, needs attention
    "stopped": "#7A7F87",   # gray: stopped / idle
}

STATE_TOOLTIP = {
    "running": "SincroGit: active",
    "paused": "SincroGit: paused",
    "conflict": "SincroGit: conflict (needs attention)",
    "stopped": "SincroGit: stopped",
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

    # --- "G" drawn as a vector (not as text) so it always renders, without
    #     depending on installed fonts ---
    _draw_g(painter, size)

    # --- Hourglass inside a circular badge (bottom-right corner) ---
    _draw_hourglass_badge(painter, size, base)


def _draw_g(painter: QPainter, size: int):
    cx = size * 0.45
    cy = size * 0.49
    r = size * 0.29
    sw = r * 0.46  # stroke width of the G
    white = QColor("#FFFFFF")
    painter.setBrush(QColor(0, 0, 0, 0))
    painter.setPen(QPen(white, sw, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))

    # Ring open on the right (like a "C").
    rect = QRectF(cx - r, cy - r, 2 * r, 2 * r)
    # ~80° gap centered at 0° (3 o'clock): draw from 40° to 320° (CCW).
    painter.drawArc(rect, int(40 * 16), int(280 * 16))

    # Horizontal bar of the "G" entering from the right up to the center.
    painter.drawLine(QPointF(cx + r * 0.08, cy), QPointF(cx + r, cy))


def _draw_hourglass_badge(painter: QPainter, size: int, base: QColor):
    # Dark circular badge that separates the hourglass from the "G".
    r = size * 0.21
    cx = size * 0.74
    cy = size * 0.74
    painter.setBrush(QBrush(base.darker(150)))
    painter.setPen(QPen(QColor("#FFFFFF"), max(1.0, size * 0.022)))
    painter.drawEllipse(QPointF(cx, cy), r, r)

    # Hourglass (two triangles + caps) in white, inside the badge.
    hw = r * 0.92
    hh = r * 1.12
    left = cx - hw / 2
    right = cx + hw / 2
    top = cy - hh / 2
    bot = cy + hh / 2

    white = QColor("#FFFFFF")
    painter.setBrush(QBrush(white))
    painter.setPen(QPen(white, max(1.0, size * 0.012), Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
    painter.drawPolygon(QPolygonF([
        QPointF(left, top), QPointF(right, top), QPointF(cx, cy),
    ]))
    painter.drawPolygon(QPolygonF([
        QPointF(left, bot), QPointF(right, bot), QPointF(cx, cy),
    ]))
    cap = max(1.4, size * 0.026)
    painter.setPen(QPen(white, cap, Qt.SolidLine, Qt.RoundCap))
    painter.drawLine(QPointF(left, top), QPointF(right, top))
    painter.drawLine(QPointF(left, bot), QPointF(right, bot))


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
