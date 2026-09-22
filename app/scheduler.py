"""
Windows Task Scheduler integration for automatic maintenance.
"""

import datetime
import os
import sys
import subprocess
from app.config_persist import load_config, save_config, update_config
from app.elevation import is_admin
from app.utils import resolve_exe

TASK_NAME = "CleanerTool_AutoMaintenance"
TASK_DESC = "Cleaner Tool automatic maintenance run"

# Startup tray (Phase 5): logon task booting the app hidden with --tray
# (resident monitors + Auto-Pilot from login). Deliberately WITHOUT /RL
# HIGHEST — a permanently-elevated desktop process is a security posture
# downgrade; the boot copy runs standard-rights and the user elevates
# per-session (or via auto-elevate) exactly as today. Same per-user /IT
# shape as the maintenance tasks: no stored credentials, ever.
TASK_STARTUP = "CleanerTool_StartupTray"
TASK_STARTUP_DESC = "Cleaner Tool tray at logon (minimized)"


def _build_startup_cmd(exe_override: str | None = None) -> "list[str]":
    """schtasks /Create for the logon tray task. Pure builder (exe_override
    is the test seam for space-containing paths)."""
    if exe_override is not None:
        import subprocess as _sp
        task_run = _sp.list2cmdline([exe_override, "--tray"])
    else:
        exe, args = _get_executable_and_args(["--tray"])
        task_run = subprocess.list2cmdline([exe] + args)
    return [
        resolve_exe("schtasks"), "/Create", "/TN", TASK_STARTUP,
        "/TR", task_run,
        "/SC", "ONLOGON", "/IT", "/F",
    ]


def _build_startup_delete_cmd() -> "list[str]":
    return [resolve_exe("schtasks"), "/Delete", "/TN", TASK_STARTUP, "/F"]


def _build_startup_query_cmd() -> "list[str]":
    return [resolve_exe("schtasks"), "/Query", "/TN", TASK_STARTUP,
            "/V", "/FO", "LIST"]


def _want_startup_tr(exe_override: str | None = None) -> str:
    """Expected /TR string (drift comparison)."""
    if exe_override is not None:
        import subprocess as _sp
        return _sp.list2cmdline([exe_override, "--tray"])
    exe, args = _get_executable_and_args(["--tray"])
    return subprocess.list2cmdline([exe] + args)


