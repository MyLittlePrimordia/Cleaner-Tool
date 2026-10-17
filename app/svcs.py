"""Windows services: what is installed, what is running, and what it is FOR.

Tk-free by design (matches app/netconn.py, app/filelocks.py, app/dupes.py):
this is a sampler, the dialog that shows it lives in app/ui/dialogs/.

Why not the Service Control Manager
-----------------------------------
The obvious implementation is `OpenSCManager` + `EnumServicesStatusExW`. It
does not work here. Measured on this machine with a non-elevated token:

    OpenSCManagerW(NULL, NULL, SC_MANAGER_ENUMERATE_SERVICE)   -> OK
    EnumServicesStatusExW(scm, SERVICE_ENUM_ALL_INFO, ...)     -> count=0

for every combination of SC_ENUM_TYPE (0/1/2) and SC_STATUS_TYPE (1/4), with
Win32 error 1783 or 124 each time. `SC_MANAGER_ALL_ACCESS` and
`SC_MANAGER_CONNECT` are refused outright (error 5), so that route is only
even available elevated, and then it still enumerates nothing. Two of those
were my own mistakes on the way - SC_ENUM_TYPE is 0-based, so
SERVICE_ENUM_ALL_INFO is 0 and not 1 - but the brute force over both enums is
what settled it.

Where the data comes from, and what each source is for
------------------------------------------------------
    Get-CimInstance Win32_Service   379 ms, no admin. State, StartMode,
                                    Description (281/288 in plain English),
                                    PathName, StartName, ServiceType, PID.
    Registry HKLM\\..\\Services      27 ms, no admin. The exact `Start` DWORD,
                                    `DelayedAutostart`, `DependOnService`.
    VersionInfo.CompanyName         publisher, offline, via the same call.

Both halves are needed and neither replaces the other:

* WMI's `StartMode` is a friendly string and it LOSES the distinction between
  "Automatic" and "Automatic (Delayed Start)" - which is exactly the
  distinction that explains a slow boot. The registry DWORD has it, and 23
  services here use it.
* The registry's `Description` is usually an MUI reference
  (`@%SystemRoot%\\system32\\Foo.dll,-101`), not readable English. WMI
  resolves it.

The publisher is looked up through a path cache inside the script: about a
hundred services are hosted by the same `svchost.exe`, and asking for its
version resource once per service cost 1066 ms instead of 379 ms.

The "is this Windows or did I install it?" question is answered by
CompanyName, which is what Autoruns uses. The earlier name blocklist in this
module was a guess; this is evidence.

What it changes, and what it refuses to
-------------------------------------
This module DOES start, stop, restart, pause and resume services, and change
their startup type - apply_change() and undo_last_change() are here, guarded
three ways:

  * every action needs administrator rights, and a declined UAC prompt is
    reported as `cancelled`, which is a normal outcome and not a failure;
  * never_change() refuses the session-critical and boot-start entries, and
    change_refusal() additionally refuses to DISABLE anything another service
    depends on - stopping it is allowed, because dependants survive a restart
    but a disabled dependency does not come back;
  * the previous value is recorded BEFORE the change, so undo_last_change()
    can put it back even if the app is closed immediately afterwards.

What is still absent, deliberately: DeleteService, Remove-Service and
New-Service. Creating or removing a service is not something a cleanup tool
should do on a user's machine, at any privilege level.
"""
from __future__ import annotations

import ctypes
import os
import subprocess
import sys

IS_WINDOWS = sys.platform.startswith("win")

#: Win32_Service.StartMode says "Auto"; Get-Service.StartType says
#: "Automatic". One spelling in, one spelling out.
_START_TYPE_ALIASES = {"Auto": "Automatic", "Automatic": "Automatic",
                       "Manual": "Manual", "Disabled": "Disabled",
                       "Boot": "Boot", "System": "System"}


def _canonical_start_type(value: str) -> str:
    v = (value or "").strip() or "Unknown"
    return _START_TYPE_ALIASES.get(v, v)

