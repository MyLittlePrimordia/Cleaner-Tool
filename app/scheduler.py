"""
Windows Task Scheduler integration for automatic maintenance.
"""

import datetime
import os
import sys
import subprocess
from app.config_persist import load_config, save_config
from app.elevation import is_admin

TASK_NAME = "CleanerTool_AutoMaintenance"
TASK_DESC = "Cleaner Tool automatic maintenance run"


def _get_executable_and_args(extra_args=None):
    """Return (executable, arguments) for the current run. `extra_args`
    selects the headless mode: default --auto-clean, or --auto-update
    for the 'Update Everything' schedule option."""
    mode_args = extra_args if extra_args else ["--auto-clean"]
    if getattr(sys, "frozen", False):
        exe = sys.executable
        args = mode_args
    else:
        exe = sys.executable
        script = os.path.abspath(sys.argv[0])
        if not os.path.isfile(script):
            script = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "main.py"))
        args = [script] + mode_args
    return exe, args


def _build_schtasks_cmd(frequency, time_str, extra_args=None, today=None):
    """Build schtasks command to create the scheduled task.

    Returns a list suitable for subprocess.run(shell=False) to avoid quoting issues
    when the executable path contains spaces (e.g. C:\\Program Files\\...).

    `today` is injectable for tests; default = the day the schedule is
    enabled (see _schedule_day_args for the M4 /D pinning).

    H1 fix: /RL HIGHEST requires the CREATING process to already hold admin
    rights. The Auto Maintenance dialog is reachable from the default,
    non-elevated Limited Mode, so every non-admin user who tried to enable
    it got "ERROR: Access is denied" and nothing was ever scheduled. Fix:
    only request /RL HIGHEST when we're actually running elevated; a
    non-admin user gets a normal per-user scheduled task instead (still
    enough to run Clean/Repair/Tweak tasks that don't themselves require
    admin — the same tasks already gated by admin_required=True in the GUI
    would still need the app run elevated to do anything on that front).

    /IT ("only run if the user is logged on") is intentionally kept for
    both cases for now: removing it so the task can run while logged out
    requires storing the user's password with schtasks /RP, which is a
    real credential-storage decision we shouldn't make silently inside a
    "simplify the UI" pass. Flagging this rather than guessing — see the
    punch list item for this file.
    """
    exe, args = _get_executable_and_args(extra_args)
    if today is None:
        today = datetime.date.today()
    schedule_map = {
        "daily": "DAILY",
        "weekly": "WEEKLY",
        "monthly": "MONTHLY",
    }
    sched = schedule_map.get(frequency, "WEEKLY")
    task_run = subprocess.list2cmdline([exe] + args)
    task_name = TASK_NAME if not extra_args else TASK_NAME + "_" + extra_args[0].lstrip("-").replace("-", "_")
    cmd = [
        "schtasks", "/Create", "/TN", task_name,
        "/TR", task_run,
        "/SC", sched, "/ST", time_str,
    ]
    # M4 audit fix: /SC WEEKLY without /D silently defaults to Mondays and
    # /SC MONTHLY to the 1st — neither matches what the dialog's 'enable
    # on this day' mental model is. Pass the day the schedule is
    # enabled/created on explicitly: weekly -> the 3-letter weekday of
    # today, monthly -> today's day-of-month clamped to 28 (schtasks
    # silently skips months with FEWER days than /D, so 29-31 would make
    # some months never run). Tasks created before this fix keep whatever
    # /D schtasks gave them; the /Create /F below rewrites them with the
    # explicit day on the next enable.
    cmd += _schedule_day_args(sched, today)
    cmd += ["/F", "/IT"]
    if is_admin():
        cmd += ["/RL", "HIGHEST"]
    return cmd


# schtasks /D day tokens for /SC WEEKLY (MON..SUN, locale-independent —
# built from date.weekday(), never strftime which follows the OS locale)
_WEEKDAY_TOKENS = ["MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN"]


def _schedule_day_args(sched: str, today: "datetime.date | None") -> "list[str]":
    """Extra schtasks args pinning /D for WEEKLY/MONTHLY schedules (see
    _build_schtasks_cmd). DAILY takes no /D."""
    if today is None:
        today = datetime.date.today()
    if sched == "WEEKLY":
        return ["/D", _WEEKDAY_TOKENS[today.weekday()]]
    if sched == "MONTHLY":
        return ["/D", str(min(today.day, 28))]
    return []


