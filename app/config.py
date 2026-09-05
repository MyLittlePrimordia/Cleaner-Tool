"""
Central place for app metadata, window sizing, and the color theme.

Phase 2: single dark-navy theme (user asked for dark only). Accent colors
identify tabs: blue = Clean, amber = Repair, purple = Tweak, green = Install,
red = Undo. All keys from the old palette are kept so existing modules
(task dialogs, admin gate, scheduler UI) keep working unchanged.
"""

APP_NAME = "Cleaner Tool"
APP_SHORT_NAME = "Cleaner Tool"
APP_VERSION = "2.0.0"
WINDOW_SIZE = "980x720"
WINDOW_MIN_SIZE = (920, 660)

COLORS = {
    # Base surfaces (dark navy)
    "bg": "#0D1117",            # window base
    "bg_alt": "#151B24",        # cards / panels
    "bg_widget": "#10141C",     # log well / progress track
    "surface": "#1C2430",       # buttons / controls
    "surface_hover": "#263041",
    "hairline": "#232B38",      # 1px borders
    # Text
    "text": "#EAEFF5",
    "subtext": "#7C8797",
    # Accents — tab identity + statuses
    "accent_green": "#3ED598",  # Clean (mint)
    "accent_teal": "#2EE6A8",   # bright mint (hover variant)
    "accent_yellow": "#F2B84B", # Repair (amber)
    "accent_sky": "#38BDF8",    # Install alt (sky blue — downloads/cloud feel)
    "accent_pink": "#F472B6",   # spare accent (tried for Install, unused)
    "accent_blue": "#7C6CF0",   # Tweak (violet)
    "accent_mauve": "#B48CFF",
    "accent_red": "#F2555F",    # Undo / danger / reboot badge
    "black": "#0B0E13",         # text on bright accent fills
}

FONT_FAMILY = "Segoe UI"
MONO_FONT_FAMILY = "Consolas"

RISK_SAFE = "SAFE"
RISK_ADVANCED = "ADVANCED"
RISK_REBOOT = "REBOOT REQUIRED"
