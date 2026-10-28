"""Theme primitives: palette, fonts, geometry helpers, colour blending.

H4 (architecture hardening, blueprint B1.1). Extracted verbatim from
app/gui.py, which was 20,391 lines and mixed the theme, the widget library,
the Win32 interop, 22 dialogs and the application shell in one module.

This is the bottom of the UI layer: it imports only app.config (the palette
source of truth) plus tkinter for font measurement, so widgets, dialogs and
the app shell can all depend on it without depending on each other.

Nothing here changed behaviourally. The functions are byte-identical to the
copies that were removed from gui.py.
"""
from __future__ import annotations

import tkinter.font as tkfont

# The palette keeps living in app.config — this is a re-export, not a second
# definition, so `from app.ui.theme import COLORS` and
# `from app.config import COLORS` are the same object.
from app.config import COLORS, FONT_FAMILY, MONO_FONT_FAMILY, device_scale

TAB_ACCENTS = {
    "Clean": COLORS["accent_sky"],
    "Repair": COLORS["accent_yellow"],
    "Tweak": COLORS["accent_blue"],
    "Install": COLORS["accent_green"],
    # Tools (user-approved 5th tab): pink was the palette's reserved spare
    # accent — distinct from all four existing tabs at a glance, and warm/
    # energetic for a power-tools page. Mauve was rejected as too close
    # to Tweak's violet.
    "Tools": COLORS["accent_pink"],
    # Drivers (2026-10, 6th/7th tab). Orange: the one hue no other tab uses.
    # Mauve was rejected (too close to Tweak's violet), teal (too close to
    # Install's mint), red is reserved for danger.
    "Drivers": COLORS["accent_orange"],
    # Settings is a utility surface, not a mode you switch into. It used to
    # be neutral, but the page itself used Tools' pink for Save and its rail
    # selection, so the nav item and the page disagreed (G-005). Mauve is the
    # one palette hue no other destination owns.
    "Settings": COLORS["accent_mauve"],
    # Health (2026-10): teal was the palette's remaining unused hue, and it
    # reads as "measurement" rather than as a mode. It is deliberately not
    # green: green is the Install accent and also the colour of a PASS, which
    # would make the tab itself look like a result.
    "Health": COLORS["accent_teal"],
}

# Letter grade -> colour, shared by every surface that shows a health grade
# (the Health tab's cards and ring, and its checkup history). ONE table: a
# grade drawn green on the tab and yellow in the history would read as two
# different results for the same score.
GRADE_COLORS = {
    "A": COLORS["accent_green"],
    "B": COLORS["accent_teal"],
    "C": COLORS["accent_yellow"],
    "D": COLORS["accent_orange"],
    "F": COLORS["accent_red"],
    "?": COLORS["muted"],
}

# DESIGN AUDIT (2026-10): this module used to carry its OWN copy of
# `_TAB_STATUS`, byte-identical to the one in app/ui/app.py, and nothing
# imported it -- app/gui.py re-exported the app.py original. Two tables keyed
# by tab name that must agree is exactly the kind of duplication that drifts,
# so ONE copy now lives here, at the bottom of the layering, where both the
# app shell (app/ui/app.py, which re-exports it for app/gui.py) and the tab
# pages (app/ui/tabs/tasktab.py) can read it without either importing the
# other. Importing it from app/ui/app.py instead would be a cycle: the shell
# already imports TaskTab.
#
# Keyed by tab name, exactly like TAB_ACCENTS above, so the two per-tab
# presentation tables sit together and cannot drift out of sync.
#
# TEXT-002 (design audit 2026-09-27): per-tab status-bar copy. Before this,
# the shell set ONE static string at startup ("Ready. Pick a preset and press
# Run.") and never re-evaluated it per tab, so the Install and Tools tabs --
# which have no preset cards and no Run button -- instructed the user to
# perform an action that does not exist on screen. Resolved on every tab
# switch in Application._show_tab.
# DECLUTTER 2026-10: the idle hints are empty. "Pick a preset to begin." sat
# in the most-watched strip of the window on every launch, next to four other
# things, to say what the preset cards already say by being the only thing on
# the page. The row now speaks only when something is happening or has just
# happened. The table stays (one key per tab) so callers need not change.
_TAB_STATUS = {
    "Clean": "",
    "Repair": "",
    "Tweak": "",
    "Install": "",
    "Tools": "",
    "Health": "",
}

