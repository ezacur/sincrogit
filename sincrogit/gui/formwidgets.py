"""Shared form widgets and helpers for the config forms.

The global Settings tab and the per-repo Properties dialog build the same kind
of form (combos, capped spinners, disable-sentinel intervals), so the idioms
live here once instead of one importing the other's privates.
"""

import math

from PyQt5.QtWidgets import QComboBox, QSpinBox

# Cross-machine handoff modes: (stored value, human label). Shared verbatim by
# the global Settings form and the per-repo Properties dialog.
_HANDOFF = [
    ("auto", "Automatic (fast-forward + notify)"),
    ("ask", "Ask me (notify + one-click Apply)"),
    ("off", "Off (manual only)"),
]


def _combo(pairs) -> QComboBox:
    cb = QComboBox()
    for value, label in pairs:
        cb.addItem(label, value)
    cb.setMaximumWidth(360)  # form fields shouldn't stretch across the window
    return cb


def _spin(spin: QSpinBox) -> QSpinBox:
    spin.setMaximumWidth(160)  # a number never needs the full row
    return spin


def _select(cb: QComboBox, value):
    i = cb.findData(value)
    cb.setCurrentIndex(i if i >= 0 else 0)


def _is_disabled(value) -> bool:
    """Is an interval stored as a disable sentinel (inf/off/none/never/...)?"""
    if value is None or value is False:
        return True
    if isinstance(value, float) and math.isinf(value):
        return True
    return isinstance(value, str) and value.strip().lower() in (
        "inf", "infinity", "none", "never", "off", "false", "disabled")


def _load_spin(spin: QSpinBox, value, default: int):
    """Fill an interval spinner from a config value, falling back to `default`
    when the value is a disable sentinel (inf/off/...) the spinner can't show.
    Both config forms load their snapshot/pull/autosnap intervals this way."""
    spin.setValue(int(value) if not _is_disabled(value) else default)
