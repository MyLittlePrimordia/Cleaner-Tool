"""Regression tests for the 2026-10 "make these popups fit" pass.

Second sweep, from six screenshots the user sent: Quick Tools, Mouse Tester,
Gamepad Tester, Auto-Pilot, Speed Test and Hardware Monitor still showed a
scrollbar at the window size the app runs at. The audit that followed measured
each popup's content zone against its content at `tk scaling 2.0` (150% DPI,
the worst case the app's default window allows) and found every one of them
between 13px and 235px too tall for the zone the popup had:

    Quick Tools        770 vs a 535 zone   (10 two-line tiles per column)
    Hardware Monitor   672 vs a 535 zone   (2x3 grid of tall cards)
    Gamepad Tester     660 vs a 535 zone   (a 400px artwork + 6 stacked rows)
    Auto-Pilot         616 vs a 517 zone   (3 cards stacked in ONE column)
    Mouse Tester       551 vs a 535 zone   (a fixed 372px artwork)
    Speed Test         530 vs a 517 zone   (a fixed 286px stage)

The fix was per-dialog layout work, not another shell mode: the auto-fit panel
from the first sweep already made overflow *scroll* instead of clip, and these
tests are about the six pages no longer needing that scroll at the sizes the
popups actually open at. Each layout choice is pinned here because each one is
the kind of number a later edit could quietly put back.

THIRD SWEEP (same week, two more user screenshots): the Keyboard Tester's key
board was sheared on its right-hand side — it was sized from the popup WINDOW
width, so its canvas asked for 976px inside a 932px content zone and the
packer cut the overflow off — and the Auto-Pilot's profile picker rendered
"Gam…" for the same reason (it shared the switch's row and got 55px of the
218px it asked for), with its old single-line headline sheared at both ends.
Those two are pinned at the bottom of this file, plus the close-list rebuild
("Add app" is what the user asked for; see test_game_night_custom_apps.py).

Side-effect free: source/text contracts plus one pure geometry check. No
samplers, no audio, no WMI, no subprocess. Tk is skipped when headless.
"""
from __future__ import annotations

import inspect

import pytest

tk = pytest.importorskip("tkinter")


def _src(module_name):
    import importlib

    return inspect.getsource(importlib.import_module(module_name))


# --------------------------------------------------------------- Quick Tools
def test_quick_tools_tiles_stay_one_line_short():
    """10 tiles must fit a 535px zone: the tile pitch is what decides it.

    Was: 46px minimum + a 12pt label with 10px of vertical padding = a 72px
    pitch, so the 10-item "Windows Settings" column alone asked for 720px.
    """
    src = _src("app.ui.dialogs.quick_tools_dialog")
    assert "min_height=32" in src, (
        "Quick Tools tiles must keep the compact minimum height")
    assert "text=label, font=(F, 10)" in src, (
        "tiles must keep the one-line label size")
    assert "text=label, font=(F, 12)" not in src, (
        "a 12pt tile label re-inflates the row pitch past the content zone")
    assert "pady=10" not in src, (
        "10px of label padding per side is what made a 72px row pitch")


# ------------------------------------------------------------ Mouse Tester
def test_mouse_tester_artwork_is_flexible_not_fixed_372():
    src = _src("app.ui.dialogs.mouse_tester_dialog")
    assert "_ART_REQ_H" in src, (
        "the mouse canvas must request a small height and expand into the "
        "leftover zone; a fixed 372px canvas was 16px more than the zone had")
    assert "_art_scale" in src, (
        "the artwork must scale to the canvas it is given")
    assert "expand=True" in src, "the canvas must be allowed to grow"


# ---------------------------------------------------------- Gamepad Tester
def test_gamepad_artwork_size_is_derived_and_subdivides_the_asset():
    src = _src("app.ui.dialogs.gamepad_dialog")
    assert "_ART_PX = 300" in src, (
        "a 400px pad plus the rows around it was 660px against a 535px zone")
    assert "self._SCALE = self._ART_PX / 300.0" in src, (
        "the overlay scale must follow the artwork size, not be a constant")
    assert ".subsample(3, 3)" not in src, (
        "the artwork must be subsampled to _ART_PX, not hard-coded to /3")


def test_gamepad_control_rows_are_single_line():
    src = _src("app.ui.dialogs.gamepad_dialog")
    assert 'box = tk.Frame(row, bg=COLORS["bg"])' not in src, (
        "the rumble sliders went back to a title stacked over the track; each "
        "slider is 62px tall that way and the row alone overflows the zone")
    assert 'column=3, sticky="e"' in src, (
        "Re-zero belongs in the stats strip's row (its own row cost 35px)")


