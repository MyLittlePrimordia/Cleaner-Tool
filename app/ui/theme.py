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

import tkinter as tk
import tkinter.font as tkfont

# The palette keeps living in app.config — this is a re-export, not a second
# definition, so `from app.ui.theme import COLORS` and
# `from app.config import COLORS` are the same object.
from app.config import COLORS, FONT_FAMILY, MONO_FONT_FAMILY

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
    # Settings is a utility surface, not a mode you switch into, so it is
    # deliberately neutral - the same reason the status-row glyphs are.
    "Settings": COLORS["subtext"],
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
    "title_large":     (F, 16),
    "title_medium":    (F, 11, "bold"),
    "title_small":     (F, 10, "bold"),
    "body_large":      (F, 11),
    "body_medium":     (F, 10),
    "body_small":      (F, 9),
    "label_large":     (F, 10, "bold"),
    "label_medium":    (F, 9, "bold"),
    "label_small":     (F, 8, "bold"),
}


def state_layer(container: str, content: str, opacity: float) -> str:
    """`container` with a `content`-coloured M3 state layer at `opacity`."""
    return _hex_lerp(container, content, opacity)


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
ICON_ROW_PX = 32


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


def _hex_lerp(c1, c2, t):
    """Blend two #rrggbb colors. t is clamped to [0, 1] — spring overshoot
    can pass 1.0 and would otherwise produce >FF channel values, which Tk
    rejects as invalid color names."""
    t = max(0.0, min(1.0, t))
    r1, g1, b1 = int(c1[1:3], 16), int(c1[3:5], 16), int(c1[5:7], 16)
    r2, g2, b2 = int(c2[1:3], 16), int(c2[3:5], 16), int(c2[5:7], 16)
    return f"#{int(_lerp(r1, r2, t)):02x}{int(_lerp(g1, g2, t)):02x}{int(_lerp(b1, b2, t)):02x}"


def _round_rect_points(x0, y0, x1, y1, r, steps=5):
    """Point list for a rounded rectangle (for Tk smooth polygons, which
    ARE antialiased on Windows — unlike create_rectangle)."""
    import math
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
    must agree: ToolRow row titles, the 24 Tools card titles, and the preset
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
