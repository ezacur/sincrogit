"""Self-update: release discovery, verified download, and the rename dance.

The invariants that matter, in order of how much damage their absence does:
  1. a download that doesn't match the published SHA-256 is NEVER installed
  2. a failed swap leaves a WORKING exe at the path (never nothing)
  3. "up to date" is decided by the exe's digest, not by __version__ (which is
     identical across builds and so cannot answer the question)
No network: urlopen is monkeypatched.
"""

import hashlib
import io
import json
import os
import urllib.error

import pytest

from sincrogit import updater

EXE = b"MZ-this-is-the-published-build" * 400
EXE_SHA = hashlib.sha256(EXE).hexdigest()


def _release(assets):
    return json.dumps({"tag_name": "v9.9.9", "assets": assets}).encode()


def _asset(name, size, url="https://example.invalid/a"):
    return {"name": name, "size": size, "browser_download_url": url}


class _Resp(io.BytesIO):
    """Minimal urlopen() context-manager stand-in."""
    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()
        return False


@pytest.fixture
def fake_net(monkeypatch):
    """Map URL -> bytes (or an Exception to raise)."""
    routes = {}

    def urlopen(req, timeout=None):
        url = req.full_url if hasattr(req, "full_url") else req
        for key, val in routes.items():
            if key in url:
                if isinstance(val, Exception):
                    raise val
                return _Resp(val)
        raise urllib.error.HTTPError(url, 404, "not found", None, None)

    monkeypatch.setattr(updater.urllib.request, "urlopen", urlopen)
    return routes


# --------------------------------------------------------------- discovery

def test_latest_release_picks_the_exe_and_its_digest(fake_net):
    fake_net["api.github.com"] = _release([
        _asset("SincroGit.exe", len(EXE), "https://example.invalid/exe"),
        _asset("SincroGit.exe.sha256", 70, "https://example.invalid/sha"),
    ])
    fake_net["example.invalid/sha"] = f"{EXE_SHA}  SincroGit.exe\n".encode()
    rel = updater.latest_release()
    assert rel["tag"] == "v9.9.9"
    assert rel["size"] == len(EXE)
    assert rel["digest"] == EXE_SHA


def test_a_bare_digest_file_is_accepted_too(fake_net):
    fake_net["api.github.com"] = _release([
        _asset("SincroGit.exe", len(EXE), "https://example.invalid/exe"),
        _asset("SincroGit.exe.sha256", 65, "https://example.invalid/sha"),
    ])
    fake_net["example.invalid/sha"] = (EXE_SHA + "\n").encode()
    assert updater.latest_release()["digest"] == EXE_SHA


def test_garbage_digest_file_degrades_to_unverifiable(fake_net):
    """A corrupt sidecar must not be mistaken for a digest — better to report
    'no digest' (the UI then warns) than to compare against nonsense."""
    fake_net["api.github.com"] = _release([
        _asset("SincroGit.exe", len(EXE), "https://example.invalid/exe"),
        _asset("SincroGit.exe.sha256", 9, "https://example.invalid/sha"),
    ])
    fake_net["example.invalid/sha"] = b"<html>404</html>"
    assert updater.latest_release()["digest"] is None


def test_no_releases_yet_says_so(fake_net):
    fake_net["api.github.com"] = urllib.error.HTTPError(
        "u", 404, "Not Found", None, None)
    with pytest.raises(updater.UpdateError, match="no published release"):
        updater.latest_release()


def test_release_without_the_exe_asset_is_an_error(fake_net):
    fake_net["api.github.com"] = _release([_asset("notes.txt", 10)])
    with pytest.raises(updater.UpdateError, match="no SincroGit.exe"):
        updater.latest_release()


def test_implausible_size_is_refused(fake_net):
    fake_net["api.github.com"] = _release([
        _asset("SincroGit.exe", updater.MAX_ASSET_BYTES + 1)])
    with pytest.raises(updater.UpdateError, match="implausible size"):
        updater.latest_release()


