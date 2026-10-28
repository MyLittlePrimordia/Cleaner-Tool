"""U-007: leftovers arrive reviewable — sized, and ticked by risk.

The review panel used to tick EVERY candidate, including registry keys,
startup entries and shortcuts whose only evidence is a name match, and showed
no size for any of them. These tests pin the replacement defaults: folders are
ticked and carry their measured size; rows that are not files start unticked;
and the tick helpers can never sweep a registry key back on by accident.
"""
from __future__ import annotations

import pytest

from dev.tests._tkhelpers import make_root

tk = pytest.importorskip("tkinter")

ITEMS = [
    {"path": "C:/Leftovers/GameData", "label": "C:/Leftovers/GameData",
     "kind": "folder", "bytes": 2_000_000_000},
    {"path": "REGKEY::HKCU::Software\\Gone", "label": "Leftover registry entry",
     "kind": "regkey"},
    {"path": "STARTUP::abc", "label": "Startup entry (target missing)",
     "kind": "startup"},
    {"path": "SHORTCUT::C:/x.lnk", "label": "Shortcut: C:/x.lnk",
     "kind": "shortcut"},
]


def _panel(root):
    """The review panel on a bare instance: only the widget bits it reads."""
    import app.ui.dialogs.uninstall_programs_dialog as mod

    dlg = object.__new__(mod.UninstallProgramsDialog)
    dlg._leftover_frame = tk.Frame(root, bg="#000")
    dlg._leftover_vars = {}
    dlg._leftover_kinds = {}
    dlg.body_width = lambda _margin=0: 420
    dlg._status_texts = []
    return dlg


@pytest.fixture
def root():
    r = make_root()
    r.geometry("700x500+3000+3000")
    try:
        yield r
    finally:
        r.destroy()


def test_only_folder_candidates_start_ticked(root):
    dlg = _panel(root)
    dlg._show_leftover_review("Gone App", [dict(i) for i in ITEMS])
    try:
        ticked = {p: v.get() for p, v in dlg._leftover_vars.items()}
        assert ticked["C:/Leftovers/GameData"] is True
        assert ticked["REGKEY::HKCU::Software\\Gone"] is False
        assert ticked["STARTUP::abc"] is False
        assert ticked["SHORTCUT::C:/x.lnk"] is False
    finally:
        dlg._clear_leftover_panel()


def test_a_folder_row_shows_its_measured_size(root):
    from app.utils import format_bytes

    dlg = _panel(root)
    dlg._show_leftover_review("Gone App", [dict(i) for i in ITEMS])
    try:
        # M3Checkbutton is canvas-drawn: its text lives on the inner label.
        labels = [w._lbl.cget("text")
                  for w in dlg._leftover_frame.winfo_children()
                  if getattr(w, "_lbl", None) is not None]
        joined = "\n".join(str(x) for x in labels)
        assert format_bytes(2_000_000_000, compact=True) in joined
        # Rows with no measured size must not print a confident "0 B".
        assert "0 B" not in joined
    finally:
        dlg._clear_leftover_panel()


def test_untick_all_clears_everything_and_tick_folders_is_not_tick_all(root):
    dlg = _panel(root)
    dlg._show_leftover_review("Gone App", [dict(i) for i in ITEMS])
    try:
        # Turn the risky rows on by hand first, then prove the helpers undo it.
        for path in ("REGKEY::HKCU::Software\\Gone", "STARTUP::abc"):
            dlg._leftover_vars[path].set(True)
        dlg._tick_only_folders(False)
        assert not any(v.get() for v in dlg._leftover_vars.values())

        for var in dlg._leftover_vars.values():
            var.set(True)
        dlg._tick_only_folders(True)
        ticked = {p for p, v in dlg._leftover_vars.items() if v.get()}
        assert ticked == {"C:/Leftovers/GameData"}
    finally:
        dlg._clear_leftover_panel()


def test_clearing_the_panel_forgets_the_previous_kinds(root):
    dlg = _panel(root)
    dlg._show_leftover_review("Gone App", [dict(i) for i in ITEMS])
    assert dlg._leftover_kinds
    dlg._clear_leftover_panel()
    assert dlg._leftover_kinds == {}
    assert dlg._leftover_vars == {}
