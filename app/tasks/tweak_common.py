"""The data and primitives every tweak task shares.

Extracted from app/tasks/tweak_tasks.py (audit 2026-10, R-002 stage 2). This
is the layer the per-domain modules import:

  * the registry/powercfg spec tables (what each tweak writes, and what its
    Undo restores) and their fallbacks;
  * the snapshot primitives (`_snap_reg_values`, `_restore_reg_values`) and the
    code-owned registry allowlist, which is what makes a task's write legal;
  * the shared helpers - Explorer restart, key pruning, monitor/GPU queries,
    the hosts-file block editor, the verify readers;
  * the module-level caches (`_DEVMODE_CLS`) and the single `_has_ssd` probe.

Nothing here is a task: no `apply_*`, `revert_*` or `verify_*` lives in this
file, and `TASKS` is assembled next door in tweak_tasks.py. The split keeps
each domain readable without changing a single command any task runs.
"""
from __future__ import annotations

import functools
import os
import subprocess

from app.config_persist import (
    clear_tweak_snapshot, get_tweak_snapshot,
)
from app.utils import (
    IS_WINDOWS, TaskContext, atomic_write_text,
    reg_delete_value, reg_set_value,
    reg_set_value_checked, restart_explorer,
)
from app.tasks.ps import ps_argv

if IS_WINDOWS:
    import winreg




BALANCED_GUID = "381b4222-f694-41f0-9685-ff5bb260df2e"


CONTEXT_MENU_CLSID = "{86ca1aa0-34aa-4e8b-a509-50c905bae2a2}"



_HOSTS_PATH = os.path.join(
    os.environ.get("WINDIR", "C:\\Windows"), "System32", "drivers", "etc", "hosts"
)


_HOSTS_BACKUP_PATH = _HOSTS_PATH + ".cleanertool_bak"


_ADBLOCK_MARKER_START = "# >>> Cleaner Tool AdBlock Start >>>"


_ADBLOCK_MARKER_END = "# <<< Cleaner Tool AdBlock End <<<"



@functools.lru_cache(maxsize=1)
def _has_ssd() -> bool:
    """Check if system has at least one SSD drive (cached — PowerShell is slow).

    C1 fix: the old command wrapped the PowerShell -eq comparand in escaped
    double quotes (\"SSD\"). Under shell=True, cmd.exe strips those before
    PowerShell ever sees them, so PowerShell received a bare `-eq SSD` with
    no value — a parse error — and this function always returned False,
    silently disabling all 4 SSD tweaks (and their reverts).
    Fix: build the argv as a list and call PowerShell with shell=False, so
    cmd.exe never gets a chance to mangle the quoting. -eq 'SSD' (single
    quotes, PowerShell's own string syntax) is used inside the script text
    since there's no outer shell to strip it now."""
    if not IS_WINDOWS:
        return False
    try:
        ps_script = "(Get-PhysicalDisk | Where-Object {$_.MediaType -eq 'SSD'} | Select-Object -First 1) -ne $null"
        result = subprocess.run(
            ps_argv(ps_script),
            shell=False, capture_output=True, text=True, timeout=10,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        return result.returncode == 0 and result.stdout.strip().lower() == "true"
    except Exception:
        return False




def _restart_explorer(ctx: TaskContext) -> bool:
    """Thin alias — the real implementation now lives in app.utils.restart_explorer
    so clean_tasks.py can reuse it too (see C3 fix in clean_tasks.py)."""
    return restart_explorer(ctx)




def _prune_key_if_empty(ctx: TaskContext, hive: str, path: str) -> bool:
    """Delete `path` only if it has no values and no subkeys left.

    Used after a value-level revert, where the tweak may have created the key
    in the first place. Refuses to touch anything non-empty, which is the whole
    point: the old reg_delete_key revert destroyed a pre-existing
    InprocServer32 and anything else living under the CLSID.
    """
    if not IS_WINDOWS:
        return False
    root = {"HKCU": winreg.HKEY_CURRENT_USER,
            "HKLM": winreg.HKEY_LOCAL_MACHINE}.get(hive)
    if root is None:
        return False
    try:
        with winreg.OpenKey(root, path, 0, winreg.KEY_READ) as key:
            try:
                winreg.QueryValueEx(key, "")           # any value left?
            except FileNotFoundError:
                pass
            else:
                ctx.log("  (left %s\\%s in place — it still holds data)"
                        % (hive, path))
                return False
            if winreg.QueryInfoKey(key)[0]:            # any subkey left?
                ctx.log("  (left %s\\%s in place — it still has subkeys)"
                        % (hive, path))
                return False
    except FileNotFoundError:
        return False                                # already gone
    except Exception as exc:
        ctx.log("  (could not inspect %s\\%s: %s)" % (hive, path, exc))
        return False
    try:
        with winreg.OpenKey(root, path, 0, winreg.KEY_WRITE | winreg.KEY_READ):
            winreg.DeleteKey(root, path)
        return True
    except Exception as exc:
        ctx.log("  (could not remove the now-empty %s\\%s: %s)" % (hive, path, exc))
        return False




_GAME_DVR_SPECS = [
    ("HKCU", "System\\GameConfigStore", "GameDVR_Enabled"),
    ("HKCU", "Software\\Microsoft\\Windows\\CurrentVersion\\GameDVR", "AppCaptureEnabled"),
    ("HKLM", "SOFTWARE\\Policies\\Microsoft\\Windows\\GameDVR", "AllowGameDVR"),
]




# --------------------------------------------------------------------------- #
# Game Bar capture (user request 2026): make Win+G screenshots and clips work.
#
# On stripped Windows (LTSC) the Game Bar's screenshot/record buttons arrive
# GREYED OUT because HKCU\System\GameConfigStore\GameDVR_Enabled and
# ...\GameDVR\AppCaptureEnabled are 0 — and are often absent entirely until
# Game Bar is installed, at which point they come up 0. Both must be 1.
#
# These are the SAME two values disable_game_dvr ("Stop Background Recording",
# on by default in the Minimal/Recommended presets) writes to 0, so this tweak
# is its deliberate opposite: a user who wants clips/screenshots turns this on,
# a user who wants the perf back runs the other. They cannot both hold.
#
# disable_game_dvr also writes HKLM AllowGameDVR=0, a machine policy that
# DISABLES Game DVR outright — it greys the capture controls even once the two
# HKCU values are 1 again. So this tweak clears that policy, but ONLY when it is
# present and 0: an absent AllowGameDVR (the normal case) or an explicit 1 is
# left untouched, so the fix never creates a machine policy that was not there.
# --------------------------------------------------------------------------- #

_GAMEBAR_CAPTURE_SPECS = [
    ("HKCU", "System\\GameConfigStore", "GameDVR_Enabled"),
    ("HKCU", "Software\\Microsoft\\Windows\\CurrentVersion\\GameDVR", "AppCaptureEnabled"),
    ("HKLM", "SOFTWARE\\Policies\\Microsoft\\Windows\\GameDVR", "AllowGameDVR"),
]




_MOUSE_SPECS = [
    ("HKCU", "Control Panel\\Mouse", "MouseSpeed", "REG_SZ"),
    ("HKCU", "Control Panel\\Mouse", "MouseThreshold1", "REG_SZ"),
    ("HKCU", "Control Panel\\Mouse", "MouseThreshold2", "REG_SZ"),
]




# Every registry value apply_visual_effects_perf writes. Kept as one list so
# the snapshot, the writes and the SEC-001 allowlist entry cannot drift apart
# again -- they had already drifted, which is how three of the four values
# ended up unrestorable.
#
# UNDO-001: the snapshot used to cover MenuShowDelay ONLY, while the apply
# wrote four values. The revert restored that one from the snapshot and
# hardcoded 1 for the other three, so undoing the tweak silently destroyed
# the user's real EnableTransparency / MinAnimate / TaskbarAnimations
# settings. The allowlist entry mirrored the incomplete snapshot, so the
# SEC-001 drift guard could not see it either.
_VISUAL_EFFECTS_SPECS = [
    ("HKCU", "Software\\Microsoft\\Windows\\CurrentVersion\\Themes\\Personalize",
     "EnableTransparency"),
    ("HKCU", "Control Panel\\Desktop\\WindowMetrics", "MinAnimate"),
    ("HKCU", "Software\\Microsoft\\Windows\\CurrentVersion\\Explorer\\Advanced",
     "TaskbarAnimations"),
    ("HKCU", "Control Panel\\Desktop", "MenuShowDelay"),
]



#: Windows' own out-of-the-box values, used only when no snapshot covers a
#: key (tweak applied by an older build, or config.json cleared). Preferring
#: absence is right where Windows ships no value; these two are the documented
#: defaults for the others.
_VISUAL_EFFECTS_DEFAULTS = {
    ("HKCU", "Software\\Microsoft\\Windows\\CurrentVersion\\Themes\\Personalize",
     "EnableTransparency"): ("1", None),
    ("HKCU", "Control Panel\\Desktop\\WindowMetrics", "MinAnimate"):
        ("1", "REG_SZ"),
    ("HKCU", "Software\\Microsoft\\Windows\\CurrentVersion\\Explorer\\Advanced",
     "TaskbarAnimations"): ("1", None),
    ("HKCU", "Control Panel\\Desktop", "MenuShowDelay"):
        (None, "REG_SZ"),          # absent is the Windows default
}




_NET_THROTTLE_SPECS = [
    ("HKLM", "SOFTWARE\\Microsoft\\Windows NT\\CurrentVersion\\Multimedia\\SystemProfile",
     "NetworkThrottlingIndex"),
    ("HKLM", "SOFTWARE\\Microsoft\\Windows NT\\CurrentVersion\\Multimedia\\SystemProfile",
     "SystemResponsiveness"),
]




_GAMES_PRIORITY_SPECS = [
    ("HKLM", "SOFTWARE\\Microsoft\\Windows NT\\CurrentVersion\\Multimedia\\SystemProfile\\Tasks\\Games",
     "Scheduling Category", "REG_SZ"),
    ("HKLM", "SOFTWARE\\Microsoft\\Windows NT\\CurrentVersion\\Multimedia\\SystemProfile\\Tasks\\Games",
     "GPU Priority"),
    ("HKLM", "SOFTWARE\\Microsoft\\Windows NT\\CurrentVersion\\Multimedia\\SystemProfile\\Tasks\\Games",
     "Priority"),
    ("HKLM", "SOFTWARE\\Microsoft\\Windows NT\\CurrentVersion\\Multimedia\\SystemProfile\\Tasks\\Games",
     "SFIO Priority"),
]




def _open_interfaces_key():
    # Use 64-bit view to match reg_set_value's WOW64 writes
    access = winreg.KEY_READ
    try:
        access |= winreg.KEY_WOW64_64KEY  # type: ignore[attr-defined]
    except AttributeError:
        pass
    return winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, "SYSTEM\\CurrentControlSet\\Services\\Tcpip\\Parameters\\Interfaces", 0, access)




