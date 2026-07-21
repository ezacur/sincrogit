"""Timeline v4 — the fourth proposal: the Time machine tab, refined.

Per Ernesto's direction after seeing v1/v2/v3: "closer to v1, a bit more
minimalist (but not much), and a PRECISE tooltip on every button". So this is
deliberately a SUBCLASS of TimeMachineTab — every feature (pin, search,
restores, hunks, save-a-copy) keeps working through the inherited machinery —
with three cosmetic-but-real changes on top:

  1. Every interactive control carries a tooltip that says exactly what
     pressing it DOES (and what it never does — e.g. that restores snapshot
     the pre-restore state first, so they are undoable).
  2. Less chrome: the two rare/heavyweight actions (Fetch autosnaps,
     Restore ENTIRE repo…) fold into one "More ▾" menu; Select all/none
     collapse into a single "All" toggle. Primary actions stay visible.
  3. Airier spacing, and it leans on the theme fix that makes combos look
     like list-openers instead of buttons.

Comparing v4 against v1 is therefore a pure look-and-feel comparison — the
behavior underneath is identical by construction.
"""

from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import QMenu

from .time_machine_tab import TimeMachineTab


class TimelineV4Tab(TimeMachineTab):

    def __init__(self, controller, parent=None):
        super().__init__(controller, parent)
        self._build_more_menu()
        self._collapse_select_buttons()
        self._precise_tooltips()
        lay = self.layout()
        lay.setContentsMargins(12, 12, 12, 8)
        lay.setSpacing(9)
        self._apply_mode_ui()  # re-run with the v4 visibility overrides below

    # ------------------------------------------------------------ regrouping
    def _build_more_menu(self):
        """Fold the two rare actions into one menu: btn_fetch becomes the
        trigger ("More ▾"), btn_restore_repo hides — both keep their slots and
        enabled-state logic; the menu actions just delegate to them."""
        self.btn_fetch.clicked.disconnect()
        self.btn_fetch.setText("More ▾")
        menu = QMenu(self)
        self._act_fetch = menu.addAction("Fetch autosnaps",
                                         self._fetch_autosnaps)
        self._act_fetch.setToolTip(
            "Download your other machines' live mirrors from the remote so "
            "their states appear on the rail (disaster recovery / handoff).")
        self._act_restore_repo = menu.addAction("Restore ENTIRE repo…",
                                                self._restore_repo)
        self._act_restore_repo.setToolTip(
            "Set every file back to the selected state, after a preview of "
            "what would change. Undoable: the pre-restore state is captured "
            "as a snapshot first.")
        menu.setToolTipsVisible(True)
        self.btn_fetch.setMenu(menu)
        self.btn_restore_repo.setVisible(False)

    def _collapse_select_buttons(self):
        """One 'All' toggle instead of the Select all / Select none pair."""
        self.btn_all.clicked.disconnect()
        self.btn_all.setText("All")
        self.btn_all.setCheckable(True)
        self.btn_all.toggled.connect(
            lambda on: self._set_all(Qt.Checked if on else Qt.Unchecked))
        self.btn_none.setVisible(False)

    def _apply_mode_ui(self):
        super()._apply_mode_ui()
        # The parent re-shows these per mode; v4 keeps them folded away.
        if hasattr(self, "_act_restore_repo"):   # (parent __init__ calls this early)
            self.btn_none.setVisible(False)
            self.btn_restore_repo.setVisible(False)

    def _sync_actions(self, *_):
        super()._sync_actions()
        if hasattr(self, "_act_restore_repo"):
            self._act_restore_repo.setEnabled(self.btn_restore_repo.isEnabled())

    # -------------------------------------------------------------- tooltips
    def _precise_tooltips(self):
        """Say what each control DOES — precisely, per request. Overrides the
        inherited ones too, so the whole tab speaks with one voice."""
        tips = {
            "cb_repo": "Which repo's history this tab shows.",
            "btn_browse": "Follow ONE file through time: the rail switches to "
                          "that file's versions, with search, per-file restore "
                          "and hunk restore. Double-clicking a file in the "
                          "list below pins it too.",
            "btn_unpin": "Stop following the pinned file and return to the "
                         "whole-repo timeline.",
            "btn_fetch": "The rare actions: fetch your other machines' "
                         "mirrors, or roll the whole repo back to a state.",
            "rb_then": "Answer 'what changed AT that moment': each state's "
                       "files compared against its parent — the work diary.",
            "rb_today": "Answer 'what is different FROM today': each state's "
                        "files compared against the current worktree, with "
                        "checkboxes to restore exactly what you pick.",
            "cb_filter": "What the rail lists: every capture (~5-min "
                         "snapshots, seals, mirrors) or only the permanent "
                         "commits.",
            "ed_search": "Type a text and press Enter: every version of the "
                         "pinned file is searched, and the rail highlights "
                         "where the text appeared, changed count, or "
                         "vanished.",
            "btn_search": "Search the text in every version of the pinned "
                          "file (same as pressing Enter).",
            "btn_all": "Tick / untick every restorable file at once "
                       "(⚠ uncapturable files always stay out).",
            "btn_none": "Untick every file. (Hidden in this proposal — the "
                        "'All' toggle covers both directions.)",
            "cb_content": "Show the selected version's raw text instead of a "
                          "diff.",
            "cb_sbs": "Two-column diff (before | after) instead of the "
                      "unified single-column view.",
            "btn_saveas": "Write the selected version to a NEW file you name "
                          "— recover old content without overwriting "
                          "anything.",
            "btn_restore_file": "Overwrite the current file with the selected "
                                "version's content. Undoable: the pre-restore "
                                "state is captured as a snapshot first.",
            "btn_hunks": "Roll back only SOME of the changed blocks of the "
                         "pinned file, keeping your other current edits "
                         "intact. Text files only.",
            "btn_restore_sel": "Set the TICKED files back to this state's "
                               "content — restoring creates, overwrites or "
                               "deletes as needed. Undoable: the pre-restore "
                               "state is captured as a snapshot first.",
            "lst": "The timeline, newest first, grouped by day: gray = "
                   "snapshot, green = permanent commit (seal), purple = "
                   "another machine's mirror. Click a state to open it.",
            "tbl_files": "The selected state's files. Double-click one to "
                         "follow it through time (pin).",
        }
        for attr, tip in tips.items():
            getattr(self, attr).setToolTip(tip)
