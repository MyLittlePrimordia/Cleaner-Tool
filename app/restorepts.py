"""System Restore points: what safety net you have, and how to add to it.

Tk-free by design (matches app/netconn.py, app/svcs.py, app/filelocks.py,
app/dupes.py): this is a sampler, the dialog lives in app/ui/dialogs/.

What each route can and cannot do, measured on this machine
----------------------------------------------------------
    Get-ComputerRestorePoint                 -> works non-admin, 125 ms, 0 rows
    Get-CimInstance root/default SystemRestore -> "Access denied" non-admin
    vssadmin list shadowstorage              -> "You don't have the correct
                                                permissions", non-admin
    Checkpoint-Computer                      -> needs elevation

So listing is done with `Get-ComputerRestorePoint` because it is the only
route that answers without admin, and creation is expected to need elevation
and says so plainly rather than failing silently.

The honest bit
--------------
A machine with zero restore points cannot be rolled back, and that is the
single most useful thing this tool can say. It is stated first, in plain
words, rather than buried under a count.

The one thing this tool will NOT do is RUN a restore. Rolling a machine back
is destructive by definition, Windows ships a purpose-built wizard for it,
and re-implementing it here would add risk and save nothing. The dialog says
where the wizard is instead.

Scope guard: deleting old restore points to free space is deliberately not
offered. It is a real feature and it belongs to the Clean side of the app
where disk accounting and confirmations already live, not bolted onto a
diagnostic popup.
"""
from __future__ import annotations

import datetime
import os
import re as _re
import subprocess
import sys

IS_WINDOWS = sys.platform.startswith("win")

#: One tab-separated row per restore point. The date is formatted IN
#: PowerShell on purpose: parsing a locale-formatted timestamp back in Python
#: is a reliable source of wrong dates, and this avoids the round trip.
_PS_LIST = (
    "$ErrorActionPreference='Stop';"
    "[Console]::OutputEncoding=[Text.Encoding]::UTF8;"
    "Get-ComputerRestorePoint | ForEach-Object {"
    "\"$($_.SequenceNumber)`t$($_.RestorePointType)`t"
    "$($_.CreationTime.ToString('yyyy-MM-dd HH:mm'))`t"
    "$($_.Description)\"};"
    # B-057: a fixed sentinel so the elevated runner can tell "listed zero
    # points successfully" (token present, no rows) from "the script never
    # produced output" (token absent).
    "Write-Output 'RESTORE_LIST_DONE'"
)

#: RestorePointType as PowerShell spells it -> what it means to a person.
TYPE_PLAIN = {
    "APPLICATION_INSTALL": "before an app was installed",
    "APPLICATION_UNINSTALL": "before an app was removed",
    "DEVICE_INSTALL": "before a driver was installed",
    "DEVICE_DRIVER_INSTALL": "before a driver was installed",
    "CUSTOM": "made by hand",
    "MODIFY_SETTINGS": "before settings were changed",
    "AUTOMATIC_CHECKPOINT": "made by Windows",
    "UNKNOWN": "made by something",
}

#: The stamp format _PS_LIST emits. Parsed back here rather than formatted
#: in PowerShell because the RELATIVE form ("3 days ago") needs a clock
#: comparison, and doing that against a locale-formatted string is how tools
#: end up confidently wrong.
_STAMP = "%Y-%m-%d %H:%M"


def _parse_stamp(when):
    """'2026-09-27 14:03' -> datetime, or None if it is not that shape."""
    try:
        return datetime.datetime.strptime(str(when).strip(), _STAMP)
    except (TypeError, ValueError):
        return None