def test_rate_limit_message_is_actionable(fake_net):
    fake_net["api.github.com"] = urllib.error.HTTPError(
        "u", 403, "rate limited", None, None)
    with pytest.raises(updater.UpdateError, match="rate limit"):
        updater.latest_release()


# ------------------------------------------------------------------- check

def test_check_compares_digests_not_versions(fake_net, tmp_path):
    """__version__ is the same string in every build, so sameness is decided by
    hashing the exe we are actually running."""
    exe = tmp_path / "SincroGit.exe"
    exe.write_bytes(EXE)
    fake_net["api.github.com"] = _release([
        _asset("SincroGit.exe", len(EXE), "https://example.invalid/exe"),
        _asset("SincroGit.exe.sha256", 70, "https://example.invalid/sha"),
    ])
    fake_net["example.invalid/sha"] = f"{EXE_SHA}  SincroGit.exe\n".encode()
    assert updater.check(str(exe))[0] == "up-to-date"

    exe.write_bytes(EXE + b"locally-built-differently")
    assert updater.check(str(exe))[0] == "available"


def test_check_offers_the_update_when_the_release_has_no_digest(fake_net, tmp_path):
    """Cannot prove sameness -> offer it, rather than silently claim to be
    current (the UI shows the unverifiable warning)."""
    exe = tmp_path / "SincroGit.exe"
    exe.write_bytes(EXE)
    fake_net["api.github.com"] = _release([
        _asset("SincroGit.exe", len(EXE), "https://example.invalid/exe")])
    assert updater.check(str(exe))[0] == "available"


# ---------------------------------------------------------------- download

def test_download_verifies_and_keeps_the_file(fake_net, tmp_path):
    fake_net["example.invalid/exe"] = EXE
    dest = tmp_path / "SincroGit.exe.new"
    seen = []
    updater.download("https://example.invalid/exe", str(dest), len(EXE), EXE_SHA,
                     progress=lambda d, t: seen.append(d))
    assert dest.read_bytes() == EXE
    assert seen and seen[-1] == len(EXE)


def test_download_with_a_wrong_digest_installs_nothing(fake_net, tmp_path):
    """THE invariant: a tampered or corrupt transfer never reaches the swap."""
    fake_net["example.invalid/exe"] = EXE + b"tampered"
    dest = tmp_path / "SincroGit.exe.new"
    with pytest.raises(updater.UpdateError, match="does not match the published"):
        updater.download("https://example.invalid/exe", str(dest),
                         len(EXE) + 8, EXE_SHA)
    assert not dest.exists()          # partial file cleaned up


def test_truncated_download_is_refused(fake_net, tmp_path):
    fake_net["example.invalid/exe"] = EXE[:100]
    dest = tmp_path / "SincroGit.exe.new"
    with pytest.raises(updater.UpdateError, match="truncated"):
        updater.download("https://example.invalid/exe", str(dest), len(EXE), None)
    assert not dest.exists()


def test_network_failure_mid_download_leaves_nothing_behind(fake_net, tmp_path):
    fake_net["example.invalid/exe"] = urllib.error.URLError("connection reset")
    dest = tmp_path / "SincroGit.exe.new"
    with pytest.raises(updater.UpdateError, match="download failed"):
        updater.download("https://example.invalid/exe", str(dest), len(EXE), EXE_SHA)
    assert not dest.exists()


# -------------------------------------------------------------- swap / clean

def test_swap_parks_the_old_binary_and_installs_the_new(tmp_path):
    exe = tmp_path / "SincroGit.exe"
    exe.write_bytes(b"OLD")
    new = tmp_path / "SincroGit.exe.new"
    new.write_bytes(b"NEW")
    parked = updater.swap_in(str(exe), str(new))
    assert exe.read_bytes() == b"NEW"
    assert open(parked, "rb").read() == b"OLD"
    assert not new.exists()