F = FONT_FAMILY

# Monospace face for paths, ports and addresses (Consolas on Windows).
FM = MONO_FONT_FAMILY

# ---------------------------------------------------------------------------
# Material Design 3 foundations (redesign): shape scale, state layers and
# the type scale. Widgets read these instead of carrying their own numbers,
# so the whole app moves together.
# ---------------------------------------------------------------------------

# Shape scale (corner radii in px).
SHAPE_XS = 4      # extra small  - tooltips, snackbars
SHAPE_S = 8       # small        - chips, small fields
SHAPE_M = 12      # medium       - cards
SHAPE_L = 16      # large        - large cards, FAB
SHAPE_XL = 28     # extra large  - dialogs, nav pills, sheets

# Semantic radius roles. Call sites must use these names, never a literal:
# a component that picks its own radius is how an app ends up with 12, 14, 16,
# 24 and 28 around surfaces that are meant to be siblings (DESIGN G-002).
RADIUS_CONTROL = SHAPE_S        # fields, dropdowns, small chips
RADIUS_CARD = SHAPE_M           # list rows, cards, tiles
RADIUS_SURFACE = SHAPE_L        # panels, tool rows, scroll faces
RADIUS_WINDOW = SHAPE_XL        # dialog cards, the page panel
#: The OS window's own corner. It is smaller than RADIUS_WINDOW on purpose:
#: the window mask lives in the shell and is a different surface from a card.
RADIUS_CHROME = SHAPE_L
#: A full pill. Any value at or above half the shortest side is clamped by
#: `_round_rect_points`, so a pill is a ROLE, not a per-widget measurement.
RADIUS_PILL = 999
#: Text fields and dropdowns: the small shape, so every field in the app
#: shares its corners with the control it sits beside.
RADIUS_FIELD = SHAPE_S

# ---------------------------------------------------------------------------
# Surface roles. These are semantic on purpose: a card must never be the same
# tone as the control the user clicks. `app.config` keeps the raw values;
# call sites read these names so moving one rung moves every surface that
# shares the job (DESIGN G-002).
# ---------------------------------------------------------------------------
SURFACE_CARD = COLORS["bg_alt"]
#: A recessed reading surface (log wells, progress tracks). The palette has
#: no darker container than the page, so a well is the page with the hairline
#: outline the owning widget draws; the name keeps that intent explicit.
SURFACE_WELL = COLORS["bg"]
#: The tonal container a selected row/field paints with.
SURFACE_SELECTED = COLORS["secondary_container"]


# ---------------------------------------------------------------------------
# Spacing scale (px). A 4px grid, exposed by ROLE rather than by number so
# two pages cannot disagree about their gutter (DESIGN G-003). Anything that
# needs a gap picks the nearest role, not `padx=26`. Optical sub-rung values
# live in app/ui/metrics.py with component-level names, not as extra rungs.
# ---------------------------------------------------------------------------
SPACE_1 = 4
SPACE_2 = 8
SPACE_3 = 12
SPACE_4 = 16
SPACE_5 = 24
SPACE_6 = 32

PAGE_GUTTER = SPACE_5           # left/right edge of every tab page
STACK_GAP = SPACE_3             # between stacked blocks

# Semantic spacing. These are the roles a PAGE and a DIALOG read, so a
# rhythm change reaches every destination at once instead of one file at a
# time (DESIGN G-001). All are on the 4px grid.
PAGE_TOP = SPACE_4              # first block below the app bar
PAGE_BOTTOM = SPACE_3           # last block above the status band
SECTION_GAP = SPACE_3           # between two page sections