# --------------------------------------------------------- Hardware Monitor
def test_hardware_monitor_uses_a_three_column_grid():
    src = _src("app.ui.dialogs.hardware_monitor_dialog")
    assert "for c in (0, 1, 2):" in src, (
        "the card grid must be 3 columns: 2x3 was 672px against a 535px zone")
    assert "for r in (0, 1):" in src, "the card grid must be 2 rows"
    assert 'grid.pack(fill="both", expand=True' in src, (
        "the grid should fill the zone so the cards grow with a tall window")


def test_hardware_monitor_captions_are_width_fitted():
    src = _src("app.ui.dialogs.hardware_monitor_dialog")
    assert "_fit_card_text" in src, (
        "device names are longer than a 3-column card: the caption must be "
        "measured and ellipsized rather than cut at the card edge")
    assert "_set_card_text(card[\"name\"]" in src, (
        "_paint_card must route the name through the width-fitted setter")


# ------------------------------------------------------------- Auto-Pilot
def test_pilot_is_two_columns_not_one_stack():
    src = _src("app.ui.dialogs.pilot_dialog")
    assert 'uniform="pilcol"' in src, (
        "Pilot must lay its cards out in two uniform columns; one stacked "
        "column was 616px against a 517px zone")
    assert 'sticky="new"' in src, (
        "the columns must not stretch their cards to each other's height")


def test_pilot_close_list_is_one_line_rows_that_size_their_box():
    src = _src("app.ui.dialogs.pilot_dialog")
    assert "_CLOSE_HINTS" in src, (
        "the close-list rows are one line: the full sentence lives in the "
        "row tooltip and the hint must stay short")
    assert 'text = tk.Frame(row, bg=COLORS["bg_alt"])' not in src, (
        "the close list went back to two-line rows (6 x 64px in a 150px box, "
        "i.e. barely two rows visible and the rest scrolled)")
    assert "panel.config(" in src and "winfo_reqheight()" in src, (
        "the close box must be sized to what it holds so it does not scroll")
    assert "_CLOSE_LABELS" in src, (
        "rows whose built-in label is too wide for its hint on one line keep "
        "a shorter display name (the full one stays in the tooltip)")


def test_pilot_profile_picker_has_its_own_line():
    """USER FIX 2026-10: the profile picker shared the switch's row and was
    handed 55px of the 218px it asked for, so the profile rendered "Gam…"."""
    src = _src("app.ui.dialogs.pilot_dialog")
    assert 'combo.pack(side="left", fill="x", expand=True' in src, (
        "the profile picker must stretch across the card, not live on the "
        "leftover of the switch's row")
    assert 'tk.Label(mrow, text="Profile"' not in src, (
        "Profile moved to its own row; sharing that row is what squeezed the "
        "picker")


def test_pilot_text_lines_are_width_fitted_not_sheared():
    """A centred Label handed less width than its text loses glyphs at BOTH
    ends, which is how "plus yours" came back as "/atching … your"."""
    src = _src("app.ui.dialogs.pilot_dialog")
    assert "_fit_label" in src and "ellipsize" in src, (
        "the watchlist headline and supporting line must be measured and "
        "ellipsized rather than sheared")
    assert 'f"Watching {n_known} built-in games plus yours"' not in src, (
        "the single line that overflowed the two-column card must stay gone")
    assert "_fit_close_row" in src, (
        "a narrow close row must drop its hint rather than shear it")


def test_keyboard_canvas_fills_the_zone_and_rescales():
    """USER FIX 2026-10: the board was sized from the popup WINDOW width and
    packed without fill, so at the normal size it asked for a 976px canvas in
    a 932px zone and the packer sheared the right end off Bksp/Enter/Shift."""
    src = _src("app.ui.dialogs.keyboard_tester_dialog")
    assert 'self._cv.pack(fill="x"' in src, (
        "the canvas must fill the content zone, not float at its own width")
    assert "_on_canvas_resize" in src and "_ROWS_UNITS" in src, (
        "the board must be rebuilt from the width the canvas is actually "
        "given")
    assert "int(self.body_width(0)) - 12" not in src, (
        "sizing the board from the window width is the bug that was fixed")
    assert "15.2 * self._UNIT_W" not in src, (
        "the canvas width must not come from a unit size derived from the "
        "window")


# --------------------------------------------------------------- Speed Test
def test_speed_test_stage_and_dial_are_short_enough():
    src = _src("app.ui.dialogs.speed_test_dialog")
    assert "height=236" in src, (
        "the fixed stage was 286px; with the dial at 200px the page came to "
        "530px against a 517px zone")
    assert "height=156" in src, "the dial itself must come down with it"
