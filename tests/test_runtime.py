"""runtime.py: config resolution, socket single-instance + activation protocol."""

import socket
import sys
import threading

import pytest

from sincrogit import runtime
from sincrogit.config import load_config


# --------------------------------------------------------------- ensure_config
def test_ensure_config_explicit_does_not_create(tmp_path):
    """An explicit path is returned as (abspath, False) without touching disk."""
    explicit = str(tmp_path / "somewhere" / "my.yaml")
    path, created = runtime.ensure_config(explicit)
    assert created is False
    assert path == runtime.os.path.abspath(explicit)
    assert not runtime.os.path.exists(path)  # nothing was written


def test_ensure_config_creates_default_and_roundtrips(tmp_path, monkeypatch):
    """With no config in any candidate, a default is created and load_config reads it."""
    target = tmp_path / "exehome"
    target.mkdir()
    monkeypatch.setattr(runtime, "exe_dir", lambda: str(target))
    monkeypatch.setattr(runtime, "appdata_dir", lambda: str(target))
    monkeypatch.setattr(runtime.os, "getcwd", lambda: str(target))

    path, created = runtime.ensure_config(None)
    assert created is True
    assert runtime.os.path.exists(path)

    cfg = load_config(path)
    assert cfg.repos == []
    assert cfg.ai.mode == "hybrid"


# --------------------------------------------------------- single instance (socket)
def _free_port() -> int:
    """A currently-free high port (bind to 0, read what the OS assigned, release)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def test_acquire_single_instance_then_second_fails():
    """First acquire on a free port returns a socket; a second on it returns None."""
    port = _free_port()
    s1 = runtime.acquire_single_instance(port)
    try:
        assert s1 is not None
        assert runtime.acquire_single_instance(port) is None
    finally:
        s1.close()


# --------------------------------------------------------- activation protocol
def _serve_once(port):
    """Bind an ephemeral listener, accept one connection, run serve_activation.
    Returns (bound_port, verdict_holder). verdict_holder[0] is filled by the thread."""
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.bind(("127.0.0.1", port))
    srv.listen(1)
    srv.settimeout(5)
    verdict = [Ellipsis]

    def _run():
        try:
            conn, _ = srv.accept()
            verdict[0] = runtime.serve_activation(conn)
        except OSError:
            verdict[0] = "ACCEPT-FAILED"
        finally:
            srv.close()

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    return t, verdict


def _run_protocol(client_call):
    """Start a one-shot server on an ephemeral port, run client_call(port) against
    it, join the server. Returns (client_result, server_verdict)."""
    port = _free_port()
    t, verdict = _serve_once(port)
    result = client_call(port)
    t.join(timeout=5)
    return result, verdict[0]


def test_signal_existing_instance_show():
    """signal_existing_instance -> True and the server verdict is 'show'."""
    result, verdict = _run_protocol(runtime.signal_existing_instance)
    assert result is True
    assert verdict == "show"


def test_ping_existing_instance_no_action():
    """ping_existing_instance -> True but the server verdict is None (no panel)."""
    result, verdict = _run_protocol(runtime.ping_existing_instance)
    assert result is True
    assert verdict is None


def test_request_flush_quit():
    """request_flush_quit -> True and the server verdict is 'flushquit'."""
    result, verdict = _run_protocol(runtime.request_flush_quit)
    assert result is True
    assert verdict == "flushquit"


def test_garbage_client_gets_no_ack_and_no_action():
    """A client sending an unrelated payload gets no ACK and yields verdict None."""
    def _garbage(port):
        with socket.create_connection(("127.0.0.1", port), timeout=2) as c:
            c.sendall(b"GET / HTTP/1.1\r\n\r\n")
            c.settimeout(3)
            try:
                reply = c.recv(64)
            except OSError:
                reply = b""
        return reply

    reply, verdict = _run_protocol(_garbage)
    assert not reply.startswith(b"SINCROGIT:ok")
    assert verdict is None


# --------------------------------------------------------- named mutex (Windows only)
@pytest.mark.skipif(sys.platform != "win32", reason="named mutex is Windows-only")
def test_acquire_instance_mutex_and_release():
    """First acquisition is False (nobody held it); after release no handle remains."""
    name = "Local\\SincroGit-test-%d" % _free_port()  # unique, never the real name
    runtime.release_instance_mutex()  # ensure a clean slate for this test
    assert runtime.acquire_instance_mutex(name) is False
    assert runtime._instance_mutex_handle is not None
    runtime.release_instance_mutex()
    assert runtime._instance_mutex_handle is None


# ------------------------------------- single-instance: mutex vs port ACK trust
def test_daemon_running_ignores_spoofed_ack_when_mutex_is_ours(monkeypatch):
    """When WE hold the authoritative mutex, a (spoofed) ACK on the port must
    not make one-shots believe a daemon runs — else any local process squatting
    the port could block every CLI command."""
    from sincrogit import __main__ as m
    monkeypatch.setattr(m, "acquire_instance_mutex", lambda: False)  # ours
    monkeypatch.setattr(m, "ping_existing_instance", lambda: True)   # impostor ACK
    assert m._daemon_running() is False


def test_daemon_running_uses_ping_without_mutex(monkeypatch):
    from sincrogit import __main__ as m
    monkeypatch.setattr(m, "acquire_instance_mutex", lambda: None)   # no mutex support
    monkeypatch.setattr(m, "ping_existing_instance", lambda: True)
    assert m._daemon_running() is True


def test_daemon_running_true_when_other_holds_mutex(monkeypatch):
    from sincrogit import __main__ as m
    monkeypatch.setattr(m, "acquire_instance_mutex", lambda: True)   # another instance
    assert m._daemon_running() is True


def test_headless_starts_despite_spoofed_ack_when_mutex_is_ours(monkeypatch, capsys):
    """Same distrust for the headless daemon: mutex ours + port taken + impostor
    ACK -> start anyway (without the activation channel), don't exit."""
    from sincrogit import __main__ as m
    monkeypatch.setattr(m, "acquire_instance_mutex", lambda: False)
    monkeypatch.setattr(m, "acquire_single_instance", lambda: None)  # port taken
    monkeypatch.setattr(m, "signal_existing_instance", lambda: True)
    ok, lock = m._acquire_headless_instance()
    assert ok is True and lock is None


def test_headless_backs_off_on_ack_without_mutex(monkeypatch):
    """Without a mutex (non-Windows), the port handshake still decides."""
    from sincrogit import __main__ as m
    monkeypatch.setattr(m, "acquire_instance_mutex", lambda: None)
    monkeypatch.setattr(m, "acquire_single_instance", lambda: None)
    monkeypatch.setattr(m, "signal_existing_instance", lambda: True)
    ok, lock = m._acquire_headless_instance()
    assert ok is False and lock is None