def test_swap_reuses_the_slot_of_a_previous_update(tmp_path):
    exe = tmp_path / "SincroGit.exe"
    exe.write_bytes(b"OLD2")
    (tmp_path / "SincroGit.exe.old").write_bytes(b"ANCIENT")
    new = tmp_path / "SincroGit.exe.new"
    new.write_bytes(b"NEW2")
    updater.swap_in(str(exe), str(new))
    assert exe.read_bytes() == b"NEW2"
    assert (tmp_path / "SincroGit.exe.old").read_bytes() == b"OLD2"


def test_a_failed_swap_puts_the_working_exe_back(tmp_path, monkeypatch):
    """Invariant #2: an update that goes wrong must never leave the machine
    without a runnable SincroGit."""
    exe = tmp_path / "SincroGit.exe"
    exe.write_bytes(b"OLD")
    new = tmp_path / "SincroGit.exe.new"
    new.write_bytes(b"NEW")

    real = os.replace
    calls = []

    def flaky(src, dst):
        calls.append((src, dst))
        if len(calls) == 2:          # the second move (new -> exe) fails
            raise OSError("locked by antivirus")
        return real(src, dst)

    monkeypatch.setattr(updater.os, "replace", flaky)
    with pytest.raises(updater.UpdateError, match="could not put the new exe"):
        updater.swap_in(str(exe), str(new))
    assert exe.exists() and exe.read_bytes() == b"OLD"


def test_swap_without_a_staged_file_changes_nothing(tmp_path):
    exe = tmp_path / "SincroGit.exe"
    exe.write_bytes(b"OLD")
    with pytest.raises(updater.UpdateError, match="gone"):
        updater.swap_in(str(exe), str(tmp_path / "absent.new"))
    assert exe.read_bytes() == b"OLD"


def test_cleanup_old_removes_the_parked_build_once(tmp_path):
    exe = tmp_path / "SincroGit.exe"
    (tmp_path / "SincroGit.exe.old").write_bytes(b"OLD")
    assert updater.cleanup_old(str(exe)) is True
    assert not (tmp_path / "SincroGit.exe.old").exists()
    assert updater.cleanup_old(str(exe)) is False    # idempotent


def test_sha256_file_handles_a_missing_path(tmp_path):
    assert updater.sha256_file(str(tmp_path / "nope")) is None


# ------------------------------------------------ the tray action's decisions

class _StubApp:
    """Just enough TrayApp surface to drive the update handlers unbound.

    Building a real TrayApp means a config, an Engine and the whole panel; what
    needs locking in here is narrower and more important: WHEN does this code
    decide to touch the installed binary.
    """
    class _Tray:
        def setToolTip(self, text):
            pass

        def setIcon(self, icon):
            pass

    def __init__(self):
        self.events, self.acks, self.done, self.threads = [], [], 0, 0
        self.progress = []
        self._quitting = False
        self._updating = True
        self._pending_update = None
        self._last_state = None
        self.tray = self._Tray()
        self.act_update = type("U", (), {"setEnabled": lambda s, v: None})()
        self.act_pause = type("A", (), {"setText": lambda s, t: None})()

    def app_state(self):
        return "running"

    def _refresh_tray(self):
        pass

    def _on_update_progress(self, payload):
        self.progress.append(payload)


    def _on_engine_event(self, repo, action, msg, level="INFO"):
        self.events.append((action, level, msg))

    def _tray_ack(self, title, body):
        self.acks.append(title)

    def _update_done(self):
        self.done += 1

    def _finish_update(self):
        """The real continuation does the swap; here we only assert it is the one
        handed to the teardown."""
        self.events.append(("swap", "INFO", "would swap"))

    def _teardown_engine_async(self, then):
        self.threads += 1
        self.continuation = then   # must be _finish_update, not _finish_restart