def _network_interface_subs() -> list:
    """Every subkey of the Tcpip Interfaces key, i.e. every network adapter.

    Shared by apply and revert so the two always agree on WHICH adapters they
    are talking about. Returns names, not full paths.
    """
    key = _open_interfaces_key()
    try:
        names, i = [], 0
        while True:
            try:
                names.append(winreg.EnumKey(key, i))
            except OSError:
                break
            i += 1
        return names
    finally:
        winreg.CloseKey(key)




_HAGS_SPECS = [
    ("HKLM", "SYSTEM\\CurrentControlSet\\Control\\GraphicsDrivers", "HwSchMode"),
]




_GAME_MODE_SPECS = [
    ("HKCU", "Software\\Microsoft\\GameBar", "AutoGameModeEnabled"),
    ("HKCU", "Software\\Microsoft\\GameBar", "AllowAutoGameMode"),
]




_FSE_SPECS = [
    ("HKCU", "System\\GameConfigStore", "GameDVR_FSEBehaviorMode"),
    ("HKCU", "System\\GameConfigStore", "GameDVR_DXGIHonorFSEWindowsCompatible"),
    ("HKCU", "System\\GameConfigStore", "GameDVR_HonorUserFSEBehaviorMode"),
]




_HIBERBOOT_SPECS = [
    ("HKLM", "SYSTEM\\CurrentControlSet\\Control\\Session Manager\\Power", "HiberbootEnabled"),
]





_PRIORITY_SEP_SPECS = [
    ("HKLM", "SYSTEM\\CurrentControlSet\\Control\\PriorityControl", "Win32PrioritySeparation"),
]




_POWER_THROTTLE_SPECS = [
    ("HKLM", "SYSTEM\\CurrentControlSet\\Control\\Power\\PowerThrottling", "PowerThrottlingOff"),
]





#: USB selective suspend: subgroup/setting GUID pair, plus the value this app
#: writes (0 = disabled) and the Windows default (1 = enabled) used only as a
#: last-resort fallback when no snapshot exists.
_USB_SUSPEND_SUBGROUP = "2a737441-1930-4402-8d77-b2bbe5a308a3"


_USB_SUSPEND_SETTING = "48e6b7a6-50f5-4782-a5d4-53bb8fcc84df"


_USB_SUSPEND_PAIRS = [(_USB_SUSPEND_SUBGROUP, _USB_SUSPEND_SETTING)]


_USB_SUSPEND_FALLBACK = {
    "%s:%s" % (_USB_SUSPEND_SUBGROUP, _USB_SUSPEND_SETTING): 1,
}




_KEYBOARD_SPECS = [
    ("HKCU", "Control Panel\\Keyboard", "KeyboardDelay"),
    ("HKCU", "Control Panel\\Keyboard", "KeyboardSpeed"),
]




_SSD_PREFETCH_PATH = "SYSTEM\\CurrentControlSet\\Control\\Session Manager\\Memory Management\\PrefetchParameters"


_SSD_PREFETCH_SPECS = [
    ("HKLM", _SSD_PREFETCH_PATH, "EnablePrefetcher"),
    ("HKLM", _SSD_PREFETCH_PATH, "EnableSuperfetch"),
]




# Disables activity history #
_ACTIVITY_HISTORY_SPECS = [
    ("HKLM", "SOFTWARE\\Policies\\Microsoft\\Windows\\System", "EnableActivityFeed"),
    ("HKLM", "SOFTWARE\\Policies\\Microsoft\\Windows\\System", "PublishUserActivities"),
    ("HKLM", "SOFTWARE\\Policies\\Microsoft\\Windows\\System", "UploadUserActivities"),
]



# Sets GPU to prefer max performance #
_NVIDIA_MAX_PERF_PAIRS = [("sub_processor", "PROCTHROTTLEMAX")]


_NVIDIA_MAX_PERF_FALLBACK = {"sub_processor:PROCTHROTTLEMAX": 100}  # Windows' documented default (0x64)





# --------------------------------------------------------------------------- #
# New merged tweaks (researched from Sophia Script / privacy.sexy — reversible)
# --------------------------------------------------------------------------- #

_MAX_CPU_POWER_PAIRS = [
    ("sub_processor", "PERFBOOSTMODE"),
    ("sub_processor", "CPMINCORES"),
    ("sub_processor", "CPMAXCORES"),
    ("sub_processor", "PROCTHROTTLEMIN"),
]


# Windows' documented stock AC defaults — only used as a last-resort fallback
# when no snapshot exists (e.g. tweak applied by an older app version).
_MAX_CPU_POWER_FALLBACK = {
    "sub_processor:PERFBOOSTMODE": 1,     # Enabled (stock default on most editions)
    "sub_processor:CPMINCORES": 0,        # 0% min cores — Windows manages core parking
    "sub_processor:CPMAXCORES": 100,
    "sub_processor:PROCTHROTTLEMIN": 5,   # Windows' typical stock min processor state
}




def _is_win11_or_newer() -> bool:
    """True on Windows 11+ (build 22000+). Win10 (incl. IoT LTSC 2021) has
    no Widgets/Chat/search-highlights keys — attempting them there creates
    junk values at best, Access-denied noise at worst."""
    try:
        import sys as _sys
        if not _sys.platform.startswith("win"):
            return False
        import platform as _pf
        ver = _pf.version()  # e.g. '10.0.22631'
        parts = ver.split(".")
        if len(parts) >= 3 and parts[0] == "10" and parts[1] == "0":
            return int(parts[2]) >= 22000
    except Exception:
        pass
    return False




_LOCAL_SEARCH_SPECS = [
    ("HKCU", "Software\\Policies\\Microsoft\\Windows\\Explorer", "DisableSearchBoxSuggestions"),
    ("HKLM", "SOFTWARE\\Policies\\Microsoft\\Windows\\Explorer", "DisableSearchBoxSuggestions"),
    ("HKCU", "Software\\Microsoft\\Windows\\CurrentVersion\\Search", "BingSearchEnabled"),
    ("HKCU", "Software\\Microsoft\\Windows\\CurrentVersion\\Search", "CortanaConsent"),
    ("HKCU", "Software\\Microsoft\\Windows\\CurrentVersion\\SearchSettings", "IsMSACloudSearchEnabled"),
    ("HKCU", "Software\\Microsoft\\Windows\\CurrentVersion\\SearchSettings", "IsAADCloudSearchEnabled"),
]




_ADS_CDM_NAMES = (
    "SubscribedContent-338387Enabled",
    "SubscribedContent-338388Enabled",
    "SubscribedContent-338389Enabled",
    "SubscribedContent-338393Enabled",
    "SubscribedContent-353694Enabled",
    "SubscribedContent-353696Enabled",
    "SilentInstalledAppsEnabled",
    "PreInstalledAppsEnabled",
    "OemPreInstalledAppsEnabled",
    "SystemPaneSuggestionsEnabled",
    "RotatingLockScreenOverlayEnabled",
    "SoftLandingEnabled",
    # Winhance-audit additions: timeline suggestions + welcome
    # experience slots, same safety shape as the rest (HKCU CDM DWORDs).
    "SubscribedContent-353698Enabled",
    "SubscribedContent-310093Enabled",
)


_ADS_CDM = "Software\\Microsoft\\Windows\\CurrentVersion\\ContentDeliveryManager"


_STOP_ADS_SPECS = (
    [("HKCU", _ADS_CDM, n) for n in _ADS_CDM_NAMES]
    + [("HKCU", "Software\\Microsoft\\Windows\\CurrentVersion\\UserProfileEngagement",
        "ScoobeSystemSettingEnabled")]
    + [("HKLM", "SOFTWARE\\Policies\\Microsoft\\Windows\\CloudContent", "DisableSoftLanding")]
    + [("HKLM", "SOFTWARE\\Policies\\Microsoft\\Windows\\CloudContent", "DisableCloudOptimizedContent")]
)




_PRIVACY_BASELINE_SPECS = [
    ("HKCU", "Software\\Microsoft\\Windows\\CurrentVersion\\AdvertisingInfo", "Enabled"),
    ("HKCU", "Software\\Microsoft\\Windows\\CurrentVersion\\Privacy", "TailoredExperiencesWithDiagnosticDataEnabled"),
    ("HKCU", "Software\\Microsoft\\Windows\\CurrentVersion\\Explorer\\Advanced", "Start_TrackProgs"),
    ("HKLM", "SOFTWARE\\Policies\\Microsoft\\Windows\\System", "EnableActivityFeed"),
    ("HKCU", "Software\\Microsoft\\Windows\\CurrentVersion\\CapabilityAccessManager\\ConsentStore\\humaninterfaceenterprise",
     "Value", "REG_SZ"),
    ("HKCU", "Control Panel\\International\\User Profile", "HttpAcceptLanguageOptOut"),
    ("HKCU", "Software\\Microsoft\\Windows\\CurrentVersion\\OnlineSpeechPrivacy", "HasAccepted"),
    ("HKCU", "Software\\Microsoft\\Siuf\\Rules", "NumberOfSIUFInPeriod"),
    ("HKCU", "Software\\Microsoft\\Input\\TIPC", "Enabled"),
    ("HKLM", "SOFTWARE\\Policies\\Microsoft\\Input\\TIPC", "Enabled"),
    ("HKCU", "Software\\Microsoft\\Personalization\\Settings", "AcceptedPrivacyPolicy"),
    ("HKCU", "Software\\Microsoft\\InputPersonalization\\TrainedDataStore", "HarvestContacts"),
]




def _strip_adblock_block(content: str) -> str:
    """Remove our marker block (and one blank line we add before it) from
    hosts-file content, if present. Leaves everything else untouched."""
    start = content.find(_ADBLOCK_MARKER_START)
    if start == -1:
        return content
    end = content.find(_ADBLOCK_MARKER_END, start)
    if end == -1:
        # B-022: a start marker with no matching end marker is corruption.
        # Cutting to EOF here silently deleted every host entry the user had
        # appended after our block. Refuse and keep the data.
        raise RuntimeError(
            "Ad-block markers in the hosts file are corrupted (start marker "
            "without an end marker). Refusing to edit and risk deleting your "
            "own entries — restore the backup or edit the file manually.")
    end += len(_ADBLOCK_MARKER_END)
    cleaned = content[:start] + content[end:]
    # Trim the blank line we insert right before the start marker
    cleaned = cleaned.rstrip("\n") + "\n"
    return cleaned




def _write_hosts_file(content: str):
    """Write hosts file content atomically — unique temp file in the SAME
    directory, flush + fsync, then os.replace (the config_persist.py
    pattern). The old truncate-and-write (open(path, "w")) left a corrupt,
    truncated hosts file — breaking ALL DNS resolution — if the machine
    crashed, lost power, or an AV tool locked the file mid-write, with no
    auto-recovery. restore_readonly clears the read-only attribute that some
    AV/security tools flip (os.replace also needs the destination writable)
    and re-applies it to the replaced file — the exact dance this function
    used to hand-roll.
    F3-2: the shared helper's unique temp name means a concurrent repair-tab
    hosts restore can no longer clobber this write mid-flight (both used to
    blindly reuse the fixed name hosts.cleanertool.tmp)."""
    atomic_write_text(_HOSTS_PATH, content, newline="\n", restore_readonly=True,
                      errors="surrogateescape")




