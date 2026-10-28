"""
Windows Task Scheduler integration for automatic maintenance.
"""

import datetime
import os
import sys
import time
import subprocess
from app.config_persist import update_config
from app.elevation import is_admin
from app.utils import resolve_exe, run_capture
from app.runner.task_runner import log_block, run_batch, print_context

TASK_NAME = "CleanerTool_AutoMaintenance"

# Startup tray (Phase 5): logon task booting the app hidden with --tray
# (resident monitors + Auto-Pilot from login). Deliberately WITHOUT /RL
# HIGHEST — a permanently-elevated desktop process is a security posture
# downgrade; the boot copy runs standard-rights and the user elevates
# per-session (or via auto-elevate) exactly as today. Same per-user /IT
# shape as the maintenance tasks: no stored credentials, ever.
TASK_STARTUP = "CleanerTool_StartupTray"


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


def _task_name_for(extra_args=None) -> str:
    """Scheduled-task name for a mode. ONE definition: create, query, delete
    and the drift check must all name the same task."""
    if not extra_args:
        return TASK_NAME
    return TASK_NAME + "_" + str(extra_args[0]).lstrip("-").replace("-", "_")


def _want_startup_tr(exe_override: str | None = None) -> str:
    """Expected /TR string (drift comparison)."""
    if exe_override is not None:
        import subprocess as _sp
        return _sp.list2cmdline([exe_override, "--tray"])
    exe, args = _get_executable_and_args(["--tray"])
    return subprocess.list2cmdline([exe] + args)


def _norm_task_line(text: str) -> str:
    """Compare task command lines without quoting/whitespace noise."""
    return " ".join((text or "").replace('"', " ").split()).lower()


def _xml_exec(xml: "str | None") -> "tuple[str, str] | None":
    """(Command, Arguments) from a task-definition XML, or None.

    Element names are schema-defined and identical in every Windows locale.
    The `/FO LIST` labels are TRANSLATED, so nothing here parses that output
    any more: the old "Task To Run:" match silently failed on non-English
    Windows, which disabled auto-elevate (status() reported "points
    elsewhere") and the startup-tray drift check on every such machine.
    """
    if not xml or "<" not in (xml or ""):
        return None
    try:
        import xml.etree.ElementTree as ET
        root = ET.fromstring(xml)
        command = arguments = None
        for el in root.iter():
            tag = el.tag.rsplit("}", 1)[-1]
            if tag == "Command" and command is None:
                command = (el.text or "").strip()
            elif tag == "Arguments" and arguments is None:
                arguments = (el.text or "").strip()
        if not command:
            return None
        return command, arguments or ""
    except Exception:
        return None


def _live_task_xml(task_name: str) -> "str | None":
    """One live task's definition XML, or None when it cannot be read."""
    cmd = [resolve_exe("schtasks"), "/Query", "/TN", task_name, "/XML"]
    rc, out = run_capture(cmd, timeout=30)
    if rc != 0:
        return None
    return out or None


def _task_run_matches(task_name: str, want_line: str,
                      xml: "str | None" = None) -> "bool | None":
    """True/False when the live task runs `want_line`; None if unreadable.

    None is the real third answer: callers must only skip a rewrite on a
    POSITIVE match (skipping on a failed check is what BUG-010 was).
    """
    got = _xml_exec(xml if xml is not None else _live_task_xml(task_name))
    if got is None:
        return None
    command, arguments = got
    live = (command + " " + arguments) if command else arguments
    return _norm_task_line(live) == _norm_task_line(want_line)


def _run_schtasks(cmd) -> "tuple[bool, str]":
    """(ok, output) for one schtasks call, via the shared capture helper."""
    rc, out = run_capture(cmd, timeout=30)
    return rc == 0, out


def get_startup_status() -> "tuple[bool, str]":
    """(task exists, raw query output). Read-only probe, never raises."""
    return _run_schtasks(_build_startup_query_cmd())


def _set_startup_flag(on: bool) -> None:
    """Persist the startup checkbox (single-lock RMW, never a stale whole
    save). Split out so tests cover the flag without touching schtasks."""
    try:
        def _mut(cfg, _on=bool(on)):
            cfg["startup_tray_enabled"] = _on
        update_config(_mut)
    except Exception as exc:
        from app.utils import log_or_swallow
        log_or_swallow(None, "scheduler.startup_flag", exc)