def _run_schtasks(cmd) -> "tuple[bool, str]":
    try:
        result = subprocess.run(
            cmd, shell=False, capture_output=True, text=True, timeout=30,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return result.returncode == 0, (result.stdout or "") + (result.stderr or "")
    except Exception as exc:
        return False, str(exc)


def get_startup_status() -> "tuple[bool, str]":
    """(task exists, raw query output). Read-only probe, never raises."""
    return _run_schtasks(_build_startup_query_cmd())


def _startup_tr_matches(out: str, exe_override: str | None = None) -> "bool | None":
    """True/False when the query output names a runnable; None when the
    output carries no Task-To-Run row to judge by."""
    try:
        for line in (out or "").splitlines():
            if line.strip().lower().startswith("task to run"):
                if ":" not in line:
                    return None
                live = line.split(":", 1)[1].strip().strip('"')
                return live == _want_startup_tr(exe_override).strip().strip('"')
    except Exception:
        pass
    return None


def _set_startup_flag(on: bool) -> None:
    """Persist the startup checkbox (single-lock RMW, never a stale whole
    save). Split out so tests cover the flag without touching schtasks."""
    try:
        def _mut(cfg, _on=bool(on)):
            cfg["startup_tray_enabled"] = _on
        update_config(_mut)
    except Exception:
        pass


def enable_startup_tray() -> "tuple[bool, str]":
    """Create (or refresh, /F) the logon tray task. F14-style: skip the
    rewrite when the live task already matches."""
    try:
        ok, out = get_startup_status()
        if ok and _startup_tr_matches(out) is True:
            _set_startup_flag(True)
            return True, "Startup entry already up to date."
    except Exception:
        pass
    ok, msg = _run_schtasks(_build_startup_cmd())
    if ok:
        _set_startup_flag(True)
        return True, msg or "Startup entry created."
    return False, msg


def disable_startup_tray() -> "tuple[bool, str]":
    """Delete the logon tray task. Missing task counts as success."""
    ok, msg = _run_schtasks(_build_startup_delete_cmd())
    if ok:
        _set_startup_flag(False)
        return True, msg or "Startup entry removed."
    # deleting what isn't there is the desired end state too
    try:
        exists, _out = get_startup_status()
        if not exists:
            _set_startup_flag(False)
            return True, "Startup entry already absent."
    except Exception:
        pass
    return False, msg


def resync_startup_flag() -> bool:
    """Reconcile config with live Task Scheduler state (same stale-flag
    hazard resync_schedule_flag covers for the maintenance tasks)."""
    try:
        ok, _out = get_startup_status()
    except Exception:
        ok = False
    try:
        _set_startup_flag(bool(ok))
    except Exception:
        pass
    return bool(ok)


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
    # F14: validate instead of silently coercing typos to WEEKLY.
    frequency = _validate_frequency(frequency)
    time_str = _validate_time_str(time_str)
    sched = schedule_map[frequency]
    task_run = subprocess.list2cmdline([exe] + args)
    task_name = TASK_NAME if not extra_args else TASK_NAME + "_" + extra_args[0].lstrip("-").replace("-", "_")
    cmd = [
        resolve_exe("schtasks"), "/Create", "/TN", task_name,
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


def _validate_frequency(frequency) -> str:
    """F14: unknown/typo frequencies used to silently become WEEKLY."""
    f = str(frequency or "").strip().lower()
    if f not in ("daily", "weekly", "monthly"):
        raise ValueError(f"Unknown schedule frequency {frequency!r} (want daily/weekly/monthly).")
    return f


def _validate_time_str(time_str) -> str:
    t = str(time_str or "").strip()
    import re as _re
    if not _re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", t):
        raise ValueError(f"Bad schedule time {time_str!r} (want HH:MM, 24h).")
    return t


def _build_schtasks_cmd_str(frequency, time_str, extra_args=None, today=None) -> str:
    """Legacy string form for display / debugging (properly quoted).

    today pass-through (matches _build_schtasks_cmd's M4 audit param):
    lets callers pin the /D weekday deterministically instead of
    depending on whatever day the test happens to run."""
    return subprocess.list2cmdline(
        _build_schtasks_cmd(frequency, time_str, extra_args, today=today))


def enable_schedule(frequency="weekly", time_str="03:00", extra_args=None):
    """Create or update the scheduled task. `extra_args=["--auto-update"]`
    schedules the Update Everything run instead of the clean/repair run
    (separate Task Scheduler entry so both can coexist)."""
    # F14: validate before touching schtasks (fail fast, honest error).
    try:
        frequency = _validate_frequency(frequency)
        time_str = _validate_time_str(time_str)
    except ValueError as exc:
        return False, str(exc)
    # F14: skip the rewrite when the live task already matches (/TR drift
    # detection) — avoids churning Task Scheduler on every dialog Apply.
    try:
        task_name = TASK_NAME if not extra_args else TASK_NAME + "_" + extra_args[0].lstrip("-").replace("-", "_")
        ok, out = get_schedule_status(extra_args)
        if ok:
            exe, args = _get_executable_and_args(extra_args)
            want_tr = subprocess.list2cmdline([exe] + args)
            m = None
            for line in (out or "").splitlines():
                if line.strip().lower().startswith("task to run"):
                    m = line.split(":", 1)[1].strip() if ":" in line else ""
                    break
            if m is not None and m.strip().strip('"') == want_tr.strip().strip('"'):
                def _mut(extra_args=extra_args, frequency=frequency, time_str=time_str):
                    def _m(cfg):
                        if extra_args:
                            cfg["schedule_update_enabled"] = True
                        else:
                            cfg["schedule_enabled"] = True
                        cfg["schedule_frequency"] = frequency
                        cfg["schedule_time"] = time_str
                    return _m
                update_config(_mut())
                return True, "Schedule already up to date."
    except Exception:
        pass
    cmd = _build_schtasks_cmd(frequency, time_str, extra_args)
    try:
        result = subprocess.run(cmd, shell=False, capture_output=True, text=True, timeout=30)
        if result.returncode == 0:
            # F08: single-lock RMW (was load; mutate; save with lock released).
            def _mut(cfg, extra_args=extra_args, frequency=frequency, time_str=time_str):
                if extra_args:
                    cfg["schedule_update_enabled"] = True
                else:
                    cfg["schedule_enabled"] = True
                cfg["schedule_frequency"] = frequency
                cfg["schedule_time"] = time_str
            update_config(_mut)
            return True, result.stdout or result.stderr
        return False, result.stdout or result.stderr
    except Exception as e:
        return False, str(e)


def disable_schedule(extra_args=None):
    """Delete the scheduled task (clean/repair task by default, or the
    Update Everything task with extra_args=["--auto-update"])."""
    task_name = TASK_NAME if not extra_args else TASK_NAME + "_" + extra_args[0].lstrip("-").replace("-", "_")
    cmd = [resolve_exe("schtasks"), "/Delete", "/TN", task_name, "/F"]
    try:
        result = subprocess.run(cmd, shell=False, capture_output=True, text=True, timeout=30)
        if result.returncode == 0:
            # F08: single-lock RMW.
            def _mut(cfg, extra_args=extra_args):
                if extra_args:
                    cfg["schedule_update_enabled"] = False
                else:
                    cfg["schedule_enabled"] = False
            update_config(_mut)
            return True, result.stdout or result.stderr
        return False, result.stdout or result.stderr
    except Exception as e:
        return False, str(e)


def get_schedule_status(extra_args=None):
    """Check if the scheduled task exists and get its details."""
    task_name = TASK_NAME if not extra_args else TASK_NAME + "_" + extra_args[0].lstrip("-").replace("-", "_")
    cmd = [resolve_exe("schtasks"), "/Query", "/TN", task_name, "/V", "/FO", "LIST"]
    try:
        result = subprocess.run(cmd, shell=False, capture_output=True, text=True, timeout=30)
        return result.returncode == 0, result.stdout
    except Exception:
        return False, ""


def resync_schedule_flag(extra_args=None) -> bool:
    """F14: reconcile config with live Task Scheduler state (external
    schtasks /Delete left config stale-True). Returns the live state and
    persists it via a single-lock update so the dialog seeds honestly."""
    ok, _ = get_schedule_status(extra_args)
    def _mut(cfg, ok=ok, extra_args=extra_args):
        if extra_args:
            cfg["schedule_update_enabled"] = bool(ok)
        else:
            cfg["schedule_enabled"] = bool(ok)
    try:
        update_config(_mut)
    except Exception:
        pass
    return ok


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
    from app.utils import TaskContext, TaskSkipped, TaskCancelled
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
        # H8: a hand-edited config may store a plain string instead of a
        # list — iterating it would scatter per-character keys.
        if isinstance(keys, str):
            keys = [keys]
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
    # H8: dedupe by key (same task listed under two tabs, or pre/post
    # migration duplicates) — never run a task twice in one auto-clean.
    _seen: set = set()
    _deduped = []
    for t in tasks_to_run:
        if t.key not in _seen:
            _seen.add(t.key)
            _deduped.append(t)
    tasks_to_run = _deduped
    
    if not tasks_to_run:
        return False, "No tasks selected for auto-clean"
    
    # Cross-tab dangerous combo check (GUI is per-tab, scheduler runs all tabs together)
    try:
        from app.warnings import check_dangerous_combos, check_info_notices
        # check_dangerous_combos now supports List[str] keys
        # F14: coerce hand-edited {tab: "singlekey"} strings (was char-scatter).
        all_keys = []
        for keys in selected_tasks_by_tab.values():
            if isinstance(keys, str):
                keys = [keys]
            all_keys.extend(keys)
        warnings = check_dangerous_combos(all_keys)  # type: ignore[arg-type]
        try:
            warnings = list(warnings) + list(check_info_notices(all_keys))  # type: ignore[arg-type]
        except Exception:
            pass
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

    # Module-8 GUI parity: headless --auto-clean must honor the same
    # admin_required gate as run_tasks. A schedule created from Limited Mode
    # runs non-elevated (no /RL HIGHEST) — attempting admin tasks there would
    # fail/half-write instead of gracefully skipping like the GUI.
    try:
        if not is_admin():
            _blocked = [t for t in tasks_to_run if getattr(t, "admin_required", False)]
            if _blocked:
                _names = ", ".join(t.label for t in _blocked)
                ctx.log(f"Limited mode: skipping {len(_blocked)} admin-only task(s): {_names}")
                skipped_n += len(_blocked)
            tasks_to_run = [t for t in tasks_to_run if not getattr(t, "admin_required", False)]
            if not tasks_to_run:
                _summary = "Auto-clean skipped: all selected tasks need Administrator rights."
                ctx.log(_summary)
                return True, _summary
    except Exception:
        pass

    # Show the scheduled-run toast (audit dead-code fix: notify_scheduled_run
    # existed with zero callers — a scheduled run was invisible unless the
    # user happened to see a window). F14: fire-and-forget on a daemon
    # thread — the sync powershell call can block ~30s and must never stall
    # the headless run's task loop.
    try:
        import threading as _th
        def _toast():
            try:
                from app.toast import notify_scheduled_run
                notify_scheduled_run()
            except Exception:
                pass
        _th.Thread(target=_toast, daemon=True).start()
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
        except TaskCancelled as exc:
            # F-2 audit fix (mirrors the GUI runner): a user/tool Stop is
            # 'stopped', not a failure — run_cmd_checked's H6 contract.
            # The next iteration's cancelled() check ends the loop. Without
            # this catch the generic handler counted the interrupted task
            # as failed, flipping the run's exit code for Task Scheduler.
            ctx.log(f"Stopped {task.label}: {exc}")
            break
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