"""
Windows Task Scheduler integration for automatic maintenance.
"""

import os
import sys
import subprocess
from app.config_persist import load_config, save_config

TASK_NAME = "CleanerTool_AutoMaintenance"
TASK_DESC = "Cleaner Tool automatic maintenance run"


def _get_executable_and_args():
    """Return (executable, arguments) for the current run."""
    if getattr(sys, "frozen", False):
        exe = sys.executable
        args = ["--auto-clean"]
    else:
        exe = sys.executable
        script = os.path.abspath(sys.argv[0])
        args = [script, "--auto-clean"]
    return exe, args


def _build_schtasks_cmd(frequency, time_str):
    """Build schtasks command to create the scheduled task."""
    exe, args = _get_executable_and_args()
    schedule_map = {
        "daily": "DAILY",
        "weekly": "WEEKLY",
        "monthly": "MONTHLY",
    }
    sched = schedule_map.get(frequency, "WEEKLY")
    
    # Build command with proper quoting using subprocess.list2cmdline
    task_run = subprocess.list2cmdline([exe] + args)
    cmd = f'schtasks /Create /TN "{TASK_NAME}" /TR {task_run} /SC {sched} /ST {time_str} /F /RL HIGHEST /IT'
    return cmd


def enable_schedule(frequency="weekly", time_str="03:00"):
    """Create or update the scheduled task."""
    config = load_config()
    config["schedule_enabled"] = True
    config["schedule_frequency"] = frequency
    config["schedule_time"] = time_str
    save_config(config)
    
    cmd = _build_schtasks_cmd(frequency, time_str)
    try:
        result = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=30)
        return result.returncode == 0, result.stdout or result.stderr
    except Exception as e:
        return False, str(e)


def disable_schedule():
    """Delete the scheduled task."""
    config = load_config()
    config["schedule_enabled"] = False
    save_config(config)
    
    cmd = f'schtasks /Delete /TN "{TASK_NAME}" /F'
    try:
        result = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=30)
        return result.returncode == 0, result.stdout or result.stderr
    except Exception as e:
        return False, str(e)


def get_schedule_status():
    """Check if the scheduled task exists and get its details."""
    cmd = f'schtasks /Query /TN "{TASK_NAME}" /V /FO LIST'
    try:
        result = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=30)
        return result.returncode == 0, result.stdout
    except Exception:
        return False, ""


def run_auto_clean(selected_tasks_by_tab):
    """
    Run the auto-clean with pre-selected tasks.
    Called when the app is launched with --auto-clean.
    """
    from app.utils import TaskContext
    from app.tasks import clean_tasks, repair_tasks, tweak_tasks, game_tasks
    import threading
    import time
    
    # Collect tasks to run
    tasks_to_run = []
    all_tasks = {
        "Clean": {t.key: t for t in clean_tasks.TASKS},
        "Repair": {t.key: t for t in repair_tasks.TASKS},
        "Tweak": {t.key: t for t in tweak_tasks.TASKS},
        "Games": {t.key: t for t in game_tasks.TASKS},
    }
    
    for tab, keys in selected_tasks_by_tab.items():
        for key in keys:
            if key in all_tasks.get(tab, {}):
                tasks_to_run.append(all_tasks[tab][key])
    
    if not tasks_to_run:
        return False, "No tasks selected for auto-clean"
    
    # Run synchronously (this is called from the scheduled task, not GUI).
    # Must satisfy the same interface as TaskContext (dry_run / cancelled) —
    # every task function and helper (run_cmd, clean_folder_contents,
    # reg_set_value, ...) reads ctx.dry_run / ctx.cancelled() directly, so a
    # bare log/set_status-only stand-in raises AttributeError on every task.
    logs = []

    def _log(msg):
        logs.append(msg)
        print(f"[AUTO] {msg}")

    def _set_status(msg):
        print(f"[AUTO STATUS] {msg}")

    ctx = TaskContext(log=_log, set_status=_set_status, dry_run=False, cancelled=lambda: False)
    total_bytes = 0
    completed, failed = 0, 0
    
    for task in tasks_to_run:
        ctx.log(f"Running: {task.label}")
        try:
            result = task.run(ctx)
            if isinstance(result, int) and not isinstance(result, bool):
                total_bytes += result
            completed += 1
        except Exception as e:
            failed += 1
            ctx.log(f"ERROR in {task.label}: {e}")
    
    summary = f"Auto-clean complete: {completed} succeeded, {failed} failed."
    if total_bytes > 0:
        from app.utils import format_bytes
        summary += f" Freed: {format_bytes(total_bytes)}"
    
    ctx.log(summary)
    return failed == 0, summary