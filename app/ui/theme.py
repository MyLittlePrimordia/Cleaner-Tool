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
from app.config import COLORS, FONT_FAMILY

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
}

# TEXT-002 (design audit 2026-09-27): per-tab status-bar copy. Before this,
# Application set ONE static string at startup ("Ready. Pick a preset and
# press Run.") and never re-evaluated it per tab, so the Install and Tools
# tabs -- which have no preset cards and no Run button -- instructed the user
# to perform an action that does not exist on screen. Resolved on every tab
# switch in Application._show_tab.
#
# Keyed by tab name, exactly like TAB_ACCENTS above, so the two per-tab
# presentation tables sit together and cannot drift out of sync.
_TAB_STATUS = {
    "Clean": "Pick a preset to begin.",
    "Repair": "Pick a preset to begin.",
    "Tweak": "Pick a preset to begin.",
    "Install": "Select apps to install.",
    "Tools": "Choose a tool.",
}

F = FONT_FAMILY

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
