"""
Central place for app metadata, window sizing, and the color theme.

Phase 2: single dark-navy theme (user asked for dark only). Accent colors
identify tabs: blue = Clean, amber = Repair, purple = Tweak, green = Install,
red = Undo. All keys from the old palette are kept so existing modules
(task dialogs, admin gate, scheduler UI) keep working unchanged.
"""

APP_NAME = "Cleaner Tool"
APP_VERSION = "2.0.0"

# --------------------------------------------------------------------------
# Window sizing
# --------------------------------------------------------------------------
# The window used to open at a hardcoded "1040x800". That number is in
# PHYSICAL pixels, so its apparent size depended entirely on the machine:
# on a 2560x1440 display it covered 41% of the width, while the same 1040
# on a 1366x768 laptop filled three quarters of it. Two 1440p users with
# different scaling got visibly different apps from the same build.
#
# Worse, the *test suite* never agreed with either. The DPI opt-in lives in
# app/__main__.py at import time, and the smoke tests import app.gui
# directly -- so every automated run rendered DPI-unaware (tk scaling 1.33)
# while both the source launcher and the frozen .exe render DPI-aware
# (tk scaling 2.0). The tests were validating a layout no user ever saw.
#
# The fix is to stop guessing an absolute size and derive it from the work
# area instead, which is what actually determines how big the window can
# usefully be. compute_window_size() below is PURE -- it takes numbers and
# returns numbers -- so the clamping behaviour is unit-testable without a
# display.

# Fallback when the work area cannot be read (headless, or a locked-down Tk).
# It is a PHYSICAL size: the fallback path does not see a display, so it can
# never be multiplied by a density scale. It therefore carries the default
# density itself -- 1360x960 is the old 1280x900 footprint seen through the
# 125% UI density (app/dpi.py), so a failed work-area read still opens a
# correctly proportioned window instead of a cramped one.
WINDOW_DEFAULT_SIZE = (1360, 960)
# Same value as before, kept as a string because app/gui.py re-exports it
# and the smoke suite imports it by name.
WINDOW_SIZE = "%dx%d" % WINDOW_DEFAULT_SIZE
# The floor the resize grips clamp to (app/ui/widgets/chrome.py). Also the
# floor compute_window_size clamps up to, so a tiny screen still opens at
# something usable rather than something unusable.
WINDOW_MIN_SIZE = (980, 700)
# Ceiling, in 96-DPI logical pixels (dpi_window_size multiplies it by the
# display scale). Without one, a 4K display would open a 3000px-wide window
# that nobody can use. It used to be 1280x900, i.e. the exact size of the
# layout it caps -- so every large display rendered the same small window
# and the extra room the display offers was never used. 1600x1000 is the
# widest the pages are actually designed for (Install's two-column catalog
# and Tools' three-column grid both stop growing past it) and it only binds
# on a 4K-class work area; 1080p and 1440p are decided by the fraction.
WINDOW_MAX_SIZE = (1600, 1000)
# Fraction of the work area to occupy. Width is the tighter constraint on
# the Install/Tools tab, so it is allowed to be greedier than the height.
# Raised from (0.78, 0.82) with the density step: a default that uses a fifth
# of the screen for desktop had the shell reading as a utility panel on a
# 1080p desktop while its pages -- which are laid out for a wide catalog
# grid -- scrolled. 0.82/0.86 still leaves a visible desktop strip all round.
WINDOW_WORKAREA_FRACTION = (0.82, 0.86)
#: Desktop margin left around the default window, in physical pixels. The
#: scaled minimum (980x700 logical) can be taller than a small screen's work
#: area, which used to open the shell flush against the taskbar -- the one
#: arrangement that is indistinguishable from "maximized" even though the
#: window is not. 48px keeps the bottom edge (and the resize grip on it)
#: reachable and the window reading as a window.
WINDOW_WORKAREA_MARGIN = 48


def compute_window_size(work_w, work_h, fraction=WINDOW_WORKAREA_FRACTION,
                        minimum=WINDOW_MIN_SIZE, maximum=WINDOW_MAX_SIZE,
                        fallback=WINDOW_DEFAULT_SIZE):
    """Return the (width, height) the main window should open at.

    Pure function: no Tk, no globals mutated, no I/O. Given a work area it
    returns the size to use, so the clamping rules are unit-testable.

    The work area (screen minus taskbar) is the only input that actually
    predicts how much room the window has. Anything we would hardcode is a
    guess that is wrong on some machine.

    Rules, in order:
      1. a missing or non-positive work area falls back to `fallback`,
         so a headless run never crashes on a geometry call;
      2. scale by `fraction`, then clamp into [minimum, maximum];
      3. never return anything smaller than `minimum` -- a window the user
         cannot resize is a bug, not a small screen.
    """
    try:
        work_w = int(work_w)
        work_h = int(work_h)
    except (TypeError, ValueError):
        return tuple(fallback)
    if work_w <= 0 or work_h <= 0:
        return tuple(fallback)

    frac_w, frac_h = fraction
    width = int(work_w * float(frac_w))
    height = int(work_h * float(frac_h))

    min_w, min_h = minimum
    max_w, max_h = maximum
    width = max(min_w, min(max_w, width))
    height = max(min_h, min(max_h, height))
    return width, height


