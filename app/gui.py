"""Cleaner Tool — GUI compatibility facade.

This module used to be the whole interface: 20,391 lines holding the theme,
the hand-drawn widget set, the Win32 interop, 22 feature dialogs, 4 tab pages
and the application shell. It is now 265 lines that re-export the public
surface, so nothing outside the `app` package had to change when the layers
moved out (H4/H5 widgets+theme+hardware, H12 dialogs, H13 tabs, H14 the shell).

The layering, bottom-up:

    app/ui/theme.py     palette, fonts, colour blending, geometry
    app/ui/hardware/    XInput, MMDevice, capture, drives   (Tk-free)
    app/ui/widgets/     one widget per module
    app/ui/base.py      ThemedModal, the scrim, Tooltip, themed message boxes
    app/ui/dialogs/     one feature dialog per module
    app/ui/tabs/        one tab page per module
    app/ui/app.py       Application: the composition root

The rule the split enforces: nothing below `base` imports anything above it,
and a widget never imports a dialog. The two exceptions are both pre-existing
and documented at their definition sites — PilotDialog opens
_CatalogPickerDialog, and InstallTab opens InstallProfilesDialog.

app/gui.py stays because tools/smoke_gui.py imports ~40 names from it through
`__import__("app.gui", ...)`. That contract is pinned by
tools/check_architecture.py, which fails the build if any of these names stops
resolving.
"""
from app.ui.theme import (  # noqa: F401
    COLORS, F, TAB_ACCENTS, _hex_lerp, _lerp, _measure_font, _round_pts,
    _round_rect_points,
)
from app.ui.base import (  # noqa: F401
    ThemedModal, Tooltip, _ModalScrim, _themed_askyesno, _themed_showinfo,
)
from app.ui.widgets import (  # noqa: F401
    AnimatedButton, AnimatedProgressBar, GaugeDial, RoundedEntry,
    ScrollableRoundedPanel, SegmentedTabs, ToggleSwitch,
)
from app.ui.widgets.chrome import (  # noqa: F401
    BorderlessChrome, _enable_dark_titlebar, _set_window_icon,
)
from app.ui.hardware.drives import (  # noqa: F401
    _drive_gains, _snapshot_all_drives, _system_drive_root,
)
from app.ui.hardware.xinput import (  # noqa: F401
    _XINPUT_BUTTONS, _XINPUT_RUMBLE_MS, _XINPUT_STICK_DEADZONE,
    _XINPUT_TRIGGER_THRESHOLD, _xinput_rumble, _xinput_state_fn,
)
from app.ui.hardware.mmdev import (  # noqa: F401
    _MM_COM_INIT, _MM_STATE_NAMES, _InputMeter, _PadReportStats, _mm_guid,
    _mm_vfn,
)
from app.ui.hardware.capture import _capture_inputs, _default_capture_id  # noqa: F401
from app.ui.dialogs import *  # noqa: F401,F403
from app.ui.tabs import (  # noqa: F401
    AdminGateFrame, InstallTab, TaskTab, ToolsTab,
)
from app.ui.app import Application, _TAB_STATUS  # noqa: F401
from app.config import (  # noqa: F401
    APP_NAME, FONT_FAMILY, RISK_ADVANCED, RISK_REBOOT, RISK_SAFE,
    WINDOW_MIN_SIZE, WINDOW_SIZE,
)
from app.tab_presets import PRESETS, TAB_NAMES, TABS  # noqa: F401
from app.utils import (  # noqa: F401
    IS_WINDOWS, TaskCancelled, TaskContext, TaskSkipped, format_bytes,
)
from app.config_persist import (  # noqa: F401
    get_tweak_state, mark_tweak_applied, mark_tweak_reverted,
)
from app.elevation import is_admin, relaunch_as_admin  # noqa: F401
from app.toast import (  # noqa: F401
    notify_clean_complete, notify_low_space, show_toast,
)
from app.warnings import check_dangerous_combos, check_info_notices  # noqa: F401
from app.runner import CancellationToken  # noqa: F401
from app.runner.task_runner import invoke_task  # noqa: F401
from app.tools_shortcuts import TOOLS_SHORTCUTS  # noqa: F401


def launch():
    """Entry point used by app/__main__.py and the frozen exe: build the Tk
    root, hand it to Application, and run the event loop. Imported lazily so
    that `--auto-clean` / `--auto-update` headless runs never pay to import Tk
    (M16)."""
    import tkinter as tk
    root = tk.Tk()
    Application(root)
    root.mainloop()


__all__ = [
    "Application", "launch",
    # theme
    "COLORS", "F", "TAB_ACCENTS", "_lerp", "_hex_lerp", "_round_rect_points",
    "_round_pts", "_measure_font",
    # base
    "ThemedModal", "Tooltip", "_ModalScrim", "_themed_askyesno",
    "_themed_showinfo",
    # widgets
    "AnimatedButton", "AnimatedProgressBar", "GaugeDial", "RoundedEntry",
    "ScrollableRoundedPanel", "SegmentedTabs", "ToggleSwitch",
    "BorderlessChrome", "_enable_dark_titlebar", "_set_window_icon",
    # hardware
    "_drive_gains", "_snapshot_all_drives", "_system_drive_root",
    "_xinput_state_fn", "_xinput_rumble", "_XINPUT_BUTTONS",
    "_XINPUT_RUMBLE_MS", "_XINPUT_STICK_DEADZONE", "_XINPUT_TRIGGER_THRESHOLD",
    "_InputMeter", "_PadReportStats", "_mm_guid", "_mm_vfn", "_MM_STATE_NAMES",
    "_MM_COM_INIT", "_capture_inputs", "_default_capture_id",
    # tabs
    "AdminGateFrame", "InstallTab", "TaskTab", "ToolsTab",
    # misc re-exports the suite and callers expect
    "COLORS", "APP_NAME", "FONT_FAMILY", "WINDOW_SIZE", "WINDOW_MIN_SIZE",
    "RISK_SAFE", "RISK_ADVANCED", "RISK_REBOOT", "TABS", "PRESETS",
    "TAB_NAMES", "IS_WINDOWS", "TaskContext", "TaskSkipped", "TaskCancelled",
    "format_bytes", "get_tweak_state", "mark_tweak_applied",
    "mark_tweak_reverted", "is_admin", "relaunch_as_admin", "show_toast",
    "notify_clean_complete", "notify_low_space", "check_dangerous_combos",
    "check_info_notices", "CancellationToken", "invoke_task",
    "TOOLS_SHORTCUTS", "_TAB_STATUS",
]
