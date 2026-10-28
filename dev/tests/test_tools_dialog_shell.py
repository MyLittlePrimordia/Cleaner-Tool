"""Contract test: every Tools-tab popup uses the shared M3 ToolPage shell.

Why a contract test rather than a visual one. The 2026-10 audit found 19 of the
25 Tools popups hand-rolling their own body on bare `ThemedModal`: headline
fonts from 12 to 34, button padding from 4 to 120, no shared header, and
card-in-card nesting. Re-skinning each one is easy to regress, because adding a
new dialog (or reverting one) silently reintroduces the drift and NOTHING in the
suite notices — the popups only fail by looking wrong.

Two cheap, side-effect-free guards:

  * ancestry — every Tools dialog is a `ToolPage`, so it inherits the single
    Header banner, the one content zone and the role-styled footer;
  * the shell itself — `ToolPage(scroll=True)` really does route content
    through the shared ScrollableRoundedPanel, which is what stops a list
    dialog from being clipped once the banner is added.

Neither builds a real feature dialog, so no registry/WMI/subprocess/audio code
runs. Tk is skipped gracefully when there is no display.
"""
from __future__ import annotations

import pytest

tk = pytest.importorskip("tkinter")

from app.ui import dialogs as D
from app.ui.dialogs._tool_page import ToolPage

from _tkhelpers import make_root

#: Every dialog still exported for a Tools surface. If one is added or
#: removed, this list — and the assertion below — must move with it.
TOOLS_DIALOGS = (
    "SpecsDialog", "StartupManagerDialog",
    "UninstallProgramsDialog", "StorageInsightDialog", "DnsTesterDialog",
    "SpeedTestDialog", "GameServerPingDialog", "DriveToolkitDialog",
    "HardwareMonitorDialog", "PilotDialog", "ProcessManagerDialog",
    "NetworkConnectionsDialog",
    "ServicesDialog", "FileLockDialog", "DuplicatesDialog",
    "RestorePointDialog", "SafetyCenterDialog", "GameSavesDialog",
    "GameReadinessDialog",
)


@pytest.mark.parametrize("name", TOOLS_DIALOGS)
def test_tools_dialog_uses_the_shared_shell(name):
    cls = getattr(D, name)
    assert issubclass(cls, ToolPage), (
        "%s must build on the shared ToolPage shell, not bare ThemedModal"
        % name)


def test_tools_dialogs_are_all_exported():
    """The list above must not drift from the package's public surface."""
    missing = [n for n in TOOLS_DIALOGS if n not in D.__all__]
    assert not missing, "not exported from app.ui.dialogs: %s" % missing


def test_quick_tools_stays_removed():
    """Quick Tools went with Run History in 2026-10 (the grid's scroll fix).

    Nothing should reach it: no package export, no registry key, no Tools
    card, no Application launcher. A half-revert that restores only one of
    those used to be invisible to this suite - see the Run History guard in
    dev/tests/test_run_history_dialog.py for the same contract.
    """
    from app.ui.dialogs.registry import DIALOGS
    from app.ui.tabs.toolstab import ToolsTab

    assert not hasattr(D, "QuickToolsDialog")
    assert "QuickToolsDialog" not in D.__all__
    assert "quick_tools" not in DIALOGS

    keys = [card[0] for card in ToolsTab._CARDS + ToolsTab._FIX_CARDS]
    assert "quick_tools" not in keys

    from app.ui.app import Application

    assert not hasattr(Application, "_open_quick_tools")


def test_toolpage_scroll_routes_content_through_the_shared_panel():
    from app.ui.widgets import ScrollableRoundedPanel

    root = make_root()
    try:
        root.withdraw()

        class _Probe(ToolPage):
            def __init__(self, parent):
                super().__init__(parent, title="Probe", scope="x",
                                 scroll=True)
                tk.Label(self.content, text="row").pack()

        dlg = _Probe(root)
        try:
            assert dlg._content_panel is not None, (
                "scroll=True must build a ScrollableRoundedPanel")
            assert isinstance(dlg._content_panel, ScrollableRoundedPanel)
            assert dlg.content.master is dlg._content_panel.inner, (
                "content must live inside the scroll panel's inner frame")
        finally:
            dlg.close()
    finally:
        root.destroy()


def test_toolpage_without_scroll_is_a_plain_content_frame():
    root = make_root()
    try:
        root.withdraw()

        class _Probe(ToolPage):
            def __init__(self, parent):
                super().__init__(parent, title="Probe", scope="x")

        dlg = _Probe(root)
        try:
            assert dlg._content_panel is None
        finally:
            dlg.close()
    finally:
        root.destroy()