# --------------------------------------------------------------------------- #
# Focus / stability tweaks (tasks.txt additions)
# --------------------------------------------------------------------------- #

_ACCESSIBILITY_FLAGS = (
    ("HKCU", "Control Panel\\Accessibility\\StickyKeys", "Flags", "506"),
    ("HKCU", "Control Panel\\Accessibility\\Keyboard Response", "Flags", "122"),
    ("HKCU", "Control Panel\\Accessibility\\ToggleKeys", "Flags", "58"),
)





_HIDE_RECENT_SPECS = [
    ("HKCU", "Software\\Microsoft\\Windows\\CurrentVersion\\Explorer", "ShowRecent"),
    ("HKCU", "Software\\Microsoft\\Windows\\CurrentVersion\\Explorer", "ShowFrequent"),
]




_TDR_SPECS = [
    ("HKLM", "SYSTEM\\CurrentControlSet\\Control\\GraphicsDrivers", "TdrDelay"),
]





# --------------------------------------------------------------------------- #
# Round 3 tasks (user request): NIC Energy-Efficient Ethernet, NTFS 8.3
# names, dynamic tick. All reversible — EEE snapshots each adapter's exact
# prior display values (gaming_dns pattern), 8.3 snapshots the queried
# prior fsutil state, dynamic tick reverts via bcdedit /deletevalue.
# --------------------------------------------------------------------------- #

_EEE_DISPLAY_NAMES = ("Energy Efficient Ethernet", "Energy-Efficient Ethernet (EEE)",
                      "Green Ethernet", "Power Saving Mode", "Gigabit Lite")




def _query_eee_props() -> "list[tuple[str, str, str]]":
    """(adapter name, property display name, current display value) for every
    active adapter that exposes an EEE-family property.

    C1-fix discipline: PowerShell argv list + shell=False so cmd.exe never
    mangles the quoting; single quotes inside the script text (PS's own
    string syntax) since there's no outer shell to strip them."""
    import subprocess as _sp
    names = "@('" + "','".join(_EEE_DISPLAY_NAMES) + "')"
    ps = ("Get-NetAdapter | Where-Object Status -eq 'Up' | "
          "Get-NetAdapterAdvancedProperty -ErrorAction SilentlyContinue | "
          f"Where-Object DisplayName -in {names} | "
          "ForEach-Object { 'EEE|' + $_.Name + '|' + $_.DisplayName + '|' + $_.DisplayValue }")
    try:
        out = _sp.run(ps_argv(ps), shell=False,
                      capture_output=True, text=True, timeout=30,
                      creationflags=getattr(_sp, "CREATE_NO_WINDOW", 0))
    except Exception:
        return []
    rows = []
    for line in (out.stdout or "").splitlines():
        parts = line.split("|", 3)
        if len(parts) == 4 and parts[0] == "EEE":
            rows.append((parts[1], parts[2], parts[3]))
    return rows




def _ps_sq(s: str) -> str:
    """Escape a string for embedding in a PowerShell single-quoted literal
    (L3: adapter/property names are system-controlled but can contain `'`,
    e.g. "John's Ethernet" — an unescaped quote breaks out of the PS
    string; doubling is PS's escape)."""
    return str(s).replace("'", "''")




def _bcd_value(name):
    """Return the current BCD element value string for `name`, or None.

    bcdedit output is `key<whitespace>value` per line (e.g.
    "disabledynamictick         Yes"). The key names are not localized, so
    matching on the first token is safe; only the value keywords are.
    """
    try:
        import subprocess as _sp
        out = _sp.run(["bcdedit", "/enum", "{current}"], capture_output=True,
                      text=True, timeout=30,
                      creationflags=getattr(_sp, "CREATE_NO_WINDOW", 0))
        if out.returncode != 0:
            return None
        for line in (out.stdout or "").splitlines():
            parts = line.split()
            if len(parts) >= 2 and parts[0].lower() == name.lower():
                return " ".join(parts[1:]).strip() or None
    except Exception:
        return None
    return None



# Each active network adapter's DNS is snapshotted (by adapter GUID+index)
# before the switch, so revert puts back exactly what was there — including
# "no static DNS" (DHCP), which we record as None and restore by clearing.
_DNS_CLOUDFLARE = ["1.1.1.1", "1.0.0.1"]




def _dns_snapshot_key():
    return {"schema": 1, "adapters": {}}




def _dns_set_command(idx: str, servers) -> list:
    """PowerShell argv that sets an adapter's DNS servers (shell=False,
    no quoting surface). Extracted pure so the harness can assert the
    exact command for default AND custom servers without executing it."""
    quoted = ",".join(f"'{s}'" for s in servers)
    return ps_argv(f"Set-DnsClientServerAddress -InterfaceIndex {idx} "
                   f"-ServerAddresses ({quoted})")




_DEVMODE_CLS = None




def _devmode_class():
    """Build (and cache) the one correct, complete DEVMODEW ctypes layout
    (wingdi.h) for EnumDisplaySettingsW / ChangeDisplaySettingsW.

    Grok-assisted audit fix: this file previously had THREE separate inline
    DEVMODE definitions (in _query_video_mode_list, apply_refresh_rate_fix,
    revert_refresh_rate_fix), and all three were missing ~84 bytes in the
    middle of the real struct — the dmColor/dmDuplex/dmYResolution/
    dmTTOption/dmCollate SHORTs, the 32-WCHAR dmFormName, and dmLogPixels —
    and had dmBitsPerPel positioned before dmPelsWidth/dmPelsHeight instead
    of after. dmSize told Windows how big THIS (too-short) buffer was, but
    EnumDisplaySettingsW/ChangeDisplaySettingsW write to the real struct's
    fixed offsets regardless of what dmSize claims — so this code's
    dmPelsWidth/dmPelsHeight fields were reading (and writing, in the
    apply/revert direction) bytes from the wrong real field entirely,
    producing the "0x0" resolution shown everywhere it's surfaced (Monitor
    Test, PC Specs) while dmDisplayFrequency, positioned differently,
    happened to still land close enough to read correctly. This is also
    the exact class of bug the file's own comment above already worried
    about ("a short struct lets a sloppy display driver write past the
    buffer — heap corruption... observed on virtual GPUs") — the fix for
    that is a COMPLETE struct (this one totals the documented 220 bytes),
    not a slightly-larger-but-still-wrong one.
    One shared, correct, size-verified definition now backs every caller
    instead of three separately-maintained (and equally wrong) copies.
    """
    global _DEVMODE_CLS
    if _DEVMODE_CLS is not None:
        return _DEVMODE_CLS
    import ctypes
    from ctypes import wintypes

    class DEVMODE(ctypes.Structure):
        _fields_ = [
            ("dmDeviceName", wintypes.WCHAR * 32),
            ("dmSpecVersion", wintypes.WORD),
            ("dmDriverVersion", wintypes.WORD),
            ("dmSize", wintypes.WORD),
            ("dmDriverExtra", wintypes.WORD),
            ("dmFields", wintypes.DWORD),
            # 16-byte union (display case: position + orientation/output).
            ("dmPositionX", ctypes.c_long),
            ("dmPositionY", ctypes.c_long),
            ("dmDisplayOrientation", wintypes.DWORD),
            ("dmDisplayFixedOutput", wintypes.DWORD),
            ("dmColor", ctypes.c_short),
            ("dmDuplex", ctypes.c_short),
            ("dmYResolution", ctypes.c_short),
            ("dmTTOption", ctypes.c_short),
            ("dmCollate", ctypes.c_short),
            ("dmFormName", wintypes.WCHAR * 32),
            ("dmLogPixels", wintypes.WORD),
            ("dmBitsPerPel", wintypes.DWORD),
            ("dmPelsWidth", wintypes.DWORD),
            ("dmPelsHeight", wintypes.DWORD),
            ("dmDisplayFlags", wintypes.DWORD),
            ("dmDisplayFrequency", wintypes.DWORD),
            ("dmICMMethod", wintypes.DWORD),
            ("dmICMIntent", wintypes.DWORD),
            ("dmMediaType", wintypes.DWORD),
            ("dmDitherType", wintypes.DWORD),
            ("dmReserved1", wintypes.DWORD),
            ("dmReserved2", wintypes.DWORD),
            ("dmPanningWidth", wintypes.DWORD),
            ("dmPanningHeight", wintypes.DWORD),
        ]
    assert ctypes.sizeof(DEVMODE) == 220, \
        f"DEVMODE layout drifted from the documented 220 bytes: {ctypes.sizeof(DEVMODE)}"
    _DEVMODE_CLS = DEVMODE
    return DEVMODE




def _query_video_mode_list():
    """Return (current, maximum) (width, height, hz, bits) tuples via
    EnumDisplaySettings, or (None, None) if anything fails."""
    if not IS_WINDOWS:
        return None, None
    import ctypes
    try:
        DEVMODE = _devmode_class()
        user32 = ctypes.windll.user32
        dm = DEVMODE()
        dm.dmSize = ctypes.sizeof(DEVMODE)
        if not user32.EnumDisplaySettingsW(None, ctypes.c_ulong(0xFFFFFFFF), ctypes.byref(dm)):  # ENUM_CURRENT_SETTINGS
            return None, None
        w, h = int(dm.dmPelsWidth), int(dm.dmPelsHeight)
        # Grok-suggested defensive fallback: on the rare driver that still
        # reports 0 even off the now-correct struct, GetSystemMetrics is a
        # second, independent way to get the real desktop resolution rather
        # than surfacing "0x0" to the user.
        if w <= 0 or h <= 0:
            w = int(user32.GetSystemMetrics(0))   # SM_CXSCREEN
            h = int(user32.GetSystemMetrics(1))   # SM_CYSCREEN
        current = (w, h, dm.dmDisplayFrequency, dm.dmBitsPerPel)
        # walk mode i=0.. until 0 to find the max frequency at current resolution+depth
        best = None
        i = 0
        while True:
            m = DEVMODE()
            m.dmSize = ctypes.sizeof(DEVMODE)
            if not user32.EnumDisplaySettingsW(None, i, ctypes.byref(m)):
                break
            if (m.dmPelsWidth, m.dmPelsHeight, m.dmBitsPerPel) == current[:1] + current[1:2] + current[3:4]:
                if best is None or m.dmDisplayFrequency > best:
                    best = m.dmDisplayFrequency
            i += 1
        maximum = (current[0], current[1], best, current[3]) if best else current
        return current, maximum
    except Exception:
        return None, None