#: One tab-separated row per service, from a single non-elevated call.
#: Single-quoted PowerShell strings throughout so the script embeds in Python
#: without escaping games. Whitespace inside every field is collapsed, so no
#: description can inject a tab and shift the remaining columns.
_PS = r"""
$ErrorActionPreference = 'SilentlyContinue'
[Console]::OutputEncoding = [Text.Encoding]::UTF8
$cache = @{}
Get-CimInstance Win32_Service | ForEach-Object {
  $p = $_.PathName
  $exe = ''
  if ($p -match '^"([^"]+\.exe)"') { $exe = $matches[1] }
  elseif ($p -match '^([^"]+?\.exe)') { $exe = $matches[1].Trim() }
  $comp = ''
  if ($exe) {
    if ($cache.ContainsKey($exe)) { $comp = $cache[$exe] }
    else {
      $comp = (Get-Item -LiteralPath $exe -ErrorAction SilentlyContinue).VersionInfo.CompanyName
      if (-not $comp) { $comp = '' }
      $cache[$exe] = $comp
    }
  }
  $d = ($_.Description -replace '\s+', ' ').Trim()
  $pp = ($p -replace '\s+', ' ').Trim()
  "$($_.Name)`t$($_.DisplayName)`t$($_.State)`t$($_.StartMode)`t$d`t$pp`t$($_.StartName)`t$comp`t$($_.ProcessId)"
}
"""

#: Fallback when WMI is unavailable or broken. Four fields only, 2 ms, and it
#: is the same route the first version of this module used.
_PS_FALLBACK = (
    "$ErrorActionPreference='Stop';"
    "[Console]::OutputEncoding=[Text.Encoding]::UTF8;"
    "Get-Service | ForEach-Object {"
    "\"$($_.Name)`t$($_.DisplayName)`t$($_.Status)`t$($_.StartType)\"}"
)

_FIELDS = ("name", "display", "status", "start_mode", "description", "path",
           "account", "publisher", "pid")

#: Registry `Start` DWORD -> (plain wording, is_auto, is_disabled).
#: 0 Boot, 1 System, 2 Automatic, 3 Manual, 4 Disabled.
_START_DWORD = {
    0: ("starts when Windows boots", False, False),
    1: ("starts when Windows boots", False, False),
    2: ("starts when Windows starts", True, False),
    3: ("starts only when something needs it", False, False),
    4: ("never starts - switched off", False, True),
}

#: Startup type -> what it actually means. "Automatic" on its own tells a
#: newcomer nothing; this is the sentence they actually need.
START_PLAIN = {
    "Auto": "starts when Windows starts",
    "Automatic": "starts when Windows starts",
    "Manual": "starts only when something needs it",
    "Disabled": "never starts - switched off",
    "Unknown": "startup type unknown",
}

#: Status -> short plain label.
STATUS_PLAIN = {
    "Running": "Running",
    "Stopped": "Stopped",
    "StartPending": "Starting",
    "StopPending": "Stopping",
    "PausePending": "Pausing",
    "Paused": "Paused",
    "ContinuePending": "Resuming",
}

#: The account a service runs as, in words. "NT AUTHORITY\LocalService" is
#: not something a newcomer can act on.
_ACCOUNT_PLAIN = (
    ("localsystem", "the system itself - full control"),
    ("networkservice", "a built-in low-privilege account"),
    ("localservice", "a built-in low-privilege account"),
    ("nt authority\\system", "the system itself - full control"),
)

_SVCS_KEY = r"SYSTEM\CurrentControlSet\Services"


def is_elevated() -> bool:
    """True when this process already holds administrator rights.

    Re-exported from app.infra.elevated_run so callers do not need to know
    which module owns it, and so there is ONE implementation.
    """
    from app.infra.elevated_run import is_elevated as _e
    return _e()


def account_plain(account: str) -> str:
    """`NT AUTHORITY\\LocalService` -> 'a built-in low-privilege account'."""
    a = (account or "").strip()
    if not a:
        return ""
    low = a.lower()
    for needle, text in _ACCOUNT_PLAIN:
        if needle in low:
            return text
    if low.startswith(".\\") or "\\" not in a:
        return "your own account"
    return "another account"


def is_microsoft(publisher: str) -> bool:
    """True when the publisher says Microsoft.

    This is the check Autoruns uses, and it is evidence rather than the name
    guessing the first version of this module did. Anything we cannot
    attribute is treated as NOT Microsoft, so an unattributed entry shows up
    for inspection instead of being quietly hidden.
    """
    return "microsoft" in (publisher or "").lower()