def enable_startup_tray() -> "tuple[bool, str]":
    """Create (or refresh, /F) the logon tray task. F14-style: skip the
    rewrite when the live task already matches."""
    try:
        ok, _out = get_startup_status()
        if ok and _task_run_matches(TASK_STARTUP, _want_startup_tr()) is True:
            _set_startup_flag(True)
            return True, "Startup entry already up to date."
    except Exception as exc:
        from app.utils import log_or_swallow
        log_or_swallow(None, "scheduler.enable_startup_probe", exc)
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
    except Exception as exc:
        from app.utils import log_or_swallow
        log_or_swallow(None, "scheduler.disable_startup_probe", exc)
    return False, msg


def resync_startup_flag() -> bool:
    """Reconcile config with live Task Scheduler state (same stale-flag
    hazard resync_schedule_flag covers for the maintenance tasks)."""
    try:
        ok, _out = get_startup_status()
    except Exception as exc:
        from app.utils import log_or_swallow
        log_or_swallow(None, "scheduler.resync_startup_probe", exc)
        ok = False
    try:
        _set_startup_flag(bool(ok))
    except Exception as exc:
        from app.utils import log_or_swallow
        log_or_swallow(None, "scheduler.resync_startup_persist", exc)
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
    task_name = _task_name_for(extra_args)
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


#: The schedule element schtasks ACTUALLY writes for each frequency, plus the
#: older short names, mapped to our own vocabulary. B-005: the check used to
#: look for "Weekly"/"Daily"/"Monthly" and "StartTime", none of which appear
#: in a real `schtasks /Query /XML` export (it emits ScheduleByWeek /
#: ScheduleByDay / ScheduleByMonth and an ISO StartBoundary), so it could
#: never confirm a match and the answer was always "rewrite".
_XML_SCHEDULE_TAGS = {
    "ScheduleByDay": "daily",
    "ScheduleByWeek": "weekly",
    "ScheduleByMonth": "monthly",
    "Daily": "daily",
    "Weekly": "weekly",
    "Monthly": "monthly",
}

#: XML day names -> the locale-independent /D tokens schtasks accepts.
_XML_WEEKDAYS = {
    "Monday": "MON", "Tuesday": "TUE", "Wednesday": "WED",
    "Thursday": "THU", "Friday": "FRI", "Saturday": "SAT",
    "Sunday": "SUN",
}


def _local_name(element) -> str:
    """Tag name without the XML namespace (Windows always emits one)."""
    return str(element.tag).rsplit("}", 1)[-1]


def _xml_start_time(trigger) -> str:
    """"HH:MM" from a trigger's StartBoundary/StartTime, or "".

    Real exports carry `2026-10-24T03:00:00` (optionally with seconds, a
    fractional part or a zone offset); the legacy form is `03:00:00`.
    """
    import re as _re
    for el in trigger.iter():
        if _local_name(el) in ("StartBoundary", "StartTime"):
            m = _re.search(r"(\d{1,2}):(\d{2})", el.text or "")
            if not m:
                continue
            hour, minute = int(m.group(1)), int(m.group(2))
            if 0 <= hour <= 23 and 0 <= minute <= 59:
                return "%02d:%02d" % (hour, minute)
    return ""


def _xml_day_matches(sched_el, frequency: str, today) -> "bool | None":
    """Does the live trigger run on the day we would write with /D?

    None means the day could not be read, which is NOT a match: the caller may
    only skip a rewrite on a positive confirmation.
    """
    if frequency == "daily":
        return True
    if frequency == "weekly":
        want = _WEEKDAY_TOKENS[today.weekday()]
        days_el = None
        for el in sched_el.iter():
            if _local_name(el) == "DaysOfWeek":
                days_el = el
                break
        if days_el is None:
            return None
        live = {_XML_WEEKDAYS.get(_local_name(c))
                for c in list(days_el)}
        live.discard(None)
        if not live:
            return None
        # exact set: a task with extra days runs more often than the dialog says
        return live == {want}
    if frequency == "monthly":
        want = min(today.day, 28)
        days_el = None
        for el in sched_el.iter():
            if _local_name(el) == "DaysOfMonth":
                days_el = el
                break
        if days_el is None:
            return None
        live = set()
        for child in list(days_el):
            try:
                live.add(int((child.text or "").strip()))
            except (TypeError, ValueError):
                continue
        if not live:
            return None
        return live == {want}
    return None