@pytest.fixture
def stub(qapp, monkeypatch):
    """A stub app plus a QMessageBox whose answers the test controls."""
    import sincrogit.gui.app as appmod

    asked = []

    class FakeBox:
        Yes, No = 1, 0

        @staticmethod
        def information(*a, **k):
            asked.append(("info", a[2] if len(a) > 2 else ""))

        @staticmethod
        def warning(*a, **k):
            asked.append(("warn", a[2] if len(a) > 2 else ""))

        answer = 0

        @classmethod
        def question(cls, *a, **k):
            asked.append(("question", a[2] if len(a) > 2 else ""))
            return cls.answer

    monkeypatch.setattr(appmod, "QMessageBox", FakeBox)
    return appmod, _StubApp(), FakeBox, asked


def test_up_to_date_reports_and_downloads_nothing(stub):
    appmod, app, box, asked = stub
    appmod.TrayApp._on_update_checked(app, ("up-to-date", {"tag": "v1.2.3"}))
    assert [k for k, _ in asked] == ["info"]
    assert "already running" in asked[0][1]
    assert app.threads == 0 and app.done == 1


def test_a_check_error_warns_and_logs_it(stub):
    appmod, app, box, asked = stub
    appmod.TrayApp._on_update_checked(app, ("error", "could not reach GitHub"))
    assert [k for k, _ in asked] == ["warn"]
    assert app.events and app.events[0][1] == "ERROR"
    assert app.threads == 0 and app.done == 1


def test_declining_the_update_touches_nothing(stub):
    appmod, app, box, asked = stub
    box.answer = box.No
    appmod.TrayApp._on_update_checked(
        app, ("available", {"tag": "v9", "size": 66 << 20, "digest": "a" * 64,
                            "url": "https://example.invalid/exe"}))
    assert [k for k, _ in asked] == ["question"]
    assert app.done == 1 and app.threads == 0


def test_an_unverifiable_release_says_so_before_asking(stub):
    appmod, app, box, asked = stub
    box.answer = box.No
    appmod.TrayApp._on_update_checked(
        app, ("available", {"tag": "v9", "size": 1 << 20, "digest": None,
                            "url": "https://example.invalid/exe"}))
    assert "publishes no SHA-256" in asked[0][1]


def test_a_failed_download_keeps_the_current_build_running(stub):
    """No teardown, no swap, and the message must say the app keeps running."""
    appmod, app, box, asked = stub
    appmod.TrayApp._on_update_fetched(app, (None, "download failed: reset"))
    assert [k for k, _ in asked] == ["warn"]
    assert "keeps running on the current build" in asked[0][1]
    assert app.threads == 0 and app._pending_update is None
    assert app.events and app.events[0][1] == "ERROR"


def test_a_verified_download_stages_it_and_tears_down(stub):
    appmod, app, box, asked = stub
    appmod.TrayApp._on_update_fetched(app, (r"C:\x\SincroGit.exe.new", None))
    assert app._pending_update == r"C:\x\SincroGit.exe.new"
    assert app._quitting is True and app.threads == 1
    assert any(a == "restart" for a, _, _ in app.events)
    # The swap must be the teardown's continuation: it may only run once the
    # engine is DOWN, never while snapshots are still in flight.
    assert app.continuation == app._finish_update
    # The longest opaque stretch (flush + push + swap + relaunch) must announce
    # itself, or it reads as "SincroGit closed itself and never came back".
    assert app.progress and app.progress[-1][0] is None
    assert "restarting" in app.progress[-1][1]


def test_a_child_that_never_answers_is_reported_not_swallowed(qapp, monkeypatch):
    """The failure that actually happened: the parent Popen'd, quit, and nobody
    was left running with nothing said. Now it warns and names what to run."""
    import sincrogit.gui.app as appmod

    said, logged = [], []

    class FakeBox:
        critical = staticmethod(lambda *a, **k: said.append(a[2] if len(a) > 2 else ""))
        warning = information = staticmethod(lambda *a, **k: None)

    _qa = qapp

    class App:
        config_path = "C:/x/config.yaml"
        logger = type("L", (), {"info": lambda s, *a: None,
                                "error": lambda s, *a: logged.append(a)})()
        qapp = _qa
        tray = type("T", (), {"hide": lambda s: None})()

        def _release_lock(self):
            pass

        def _wait_for_child(self, timeout=20.0):
            return False

    monkeypatch.setattr(appmod, "QMessageBox", FakeBox)
    monkeypatch.setattr(appmod.subprocess, "Popen", lambda *a, **k: None)
    monkeypatch.setattr(appmod, "release_instance_mutex", lambda: None)
    monkeypatch.setattr(qapp, "quit", lambda: None)

    appmod.TrayApp._finish_restart(App(), verify=True)
    assert said, "a dead child must produce a visible message"
    assert "NOTHING IS BEING SNAPSHOTTED" in said[0]
    assert "SincroGit.exe.old" in said[0]
    assert logged, "and it must reach the text log for a post-mortem"