def _build_schtasks_cmd_str(frequency, time_str, extra_args=None) -> str:
    """Legacy string form for display / debugging (properly quoted)."""
    return subprocess.list2cmdline(_build_schtasks_cmd(frequency, time_str, extra_args))


def enable_schedule(frequency="weekly", time_str="03:00", extra_args=None):
    """Create or update the scheduled task. `extra_args=["--auto-update"]`
    schedules the Update Everything run instead of the clean/repair run
    (separate Task Scheduler entry so both can coexist)."""
    cmd = _build_schtasks_cmd(frequency, time_str, extra_args)
    try:
        result = subprocess.run(cmd, shell=False, capture_output=True, text=True, timeout=30)
        if result.returncode == 0:
            config = load_config()
            if extra_args:
                config["schedule_update_enabled"] = True
            else:
                config["schedule_enabled"] = True
            config["schedule_frequency"] = frequency
            config["schedule_time"] = time_str
            save_config(config)
            return True, result.stdout or result.stderr
        return False, result.stdout or result.stderr
    except Exception as e:
        return False, str(e)


def disable_schedule(extra_args=None):
    """Delete the scheduled task (clean/repair task by default, or the
    Update Everything task with extra_args=["--auto-update"])."""
    task_name = TASK_NAME if not extra_args else TASK_NAME + "_" + extra_args[0].lstrip("-").replace("-", "_")
    cmd = ["schtasks", "/Delete", "/TN", task_name, "/F"]
    try:
        result = subprocess.run(cmd, shell=False, capture_output=True, text=True, timeout=30)
        if result.returncode == 0:
            config = load_config()
            if extra_args:
                config["schedule_update_enabled"] = False
            else:
                config["schedule_enabled"] = False
            save_config(config)
            return True, result.stdout or result.stderr
        return False, result.stdout or result.stderr
    except Exception as e:
        return False, str(e)


def get_schedule_status(extra_args=None):
    """Check if the scheduled task exists and get its details."""
    task_name = TASK_NAME if not extra_args else TASK_NAME + "_" + extra_args[0].lstrip("-").replace("-", "_")
    cmd = ["schtasks", "/Query", "/TN", task_name, "/V", "/FO", "LIST"]
    try:
        result = subprocess.run(cmd, shell=False, capture_output=True, text=True, timeout=30)
        return result.returncode == 0, result.stdout
    except Exception:
        return False, ""


def run_auto_update():
    """Headless 'Update Everything' run for the --auto-update schedule:
    winget upgrade --all with the same honest-logging contract as
    run_auto_clean. Returns (ok, summary)."""
    from app.utils import TaskContext, TaskCancelled
    from app.tasks.install_tasks import update_all_apps

    class AutoCtx(TaskContext):
        def __init__(self):
            super().__init__(log=self._log, set_status=self._set_status,
                             cancelled=lambda: False)
        def _log(self, msg):
            print(f"[AUTO-UPD] {msg}")
        def _set_status(self, msg):
            print(f"[AUTO-UPD STATUS] {msg}")
        def log(self, msg):  # type: ignore[override]
            self._log(msg)
        def set_status(self, msg):  # type: ignore[override]
            self._set_status(msg)

    ctx = AutoCtx()
    try:
        update_all_apps(ctx)
        return True, "Auto-update complete: all apps current."
    except TaskCancelled as exc:
        # audit minor 2: update_all_apps used to return normally after a
        # cancelled/timeout stop, so this reported 'Auto-update complete'
        # while the log said the run was stopped. The stop now surfaces as
        # TaskCancelled — report it honestly (exit non-zero so Task
        # Scheduler sees the run did not finish).
        ctx.log(f"Auto-update cancelled: {exc}")
        return False, "Auto-update cancelled."
    except Exception as e:
        ctx.log(f"ERROR in auto-update: {e}")
        return False, f"Auto-update failed: {e}"