#: The popup body's insets, in the same 4px scale as every other gap.
#:
#: Every dialog body used to choose its own: some added 18px on both sides,
#: some none at all, one padded its content to 14px. The shared footer was
#: the only block that always held one line (18px, (12, 10)), so the body now
#: derives from that line too, on the scale instead of beside it:
#:
#:   DIALOG_PAD_X      left/right, inside the body frame
#:   DIALOG_PAD_TOP    first block below the title strip
#:   DIALOG_PAD_BOTTOM last block above the card's bottom edge
#:
#: The footer packs at (0, DIALOG_PAD_TOP) and no bottom padding: the body's
#: bottom inset is what keeps its controls off the card edge.
DIALOG_PAD_X = SPACE_4          # 16
DIALOG_PAD_TOP = SPACE_3        # 12
DIALOG_PAD_BOTTOM = SPACE_2     # 8
CARD_INSET = SPACE_4            # inside a card


# Icon ladder (px). One size per role, so an app icon is drawn at the same
# size everywhere it appears (DESIGN G-005).
ICON_INLINE = 16
ICON_CHIP = 20
ICON_CONTROL = 24
ICON_ROW = 32
ICON_NAV = 36
ICON_HERO = 44
#: The two card marks. They were 52 and 40 at their call sites; naming them
#: keeps the card sizes deliberate and stops a third value appearing (G-006).
ICON_CARD_PRESET = 52
ICON_CARD_TOOL = 40
#: A composite mark that is neither a card nor a general control glyph, named
#: so the speed-test usage row cannot drift to a third size. The ladder's
#: ordering above is unchanged.
ICON_USAGE = 28


# Frame period (ms) for the animated controls. It is the one tempo the app
# paints at; a widget that needs to count time derives its ticks from this
# rather than inventing a second clock (DESIGN G-006).
FRAME_MS = 16

# State-layer opacities (M3 spec): a translucent overlay of the content colour
# painted over the container. Tk has no alpha, so the overlay is pre-blended.
STATE_HOVER = 0.08
STATE_FOCUS = 0.10
STATE_PRESS = 0.10
STATE_DRAG = 0.16
DISABLED_CONTENT = 0.38
DISABLED_CONTAINER = 0.12

# Type scale. Tk sizes are points (M3 sp * 0.75 at 100% scaling); Tk can only
# do regular/bold, so M3's 500 weight maps to bold and 400 to regular.
TYPE = {
    "display_small":   (F, 27),
    "headline_medium": (F, 21),
    "headline_small":  (F, 18),
    # Semantic roles. These are what call sites should read: a screen title,
    # a dialog title and a card title are design decisions, "(F, 14, bold)"
    # written at a call site is not (DESIGN G-001). Values are the sizes the
    # app already renders, so adopting a token never changes a pixel.
    "screen_title":    (F, 18, "bold"),
    "dialog_title":    (F, 16, "bold"),
    "banner_title":    (F, 14, "bold"),
    "card_title":      (F, 11, "bold"),
    "row_title":       (F, 10, "bold"),
    "caption":         (F, 9),
    "micro":           (F, 8, "bold"),
    "button_large":    (F, 10, "bold"),
    "title_large":     (F, 16),
    "title_medium":    (F, 11, "bold"),
    "title_small":     (F, 10, "bold"),
    "body_large":      (F, 11),
    "body_medium":     (F, 10),
    "body_small":      (F, 9),
    "label_large":     (F, 10, "bold"),
    "label_medium":    (F, 9, "bold"),
    "label_small":     (F, 8, "bold"),
    # --- stage 3 additions ------------------------------------------- #
    # Every tuple the app still wrote by hand, named for the job it does.
    # Values are unchanged, so adopting one cannot move a pixel; the point
    # is that a READOUT and a DIALOG TITLE cannot drift into each other by
    # accident just because both happened to be 16pt.
    "micro_regular":   (F, 8),
    #: The small label above a headline that already names the surface. A
    #: popup that draws its own headline uses this in the chrome strip so the
    #: window's name is a LABEL and the headline is the only title.
    "overline":        (F, 9, "bold"),
    #: The one section-label role. It is `overline`'s twin, named for the job
    #: so a page cannot invent a third treatment for "what starts here".
    "section":         (F, 9, "bold"),
    "body_xlarge":     (F, 12),
    "subtitle":        (F, 12, "bold"),
    "link":            (F, 9, "bold", "underline"),
    "link_quiet":      (F, 9, "underline"),
    # Numbers the user is meant to read from across the room: a speed, a
    # drive score, a microphone level. One ladder, largest first.
    "metric_hero":     (F, 34),
    "metric_large":    (F, 26, "bold"),
    "metric_medium":   (F, 22, "bold"),
    "metric":          (F, 18, "bold"),
    "metric_small":    (F, 16, "bold"),
    "readout_large":   (F, 14, "bold"),   # a stat cell's value
    "readout":         (F, 15),
    "readout_bold":    (F, 15, "bold"),
    "readout_small":   (F, 13, "bold"),
    # Ornament glyphs: disclosure chevrons, separators and the mark an empty
    # state centres on. Not text: a glyph gets its own rung so nobody has to
    # describe a chevron as a "subtitle" to find its size.
    "chevron":         (F, 13, "bold"),
    "chevron_small":   (F, 11, "bold"),
    "bullet":          (F, 12, "bold"),
    "glyph_large":     (F, 22),        # an empty state's decorative glyph
    # Monospace, for paths and registry values.
    "mono":            (FM, 9),
    "mono_small":      (FM, 8),
    "mono_large":      (FM, 13),      # the run-history sparkline
}

