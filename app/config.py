"""
Central place for app metadata, window sizing, and the color theme.
"""

APP_NAME = "Cleaner Tool"
APP_SHORT_NAME = "Cleaner Tool"
APP_VERSION = "2.0.0"
WINDOW_SIZE = "800x620"
WINDOW_MIN_SIZE = (780, 560)

# Windows 11 dark theme — professional, neutral, higher contrast
COLORS = {
    "bg": "#202020",
    "bg_alt": "#272727",
    "bg_widget": "#1a1a1a",
    "surface": "#2d2d2d",
    "surface_hover": "#353535",
    "text": "#f0f0f0",
    "subtext": "#9d9d9d",
    "accent_green": "#4cc2a0",
    "accent_teal": "#60cdff",
    "accent_blue": "#60cdff",
    "accent_yellow": "#ffb86a",
    "accent_red": "#ff7b7b",
    "accent_mauve": "#cba6f7",
    "black": "#111111",
}

FONT_FAMILY = "Segoe UI"
MONO_FONT_FAMILY = "Consolas"

RISK_SAFE = "SAFE"
RISK_ADVANCED = "ADVANCED"
RISK_REBOOT = "REBOOT REQUIRED"
