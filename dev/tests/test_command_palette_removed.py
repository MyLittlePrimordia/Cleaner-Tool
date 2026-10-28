"""The Ctrl+K palette is gone: no module, no dialog, no binding, no button."""
from __future__ import annotations

import inspect
from pathlib import Path


def test_palette_modules_and_dialog_are_gone():
    root = Path(__file__).resolve().parents[2]
    assert not (root / "app" / "command_palette.py").exists()
    assert not (root / "app" / "ui" / "dialogs"
                / "command_palette_dialog.py").exists()


def test_palette_is_not_registered_exported_or_bound():
    from app.ui import dialogs as D
    from app.ui.app import Application
    from app.ui.dialogs.registry import DIALOGS

    assert "command_palette" not in DIALOGS
    assert "CommandPaletteDialog" not in D.__all__
    assert not hasattr(D, "CommandPaletteDialog")
    assert not hasattr(Application, "_open_command_palette")
    assert not hasattr(Application, "_build_palette_affordance")

    source = inspect.getsource(Application)
    assert "<Control-k>" not in source
    assert "<Control-K>" not in source
    assert "Ctrl K" not in source