def test_a_child_that_answers_is_a_silent_handover(qapp, monkeypatch):
    import sincrogit.gui.app as appmod

    said = []

    class FakeBox:
        critical = staticmethod(lambda *a, **k: said.append("box"))
        warning = information = staticmethod(lambda *a, **k: None)

    _qa = qapp

    class App:
        config_path = "C:/x/config.yaml"
        logger = type("L", (), {"info": lambda s, *a: None,
                                "error": lambda s, *a: None})()
        qapp = _qa
        tray = type("T", (), {"hide": lambda s: None})()

        def _release_lock(self):
            pass

        def _wait_for_child(self, timeout=20.0):
            return True

    monkeypatch.setattr(appmod, "QMessageBox", FakeBox)
    monkeypatch.setattr(appmod.subprocess, "Popen", lambda *a, **k: None)
    monkeypatch.setattr(appmod, "release_instance_mutex", lambda: None)
    monkeypatch.setattr(qapp, "quit", lambda: None)

    appmod.TrayApp._finish_restart(App(), verify=True)
    assert said == []


def test_mark_of_the_web_strip_leaves_the_file_alone(tmp_path):
    p = tmp_path / "SincroGit.exe.new"
    p.write_bytes(b"NEW")
    updater.strip_mark_of_the_web(str(p))     # no stream here; must not raise
    assert p.read_bytes() == b"NEW"


# ------------------------------------------------------- progress on the icon

def test_progress_pixmaps_differ_by_stage(qapp):
    """0%, mid, done and indeterminate must be visually DISTINCT — a ring that
    looks the same at 10% and 100% is decoration, not feedback."""
    from sincrogit.gui import icon as iconmod

    shots = {name: iconmod.make_progress_pixmap(f, 64).toImage()
             for name, f in (("none", None), ("zero", 0.0),
                             ("half", 0.5), ("full", 1.0))}
    seen = {}
    for name, img in shots.items():
        key = img.bits().asstring(img.byteCount())
        assert key not in seen, f"{name} renders identically to {seen[key]}"
        seen[key] = name
    # And none of them is the normal state icon.
    normal = iconmod.make_pixmap("running", 64).toImage()
    assert shots["half"] != normal


def test_progress_icon_is_grey_not_a_state_colour(qapp):
    """Mid-update the state is meaningless (the engine is stopping, the process
    is about to be replaced); a green 'all good' there is a lie."""
    from sincrogit.gui import icon as iconmod

    assert iconmod._PROGRESS_BASE not in iconmod.STATE_COLORS.values() or \
        iconmod._PROGRESS_BASE == iconmod.STATE_COLORS["stopped"]


def test_the_refresh_timer_does_not_repaint_over_a_running_update(qapp, monkeypatch):
    """The 2.5 s tray timer would overwrite the ring several times a second."""
    import sincrogit.gui.app as appmod

    painted = []

    class Tray:
        def setIcon(self, i):
            painted.append("icon")

        def setToolTip(self, t):
            pass

    class App:
        _updating = True
        _last_state = None
        tray = Tray()
        act_pause = type("A", (), {"setText": lambda s, t: None})()

        def app_state(self):
            return "running"

    appmod.TrayApp._refresh_tray(App())
    assert painted == [], "an update must own the icon"