def ago_plain(when) -> str:
    """"2026-09-27 14:03" -> "Today 14:03" / "Yesterday 09:12" / "3 days ago".

    The raw stamp is never shown to the user anywhere in the app: every other
    surface formats for humans, and a tool that prints an ISO timestamp into
    an otherwise plain-English dialog is the one thing on screen that looks
    like it was written by a machine.

    Returns "" when the input cannot be parsed, so a caller can fall back
    without having to know why.
    """
    stamp = _parse_stamp(when)
    if stamp is None:
        return ""
    now = datetime.datetime.now()
    delta = now - stamp
    secs = delta.total_seconds()
    if secs < 0:
        # A restore point stamped in the future means the clock moved; say
        # so rather than printing "in 3 days", which is nonsense here.
        return stamp.strftime("%d %b %H:%M")
    if secs < 90:
        return "Just now"
    days = (now.date() - stamp.date()).days
    if days == 0:
        return "Today %s" % stamp.strftime("%H:%M")
    if days == 1:
        return "Yesterday %s" % stamp.strftime("%H:%M")
    if days < 7:
        return "%d days ago" % days
    if days < 365:
        return stamp.strftime("%d %b")          # "12 Mar"
    return stamp.strftime("%d %b %Y")           # "12 Mar 2024"


def when_plain(when) -> str:
    """Short form for a list row: "Today 14:03" / "3 days ago" / "12 Mar"."""
    return ago_plain(when) or str(when or "")


def _run(argv, timeout: int):
    """subprocess.run with the house conventions. Returns (rc, stdout, err)."""
    try:
        p = subprocess.run(
            argv, capture_output=True, timeout=timeout,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except subprocess.TimeoutExpired:
        return None, "", "Windows did not respond in time."
    except FileNotFoundError:
        return None, "", "PowerShell is not available."
    except Exception as exc:                        # noqa: BLE001
        return None, "", str(exc)
    return (p.returncode,
            (p.stdout or b"").decode("utf-8", "replace"),
            (p.stderr or b"").decode("utf-8", "replace"))


def _parse_point(line: str):
    parts = line.split("\t")
    if len(parts) < 3:
        return None
    seq = parts[0].strip()
    kind = parts[1].strip() or "UNKNOWN"
    when = parts[2].strip()
    desc = parts[3].strip() if len(parts) > 3 else ""
    if not seq or not when:
        return None
    return {"seq": seq, "type": kind, "when": when,
            "desc": desc or "No description",
            "plain": TYPE_PLAIN.get(kind.upper(), "made by something")}


def list_points(timeout: int = 25) -> dict:
    """Existing restore points, newest first. Never raises."""
    if not IS_WINDOWS:
        return {"points": [], "error": "Restore points are Windows-only."}
    rc, out, err = _run(["powershell", "-NoProfile", "-NonInteractive",
                         "-Command", _PS_LIST], timeout)
    if rc is None:
        return {"points": [], "error": err or "Could not read restore points."}
    if rc != 0:
        blob = (err + out).lower()
        if "access" in blob and "denied" in blob:
            return {"points": [], "error":
                    "Windows will not list restore points without "
                    "administrator rights."}
        return {"points": [],
                "error": "Could not read restore points."}
    pts = []
    for line in out.splitlines():
        if line.strip():
            p = _parse_point(line)
            if p is not None:
                pts.append(p)
    # Newest first. The ISO-ish stamp sorts correctly as text, so no date
    # parsing and no timezone surprises.
    pts.sort(key=lambda p: p["when"], reverse=True)
    return {"points": pts, "error": None}


def protection_status(timeout: int = 20) -> dict:
    """Best-effort read of whether System Protection is switched on.

    There is no supported non-admin API for this, so the answer comes from
    the `DisableSR` DWORD documented at

        HKLM\\SOFTWARE\\Microsoft\\Windows NT\\CurrentVersion\\SystemRestore

        value absent -> enabled (the Windows 8+ default)
        0             -> enabled
        1             -> DISABLED
        2             -> enabled for some drives only

    BUG: this used to report `1` as "on" and had no branch that could ever
    return "off", so the one state a user most needs to be told - that they
    have no safety net at all - was both unreachable and, where it should
    have fired, actively reassuring. Corrected to the documented mapping.

    An absent KEY (not merely an absent value) means the feature has never
    been configured on this machine. That is reported as unknown rather than
    on or off: Windows enables System Protection by default from 8 onwards,
    so absence is genuinely not evidence either way.

    The VSS services being Stopped is NORMAL - they start on demand - so that
    is never reported as "off".

    Returns {"state": "on"|"limited"|"off"|"unknown", "detail": str}.
    """
    if not IS_WINDOWS:
        return {"state": "unknown", "detail": "Windows-only."}
    try:
        import winreg
    except ImportError:
        return {"state": "unknown", "detail": "No registry access."}

    key = r"SOFTWARE\Microsoft\Windows NT\CurrentVersion\SystemRestore"
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, key) as k:
            try:
                val, _t = winreg.QueryValueEx(k, "DisableSR")
            except FileNotFoundError:
                val = 0            # value absent == enabled
    except FileNotFoundError:
        return {"state": "unknown",
                "detail": "System Protection has never been set up on this PC."}
    except OSError as exc:
        return {"state": "unknown", "detail": "Could not read settings (%s)."
                                           % (exc,)}

    try:
        val = int(val)
    except (TypeError, ValueError):
        val = 0
    if val == 1:
        return {"state": "off",
                "detail": "System Protection is switched OFF."}
    if val == 2:
        return {"state": "limited",
                "detail": "System Protection is on for some drives only."}
    if val == 0:
        return {"state": "on",
                "detail": "System Protection is switched on."}
    return {"state": "unknown", "detail": "System Protection state unclear."}


