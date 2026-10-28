"""Game Readiness: one read-only pass over the settings that decide whether a
game runs well, with a one-click route to the EXISTING tweak for each.

Why a separate module rather than a dialog that reads the registry itself:
every value here is also verified by a tweak's own ``verify`` function, and
the fix is the tweak task itself (so it gets the standard snapshot/undo). The
dialog stays presentation-only, and this module is testable without Tk.

Nothing here writes. `check()` returns plain dicts:

    {"key", "label", "state", "detail", "fix", "fix_label"}

state is "ok" | "warn" | "info". `fix` is a tweak task key from
app/tab_presets.TABS["Tweak"] (or None when the right answer is a different
screen, e.g. drivers).
"""

from __future__ import annotations

import os
import shutil
import subprocess

OK, WARN, INFO = "ok", "warn", "info"

#: HKCU\\Software\\Microsoft\\GameBar
_GAMEBAR = r"Software\Microsoft\GameBar"
#: HKCU\\System\\GameConfigStore
_CONFIGSTORE = r"System\GameConfigStore"
#: HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\GameDVR
_GAMEDVR = r"Software\Microsoft\Windows\CurrentVersion\GameDVR"

_POWER_PLANS = {
    "381b4222-f694-41f0-9685-ff5bb260df2e": "Balanced",
    "8c5e7fda-e8bf-4a96-9a85-a6e23a8c635c": "High performance",
    "a1841308-3541-4fab-bc81-f71556f20b4a": "Power saver",
    "e9a42b02-d5df-448d-aa00-03f14749eb61": "Ultimate performance",
}


def _reg(path: str, name: str):
    """Read a HKCU value; None when missing or unreadable (read-only pass —
    an unreadable value is reported as unknown, never assumed)."""
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, path) as k:
            return winreg.QueryValueEx(k, name)[0]
    except OSError:
        return None


def _game_mode() -> dict:
    allow = _reg(_GAMEBAR, "AllowAutoGameMode")
    enabled = _reg(_GAMEBAR, "AutoGameModeEnabled")
    on = bool(enabled) if enabled is not None else bool(allow)
    if enabled is None and allow is None:
        return {"key": "game_mode", "label": "Game Mode", "state": INFO,
                "detail": "Windows has not recorded a choice — the default "
                          "is on.",
                "fix": "game_mode", "fix_label": "Turn on"}
    return {"key": "game_mode", "label": "Game Mode",
            "state": OK if on else WARN,
            "detail": ("On — Windows prioritises the game." if on else
                       "Off — background apps compete with your game."),
            "fix": None if on else "game_mode", "fix_label": "Turn on"}


def _game_dvr() -> dict:
    cfg = _reg(_CONFIGSTORE, "GameDVR_Enabled")
    capture = _reg(_GAMEDVR, "AppCaptureEnabled")
    on = bool(cfg) if cfg is not None else bool(capture)
    return {"key": "game_dvr", "label": "Background recording",
            "state": WARN if on else OK,
            "detail": ("On — the Game Bar records in the background, which "
                       "costs a few percent of frame rate." if on else
                       "Off — nothing records in the background."),
            "fix": "disable_game_dvr" if on else None,
            "fix_label": "Turn off"}


def active_power_plan() -> tuple[str, str]:
    """(guid, friendly_name). ("", "") when powercfg cannot be read."""
    try:
        out = subprocess.run(
            ["powercfg", "/getactivescheme"], capture_output=True, text=True,
            timeout=15, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        ).stdout or ""
    except Exception:
        return "", ""
    import re
    m = re.search(r"([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
                  r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12})", out)
    if not m:
        return "", ""
    guid = m.group(1).lower()
    return guid, _POWER_PLANS.get(guid, "")


def _power() -> dict:
    guid, name = active_power_plan()
    if not guid:
        return {"key": "power_plan", "label": "Power plan", "state": INFO,
                "detail": "Could not read the active plan.",
                "fix": None, "fix_label": ""}
    high = guid in ("8c5e7fda-e8bf-4a96-9a85-a6e23a8c635c",
                    "e9a42b02-d5df-448d-aa00-03f14749eb61")
    # An unrecognised GUID is a CUSTOM plan (OEM tools and users create them
    # all the time). Reporting it as a warning would nag about a plan whose
    # settings we have not read, so it is INFO with the same one-click offer.
    known_low = guid in ("381b4222-f694-41f0-9685-ff5bb260df2e",
                         "a1841308-3541-4fab-bc81-f71556f20b4a")
    if high:
        state, note = OK, "full speed while gaming."
    elif known_low:
        state, note = WARN, "Windows may down-clock the CPU and GPU mid-game."
    else:
        state, note = INFO, ("a custom plan — switch to Max Performance if "
                             "you want guaranteed clocks.")
    return {"key": "power_plan", "label": "Power plan",
            "state": state,
            "detail": "%s — %s" % (name or guid, note),
            "fix": None if high else "ultimate_performance",
            "fix_label": "Max performance"}


