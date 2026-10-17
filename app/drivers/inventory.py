"""What drivers this PC has, and which devices have none.

One PowerShell call joins Win32_PnPEntity (every device, with its hardware
ids and a problem code) to Win32_PnPSignedDriver (the driver actually bound to
it: version, date, provider, INF name). Pure standard library, ~2-4 seconds.

A device is:
  missing  - problem code 28 ("drivers for this device are not installed") or
             no driver bound at all,
  problem  - any other non-zero problem code (disabled, failed to start...).
             Informational: a new driver does not fix a disabled device, so
             these are listed but not offered as downloads unless missing,
  ok       - working.

Which devices are WORTH looking up is a judgement, kept in `relevant()`: the
classes people actually need drivers for, from a vendor other than Microsoft
(Windows' own class drivers are dated 2006 and are not "outdated"; looking
them up would only produce noise and a thousand catalog requests).
"""
from __future__ import annotations

import json
from typing import Iterable, List, Optional

from app.drivers.versions import parse_date

MARK = "##JSON##"

#: PnP classes where a vendor driver matters. Everything else (HID, USB hubs,
#: volumes, processors, monitors, printers...) is Windows' own business.
RELEVANT_CLASSES = frozenset({
    "display", "net", "media", "system", "bluetooth", "hdc", "scsiadapter",
    "image", "camera", "biometric", "firmware", "sensor", "smartcardreader",
    "diskdrive", "wpd", "extension", "softwarecomponent", "audioendpoint",
    "usb", "mtd", "holographic", "ports",
})

#: Device-id prefixes that are not real hardware (virtual / software).
VIRTUAL_PREFIXES = ("ROOT\\", "SWD\\", "HTREE\\", "STORAGE\\", "UMB\\",
                    "TERMINPUT", "{")

_SCRIPT = r"""
$ErrorActionPreference = 'SilentlyContinue'
$drv = @{}
Get-CimInstance Win32_PnPSignedDriver | ForEach-Object {
  if ($_.DeviceID) { $drv[$_.DeviceID] = $_ }
}
$out = @()
Get-CimInstance Win32_PnPEntity | ForEach-Object {
  $d = $drv[$_.DeviceID]
  $out += [ordered]@{
    id = [string]$_.DeviceID; name = [string]$_.Name
    cls = [string]$_.PNPClass; maker = [string]$_.Manufacturer
    code = [int]$_.ConfigManagerErrorCode
    hwids = @($_.HardwareID | ForEach-Object { [string]$_ })
    compat = @($_.CompatibleID | ForEach-Object { [string]$_ })
    version = if ($d) { [string]$d.DriverVersion } else { '' }
    date = if ($d -and $d.DriverDate) { $d.DriverDate.ToString('yyyy-MM-dd') } else { '' }
    provider = if ($d) { [string]$d.DriverProviderName } else { '' }
    inf = if ($d) { [string]$d.InfName } else { '' }
    signed = if ($d) { [bool]$d.IsSigned } else { $false }
  }
}
Write-Output ('##JSON##' + (ConvertTo-Json -InputObject @($out) -Depth 5 -Compress))
"""


def script() -> str:
    return _SCRIPT


def parse_marker(lines: Iterable[str]) -> Optional[list]:
    """The JSON array after the LAST marker line, or None."""
    found = None
    for line in lines or ():
        t = str(line)
        at = t.find(MARK)
        if at >= 0:
            try:
                found = json.loads(t[at + len(MARK):].strip())
            except Exception:
                continue
    return found if isinstance(found, list) else None


def normalise(raw: Iterable[dict]) -> List[dict]:
    """Shape the script's rows into device dicts with a computed `state`."""
    devices = []
    for r in raw or ():
        if not isinstance(r, dict):
            continue
        hw = [str(h) for h in (r.get("hwids") or []) if h]
        code = int(r.get("code") or 0)
        have = bool(r.get("version"))
        if code == 28 or (not have and code != 22):
            state = "missing"
        elif code != 0 and code != 22:
            state = "problem"
        else:
            state = "ok"
        devices.append({
            "id": str(r.get("id") or ""),
            "name": str(r.get("name") or "Unknown device"),
            "cls": str(r.get("cls") or ""),
            "maker": str(r.get("maker") or ""),
            "code": code, "state": state,
            "hwids": hw,
            "compat": [str(h) for h in (r.get("compat") or []) if h],
            "version": str(r.get("version") or ""),
            "date": str(r.get("date") or ""),
            "provider": str(r.get("provider") or ""),
            "inf": str(r.get("inf") or ""),
            "signed": bool(r.get("signed")),
        })
    return devices


def is_virtual(dev: dict) -> bool:
    did = dev.get("id", "").upper()
    return any(did.startswith(p) for p in VIRTUAL_PREFIXES) or not dev.get(
        "hwids")


def is_microsoft_inbox(dev: dict) -> bool:
    """Windows' own class driver (provider Microsoft). Never 'outdated'."""
    prov = (dev.get("provider") or "").lower()
    return "microsoft" in prov and "windows" in prov or prov == "microsoft"


def relevant(dev: dict, full: bool = False) -> bool:
    """Is this device worth a catalog lookup?

    Missing devices always are (that is the whole point). Otherwise: real
    hardware, a class people need drivers for, and a non-Microsoft driver
    that is at least a year old. `full=True` drops the class and age filters
    (slow: one request per device).
    """
    if is_virtual(dev):
        return False
    if dev["state"] == "missing":
        return True
    if dev["state"] != "ok":
        return False
    if full:
        return not is_microsoft_inbox(dev) or bool(dev.get("version"))
    if is_microsoft_inbox(dev):
        return False
    if dev.get("cls", "").lower() not in RELEVANT_CLASSES:
        return False
    d = parse_date(dev.get("date"))
    if d is None:
        return True
    import datetime
    return (datetime.date.today() - d).days >= 365


def hwid_queries(dev: dict, limit: int = 3) -> List[str]:
    """Catalog search strings for a device, most specific first.

    A PCI id looks like PCI\\VEN_8086&DEV_15F3&SUBSYS_00008086&REV_03. The
    full id finds a vendor's exact package; dropping REV, then SUBSYS, widens
    the net to the chip. Only the first `limit` distinct strings are used so
    one device costs at most three requests.
    """
    seen, out = set(), []

    def add(q):
        k = q.upper()
        if q and k not in seen:
            seen.add(k)
            out.append(q)

    for h in dev.get("hwids", []) or []:
        parts = h.split("&")
        add(h)
        if len(parts) > 2 and parts[-1].upper().startswith("REV_"):
            add("&".join(parts[:-1]))
        if len(parts) > 2:
            add("&".join(p for p in parts
                         if not p.upper().startswith(("SUBSYS_", "REV_"))))
        if len(out) >= limit:
            break
    return out[:limit]


def collect(run_powershell) -> List[dict]:
    """Run the inventory through `run_powershell(script, timeout) -> lines`.
    Raises RuntimeError with a plain message if Windows would not answer."""
    lines = run_powershell(script(), 120)
    raw = parse_marker(lines)
    if raw is None:
        raise RuntimeError("Windows did not return the device list.")
    return normalise(raw)
