"""User notifications (Windows toast), with graceful degradation.

It always logs. If `winotify` is available, it also shows a Windows notification.
If not, nothing happens (it is not a hard dependency). Used for important alerts
such as "autosync paused due to conflict" (§3.4, §11).
"""

import logging

log = logging.getLogger("sincrogit.notify")


def notify(title: str, message: str, level: str = "warning") -> None:
    getattr(log, level, log.warning)("%s — %s", title, message)
    try:
        from winotify import Notification  # type: ignore

        toast = Notification(
            app_id="SincroGit",
            title=title,
            msg=message,
        )
        toast.show()
    except Exception:  # noqa: BLE001 — the visual notification is optional
        pass