def registry_facts(names) -> dict:
    """{name: {"start": int, "delayed": bool, "deps": [str]}}.

    27 ms for 760 service keys, no elevation. This is where the exact startup
    DWORD and the dependency list come from; WMI's friendly StartMode cannot
    express "Automatic (Delayed Start)".
    """
    out = {}
    if not IS_WINDOWS:
        return out
    try:
        import winreg
    except ImportError:
        return out
    wanted = set(names)
    # Two Python 3.14 / winreg traps, both hit here and both silent-looking:
    #   * a `PyHKEY` exposes NO methods at all (no .EnumKey, no .Name) - only
    #     the module-level winreg.EnumKey(handle, i) works;
    #   * passing int(handle) instead DOES enumerate but stops after one key,
    #     because winreg reads a plain int as a predefined HKEY *constant*
    #     rather than as a live handle. Verified: 760 keys via the object,
    #     1 key via int().
    # Subkeys are therefore opened by FULL PATH from HKEY_LOCAL_MACHINE,
    # which sidesteps relative-handle handling altogether.
    try:
        root = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, _SVCS_KEY)
    except OSError:
        return out
    i = 0
    while True:
        try:
            name = winreg.EnumKey(root, i)
        except OSError:
            break
        i += 1
        if wanted and name not in wanted:
            continue
        facts = {}
        try:
            with winreg.OpenKey(
                    winreg.HKEY_LOCAL_MACHINE, _SVCS_KEY + "\\" + name) as sk:
                for key in ("Start", "DelayedAutostart"):
                    try:
                        facts[key] = int(winreg.QueryValueEx(sk, key)[0])
                    except (FileNotFoundError, TypeError, ValueError):
                        pass
                try:
                    v = winreg.QueryValueEx(sk, "DependOnService")[0]
                    if isinstance(v, (list, tuple)):
                        facts["deps"] = [x for x in v if x]
                    elif isinstance(v, str) and v.strip():
                        # Old style arrives NUL-joined inside one string.
                        facts["deps"] = [
                            x for x in v.replace("\x00", " ").split() if x]
                except FileNotFoundError:
                    pass
        except OSError:
            continue
        if facts:
            out[name] = {
                "start": facts.get("Start"),
                "delayed": facts.get("DelayedAutostart") == 1,
                "deps": facts.get("deps", []),
            }
    return out


def _run(argv, timeout):
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


def _split(line: str, fields=_FIELDS):
    """One tab-separated row -> a service dict, or None if it is malformed.

    A row with fewer fields than expected is a row we cannot render
    honestly, so it is dropped rather than padded with invented values.
    """
    parts = line.split("\t")
    if len(parts) < 4:
        return None
    parts += [""] * (len(fields) - len(parts))
    row = dict(zip(fields, parts))
    name = row["name"].strip()
    if not name:
        return None
    row["name"] = name
    row["display"] = row["display"].strip() or name
    row["status"] = row["status"].strip() or "Unknown"
    # B-0xx: Win32_Service.StartMode reports "Auto", while Get-Service's
    # StartType reports "Automatic". _START_TYPES / undo validation only knew
    # the latter, so any AUTOMATIC service recorded "Auto" as its prior state
    # and Undo then rejected it ("saved previous setting is unusable") and
    # dropped the record. Normalise once, here, so the whole module sees one
    # spelling.
    row["start_mode"] = _canonical_start_type(row["start_mode"])
    return row


def _decorate(row: dict, reg: dict) -> dict:
    """Attach the derived fields the dialog and filters rely on."""
    facts = reg.get(row["name"]) or {}
    start = facts.get("start")
    row["start_dword"] = start
    row["delayed"] = bool(facts.get("delayed"))
    row["deps"] = list(facts.get("deps") or [])
    row["running"] = row["status"] == "Running"
    row["disabled"] = row["start_mode"] == "Disabled" or start == 4
    row["auto"] = row["start_mode"] in ("Auto", "Automatic") or start == 2
    row["microsoft"] = is_microsoft(row.get("publisher"))
    row["account_plain"] = account_plain(row.get("account"))

    if row["delayed"]:
        row["start_plain"] = ("starts a couple of minutes after Windows, "
                              "so boot feels faster")
    elif start in _START_DWORD:
        row["start_plain"] = _START_DWORD[start][0]
    else:
        row["start_plain"] = START_PLAIN.get(row["start_mode"],
                                             row["start_mode"])
    try:
        row["pid"] = int(row.get("pid") or 0)
    except (TypeError, ValueError):
        row["pid"] = 0
    return row


