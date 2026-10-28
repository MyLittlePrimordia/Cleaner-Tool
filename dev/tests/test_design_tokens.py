"""The design tokens are the contract; these pin the contract itself.

The 2026-10 audit found a full M3 scale defined at the bottom of the UI and
almost nothing reading it: 411 hand-written font tuples, 28 radius literals
and five different corner values. A token only helps if it cannot be quietly
redefined per call site or per widget, so this file asserts the mapping, the
order and the defaults the shared primitives actually use.

The other half - failing a call site that writes a NUMBER where these tokens
already have a name - is one gate now, not four scans buried in this file:
`dev/tools/design_literals.py`, gated by `dev/tests/test_design_literals.py`, covers
radius, spacing, icon sizes and font tuples in one report.
"""
from __future__ import annotations

import inspect
import pathlib

from app.ui import base, metrics, theme
from app.ui.dialogs._tool_page import StatusLine
from app.ui.widgets.button import AnimatedButton
from app.ui.widgets.header import DISC
from app.ui.widgets.rounded_card import RADIUS as CARD_RADIUS
from app.ui.widgets.scroll import ScrollableRoundedPanel


def test_radius_roles_are_the_shape_scale():
    assert theme.RADIUS_CONTROL == theme.SHAPE_S == 8
    assert theme.RADIUS_CARD == theme.SHAPE_M == 12
    assert theme.RADIUS_SURFACE == theme.SHAPE_L == 16
    assert theme.RADIUS_WINDOW == theme.SHAPE_XL == 28
    # The three roles that used to be per-widget literals (14/16/24).
    assert theme.RADIUS_FIELD == theme.SHAPE_S == 8
    assert theme.RADIUS_CHROME == theme.SHAPE_L == 16
    assert theme.RADIUS_PILL > theme.SHAPE_XL


def test_surface_roles_name_the_elevation_ladder():
    # SURFACE_CARD is the role the widgets read; the palette key behind it is
    # the one thing it may equal. (SURFACE_WELL and SURFACE_SELECTED name the
    # two roles with no reader yet; they are vocabulary, and they live in the
    # dead-code report's written-reason table rather than in an assertion that
    # would look like a call site.)
    assert theme.SURFACE_CARD == theme.COLORS["bg_alt"]
    assert theme.SURFACE_CARD != theme.COLORS["bg"]


def test_the_semantic_spacing_roles_sit_on_the_grid():
    for role in (theme.PAGE_GUTTER, theme.PAGE_TOP, theme.PAGE_BOTTOM,
                 theme.SECTION_GAP, theme.CARD_INSET, theme.STACK_GAP):
        assert role % 4 == 0, role
    assert theme.PAGE_GUTTER == theme.SPACE_5 == 24


def test_the_pill_radius_is_clamped_to_half_the_shortest_side():
    # RADIUS_PILL is a request, not a measurement; the helper is what makes
    # it safe on a 36px field and on a 56px one.
    pts = theme._round_rect_points(0, 0, 200, 40, theme.RADIUS_PILL)
    ys = pts[1::2]
    xs = pts[0::2]
    assert min(xs) >= -0.01 and max(xs) <= 200.01
    assert min(ys) >= -0.01 and max(ys) <= 40.01


def test_optical_component_metrics_keep_their_pixel_values():
    expected = {
        "HAIRLINE_NUDGE": 1, "TEXT_NUDGE": 2, "TIGHT_GAP": 3,
        "COMPACT_INSET": 5, "CONTROL_GAP": 6, "LABEL_GAP": 7,
        "ROW_PAD": 9, "GROUP_GAP": 10, "FIELD_PAD": 11, "CHIP_PAD": 13,
        "CARD_BODY_INSET": 14, "PAGE_INSET": 18, "SECTION_BREAK": 20,
        "HERO_OFFSET": 22, "INDENT": 30, "MEASURE_CAP": 70,
    }
    assert {name: getattr(metrics, name) for name in expected} == expected


def test_every_shared_surface_uses_a_role_not_a_number():
    assert base.ThemedModal.RADIUS == theme.RADIUS_WINDOW
    assert CARD_RADIUS == theme.RADIUS_CARD
    assert ScrollableRoundedPanel.RADIUS == theme.RADIUS_SURFACE
    # ToolRow asks RoundedCard for RADIUS_SURFACE; if someone reintroduces a
    # literal this stays green only while the value happens to match, which
    # is exactly why the call site reads the token.
    assert theme.RADIUS_SURFACE is theme.SHAPE_L