def create(description: str = "Cleaner Tool - safety point",
           timeout: int = 180) -> dict:
    """Create a restore point. Safe: this ADDS protection, it removes nothing.

    Needs administrator rights; that is reported as such rather than as a
    generic failure. Windows allows one per 24 hours, which is reported as a
    skip with the reason rather than an error (BUG-013 already established
    that a throttle is not a fault).
    """
    if not IS_WINDOWS:
        return {"ok": False, "error": "Windows-only.", "skipped": False}
    safe = (description or "").replace("'", "''").strip()[:120] \
        or "Cleaner Tool - safety point"
    from app.utils import restore_point_create_ps
    script = restore_point_create_ps(safe)
    rc, out, err = _run(["powershell", "-NoProfile", "-NonInteractive",
                         "-Command", script], timeout)
    blob = (out + err)
    low = blob.lower()
    if rc is None:
        return {"ok": False, "error": err or "Windows did not respond.",
                "skipped": False}

    # The 24-hour rule: a throttle is a skip, not a failure. B-056: match the
    # language-neutral RESTORE_THROTTLED token the script prints first; the
    # English phrasings remain only as a fallback for unexpected output.
    if "restore_throttled" in low or "1440 minutes" in low or \
            "restore point cannot be created" in low or \
            ("a new system restore point" in low and "cannot be created" in low):
        return {"ok": True, "skipped": True, "error": None,
                "message": "Windows allows only one restore point a day, and "
                           "one was already made today. Nothing to do."}
    if rc == 0 or "restore_ok" in low:
        return {"ok": True, "skipped": False, "error": None,
                "message": "Restore point created."}
    if "access" in low and "denied" in low:
        return {"ok": False, "skipped": False,
                "error": "Creating a restore point needs administrator "
                         "rights. Restart Cleaner Tool as administrator "
                         "and try again."}
    if "disabled" in low or "not enabled" in low:
        return {"ok": False, "skipped": False,
                "error": "System Protection is switched off for this drive, "
                         "so restore points cannot be made. Turn it on in "
                         "Settings > System > About > System protection."}
    return {"ok": False, "skipped": False,
            "error": "Windows could not create a restore point."}


def verdict_for(points: list) -> str:
    """The most useful sentence this tool can say, said first.

    Zero restore points is a WARNING, not a zero: a machine with no way back
    is the reason to open this window at all.
    """
    if not points:
        return ("You have NO restore points. If something goes wrong, there "
                "is nothing to go back to.")
    n = len(points)
    newest = points[0]
    when = ago_plain(newest.get("when") or "")
    if n == 1:
        return "1 restore point, made %s." % (when or "recently")
    return "%d restore points. The newest is from %s." % (n, when or "recently")


