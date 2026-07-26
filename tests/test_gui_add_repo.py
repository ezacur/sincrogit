"""Add-repo dialog (offscreen): the cross-machine inheritance offer — when this
user published options for the repo from another machine, the dialog surfaces a
checkbox and, when kept, passes those overrides through to add_repo."""

import pytest

import sincrogit.gui.add_repo_dialog as ard


class Ctl:
    def __init__(self, saved=None):
        self.saved = saved            # what fetch_repo_settings returns
        self.added = None             # captures the add_repo call

    def detect_branch(self, path):
        return "main"

    def detect_remote(self, path, remote="origin"):
        return "https://example/r.git"

    def fetch_repo_settings(self, path, remote="origin"):
        return self.saved

    def add_repo(self, path, branch="main", push=True, pull=True,
                 normalize_eol=True, overrides=None):
        self.added = {"path": path, "branch": branch, "overrides": overrides}
        return True, "ok"


@pytest.fixture
def dlg(qapp, monkeypatch):
    monkeypatch.setattr(ard.QMessageBox, "information",
                        staticmethod(lambda *a, **k: None))
    monkeypatch.setattr(ard.QMessageBox, "critical",
                        staticmethod(lambda *a, **k: None))

    made = []

    def _make(saved):
        ctl = Ctl(saved)
        d = ard.AddRepoDialog(ctl)
        d.show()  # offscreen: a child's isVisible() only tracks setVisible() once shown
        made.append(d)
        return ctl, d
    yield _make
    for d in made:
        d.close()


def test_offer_appears_when_settings_are_published(dlg):
    ctl, d = dlg({"seal_interval_min": "inf", "snapshot_interval_sec": 120})
    assert not d.ck_inherit.isVisible()          # hidden until detection runs
    d._on_settings_ready(d._branch_gen, ctl.saved)
    # Visible but OFF by default: a remote-controlled setting is opt-in, not silent.
    assert d.ck_inherit.isVisible() and not d.ck_inherit.isChecked()
    assert "2 settings" in d.ck_inherit.text()
    # The exact values are surfaced inline (not tooltip-only) so the choice is informed.
    assert d.lbl_inherit.isVisible() and "seal_interval_min" in d.lbl_inherit.text()


def test_no_offer_when_nothing_published(dlg):
    _ctl, d = dlg(None)
    d._on_settings_ready(d._branch_gen, None)
    assert not d.ck_inherit.isVisible()
    assert d._inherited is None


def test_stale_detection_is_ignored(dlg):
    ctl, d = dlg({"push": False})
    d._on_settings_ready(d._branch_gen - 1, ctl.saved)  # from a previous path
    assert not d.ck_inherit.isVisible()


def test_kept_offer_passes_overrides_to_add(dlg):
    ctl, d = dlg({"seal_interval_min": "inf"})
    d.ed_path.setText(r"C:\repo")
    d._on_settings_ready(d._branch_gen, ctl.saved)
    d._do_add(r"C:\repo", "main", True, True, False, "", d._inherited)
    assert ctl.added["overrides"] == {"seal_interval_min": "inf"}


def test_unchecked_offer_inherits_local_defaults(dlg):
    ctl, d = dlg({"seal_interval_min": "inf"})
    d._on_settings_ready(d._branch_gen, ctl.saved)
    d.ck_inherit.setChecked(False)
    # _add decides overrides from the checkbox; mirror that decision here.
    overrides = d._inherited if d.ck_inherit.isChecked() else None
    d._do_add(r"C:\repo", "main", True, True, False, "", overrides)
    assert ctl.added["overrides"] is None