def test_finishing_an_update_hands_the_icon_back(qapp, monkeypatch):
    """_last_state must be cleared, or _refresh_tray sees 'no change' and the
    grey ring stays on screen forever."""
    import sincrogit.gui.app as appmod

    icons = []

    class App:
        _refresh_tray = appmod.TrayApp._refresh_tray
        _apply_update_action_state = lambda self: None   # covered by its own tests
        _updating = True
        _update_checked_mono = None
        _last_state = "running"          # what it was before the update
        tray = type("T", (), {"setIcon": lambda s, i: icons.append(i),
                              "setToolTip": lambda s, t: None})()
        act_pause = type("A", (), {"setText": lambda s, t: None})()
        act_update = type("U", (), {"setEnabled": lambda s, v: None})()

        def app_state(self):
            return "running"

    app = App()
    appmod.TrayApp._update_done(app)
    assert app._updating is False
    assert icons, "the state icon must be repainted when the update ends"


# ------------------------------------------------ the menu entry tells the truth

def _menu_app(qapp, monkeypatch, frozen=True):
    """A stub carrying just the menu-entry surface, with a fake QAction."""
    import sincrogit.gui.app as appmod

    monkeypatch.setattr(appmod.sys, "frozen", frozen, raising=False)

    class Font:
        def __init__(self):
            self.bold = False

        def setBold(self, v):
            self.bold = v

    class Act:
        def __init__(self):
            self.text = ""
            self.enabled = True
            self.icon = None
            self.tip = ""
            self._font = Font()

        def setText(self, t):
            self.text = t

        def setEnabled(self, v):
            self.enabled = v

        def setIcon(self, i):
            self.icon = i

        def setToolTip(self, t):
            self.tip = t

        def font(self):
            return self._font

        def setFont(self, f):
            self._font = f

    class App:
        UPDATE_CHECK_TTL = appmod.TrayApp.UPDATE_CHECK_TTL
        _updating = False
        _update_state = None
        _update_tag = None
        _update_checking = False
        _update_checked_mono = None

        def __init__(self):
            self.act_update = Act()

    return appmod, App()


def test_entry_is_disabled_when_there_is_nothing_to_update_to(qapp, monkeypatch):
    appmod, app = _menu_app(qapp, monkeypatch)
    app._update_state = "up-to-date"
    appmod.TrayApp._apply_update_action_state(app)
    assert app.act_update.enabled is False
    assert "up to date" in app.act_update.text
    assert app.act_update._font.bold is False


def test_entry_shouts_when_a_build_is_published(qapp, monkeypatch):
    appmod, app = _menu_app(qapp, monkeypatch)
    app._update_state, app._update_tag = "available", "v9.9.9"
    appmod.TrayApp._apply_update_action_state(app)
    assert app.act_update.enabled is True
    assert "v9.9.9" in app.act_update.text
    assert app.act_update._font.bold is True
    assert app.act_update.icon is not None, "the red dot carries the colour"


def test_an_unknown_answer_keeps_the_entry_clickable(qapp, monkeypatch):
    """Offline or rate-limited must never disable it: clicking IS how you find
    out, and disabling would remove the manual retry."""
    appmod, app = _menu_app(qapp, monkeypatch)
    app._update_state = None
    appmod.TrayApp._apply_update_action_state(app)
    assert app.act_update.enabled is True
    assert app.act_update._font.bold is False
    assert app.act_update.icon is None or app.act_update.icon.isNull()


def test_from_source_there_is_nothing_to_replace(qapp, monkeypatch):
    appmod, app = _menu_app(qapp, monkeypatch, frozen=False)
    app._update_state = "available"          # even so: no exe to swap
    appmod.TrayApp._apply_update_action_state(app)
    assert app.act_update.enabled is False
    assert "source" in app.act_update.text