def _query_precise_refresh_rate():
    """Exact rational refresh rate (e.g. 59.94Hz) via QueryDisplayConfig.
    Read-only, active paths only. Returns float or None — never guesses.
    Fail-closed: struct-size approximations that don't match hardware just
    return None and callers fall back to the integer Hz."""
    if not IS_WINDOWS:
        return None
    import ctypes
    from ctypes import wintypes
    try:
        class LUID(ctypes.Structure):
            _fields_ = [("LowPart", wintypes.DWORD), ("HighPart", wintypes.LONG)]

        class _RATIONAL(ctypes.Structure):
            _fields_ = [("Numerator", wintypes.UINT), ("Denominator", wintypes.UINT)]

        class _SRC(ctypes.Structure):
            _fields_ = [("adapterId", LUID), ("id", wintypes.UINT),
                        ("modeInfoIdx", wintypes.UINT), ("statusFlags", wintypes.UINT)]

        class _TGT(ctypes.Structure):
            _fields_ = [("adapterId", LUID), ("id", wintypes.UINT),
                        ("modeInfoIdx", wintypes.UINT), ("outputTechnology", wintypes.UINT),
                        ("rotation", wintypes.UINT), ("scaling", wintypes.UINT),
                        ("refreshRate", _RATIONAL), ("scanLineOrdering", wintypes.UINT),
                        ("targetAvailable", wintypes.BOOL), ("statusFlags", wintypes.UINT)]

        class _PATH(ctypes.Structure):
            _fields_ = [("sourceInfo", _SRC), ("targetInfo", _TGT), ("flags", wintypes.UINT)]

        user32 = ctypes.windll.user32
        pc, mc = wintypes.UINT(), wintypes.UINT()
        if user32.GetDisplayConfigBufferSizes(2, ctypes.byref(pc), ctypes.byref(mc)) != 0:
            return None
        if not pc.value:
            return None
        paths = (_PATH * pc.value)()
        modes = (ctypes.c_byte * (mc.value * 64))()
        if user32.QueryDisplayConfig(2, ctypes.byref(pc), ctypes.byref(paths),
                                     ctypes.byref(mc), ctypes.byref(modes), None) != 0:
            return None
        rr = paths[0].targetInfo.refreshRate
        if not rr.Denominator:
            return None
        return rr.Numerator / rr.Denominator
    except Exception:
        return None




def _query_monitor_model():
    """Best-effort monitor name from EDID via WmiMonitorID (root\\wmi
    namespace) — the same data Device Manager reads. Returns a plain
    string like 'AG276QZD2', or None if it can't be read: some
    monitors/cables/KVMs don't carry usable EDID data, and the class can
    be blocked on some locked-down images. Never guesses."""
    if not IS_WINDOWS:
        return None
    import subprocess as _sp
    try:
        cmd = ("Get-CimInstance -Namespace root\\wmi -ClassName WmiMonitorID "
               "-ErrorAction Stop | Select-Object UserFriendlyName,"
               "UserFriendlyNameLength | ConvertTo-Json -Compress")
        creationflags = getattr(_sp, "CREATE_NO_WINDOW", 0)
        proc = _sp.run(
            ps_argv(cmd, non_interactive=True),
            capture_output=True, text=True, timeout=4.0,
            creationflags=creationflags)
        raw = (proc.stdout or "").strip()
        if not raw:
            return None
        import json as _json
        data = _json.loads(raw)
        if isinstance(data, dict):
            data = [data]
        for row in data or []:
            codes = row.get("UserFriendlyName") or []
            length = row.get("UserFriendlyNameLength")
            if not codes:
                continue
            if not isinstance(length, int) or length <= 0:
                length = len(codes)
            try:
                name = "".join(chr(c) for c in codes[:length] if c).strip()
            except Exception:
                continue
            if name:
                return name
        return None
    except Exception:
        return None




# GpuPreference: 0 = let Windows decide, 1 = power-saving (iGPU), 2 = high
# performance (dGPU). A global "prefer dGPU" directive keeps laptops from
# launching games on the iGPU. Written as a REG_SZ — that's what Windows'
# own Graphics Settings UI writes.
_GPU_PREF_PATH = "SOFTWARE\\Microsoft\\DirectX\\UserGpuPreferences"


_GPU_PREF_VALUE = "DirectXUserGlobalSettings"




def _read_gpu_pref() -> str:
    """Read the current global GpuPreference string (empty if unset)."""
    if not IS_WINDOWS:
        return ""
    try:
        root = winreg.OpenKey(winreg.HKEY_CURRENT_USER, _GPU_PREF_PATH)
        try:
            val, _ = winreg.QueryValueEx(root, _GPU_PREF_VALUE)
            return str(val)
        finally:
            winreg.CloseKey(root)
    except OSError:
        return ""




# --- Round 4: snapshot helpers (one value or several per tweak) --- #
def _snap_reg_values(ctx: TaskContext, task_id: str, specs: "list[tuple]"):
    """Snapshot the real prior state (value or absent) of each registry
    value so revert restores exactly what was there — never a guess.

    Each spec is (hive, path, name) or (hive, path, name, value_type);
    the per-value type is stored so mixed-type tweaks (REG_SZ + REG_DWORD)
    restore with the right type (M7 audit gap: the old single value_type
    arg restored every value as one type).
    """
    from app.config_persist import save_tweak_snapshot
    from app.utils import _REG_DENIED, reg_get_value_typed
    data: dict = {"specs": [list(s[:3]) for s in specs]}
    for i, spec in enumerate(specs):
        hive, path, name = spec[0], spec[1], spec[2]
        # CT-001 audit fix: read the REAL prior type via QueryValueEx instead
        # of guessing REG_DWORD for any spec that didn't spell out a 4th
        # element. The old default silently mis-stamped REG_SZ priors (e.g.
        # KeyboardDelay/KeyboardSpeed, WaitToKillAppTimeout — all natively
        # REG_SZ on Windows) as REG_DWORD, so restoring the string prior
        # under that type raised inside winreg.SetValueEx and revert failed
        # outright for every tweak using a 3-element spec against a REG_SZ
        # value (fast_app_close, keyboard_tuning, numlock_boot,
        # visual_effects and any future 3-element spec against a string
        # value). Precedence: real queried type first (it's what's actually
        # there); an explicit spec[3] override only when nothing could be
        # queried (value absent/denied); "REG_DWORD" as the last-resort
        # fallback so legacy snapshots without recorded types keep working.
        prior, queried_type = reg_get_value_typed(ctx, hive, path, name)
        # F13: denied reads are PRESENT-but-unreadable — never "absent"
        # (absent made revert delete a value the user already had).
        data[f"{i}:present"] = prior is not None and prior is not _REG_DENIED
        data[f"{i}:denied"] = prior is _REG_DENIED
        # config.json can't hold bytes (REG_BINARY priors) — hex-encode
        # with a marker; restore decodes. Without this the save below
        # throws TypeError and the tweak wrongly reports failure.
        if isinstance(prior, bytes):
            data[f"{i}:value"] = {"__bytes_hex__": prior.hex()}
        else:
            data[f"{i}:value"] = None if prior is _REG_DENIED else prior
        data[f"{i}:type"] = queried_type or (spec[3] if len(spec) > 3 else "REG_DWORD")
    save_tweak_snapshot(task_id, data)




# ---- prior SEC-001: code-owned per-task registry allowlist ------------------------------
#
# The tweak snapshot lives in USER-WRITABLE config.json and is replayed
# into the registry while ELEVATED. Until now the only check on those
# persisted (hive, path, name) triples was a DENYLIST of persistence
# locations, so anything not on that list was permitted - a tampered
# record could name any non-persistence HKCU/HKLM/HKCR target.
#
# This table is CODE-OWNED and is the primary control: a snapshot spec is
# honoured only if the task itself is defined to touch that location.
# It is derived from the same spec lists the apply path passes, and
# dev/tools/verify_sec001_task_allowlist.py recomputes it from this source
# and fails on drift, so a new tweak location cannot slip in undeclared.
#
# SCOPE, RE-MEASURED 2026-10 - and this correction closes the stale claim
# that used to sit here. That note listed SIX tasks as still denylist-only
# "because they build their locations dynamically and need PREFIX rules":
# disable_nagle, ssd_prefetch, stop_windows_ads, disable_sticky_keys,
# prefer_ipv4, taskbar_cleanup. Re-deriving against this file shows five of
# those six no longer build anything at run time:
#   * ssd_prefetch, stop_windows_ads and prefer_ipv4 were moved onto
#     module-level constant spec lists (_SSD_PREFETCH_SPECS,
#     _STOP_ADS_SPECS, _IPV4_SPECS) that the apply path passes to
#     _snap_reg_values, and disable_sticky_keys onto _regn_apply with
#     literal triples - so the drift guard derives all four exactly like
#     every other static task. They are in _TASK_REG_ALLOWLIST below, not
#     missing from it;
#   * taskbar_cleanup is version-FILTERED rather than dynamic, so it is
#     transcribed in _TASK_REG_ALLOWLIST_MANUAL.
# disable_nagle is the only genuinely dynamic one (a different subkey per
# interface GUID), and it is the one entry in _TASK_REG_ALLOW_PREFIX.
#
# Measured totals, reproduced by dev/tools/verify_sec001_task_allowlist.py:
#   67 derived + 4 manual + 1 prefix = 72 ids over 89 task keys.
# The remaining tasks do not snapshot registry values at all (other helpers,
# or no registry write), so there is nothing for this table to authorise.
#: Allowlist ids that are deliberately NOT Tweak task keys, each with the reason
#: it exists. SEC-001's guard used to infer this from the id's SPELLING - any
#: entry ending in "_regs"/"_pmz"/"_specs" was reported as "expected, internal"
#: - which meant a typo'd or invented entry that happened to carry one of those
#: suffixes passed without anyone looking at it. The reason now lives beside the
#: entry it explains, and the guard prints it on every run, so an id that is not
#: a task key has to say why in one sentence.
_TASK_REG_ALLOWLIST_NOT_A_TASK = {
    "activity_history_disable":
        "snapshot id used by apply_activity_history_disable; the task key is 'activity_history'",
    "max_performance_gpu_pmz":
        "second snapshot taken under 'max_performance_gpu' (the NVIDIA PowerMizer value)",
    "nvidia_telemetry_regs":
        "snapshot id for the NVIDIA telemetry triples; no task carries this key",
    "adv_memory_integrity":
        "cut from the Tweak tab (CUT_TASK_KEYS), but its apply/revert still read their snapshot",
    "wpbt_disable":
        "cut from the Tweak tab (CUT_TASK_KEYS), but its apply/revert still read their snapshot",
}

