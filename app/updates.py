"""Windows and driver updates, through Microsoft's own update API.

Why this exists
---------------
PC Health Report used to say "Driver update suggested" and then open the
Install tab. That is not an update; it is a signpost. The honest, official way
to find and install driver updates on Windows - and the only one that does not
mean trusting a third party's driver pack - is the Windows Update Agent (WUA)
COM API that Windows itself uses. It is documented, present on every Windows
10/11 PC, needs no download, and returns only drivers Microsoft has signed and
published for THIS machine's hardware ids ("at most one assigned driver update
for each local device" - Microsoft's own wording for the Driver search type).

What this does
--------------
* `scan()`     - ask Windows Update what is pending: drivers, security and
                 definition updates, other software updates. Read-only; works
                 without administrator rights.
* `run_windows_updates(ctx)` - the Task: download and install the safe set.
                 Needs administrator rights; skipped (not failed) without.

What it deliberately does NOT do
--------------------------------
* No Windows FEATURE upgrades (a new Windows version is not a "fix").
* No update that needs an EULA the user has not seen - those are listed and
  left for the user, not accepted on their behalf.
* No third-party driver packs. Snappy-Driver-Installer-style packs are tens of
  gigabytes, are not signed for this machine, and a wrong driver is a blue
  screen, not a slow PC. Windows Update does not find every driver; for the
  rest the right source is the hardware maker, and the report says so.
* No change to the Windows Update service registration or policy.

The WUA `Search` call is synchronous and can block for minutes on a broken
update datastore, so every call runs in a CHILD PowerShell process with a hard
timeout. The script is passed as -EncodedCommand (base64 UTF-16LE), which
removes every quoting and escaping question between Python, CreateProcess and
PowerShell. Results come back as one line of JSON after a marker, because a
COM method can print arbitrary text first.

Threading: nothing here touches Tk; every function is safe on a worker.
"""
from __future__ import annotations

import base64
import json
from typing import Iterable, List, Optional

from app.utils import (IS_WINDOWS, TaskContext, TaskSkipped, resolve_exe,
                       run_cmd)

MARK = "##JSON##"

#: Update kinds this module will INSTALL. "upgrade" (a new Windows version) is
#: classified so it can be reported, but is never in this set.
INSTALL_KINDS = ("driver", "security", "definition", "software")

SCAN_TIMEOUT = 300
INSTALL_TIMEOUT = 3600


# --------------------------------------------------------------------------
# Pure helpers (unit-tested; no I/O)
# --------------------------------------------------------------------------

def classify(is_driver: bool, categories: Iterable[str]) -> str:
    """One of: driver, upgrade, definition, security, software."""
    cats = " ".join(str(c) for c in (categories or ())).lower()
    if is_driver or "driver" in cats:
        return "driver"
    if "upgrade" in cats:
        return "upgrade"
    if "definition" in cats:
        return "definition"
    if "security" in cats or "critical" in cats:
        return "security"
    return "software"


def parse_marker(lines: Iterable[str]) -> Optional[dict]:
    """The JSON object after the LAST marker line, or None.

    Last, not first: a retry inside the script may print more than once, and
    the final line is the one that reflects the end state.
    """
    found = None
    for line in lines or ():
        text = str(line)
        at = text.find(MARK)
        if at >= 0:
            try:
                found = json.loads(text[at + len(MARK):].strip())
            except Exception:
                continue
    return found if isinstance(found, dict) else None


def encode_command(script: str) -> str:
    """base64(UTF-16LE) for powershell -EncodedCommand."""
    return base64.b64encode(script.encode("utf-16-le")).decode("ascii")


def powershell_argv(script: str) -> List[str]:
    return [resolve_exe("powershell"), "-NoProfile", "-NonInteractive",
            "-EncodedCommand", encode_command(script)]


def normalise(raw: dict) -> dict:
    """Shape the script's JSON into the dict the rest of the app uses."""
    out = {"ok": bool(raw.get("ok")), "error": raw.get("error") or "",
           "updates": []}
    for u in (raw.get("updates") or []):
        if not isinstance(u, dict):
            continue
        kind = classify(bool(u.get("driver")), u.get("categories") or ())
        out["updates"].append({
            "id": str(u.get("id") or ""),
            "title": str(u.get("title") or "Update"),
            "kind": kind,
            "size": int(u.get("size") or 0),
            "eula": bool(u.get("eula", True)),
            "reboot": int(u.get("reboot") or 0) > 0,
            "maker": str(u.get("maker") or ""),
            "model": str(u.get("model") or ""),
        })
    return out