def services(timeout: int = 60) -> dict:
    """{"services": [...], "error": str|None, "source": str}. Never raises."""
    if not IS_WINDOWS:
        return {"services": [], "error": "Services are Windows-only.",
                "source": "none"}
    rc, out, err = _run(["powershell", "-NoProfile", "-NonInteractive",
                         "-Command", _PS], timeout)

    rows = []
    source = "wmi"
    if rc == 0:
        for line in out.splitlines():
            if line.strip():
                r = _split(line)
                if r is not None:
                    rows.append(r)

    # WMI can be disabled, broken or slow on some machines. Falling back keeps
    # the tool useful with four fields instead of nine, and SAYS so via
    # `source` rather than pretending the detail is there.
    if not rows:
        rc2, out2, err2 = _run(["powershell", "-NoProfile", "-NonInteractive",
                                "-Command", _PS_FALLBACK], timeout)
        source = "fallback"
        if rc2 == 0:
            for line in out2.splitlines():
                if line.strip():
                    r = _split(line, ("name", "display", "status", "start_mode"))
                    if r is not None:
                        rows.append(r)
        if not rows:
            blob = (err2 or err or "").strip()
            return {"services": [], "source": "none",
                    "error": blob.splitlines()[-1] if blob
                    else "Could not list services."}

    reg = registry_facts([r["name"] for r in rows])
    rows = [_decorate(r, reg) for r in rows]
    return {"services": rows, "error": None, "source": source}


def summarize(data: dict) -> dict:
    """Counts for the headline. Plain numbers, no judgement."""
    rows = (data or {}).get("services") or []
    running = [r for r in rows if r["running"]]
    return {
        "total": len(rows),
        "running": len(running),
        "stopped": len(rows) - len(running),
        "auto": len([r for r in rows if r.get("auto")]),
        "disabled": len([r for r in rows if r.get("disabled")]),
        "delayed": len([r for r in rows if r.get("delayed")]),
        "third_party": len([r for r in rows if not r.get("microsoft")]),
        "described": len([r for r in rows if r.get("description")]),
        "deps": len([r for r in rows if r.get("deps")]),
        "source": (data or {}).get("source", "none"),
        "error": (data or {}).get("error"),
    }


def verdict_for(summary: dict) -> str:
    """One plain sentence for the top of the dialog."""
    if summary.get("error"):
        return summary["error"]
    run = int(summary.get("running") or 0)
    total = int(summary.get("total") or 0)
    if total <= 0:
        return "No services found."
    if run == 0:
        return "No services are running (unusual - Windows needs some)."
    base = ("1 service is running, out of %d." % total if run == 1
            else "%d of %d services are running." % (run, total))
    third = int(summary.get("third_party") or 0)
    if third:
        base += " %d are not from Microsoft." % third
    return base


def filter_services(rows, *, query: str = "", only: str = "") -> list:
    """Search + filter. `only` is one of "", "running", "stopped", "auto",
    "disabled", "delayed", "third", "described", "deps".

    Sorted running-first, then alphabetically, because the question is
    usually "what is running that I do not recognise".
    """
    q = (query or "").strip().lower()
    out = []
    for r in rows:
        if only == "running" and not r["running"]:
            continue
        if only == "stopped" and r["running"]:
            continue
        if only == "auto" and not r.get("auto"):
            continue
        if only == "disabled" and not r.get("disabled"):
            continue
        if only == "delayed" and not r.get("delayed"):
            continue
        if only == "third" and r.get("microsoft"):
            continue
        if only == "described" and not r.get("description"):
            continue
        if only == "deps" and not r.get("deps"):
            continue
        if q:
            hay = " ".join((
                r.get("display") or "", r["name"],
                r.get("description") or "", r.get("publisher") or "",
                r.get("path") or "")).lower()
            if q not in hay:
                continue
        out.append(r)
    out.sort(key=lambda r: (not r["running"], r["display"].lower()))
    return out


