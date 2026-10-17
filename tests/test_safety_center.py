"""U-002: the Safety & Undo panel.

Two kinds of check: pure helpers (no Tk), and a real built dialog whose
section state is rendered from canned data (the gather step is patched out so
nothing reads the registry, PowerShell or the disk).
"""
from __future__ import annotations

import pytest

tk = pytest.importorskip("tkinter")

from app.ui.dialogs import SafetyCenterDialog
from app.ui.dialogs._tool_page import ToolPage
from app.ui.dialogs.registry import DIALOGS
from app.ui.dialogs.safety_center_dialog import _count_tree, _plural

from _tkhelpers import make_root


class _StubApp:
    def __getattr__(self, name):
        return lambda *a, **k: None


def test_registered_exported_and_on_the_shared_shell():
    assert issubclass(SafetyCenterDialog, ToolPage)
    assert "safety_center" in DIALOGS
    assert DIALOGS["safety_center"].cls is SafetyCenterDialog


def test_plural_helper():
    assert _plural(1, "tweak") == "tweak"
    assert _plural(2, "tweak") == "tweaks"
    assert _plural(0, "change") == "changes"


def test_count_tree(tmp_path):
    assert _count_tree("") == (0, 0)
    assert _count_tree(str(tmp_path / "missing")) == (0, 0)
    backup = tmp_path / "backup"
    backup.mkdir()
    (backup / "a.bin").write_bytes(b"x" * 10)
    (backup / "b.bin").write_bytes(b"y" * 5)
    assert _count_tree(str(backup)) == (2, 15)


@pytest.fixture
def quiet_start(monkeypatch):
    """Stop __init__ from kicking off the background gather."""
    monkeypatch.setattr(SafetyCenterDialog, "_start",
                        lambda self, note=None: None)


def _build(quiet_start):
    root = make_root()
    root.withdraw()
    dlg = SafetyCenterDialog(root, _StubApp())
    return root, dlg


def test_nothing_to_undo_is_stated_honestly(quiet_start):
    root, dlg = _build(quiet_start)
    try:
        dlg._data = {"protect": {"state": "off"},
                     "points": {"points": [], "error": None},
                     "tweaks": [], "svc_undo": [],
                     "drivers_root": "", "drivers": (0, 0),
                     "quarantine_root": "", "quarantine": (0, 0)}
        dlg._render_banner()
        dlg._render_sections()
        assert "No changes to undo" in dlg.banner._title_full
        assert "no way back" in dlg._sections["restore"]["status"].cget("text")
        assert dlg._sections["tweaks"]["buttons"]["Undo all tweaks"]._enabled is False
        assert dlg._sections["services"]["buttons"]["Undo last change"]._enabled is False
        assert dlg._sections["drivers"]["buttons"]["Open backups folder"]._enabled is False
    finally:
        dlg.close()
        root.destroy()


def test_undoable_changes_are_counted_and_actions_enabled(quiet_start):
    root, dlg = _build(quiet_start)
    try:
        dlg._data = {
            "protect": {"state": "on"},
            "points": {"points": [{"when": "2026-10-16 09:00"}], "error": None},
            "tweaks": [object(), object()],
            "svc_undo": [{"name": "SysMain"}],
            "drivers_root": r"C:\x\Drivers",
            "drivers": (3, 2048),
            "quarantine_root": r"C:\x\Quarantine",
            "quarantine": (1, 512),
        }
        dlg._render_banner()
        dlg._render_sections()
        assert "undo 3 changes" in dlg.banner._title_full
        assert "2 applied tweaks" in dlg._sections["tweaks"]["status"].cget("text")
        assert dlg._sections["tweaks"]["buttons"]["Undo all tweaks"]._enabled is True
        assert dlg._sections["services"]["buttons"]["Undo last change"]._enabled is True
        assert dlg._sections["drivers"]["buttons"]["Open backups folder"]._enabled is True
        assert dlg._sections["quarantine"]["buttons"]["Open quarantine folder"]._enabled is True
    finally:
        dlg.close()
        root.destroy()