def test_spacing_is_a_4px_grid_with_one_page_gutter():
    scale = [theme.SPACE_1, theme.SPACE_2, theme.SPACE_3, theme.SPACE_4,
             theme.SPACE_5, theme.SPACE_6]
    assert scale == sorted(scale)
    assert all(v % 4 == 0 for v in scale)
    assert theme.PAGE_GUTTER == theme.SPACE_5 == 24
    # The dialog body and its footer are the same column.
    assert theme.DIALOG_PAD_X == theme.CARD_INSET == 16
    assert theme.DIALOG_PAD_TOP == theme.SECTION_GAP == theme.STACK_GAP == 12


def test_semantic_type_roles_exist_and_keep_their_pixel_values():
    # Adopting a token must never silently resize existing text, so each
    # semantic role matches the tuple the call sites used before.
    assert theme.TYPE["banner_title"] == (theme.F, 14, "bold")
    assert theme.TYPE["card_title"] == (theme.F, 11, "bold")
    assert theme.TYPE["row_title"] == (theme.F, 10, "bold")
    assert theme.TYPE["caption"] == (theme.F, 9)
    assert theme.TYPE["label_large"] == (theme.F, 10, "bold")
    assert theme.TYPE["button_large"] == (theme.F, 10, "bold")
    # The hierarchy the app renders, largest to smallest.
    assert (theme.TYPE["screen_title"][1] > theme.TYPE["dialog_title"][1]
            > theme.TYPE["banner_title"][1] > theme.TYPE["card_title"][1]
            > theme.TYPE["row_title"][1] > theme.TYPE["caption"][1]
            > theme.TYPE["micro"][1])


#: Every font tuple the app wrote by hand before stage 3, mapped to the role
#: that replaced it. The values must be IDENTICAL: the sweep was a rename, so
#: adopting a role cannot move a pixel. Roles that legitimately share a value
#: (a row title and a small label are both 10pt bold) are listed once.
LEGACY_TO_ROLE = {
    (theme.F, 8): "micro_regular",
    (theme.F, 8, "bold"): "label_small",
    (theme.F, 9): "caption",
    (theme.F, 9, "bold"): "label_medium",
    (theme.F, 9, "bold", "underline"): "link",
    (theme.F, 9, "underline"): "link_quiet",
    (theme.F, 10): "body_medium",
    (theme.F, 10, "bold"): "label_large",
    (theme.F, 11): "body_large",
    (theme.F, 11, "bold"): "card_title",
    (theme.F, 12): "body_xlarge",
    (theme.F, 12, "bold"): "subtitle",
    (theme.F, 13, "bold"): "readout_small",
    (theme.F, 14, "bold"): "readout_large",
    (theme.F, 15): "readout",
    (theme.F, 15, "bold"): "readout_bold",
    (theme.F, 16): "title_large",
    (theme.F, 16, "bold"): "metric_small",
    (theme.F, 18): "headline_small",
    (theme.F, 18, "bold"): "metric",
    (theme.F, 22, "bold"): "metric_medium",
    (theme.F, 26, "bold"): "metric_large",
    (theme.F, 27): "display_small",
    (theme.F, 34): "metric_hero",
    (theme.FM, 9): "mono",
    (theme.FM, 8): "mono_small",
    (theme.FM, 13): "mono_large",
}


def test_every_legacy_font_tuple_kept_its_value():
    for literal, role in LEGACY_TO_ROLE.items():
        assert theme.TYPE[role] == literal, role


def test_the_scale_is_ordered_by_size_where_it_claims_to_be():
    # The numeric ladder: each metric role is bigger than the next one down.
    ladder = ["metric_hero", "metric_large", "metric_medium", "metric",
              "metric_small", "readout_large", "readout_small"]
    sizes = [theme.TYPE[r][1] for r in ladder]
    assert sizes == sorted(sizes, reverse=True), sizes
    # Glyph ornaments are separate from text roles on purpose.
    assert theme.TYPE["chevron"][1] > theme.TYPE["chevron_small"][1]