#: Emoji family. Emoji glyphs are a SEPARATE FAMILY from the UI face: Segoe UI
#: has no colour emoji and Tk does not fall back per glyph, so a label showing
#: an emoji has to name this family explicitly or it renders a box.
EF = "Segoe UI Emoji"

#: Emoji sizes, paired with the icon ladder they sit beside (ICON_INLINE 16,
#: ICON_CHIP 20, ICON_CONTROL 24, ICON_ROW 32, ICON_NAV 36, ICON_HERO 44).
EMOJI = {
    "inline": (EF, 9),      # a glyph inside a sentence or row
    "badge":  (EF, 14),     # the shield/badge next to a title
    "button": (EF, 16),     # inside a control
    "header": (EF, 18),     # a banner glyph
    "hero":   (EF, 28),     # the one big glyph on an empty or gate screen
}


def tonal(accent: str, base: str = None, amount: float = 0.16) -> str:
    """A low-emphasis container tinted with `accent` (M3 tonal surface)."""
    return _hex_lerp(base or COLORS["bg_alt"], accent, amount)

# Icon metrics live here, not at each call site (user request: "I would like
# things to be more symmetrical in my app as far as sizes and positions").
#
# The Install catalog drew its row icons at 32px while the Uninstall and
# Startup popups drew theirs at 18px, so the same app looked like two
# different apps depending on which surface you were on. One constant, so the
# next time anyone tunes an icon size it cannot drift apart again per-file.
#: Kept for the five popups that already import it by this name; the ladder
#: above is the canonical spelling for new code.
ICON_ROW_PX = ICON_ROW


def icon_bg_rgb(color: str) -> tuple[int, int, int]:
    """A theme color as an (r, g, b) tuple for the PPM icon extractors.

    `icon_extract` / `icon_resolve` composite an icon onto an opaque
    background rectangle. That background MUST be the exact color of the
    widget the icon is drawn on, or the rectangle shows up as a visible
    square plate around the icon.

    That is not hypothetical: the Uninstall popup hardcoded (30, 32, 36),
    which matches neither COLORS["bg"] (13, 17, 23) nor COLORS["bg_alt"]
    (21, 27, 36), so every program icon sat on a mismatched square. Passing
    the row's own theme color through here makes that class of bug
    impossible to reintroduce by hand-editing a literal.
    """
    h = (color or "").lstrip("#")
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    try:
        return (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16))
    except (ValueError, IndexError):
        return (0, 0, 0)

def _lerp(a, b, t):
    # clamp t: spring overshoot can push t past 1.0, producing colors
    # outside 00-FF (invalid Tk color names) and off-canvas coords
    t = max(0.0, min(1.0, t)) if isinstance(t, float) else t
    return a + (b - a) * t