def device_scale(root=None):
    """Tk's device scale for `root`: 1.0 at 96 DPI, 2.0 at 200%.

    Window and dialog geometry is in pixels, but Tk fonts are in points and
    scale with the display. Sizing the shell from the *physical* work area
    therefore left a 200% display with a 100%-sized window and 200%-sized
    text: the app opened with its right column and footer off-screen. The
    sizing rules below are expressed in 96-DPI pixels and multiplied by this.

    Clamped to 1.0..3.0 for the same reason theme.ui_scale is: a nonsense
    Tk answer must not ask for a 0-px window. Falls back to 1.0 when Tk
    cannot answer, so a headless harness keeps its old geometry.
    """
    try:
        target = root
        if target is None:
            import tkinter as _tk
            target = _tk._default_root
        if target is None:
            return 1.0
        scale = float(target.winfo_fpixels("1i")) / 96.0
    except Exception:
        return 1.0
    if not (scale > 0):
        return 1.0
    return max(1.0, min(3.0, scale))


def _usable_px(work, margin=WINDOW_WORKAREA_MARGIN):
    """Physical pixels left after the desktop margin, never below 1.

    The margin shrinks on a work area too small to give it away (a quarter of
    the axis at most), so a degenerate 20px screen still yields a window
    rather than a negative one.
    """
    try:
        work = int(work)
    except (TypeError, ValueError):
        return 1
    if work <= 0:
        return 1
    return max(1, work - min(int(margin), work // 4))


def dpi_window_size(work_w, work_h, scale=1.0, **kwargs):
    """(width, height) in physical pixels for a 96-DPI sized window.

    `compute_window_size` is the layout contract: apply the work-area
    fraction, clamp into the min/max in 96-DPI pixels. This wrapper divides
    the physical work area by the display scale, runs that contract in
    logical pixels, then multiplies the result back up. The result is capped
    to the work area (less WINDOW_WORKAREA_MARGIN) because the minimum
    (980x700 logical) can be taller than a small screen's work area -- a
    window that hangs off the screen is worse than one 16px under its
    declared minimum, and one that opens flush against the taskbar is worse
    than one with a strip of desktop left around it.
    """
    try:
        work_w = int(work_w)
        work_h = int(work_h)
    except (TypeError, ValueError):
        return compute_window_size(work_w, work_h, **kwargs)
    if work_w <= 0 or work_h <= 0:
        return compute_window_size(work_w, work_h, **kwargs)
    try:
        scale = float(scale)
    except (TypeError, ValueError):
        scale = 1.0
    if not (scale > 0):
        scale = 1.0
    width, height = compute_window_size(work_w / scale, work_h / scale,
                                        **kwargs)
    # int(), not round(): Tk's answer for 96 DPI is occasionally a hair above
    # 96, and rounding 1280 * 1.0005 back up produced a 1281px window. A
    # sub-pixel truncation is invisible; a one-pixel size drift is not.
    return (min(_usable_px(work_w), max(1, int(width * scale))),
            min(_usable_px(work_h), max(1, int(height * scale))))


def resolve_window_size(root=None, default=None):
    """Ask Tk for the usable work area and return a "WxH" geometry string.

    Falls back to `default` (WINDOW_SIZE) whenever the work area cannot be
    determined, so this is always safe to call. `root` is the Tk root; when
    omitted the module-level default root is used.

    The work area is read from SPI_GETWORKAREA rather than screenwidth/
    screenheight because the latter includes the taskbar and the Windows
    taskbar is not always at the bottom -- a side or top taskbar would make
    the window hang off-screen.

    The result is DPI-scaled: see device_scale/dpi_window_size. That is what
    keeps a 200% display from opening a 1280x900 physical window around
    200%-sized text.
    """
    fallback = default or WINDOW_SIZE
    try:
        import ctypes
        import ctypes.wintypes

        target = root
        if target is None:
            import tkinter as _tk
            target = _tk._default_root
        if target is None:
            return fallback

        hwnd = ctypes.windll.user32.GetParent(target.winfo_id())
        if not hwnd:
            hwnd = target.winfo_id()
        rect = ctypes.wintypes.RECT()
        # 48 == SPI_GETWORKAREA
        ok = ctypes.windll.user32.SystemParametersInfoW(
            48, 0, ctypes.byref(rect), 0)
        if not ok:
            return fallback
        work_w = rect.right - rect.left
        work_h = rect.bottom - rect.top
        if work_w <= 0 or work_h <= 0:
            return fallback
        width, height = dpi_window_size(work_w, work_h, device_scale(target))
        return "%dx%d" % (width, height)
    except Exception:
        # A geometry string is a convenience, never a correctness
        # requirement -- failing to compute one must not stop the app.
        return fallback


def resolve_window_minimum(root=None, window_size=None):
    """The physical minsize for the window, scaled like its geometry.

    Tk enforces minsize in pixels. Leaving it at 980x700 physical would let
    a 200% window shrink to half its designed logical footprint. The result
    is capped at the window that was actually resolved, so a short work area
    (where the scaled minimum cannot fit) still gets a resizable window.
    """
    try:
        if window_size is None:
            window_size = resolve_window_size(root)
        win_w, win_h = (int(part) for part in str(window_size).split("x", 1))
    except Exception:
        win_w, win_h = WINDOW_MAX_SIZE
    scale = device_scale(root)
    return (min(win_w, max(1, int(round(WINDOW_MIN_SIZE[0] * scale)))),
            min(win_h, max(1, int(round(WINDOW_MIN_SIZE[1] * scale)))))

COLORS = {
    # ---------------------------------------------------------------------
    # Material Design 3 - dark scheme, Google blue seed.
    #
    # Key NAMES are unchanged on purpose: ~30k lines of tabs and dialogs read
    # these tokens, so re-mapping the VALUES to M3 colour roles re-skins every
    # surface at once. The role each token now plays is noted per line.
    #
    # Surfaces use M3's tonal elevation (lighter = higher) rather than shadow
    # or hairline tricks: surface < container-low < container < high < highest.
    # Values are the Google dark-theme neutrals (the ones Gmail / Drive /
    # Gemini ship), not the purple baseline.
    # ---------------------------------------------------------------------
    "bg":           "#131314",   # M3 surface
    "bg_alt":       "#1E1F20",   # M3 surface-container (cards, panels, rows)
    "bg_widget":    "#282A2C",   # M3 surface-container-high (log well, tracks)
    "surface":      "#282A2C",   # controls / chips / neutral buttons
    "surface_hover":"#333537",   # M3 surface-container-highest
    "hairline":     "#444746",   # M3 outline-variant (dividers, outlines)
    "muted":        "#8E918F",   # M3 outline (de-emphasised text, idle dots)
    # Alert containers (M3 error-container / tertiary-container, dark).
    "alert_red_bg":   "#601410",
    "alert_amber_bg": "#3D2E00",
    # Text: on-surface and on-surface-variant.
    "text":         "#E3E3E3",
    "subtext":      "#C4C7C5",
    # Text drawn ON a pastel accent fill (M3 on-primary family, tone 20).
    "black":        "#1F1F1F",
    # ---- M3 role tokens (new; used by the redesigned shell/widgets) ------
    "primary":                "#A8C7FA",   # Google blue 80
    "on_primary":             "#062E6F",
    "primary_container":      "#0842A0",
    "on_primary_container":   "#D3E3FD",
    "secondary_container":    "#004A77",   # nav-rail indicator / tonal button
    "on_secondary_container": "#C2E7FF",
    "surface_container_low":  "#1B1B1C",
    # App shell: window chrome, navigation rail and top app bar sit on this
    # tone; the page lives in a rounded "bg" panel set into it (the Gmail /
    # Drive / Gemini layout).
    "shell":                  "#1E1F20",
    "scrim":                  "#000000",
    # ---------------------------------------------------------------------
    # Accents: Google's tone-80 hues. Tab identity is kept (blue/yellow/
    # violet/green/...), expressed as M3 tonal colours instead of neon.
    # ---------------------------------------------------------------------
    "accent_green": "#6DD58C",   # Install
    "accent_teal":  "#7AE7DC",
    "accent_yellow":"#FDD663",   # Repair
    "accent_sky":   "#7FCFFF",   # Clean
    "accent_pink":  "#FFADD2",   # Tools
    "accent_blue":  "#A8C7FA",   # Tweak / primary
    "accent_mauve": "#D0BCFF",
    "accent_orange": "#FFB77C",  # Drivers
    "accent_red":   "#F28B82",   # Undo / danger / reboot badge
}

FONT_FAMILY = "Segoe UI"
MONO_FONT_FAMILY = "Consolas"

RISK_SAFE = "SAFE"
RISK_ADVANCED = "ADVANCED"
RISK_REBOOT = "REBOOT REQUIRED"
# Irreversible removal of data the user may still want (Recycle Bin, captures,
# uninstalled apps). Distinct from ADVANCED, which is reversible.
RISK_DATA_LOSS = "DATA LOSS"