def installable(updates: Iterable[dict]) -> List[dict]:
    """What the Task would install: safe kinds, EULA already accepted."""
    return [u for u in (updates or ())
            if u.get("kind") in INSTALL_KINDS and u.get("eula", True)]


def summarize(updates: Iterable[dict]) -> dict:
    """Counts for the UI: how many of each kind, and what needs the user."""
    ups = list(updates or ())
    kinds = {}
    for u in ups:
        kinds[u["kind"]] = kinds.get(u["kind"], 0) + 1
    return {
        "total": len(ups),
        "drivers": kinds.get("driver", 0),
        "security": kinds.get("security", 0) + kinds.get("definition", 0),
        "other": kinds.get("software", 0),
        "upgrades": kinds.get("upgrade", 0),
        "needs_eula": sum(1 for u in ups if not u.get("eula", True)),
        "installable": len(installable(ups)),
        "bytes": sum(int(u.get("size") or 0) for u in installable(ups)),
    }


# --------------------------------------------------------------------------
# PowerShell scripts
# --------------------------------------------------------------------------

_SCAN_PS = r"""
$ErrorActionPreference = 'Stop'
function Emit($o) { Write-Output ('##JSON##' + (ConvertTo-Json -InputObject $o -Depth 6 -Compress)) }
try {
  $session  = New-Object -ComObject Microsoft.Update.Session
  $searcher = $session.CreateUpdateSearcher()
  $result   = $searcher.Search("IsInstalled=0 and IsHidden=0")
  $list = @()
  foreach ($u in $result.Updates) {
    $cats = @(); foreach ($c in $u.Categories) { $cats += [string]$c.Name }
    $isDrv = ([int]$u.Type -eq 2)
    $o = [ordered]@{
      id = [string]$u.Identity.UpdateID; title = [string]$u.Title
      driver = $isDrv; categories = $cats
      size = [int64]$u.MaxDownloadSize; eula = [bool]$u.EulaAccepted
      reboot = [int]$u.InstallationBehavior.RebootBehavior
      maker = ''; model = ''
    }
    if ($isDrv) {
      try { $o.maker = [string]$u.DriverManufacturer; $o.model = [string]$u.DriverModel } catch {}
    }
    $list += $o
  }
  Emit @{ ok = $true; updates = @($list) }
} catch {
  $m = $_.Exception.Message
  if ($_.Exception.InnerException) { $m = $_.Exception.InnerException.Message }
  Emit @{ ok = $false; error = $m; updates = @() }
}
"""

