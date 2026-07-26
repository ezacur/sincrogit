"""Start-at-login (per-user Run key): register/read/remove/heal against a
private test subkey — the user's real Run hive is never touched."""

import os
import sys

import pytest

pytestmark = pytest.mark.skipif(sys.platform != "win32",
                                reason="Windows registry")

import sincrogit.autostart as auto  # noqa: E402

TEST_KEY = r"Software\SincroGitTests\Run"


@pytest.fixture
def runkey(monkeypatch):
    monkeypatch.setattr(auto, "RUN_KEY", TEST_KEY)
    yield
    import winreg
    for key in (TEST_KEY, r"Software\SincroGitTests"):
        try:
            winreg.DeleteKey(winreg.HKEY_CURRENT_USER, key)
        except OSError:
            pass


def test_roundtrip_enable_read_disable(runkey, tmp_path):
    cfg = str(tmp_path / "config.yaml")
    assert not auto.is_enabled()
    ok, msg = auto.set_autostart(True, cfg)
    assert ok and "enabled" in msg
    assert auto.is_enabled()
    assert auto.get_autostart() == auto.autostart_command(cfg)
    ok, msg = auto.set_autostart(False, cfg)
    assert ok and not auto.is_enabled()
    ok, _ = auto.set_autostart(False, cfg)   # removing a missing value is fine
    assert ok


def test_command_shape_frozen_vs_source(tmp_path, monkeypatch):
    cfg = str(tmp_path / "config.yaml")
    cmd = auto.autostart_command(cfg)        # source checkout (this test run)
    assert "-m sincrogit" in cmd and "--tray" in cmd
    assert f'"{os.path.abspath(cfg)}"' in cmd
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    cmd = auto.autostart_command(cfg)
    assert cmd.startswith(f'"{sys.executable}"') and "-m sincrogit" not in cmd
    assert "--tray" in cmd


def test_stale_detection_and_heal(runkey, tmp_path):
    """An entry whose exe is gone (moved dist\\, removed install) is stale;
    heal() re-registers the CURRENT invocation."""
    import winreg
    cfg = str(tmp_path / "config.yaml")
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, TEST_KEY) as key:
        winreg.SetValueEx(key, auto.VALUE_NAME, 0, winreg.REG_SZ,
                          r'"C:\gone\SincroGit.exe" --tray -c "C:\gone\c.yaml"')
    assert auto.is_stale()
    assert auto.heal(cfg)
    assert not auto.is_stale()
    assert auto.get_autostart() == auto.autostart_command(cfg)


def test_heal_respects_a_live_entry(runkey, tmp_path):
    """A dev launch must never hijack an entry that points at an executable
    that still EXISTS — even if the command differs from what we'd write."""
    import winreg
    live = f'"{sys.executable}" --tray -c "C:\\somewhere\\else.yaml"'
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, TEST_KEY) as key:
        winreg.SetValueEx(key, auto.VALUE_NAME, 0, winreg.REG_SZ, live)
    assert not auto.is_stale()
    assert not auto.heal(str(tmp_path / "config.yaml"))
    assert auto.get_autostart() == live      # untouched


def test_disabled_entry_never_heals(runkey, tmp_path):
    assert not auto.heal(str(tmp_path / "config.yaml"))
    assert not auto.is_enabled()


def test_source_checkout_with_dead_package_is_stale(runkey, tmp_path, monkeypatch):
    """A source-checkout entry ("<python> -m sincrogit …") whose interpreter
    survives but can no longer import the package (checkout deleted after an
    editable install) is stale — even though target_of(cmd) still exists."""
    import winreg
    cfg = str(tmp_path / "config.yaml")
    cmd = f'"{sys.executable}" -m sincrogit --tray -c "{cfg}"'
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, TEST_KEY) as key:
        winreg.SetValueEx(key, auto.VALUE_NAME, 0, winreg.REG_SZ, cmd)
    # The interpreter exists, so staleness hinges purely on the import probe.
    monkeypatch.setattr(auto, "_interpreter_can_import", lambda exe: True)
    assert not auto.is_stale()
    monkeypatch.setattr(auto, "_interpreter_can_import", lambda exe: False)
    assert auto.is_stale()
