"""A tiny, reusable "working…" indicator.

The panel and its dialogs kick every slow operation (git, network, the AI)
onto a worker thread — good for not freezing, but the window then just sits
there looking dead. This is the missing visible half: a slim indeterminate
progress bar with a message, shown while work is in flight.

Ref-counted on purpose: a single view can have several workers running at once
(the Time machine tab loads the rail AND a file diff concurrently), so `start`
increments and `stop` decrements — the bar hides only when the LAST one
finishes. Each `start(msg)` updates the caption; when several overlap the most
recent message wins, and as they finish the caption falls back to whatever is
still running (so it never lies about being idle while a worker lives).

Everything here runs on the GUI thread — callers flip it from their queued
signal handlers, never from the worker itself.
"""

from PyQt5.QtWidgets import QHBoxLayout, QLabel, QProgressBar, QWidget


class BusyBar(QWidget):
    """A slim indeterminate progress bar + caption. Hidden when idle.

    Usage from the GUI thread:
        self.busy.start("Loading history…")   # when a worker is dispatched
        ...                                    # (in the completion signal:)
        self.busy.stop()
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self._count = 0
        self._messages = []   # active captions, newest last (a small stack)
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(8)
        self._label = QLabel("")
        self._label.setProperty("cssClass", "muted")
        row.addWidget(self._label)
        self._bar = QProgressBar()
        self._bar.setRange(0, 0)          # 0..0 = indeterminate "marquee"
        self._bar.setTextVisible(False)
        self._bar.setFixedHeight(4)       # a hairline, not a chunky widget
        self._bar.setProperty("cssClass", "busy")
        row.addWidget(self._bar, 1)
        self.setVisible(False)

    def start(self, message: str = "Working…"):
        """A worker was dispatched: show the bar (or bump the ref count) and set
        the caption. Every start MUST be paired with exactly one stop."""
        self._count += 1
        self._messages.append(message)
        self._label.setText(message)
        self.setVisible(True)

    def stop(self):
        """A worker finished. Hides the bar only when the last one is done;
        otherwise the caption falls back to the previous still-running message.
        Safe to over-call (never goes negative)."""
        if self._count <= 0:
            return
        self._count -= 1
        if self._messages:
            self._messages.pop()
        if self._count <= 0:
            self._count = 0
            self._messages.clear()
            self.setVisible(False)
        else:
            self._label.setText(self._messages[-1])

    def reset(self):
        """Force back to idle (e.g. a dialog re-opening) regardless of count."""
        self._count = 0
        self._messages.clear()
        self.setVisible(False)

    @property
    def active(self) -> bool:
        return self._count > 0