def _parse_hex_colour(value):
    """`#rrggbb` -> (r, g, b), or None for anything Tk cannot paint.

    The 2026-10 log had a real crash from a caller passing an empty string
    into the blend (`invalid literal for int() with base 16: ''`) — an accent
    lookup that returned "" took the whole Tools grid down at startup. A
    colour helper must never be able to do that, so every caller now gets
    either the blended colour or the documented fallback below.
    """
    try:
        s = str(value or "").strip()
        if s.startswith("#"):
            s = s[1:]
        if len(s) == 3:
            s = "".join(ch * 2 for ch in s)
        if len(s) != 6:
            return None
        return (int(s[0:2], 16), int(s[2:4], 16), int(s[4:6], 16))
    except (TypeError, ValueError):
        return None


def _hex_lerp(c1, c2, t):
    """Blend two #rrggbb colors. t is clamped to [0, 1] — spring overshoot
    can pass 1.0 and would otherwise produce >FF channel values, which Tk
    rejects as invalid color names.

    A malformed colour is treated as the OTHER colour (so a missing accent
    degrades to the surface it was blending with rather than to black), and
    two malformed colours return `COLORS["bg"]`. Never raises: this is called
    on hover/paint paths all over the app, including from event callbacks
    where Tk would swallow the exception and leave a half-painted widget."""
    try:
        t = max(0.0, min(1.0, float(t)))
    except (TypeError, ValueError):
        t = 0.0
    a = _parse_hex_colour(c1)
    b = _parse_hex_colour(c2)
    if a is None and b is None:
        return COLORS["bg"]
    if a is None:
        return "#%02x%02x%02x" % b
    if b is None:
        return "#%02x%02x%02x" % a
    r1, g1, b1 = a
    r2, g2, b2 = b
    return f"#{int(_lerp(r1, r2, t)):02x}{int(_lerp(g1, g2, t)):02x}{int(_lerp(b1, b2, t)):02x}"


def _round_rect_points(x0, y0, x1, y1, r, steps=5):
    """Point list for a rounded rectangle (for Tk smooth polygons, which
    ARE antialiased on Windows — unlike create_rectangle).

    The radius is clamped to half the shortest side. That is what makes
    RADIUS_PILL a real role: a caller can ask for a full pill without knowing
    the control's height, and a mistaken 24 on a 20px field degrades to a
    semicircle instead of drawing self-intersecting corners.
    """
    import math
    r = max(0.0, min(float(r), (x1 - x0) / 2.0, (y1 - y0) / 2.0))
    pts = []
    corners = [(x1 - r, y0 + r, -90), (x1 - r, y1 - r, 0),
               (x0 + r, y1 - r, 90), (x0 + r, y0 + r, 180)]
    for cx, cy, start in corners:
        for i in range(steps + 1):
            a = math.radians(start + 90 * i / steps)
            pts += [cx + r * math.cos(a), cy + r * math.sin(a)]
    return pts

def _measure_font(root, font):
    """F7: ONE tkfont.Font per (root, font spec), cached on the root.

    _measure used to create + destroy a throwaway tk.Label per call just to
    ask the font how wide/tall the text is — and config_text runs on every
    run-count change, so All On/All Off paid one widget create/destroy +
    redraw per row. Font objects are cheap to keep and die with their root,
    so the cache lives ON the root object (a harness that destroys and
    recreates roots can never serve a stale handle)."""
    cache = getattr(root, "_measure_fonts", None)
    if cache is None:
        cache = root._measure_fonts = {}
    f = cache.get(font)
    if f is None:
        f = cache[font] = tkfont.Font(root=root, font=font)
    return f

def ellipsize(text, font, room):
    """`text` cut to fit `room` px in `font`, with a trailing ellipsis.

    One implementation, because this problem has three call sites and they
    must agree: ToolRow row titles, every Tools card title, and the preset
    card titles. Before this existed, a too-long label was handled by nothing
    - pack simply gave the Label less width than it asked for and Tk clipped
    the glyphs. Because a Label's default anchor is CENTER, that clipped the
    text at BOTH ends, so "Duplicate & Large Files" rendered as
    "plicate & Large File" with the D sheared off, which reads as a
    rendering fault rather than as truncation.

    `font` may be a live tkfont.Font or any spec tuple. Returns `text`
    unchanged when it already fits, so the common case costs one measure()
    call and no visible change. Fails soft to the untruncated string.
    """
    text = str(text or "")
    try:
        if not text:
            return ""
        room = int(room)
        if room < 24:
            return text          # not laid out yet; show it all
        measure = (font.measure if hasattr(font, "measure")
                   else tkfont.Font(font=font).measure)
        if measure(text) <= room:
            return text
        while text and measure(text + "…") > room:
            text = text[:-1]
        return (text + "…") if text else "…"
    except Exception:
        return text


