"""Regression tests for the 2026-10 "make the popups look Material 3" pass.

The user's report, restated as the things these tests pin down:

  * "some have a scroll inside their popup window" - the scrollbars must be
    floating overlays that fade out, with NO permanent track;
  * "some like services and network connections don't show the app icons" -
    DataTable must reserve an icon slot and accept per-row icons;
  * "some popups have thin white lines" - the table header rule and the
    filter-chip / clip-lamp hairlines must be gone;
  * "the mic tester bar isn't one solid bar" - the level meter must draw a
    single solid fill, not a segmented ladder;
  * "the mouse tester has big wonky buttons" - the artwork must still expose
    exactly one zone per tracked button;
  * "the gray fill for the toggle options looks weirds" - an unselected
    switch must read as the M3 outline + thumb on the surface behind it, not
    as a grey-filled pill.

All side-effect-free: no engine/registry/subprocess/audio code runs, and the
mouse artwork is exercised on a bare instance so no feature dialog is built.
Tk is skipped gracefully when there is no display.
"""
from __future__ import annotations

import inspect

import pytest

tk = pytest.importorskip("tkinter")

from app.icon_extract import default_icon_ppm  # noqa: E402
from app.ui.theme import COLORS  # noqa: E402


def _tk_root():
    try:
        root = tk.Tk()
    except tk.TclError as exc:  # pragma: no cover - headless CI
        pytest.skip("no display: %s" % exc)
    root.withdraw()
    return root


def _mapped_root(w=480, h=160):
    """A real (but off-screen) root, for widgets that need true geometry."""
    try:
        root = tk.Tk()
    except tk.TclError as exc:  # pragma: no cover - headless CI
        pytest.skip("no display: %s" % exc)
    root.geometry("%dx%d+3000+3000" % (w, h))
    return root


def _img(root_or_dummy=None, size=8):
    return tk.PhotoImage(data=default_icon_ppm(size=size))


# --------------------------------------------------------------- DataTable
def test_datatable_reserves_an_icon_slot_and_accepts_icons():
    from app.ui.widgets.datatable import DataTable

    root = _tk_root()
    try:
        cols = ({"key": "name", "title": "Name", "w": 220, "weight": 1},)
        t = DataTable(root, cols, icon_col="name")
        t.pack(fill="both", expand=True)
        root.update_idletasks()
        t._fit_metrics()
        assert t._icon_w > 0, (
            "icon_col must reserve a leading icon slot so text stays aligned")
        t.set_rows([{"_key": "a", "_cells": {"name": {"text": "Alpha"}}}])
        img = _img()
        t.apply_icons({"a": img})
        cell = t.rows[0]["_cells"]["name"]
        assert cell.get("icon") is img, (
            "apply_icons must attach the image to the row's icon cell")
        assert t._icon_index() == 0
    finally:
        root.destroy()


def test_datatable_without_icon_col_reserves_nothing():
    from app.ui.widgets.datatable import DataTable

    root = _tk_root()
    try:
        t = DataTable(root, ({"key": "name", "title": "Name"},))
        t.pack(fill="both", expand=True)
        root.update_idletasks()
        t._fit_metrics()
        assert t._icon_w == 0
        assert t._icon_index() == -1
    finally:
        root.destroy()


def test_datatable_header_draws_no_divider_rule():
    from app.ui.widgets.datatable import DataTable

    root = _tk_root()
    try:
        t = DataTable(root, ({"key": "name", "title": "Name", "w": 200},),
                      show_header=True)
        t.pack(fill="both", expand=True)
        root.update_idletasks()
        t.set_rows([{"_key": "a", "_cells": {"name": "x"}}])
        t._draw_head()
        kinds = {t._head.type(i) for i in t._head.find_all()}
        assert "line" not in kinds, (
            "the header underline read as a stray thin white line")
    finally:
        root.destroy()


def test_datatable_scrollbar_is_an_overlay_that_fades():
    from app.ui.widgets.datatable import DataTable

    root = _tk_root()
    try:
        t = DataTable(root, ({"key": "name", "title": "Name"},))
        t.pack(fill="both", expand=True)
        root.update_idletasks()
        t.set_sort(None)
        assert t._sb_show is True
        t._sb_conceal()
        assert t._sb_show is False
        t._sb_poke()
        assert t._sb_show is True
        assert t._sb_after is not None, "reveal must arm the idle fade"
    finally:
        root.destroy()


# -------------------------------------------------- ScrollableRoundedPanel
def test_scroll_panel_scrollbar_has_no_track_and_auto_hides():
    from app.ui.widgets.scroll import ScrollableRoundedPanel

    root = _tk_root()
    try:
        p = ScrollableRoundedPanel(root)
        p.pack(fill="both", expand=True)
        root.update_idletasks()
        p._ensure_sb_items()
        # Only the thumb exists now - the permanent track was removed.
        assert len(p.find_withtag("sb")) == 1
        # Force an overflowing body without depending on real geometry.
        p._content_h, p._view_h = 5000, 200
        assert p.scroll_enabled
        p._reveal_sb()
        assert p._sb_vis is True
        assert p.itemcget(p._sb_thumb, "state") == "normal"
        assert p._sb_hide_id is not None, "reveal must arm the idle fade"
        p._conceal_sb()
        assert p._sb_vis is False
        assert p.itemcget(p._sb_thumb, "state") == "hidden"
    finally:
        root.destroy()