def test_the_emoji_family_is_explicit():
    # Segoe UI has no colour emoji and Tk does not fall back per glyph.
    assert theme.EF == "Segoe UI Emoji"
    for name, font in theme.EMOJI.items():
        assert font[0] == theme.EF, name
        assert isinstance(font[1], int), name


def test_every_type_token_keeps_the_family_it_declares():
    """A size has no business switching family: the type scale is the only
    place a role picks its face, so `ui_font` (a second adapter nothing
    called) is gone and a dynamic size reads its role instead."""
    for role, spec in theme.TYPE.items():
        assert spec[0] in (theme.F, theme.FM, theme.EF), role


def test_every_type_token_used_in_the_app_exists():
    """A typo in a role name would only raise when that widget is built."""
    import re

    used, used_emoji = set(), set()
    for path in pathlib.Path("app").rglob("*.py"):
        src = path.read_text(encoding="utf-8")
        used |= set(re.findall(r'TYPE\["([a-z_]+)"\]', src))
        used_emoji |= set(re.findall(r'EMOJI\["([a-z_]+)"\]', src))
    assert used, "no TYPE token is used at all"
    assert not (used - set(theme.TYPE)), sorted(used - set(theme.TYPE))
    assert not (used_emoji - set(theme.EMOJI)), sorted(
        used_emoji - set(theme.EMOJI))


def test_emoji_family_is_not_written_by_hand():

    offenders = []
    for path in pathlib.Path("app").rglob("*.py"):
        if path.name == "theme.py":
            continue
        for n, line in enumerate(path.read_text(encoding="utf-8")
                                 .splitlines(), 1):
            if '"Segoe UI Emoji"' in line:
                offenders.append("%s:%d %s" % (path, n, line.strip()[:80]))
    assert not offenders, "\n  ".join(offenders)


def test_icon_ladder_is_ordered_and_back_compatible():
    ladder = [theme.ICON_INLINE, theme.ICON_CHIP, theme.ICON_CONTROL,
              theme.ICON_ROW, theme.ICON_NAV, theme.ICON_HERO]
    assert ladder == sorted(ladder)
    assert theme.ICON_ROW_PX == theme.ICON_ROW == 32
    # The composite mark that is not a general control glyph.
    assert theme.ICON_USAGE == 28


def test_one_tempo_for_every_animated_widget():
    """A widget does not invent its own frame rate: it counts FRAME_MS."""
    from app.ui.widgets import chrome
    assert chrome.BorderlessChrome.FADE_STEP_MS == theme.FRAME_MS


def test_the_kit_reads_the_tokens():
    # Spot checks that the primitives were switched over, not copied.
    assert DISC == theme.ICON_HERO
    assert 'TYPE["label_large"]' in inspect.getsource(
        AnimatedButton.__init__)


def test_status_line_caption_uses_the_caption_role():
    # The component itself is only constructible with Tk; assert the source
    # intent instead of a widget instance so this stays headless-safe.
    src = inspect.getsource(StatusLine.__init__)
    assert 'TYPE["caption"]' in src


def test_tool_page_stack_gap_is_the_shared_token():
    from app.ui.dialogs import _tool_page
    assert _tool_page.GAP == theme.STACK_GAP


def test_the_palette_keeps_aa_contrast_for_body_text():
    """Guard the palette while it is being refactored.

    Measured before the makeover: text/bg 14.47, subtext/bg 10.90,
    muted/bg 5.83, muted/surface 4.53. AA for normal text is 4.5:1.
    """
    def lum(hex_color):
        h = hex_color.lstrip("#")
        vals = [int(h[i:i + 2], 16) / 255 for i in (0, 2, 4)]
        vals = [(v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4)
                for v in vals]
        return 0.2126 * vals[0] + 0.7152 * vals[1] + 0.0722 * vals[2]

    def ratio(a, b):
        la, lb = lum(a), lum(b)
        hi, lo = max(la, lb), min(la, lb)
        return (hi + 0.05) / (lo + 0.05)

    colors = theme.COLORS
    assert ratio(colors["text"], colors["bg"]) >= 7
    assert ratio(colors["subtext"], colors["bg"]) >= 4.5
    assert ratio(colors["muted"], colors["surface"]) >= 4.5
    for accent in ("accent_green", "accent_yellow", "accent_orange",
                   "accent_pink", "accent_blue", "accent_sky"):
        assert ratio(colors["black"], colors[accent]) >= 4.5, accent