_INSTALL_PS = r"""
$ErrorActionPreference = 'Stop'
function Emit($o) { Write-Output ('##JSON##' + (ConvertTo-Json -InputObject $o -Depth 6 -Compress)) }
$wantDrivers  = __DRIVERS__
$wantSoftware = __SOFTWARE__
$wantIds      = @(__IDS__)
try {
  $session  = New-Object -ComObject Microsoft.Update.Session
  $searcher = $session.CreateUpdateSearcher()
  $result   = $searcher.Search("IsInstalled=0 and IsHidden=0")
  $coll = New-Object -ComObject Microsoft.Update.UpdateColl
  $titles = @()
  foreach ($u in $result.Updates) {
    $cats = @(); foreach ($c in $u.Categories) { $cats += [string]$c.Name }
    $isDrv = ([int]$u.Type -eq 2)
    if (($cats -join ' ') -match 'Upgrade') { continue }
    if (-not $u.EulaAccepted) { continue }
    if ($wantIds.Count -gt 0) {
      if ($wantIds -notcontains ([string]$u.Identity.UpdateID)) { continue }
    } else {
      if ($isDrv -and -not $wantDrivers) { continue }
      if ((-not $isDrv) -and -not $wantSoftware) { continue }
    }
    [void]$coll.Add($u); $titles += [string]$u.Title
  }
  if ($coll.Count -eq 0) { Emit @{ ok = $true; installed = 0; failed = 0; reboot = $false; items = @() }; return }
  Write-Output ("Downloading " + $coll.Count + " update(s)...")
  $dl = $session.CreateUpdateDownloader(); $dl.Updates = $coll
  [void]$dl.Download()
  $ready = New-Object -ComObject Microsoft.Update.UpdateColl
  foreach ($u in $coll) { if ($u.IsDownloaded) { [void]$ready.Add($u) } }
  if ($ready.Count -eq 0) { Emit @{ ok = $false; error = 'Windows could not download the updates.'; installed = 0; failed = $coll.Count; reboot = $false; items = @() }; return }
  Write-Output ("Installing " + $ready.Count + " update(s)...")
  $inst = $session.CreateUpdateInstaller(); $inst.Updates = $ready
  $res = $inst.Install()
  $items = @(); $ok = 0; $bad = 0
  for ($i = 0; $i -lt $ready.Count; $i++) {
    $rc = [int]$res.GetUpdateResult($i).ResultCode
    $good = ($rc -eq 2 -or $rc -eq 3)
    if ($good) { $ok++ } else { $bad++ }
    $items += [ordered]@{ title = [string]$ready.Item($i).Title; ok = $good; code = $rc }
  }
  Emit @{ ok = $true; installed = $ok; failed = $bad; reboot = [bool]$res.RebootRequired; items = @($items) }
} catch {
  $m = $_.Exception.Message
  if ($_.Exception.InnerException) { $m = $_.Exception.InnerException.Message }
  Emit @{ ok = $false; error = $m; installed = 0; failed = 0; reboot = $false; items = @() }
}
"""


def scan_script() -> str:
    return _SCAN_PS


_GUID_RE = __import__("re").compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{12}$")


def install_script(drivers: bool = True, software: bool = True,
                   ids: Iterable[str] = ()) -> str:
    """The install script. With `ids`, ONLY those UpdateIDs (the user's own
    picks) are installed, whatever their kind; without, the drivers/software
    switches apply. Every id is checked to be a GUID before it is placed in
    the script - the id comes from a scan, but it still never gets to be
    anything but 36 hex-and-dash characters."""
    clean = []
    for i in ids or ():
        if not _GUID_RE.match(str(i)):
            raise ValueError("not an update id: %r" % (i,))
        clean.append("'%s'" % str(i))
    return (_INSTALL_PS
            .replace("__DRIVERS__", "$true" if drivers else "$false")
            .replace("__SOFTWARE__", "$true" if software else "$false")
            .replace("__IDS__", ",".join(clean)))


# --------------------------------------------------------------------------
# Running them
# --------------------------------------------------------------------------

def scan(ctx: Optional[TaskContext] = None,
         timeout: int = SCAN_TIMEOUT) -> dict:
    """Ask Windows Update what is pending. Never raises; read-only.

    Returns {"ok", "error", "updates": [...], "summary": {...}}.
    """
    if not IS_WINDOWS:
        return {"ok": False, "error": "Windows Update is Windows-only.",
                "updates": [], "summary": summarize([])}
    quiet = ctx or TaskContext(log=lambda _m: None,
                               set_status=lambda _m: None)
    lines: List[str] = []
    try:
        run_cmd(quiet, powershell_argv(scan_script()), shell=False,
                timeout=timeout, collect=lines)
    except Exception as exc:                        # noqa: BLE001
        return {"ok": False, "error": "Could not ask Windows Update: %s" % exc,
                "updates": [], "summary": summarize([])}
    raw = parse_marker(lines)
    if raw is None:
        return {"ok": False,
                "error": "Windows Update did not answer (it may be busy or "
                         "its service stopped).",
                "updates": [], "summary": summarize([])}
    out = normalise(raw)
    out["summary"] = summarize(out["updates"])
    return out


