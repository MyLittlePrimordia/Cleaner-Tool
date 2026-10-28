"""Preset summary chips must render at ONE density, whatever the task count.

User-reported defect (screenshots): Quick Clean's 8 chips were ~95px tall with
huge gaps while Deep Clean's 28 were compact 34px rows — the same component,
two different designs, depending only on how many tasks the preset has.

Root cause (SUM-001): the cells are built before the scroll panel has a width,
so their labels are wrapped at the 80px provisional fallback. `_regrid` later
re-widens them, but it called `cell._sync()` in the same call stack, before Tk
had recomputed the label's requested height — so every card re-applied the
height derived from the 80px wrap. A long preset escaped because the scroll
panel's extra geometry passes corrected it; a short preset has no further
passes, so the tall rows stuck.

SUM-002: preset bodies never stored their grid state, so switching
Quick -> Deep -> Quick left `_cells` pointing at the hidden Deep Clean chips,
and the panel's resize callback then re-gridded the wrong list.

These tests build the real Clean tab on a real Tk root and measure it.
"""
from __future__ import annotations

import time

import pytest

tk = pytest.importorskip("tkinter")

from app.ui.tabs.tasktab import TaskTab

from _tkhelpers import make_root


class _StubApp:
    tweak_state = {}

    def __getattr__(self, name):
        return lambda *a, **k: None


def _build():
    root = make_root()
    root.geometry("1280x900")
    page = tk.Frame(root, bg="#131314")
    page.pack(fill="both", expand=True)
    tab = TaskTab(page, _StubApp(), "Clean")
    tab.pack(fill="both", expand=True)
    _pump(root, 700)          # past the panel's 180 ms re-grid debounce
    if not _wait_until(root, lambda: bool(root.winfo_ismapped()), 3000):
        # Every assertion in this file measures a REAL window. A Tk root that
        # never mapped (observed ~1 in 12 runs on a loaded machine, typically
        # when several interpreters are created in quick succession) has no
        # geometry to assert on; measuring it would test the environment, not
        # the app.
        root.destroy()
        pytest.skip("Tk root did not map; no geometry to measure")
    return root, tab


def _pump(root, ms):
    end = time.time() + ms / 1000.0
    while time.time() < end:
        root.update_idletasks()
        root.update()
        time.sleep(0.01)


def _wait_until(root, predicate, timeout_ms=2500):
    """Pump until `predicate()` is true, or fail with the last state.

    The fixed _pump() windows below are debounce budgets, not guarantees: on a
    loaded CI box the panel can still be mid-regrid when 500 ms elapse, which
    made these two assertions fail intermittently (observed twice in the same
    session). Waiting for the condition keeps every assertion exactly as
    strict and removes the timing race."""
    end = time.time() + timeout_ms / 1000.0
    while time.time() < end:
        root.update_idletasks()
        root.update()
        if predicate():
            return True
        time.sleep(0.01)
    return False


def _visible_panel(tab):
    """The panel inside the body that is currently PACKED.

    Structural on purpose: `winfo_ismapped()` depends on the window manager
    having mapped the root, which is exactly the thing this suite cannot rely
    on. A cached body is shown with pack() and hidden with pack_forget(), so
    `winfo_manager()` says which body the user is looking at whether or not
    the window has been painted yet.
    """
    try:
        wraps = tab.body_area.winfo_children()
    except Exception:
        return None
    for wrap in wraps:
        if not wrap.winfo_manager():
            continue
        for child in wrap.winfo_children():
            if child.__class__.__name__ == "ScrollableRoundedPanel":
                return child
    return None


def _heights(tab):
    return [cell.winfo_height() for cell, _task in tab._cells]