def run_auto_clean(selected_tasks_by_tab):
    """
    Run the auto-clean with pre-selected tasks.
    Called when the app is launched with --auto-clean.
    """
    from app.utils import TaskContext, TaskSkipped
    from app.tasks import clean_tasks, repair_tasks, tweak_tasks, game_tasks, advanced_tasks
    import threading
    import time

    # Collect tasks to run.
    # Phase 2 (#12): the UI now has 3 tabs (Games merged into Clean,
    # Advanced into Tweak, 3 advanced tasks cut). Old configs and the
    # current GUI both save under the 3-tab names, but keep resolving the
    # legacy 5-tab names too so a config saved by the old version (or the
    # in-flight merge map in config_persist) still runs every task.
    tasks_to_run = []
    all_tasks = {
        "Clean": {t.key: t for t in clean_tasks.TASKS}
                   | {t.key: t for t in game_tasks.TASKS},
        "Repair": {t.key: t for t in repair_tasks.TASKS},
        "Tweak": {t.key: t for t in tweak_tasks.TASKS}
                   | {t.key: t for t in advanced_tasks.TASKS},
        # legacy names keep resolving (config_persist migrates them on load,
        # but be tolerant if a config file slipped through un-migrated)
        "Games": {t.key: t for t in game_tasks.TASKS},
        "Advanced": {t.key: t for t in advanced_tasks.TASKS},
    }

    for tab, keys in selected_tasks_by_tab.items():
        for key in keys:
            if key in all_tasks.get(tab, {}):
                tasks_to_run.append(all_tasks[tab][key])
            # Games-tab twins that config_persist remapped to their Clean
            # equivalents are handled by the migration; a raw legacy key
            # still resolves above via the "Games" alias.

    # Skip tasks removed from the app (punch-list #13) even if an old
    # config still names them. Import the single source of truth instead of
    # a duplicated literal (audit: two hardcoded copies can drift).
    from app.tab_presets import CUT_TASK_KEYS as _cut
    tasks_to_run = [t for t in tasks_to_run if t.key not in _cut]
    
    if not tasks_to_run:
        return False, "No tasks selected for auto-clean"
    
    # Cross-tab dangerous combo check (GUI is per-tab, scheduler runs all tabs together)
    try:
        from app.warnings import check_dangerous_combos
        # check_dangerous_combos now supports List[str] keys
        all_keys = [k for keys in selected_tasks_by_tab.values() for k in keys]
        warnings = check_dangerous_combos(all_keys)  # type: ignore[arg-type]
        if warnings:
            # Log but don't block scheduled run — user not present to confirm
            print("[AUTO] Cross-tab warnings: " + " | ".join(warnings))
    except Exception:
        pass
    
    # Run synchronously (this is called from the scheduled task, not GUI)
    # Must satisfy TaskContext contract (log, set_status, cancelled)
    class AutoCtx(TaskContext):
        def __init__(self):
            self.logs = []
            super().__init__(
                log=self._log,
                set_status=self._set_status,
                cancelled=lambda: False,
            )
        def _log(self, msg):
            self.logs.append(msg)
            print(f"[AUTO] {msg}")
        def _set_status(self, msg):
            print(f"[AUTO STATUS] {msg}")
        # Keep public aliases for callers that reference .log/.set_status directly
        def log(self, msg):  # type: ignore[override]
            self._log(msg)
        def set_status(self, msg):  # type: ignore[override]
            self._set_status(msg)

    ctx = AutoCtx()
    total_bytes = 0
    completed, failed = 0, 0
    skipped_n = 0

    # Show the scheduled-run toast (audit dead-code fix: notify_scheduled_run
    # existed with zero callers — a scheduled run was invisible unless the
    # user happened to see a window)
    try:
        from app.toast import notify_scheduled_run
        notify_scheduled_run()
    except Exception:
        pass

    for task in tasks_to_run:
        if ctx.cancelled():
            ctx.log("Cancelled — remaining tasks were skipped.")
            break
        ctx.log(f"Running: {task.label}")
        try:
            result = task.run(ctx)
            # H6 fix: mirror the GUI's result classification so failures are
            # reported honestly in headless runs (previously every run was
            # 'succeeded' and the exit code lied to Task Scheduler)
            if isinstance(result, int) and not isinstance(result, bool):
                total_bytes += result
            elif isinstance(result, bool) and not result:
                raise RuntimeError("Task returned False")
            completed += 1
        except TaskSkipped as exc:
            # B5 audit fix (mirrors the GUI runner): a tweak with nothing
            # to do on this machine is completed-with-skip — it must not
            # count as a failure (no error exit for Task Scheduler) and is
            # logged as skipped, not succeeded.
            skipped_n += 1
            ctx.log(f"Skipped {task.label}: {exc}")
        except Exception as e:
            failed += 1
            ctx.log(f"ERROR in {task.label}: {e}")

    if skipped_n:
        summary = (f"Auto-clean complete: {completed} succeeded, "
                   f"{skipped_n} skipped (nothing to change), {failed} failed.")
    else:
        summary = f"Auto-clean complete: {completed} succeeded, {failed} failed."
    if total_bytes > 0:
        from app.utils import format_bytes
        summary += f" Freed: {format_bytes(total_bytes)}"
    
    ctx.log(summary)
    return failed == 0, summary