def dependents_of(name: str, rows) -> list:
    """Which services list `name` in their DependOnService.

    Used to answer "what breaks if I stop this?" - disabling a service that
    three others depend on breaks them silently, which is how the endless
    "PC speedup" service guides cause damage.
    """
    key = (name or "").strip().lower()
    if not key:
        return []
    out = []
    for r in rows:
        for d in (r.get("deps") or []):
            if d.strip().lower() == key:
                out.append(r["display"])
                break
    return out


#: Services Windows cannot boot without. Deliberately short: a long list
#: trains the user to click past refusals, and a broad list stops the tool
#: being useful. Everything else is judged on evidence (dependents, startup
#: type) rather than on membership of a list.
#:
#: Windows Audio is NOT here on purpose - stopping it is a reversible,
#: ordinary thing a user might want, and refusing it would be the tool
#: deciding what is best for someone who can undo it in one click.
_NEVER_CHANGE = frozenset((
    "rpcss", "dcomlaunch", "lsm", "power", "bfe", "eventlog", "plugplay",
    "profsvc", "samSs".lower(), "schedule", "themes", "nlasvc", "netlogon",
    "windefend", "securityhealthservice", "mpssvc", "w32time", "cryptsvc",
))


def never_change(name: str) -> bool:
    """True for the short list above, matched on bare name."""
    return (name or "").strip().lower() in _NEVER_CHANGE


#: Startup-type changes are the persistent ones, so they are what the undo
#: record stores. Start/stop is transient - a reboot undoes it - and putting
#: every stop into a log would fill it with noise.
_UNDO_KEY = "service_change_undo"


def record_start_type(name: str, old_start_mode: str) -> bool:
    """Remember a service's previous startup type so it can be put back."""
    if not (name or "").strip():
        return False
    try:
        from app.config_persist import load_config, save_config
        cfg = load_config()
        log = cfg.get(_UNDO_KEY) or []
        if not isinstance(log, list):
            log = []
        log = [e for e in log
               if not (isinstance(e, dict) and e.get("name") == name)]
        log.append({"name": name, "start": old_start_mode})
        cfg[_UNDO_KEY] = log[-50:]
        save_config(cfg)
        return True
    except Exception:
        return False


def undo_record() -> list:
    try:
        from app.config_persist import load_config
        log = load_config().get(_UNDO_KEY) or []
        return [e for e in log if isinstance(e, dict)] if isinstance(
            log, list) else []
    except Exception:
        return []


def clear_undo_record() -> bool:
    try:
        from app.config_persist import load_config, save_config
        cfg = load_config()
        cfg[_UNDO_KEY] = []
        save_config(cfg)
        return True
    except Exception:
        return False


def pop_undo_record() -> bool:
    """Drop ONLY the most recent entry, keeping the ones before it.

    BUGFIX 2026-10: undo_last_change used to call clear_undo_record(), which
    sets the entire log to []. The log is a STACK of changes, so undoing the
    newest one also destroyed the record of every change before it: undo
    three services, press Undo once, and the other two become permanently
    unrecoverable - the previous start type was the only copy and it was
    deleted. Undo should be repeatable, not one-shot.

    The name is deliberately not "undo_clear": clear_undo_record() keeps its
    wipe-everything meaning because it is also the honest "forget everything"
    operation.
    """
    try:
        from app.config_persist import load_config, save_config
        cfg = load_config()
        log = cfg.get(_UNDO_KEY)
        if not isinstance(log, list) or not log:
            return False
        cfg[_UNDO_KEY] = log[:-1]
        save_config(cfg)
        return True
    except Exception:
        return False


def undo_last_change() -> dict:
    """Put the most recent startup-type change back.

    Returns {"ok", "cancelled", "error", "message", "name"}.
    """
    log = undo_record()
    if not log:
        return {"ok": False, "cancelled": False, "name": None,
                "error": "There is nothing to undo."}
    entry = log[-1]
    name = entry.get("name")
    start = entry.get("start")
    if not name or start not in _START_TYPES:
        pop_undo_record()
        return {"ok": False, "cancelled": False, "name": name,
                "error": "The saved previous setting is unusable."}
    res = apply_change(name, start)
    if res.get("ok"):
        # Pop, don't clear - see pop_undo_record(). Clearing here meant a
        # single Undo erased the history of every other change.
        pop_undo_record()
        res["name"] = name
    return res


