"""Cleaner Tool — GUI compatibility facade.

This module used to be the whole interface: 20,391 lines holding the theme,
the hand-drawn widget set, the Win32 interop, 22 feature dialogs, 4 tab pages
and the application shell. It is now 121 lines that re-export the public
surface, so nothing outside the `app` package had to change when the layers
moved out (H4/H5 widgets+theme+hardware, H12 dialogs, H13 tabs, H14 the shell).

The layering, bottom-up:

    app/ui/theme.py     palette, fonts, colour blending, geometry
    app/ui/hardware/    drive sampling                     (Tk-free)
    app/ui/widgets/     one widget per module
    app/ui/base.py      ThemedModal, the scrim, Tooltip, themed message boxes
    app/ui/dialogs/     one feature dialog per module
    app/ui/tabs/        one tab page per module
    app/ui/runflow.py   the run engine, composed into Application
    app/ui/traycontrols.py  the tray menu, composed into Application
    app/ui/app.py       Application: the composition root

The rule the split enforces: nothing below `base` imports anything above it,
and a widget never imports a dialog. The two exceptions are both pre-existing
and documented at their definition sites — PilotDialog opens
_CatalogPickerDialog, and InstallTab opens InstallProfilesDialog.

app/gui.py stays as the public facade: app/__main__.py launches through
`app.gui.launch`, and the re-exports below keep every name the old monolithic
module exposed reachable from one place after the layers moved out.
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
# Explicit re-exports, NOT a wildcard: `from ... import *` makes static
# analysis give up on this module, which is how three undefined names shipped
# in v1 (audit 2026-10, B14). Adding a dialog means adding it here too.
from app.ui.dialogs import (  # noqa: F401
    AppUpdateDialog, DnsTesterDialog, DriverStoreDialog,
    DriveToolkitDialog,
    DuplicatesDialog,
    FileLockDialog, GameServerPingDialog, HardwareMonitorDialog,
    InstallProfilesDialog,
    NetworkConnectionsDialog, PilotDialog, ProcessManagerDialog,
    RestorePointDialog, SafetyCenterDialog, ScorecardDialog, ServicesDialog,
    SpecsDialog, SpeedTestDialog, StartupManagerDialog,
    StorageInsightDialog, UninstallProgramsDialog, UpdatePickerDialog,
    _CatalogPickerDialog,
)
from app.ui.tabs import (  # noqa: F401
    AdminGateFrame, HealthTab, InstallTab, TaskTab, ToolsTab,
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
    get_tweak_state, load_config, mark_tweak_applied, mark_tweak_reverted,
)
from app.elevation import is_admin, relaunch_as_admin  # noqa: F401
from app.toast import (  # noqa: F401
    notify_clean_complete, show_toast,
)
from app.warnings import check_dangerous_combos, check_info_notices  # noqa: F401
from app.runner import CancellationToken  # noqa: F401
from app.runner.task_runner import invoke_task  # noqa: F401


def launch():
    """Entry point used by app/__main__.py and the frozen exe: build the Tk
    root, hand it to Application, and run the event loop. Imported lazily so
    that `--auto-clean` / `--auto-update` headless runs never pay to import Tk
    (M16)."""
    import tkinter as tk
    root = tk.Tk()
    # UI DENSITY (2026-10): the Settings page owns the choice, so read it
    # before the first widget exists. "system" is the historical default (Tk's
    # 96-DPI 1.333 looks small on a 100% display, so the floor raises it to
    # 125%); an explicit 100..200 forces that density instead, including below
    # the floor. This also grows the window via device_scale and the icons via
    # winfo_fpixels, so the whole surface stays proportional.
    from app.dpi import apply_ui_density
    try:
        ui_scale = str(load_config().get("ui_scale", "system"))
    except Exception:
        ui_scale = "system"        # a broken config must never block launch
    apply_ui_density(root, ui_scale)
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
    # dialogs (explicit - see the import above)
    "AppUpdateDialog",
    "DnsTesterDialog",
    "DriverStoreDialog",
    "DriveToolkitDialog", "DuplicatesDialog", "FileLockDialog",
    "GameServerPingDialog",
    "HardwareMonitorDialog",
    "InstallProfilesDialog", "NetworkConnectionsDialog",
    "PilotDialog", "ProcessManagerDialog", "RestorePointDialog",
    "SafetyCenterDialog", "ScorecardDialog", "ServicesDialog",
    "SpecsDialog", "SpeedTestDialog",
    "StartupManagerDialog", "StorageInsightDialog", "UninstallProgramsDialog",
    "UpdatePickerDialog", "_CatalogPickerDialog",
    # tabs
    "AdminGateFrame", "HealthTab", "InstallTab", "TaskTab", "ToolsTab",
    # misc re-exports the suite and callers expect
    "COLORS", "APP_NAME", "FONT_FAMILY", "WINDOW_SIZE", "WINDOW_MIN_SIZE",
    "RISK_SAFE", "RISK_ADVANCED", "RISK_REBOOT", "TABS", "PRESETS",
    "TAB_NAMES", "IS_WINDOWS", "TaskContext", "TaskSkipped", "TaskCancelled",
    "format_bytes", "get_tweak_state", "mark_tweak_applied",
    "mark_tweak_reverted", "is_admin", "relaunch_as_admin", "show_toast",
    "notify_clean_complete", "check_dangerous_combos",
    "check_info_notices", "CancellationToken", "invoke_task",
    "_TAB_STATUS",
]