_TASK_REG_ALLOWLIST = {
    "activity_history_disable": (
        ("HKLM", "SOFTWARE\\Policies\\Microsoft\\Windows\\System", "EnableActivityFeed"),
        ("HKLM", "SOFTWARE\\Policies\\Microsoft\\Windows\\System", "PublishUserActivities"),
        ("HKLM", "SOFTWARE\\Policies\\Microsoft\\Windows\\System", "UploadUserActivities"),
    ),
    "ads_master_off": (
        ("HKCU", "Software\\Microsoft\\Windows\\CurrentVersion\\ContentDeliveryManager", "ContentDeliveryAllowed"),
        ("HKCU", "Software\\Microsoft\\Windows\\CurrentVersion\\ContentDeliveryManager", "FeatureManagementEnabled"),
        ("HKCU", "Software\\Microsoft\\Windows\\CurrentVersion\\ContentDeliveryManager", "SubscribedContentEnabled"),
    ),
    "aero_shake": (
        ("HKCU", "Software\\Microsoft\\Windows\\CurrentVersion\\Explorer\\Advanced", "DisallowShaking"),
    ),
    "alt_tab_windows_only": (
        ("HKCU", "Software\\Microsoft\\Windows\\CurrentVersion\\Explorer\\Advanced", "MultiTaskingAltTabFilter"),
    ),
    "autoplay_off": (
        ("HKCU", "Software\\Microsoft\\Windows\\CurrentVersion\\Explorer\\AutoplayHandlers", "DisableAutoplay"),
        ("HKCU", "Software\\Microsoft\\Windows\\CurrentVersion\\Policies\\Explorer", "NoDriveTypeAutoRun"),
    ),
    "background_apps": (
        ("HKCU", "Software\\Microsoft\\Windows\\CurrentVersion\\BackgroundAccessApplications", "GlobalUserDisabled"),
    ),
    "classic_context_menu": (
        ("HKCU", "Software\\Classes\\CLSID\\{86ca1aa0-34aa-4e8b-a509-50c905bae2a2}\\InprocServer32", ""),
    ),
    "classic_shortcut_icons": (
        ("HKCU", "Software\\Microsoft\\Windows\\CurrentVersion\\Explorer", "link"),
        ("HKLM", "SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\Explorer\\Shell Icons", "29"),
    ),
    "clipboard_sync_off": (
        ("HKLM", "SOFTWARE\\Policies\\Microsoft\\Windows\\System", "AllowCrossDeviceClipboard"),
    ),
    "clock_seconds": (
        ("HKCU", "Software\\Microsoft\\Windows\\CurrentVersion\\Explorer\\Advanced", "ShowSecondsInSystemClock"),
    ),
    "cortana_off": (
        ("HKLM", r"SOFTWARE\Policies\Microsoft\Windows\Windows Search", "AllowCortana"),
        ("HKLM", r"SOFTWARE\Policies\Microsoft\Windows\Windows Search", "CortanaConsent"),
    ),
    "consumer_features": (
        ("HKLM", "SOFTWARE\\Policies\\Microsoft\\Windows\\CloudContent", "DisableWindowsConsumerFeatures"),
    ),
    "dark_mode": (
        ("HKCU", "Software\\Microsoft\\Windows\\CurrentVersion\\Themes\\Personalize", "AppsUseLightTheme"),
    ),
    "device_search_history_off": (
        ("HKCU", "Software\\Microsoft\\Windows\\CurrentVersion\\SearchSettings", "IsDeviceSearchHistoryEnabled"),
    ),
    "disable_fast_startup": (
        ("HKLM", "SYSTEM\\CurrentControlSet\\Control\\Session Manager\\Power", "HiberbootEnabled"),
    ),
    "disable_game_dvr": (
        ("HKCU", "Software\\Microsoft\\Windows\\CurrentVersion\\GameDVR", "AppCaptureEnabled"),
        ("HKCU", "System\\GameConfigStore", "GameDVR_Enabled"),
        ("HKLM", "SOFTWARE\\Policies\\Microsoft\\Windows\\GameDVR", "AllowGameDVR"),
    ),
    "disable_sticky_keys": (
        ("HKCU", "Control Panel\\Accessibility\\Keyboard Response", "Flags"),
        ("HKCU", "Control Panel\\Accessibility\\StickyKeys", "Flags"),
        ("HKCU", "Control Panel\\Accessibility\\ToggleKeys", "Flags"),
    ),
    "discord_no_ducking": (
        ("HKCU", "Software\\Microsoft\\Multimedia\\Audio", "UserDuckingPreference"),
    ),
    "edge_preload": (
        ("HKLM", "SOFTWARE\\Policies\\Microsoft\\Edge", "BackgroundModeEnabled"),
        ("HKLM", "SOFTWARE\\Policies\\Microsoft\\Edge", "StartupBoostEnabled"),
    ),
    "fast_app_close": (
        ("HKCU", "Control Panel\\Desktop", "AutoEndTasks"),
        ("HKCU", "Control Panel\\Desktop", "HungAppTimeout"),
        ("HKCU", "Control Panel\\Desktop", "WaitToKillAppTimeout"),
    ),
    "file_extensions": (
        ("HKCU", "Software\\Microsoft\\Windows\\CurrentVersion\\Explorer\\Advanced", "Hidden"),
        ("HKCU", "Software\\Microsoft\\Windows\\CurrentVersion\\Explorer\\Advanced", "HideFileExt"),
    ),
    "fullscreen_opt": (
        ("HKCU", "Software\\Microsoft\\Windows NT\\CurrentVersion\\AppCompatFlags\\Layers", "DISABLEDXMAXIMIZEDWINDOWEDMODE"),
    ),
    "game_mode": (
        ("HKCU", "Software\\Microsoft\\GameBar", "AllowAutoGameMode"),
        ("HKCU", "Software\\Microsoft\\GameBar", "AutoGameModeEnabled"),
    ),
    "gamebar_capture": (
        ("HKCU", "System\\GameConfigStore", "GameDVR_Enabled"),
        ("HKCU", "Software\\Microsoft\\Windows\\CurrentVersion\\GameDVR", "AppCaptureEnabled"),
        ("HKLM", "SOFTWARE\\Policies\\Microsoft\\Windows\\GameDVR", "AllowGameDVR"),
    ),
    "games_priority": (
        ("HKLM", "SOFTWARE\\Microsoft\\Windows NT\\CurrentVersion\\Multimedia\\SystemProfile\\Tasks\\Games", "GPU Priority"),
        ("HKLM", "SOFTWARE\\Microsoft\\Windows NT\\CurrentVersion\\Multimedia\\SystemProfile\\Tasks\\Games", "Priority"),
        ("HKLM", "SOFTWARE\\Microsoft\\Windows NT\\CurrentVersion\\Multimedia\\SystemProfile\\Tasks\\Games", "SFIO Priority"),
        ("HKLM", "SOFTWARE\\Microsoft\\Windows NT\\CurrentVersion\\Multimedia\\SystemProfile\\Tasks\\Games", "Scheduling Category"),
    ),
    "hags": (
        ("HKLM", "SYSTEM\\CurrentControlSet\\Control\\GraphicsDrivers", "HwSchMode"),
    ),
    "hide_recent": (
        ("HKCU", "Software\\Microsoft\\Windows\\CurrentVersion\\Explorer", "ShowFrequent"),
        ("HKCU", "Software\\Microsoft\\Windows\\CurrentVersion\\Explorer", "ShowRecent"),
    ),
    "instant_alt_tab": (
        ("HKCU", "Control Panel\\Desktop", "ForegroundFlashCount"),
        ("HKCU", "Control Panel\\Desktop", "ForegroundLockTimeout"),
    ),
    "keyboard_tuning": (
        ("HKCU", "Control Panel\\Keyboard", "KeyboardDelay"),
        ("HKCU", "Control Panel\\Keyboard", "KeyboardSpeed"),
    ),
    "limit_telemetry": (
        ("HKLM", "SOFTWARE\\Policies\\Microsoft\\Windows\\DataCollection", "AllowTelemetry"),
    ),
    "local_search": (
        ("HKCU", "Software\\Microsoft\\Windows\\CurrentVersion\\Search", "BingSearchEnabled"),
        ("HKCU", "Software\\Microsoft\\Windows\\CurrentVersion\\Search", "CortanaConsent"),
        ("HKCU", "Software\\Microsoft\\Windows\\CurrentVersion\\SearchSettings", "IsAADCloudSearchEnabled"),
        ("HKCU", "Software\\Microsoft\\Windows\\CurrentVersion\\SearchSettings", "IsMSACloudSearchEnabled"),
        ("HKCU", "Software\\Policies\\Microsoft\\Windows\\Explorer", "DisableSearchBoxSuggestions"),
        ("HKLM", "SOFTWARE\\Policies\\Microsoft\\Windows\\Explorer", "DisableSearchBoxSuggestions"),
    ),
    "location_tracking": (
        ("HKLM", "SOFTWARE\\Policies\\Microsoft\\Windows\\LocationAndSensors", "DisableLocation"),
    ),
    "lock_screen": (
        ("HKLM", "SOFTWARE\\Policies\\Microsoft\\Windows\\Personalization", "NoLockScreen"),
    ),
    "lockscreen_tips_off": (
        ("HKCU", r"Software\Microsoft\Windows\CurrentVersion\ContentDeliveryManager", "SubscribedContent-338387Enabled"),
        ("HKCU", r"Software\Microsoft\Windows\CurrentVersion\ContentDeliveryManager", "RotatingLockScreenOverlay"),
    ),
    "long_paths": (
        ("HKLM", "SYSTEM\\CurrentControlSet\\Control\\FileSystem", "LongPathsEnabled"),
    ),
    "max_performance_gpu_pmz": (
        ("HKCU", "Software\\NVIDIA Corporation\\Global\\FTS", "PowerMizerEnable"),
    ),
    "menu_delay": (
        ("HKCU", "Control Panel\\Desktop", "MenuShowDelay"),
    ),
    "mic_privacy_allow": (
        ("HKCU", "Software\\Microsoft\\Windows\\CurrentVersion\\CapabilityAccessManager\\ConsentStore\\microphone", "Value"),
    ),
    "mouse_accel": (
        ("HKCU", "Control Panel\\Mouse", "MouseSpeed"),
        ("HKCU", "Control Panel\\Mouse", "MouseThreshold1"),
        ("HKCU", "Control Panel\\Mouse", "MouseThreshold2"),
    ),
    "mpo_fix": (
        ("HKLM", "SOFTWARE\\Microsoft\\Windows\\Dwm", "OverlayTestMode"),
    ),
    "network_throttling": (
        ("HKLM", "SOFTWARE\\Microsoft\\Windows NT\\CurrentVersion\\Multimedia\\SystemProfile", "NetworkThrottlingIndex"),
        ("HKLM", "SOFTWARE\\Microsoft\\Windows NT\\CurrentVersion\\Multimedia\\SystemProfile", "SystemResponsiveness"),
    ),
    "no_update_reboot": (
        ("HKLM", "SOFTWARE\\Policies\\Microsoft\\Windows\\WindowsUpdate\\AU", "NoAutoRebootWithLoggedOnUsers"),
    ),
    "numlock_boot": (
        ("HKCU", "Control Panel\\Desktop", "InitialKeyboardIndicators"),
    ),
    "power_throttling_off": (
        ("HKLM", "SYSTEM\\CurrentControlSet\\Control\\Power\\PowerThrottling", "PowerThrottlingOff"),
    ),
    "prefer_ipv4": (
        ("HKLM", "SYSTEM\\CurrentControlSet\\Services\\Tcpip6\\Parameters", "DisabledComponents"),
    ),
    "priority_separation": (
        ("HKLM", "SYSTEM\\CurrentControlSet\\Control\\PriorityControl", "Win32PrioritySeparation"),
    ),
    "privacy_baseline": (
        ("HKCU", "Control Panel\\International\\User Profile", "HttpAcceptLanguageOptOut"),
        ("HKCU", "Software\\Microsoft\\InputPersonalization\\TrainedDataStore", "HarvestContacts"),
        ("HKCU", "Software\\Microsoft\\Input\\TIPC", "Enabled"),
        ("HKCU", "Software\\Microsoft\\Personalization\\Settings", "AcceptedPrivacyPolicy"),
        ("HKCU", "Software\\Microsoft\\Siuf\\Rules", "NumberOfSIUFInPeriod"),
        ("HKCU", "Software\\Microsoft\\Windows\\CurrentVersion\\AdvertisingInfo", "Enabled"),
        ("HKCU", "Software\\Microsoft\\Windows\\CurrentVersion\\CapabilityAccessManager\\ConsentStore\\humaninterfaceenterprise", "Value"),
        ("HKCU", "Software\\Microsoft\\Windows\\CurrentVersion\\Explorer\\Advanced", "Start_TrackProgs"),
        ("HKCU", "Software\\Microsoft\\Windows\\CurrentVersion\\OnlineSpeechPrivacy", "HasAccepted"),
        ("HKCU", "Software\\Microsoft\\Windows\\CurrentVersion\\Privacy", "TailoredExperiencesWithDiagnosticDataEnabled"),
        ("HKLM", "SOFTWARE\\Policies\\Microsoft\\Input\\TIPC", "Enabled"),
        ("HKLM", "SOFTWARE\\Policies\\Microsoft\\Windows\\System", "EnableActivityFeed"),
    ),
    "remote_assist": (
        ("HKLM", "SYSTEM\\CurrentControlSet\\Control\\Terminal Server", "fAllowToGetHelp"),
    ),
    "snap_flyout_off": (
        ("HKCU", "Software\\Microsoft\\Windows\\CurrentVersion\\Explorer\\Advanced", "EnableSnapAssistFlyout"),
    ),
    "ssd_prefetch": (
        ("HKLM", "SYSTEM\\CurrentControlSet\\Control\\Session Manager\\Memory Management\\PrefetchParameters", "EnablePrefetcher"),
        ("HKLM", "SYSTEM\\CurrentControlSet\\Control\\Session Manager\\Memory Management\\PrefetchParameters", "EnableSuperfetch"),
    ),
    "startup_delay": (
        ("HKCU", "Software\\Microsoft\\Windows\\CurrentVersion\\Explorer\\Serialize", "StartupDelayInMSec"),
    ),
    "stop_windows_ads": (
        ("HKCU", "Software\\Microsoft\\Windows\\CurrentVersion\\ContentDeliveryManager", "OemPreInstalledAppsEnabled"),
        ("HKCU", "Software\\Microsoft\\Windows\\CurrentVersion\\ContentDeliveryManager", "PreInstalledAppsEnabled"),
        ("HKCU", "Software\\Microsoft\\Windows\\CurrentVersion\\ContentDeliveryManager", "RotatingLockScreenOverlayEnabled"),
        ("HKCU", "Software\\Microsoft\\Windows\\CurrentVersion\\ContentDeliveryManager", "SilentInstalledAppsEnabled"),
        ("HKCU", "Software\\Microsoft\\Windows\\CurrentVersion\\ContentDeliveryManager", "SoftLandingEnabled"),
        ("HKCU", "Software\\Microsoft\\Windows\\CurrentVersion\\ContentDeliveryManager", "SubscribedContent-310093Enabled"),
        ("HKCU", "Software\\Microsoft\\Windows\\CurrentVersion\\ContentDeliveryManager", "SubscribedContent-338387Enabled"),
        ("HKCU", "Software\\Microsoft\\Windows\\CurrentVersion\\ContentDeliveryManager", "SubscribedContent-338388Enabled"),
        ("HKCU", "Software\\Microsoft\\Windows\\CurrentVersion\\ContentDeliveryManager", "SubscribedContent-338389Enabled"),
        ("HKCU", "Software\\Microsoft\\Windows\\CurrentVersion\\ContentDeliveryManager", "SubscribedContent-338393Enabled"),
        ("HKCU", "Software\\Microsoft\\Windows\\CurrentVersion\\ContentDeliveryManager", "SubscribedContent-353694Enabled"),
        ("HKCU", "Software\\Microsoft\\Windows\\CurrentVersion\\ContentDeliveryManager", "SubscribedContent-353696Enabled"),
        ("HKCU", "Software\\Microsoft\\Windows\\CurrentVersion\\ContentDeliveryManager", "SubscribedContent-353698Enabled"),
        ("HKCU", "Software\\Microsoft\\Windows\\CurrentVersion\\ContentDeliveryManager", "SystemPaneSuggestionsEnabled"),
        ("HKCU", "Software\\Microsoft\\Windows\\CurrentVersion\\UserProfileEngagement", "ScoobeSystemSettingEnabled"),
        ("HKLM", "SOFTWARE\\Policies\\Microsoft\\Windows\\CloudContent", "DisableCloudOptimizedContent"),
        ("HKLM", "SOFTWARE\\Policies\\Microsoft\\Windows\\CloudContent", "DisableSoftLanding"),
    ),
    "storage_sense": (
        ("HKCU", "Software\\Microsoft\\Windows\\CurrentVersion\\StorageSense\\Parameters\\StoragePolicy", "01"),
        ("HKCU", "Software\\Microsoft\\Windows\\CurrentVersion\\StorageSense\\Parameters\\StoragePolicy", "04"),
        ("HKCU", "Software\\Microsoft\\Windows\\CurrentVersion\\StorageSense\\Parameters\\StoragePolicy", "256"),
    ),
    "suppress_crash_popups": (
        ("HKCU", "Software\\Microsoft\\Windows\\Windows Error Reporting", "DontShowUI"),
    ),
    "taskbar_endtask": (
        ("HKCU", "Software\\Microsoft\\Windows\\CurrentVersion\\Explorer\\Advanced\\TaskbarDeveloperSettings", "TaskbarEndTask"),
    ),
    "taskbar_left": (
        ("HKCU", "Software\\Microsoft\\Windows\\CurrentVersion\\Explorer\\Advanced", "TaskbarAl"),
    ),
    "taskbar_never_combine": (
        ("HKCU", "Software\\Microsoft\\Windows\\CurrentVersion\\Explorer\\Advanced", "TaskbarGlomLevel"),
    ),
    "transparency_off": (
        ("HKCU", r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize", "EnableTransparency"),
    ),
    "tdr_delay": (
        ("HKLM", "SYSTEM\\CurrentControlSet\\Control\\GraphicsDrivers", "TdrDelay"),
    ),
    "tweak_delivery_optimization": (
        ("HKLM", "SOFTWARE\\Policies\\Microsoft\\Windows\\DeliveryOptimization", "DODownloadMode"),
    ),
    "verbose_boot": (
        ("HKLM", "SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\Policies\\System", "VerboseStatus"),
    ),
    # UNDO-001: this listed only MenuShowDelay, faithfully mirroring an
    # incomplete snapshot -- the other three values the apply writes were
    # therefore unrestorable, and the revert hardcoded 1 for them, destroying
    # whatever the user actually had. All four are listed now, which is what
    # lets the snapshot cover the whole write set.
    "visual_effects": (
        ("HKCU", "Software\\Microsoft\\Windows\\CurrentVersion\\Themes\\Personalize",
         "EnableTransparency"),
        ("HKCU", "Control Panel\\Desktop\\WindowMetrics", "MinAnimate"),
        ("HKCU", "Software\\Microsoft\\Windows\\CurrentVersion\\Explorer\\Advanced",
         "TaskbarAnimations"),
        ("HKCU", "Control Panel\\Desktop", "MenuShowDelay"),
    ),
    # UNDO-003: the Advanced tab's registry-backed tasks. They shipped with
    # no snapshot at all and reverted by hardcoding a default or deleting the
    # value, which destroyed the user's real prior state. They now snapshot
    # through the same helpers as everything else here, and these entries are
    # what let the restore path accept exactly those keys.
    "adv_memory_integrity": (
        ("HKLM", "SYSTEM\\CurrentControlSet\\Control\\DeviceGuard",
         "EnableVirtualizationBasedSecurity"),
        ("HKLM", "SYSTEM\\CurrentControlSet\\Control\\DeviceGuard\\Scenarios\\HypervisorEnforcedCodeIntegrity",
         "Enabled"),
    ),
    "adv_copilot": (
        ("HKCU", "Software\\Policies\\Microsoft\\Windows\\WindowsCopilot",
         "TurnOffWindowsCopilot"),
    ),
    "adv_disable_ai": (
        ("HKLM", "SOFTWARE\\Policies\\Microsoft\\Windows\\WindowsAI",
         "AllowRecallEnablement"),
        ("HKLM", "SOFTWARE\\Policies\\Microsoft\\Windows\\WindowsAI",
         "DisableAIDataAnalysis"),
        ("HKLM", "SOFTWARE\\Policies\\Microsoft\\Windows\\WindowsAI",
         "DisableClickToDo"),
        ("HKLM", "SOFTWARE\\Policies\\Microsoft\\Windows\\WindowsAI",
         "RemoveMicrosoftCopilotApp"),
    ),
    "wpbt_disable": (
        ("HKLM", "SYSTEM\\CurrentControlSet\\Control\\Session Manager",
         "DisableWpbtExecution"),
    ),
    "widgets_board_off": (
        ("HKCU", "Software\\Microsoft\\Windows\\CurrentVersion\\Feeds", "ShellFeedsTaskbarViewMode"),
        ("HKLM", "SOFTWARE\\Policies\\Microsoft\\Dsh", "AllowNewsAndInterests"),
    ),
    "windowed_optimize": (
        ("HKCU", "System\\GameConfigStore", "GameDVR_DXGIHonorFSEWindowsCompatible"),
        ("HKCU", "System\\GameConfigStore", "GameDVR_FSEBehaviorMode"),
        ("HKCU", "System\\GameConfigStore", "GameDVR_HonorUserFSEBehaviorMode"),
    ),
}




# ---- prior SEC-001: entries the source-derivation cannot reach

# taskbar_cleanup builds its spec list from FUNCTION-LOCAL variables
#
#   writes = [("HKCU", adv, "TaskbarDa", 0, True), ...]
#   active = [w for w in writes if not (w[4] and not is_win11)]
#   _snap_reg_values(ctx, "taskbar_cleanup", [(h, p, n) for h, p, n, _v, _w in active])
#
# so it is invisible to a resolver that walks module-level constants, and it is
# filtered by Windows version - honouring that filter would make a derived table
# depend on the machine it was generated on. These five are transcribed by hand,
# which is precisely what the drift guard exists to make safe, so they are
# declared SEPARATELY. `_verify_manual_rules` in the guard then checks each one
# still appears as a literal in the owning apply function, so an entry cannot
# quietly authorise a location the code no longer writes. Its automatic
# derivation is NOT weakened: it still proves completeness for every task it can
# derive (`shader_cache_10gb`, `ssd_last_access` and `nvidia_telemetry_regs` are
# listed here too, but the scanner DOES derive those, so the normal drift check
# compares them exactly and the hand check skips them).
#
# The list is the UNFILTERED superset. A snapshot can only ever hold a subset of
# what apply wrote, so allowlisting the superset grants nothing extra.
_TASK_REG_ALLOWLIST_MANUAL = {
    "taskbar_cleanup": (
        ("HKCU", r"Software\Microsoft\Windows\CurrentVersion\Explorer\Advanced", "TaskbarDa"),
        ("HKCU", r"Software\Microsoft\Windows\CurrentVersion\Explorer\Advanced", "TaskbarMn"),
        ("HKCU", r"Software\Microsoft\Windows\CurrentVersion\Explorer\Advanced", "ShowSyncProviderNotifications"),
        ("HKCU", r"Software\Microsoft\Windows\CurrentVersion\SearchSettings", "IsDynamicSearchBoxEnabled"),
        ("HKLM", r"Software\Microsoft\Windows\CurrentVersion\Policies\Explorer", "HideSCAMeetNow"),
    ),
    # Phase 4 (4.1): added when M-6 / M-8 moved these tasks onto _regn_apply,
    # which makes their write set exactly these triples — so the allowlist entry
    # is COMPLETE and cannot break Undo. dev/tools/verify_sec001_task_allowlist.py
    # statically re-derives these from the _regn_apply calls and fails on drift.
    "shader_cache_10gb": (
        ("HKCU", r"Software\NVIDIA Corporation\Global\FTS", "EnableGR535"),
        ("HKCU", r"Software\NVIDIA Corporation\Global\FTS", "RMShaderCacheSize"),
    ),
    "ssd_last_access": (
        ("HKLM", r"SYSTEM\CurrentControlSet\Control\FileSystem", "NtfsDisableLastAccessUpdate"),
    ),
    # M-7 introduced this SUB-snapshot id (apply_nvidia_telemetry_optout writes
    # the four preference values under it, separate from the service/task
    # snapshot so restoring one cannot clear the other). Strictly enumerable, so
    # it is allowlisted too rather than left denylist-only.
    "nvidia_telemetry_regs": (
        ("HKCU", r"Software\NVIDIA Corporation\Global\NvControlPanel2\Client", "OptInOrOutPreference"),
        ("HKCU", r"Software\NVIDIA Corporation\Global\FTS", "EnableRID44231"),
        ("HKCU", r"Software\NVIDIA Corporation\Global\FTS", "EnableRID64640"),
        ("HKCU", r"Software\NVIDIA Corporation\Global\FTS", "EnableRID66610"),
    ),
}




# ---- prior SEC-001: PREFIX rules for tweaks whose key path is built at run time

# Most tweaks name a fixed registry path, so _TASK_REG_ALLOWLIST above can hold
# exact (hive, path, name) triples. A few cannot, and this table is how they are
# expressed.
#
# disable_nagle is the case that forced it: it enumerates network interfaces and
# writes a DIFFERENT subkey per interface GUID, under
#   SYSTEM\CurrentControlSet\Services\Tcpip\Parameters\Interfaces\{GUID}
# for the values TcpAckFrequency and TCPNoDelay. There is no fixed path to
# allowlist, and hardcoding today's GUIDs would be both wrong and useless.
#
# A prefix entry authorises (hive, path, name) when the normalised path starts
# with the normalised prefix AND the value name matches. Both halves matter:
# the prefix alone would also authorise a sibling value under the same key, and
# the name alone would authorise any path.
_TASK_REG_ALLOW_PREFIX = {
    "disable_nagle": (
        ("HKLM",
         r"SYSTEM\CurrentControlSet\Services\Tcpip\Parameters\Interfaces",
         "TcpAckFrequency"),
        ("HKLM",
         r"SYSTEM\CurrentControlSet\Services\Tcpip\Parameters\Interfaces",
         "TCPNoDelay"),
    ),
}



_TASK_REG_ALLOW_PREFIX_IDS = frozenset(_TASK_REG_ALLOW_PREFIX)




def _norm_reg_spec(hive, path, name):
    """Canonical form of a registry spec, for allowlist matching only.

    prior SEC-001's verification plan explicitly calls out ALTERNATE PATH
    SPELLINGS, so matching has to be case- and separator-insensitive:
    "software\\microsoft", "SOFTWARE\\Microsoft\\", "SOFTWARE\\\\Microsoft" and
    a trailing backslash must all collapse to the same key, or the allowlist
    could be walked straight past by a tampered record.

    Only ever used to COMPARE. The value handed to the registry API is the
    original, untouched one.
    """
    def _n(s):
        s = str(s or "").replace("/", "\\")
        while "\\\\" in s:
            s = s.replace("\\\\", "\\")
        # Lexically resolve "." and ".." BEFORE comparing. Without this, a
        # tampered spec could spell
        #   ...\Tcpip\Parameters\Interfaces\..\..\..\evil
        # which still *starts with* the Interfaces prefix and so passed the
        # prefix rule, while Windows would resolve it to a completely different
        # key. Purely lexical on purpose: the key need not exist to be compared.
        parts = []
        for seg in s.strip("\\").split("\\"):
            if seg in ("", "."):
                continue
            if seg == "..":
                if parts:
                    parts.pop()
                continue
            parts.append(seg)
        return "\\".join(parts).casefold()
    return (_n(hive), _n(path), _n(name))




# Pre-normalised form of _TASK_REG_ALLOWLIST, built once at import.
# This is a dict of per-task sets, NOT one flat union across all tasks. A flat
# union looks equivalent and is not: it would let `hags` "own"
# Control Panel\Mouse\MouseSpeed just because `mouse_accel` writes it, which
# is precisely the cross-task confusion the report forbids ("match the
# persisted snapshot to the exact expected task").
# Derived entries come from the source-verified table; the hand-transcribed
# ones are merged in afterwards, and both are enforced identically.
_TASK_REG_ALLOWED_NORM = {}


for _task_id, _specs in _TASK_REG_ALLOWLIST.items():
    _TASK_REG_ALLOWED_NORM[_task_id] = frozenset(
        _norm_reg_spec(*_s) for _s in _specs)


for _task_id, _specs in _TASK_REG_ALLOWLIST_MANUAL.items():
    _TASK_REG_ALLOWED_NORM[_task_id] = frozenset(
        _norm_reg_spec(*_s) for _s in _specs)


_TASK_REG_ALLOWLIST_IDS = frozenset(_TASK_REG_ALLOWED_NORM)




# Normalised prefix table, built AFTER _norm_reg_spec exists (it is used above,
# so it must not be computed inline at definition time).
_TASK_REG_ALLOW_PREFIX_NORM = {
    _task_id: tuple(_norm_reg_spec(h, p, n) for h, p, n in _entries)
    for _task_id, _entries in _TASK_REG_ALLOW_PREFIX.items()
}



_TASK_REG_ALLOW_PREFIX_IDS = frozenset(_TASK_REG_ALLOW_PREFIX)




def _spec_allowed_for_task(task_id, hive, path, name):
    """True if `task_id` is code-defined to write exactly (hive, path, name).

    A task with NO allowlist entry returns True here and is governed by the
    denylist alone. That is a documented, measured gap - see the scope note on
    _TASK_REG_ALLOWLIST - and it is deliberately not fail-closed, because
    refusing every unmapped task would break Undo for them outright, which is
    the MED-007 regression (an Undo that can never run) this table exists to
    make impossible. Narrowing the gap is additive: each task added here moves
    from denylist-only to allowlist without touching the others.
    """
    allowed = _TASK_REG_ALLOWED_NORM.get(task_id)
    if allowed is None:
        prefixes = _TASK_REG_ALLOW_PREFIX_NORM.get(task_id)
        if prefixes is None:
            return True          # unmapped task: denylist only (documented gap)
        _h, _p, _n = _norm_reg_spec(hive, path, name)
        for _ph, _pp, _pn in prefixes:
            # The prefix is stored without a trailing separator, and the
            # candidate is compared on SEGMENT boundaries so that
            # "...\InterfacesXyz" cannot pass as "...\Interfaces".
            if _h == _ph and _n == _pn and (_p == _pp or _p.startswith(_pp + "\\")):
                return True
        return False
    return _norm_reg_spec(hive, path, name) in allowed




def _restore_reg_values(ctx: TaskContext, task_id: str, value_type: str = "REG_DWORD"):
    """Restore a snapshot taken by _snap_reg_values (exact prior values;
    absent values are deleted back to Windows defaults). Per-value stored
    types win; `value_type` is the fallback for pre-fix snapshots.

    F15: snapshots live in user-writable config.json, so they are treated as
    UNTRUSTED DATA, never as authority. Two independent gates apply, and both
    must pass:

      1. prior SEC-001 - the per-task ALLOWLIST. `_spec_allowed_for_task`
         requires the persisted (hive, path, name) to be a location the task
         itself is code-defined to write. This is the primary control: it is
         what stops a tampered record naming an arbitrary non-persistence
         HKCU/HKLM/HKCR target. Matching normalises case and separators, so an
         alternate spelling cannot walk past it.
      2. the historical DENYLIST of persistence locations, retained underneath
         as defence in depth.

    Known, measured scope gap: the allowlist covers 40 of ~65 registry-backed
    tweaks. The rest (dynamic-path tasks such as disable_nagle, and tasks that
    write without a snapshot list) are still on the denylist alone. See
    _TASK_REG_ALLOWLIST and references/FINDINGS_LEDGER.txt.

    Returns the number of values actually put back (0 when there was no
    snapshot). BUG-017: the old version returned None, so a caller had no way
    to tell "restored everything" from "did nothing", and revert_taskbar_cleanup
    printed "Taskbar items restored." even when every write had failed. A
    value skipped as `denied` does NOT count — it was deliberately left alone.
    Any real write/delete failure still raises, as before.
    """
    from app.config_persist import get_tweak_snapshot, clear_tweak_snapshot
    snap = get_tweak_snapshot(task_id)
    if not snap or "specs" not in snap:
        ctx.log("  (no snapshot found — nothing to restore)")
        return 0
    _SENSITIVE = ("\\run", "\\runonce", "\\services", "winlogon",
                  "image file execution options", "appinit_dlls", "userinit",
                  "\\lsa", "\\sam", "credential")

    def _is_persistence_location(path: str) -> bool:
        """True if `path` is somewhere a snapshot must never be replayed into.

        The substring list above blocks "\\services" outright, because
        Services\\<name>\\Start and Services\\<name>\\ImagePath are genuine
        persistence: restoring a value there can make Windows launch something.

        But that substring also matches Services\\<name>\\Parameters\\..., which
        is where a service's ordinary CONFIGURATION lives. disable_nagle writes
        TcpAckFrequency / TCPNoDelay under
        SYSTEM\\CurrentControlSet\\Services\\Tcpip\\Parameters\\Interfaces\\<guid>
        - squarely a Parameters subtree, never a startup mechanism. Blocking it
        meant Undo for that tweak ALWAYS raised
        "Refusing to restore ... persistence location", i.e. the fix for
        MED-007 shipped a tweak whose Undo could not run at all.

        So the rule is narrowed to what is actually dangerous: a path directly
        under a service key (\\services\\<name>\\<value>) rather than inside its
        Parameters subtree. This does NOT weaken the guard for Start /
        ImagePath, which sit exactly one level under Services\\<name> and are
        still blocked. The full per-task exact-match table the docstring
        promises remains the right follow-up; this only removes a false
        positive that broke a legitimate restore.
        """
        low = "\\" + path.lower().strip("\\") + "\\"
        if "\\services\\" not in low:
            return False
        # Exempt only paths strictly INSIDE a service's Parameters subtree
        # (Services\<svc>\Parameters\<something>\...), never the Parameters key
        # itself. H-003: the old `not endswith("\\parameters\\")` check also
        # exempted direct load mechanisms such as ServiceDll, contradicting
        # this comment and the intent. Block those explicitly; allow the known
        # safe subtrees (Tcpip\...\Interfaces, Tcpip6\...\DisabledComponents).
        idx = low.find("\\parameters\\")
        if idx >= 0:
            inside = low[idx + len("\\parameters\\"):]
            segs = [s for s in inside.split("\\") if s]
            if any(s in ("servicedll", "servicemain", "libraryreadonly")
                   for s in segs):
                return True
            if not low.endswith("\\parameters\\"):
                return False
        return True

    for i, (hive, path, name) in enumerate(snap["specs"]):
        # B-004: an EMPTY value name is legal — it is a key's default value
        # (e.g. the CLSID InprocServer32 default snapshotted by the classic
        # right-click-menu tweak). Rejecting it made that tweak's Undo raise
        # on every attempt and leave a poison snapshot behind forever.
        if hive not in ("HKCU", "HKLM", "HKCR") or not isinstance(path, str) or not path \
                or not isinstance(name, str) or "\x00" in path or "\x00" in name:
            raise RuntimeError(f"Refusing to restore {task_id}: snapshot spec #{i} is malformed (config tampering?).")
        if not _spec_allowed_for_task(task_id, hive, path, name):
            raise RuntimeError(
                f"Refusing to restore {task_id}: the saved snapshot names "
                f"{hive}\\{path}\\{name}, which this tweak is not defined to "
                f"write. Treat the config as tampered.")
        if _is_persistence_location(path) or any(
                s in f"\\{path}\\".lower() + name.lower()
                for s in _SENSITIVE if s != "\\services"):
            raise RuntimeError(f"Refusing to restore {task_id}: snapshot target is a persistence location (config tampering?).")
        vtype = snap.get(f"{i}:type", value_type)
        if vtype not in ("REG_DWORD", "REG_SZ", "REG_QWORD", "REG_BINARY", "REG_MULTI_SZ", "REG_EXPAND_SZ"):
            raise RuntimeError(f"Refusing to restore {task_id}: bad value type (config tampering?).")
    restored = 0
    for i, (hive, path, name) in enumerate(snap["specs"]):
        if snap.get(f"{i}:denied"):
            # Prior existed but was unreadable — leave the current value in
            # place and say so (deleting it would destroy user data we never
            # saw; writing a guessed default would do the same). Not counted:
            # this value was NOT restored.
            ctx.log(f"  (kept {hive}\\{path}\\{name}: prior value was unreadable, leaving current)")
            continue
        if snap.get(f"{i}:present"):
            vtype = snap.get(f"{i}:type", value_type)
            _val = snap.get(f"{i}:value")
            # hex-encoded REG_BINARY priors (see _snap_reg_values)
            if isinstance(_val, dict) and isinstance(_val.get("__bytes_hex__"), str):
                try:
                    _val = bytes.fromhex(_val["__bytes_hex__"])
                except Exception:
                    raise RuntimeError(f"Refusing to restore {task_id}: corrupt snapshot value (config tampering?).")
            reg_set_value_checked(ctx, hive, path, name, _val, value_type=vtype)
            restored += 1
        else:
            if not reg_delete_value(ctx, hive, path, name):
                raise RuntimeError(f"Could not remove {hive}\\{path}\\{name} during revert.")
            restored += 1
    clear_tweak_snapshot(task_id)
    return restored




# --------------------------------------------------------------------------- #
# MED-007: snapshot-based apply/revert for the single-purpose registry tweaks
# --------------------------------------------------------------------------- #
# Fourteen of these tweaks used to Undo with a blind `reg_delete_value`, or by
# writing a hardcoded "Windows default" that was not the user's value. Both
# destroy a pre-existing different setting: a user who had already turned
# something off loses that choice, and one who had it on gets it silently
# changed. The class also falsifies the README promise that every tweak's Undo
# is a full, verified revert.
#
# These two helpers make the correct shape the easy one, so the remaining
# tweaks no longer have an excuse.


def _regn_apply(ctx: TaskContext, task_id: str, writes: "list[tuple]",
                error: str = "") -> None:
    """Snapshot every registry value in `writes`, then write them.

    `writes` is a list of (hive, path, name, value) or
    (hive, path, name, value, value_type).

    The snapshot is taken BEFORE the first write, and dropped again if any
    write fails - unless one already existed, in which case the older (true
    pre-tweak) record is kept. That is the BUG-015 ordering fix applied
    uniformly, so a failed apply can never leave an Undo record describing a
    change that never happened.
    """
    specs = [(w[0], w[1], w[2]) for w in writes]
    had_snapshot = bool(get_tweak_snapshot(task_id))
    _snap_reg_values(ctx, task_id, specs)
    try:
        for w in writes:
            vtype = w[4] if len(w) > 4 else "REG_DWORD"
            if not reg_set_value(ctx, w[0], w[1], w[2], w[3], value_type=vtype):
                raise RuntimeError(
                    error or "Could not write %s\\%s\\%s." % (w[0], w[1], w[2]))
    except Exception:
        if not had_snapshot:
            clear_tweak_snapshot(task_id)
        raise




def _regn_revert(ctx: TaskContext, task_id: str, label: str) -> int:
    """Undo a _regn_apply tweak by restoring what was actually there.

    Never blind-deletes and never substitutes a guessed default: a value that
    was absent before comes back absent, and a value the user had set comes
    back with the user's value. Raises when nothing could be restored, so a
    failed Undo is reported as the failure it is rather than logged as
    success.
    """
    had_snapshot = bool(get_tweak_snapshot(task_id))
    restored = _restore_reg_values(ctx, task_id)
    if restored <= 0:
        raise RuntimeError(
            "%s Undo could not restore anything%s. Nothing was changed."
            % (label,
               "" if had_snapshot else
               " - no snapshot was recorded, so the prior value is unknown and"
               " guessing would overwrite a setting you may have had")
        )
    ctx.log("%s reverted to its previous setting." % label)
    return restored




_ADV = "Software\\Microsoft\\Windows\\CurrentVersion\\Explorer\\Advanced"




_SENSE = ("HKCU",
          "Software\\Microsoft\\Windows\\CurrentVersion\\StorageSense"
          "\\Parameters\\StoragePolicy")




_DESKTOP = "Control Panel\\Desktop"




_TaskbarDevSettings = "Software\\Microsoft\\Windows\\CurrentVersion\\Explorer\\Advanced\\TaskbarDeveloperSettings"




_MIC_CONSENT = "Software\\Microsoft\\Windows\\CurrentVersion\\CapabilityAccessManager\\ConsentStore\\microphone"




_CDM_POL = "Software\\Microsoft\\Windows\\CurrentVersion\\ContentDeliveryManager"




_SEARCH_SETTINGS = "Software\\Microsoft\\Windows\\CurrentVersion\\SearchSettings"




_LINK_PATH = "Software\\Microsoft\\Windows\\CurrentVersion\\Explorer"


_SHELL_ICONS = "SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\Explorer\\Shell Icons"




_IPV4_PATH = "SYSTEM\\CurrentControlSet\\Services\\Tcpip6\\Parameters"


_IPV4_SPECS = [("HKLM", _IPV4_PATH, "DisabledComponents", "REG_DWORD")]




def _reserved_storage_enabled(ctx: TaskContext):
    """True/False if DISM reports the reserved-storage state, else None.

    The state word in DISM output is localized, so this matches on the
    enabled/disabled difference that survives most localizations and returns
    None when it cannot tell — the caller then keeps the documented default
    rather than guessing.
    """
    try:
        import subprocess as _sp
        out = _sp.run(["dism", "/Online", "/Get-ReservedStorageState"],
                      capture_output=True, text=True, timeout=300,
                      creationflags=getattr(_sp, "CREATE_NO_WINDOW", 0))
    except Exception:
        return None
    text = ((out.stdout or "") + "\n" + (out.stderr or "")).lower()
    if "disab" in text:
        return False
    if "enab" in text:
        return True
    return None




# --------------------------------------------------------------------------- #
# Tweak Health verify functions (user-approved feature 2, 2026-09)
#
# READ-ONLY drift detectors: each returns True (tweak still applied), False
# (Windows Update / a driver / the user reset it), or None (cannot tell).
# They read EXACTLY the values the matching apply_* function writes — no
# new registry knowledge, just the existing write specs read back. A tweak
# with no verify function simply shows 'applied' in the Health view (the
# config registry's word is trusted); False only ever comes from a
# definitive read of a tweaked value that is no longer the tweaked value.
# __verify_read is a no-log reader: the health check runs outside a run
# and must not pollute the run log.
# --------------------------------------------------------------------------- #

def __verify_read(hive: str, path: str, name: str):
    """reg_get_value without a TaskContext — silent, read-only, no log."""
    try:
        from app.utils import reg_get_value as _rgv
        class _NoCtx:
            @staticmethod
            def log(_m):
                pass
            @staticmethod
            def set_status(_m):
                pass
            @staticmethod
            def cancelled():
                return False
        return _rgv(_NoCtx, hive, path, name)  # type: ignore[arg-type]
    except Exception:
        return None




def _verify_all_values(specs_and_expected) -> "bool | None":
    """True when EVERY (hive, path, name, expected) tuple reads the
    expected value; None when the FIRST readable value doesn't match (or
    nothing could be read). Missing keys count as drifted: apply's writes
    make the values exist, so an absent value after an apply means it was
    removed. But if NOTHING is readable at all we return None — the honest
    'cannot tell' — rather than guessing False from a dead registry read."""
    saw_any = False
    for hive, path, name, expected in specs_and_expected:
        v = __verify_read(hive, path, name)
        if v is None:
            continue
        saw_any = True
        if v != expected and str(v) != str(expected):
            return False
    return True if saw_any else None




def _verify_value(hive: str, path: str, name: str, expected) -> "bool | None":
    return _verify_all_values([(hive, path, name, expected)])





# --- Round 8: Cortana, transparency, lock-screen tips ---------------------- #
# All three are policy/appearance switches with a real prior state, so all
# three snapshot -> revert -> verify. Cortana is the substantive one (it is
# a background process that listens for a wake word and uploads voice data);
# the other two are cosmetic.


#: Windows Search policy key. AllowCortana is the "Cortana may run at all"
#: switch; CortanaConsent is the older per-user consent value. Both are set
#: because sources disagree on which one a given build honours - setting both
#: is what makes this hold across 22H2 through 25H2.
_CORTANA_KEY = r"SOFTWARE\Policies\Microsoft\Windows\Windows Search"


_PERSONALIZE = r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize"


_CONTENT_DELIVERY = (r"Software\Microsoft\Windows\CurrentVersion"
                     r"\ContentDeliveryManager")