# --------------------------------------------------------------- ChipBar
def test_filter_chips_draw_no_hairline_outline():
    from app.ui.widgets.chips import ChipBar

    root = _tk_root()
    try:
        bar = ChipBar(root, [("a", "Other companies"), ("b", "Running")],
                      initial="a")
        bar.pack()
        root.update_idletasks()
        polys = [i for i in bar.find_all() if bar.type(i) == "polygon"]
        assert polys, "chips must render"
        for i in polys:
            assert str(bar.itemcget(i, "outline")) == "", (
                "unselected chips were outlined in the hairline colour, "
                "which read as a thin white line")
    finally:
        root.destroy()


# ------------------------------------------------------------- LevelMeter
def test_level_meter_is_one_solid_bar_without_hairlines():
    from app.ui.widgets.level_meter import LevelMeter

    root = _mapped_root()
    try:
        m = LevelMeter(root)
        m.pack(fill="x")
        root.update()
        m.set_level(-20.0, -18.0)
        fills = [str(m.itemcget(i, "fill")).upper()
                 for i in m.find_all() if m.type(i) == "polygon"]
        # The old segmented ladder's unlit cell colours must be gone.
        for dead in ("#2A1519", "#2A220D", "#0F2A21"):
            assert dead not in fills
        assert COLORS["accent_green"].upper() in fills, (
            "a healthy level must paint one solid green fill")
        for i in m.find_all():
            if m.type(i) == "polygon":
                assert str(m.itemcget(i, "outline")) == "", (
                    "the track and clip lamp must not carry a hairline")
    finally:
        root.destroy()


# ---------------------------------------------------------- Mouse artwork
def test_mouse_artwork_has_one_zone_per_tracked_button():
    """Exercise the drawing on a bare instance (no app / feature dialog)."""
    from app.ui.dialogs.mouse_tester_dialog import MouseTesterDialog

    root = _tk_root()
    try:
        d = MouseTesterDialog.__new__(MouseTesterDialog)
        d._btn_shapes = {}
        d._held = set()
        d._cv_size = None
        d._mouse_cv = tk.Canvas(root, width=300, height=372)
        d._mouse_cv.pack()
        root.update()
        MouseTesterDialog._draw_mouse_wireframe(d)
        assert set(d._btn_shapes) == {1, 2, 3, 4, 5}, (
            "left / middle / right + two side buttons must each have a zone")
        for num, (shape, text) in d._btn_shapes.items():
            assert shape and text, "zone %r is missing a shape/label" % num
        # Popup-fit pass 2026-10: the artwork is authored in a 300x372 box and
        # SCALED onto whatever canvas it gets, so a shorter canvas shrinks the
        # drawing instead of cropping it. (The 372px canvas request was itself
        # what pushed this popup's content past its content zone.)
        big = d._mouse_cv.bbox("all")
        assert big[3] - big[1] > 300, "sanity: full-size artwork is ~350 tall"
        d._mouse_cv.configure(height=200)
        root.update()
        MouseTesterDialog._draw_mouse_wireframe(d)
        small = d._mouse_cv.bbox("all")
        assert small[3] - small[1] < big[3] - big[1], (
            "a shorter canvas must shrink the artwork, not crop it")
        assert small[3] <= d._mouse_cv.winfo_height() + 1, (
            "the scaled artwork must stay inside its canvas")
        assert set(d._btn_shapes) == {1, 2, 3, 4, 5}
    finally:
        root.destroy()


# --------------------------------------------------- dialog contract checks
def test_services_and_network_request_an_icon_column():
    """Contract: the two list popups the user called out must ask the shared
    table for the icon slot and load icons for their rows."""
    from app.ui.dialogs import network_connections_dialog, services_dialog

    for mod in (services_dialog, network_connections_dialog):
        src = inspect.getsource(mod)
        assert "icon_col=" in src, "%s lost its icon column" % mod.__name__
        assert "_load_icons" in src and "_icon_cache" in src


def test_specs_nav_is_a_rounded_card_with_a_selected_state():
    """The section nav was a hard square tk.Frame; it is a RoundedCard now."""
    from app.ui.dialogs import specs_dialog

    src = inspect.getsource(specs_dialog)
    assert "RoundedCard" in src
    assert "_paint_nav" in src


def test_toggle_off_state_is_hollow_not_grey_filled():
    """USER FIX 2026-10: "the gray fill for the toggle options looks weird".

    M3 puts the unselected track in surfaceContainerHighest, which on this
    palette (#333537) is a grey FILLED pill on a #1E1F20 card and reads as a
    disabled control. The OFF track is now painted in the surface behind the
    switch, leaving exactly the M3 outline + thumb; ON is unchanged."""
    from app.ui.widgets.toggle import ToggleSwitch

    root = _mapped_root()
    try:
        parent_bg = "#1E1F20"
        holder = tk.Frame(root, bg=parent_bg)
        holder.pack()
        sw = ToggleSwitch(holder, tk.BooleanVar(value=False))
        sw.pack()
        root.update_idletasks()
        sw._draw()
        off_fills = [str(sw.itemcget(i, "fill")).lower()
                     for i in sw.find_all()]
        assert parent_bg.lower() in off_fills, (
            "the OFF track must be hollow: the surface behind the switch")
        assert COLORS["surface_hover"].lower() not in off_fills, (
            "a grey-filled track is the look the user reported as weird")
        # ON: no animation needed to look at the finished frame
        sw._pos = 1.0
        sw._draw()
        on_fills = [str(sw.itemcget(i, "fill")).lower()
                    for i in sw.find_all()]
        assert COLORS["primary"].lower() in on_fills, (
            "the selected track must still be the primary fill")
        assert COLORS["on_primary"].lower() in on_fills, (
            "the selected thumb must still be on-primary")
    finally:
        root.destroy()