def change_refusal(service: dict, rows=None, new_start=None):
    """Why this change is refused, or "" when it is allowed.

    The rules, in order, each with a sentence the UI can show verbatim:

    1. Boot-start and System-start entries (registry Start 0/1) load before the
       OS is usable. Measured: every Start=0/1 entry in this machine's service
       key is a DRIVER rather than a service, so Win32_Service never returns
       one - the rule is kept anyway because it costs nothing and the day a
       boot service appears it is the one that matters.
    2. A service other services depend on cannot be disabled. Stopping it is
       allowed (the dependants usually survive a restart), disabling it is
       not: they would fail to start afterwards.
    3. The short critical list is never touched at all.

    Returns (allowed: bool, reason: str).
    """
    if not service:
        return False, "No service selected."
    name = service.get("name") or ""
    display = service.get("display") or name

    if service.get("start_dword") in (0, 1):
        return False, ("%s loads before Windows is ready, so changing it "
                       "could stop the PC booting." % display)

    if never_change(name):
        return False, ("%s is part of Windows itself. Changing it can break "
                       "the system." % display)

    if new_start in ("Disabled", 4):
        fans = dependents_of(name, rows or [])
        if fans:
            shown = ", ".join(fans[:3])
            more = "" if len(fans) <= 3 else " (+%d more)" % (len(fans) - 3)
            return False, ("%d other service(s) need %s - %s%s. Stopping it "
                           "is fine; disabling it would stop them starting."
                           % (len(fans), display, shown, more))
    return True, ""


#: Startup types the UI offers, mapped to the PowerShell spelling. "Auto" is
#: accepted too so undo records written by older versions (which stored the raw
#: WMI "Auto") still restore; _service_script canonicalises it.
_START_TYPES = {"Automatic", "Manual", "Disabled", "Auto"}


def _service_script(name: str, action: str) -> str:
    """The PowerShell for one operation, with the name quoted safely."""
    from app.infra.elevated_run import quote_ps
    q = quote_ps(name)
    if action in _START_TYPES:
        verb = "Set-Service -Name %s -StartupType %s" % (
            q, quote_ps(_canonical_start_type(action)))
    elif action == "Start":
        verb = "Start-Service -Name %s" % q
    elif action == "Stop":
        verb = "Stop-Service -Name %s -Force" % q
    elif action == "Restart":
        verb = "Restart-Service -Name %s -Force" % q
    elif action == "Pause":
        verb = "Suspend-Service -Name %s" % q
    elif action == "Continue":
        verb = "Resume-Service -Name %s" % q
    else:
        raise ValueError("unknown action %r" % action)
    return ("$ErrorActionPreference='Stop';%s;"
            "'CLEANER_OK'" % verb)


