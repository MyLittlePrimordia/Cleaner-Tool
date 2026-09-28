"""The UI layer, split out of the 20,391-line app/gui.py.

Layering, bottom-up (H4/H5, blueprint B1):

    theme.py        palette, fonts, colour blending, rounded-rect geometry
    hardware/       XInput, MMDevice, capture pump, drive sampling  (Tk-free)
    widgets/        AnimatedButton, ToggleSwitch, SegmentedTabs, ... one per file
    base.py         ThemedModal, _ModalScrim, Tooltip
    (dialogs, tabs, app shell, and the app/gui.py facade land in later steps)

The rule the split enforces: a widget never imports a dialog, a dialog never
imports a tab, and nothing below `base` imports anything above it. The
measured coupling in the original file already satisfied this -- it was
simply invisible while everything shared one module.

app/gui.py remains the compatibility facade and re-exports every name the
smoke suite imports, so nothing outside this package has to change yet.
"""
from app.ui.theme import (  # noqa: F401
    COLORS,
    F,
    TAB_ACCENTS,
    _hex_lerp,
    _lerp,
    _measure_font,
    _round_pts,
    _round_rect_points,
)
from app.ui.base import ThemedModal, Tooltip, _ModalScrim  # noqa: F401
from app.ui.widgets import (  # noqa: F401
    AnimatedButton,
    AnimatedProgressBar,
    GaugeDial,
    RoundedEntry,
    ScrollableRoundedPanel,
    SegmentedTabs,
    ToggleSwitch,
)
from app.ui.hardware.drives import (  # noqa: F401
    _drive_gains,
    _snapshot_all_drives,
    _system_drive_root,
)
from app.ui.hardware.xinput import (  # noqa: F401
    _xinput_rumble,
    _xinput_state_fn,
)
from app.ui.hardware.mmdev import _InputMeter, _PadReportStats  # noqa: F401
from app.ui.hardware.capture import _capture_inputs  # noqa: F401

__all__ = [
    "COLORS", "F", "TAB_ACCENTS",
    "_lerp", "_hex_lerp", "_round_rect_points", "_round_pts", "_measure_font",
    "ThemedModal", "Tooltip", "_ModalScrim",
    "AnimatedButton", "AnimatedProgressBar", "GaugeDial", "RoundedEntry",
    "ScrollableRoundedPanel", "SegmentedTabs", "ToggleSwitch",
    "_system_drive_root", "_snapshot_all_drives", "_drive_gains",
    "_xinput_state_fn", "_xinput_rumble", "_InputMeter", "_PadReportStats",
    "_capture_inputs",
]