# --------------------------------------------------------------- extra reads
def _to_float_num(tok: str):
    """Parse a vssadmin number that may use '.' or ',' as the decimal mark.

    Returns a float, or None when the token is not a number.
    """
    try:
        if "." in tok and "," in tok:
            # The LAST separator is the decimal point; the other is thousands.
            dec = max(tok.rfind("."), tok.rfind(","))
            whole = tok[:dec].replace(".", "").replace(",", "")
            frac = tok[dec + 1:]
            return float((whole or "0") + ("." + frac if frac else ""))
        if "," in tok:
            tail = len(tok) - tok.rfind(",") - 1
            return float(tok.replace(",", ".") if 1 <= tail <= 2
                         else tok.replace(",", ""))
        if "." in tok:
            tail = len(tok) - tok.rfind(".") - 1
            return float(tok if 1 <= tail <= 2 else tok.replace(".", ""))
        return float(tok)
    except ValueError:
        return None


def space_used(timeout: int = 25) -> dict:
    """How much disk the shadow copies are taking, best effort.

    The question a user opens this tool with a full disk is often "why is
    there no room?", and "System Restore is holding N GB" is the answer.
    Without this the dialog can list restore points but cannot say whether
    they are cheap or expensive, which makes "should I delete old ones?"
    unanswerable.

    `vssadmin list shadowstorage` needs administrator rights, so this
    returns {"ok": False} on the non-elevated path and the caller simply does
    not draw the tile. It is called on the same worker as the point listing
    and is never allowed to fail the dialog.

    Returns {"ok": bool, "bytes": int, "detail": str}.
    """
    if not IS_WINDOWS:
        return {"ok": False, "bytes": 0, "detail": "Windows-only."}
    rc, out, err = _run(["vssadmin", "list", "shadowstorage"], timeout)
    blob = (out or "") + (err or "")
    low = blob.lower()
    if rc != 0 or "you don't have the correct permissions" in low:
        return {"ok": False, "bytes": 0,
                "detail": "Needs administrator rights."}
    total = 0
    found = False
    for line in (out or "").splitlines():
        if "used shadow copy storage space" not in line.lower():
            continue
        found = True
        # B-0xx: vssadmin prints e.g. "1.234 GB". The old digit-collector
        # stopped at the '.', so 1.234 GB was read as 1 GB and 0.9 GB as 0.
        m = _re.search(r"[0-9][0-9.,]*[0-9]|[0-9]", line)
        if not m:
            continue
        value = _to_float_num(m.group(0))
        if value is None:
            continue
        unit = line.lower()
        if "gb" in unit:
            value *= 1024 ** 3
        elif "mb" in unit:
            value *= 1024 ** 2
        elif "kb" in unit:
            value *= 1024
        total += int(value)
    if not found:
        return {"ok": False, "bytes": 0, "detail": "Windows did not say."}
    return {"ok": True, "bytes": total, "detail": "used by restore points"}


def open_system_protection():
    """Open Windows' own System Protection window.

    The dialog's whole job when protection is off or partial is to get the
    user to the one screen that can change it. Handing them a sentence
    naming a four-level Settings path and stopping there is how a tool that
    can diagnose a problem but not help with it earns its reputation.

    Measured on this machine: SystemPropertiesProtection.exe refuses to start
    unelevated (ShellExecute ERROR_ELEVATION_REQUIRED, 740), so this has to go
    through the UAC verb. Declining is a normal outcome.

    Returns (ok, reason) - reason is "cancelled" or "failed".
    """
    if not IS_WINDOWS:
        return False, "unsupported"
    exe = os.path.join(os.environ.get("WINDIR", r"C:\Windows"),
                       "System32", "SystemPropertiesProtection.exe")
    if not os.path.isfile(exe):
        return False, "missing"
    from app.infra.elevated_run import run_elevated_exe
    return run_elevated_exe(exe)


