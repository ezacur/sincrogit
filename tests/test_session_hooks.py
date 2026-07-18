"""Windows session hooks: the native filter's message decoding — lock/unlock,
suspend/resume, and the session-end family (shutdown vs logoff bit, cancel)."""

import ctypes
import sys

import pytest

pytestmark = pytest.mark.skipif(sys.platform != "win32",
                                reason="Windows-only native messages")

import sincrogit.gui.app as appmod  # noqa: E402


@pytest.fixture
def filt_calls(qapp):
    calls = []
    filt = appmod._WinSessionEventFilter(
        on_leave=lambda r: calls.append(("leave", r)),
        on_arrive=lambda r: calls.append(("arrive", r)),
        on_ending=lambda k: calls.append(("ending", k)),
        on_end_canceled=lambda: calls.append(("canceled",)),
    )
    return filt, calls


def _send(filt, message, wparam=0, lparam=0):
    msg = filt._MSG(hwnd=None, message=message, wParam=wparam, lParam=lparam,
                    time=0, pt_x=0, pt_y=0)
    return filt.nativeEventFilter(b"windows_generic_MSG", ctypes.addressof(msg))


def test_lock_unlock_suspend_resume(filt_calls):
    filt, calls = filt_calls
    _send(filt, appmod._WM_WTSSESSION_CHANGE, wparam=appmod._WTS_SESSION_LOCK)
    _send(filt, appmod._WM_WTSSESSION_CHANGE, wparam=appmod._WTS_SESSION_UNLOCK)
    _send(filt, appmod._WM_POWERBROADCAST, wparam=appmod._PBT_APMSUSPEND)
    _send(filt, appmod._WM_POWERBROADCAST, wparam=appmod._PBT_APMRESUMESUSPEND)
    _send(filt, appmod._WM_POWERBROADCAST, wparam=appmod._PBT_APMRESUMEAUTOMATIC)
    assert calls == [("leave", "lock"), ("arrive", "unlock"),
                     ("leave", "suspend"), ("arrive", "resume"),
                     ("arrive", "resume")]


def test_session_end_shutdown_vs_logoff(filt_calls):
    filt, calls = filt_calls
    _send(filt, appmod._WM_QUERYENDSESSION, lparam=0)
    _send(filt, appmod._WM_QUERYENDSESSION, lparam=appmod._ENDSESSION_LOGOFF)
    _send(filt, appmod._WM_ENDSESSION, wparam=1, lparam=0)
    assert calls == [("ending", "shutdown"), ("ending", "logoff"),
                     ("ending", "shutdown")]


def test_session_end_cancel_and_noise_ignored(filt_calls):
    filt, calls = filt_calls
    _send(filt, appmod._WM_ENDSESSION, wparam=0)          # vetoed shutdown
    assert calls == [("canceled",)]
    calls.clear()
    _send(filt, 0x0400)                                   # unrelated message
    ok = filt.nativeEventFilter(b"not_windows", 0)        # unrelated event type
    assert calls == [] and ok == (False, 0)


def test_filter_never_consumes_the_message(filt_calls):
    filt, _ = filt_calls
    assert _send(filt, appmod._WM_ENDSESSION, wparam=1) == (False, 0)