def _gpu_driver() -> dict:
    """Driver date via WMI, formatted invariantly (see repair_gpu_driver_age,
    which learned the culture lesson the hard way)."""
    try:
        from app.tasks.ps import ps_argv
        out = subprocess.run(
            ps_argv("Get-CimInstance Win32_VideoController | Where-Object DriverDate | "
                    "Sort-Object DriverDate -Descending | Select-Object -First 1 | "
                    "ForEach-Object { $_.DriverDate.ToString('yyyy-MM-dd', "
                    "[System.Globalization.CultureInfo]::InvariantCulture) }"),
            capture_output=True, text=True, timeout=45,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        ).stdout or ""
    except Exception:
        out = ""
    raw = next((ln.strip() for ln in out.splitlines() if ln.strip()), "")
    if not raw:
        return {"key": "gpu_driver", "label": "Graphics driver", "state": INFO,
                "detail": "Could not read the driver date.",
                "fix": None, "fix_label": ""}
    try:
        from datetime import date, datetime
        d = datetime.strptime(raw, "%Y-%m-%d").date()
        age = (date.today() - d).days
    except ValueError:
        return {"key": "gpu_driver", "label": "Graphics driver", "state": INFO,
                "detail": "Driver dated %s." % raw,
                "fix": None, "fix_label": ""}
    if age > 365:
        state, note = WARN, ("%d months old — a newer driver often fixes "
                             "crashes and stutter." % (age // 30))
    elif age > 180:
        state, note = INFO, "%d months old." % (age // 30)
    else:
        state, note = OK, "Up to date (%d days old)." % age
    return {"key": "gpu_driver", "label": "Graphics driver", "state": state,
            "detail": "%s — %s" % (d.isoformat(), note),
            # Not a tweak: drivers are installed by the Drivers tab, which
            # already verifies vendor tooling. The dialog routes this one.
            "fix": "drivers_tab" if state == WARN else None,
            "fix_label": "Open Drivers"}


def _refresh_rate() -> dict:
    """Highest refresh rate any attached display offers, via EnumDisplaySettings."""
    hz = 0
    try:
        import ctypes
        from ctypes import wintypes

        class DEVMODE(ctypes.Structure):
            _fields_ = [
                ("dmDeviceName", ctypes.c_wchar * 32),
                ("dmSpecVersion", wintypes.WORD),
                ("dmDriverVersion", wintypes.WORD),
                ("dmSize", wintypes.WORD),
                ("dmDriverExtra", wintypes.WORD),
                ("dmFields", wintypes.DWORD),
                ("dmOrientation", ctypes.c_short),
                ("dmPaperSize", ctypes.c_short),
                ("dmPaperLength", ctypes.c_short),
                ("dmPaperWidth", ctypes.c_short),
                ("dmScale", ctypes.c_short),
                ("dmCopies", ctypes.c_short),
                ("dmDefaultSource", ctypes.c_short),
                ("dmPrintQuality", ctypes.c_short),
                ("dmColor", ctypes.c_short),
                ("dmDuplex", ctypes.c_short),
                ("dmYResolution", ctypes.c_short),
                ("dmTTOption", ctypes.c_short),
                ("dmCollate", ctypes.c_short),
                ("dmFormName", ctypes.c_wchar * 32),
                ("dmLogPixels", wintypes.WORD),
                ("dmBitsPerPel", wintypes.DWORD),
                ("dmPelsWidth", wintypes.DWORD),
                ("dmPelsHeight", wintypes.DWORD),
                ("dmDisplayFlags", wintypes.DWORD),
                ("dmDisplayFrequency", wintypes.DWORD),
            ]

        ENUM_CURRENT = -1
        user32 = ctypes.windll.user32
        dm = DEVMODE()
        dm.dmSize = ctypes.sizeof(DEVMODE)
        if user32.EnumDisplaySettingsW(None, ENUM_CURRENT, ctypes.byref(dm)):
            hz = int(dm.dmDisplayFrequency)
    except Exception:
        hz = 0
    if not hz:
        return {"key": "refresh", "label": "Refresh rate", "state": INFO,
                "detail": "Could not read the display mode.", "fix": None,
                "fix_label": ""}
    if hz <= 60:
        return {"key": "refresh", "label": "Refresh rate", "state": WARN,
                "detail": "%d Hz — a 120/144 Hz monitor left at 60 Hz is the "
                          "single most visible setting to fix (Windows "
                          "Settings > Display > Advanced)." % hz,
                "fix": None, "fix_label": ""}
    return {"key": "refresh", "label": "Refresh rate", "state": OK,
            "detail": "%d Hz." % hz, "fix": None, "fix_label": ""}


def _game_drive() -> str:
    """The drive holding the largest Steam library, else the system drive."""
    try:
        import glob
        import re
        roots = []
        for cfg in glob.glob(os.path.join(
                os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)"),
                "Steam", "steamapps", "libraryfolders.vdf")):
            try:
                text = open(cfg, encoding="utf-8", errors="replace").read()
            except OSError:
                continue
            roots += [m.replace("\\\\", "\\") for m in
                      re.findall(r'"path"\s+"([^"]+)"', text)]
        roots.append(os.environ.get("SystemDrive", "C:") + "\\")
        best, best_free = None, -1
        for r in roots:
            drive = os.path.splitdrive(r)[0] + "\\"
            try:
                free = shutil.disk_usage(drive).free
            except OSError:
                continue
            if free > best_free:
                best, best_free = drive, free
        return best or (os.environ.get("SystemDrive", "C:") + "\\")
    except Exception:
        return (os.environ.get("SystemDrive", "C:") + "\\")


def _free_space() -> dict:
    drive = _game_drive()
    try:
        total, _used, free = shutil.disk_usage(drive)
    except OSError:
        return {"key": "free_space", "label": "Space for games", "state": INFO,
                "detail": "Could not read %s." % drive, "fix": None,
                "fix_label": ""}
    gb = free / (1024 ** 3)
    # A modern title is 50-150 GB; below 20 GB a patch cannot install.
    state = WARN if gb < 20 else (INFO if gb < 50 else OK)
    return {"key": "free_space", "label": "Space for games", "state": state,
            "detail": "%.0f GB free on %s%s" % (
                gb, drive,
                " — a large patch may not fit." if state == WARN else
                (" — enough, but a big update could be tight."
                 if state == INFO else ".")),
            "fix": "storage_insight" if state == WARN else None,
            "fix_label": "Free space"}


def _reboot() -> dict:
    pending = False
    try:
        import winreg
        keys = (
            (winreg.HKEY_LOCAL_MACHINE,
             r"SOFTWARE\Microsoft\Windows\CurrentVersion\WindowsUpdate\Auto Update\RebootRequired"),
            (winreg.HKEY_LOCAL_MACHINE,
             r"SOFTWARE\Microsoft\Windows\CurrentVersion\Component Based Servicing\RebootPending"),
        )
        for hive, path in keys:
            try:
                winreg.OpenKey(hive, path).Close()
                pending = True
            except OSError:
                continue
    except Exception:
        pending = False
    return {"key": "reboot", "label": "Pending restart",
            "state": WARN if pending else OK,
            "detail": ("Windows is waiting for a restart — drivers and some "
                       "tweaks only take effect after it." if pending else
                       "Nothing waiting."),
            "fix": None, "fix_label": ""}


def check() -> list[dict]:
    """Every readiness row, in display order. Never raises."""
    rows = []
    for fn in (_game_mode, _game_dvr, _power, _gpu_driver, _refresh_rate,
               _free_space, _reboot):
        try:
            rows.append(fn())
        except Exception as exc:                       # noqa: BLE001
            rows.append({"key": fn.__name__, "label": fn.__name__,
                         "state": INFO, "detail": "Check failed: %s" % exc,
                         "fix": None, "fix_label": ""})
    return rows


def summary(rows: list[dict]) -> tuple[int, int]:
    """(warnings, checked) — what the header says."""
    return (sum(1 for r in rows if r.get("state") == WARN), len(rows))


#: Warnings this tool owns that Health does not already grade. The graphics
#: driver and free-space checks are deliberately absent: Health has a card and
#: an action for both, and publishing the same fact twice would make the
#: Health tab's "found by your tools" band repeat its own findings.
_BUS_KEYS = ("game_mode", "game_dvr", "power_plan", "refresh", "reboot")


def publish_findings(rows) -> list:
    """Publish this read's gaming-specific warnings (AUDIT TX-002).

    Only WARN: INFO is "could not read" or "this is a custom plan", which is
    not a problem to act on. The read is authoritative for the ids it owns, so
    fixing Game Mode or switching power plans clears the old row on the next
    check.
    """
    try:
        from app import insights
        from app.insights import Finding, slug
        import time as _time
    except Exception:
        return []

    now = _time.time()
    findings = []
    for row in rows or []:
        key = str(row.get("key") or "")
        if key not in _BUS_KEYS or row.get("state") != WARN:
            continue
        findings.append(Finding(
            id="readiness." + slug(key),
            domain="gaming",
            severity="warn",
            title=str(row.get("label") or "A gaming setting"),
            detail=str(row.get("detail") or ""),
            evidence={"check": key},
            action={"kind": "route", "target": "readiness",
                    "label": "Open Game Readiness"},
            observed_at=now,
        ))
    try:
        return insights.replace("readiness.", *findings)
    except Exception:
        return []