def _xml_schedule_matches(xml: str, frequency: str, time_str: str,
                          today=None) -> "bool | None":
    """True/False if the live schedule matches what we would write; None if the
    XML cannot be read or understood.

    prior BUG-010: the drift check compared ONLY the `/TR` (Task To Run) string,
    so a task whose time or frequency had been changed - in Task Scheduler
    directly, or by an older build - was judged "already matches" and the
    rewrite was SKIPPED. The dialog then said 09:00 while the machine ran the
    job at 03:00, and Apply silently did nothing.

    B-005: the replacement read the wrong elements entirely (see
    _XML_SCHEDULE_TAGS) and never compared the DAY, so a weekly task moved to
    another weekday - or a monthly one moved to another date - was reported as
    matching. Both are drift now, and `today` lets a test pin which /D we
    would write.

    None is a real third answer and matters: the caller must only skip the
    rewrite when it can POSITIVELY confirm a match. Any inability to read or
    understand the XML has to fall through to "rewrite", because skipping on a
    failed check is what produced the bug.
    """
    if not xml or "<" not in str(xml or ""):
        return None
    try:
        import xml.etree.ElementTree as ET
        root = ET.fromstring(xml)
        if today is None:
            today = datetime.date.today()

        # The trigger that owns the schedule; its StartBoundary is the time.
        sched_el = None
        live_frequency = None
        for el in root.iter():
            kind = _XML_SCHEDULE_TAGS.get(_local_name(el))
            if kind:
                sched_el, live_frequency = el, kind
                break
        if sched_el is None:
            return None
        if live_frequency != frequency:
            return False

        trigger = sched_el
        for parent in root.iter():
            if any(child is sched_el for child in list(parent)):
                trigger = parent
                break

        live_time = _xml_start_time(trigger)
        if not live_time:
            return None
        if live_time != _validate_time_str(time_str):
            return False

        # A disabled task never runs, so it does not match an enabled one.
        for el in trigger.iter():
            if _local_name(el) == "Enabled":
                if (el.text or "").strip().lower() == "false":
                    return False
                break

        day_ok = _xml_day_matches(sched_el, frequency, today)
        if day_ok is None:
            return None
        return bool(day_ok)
    except Exception:
        return None


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
    # F14: skip the rewrite when the live task already matches. The check is
    # TR *and* schedule, both read from the locale-independent XML.
    #
    # BUG (2026-10): the comparison used to sit INSIDE the row-scan loop,
    # below the `break`, so it never ran and every Apply rewrote the task (a
    # transient schtasks failure then reported "failed" for a correct
    # schedule). It also treated an UNREADABLE xml (`None`) as a match; only a
    # positive confirmation may skip the rewrite.
    try:
        task_name = _task_name_for(extra_args)
        ok, _out = get_schedule_status(extra_args)
        if ok:
            exe, args = _get_executable_and_args(extra_args)
            want_tr = subprocess.list2cmdline([exe] + args)
            xml = _live_task_xml(task_name)
            if _task_run_matches(task_name, want_tr, xml=xml) is True \
                    and _xml_schedule_matches(xml, frequency, time_str) is True:
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
    except Exception as exc:
        from app.utils import log_or_swallow
        log_or_swallow(None, "scheduler.enable_schedule_probe", exc)
    cmd = _build_schtasks_cmd(frequency, time_str, extra_args)
    rc, out = run_capture(cmd, timeout=30)
    if rc == 0:
        # A-003: a missed 03:00 must not be skipped forever. Best-effort: an
        # older Windows or a refused settings write leaves the task working.
        try:
            _set_start_when_available(extra_args)
        except Exception as exc:
            from app.utils import log_or_swallow
            log_or_swallow(None, "scheduler.start_when_available", exc)
        # F08: single-lock RMW (was load; mutate; save with lock released).
        def _mut(cfg, extra_args=extra_args, frequency=frequency, time_str=time_str):
            if extra_args:
                cfg["schedule_update_enabled"] = True
            else:
                cfg["schedule_enabled"] = True
            cfg["schedule_frequency"] = frequency
            cfg["schedule_time"] = time_str
        update_config(_mut)
    return rc == 0, out