def run_windows_updates(ctx: TaskContext):
    """Task body: install the safe set of Windows and driver updates.

    Skips (does not fail) when there is no internet, no administrator rights,
    or nothing pending. Logs a "[REBOOT]" line when Windows says a restart is
    needed, which the care run reads to tell the user.
    """
    from app.elevation import is_admin
    from app.downloader import has_network
    if not IS_WINDOWS:
        raise TaskSkipped("Windows Update is Windows-only.")
    if not has_network():
        raise TaskSkipped("No internet connection - updates need to download.")
    if not is_admin():
        raise TaskSkipped("Installing updates needs administrator rights.")

    ctx.set_status("Asking Windows Update what is available...")
    found = scan(ctx)
    if not found["ok"]:
        raise RuntimeError(found["error"] or "Windows Update would not answer.")
    s = found["summary"]
    # Unattended runs install Windows and security updates only. Driver
    # updates are LISTED, not installed: a driver is a choice (Windows Update
    # sometimes offers an older or buggy graphics driver), so installing one
    # is the Drivers tab's job, with the user picking, a restore point and a
    # backup - or the update picker in PC Health.
    todo = [u for u in installable(found["updates"]) if u["kind"] != "driver"]
    n_drv = sum(1 for u in found["updates"] if u["kind"] == "driver")
    if n_drv:
        ctx.log("  %d driver update(s) are available - review them in the "
                "Drivers tab (they are not installed automatically)." % n_drv)
    if s["needs_eula"]:
        ctx.log("  %d update(s) need you to accept a licence first - left "
                "for you in Settings > Windows Update." % s["needs_eula"])
    if s["upgrades"]:
        ctx.log("  A new Windows version is offered; it is never installed "
                "automatically.")
    if not todo:
        ctx.log("  [OK] Windows is up to date.")
        return
    for u in todo[:12]:
        label = "driver" if u["kind"] == "driver" else u["kind"]
        ctx.log("  - %s (%s)" % (u["title"], label))
    if len(todo) > 12:
        ctx.log("  ... and %d more" % (len(todo) - 12))

    ctx.set_status("Installing %d update(s) (this can take a while)..."
                   % len(todo))
    lines: List[str] = []
    run_cmd(ctx, powershell_argv(install_script(drivers=False,
                                                 ids=[u["id"] for u in todo])),
            shell=False, timeout=INSTALL_TIMEOUT, collect=lines)
    res = parse_marker(lines)
    if res is None:
        raise RuntimeError("Windows Update did not report a result. Check "
                           "Settings > Windows Update.")
    if not res.get("ok") and not res.get("installed"):
        raise RuntimeError(res.get("error") or "Windows Update failed.")
    done, bad = int(res.get("installed") or 0), int(res.get("failed") or 0)
    for item in (res.get("items") or []):
        if isinstance(item, dict) and not item.get("ok"):
            ctx.log("  ! did not install: %s" % item.get("title"))
    ctx.log("  [OK] Installed %d update(s)%s." % (
        done, (", %d failed" % bad) if bad else ""))
    if res.get("reboot"):
        ctx.log("  [REBOOT] Windows needs a restart to finish these updates.")
    if done == 0 and bad:
        raise RuntimeError("None of the %d updates installed." % bad)


def install_selected(ctx: TaskContext, ids: Iterable[str]) -> dict:
    """Install exactly the updates the user ticked (any kind, drivers too).

    Returns the install result dict ({"installed", "failed", "reboot",
    "items", ...}). Skips with a reason, like the Task, when offline or not
    elevated; raises RuntimeError when Windows Update gives no answer.
    """
    from app.elevation import is_admin
    from app.downloader import has_network
    ids = [str(i) for i in ids or ()]
    if not ids:
        return {"ok": True, "installed": 0, "failed": 0, "reboot": False,
                "items": []}
    if not IS_WINDOWS:
        raise TaskSkipped("Windows Update is Windows-only.")
    if not has_network():
        raise TaskSkipped("No internet connection.")
    if not is_admin():
        raise TaskSkipped("Installing updates needs administrator rights.")
    lines: List[str] = []
    run_cmd(ctx, powershell_argv(install_script(ids=ids)), shell=False,
            timeout=INSTALL_TIMEOUT, collect=lines)
    res = parse_marker(lines)
    if res is None:
        raise RuntimeError("Windows Update did not report a result.")
    if res.get("reboot"):
        ctx.log("  [REBOOT] Windows needs a restart to finish these updates.")
    return res