def _round_pts(x0, y0, x1, y1, r):
    return [
        x0 + r, y0, x1 - r, y0, x1, y0, x1, y0 + r,
        x1, y1 - r, x1, y1, x1 - r, y1, x0 + r, y1,
        x0, y1, x0, y1 - r, x0, y0 + r, x0, y0,
    ]

# module-level transparent-color dispenser: two dialogs can't both use the
# same key color if one is ever stacked over the other's corner pixels
_TRANS_USED = set()
_TRANS_PALETTE = ("#010203", "#020304", "#030405", "#040506", "#050607")


def ui_scale(widget=None):
    """Tk's device scale for `widget`: 1.0 at 96 DPI, ~1.33 at 125%, 2.0 at
    200%.

    Fonts scale with this automatically; `PhotoImage` icons do not. A fixed
    subsample factor therefore left every generated icon physically smaller
    on exactly the high-DPI screens that need it larger, which the 2026-10
    audit called out as a visual bug (a 24px icon beside 18pt text).

    Clamped to a sane range: a nonsense Tk answer must not ask for a 0-px or
    a 4000-px icon. The implementation lives in app.config.device_scale so
    window sizing and icon sizing cannot disagree about the display scale.
    """
    return device_scale(widget)


def icon_target_px(base_px, widget=None):
    """The physical pixel size a `base_px` icon should render at.

    Callers still have to snap to an integer `subsample` factor; this is the
    target they snap to, so the same drawer produces 36px icons at 100% and
    the asset's full 72px at 200% instead of 24px at every scale.
    """
    try:
        base = int(base_px)
    except (TypeError, ValueError):
        base = 24
    return max(1, int(round(base * ui_scale(widget))))


def scaled_photo(photo, target):
    """Resize a PhotoImage to `target` px using only Tk's integer operations.

    Tk has `zoom` and `subsample`, both integer-only. A single subsample
    factor cannot reach every DPI target: at 200% a 24px base needs 48px,
    and 72/2 = 36 is the closest a bare subsample gets, which is why icons
    stayed visibly small beside doubled text. Trying small integer zoom /
    subsample pairs reaches 72*2/3 = 48 exactly, and 45px approximately.

    Returns the original when it is already the target size. Callers keep the
    result alive (a Label stores a name, not the image).
    """
    try:
        target = max(1, int(target))
        w, h = int(photo.width()), int(photo.height())
    except Exception:
        return photo
    if w <= 0 or h <= 0:
        return photo
    best = None
    for zoom in (1, 2, 3):
        for sub in (1, 2, 3, 4, 5, 6):
            nw = w * zoom // sub
            if nw < 1:
                continue
            err = abs(nw - target)
            # On a tie prefer the LARGER mark: at 125% a 30px target can be
            # reached by 24 or 36, and the smaller choice reads as an icon
            # that forgot to grow beside its label.
            if (best is None or err < best[0]
                    or (err == best[0] and nw > best[3])):
                best = (err, zoom, sub, nw)
    if best is None:
        return photo
    _err, zoom, sub, _nw = best
    out = photo
    if zoom > 1:
        out = out.zoom(zoom, zoom)
    if sub > 1:
        out = out.subsample(sub, sub)
    return out


def _next_transparent_color():
    try:
        for c in _TRANS_PALETTE:
            if c not in _TRANS_USED:
                _TRANS_USED.add(c)
                return c
        return "#060708"
    except Exception:
        return "#010203"


def _release_transparent_color(c):
    try:
        _TRANS_USED.discard(c)
    except Exception:
        pass