def disable_schedule(extra_args=None):
    """Delete the scheduled task (clean/repair task by default, or the
    Update Everything task with extra_args=["--auto-update"])."""
    task_name = _task_name_for(extra_args)
    cmd = [resolve_exe("schtasks"), "/Delete", "/TN", task_name, "/F"]
    rc, out = run_capture(cmd, timeout=30)
    if rc == 0:
        # F08: single-lock RMW.
        def _mut(cfg, extra_args=extra_args):
            if extra_args:
                cfg["schedule_update_enabled"] = False
            else:
                cfg["schedule_enabled"] = False
        update_config(_mut)
    return rc == 0, out


def get_schedule_status(extra_args=None):
    """Check if the scheduled task exists and get its details."""
    task_name = _task_name_for(extra_args)
    cmd = [resolve_exe("schtasks"), "/Query", "/TN", task_name, "/V", "/FO", "LIST"]
    rc, out = run_capture(cmd, timeout=30)
    return rc == 0, (out if rc == 0 else "")


# --------------------------------------------------------------------------- #
# A-003 — what the schedule has actually done
# --------------------------------------------------------------------------- #
# `schtasks /V` prints "Next Run Time"/"Last Run Time" in the OS LOCALE, and
# parsing a localized date is the same trap the drift check already avoids by
# reading the task XML instead. These fields come from Get-ScheduledTaskInfo as
# FILETIME integers (100ns ticks since 1601), which no locale can change, and
# the JSON shape is parsed by `parse_schedule_info` so the mapping is testable
# without a Task Scheduler.
_NEVER = 0
_FILETIME_EPOCH_OFFSET = 11_644_473_600  # seconds between 1601 and 1970


def _ps_quote(text) -> str:
    """Single-quote a value for a PowerShell literal."""
    return "'%s'" % str(text or "").replace("'", "''")


def _schedule_info_argv(extra_args=None):
    """The argv that reads one task's live state. Built by `ps_argv`."""
    from app.powershell import ps_argv

    name = _ps_quote(_task_name_for(extra_args))
    script = (
        "$ErrorActionPreference='Stop';"
        "$t=Get-ScheduledTask -TaskName %s;"
        "$i=Get-ScheduledTaskInfo -TaskName %s;"
        "[pscustomobject]@{"
        "last=if($i.LastRunTime){$i.LastRunTime.ToFileTimeUtc()}else{0};"
        "next=if($i.NextRunTime){$i.NextRunTime.ToFileTimeUtc()}else{0};"
        "result=[int64]$i.LastTaskResult;"
        "state=[string]$t.State}"
        "|ConvertTo-Json -Compress" % (name, name)
    )
    return ps_argv(script, non_interactive=True)


def _filetime_to_epoch(ticks):
    """FILETIME ticks -> Unix seconds; 0/None -> None (never ran / no next)."""
    try:
        ticks = int(ticks)
    except (TypeError, ValueError, OverflowError):
        return None
    if ticks <= _NEVER:
        return None
    return ticks / 10_000_000.0 - _FILETIME_EPOCH_OFFSET


