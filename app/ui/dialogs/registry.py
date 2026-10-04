"""One declarative table for the 20 feature dialogs, plus one generic opener.

Why this exists. `Application` grew twenty `_open_*` methods that were, with
two exceptions, the same three lines of code:

    def _open_dns_tester(self):
        '''...a docstring explaining the busy-guard decision...'''
        try:
            DnsTesterDialog(self.root, self).wait()
        except Exception:
            pass

Twenty copies of that is 168 lines whose only real information is the pair
(dialog class, does this one need a busy-guard). The information belongs in a
table; the repetition belonged nowhere.

The two exceptions are the reason this is a table and not a loop over bare
class names:

  * `storage_insight` refuses to open while a run owns the progress UI, and
    says so in the status bar instead of silently doing nothing;
  * `quick_tools` takes a third constructor argument resolved from the Tools
    tab, and must NOT open at all when that target is missing.

Both are data (`guard_busy`, `extra_arg`) rather than control flow, which is
what keeps the opener generic.

Layering: this module lives INSIDE app/ui/dialogs/ so the architecture guard
still applies to it — it may import its own layer and anything below, and
must not reach up into app.ui.tabs or app.ui.app. That is why it takes the
Application as an argument instead of importing it.

Contract preservation: every `Application._open_*` name survives as a
three-line delegating forwarder, because tools/smoke/*.py and several dialogs
call them by name. Nothing outside this module changed shape.
"""

from __future__ import annotations

from collections import namedtuple

from app.ui.dialogs.dns_tester_dialog import DnsTesterDialog
from app.ui.dialogs.drive_toolkit_dialog import DriveToolkitDialog
from app.ui.dialogs.game_server_ping_dialog import GameServerPingDialog
from app.ui.dialogs.gamepad_dialog import GamepadDialog
from app.ui.dialogs.hardware_monitor_dialog import HardwareMonitorDialog
from app.ui.dialogs.keyboard_tester_dialog import KeyboardTesterDialog
from app.ui.dialogs.mic_check_dialog import MicCheckDialog
from app.ui.dialogs.monitor_test_dialog import MonitorTestDialog
from app.ui.dialogs.mouse_tester_dialog import MouseTesterDialog
from app.ui.dialogs.pilot_dialog import PilotDialog
from app.ui.dialogs.process_manager_dialog import ProcessManagerDialog
from app.ui.dialogs.quick_tools_dialog import QuickToolsDialog
from app.ui.dialogs.speaker_test_dialog import SpeakerTestDialog
from app.ui.dialogs.specs_dialog import SpecsDialog
from app.ui.dialogs.speed_test_dialog import SpeedTestDialog
from app.ui.dialogs.startup_manager_dialog import StartupManagerDialog
from app.ui.dialogs.storage_insight_dialog import StorageInsightDialog
from app.ui.dialogs.uninstall_programs_dialog import UninstallProgramsDialog
from app.ui.dialogs.webcam_dialog import WebcamDialog

#: Returned by an `extra_arg` resolver to mean "do not open this dialog".
ABORT = object()

DialogSpec = namedtuple("DialogSpec", "cls guard_busy extra_arg")
DialogSpec.__new__.__defaults__ = (False, None)


def _quick_tools_target(app):
    """Quick Tools needs the Tools tab's launch target, and opens nothing
    without one (the tab may not be built yet)."""
    tools_tab = app.tabs.get("Tools")
    target = getattr(tools_tab, "_open_target", None)
    return ABORT if target is None else target


#: key -> spec. The key is what Application's forwarders pass through.
DIALOGS = {
    # --- guarded: refuses to open while a run owns the progress UI -------
    "storage_insight": DialogSpec(StorageInsightDialog, guard_busy=True),

    # --- needs a third ctor arg from the Tools tab ------------------------
    "quick_tools": DialogSpec(QuickToolsDialog, extra_arg=_quick_tools_target),

    # --- plain -----------------------------------------------------------
    "dns_tester": DialogSpec(DnsTesterDialog),
    "process_manager": DialogSpec(ProcessManagerDialog),
    "pilot": DialogSpec(PilotDialog),
    "speed_test": DialogSpec(SpeedTestDialog),
    "hw_monitor": DialogSpec(HardwareMonitorDialog),
    "gamepad_tester": DialogSpec(GamepadDialog),
    "mic_check": DialogSpec(MicCheckDialog),
    "game_server_ping": DialogSpec(GameServerPingDialog),
    "keyboard_tester": DialogSpec(KeyboardTesterDialog),
    "monitor_test": DialogSpec(MonitorTestDialog),
    "speaker_test": DialogSpec(SpeakerTestDialog),
    "drive_toolkit": DialogSpec(DriveToolkitDialog),
    "startup_manager": DialogSpec(StartupManagerDialog),
    "uninstall_programs": DialogSpec(UninstallProgramsDialog),
    "webcam_test": DialogSpec(WebcamDialog),
    "mouse_tester": DialogSpec(MouseTesterDialog),
    "pc_specs": DialogSpec(SpecsDialog),
}

_BUSY_MESSAGE = ("Busy \u2014 wait for the current task to finish "
                 "(or press \u2715 Stop).")


def open_dialog(app, key):
    """Open the dialog registered under `key`. Returns it, or None.

    Never raises: a dialog that fails to build must not take the app down,
    which is why all twenty openers swallowed their exception individually.
    """
    spec = DIALOGS.get(key)
    if spec is None:
        return None

    if spec.guard_busy:
        try:
            with app._busy_lock:
                busy = app._busy
        except Exception:
            busy = False
        if busy:
            try:
                app.set_status(_BUSY_MESSAGE)
            except Exception:
                pass
            return None

    try:
        if spec.extra_arg is not None:
            extra = spec.extra_arg(app)
            if extra is ABORT:
                return None
            dialog = spec.cls(app.root, app, extra)
        else:
            dialog = spec.cls(app.root, app)
        dialog.wait()
        return dialog
    except Exception:
        return None