def test_the_menu_does_not_re_ask_within_the_ttl(qapp, monkeypatch):
    """GitHub allows 60 unauthenticated calls an hour; opening the tray menu
    must not burn them."""
    import sincrogit.gui.app as appmod

    appmod_, app = _menu_app(qapp, monkeypatch)
    spawned = []
    monkeypatch.setattr(appmod.threading, "Thread",
                        lambda *a, **k: spawned.append(k.get("name")) or
                        type("T", (), {"start": lambda s: None})())
    app._update_checking = False
    app._update_checked_mono = appmod.time.monotonic()   # just asked
    app._apply_update_action_state = lambda: None
    appmod.TrayApp._refresh_update_action(app)
    assert spawned == [], "a fresh answer must not trigger another call"


# --------------------------------------- abandoned onefile runtimes in %TEMP%

def _stale_setup(tmp_path, monkeypatch, names):
    """Lay out %TEMP% with extraction folders, all aged past the guard."""
    import tempfile as _tf
    import time as _t

    from sincrogit import runtime

    old = _t.time() - 2 * runtime._MEI_MIN_AGE_SEC
    for name, age in names:
        d = tmp_path / name
        d.mkdir()
        (d / "base_library.zip").write_bytes(b"x" * 1024)
        if age == "old":
            os.utime(d, (old, old))
    monkeypatch.setattr(runtime.sys, "frozen", True, raising=False)
    monkeypatch.setattr(runtime.sys, "_MEIPASS", str(tmp_path / "_MEIours"),
                        raising=False)
    monkeypatch.setattr(_tf, "gettempdir", lambda: str(tmp_path))
    return runtime


def test_stale_extractions_are_removed_and_ours_is_kept(tmp_path, monkeypatch):
    """One real machine had 18 of these, ~2 GB: every forced kill strands one."""
    runtime = _stale_setup(tmp_path, monkeypatch, [
        ("_MEI111", "old"), ("_MEI222", "old"), ("_MEIours", "old"),
        ("notmine", "old"), ("_MEIfresh", "new"),
    ])
    gone, freed = runtime.cleanup_stale_temp_dirs()
    assert gone == 2 and freed >= 2048
    assert not (tmp_path / "_MEI111").exists()
    assert (tmp_path / "_MEIours").exists(), "never delete the one we run from"
    assert (tmp_path / "notmine").exists(), "only _MEI* folders"
    assert (tmp_path / "_MEIfresh").exists(), "too young: a sibling may have started"


def test_a_locked_extraction_is_left_alone(tmp_path, monkeypatch):
    """The rename guard: a folder another app is USING must survive untouched —
    a partial delete of a live runtime is the very bug this code exists for."""
    runtime = _stale_setup(tmp_path, monkeypatch, [("_MEIbusy", "old")])
    monkeypatch.setattr(runtime.os, "rename",
                        lambda a, b: (_ for _ in ()).throw(OSError("in use")))
    assert runtime.cleanup_stale_temp_dirs() == (0, 0)
    assert (tmp_path / "_MEIbusy" / "base_library.zip").exists()


def test_from_source_there_is_nothing_to_clean(monkeypatch):
    from sincrogit import runtime
    monkeypatch.setattr(runtime.sys, "frozen", False, raising=False)
    assert runtime.cleanup_stale_temp_dirs() == (0, 0)


def test_a_missing_runtime_is_not_reported_as_a_network_problem(monkeypatch):
    """What actually happened: urlopen lazily imports ssl/http out of
    base_library.zip; with the extraction gone the OSError read as 'could not
    reach GitHub' and sent the user to check their connection."""
    from sincrogit import runtime
    monkeypatch.setattr(runtime.sys, "_MEIPASS", r"C:\gone\_MEI177442",
                        raising=False)
    msg = updater._explain_transport_failure(
        OSError(2, "No such file or directory",
                r"C:\gone\_MEI177442\base_library.zip"))
    assert "network" in msg and "start it again" in msg
    assert "could not reach GitHub" not in msg


def test_a_real_network_error_still_says_so(monkeypatch):
    from sincrogit import runtime
    monkeypatch.setattr(runtime.sys, "_MEIPASS", None, raising=False)
    msg = updater._explain_transport_failure(OSError("connection refused"))
    assert msg.startswith("could not reach GitHub")