def apply_change(name: str, action: str, timeout: int = 90) -> dict:
    """Start / stop / restart / pause / resume / set startup type.

    Requires administrator rights; a declined UAC prompt is reported as
    `cancelled`, not as a failure. Never raises.

    Returns {"ok", "cancelled", "error", "message"}.
    """
    if not IS_WINDOWS:
        return {"ok": False, "cancelled": False, "error": "Windows-only."}
    from app.infra.elevated_run import is_elevated, run_elevated
    try:
        script = _service_script(name, action)
    except ValueError as exc:
        return {"ok": False, "cancelled": False, "error": str(exc)}

    # Already elevated: run it inline. Going through ShellExecuteW anyway
    # would raise a pointless second UAC prompt.
    if is_elevated():
        import subprocess
        try:
            p = subprocess.run(
                ["powershell", "-NoProfile", "-NonInteractive", "-Command",
                 script], capture_output=True, timeout=timeout,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            out = (p.stdout or b"").decode("utf-8", "replace")
            if p.returncode == 0 and "CLEANER_OK" in out:
                return {"ok": True, "cancelled": False, "error": None,
                        "message": _describe(name, action)}
            err = (p.stderr or b"").decode("utf-8", "replace").strip()
            return {"ok": False, "cancelled": False,
                    "error": _friendly_error(err),
                    "message": None}
        except subprocess.TimeoutExpired:
            return {"ok": False, "cancelled": False,
                    "error": "Windows did not respond in time."}
        except Exception as exc:
            return {"ok": False, "cancelled": False, "error": str(exc)}

    ok, text = run_elevated(script, timeout)
    if not ok:
        return {"ok": False, "cancelled": True, "error": None,
                "message": ("Administrator access was declined, so nothing "
                            "was changed.")}
    if "CLEANER_OK" in (text or ""):
        return {"ok": True, "cancelled": False, "error": None,
                "message": _describe(name, action)}
    return {"ok": False, "cancelled": False,
            "error": _friendly_error(text or ""), "message": None}


def _describe(name: str, action: str) -> str:
    verb = {"Start": "started", "Stop": "stopped", "Restart": "restarted",
            "Pause": "paused", "Continue": "resumed",
            "Automatic": "set to start with Windows",
            "Manual": "set to start only when needed",
            "Disabled": "switched off"}.get(action, "updated")
    return "%s: %s." % (name, verb)


_FRIENDLY = (
    ("cannot be stopped", "Windows will not stop that service; something "
                          "else is using it."),
    ("cannot accept", "That service does not support being paused."),
    ("not started", "That service is not running, so there is nothing to "
                    "stop or pause."),
    ("access is denied", "Windows refused - another program may hold it. Try "
                         "again as administrator."),
    ("dependent services", "Other services depend on this one, so Windows "
                           "refused to stop it."),
    ("timeout", "The service did not respond in time."),
)


def _friendly_error(blob: str) -> str:
    low = (blob or "").lower()
    for needle, text in _FRIENDLY:
        if needle in low:
            return text
    for line in reversed((blob or "").strip().splitlines()):
        if line.strip():
            return line.strip()[:160]
    return "Windows could not complete that change."


# R-010: temp_script_path() was removed — it returned a fixed
# %TEMP%\_cleaner_svcs.ps1 path that nothing writes to any more (the elevated
# path goes through app.infra.elevated_run, which uses mkstemp). Keeping a
# helper that hands out a predictable temp path invited a future caller to
# reintroduce a symlink/pre-plant write.


#: Column order for as_clipboard_text. Fixed, and the header row is included,
#: because the overwhelmingly common use for this list is pasting it into a
#: forum thread - and "which of these did I install" is unanswerable from a
#: screenshot of a dark dialog.
CLIPBOARD_COLUMNS = ("Name", "Description", "Status", "Starts", "Account",
                     "Publisher", "Path")


def as_clipboard_text(rows) -> str:
    """The services as tab-separated text, header first.

    Tab-separated rather than CSV because a service DESCRIPTION routinely
    contains commas, and a quoted CSV blob pasted into a forum is mangled by
    every forum editor there is. Windows' own services.msc exports tab-
    delimited for the same reason.

    Lives here, not in the dialog, so the formatting is testable without a
    display and so there is exactly one definition of "what a row is".
    """
    out = ["\t".join(CLIPBOARD_COLUMNS)]
    for r in rows or []:
        out.append("\t".join(str(x or "").replace("\t", " ").replace(
            "\n", " ") for x in (
                r.get("display"), r.get("description"),
                STATUS_PLAIN.get(r.get("status"), r.get("status")),
                r.get("start_plain") or r.get("start_mode"),
                r.get("account_plain"), r.get("publisher"), r.get("path"))))
    return "\n".join(out)


def open_services_msc() -> bool:
    """Open Windows' own Services window.

    The dialog can start and stop a service, but it cannot show you a
    service's event log, its recovery actions, its service account, or its
    security descriptor - all of which live in services.msc. Handing the user
    a button that opens the real thing is worth more than any summary this
    tool could render of it.

    ShellExecuteW, NOT subprocess.Popen, and that is not a style choice.
    services.msc is an MMC console file, not a PE executable: measured on
    this machine, Popen fails with WinError 193 ("%1 is not a valid Win32
    application") for BOTH the bare name and the full path. The shell knows
    the .msc association; CreateProcess does not. Needs no elevation and no
    console suppression. Never raises.
    """
    if not IS_WINDOWS:
        return False
    try:
        exe = os.path.join(os.environ.get("WINDIR", r"C:\Windows"),
                           "System32", "services.msc")
        if not os.path.isfile(exe):
            return False
        rc = ctypes.windll.shell32.ShellExecuteW(None, "open", exe, None,
                                                 None, 1)
        return rc is not None and int(rc) > 32
    except Exception:
        return False