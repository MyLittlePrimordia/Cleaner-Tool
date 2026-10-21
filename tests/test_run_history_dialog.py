"""The Run History dialog is GONE - this file keeps it gone.

Historical name: this module used to render the dialog's empty and populated
states. In 2026-10 the Run History card was dropped from the Tools tab (it was
one of the two weakest there, and dropping two cards is what stopped the grid
from scrolling), and the dialog itself went with it.

It is a guard rather than a deletion on purpose. The 2026-10 removal shipped
in a commit that carried the edits but not the file deletions, so the stale
import in the old version of this file was the only thing that broke CI; a
guard cannot rot that way. It pins the parts a half-revert would bring back
one at a time: a resurrected export, a registry key nothing opens, a launcher
calling a dialog that no longer exists, or the card reappearing in the grid.

What is deliberately NOT asserted here: that the dialog module is
unimportable. A deletion only reaches the remote once a commit carries it, so
that assertion would be true locally and false on a runner. The contract that
matters is that nothing can reach the feature.
"""
from __future__ import annotations

import app.ui.dialogs as dialogs
from app.ui.dialogs.registry import DIALOGS


def test_dialog_is_not_exported_from_the_package():
    assert not hasattr(dialogs, "RunHistoryDialog")
    assert "RunHistoryDialog" not in dialogs.__all__


def test_registry_has_no_run_history_key():
    assert "run_history" not in DIALOGS


def test_application_has_no_run_history_launcher():
    from app.ui.app import Application

    assert not hasattr(Application, "_open_run_history")


def test_tools_tab_has_no_run_history_card():
    from app.ui.tabs.toolstab import ToolsTab

    keys = [card[0] for card in ToolsTab._CARDS + ToolsTab._FIX_CARDS]
    assert "run_history" not in keys


def test_the_reclaimed_over_time_data_survives_the_dialog():
    """The rolling history is still written and still read elsewhere.

    The scorecard's "reclaimed over 30 days" line reads it through
    config_persist.freed_since(); deleting the dialog must not take the data
    plumbing with it.
    """
    from app.config_persist import freed_since

    total, runs = freed_since(30)
    assert isinstance(total, int) and total >= 0
    assert isinstance(runs, int) and runs >= 0