def parse_schedule_info(raw) -> dict:
    """Get-ScheduledTaskInfo JSON -> {last_run, next_run, last_result, state}.

    Epoch seconds (or None) for the times, so a caller never has to know what
    FILETIME is. Anything unreadable yields an empty dict: the caller shows
    the switch without a claim, rather than inventing a run time.
    """
    import json

    if not raw:
        return {}
    try:
        data = json.loads(raw) if isinstance(raw, str) else dict(raw)
    except (TypeError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    out = {}
    last = _filetime_to_epoch(data.get("last"))
    nxt = _filetime_to_epoch(data.get("next"))
    if last is not None:
        out["last_run"] = last
    if nxt is not None:
        out["next_run"] = nxt
    try:
        out["last_result"] = int(data.get("result"))
    except (TypeError, ValueError):
        pass
    state = str(data.get("state") or "").strip()
    if state:
        out["state"] = state
    return out


def schedule_info(extra_args=None) -> dict:
    """Live last/next run for the scheduled task. {} when it cannot be read.

    One PowerShell process (~300 ms), so callers run it off the Tk thread.
    """
    try:
        rc, out = run_capture(_schedule_info_argv(extra_args), timeout=20)
    except Exception:
        return {}
    if rc != 0:
        return {}
    return parse_schedule_info(out)


#: The interval each frequency means, for deciding whether a schedule is late.
_INTERVAL_DAYS = {"daily": 1, "weekly": 7, "monthly": 30}


def describe_schedule(info, frequency="weekly", now=None) -> dict:
    """One honest sentence about the schedule's state.

    Returns {"text", "stale", "failed"}. `stale` means the last run is older
    than the interval by more than half of it again — the case the old switch
    reported as "On" while nothing had run for weeks. `failed` means the last
    run finished with a non-zero result.
    """
    now = time.time() if now is None else float(now)
    info = info if isinstance(info, dict) else {}
    last = info.get("last_run")
    nxt = info.get("next_run")
    result = info.get("last_result")
    interval = _INTERVAL_DAYS.get(str(frequency or "").lower(), 7) * 86400
    stale = False
    failed = bool(result not in (None, 0))
    if last is None:
        bits = ["Has not run yet"]
    else:
        from app.health_badge import age_text
        bits = ["Last ran %s" % age_text(last, now=now)]
        try:
            stale = (now - float(last)) > interval * 1.5
        except (TypeError, ValueError):
            stale = False
        if stale:
            bits.append("later than its %s schedule" % frequency)
    if nxt is not None:
        try:
            bits.append("next %s" % time.strftime("%a %H:%M",
                                                 time.localtime(float(nxt))))
        except (TypeError, ValueError, OverflowError):
            pass
    if failed:
        try:
            bits.append("last run failed (0x%08X)" % (int(result) & 0xFFFFFFFF))
        except (TypeError, ValueError):
            bits.append("last run failed")
    return {"text": "  \u00b7  ".join(bits), "stale": stale, "failed": failed}


def _set_start_when_available(extra_args=None) -> bool:
    """Turn on "Start when available" for the task, best-effort.

    schtasks.exe has no flag for it, so a run missed while the machine was off
    is skipped forever. Set-ScheduledTask is the documented way to change the
    setting after creation. Failure is not fatal: the schedule still exists,
    it just keeps the old catch-up behaviour, and the caller's Settings line
    will show the last run honestly either way.
    """
    from app.powershell import ps_argv

    name = _ps_quote(_task_name_for(extra_args))
    script = (
        "$n=%s;"
        "$t=Get-ScheduledTask -TaskName $n -ErrorAction Stop;"
        "$s=$t.Settings;$s.StartWhenAvailable=$true;"
        "Set-ScheduledTask -TaskName $n -Settings $s | Out-Null" % name
    )
    try:
        rc, _out = run_capture(ps_argv(script, non_interactive=True),
                               timeout=30)
        return rc == 0
    except Exception:
        return False


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
    except Exception as exc:
        from app.utils import log_or_swallow
        log_or_swallow(None, "scheduler.resync_schedule_persist", exc)
    return ok


def _auto_update_verdict(updated: int, failed: int, unreadable: int = 0) -> str:
    """The verdict a scheduled update's closing block carries.

    Pure, so the wording is asserted without running winget. The counts are
    the per-package results `update_all_apps` parses; it used to return
    nothing, so every successful scheduled update wrote "all apps current" -
    including the one that had just upgraded seven of them. 0/0 is that case
    only when winget named no package at all (nothing to update); a run that
    updated some and left others on their own installers says so.

    `unreadable` is the third case: winget prints "Successfully installed" in
    the Windows display language, so a localized run reports what it managed
    to read and says plainly how many results it could not — it never claims
    "all apps current" over packages it saw and could not follow.
    """
    if unreadable:
        counted = (f"{updated} updated, {failed} did not update, "
                   if updated or failed else "")
        return (f"Auto-update complete: {counted}{unreadable} result"
                f"{'' if unreadable == 1 else 's'} could not be read "
                f"(winget's output is localized).")
    if not updated and not failed:
        return "Auto-update complete: all apps current."
    if failed:
        return (f"Auto-update complete with warnings: {updated} updated, "
                f"{failed} did not update.")
    return f"Auto-update complete: {updated} updated, 0 failed."


def run_auto_update():
    """Headless 'Update Everything' run for the --auto-update schedule:
    winget upgrade --all with the same honest-logging contract as
    run_auto_clean. Returns (ok, summary)."""
    from app.utils import TaskCancelled
    from app.tasks.install_tasks import update_all_apps

    # ARCH-004 (3.4): one shared headless context (was a local AutoCtx that
    # duplicated run_auto_clean's). Only the print prefixes differ.
    ctx = print_context("[AUTO-UPD]", "[AUTO-UPD STATUS]")
    try:
        result = update_all_apps(ctx)
        summary, ok = (_auto_update_verdict(len(result.updated),
                                           len(result.failed),
                                           result.unreadable), True)
    except TaskCancelled as exc:
        # audit minor 2: update_all_apps used to return normally after a
        # cancelled/timeout stop, so this reported 'Auto-update complete'
        # while the log said the run was stopped. The stop now surfaces as
        # TaskCancelled — report it honestly (exit non-zero so Task
        # Scheduler sees the run did not finish).
        summary, ok = f"Auto-update cancelled: {exc}.", False
    except Exception as e:
        summary, ok = f"Auto-update failed: {e}", False
    # The verdict goes in the block, once: a windowed run ends this way and a
    # scheduled run's stdout is its whole transcript. The exception text rides
    # inside the block rather than on a line above it, so the failure is not
    # stated twice in a row.
    log_block(ctx.log, summary)
    return ok, summary


def run_auto_clean(selected_tasks_by_tab):
    """
    Run the auto-clean with pre-selected tasks.
    Called when the app is launched with --auto-clean.

    Ends its stdout with the same rule / verdict / rule block a windowed run
    writes to its transcript, so a schedule's captured log reads like the
    exported log a support report is read from.
    """
    ctx = print_context()
    # H8: TaskSkipped/TaskCancelled are no longer imported here — the
    # outcome mapping moved to app.runner.task_runner, which owns both.
    from app.tasks import clean_tasks, repair_tasks, tweak_tasks, game_tasks, advanced_tasks

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
        # This used to return quietly and rely on the caller's echo to show
        # anything at all. The block is now the line the caller reads, so a
        # fresh profile's scheduled clean says it skipped instead of looking
        # like a run that did nothing.
        summary = "Auto-clean skipped: no tasks were selected."
        log_block(ctx.log, summary)
        return False, summary
    
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
        except Exception as exc:
            from app.utils import log_or_swallow
            log_or_swallow(None, "scheduler.info_notices", exc)
        if warnings:
            # Log but don't block scheduled run — user not present to confirm
            print("[AUTO] Cross-tab warnings: " + " | ".join(warnings))
    except Exception as exc:
        from app.utils import log_or_swallow
        log_or_swallow(None, "scheduler.combo_check", exc)
    
    # Run synchronously (this is called from the scheduled task, not GUI).
    # ARCH-004 (3.4): the shared headless context lives in the runner now — the
    # local AutoCtx here duplicated run_auto_update's (and its `logs` list was
    # written but never read).
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
                log_block(ctx.log, _summary)
                return True, _summary
    except Exception as exc:
        from app.utils import log_or_swallow
        log_or_swallow(None, "scheduler.admin_gate", exc)

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

    # H8/ARCH-004: this loop used to re-implement the GUI runner's outcome
    # classification inline (int-means-bytes, bool-False-is-a-failure,
    # TaskSkipped is not a failure, TaskCancelled is not a failure) while
    # incrementing its own counters. Its comments said "mirrors the GUI
    # runner", which is exactly the maintenance hazard: the two copies
    # drifted, and the six silent-success tasks from audit MED-001 were the
    # result. Both surfaces now call app.runner.task_runner.
    #
    # The log wording below is unchanged; only the closing block was added, so
    # a scheduled run's transcript ends the way a windowed run's does.
    def _log_outcome(line: str) -> None:
        ctx.log(line)

    def _announce(task) -> None:
        ctx.log(f"Running: {task.label}")

    summary_obj = run_batch(ctx, tasks_to_run, mode="run", strict_ok=True,
                            log=_log_outcome, before=_announce)
    completed = summary_obj.succeeded
    skipped_n = summary_obj.skipped
    failed = summary_obj.failed
    total_bytes = summary_obj.bytes_freed

    if skipped_n:
        summary = (f"Auto-clean complete: {completed} succeeded, "
                   f"{skipped_n} skipped (nothing to change), {failed} failed.")
    else:
        summary = f"Auto-clean complete: {completed} succeeded, {failed} failed."
    if total_bytes > 0:
        from app.utils import format_bytes
        summary += f" Freed: {format_bytes(total_bytes)}"

    log_block(ctx.log, summary)
    return failed == 0, summary
