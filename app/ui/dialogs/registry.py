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

from app.ui.dialogs.app_update_dialog import AppUpdateDialog
from app.ui.dialogs.dns_tester_dialog import DnsTesterDialog
from app.ui.dialogs.file_lock_dialog import FileLockDialog
from app.ui.dialogs.drive_toolkit_dialog import DriveToolkitDialog
from app.ui.dialogs.duplicates_dialog import DuplicatesDialog
from app.ui.dialogs.game_server_ping_dialog import GameServerPingDialog
from app.ui.dialogs.gamepad_dialog import GamepadDialog
from app.ui.dialogs.hardware_monitor_dialog import HardwareMonitorDialog
from app.ui.dialogs.health_report_dialog import HealthReportDialog
from app.ui.dialogs.keyboard_tester_dialog import KeyboardTesterDialog
from app.ui.dialogs.mic_check_dialog import MicCheckDialog
from app.ui.dialogs.monitor_test_dialog import MonitorTestDialog
from app.ui.dialogs.mouse_tester_dialog import MouseTesterDialog
from app.ui.dialogs.network_connections_dialog import NetworkConnectionsDialog
from app.ui.dialogs.pilot_dialog import PilotDialog
from app.ui.dialogs.process_manager_dialog import ProcessManagerDialog
from app.ui.dialogs.quick_tools_dialog import QuickToolsDialog
from app.ui.dialogs.restore_point_dialog import RestorePointDialog
from app.ui.dialogs.run_history_dialog import RunHistoryDialog
from app.ui.dialogs.safety_center_dialog import SafetyCenterDialog
from app.ui.dialogs.update_picker_dialog import UpdatePickerDialog
from app.ui.dialogs.services_dialog import ServicesDialog
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
    # guard_busy: the health scan is read-only but it shells out to DISM and
    # a disk walk, which would compete with a running task for the same I/O
    # and report stale numbers mid-run. Refuse politely instead.
    "health_report": DialogSpec(HealthReportDialog, guard_busy=True),
    # Not guard_busy, unlike health_report: reading the TCP/UDP tables is a
    # few milliseconds of in-memory kernel work (measured ~8 ms for 91 TCP +
    # 54 UDP rows), with no disk walk and no subprocess, so there is nothing
    # to contend with a running task over.
    "network_connections": DialogSpec(NetworkConnectionsDialog),
    # Not guard_busy for the same reason: one PowerShell process that
    # reads the SCM and exits. ~385 ms, almost all of it PowerShell's own
    # start-up. Nothing to contend with a running task over.
    "services": DialogSpec(ServicesDialog),
    # NOT guard_busy, unlike health_report: Restart Manager answers in
    # ~70 ms and holds no handle on the target, so there is nothing to
    # contend with a running task over. The ACTIONS can need elevation,
    # but only when the user actually presses close on someone else's
    # process - and that path reports "try as administrator" itself
    # rather than silently escalating.
    "file_lock": DialogSpec(FileLockDialog),
    # NOT guard_busy: a duplicates scan can take a while on a big
    # folder, but it only READS file contents and writes nothing, so
    # running it beside a task is harmless. The removals are per-group
    # and go through the dialog's own confirm.
    "duplicates": DialogSpec(DuplicatesDialog),
    # NOT guard_busy: this only reads restore-point settings and shells
    # out to PowerShell; it competes with nothing. The elevated read is
    # per-action and behind its own UAC prompt, not an app relaunch.
    "restore_points": DialogSpec(RestorePointDialog),
    # NOT guard_busy: reading the safety nets is cheap and read-only, and the
    # one heavy action (Undo all) hands off to a real run, which has its own
    # busy gate. Opening it mid-run just shows stale-but-honest counts.
    "safety_center": DialogSpec(SafetyCenterDialog),
    # N-006: read-only history/trends; cheap to build and never writes.
    "run_history": DialogSpec(RunHistoryDialog),
    # Opened from PC Health: choose exactly which Windows updates to install.
    "update_picker": DialogSpec(UpdatePickerDialog),
    # U-006: opened from the "Update available" banner; fetch+verify in place
    # or hand off to the browser. guard_busy because it can replace the exe.
    "app_update": DialogSpec(AppUpdateDialog, guard_busy=True),
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

    dialog = None
    # B-005: snapshot the modal registry BEFORE construction. A constructor
    # that raises after super().__init__() registers its Toplevel in
    # _MODAL_OPEN (base.py) before the subclass body runs, so `dialog` is
    # still None here and there is nothing to close by name — the half-built
    # window has to be found by diffing the registry.
    before = None
    try:
        from app.ui.base import ThemedModal
        before = set(ThemedModal._MODAL_OPEN.keys())
    except Exception:
        before = None
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
    except Exception as exc:
        _cleanup_failed_dialog(dialog, before)
        # H-025: don't fail silently. Previously this swallowed the exception
        # outright, so a missing asset / import error / bad extra_arg made the
        # Tools click appear to do nothing at all. Leave a breadcrumb.
        try:
            app.log(f"! Could not open this tool: {exc}")
            import traceback
            app.log(traceback.format_exc().strip())
        except Exception:
            pass
        try:
            app.set_status("Could not open that tool \u2014 see the log.")
        except Exception:
            pass
        return None


def _cleanup_failed_dialog(dialog, before):
    """Destroy + deregister any modal left half-built by a failed constructor.

    B-005: a raised constructor leaves an alive-but-orphaned Toplevel in
    ThemedModal._MODAL_OPEN, which makes any_open() report a modal forever and
    permanently stalls Auto-Pilot apply / pre-warm / preflight. `dialog` may be
    None (the assignment never completed); `before` is the set of registry keys
    that existed prior, so any newer key is the orphan.
    """
    try:
        from app.ui.base import ThemedModal
    except Exception:
        return
    targets = []
    if dialog is not None:
        targets.append(dialog)
    if before is not None:
        for key, obj in list(ThemedModal._MODAL_OPEN.items()):
            if key not in before and obj not in targets:
                targets.append(obj)
    for obj in targets:
        # Deregister first so any_open() stops seeing it even if teardown fails.
        try:
            ThemedModal._MODAL_OPEN.pop(id(obj), None)
        except Exception:
            pass
        closed = False
        try:
            close = getattr(obj, "close", None)
            if callable(close):
                close()
                closed = True
        except Exception:
            closed = False
        if not closed:
            try:
                dlg = getattr(obj, "_dlg", None)
                if dlg is not None:
                    dlg.destroy()
            except Exception:
                pass