def test_quick_clean_chips_are_compact_and_uniform():
    root, tab = _build()
    try:
        heights = _heights(tab)
        assert len(heights) == len(tab.presets["Quick Clean"])
        assert len(set(heights)) == 1, heights
        # One line of 9pt text plus the chip's own padding: 36-38px at 100%.
        # A two- or three-line wrap (the bug) is 48px+.
        assert max(heights) <= 44, heights
    finally:
        root.destroy()


def test_quick_clean_and_deep_clean_share_one_density():
    root, tab = _build()
    try:
        quick = _heights(tab)
        tab._select_preset("Deep Clean")
        _pump(root, 700)
        deep = _heights(tab)
        assert len(deep) == len(tab.presets["Deep Clean"])
        assert set(deep) == set(quick), (quick[:4], deep[:4])
    finally:
        root.destroy()


def test_switching_back_shows_the_right_chips_in_the_right_panel():
    root, tab = _build()
    want = len(tab.presets["Quick Clean"])

    def _panels():
        found = []

        def walk(widget):
            for child in widget.winfo_children():
                if child.__class__.__name__ == "ScrollableRoundedPanel":
                    found.append((child.winfo_ismapped(),
                                  len(child.inner.grid_slaves()),
                                  child.winfo_width(),
                                  child.winfo_height()))
                walk(child)
        walk(tab)
        return found

    def _settled():
        panel = _visible_panel(tab)
        return (panel is not None and len(tab._cells) == want
                and len(panel.inner.grid_slaves()) == want)

    try:
        tab._select_preset("Deep Clean")
        _pump(root, 500)
        tab._select_preset("Quick Clean")
        # SUM-002: the summary state comes back with the body, so _cells
        # describes the SHOWN preset, not the previously built one.
        assert _wait_until(root, _settled), (
            len(tab._cells), want, _panels(),
            root.winfo_ismapped(), root.winfo_viewable())
        panel = _visible_panel(tab)
        assert len(panel.inner.grid_slaves()) == want
        # A resize re-runs the panel's column decision; the wrong list must
        # never be gridded into this panel (that path raised a TclError).
        root.geometry("1100x820")
        _pump(root, 700)
        assert len(tab._cells) == want
        assert len(panel.inner.grid_slaves()) == want
        assert _wait_until(root, lambda: len(set(_heights(tab))) == 1), \
            _heights(tab)
    finally:
        root.destroy()


def test_prewarming_a_preset_leaves_the_visible_body_alone():
    """The idle pre-warm must not move the user's current preset.

    2026-10: Quick -> Deep was the last click that still built a summary
    body synchronously. The idle chain now builds those bodies into the
    cache; the builder mutates page state, so this pins that the visible
    mode/selection/vars come back exactly as they were.
    """
    root, tab = _build()
    try:
        before = (tab.mode, tab._selected_preset, tab._cells)
        assert tab._prewarm_body("preset:Deep Clean") is True
        assert (tab.mode, tab._selected_preset, tab._cells) == before
        assert "preset:Deep Clean" in tab._body_cache
        # A second pre-warm is a no-op, not a second set of widgets.
        assert tab._prewarm_body("preset:Deep Clean") is False
    finally:
        root.destroy()


def test_a_cell_wrapped_at_a_narrow_width_grows_and_shrinks_back():
    """The card converges in both directions, not just upward.

    This is the property the re-grid relies on: a card that only grows would
    keep the tall height of a provisional narrow wrap forever.
    """
    root, tab = _build()
    try:
        cell, _task = tab._cells[0]
        label = cell._wrap_label
        short = cell.winfo_height()

        label.config(wraplength=90)          # force several lines
        cell.body.update_idletasks()
        cell._sync()
        assert _wait_until(root, lambda: cell.winfo_height() > short), short
        tall = cell.winfo_height()

        label.config(wraplength=900)         # one line again
        cell.body.update_idletasks()
        cell._sync()
        assert _wait_until(root, lambda: cell.winfo_height() == short), \
            (short, cell.winfo_height())
        assert tall > short, (short, tall)
    finally:
        root.destroy()