def open_system_restore() -> bool:
    """Open Windows' own System Restore wizard (rstrui.exe).

    This tool deliberately never RUNS a restore - rolling back is destructive
    and Windows ships a purpose-built, reviewable wizard for it. What it can
    do is take the user to that wizard in one click instead of leaving them
    to find it. rstrui raises its own UAC prompt, so no elevation is needed
    to launch it. Returns True when the process was started.
    """
    if not IS_WINDOWS:
        return False
    exe = os.path.join(os.environ.get("WINDIR", r"C:\Windows"),
                       "System32", "rstrui.exe")
    if not os.path.isfile(exe):
        return False
    try:
        import subprocess
        subprocess.Popen([exe], close_fds=True)
        return True
    except Exception:
        return False


# --------------------------------------------------------------- elevation
def is_elevated() -> bool:
    """True when this process already holds administrator rights.

    Delegates to app.infra/elevated_run.py, which is the single owner of that
    question. This module used to keep its own copy; a version of it silently
    answered "not elevated" unconditionally because it imported a function
    name that does not exist in app/elevation.py.
    """
    from app.infra.elevated_run import is_elevated as _e
    return _e()


def run_elevated(script: str, timeout: int = 120):
    """Delegate to the shared implementation.

    This module used to carry its own copy of the ShellExecuteW "runas"
    runner. app/svcs.py needed the same thing, so it now lives in
    app/infra/elevated_run.py - one implementation, one set of bugs.
    """
    from app.infra.elevated_run import run_elevated as _r
    return _r(script, timeout)


def list_points_elevated(timeout: int = 120) -> dict:
    """list_points, but with a UAC prompt. Same return shape."""
    ok, text = run_elevated(_PS_LIST, timeout)
    # B-057: with the empty-output guard in run_elevated, an empty result now
    # means the script never produced output; require the sentinel to be sure
    # the listing actually ran (zero rows is still a valid, token-bearing run).
    if not ok or "RESTORE_LIST_DONE" not in (text or ""):
        return {"points": [], "error": None, "cancelled": True,
                "needs_admin": True}
    pts = []
    for line in text.splitlines():
        if line.strip():
            p = _parse_point(line)
            if p is not None:
                pts.append(p)
    pts.sort(key=lambda p: p["when"], reverse=True)
    return {"points": pts, "error": None, "cancelled": False,
            "needs_admin": False}


def create_elevated(description: str = "Cleaner Tool - safety point",
                    timeout: int = 240) -> dict:
    """create(), with a UAC prompt."""
    safe = (description or "").replace("'", "''").strip()[:120] \
        or "Cleaner Tool - safety point"
    from app.utils import restore_point_create_ps
    ok, text = run_elevated(restore_point_create_ps(safe), timeout)
    if not ok:
        return {"ok": False, "skipped": False, "cancelled": True,
                "error": None}
    low = (text or "").lower()
    # B-056: language-neutral tokens first (the English phrasings are a
    # fallback only) so a throttled create is not misread as a hard failure on
    # a non-English Windows.
    if "restore_throttled" in low or "1440 minutes" in low or "cannot be created" in low:
        return {"ok": True, "skipped": True, "cancelled": False,
                "error": None,
                "message": "Windows allows only one restore point a day, "
                           "and one was already made today. Nothing to do."}
    if "restore_ok" in low:
        return {"ok": True, "skipped": False, "cancelled": False,
                "error": None, "message": "Restore point created."}
    if "disabled" in low or "not enabled" in low:
        return {"ok": False, "skipped": False, "cancelled": False,
                "error": "System Protection is switched off for this drive, "
                         "so restore points cannot be made."}
    if "denied" in low:
        return {"ok": False, "skipped": False, "cancelled": False,
                "error": "Windows refused to create a restore point."}
    return {"ok": False, "skipped": False, "cancelled": False,
            "error": "Windows could not create a restore point."}
