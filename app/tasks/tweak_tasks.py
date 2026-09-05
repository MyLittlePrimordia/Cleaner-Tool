"""
Tweak tab — all reversible. Short gamer-friendly labels + tooltip details.
Fixed: Fast Startup uses HiberbootEnabled, not hibernate off; added safe new tweaks.
"""

import subprocess
import time

from app.utils import (
    TaskContext, TaskSkipped, reg_set_value, reg_set_value_checked, reg_delete_value, reg_delete_key, reg_get_value, run_cmd, run_cmd_checked, create_restore_point, IS_WINDOWS,
    resolve_asset_path, powercfg_query_indexes, sc_query_start_type, restart_explorer,
)
from app.config_persist import save_tweak_snapshot, get_tweak_snapshot, clear_tweak_snapshot

if IS_WINDOWS:
    import winreg

ULTIMATE_PERF_GUID = "e9a42b02-d5df-448d-aa00-03f14749eb61"
BALANCED_GUID = "381b4222-f694-41f0-9685-ff5bb260df2e"
CONTEXT_MENU_CLSID = "{86ca1aa0-34aa-4e8b-a509-50c905bae2a2}"

# --------------------------------------------------------------------------- #
# Ad Blocker (hosts file) constants
# --------------------------------------------------------------------------- #
import os

_HOSTS_PATH = os.path.join(
    os.environ.get("WINDIR", "C:\\Windows"), "System32", "drivers", "etc", "hosts"
)
_HOSTS_BACKUP_PATH = _HOSTS_PATH + ".cleanertool_bak"
_ADBLOCK_MARKER_START = "# >>> Cleaner Tool AdBlock Start >>>"
_ADBLOCK_MARKER_END = "# <<< Cleaner Tool AdBlock End <<<"


import functools

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
            ["powershell", "-NoProfile", "-Command", ps_script],
            shell=False, capture_output=True, text=True, timeout=10,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        return result.returncode == 0 and result.stdout.strip().lower() == "true"
    except Exception:
        return False


# --------------------------------------------------------------------------- #
# Snapshot-before-apply helpers for powercfg-based tweaks (fix for M1:
# reverts used to write hardcoded "default" values instead of restoring
# what was actually on the machine before the tweak ran).
# --------------------------------------------------------------------------- #

def _snapshot_powercfg_pairs(ctx: TaskContext, task_id: str, pairs: list[tuple[str, str]]):
    """Read the CURRENT value of each (subgroup, setting) pair and save it
    under task_id, but only if no snapshot is already on file for this task
    — so re-applying a tweak twice in a row doesn't overwrite the true
    original value with the tweak's own output.

    audit fix (M7): the DC (battery) index is snapshotted too, stored under
    '<key>:dc', so a laptop's battery-side value survives a revert instead
    of being reset to whatever the fallback says.

    B6 audit fix: reads both sides in ONE locale-independent query (GUID-
    based, structural parse in utils.powercfg_query_indexes) instead of
    two separate English-label regex parsers (utils.powercfg_query_index +
    a local DC twin) that silently parsed empty on localized Windows."""
    values = {}
    for subgroup, setting in pairs:
        ac_val, dc_val = powercfg_query_indexes(ctx, subgroup, setting)
        if ac_val is not None:
            values[f"{subgroup}:{setting}"] = ac_val
        if dc_val is not None:
            values[f"{subgroup}:{setting}:dc"] = dc_val
    save_tweak_snapshot(task_id, values)


def _restore_powercfg_pairs(ctx: TaskContext, task_id: str, pairs: list[tuple[str, str]], fallback: dict[str, int]):
    """Restore each (subgroup, setting) pair from the saved snapshot. Falls
    back to a documented-correct default only if no snapshot exists (e.g.
    tweak was applied by an older app version, or config was cleared).

    audit fix (M7): the snapshot helper only read the AC index and restore
    only wrote setacvalueindex — on a laptop running on battery, a revert
    restored the wrong side (or nothing). Both sides are now snapshotted
    and restored; powercfg_query_index gains a DC variant."""
    snapshot = get_tweak_snapshot(task_id)
    used_fallback = False
    for subgroup, setting in pairs:
        key = f"{subgroup}:{setting}"
        value = snapshot.get(key)
        if value is None:
            value = fallback.get(key)
            used_fallback = True
        if value is not None:
            run_cmd(ctx, f"powercfg /setacvalueindex scheme_current {subgroup} {setting} {value}")
            # snapshot may also carry the DC ("key:dc") value if the machine
            # was on battery at apply time
            dc_value = snapshot.get(key + ":dc")
            if dc_value is not None:
                run_cmd(ctx, f"powercfg /setdcvalueindex scheme_current {subgroup} {setting} {dc_value}")
    run_cmd(ctx, "powercfg /setactive scheme_current")
    if used_fallback and not snapshot:
        ctx.log("  (no saved prior value found — restored to documented Windows default instead)")
    clear_tweak_snapshot(task_id)


def _active_scheme_guid() -> "str | None":
    """Return the GUID of the currently active power scheme, or None."""
    import subprocess as _sp
    import re as _re
    try:
        out = _sp.check_output("powercfg /getactivescheme", shell=True, text=True,
                               stderr=_sp.STDOUT, timeout=15)
        m = _re.search(r"GUID:\s*([0-9a-fA-F-]{36})", out)
        if m:
            return m.group(1)
    except Exception:
        pass
    return None


_POWER_SCHEMES_KEY = r"SYSTEM\CurrentControlSet\Control\Power\User\PowerSchemes"
# powrprof.dll resource index for the Ultimate Performance display name.
# Language-independent: on non-English Windows the FriendlyName resolves to
# localized text, but the indirect-string resource index stays -19.
_UP_RESOURCE_INDEX = "-19,"


def _find_ultimate_scheme_guid() -> "str | None":
    """Find an existing 'Ultimate Performance' scheme GUID — complete and
    language-safe.

    L7 fix (LTSC/user-reported + 24H2 overlay quirk): the old code parsed
    `powercfg /list` stdout and required the literal English name. Two
    failure modes: (a) non-English Windows localizes the scheme name, so
    the match always failed; (b) on 24H2 with the power-mode OVERLAY
    active, `powercfg /list` hides every scheme except the active one —
    verified on the user's own machine, which showed 1 of 12 schemes.
    Both cases silently fell into the dead template-GUID fallback and
    'succeeded' while doing nothing.

    Fix: enumerate schemes from the registry (always complete, unaffected
    by overlay filtering) and identify Ultimate Performance via the
    powrprof.dll resource index (-19) in the indirect FriendlyName string,
    which is language-independent.
    """
    if not IS_WINDOWS:
        return None
    import winreg as _wr
    matches = []
    try:
        root = _wr.OpenKey(_wr.HKEY_LOCAL_MACHINE, _POWER_SCHEMES_KEY)
    except OSError:
        return None
    i = 0
    while True:
        try:
            sub = _wr.EnumKey(root, i)
            i += 1
        except OSError:
            break
        try:
            k = _wr.OpenKey(root, sub)
            name = _wr.QueryValueEx(k, "FriendlyName")[0]
            _wr.CloseKey(k)
        except OSError:
            continue
        # indirect string form: @%SystemRoot%\system32\powrprof.dll,-19,Ultimate Performance
        if _UP_RESOURCE_INDEX in str(name):
            matches.append(sub)
    _wr.CloseKey(root)
    if not matches:
        return None
    # The registry may contain the CANONICAL TEMPLATE GUID itself (it is
    # registered as a scheme on some builds, observed on the user's LTSC)
    # — but `setactive` REJECTS it ('Attempted to write to unsupported
    # setting', verified rc=1). So prefer any real duplicate; fall back to
    # the template GUID only as a last resort (callers must verify after
    # activating anyway).
    real = [g for g in matches if not _guids_equal(g, ULTIMATE_PERF_GUID)]
    return real[0] if real else matches[0]


def apply_ultimate_performance(ctx: TaskContext):
    """Enable the hidden Ultimate Performance plan — idempotently and
    HONESTLY.

    H5: only duplicates the scheme if none exists yet (no junk schemes).
    L7 (LTSC/user-reported): the old code had a dead fallback — if the
    duplicate/re-scan failed it ran `setactive <TEMPLATE GUID>`, which is
    not an activatable scheme on ANY edition (verified: rc=1 'Attempted to
    write to unsupported setting'), then logged 'not supported on this
    edition' and returned None — which the runner counted as SUCCESS. The
    user (on Win11 IoT LTSC) watched this tweak 'succeed' while doing
    nothing. Fix: no dead fallbacks. Duplicate, find the resulting GUID,
    activate, then VERIFY with `getactivescheme` and raise on failure so
    the runner reports the tweak as failed instead of lying.
    """
    ctx.set_status("Enabling fastest power plan...")

    # Snapshot the user's current scheme so revert restores the REAL prior
    # plan (M1 pattern), not a hardcoded guess like 'Balanced'.
    prior_guid = _active_scheme_guid()
    if prior_guid:
        save_tweak_snapshot("ultimate_performance", {"active_scheme": prior_guid})

    guid = _find_ultimate_scheme_guid()
    if guid is None:
        # No scheme yet -> duplicate the hidden template, then re-detect
        # via the registry (language- and overlay-safe).
        rc = run_cmd(ctx, f"powercfg -duplicatescheme {ULTIMATE_PERF_GUID}")
        guid = _find_ultimate_scheme_guid()
        if rc != 0 or guid is None:
            raise RuntimeError(
                "Could not create the Ultimate Performance power plan on this "
                f"PC (powercfg returned {rc}). The plan may not be supported "
                "by this Windows edition."
            )
    else:
        ctx.log("Ultimate Performance plan already present — reusing it (no duplicate created).")

    rc2 = run_cmd(ctx, f"powercfg -setactive {guid}")
    if rc2 != 0:
        raise RuntimeError(f"powercfg -setactive failed (code {rc2}).")

    # Verify it ACTUALLY took effect — the exact check the user ran by
    # hand when the old silent failure burned them.
    active = _active_scheme_guid()
    if active is None or not _guids_equal(active, guid):
        raise RuntimeError(
            "Power plan did not activate (active scheme is "
            f"{active or 'unknown'}, expected {guid})."
        )
    ctx.log(f"Verified: active power plan is now Ultimate Performance ({guid}).")


def _guids_equal(a: str, b: str) -> bool:
    return a.lower().replace("{", "").replace("}", "") == b.lower().replace("{", "").replace("}", "")


def revert_ultimate_performance(ctx: TaskContext):
    """Restore the power plan that was active BEFORE this tweak ran (from
    the M1 snapshot), falling back to Balanced only if no snapshot exists."""
    snapshot = get_tweak_snapshot("ultimate_performance")
    target = snapshot.get("active_scheme") if snapshot else None
    if target and _guids_equal(target, ULTIMATE_PERF_GUID):
        # snapshot somehow holds the tweak's own output — don't restore
        # Ultimate as if it were the prior state
        target = None
    if target:
        # The snapshot may hold a DUPLICATE of Ultimate (random GUID) that
        # was active before apply — e.g. the user created it manually.
        # Restoring that would silently re-activate the tweak. Only treat
        # the saved scheme as 'prior state' if it is NOT an Ultimate plan.
        up = _find_ultimate_scheme_guid()
        if up and _guids_equal(target, up):
            ctx.log("  (saved prior plan is itself an Ultimate Performance plan — restoring Balanced instead)")
            target = None
    if not target:
        target = BALANCED_GUID
        ctx.log("  (no saved prior plan — restoring Balanced, the Windows default)")
    run_cmd(ctx, f"powercfg -setactive {target}")
    active = _active_scheme_guid()
    if active is None or not _guids_equal(active, target):
        ctx.log(f"  ! could not verify plan switch (active={active}, wanted {target})")
    else:
        ctx.log(f"Restored power plan {target}.")
    clear_tweak_snapshot("ultimate_performance")


def _restart_explorer(ctx: TaskContext) -> bool:
    """Thin alias — the real implementation now lives in app.utils.restart_explorer
    so clean_tasks.py can reuse it too (see C3 fix in clean_tasks.py)."""
    return restart_explorer(ctx)


def apply_classic_context_menu(ctx: TaskContext):
    ok = reg_set_value(ctx, "HKCU", f"Software\\Classes\\CLSID\\{CONTEXT_MENU_CLSID}\\InprocServer32",
                       "", "", value_type="REG_SZ")
    if not ok:
        raise RuntimeError("Could not write the classic-menu registry value.")
    # audit fix: restart_explorer's False return was ignored — the tweak
    # reported success while the taskbar might never have come back.
    if not _restart_explorer(ctx):
        raise RuntimeError(
            "Registry value set, but Explorer did not restart cleanly — "
            "your taskbar may be missing. Press Ctrl+Shift+Esc > File > Run "
            "new task > explorer.exe."
        )


def revert_classic_context_menu(ctx: TaskContext):
    reg_delete_key(ctx, "HKCU", f"Software\\Classes\\CLSID\\{CONTEXT_MENU_CLSID}")
    if not _restart_explorer(ctx):
        ctx.log("  ! Explorer did not restart cleanly — your taskbar may be missing. "
                "Press Ctrl+Shift+Esc > File > Run new task > explorer.exe.")


def apply_disable_game_dvr(ctx: TaskContext):
    reg_set_value_checked(ctx, "HKCU", "System\\GameConfigStore", "GameDVR_Enabled", 0)
    reg_set_value_checked(ctx, "HKCU", "Software\\Microsoft\\Windows\\CurrentVersion\\GameDVR", "AppCaptureEnabled", 0)
    reg_set_value_checked(ctx, "HKLM", "SOFTWARE\\Policies\\Microsoft\\Windows\\GameDVR", "AllowGameDVR", 0)


def revert_disable_game_dvr(ctx: TaskContext):
    reg_set_value_checked(ctx, "HKCU", "System\\GameConfigStore", "GameDVR_Enabled", 1)
    reg_set_value_checked(ctx, "HKCU", "Software\\Microsoft\\Windows\\CurrentVersion\\GameDVR", "AppCaptureEnabled", 1)
    reg_delete_value(ctx, "HKLM", "SOFTWARE\\Policies\\Microsoft\\Windows\\GameDVR", "AllowGameDVR")


def apply_disable_mouse_accel(ctx: TaskContext):
    reg_set_value_checked(ctx, "HKCU", "Control Panel\\Mouse", "MouseSpeed", "0", value_type="REG_SZ")
    reg_set_value_checked(ctx, "HKCU", "Control Panel\\Mouse", "MouseThreshold1", "0", value_type="REG_SZ")
    reg_set_value_checked(ctx, "HKCU", "Control Panel\\Mouse", "MouseThreshold2", "0", value_type="REG_SZ")


def revert_disable_mouse_accel(ctx: TaskContext):
    reg_set_value_checked(ctx, "HKCU", "Control Panel\\Mouse", "MouseSpeed", "1", value_type="REG_SZ")
    reg_set_value_checked(ctx, "HKCU", "Control Panel\\Mouse", "MouseThreshold1", "6", value_type="REG_SZ")
    reg_set_value_checked(ctx, "HKCU", "Control Panel\\Mouse", "MouseThreshold2", "10", value_type="REG_SZ")


def apply_visual_effects_perf(ctx: TaskContext):
    # M1 audit fix (double-owned MenuShowDelay): the menu_delay tweak also
    # writes HKCU\...\Desktop\MenuShowDelay (snapshot + 100ms). This apply
    # used to overwrite it with 0 and its revert hardcode 400 back —
    # reverting one tweak clobbered the other's bookkeeping. Snapshot the
    # prior value under OUR task id before writing (same helpers menu_delay
    # uses); the revert restores exactly what this apply overwrote.
    # Residual order-dependence (documented, not fixable without merging
    # the tweaks): each undo restores what its own apply overwrote, so if
    # both tweaks are applied, undoing them in apply order lands on the
    # true original value — undoing in reverse order lands on the other
    # tweak's output.
    _snap_reg_values(ctx, "visual_effects",
                     [("HKCU", "Control Panel\\Desktop", "MenuShowDelay")])
    reg_set_value_checked(ctx, "HKCU", "Software\\Microsoft\\Windows\\CurrentVersion\\Themes\\Personalize",
                  "EnableTransparency", 0)
    reg_set_value_checked(ctx, "HKCU", "Control Panel\\Desktop\\WindowMetrics", "MinAnimate", "0", value_type="REG_SZ")
    reg_set_value_checked(ctx, "HKCU", "Software\\Microsoft\\Windows\\CurrentVersion\\Explorer\\Advanced",
                  "TaskbarAnimations", 0)
    reg_set_value_checked(ctx, "HKCU", "Control Panel\\Desktop", "MenuShowDelay", "0", value_type="REG_SZ")


def revert_visual_effects_perf(ctx: TaskContext):
    reg_set_value_checked(ctx, "HKCU", "Software\\Microsoft\\Windows\\CurrentVersion\\Themes\\Personalize",
                  "EnableTransparency", 1)
    reg_set_value_checked(ctx, "HKCU", "Control Panel\\Desktop\\WindowMetrics", "MinAnimate", "1", value_type="REG_SZ")
    reg_set_value_checked(ctx, "HKCU", "Software\\Microsoft\\Windows\\CurrentVersion\\Explorer\\Advanced",
                  "TaskbarAnimations", 1)
    # M1 audit fix: restore the snapshotted prior MenuShowDelay (an absent
    # prior value is deleted back to absence) instead of hardcoding 400 —
    # the hardcode clobbered the menu_delay tweak's value and any custom
    # delay the user had set.
    if get_tweak_snapshot("visual_effects").get("specs"):
        _restore_reg_values(ctx, "visual_effects", value_type="REG_SZ")
    else:
        # No snapshot (tweak applied by an older app version, or config
        # cleared): Windows' own default is an ABSENT value — remove ours.
        ctx.log("  (no MenuShowDelay snapshot on file — removing the value; Windows then uses its default)")
        reg_delete_value(ctx, "HKCU", "Control Panel\\Desktop", "MenuShowDelay")


def apply_network_throttling(ctx: TaskContext):
    reg_set_value_checked(ctx, "HKLM",
                  "SOFTWARE\\Microsoft\\Windows NT\\CurrentVersion\\Multimedia\\SystemProfile",
                  "NetworkThrottlingIndex", 0xFFFFFFFF)
    # SystemResponsiveness: 10 = reserve 10% CPU for background (values <10 treated as 20%)
    reg_set_value_checked(ctx, "HKLM",
                  "SOFTWARE\\Microsoft\\Windows NT\\CurrentVersion\\Multimedia\\SystemProfile",
                  "SystemResponsiveness", 10)


def revert_network_throttling(ctx: TaskContext):
    reg_set_value_checked(ctx, "HKLM",
                  "SOFTWARE\\Microsoft\\Windows NT\\CurrentVersion\\Multimedia\\SystemProfile",
                  "NetworkThrottlingIndex", 0xA)
    reg_set_value_checked(ctx, "HKLM",
                  "SOFTWARE\\Microsoft\\Windows NT\\CurrentVersion\\Multimedia\\SystemProfile",
                  "SystemResponsiveness", 20)


def apply_games_priority(ctx: TaskContext):
    """Boost CPU/GPU/IO priority for games via Multimedia System Profile."""
    base = "SOFTWARE\\Microsoft\\Windows NT\\CurrentVersion\\Multimedia\\SystemProfile\\Tasks\\Games"
    reg_set_value_checked(ctx, "HKLM", base, "Scheduling Category", "High", value_type="REG_SZ")
    reg_set_value_checked(ctx, "HKLM", base, "GPU Priority", 8)
    reg_set_value_checked(ctx, "HKLM", base, "Priority", 6)
    reg_set_value_checked(ctx, "HKLM", base, "SFIO Priority", 8)


def revert_games_priority(ctx: TaskContext):
    base = "SOFTWARE\\Microsoft\\Windows NT\\CurrentVersion\\Multimedia\\SystemProfile\\Tasks\\Games"
    reg_set_value_checked(ctx, "HKLM", base, "Scheduling Category", "Medium", value_type="REG_SZ")
    reg_set_value_checked(ctx, "HKLM", base, "GPU Priority", 2)
    reg_set_value_checked(ctx, "HKLM", base, "Priority", 2)
    reg_set_value_checked(ctx, "HKLM", base, "SFIO Priority", 2)


def _open_interfaces_key():
    # Use 64-bit view to match reg_set_value's WOW64 writes
    access = winreg.KEY_READ
    try:
        access |= winreg.KEY_WOW64_64KEY  # type: ignore[attr-defined]
    except AttributeError:
        pass
    return winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, "SYSTEM\\CurrentControlSet\\Services\\Tcpip\\Parameters\\Interfaces", 0, access)


def apply_disable_nagle(ctx: TaskContext):
    base = "SYSTEM\\CurrentControlSet\\Services\\Tcpip\\Parameters\\Interfaces"
    if not IS_WINDOWS:
        return
    try:
        key = _open_interfaces_key()
        try:
            i = 0
            while True:
                try:
                    sub = winreg.EnumKey(key, i)
                except OSError:
                    break
                i += 1
                reg_set_value_checked(ctx, "HKLM", f"{base}\\{sub}", "TcpAckFrequency", 1)
                reg_set_value_checked(ctx, "HKLM", f"{base}\\{sub}", "TCPNoDelay", 1)
        finally:
            winreg.CloseKey(key)
    except FileNotFoundError:
        # B5 audit fix: this used to log and fall off the end of the
        # function — the runner counted it applied although nothing was
        # written. A machine with no Interfaces key has nothing to change.
        raise TaskSkipped("No network interfaces found — nothing to tweak.")


def revert_disable_nagle(ctx: TaskContext):
    base = "SYSTEM\\CurrentControlSet\\Services\\Tcpip\\Parameters\\Interfaces"
    if not IS_WINDOWS:
        return
    try:
        key = _open_interfaces_key()
        try:
            i = 0
            while True:
                try:
                    sub = winreg.EnumKey(key, i)
                except OSError:
                    break
                i += 1
                # Revert to true Windows default: delete the values (absent = delayed ACK)
                # Previously set to 1 which is not the default (default is absent -> 2)
                from app.utils import reg_delete_value
                reg_delete_value(ctx, "HKLM", f"{base}\\{sub}", "TcpAckFrequency")
                reg_delete_value(ctx, "HKLM", f"{base}\\{sub}", "TCPNoDelay")
        finally:
            winreg.CloseKey(key)
    except FileNotFoundError:
        pass


def apply_hags(ctx: TaskContext):
    reg_set_value_checked(ctx, "HKLM", "SYSTEM\\CurrentControlSet\\Control\\GraphicsDrivers", "HwSchMode", 2)
    ctx.log("Reboot required for graphics scheduling to take effect.")


def revert_hags(ctx: TaskContext):
    reg_set_value_checked(ctx, "HKLM", "SYSTEM\\CurrentControlSet\\Control\\GraphicsDrivers", "HwSchMode", 1)
    ctx.log("Reboot required for graphics scheduling to take effect.")


def apply_game_mode(ctx: TaskContext):
    reg_set_value_checked(ctx, "HKCU", "Software\\Microsoft\\GameBar", "AutoGameModeEnabled", 1)
    reg_set_value_checked(ctx, "HKCU", "Software\\Microsoft\\GameBar", "AllowAutoGameMode", 1)


def revert_game_mode(ctx: TaskContext):
    reg_set_value_checked(ctx, "HKCU", "Software\\Microsoft\\GameBar", "AutoGameModeEnabled", 0)
    reg_set_value_checked(ctx, "HKCU", "Software\\Microsoft\\GameBar", "AllowAutoGameMode", 0)


def apply_windowed_optimize(ctx: TaskContext):
    # Optimizations for windowed games — supported Windows 11 setting
    reg_set_value_checked(ctx, "HKCU", "System\\GameConfigStore", "GameDVR_FSEBehaviorMode", 2)
    reg_set_value_checked(ctx, "HKCU", "System\\GameConfigStore", "GameDVR_DXGIHonorFSEWindowsCompatible", 1)
    reg_set_value_checked(ctx, "HKCU", "System\\GameConfigStore", "GameDVR_HonorUserFSEBehaviorMode", 1)


def revert_windowed_optimize(ctx: TaskContext):
    reg_set_value_checked(ctx, "HKCU", "System\\GameConfigStore", "GameDVR_FSEBehaviorMode", 0)
    reg_delete_value(ctx, "HKCU", "System\\GameConfigStore", "GameDVR_DXGIHonorFSEWindowsCompatible")
    reg_delete_value(ctx, "HKCU", "System\\GameConfigStore", "GameDVR_HonorUserFSEBehaviorMode")


def apply_fast_startup_fix(ctx: TaskContext):
    # Correct: disable Fast Startup via HiberbootEnabled, not hibernate off
    reg_set_value_checked(ctx, "HKLM", "SYSTEM\\CurrentControlSet\\Control\\Session Manager\\Power", "HiberbootEnabled", 0)


def revert_fast_startup_fix(ctx: TaskContext):
    reg_set_value_checked(ctx, "HKLM", "SYSTEM\\CurrentControlSet\\Control\\Session Manager\\Power", "HiberbootEnabled", 1)


def apply_limit_telemetry(ctx: TaskContext):
    reg_set_value_checked(ctx, "HKLM", "SOFTWARE\\Policies\\Microsoft\\Windows\\DataCollection", "AllowTelemetry", 1)


def revert_limit_telemetry(ctx: TaskContext):
    reg_delete_value(ctx, "HKLM", "SOFTWARE\\Policies\\Microsoft\\Windows\\DataCollection", "AllowTelemetry")


def apply_priority_separation(ctx: TaskContext):
    # Foreground app gets more CPU — 0x26 = gaming bias
    reg_set_value_checked(ctx, "HKLM", "SYSTEM\\CurrentControlSet\\Control\\PriorityControl", "Win32PrioritySeparation", 38)


def revert_priority_separation(ctx: TaskContext):
    reg_set_value_checked(ctx, "HKLM", "SYSTEM\\CurrentControlSet\\Control\\PriorityControl", "Win32PrioritySeparation", 2)


def apply_power_throttling_off(ctx: TaskContext):
    """Disable CPU power throttling for consistent performance."""
    reg_set_value_checked(ctx, "HKLM", "SYSTEM\\CurrentControlSet\\Control\\Power\\PowerThrottling", "PowerThrottlingOff", 1)


def revert_power_throttling_off(ctx: TaskContext):
    reg_delete_value(ctx, "HKLM", "SYSTEM\\CurrentControlSet\\Control\\Power\\PowerThrottling", "PowerThrottlingOff")


def apply_startup_delay(ctx: TaskContext):
    reg_set_value_checked(ctx, "HKCU", "Software\\Microsoft\\Windows\\CurrentVersion\\Explorer\\Serialize", "StartupDelayInMSec", 0)


def revert_startup_delay(ctx: TaskContext):
    reg_delete_value(ctx, "HKCU", "Software\\Microsoft\\Windows\\CurrentVersion\\Explorer\\Serialize", "StartupDelayInMSec")


def apply_usb_suspend(ctx: TaskContext):
    """Disable USB selective suspend (fixes audio dropouts on USB mics/headsets).

    H4 fix: 'powercfg /change usb-selective-suspend-setting' is NOT a valid
    /change alias (verified: 'Invalid Parameters'). Only the GUID form works,
    and it must be applied to both AC and DC values.

    LTSC/user-reported fix: on some PCs (Win10 IoT LTSC, VMs, stripped
    power plans) this subgroup/setting does not exist — powercfg prints
    'The power scheme, subgroup or setting specified does not exist' and
    exits non-zero. The old code ignored the return code, so the log showed
    the error while the tweak still counted as success. Now: both sides
    failing honestly SKIPS (nothing to change on this PC); one side failing
    logs a warning but still applies the side that worked.
    """
    rc_ac = run_cmd(ctx, "powercfg /setacvalueindex scheme_current 2a737441-1930-4402-8d77-b2bbe5a308a3 48e6b7a6-50f5-4782-a5d4-53bb8fcc84df 0")
    rc_dc = run_cmd(ctx, "powercfg /setdcvalueindex scheme_current 2a737441-1930-4402-8d77-b2bbe5a308a3 48e6b7a6-50f5-4782-a5d4-53bb8fcc84df 0")
    if rc_ac != 0 and rc_dc != 0:
        raise TaskSkipped(
            "USB selective suspend setting not present in this power plan — "
            "skipping (nothing to change on this PC)."
        )
    if rc_ac != 0 or rc_dc != 0:
        ctx.log("  (one power side accepted the change, the other is not present — applied what exists)")
    run_cmd(ctx, "powercfg /setactive scheme_current")


def revert_usb_suspend(ctx: TaskContext):
    rc_ac = run_cmd(ctx, "powercfg /setacvalueindex scheme_current 2a737441-1930-4402-8d77-b2bbe5a308a3 48e6b7a6-50f5-4782-a5d4-53bb8fcc84df 1")
    rc_dc = run_cmd(ctx, "powercfg /setdcvalueindex scheme_current 2a737441-1930-4402-8d77-b2bbe5a308a3 48e6b7a6-50f5-4782-a5d4-53bb8fcc84df 1")
    if rc_ac != 0 and rc_dc != 0:
        ctx.log("  (USB selective suspend setting not present — nothing to restore)")
        return
    run_cmd(ctx, "powercfg /setactive scheme_current")


def apply_disk_timeout(ctx: TaskContext):
    run_cmd(ctx, "powercfg /change disk-timeout-ac 0")
    run_cmd(ctx, "powercfg /change disk-timeout-dc 0")


def revert_disk_timeout(ctx: TaskContext):
    run_cmd(ctx, "powercfg /change disk-timeout-ac 20")
    run_cmd(ctx, "powercfg /change disk-timeout-dc 20")


def apply_keyboard_tuning(ctx: TaskContext):
    reg_set_value_checked(ctx, "HKCU", "Control Panel\\Keyboard", "KeyboardDelay", "0", value_type="REG_SZ")
    reg_set_value_checked(ctx, "HKCU", "Control Panel\\Keyboard", "KeyboardSpeed", "31", value_type="REG_SZ")


def revert_keyboard_tuning(ctx: TaskContext):
    # Windows defaults: KeyboardDelay 1 (250ms), KeyboardSpeed 31 (fastest). Verified via AskVG/TenForums.
    reg_set_value_checked(ctx, "HKCU", "Control Panel\\Keyboard", "KeyboardDelay", "1", value_type="REG_SZ")
    reg_set_value_checked(ctx, "HKCU", "Control Panel\\Keyboard", "KeyboardSpeed", "31", value_type="REG_SZ")


def apply_ssd_trim(ctx: TaskContext):
    """Enable TRIM/DisableDeleteNotify for SSD."""
    # B5 audit fix: these skip paths used to log + `return` — the runner
    # counted them as applied and the GUI badged '✓ Active' although
    # nothing changed. TaskSkipped = completed-with-skip (no badge).
    if not _has_ssd():
        raise TaskSkipped("No SSD detected — skipping TRIM tweak (only applies to SSDs).")
    run_cmd(ctx, "fsutil behavior set disabledeletenotify 0")


def revert_ssd_trim(ctx: TaskContext):
    if not _has_ssd():
        ctx.log("No SSD detected — skipping TRIM revert.")
        return
    # Revert should restore Windows default (TRIM enabled = 0), not disable TRIM (1).
    # Disabling TRIM harms SSD performance/lifespan.
    run_cmd(ctx, "fsutil behavior set disabledeletenotify 0")


def apply_ssd_superfetch(ctx: TaskContext):
    """Disable SysMain (Superfetch) — unnecessary on SSD/NVMe."""
    if not _has_ssd():
        raise TaskSkipped("No SSD detected — skipping SysMain tweak (only applies to SSDs).")
    run_cmd(ctx, "sc config SysMain start= disabled")
    run_cmd(ctx, "net stop SysMain")


def revert_ssd_superfetch(ctx: TaskContext):
    if not _has_ssd():
        ctx.log("No SSD detected — skipping SysMain revert.")
        return
    run_cmd(ctx, "sc config SysMain start= auto")
    run_cmd(ctx, "net start SysMain")


def apply_ssd_last_access(ctx: TaskContext):
    """Disable last access timestamp updates (NtfsDisableLastAccessUpdate)."""
    if not _has_ssd():
        raise TaskSkipped("No SSD detected — skipping last access tweak (only applies to SSDs).")
    reg_set_value_checked(ctx, "HKLM", "SYSTEM\\CurrentControlSet\\Control\\FileSystem",
                  "NtfsDisableLastAccessUpdate", 1)


def revert_ssd_last_access(ctx: TaskContext):
    if not _has_ssd():
        ctx.log("No SSD detected — skipping last access revert.")
        return
    reg_set_value_checked(ctx, "HKLM", "SYSTEM\\CurrentControlSet\\Control\\FileSystem",
                  "NtfsDisableLastAccessUpdate", 0)


def apply_ssd_prefetch(ctx: TaskContext):
    """Disable Prefetcher and Superfetch for SSD."""
    if not _has_ssd():
        raise TaskSkipped("No SSD detected — skipping prefetch tweak (only applies to SSDs).")
    reg_set_value_checked(ctx, "HKLM", "SYSTEM\\CurrentControlSet\\Control\\Session Manager\\Memory Management\\PrefetchParameters",
                  "EnablePrefetcher", 0)
    reg_set_value_checked(ctx, "HKLM", "SYSTEM\\CurrentControlSet\\Control\\Session Manager\\Memory Management\\PrefetchParameters",
                  "EnableSuperfetch", 0)


def revert_ssd_prefetch(ctx: TaskContext):
    reg_set_value_checked(ctx, "HKLM", "SYSTEM\\CurrentControlSet\\Control\\Session Manager\\Memory Management\\PrefetchParameters",
                  "EnablePrefetcher", 3)
    reg_set_value_checked(ctx, "HKLM", "SYSTEM\\CurrentControlSet\\Control\\Session Manager\\Memory Management\\PrefetchParameters",
                  "EnableSuperfetch", 3)


# Disables activity history #
def apply_activity_history_disable(ctx: TaskContext):
    ctx.log("[Tweak] Activity History - Disable")
    base = "SOFTWARE\\Policies\\Microsoft\\Windows\\System"
    for name in ("EnableActivityFeed", "PublishUserActivities", "UploadUserActivities"):
        reg_set_value_checked(ctx, "HKLM", base, name, 0)
        ctx.log(f"  Set {base}\\{name}=0")
    ctx.log("Activity History disabled.")

def revert_activity_history_disable(ctx: TaskContext):
    base = "SOFTWARE\\Policies\\Microsoft\\Windows\\System"
    for name in ("EnableActivityFeed", "PublishUserActivities", "UploadUserActivities"):
        reg_delete_value(ctx, "HKLM", base, name)
    ctx.log("Activity History reverted.")

# Disables consumer features #
def apply_consumer_features_disable(ctx: TaskContext):
    ctx.log("[Tweak] ConsumerFeatures - Disable")
    reg_set_value_checked(ctx, "HKLM", "SOFTWARE\\Policies\\Microsoft\\Windows\\CloudContent", "DisableWindowsConsumerFeatures", 1)
    ctx.log("ConsumerFeatures disabled.")

def revert_consumer_features_disable(ctx: TaskContext):
    reg_delete_value(ctx, "HKLM", "SOFTWARE\\Policies\\Microsoft\\Windows\\CloudContent", "DisableWindowsConsumerFeatures")
    ctx.log("ConsumerFeatures reverted.")

# Disables delivery optimization #
def apply_delivery_optimization_disable(ctx: TaskContext):
    ctx.log("[Tweak] Delivery Optimization - Disable")
    reg_set_value_checked(ctx, "HKLM", "SOFTWARE\\Policies\\Microsoft\\Windows\\DeliveryOptimization", "DODownloadMode", 0)
    ctx.log("DeliveryOptimization disabled.")

def revert_delivery_optimization_disable(ctx: TaskContext):
    reg_delete_value(ctx, "HKLM", "SOFTWARE\\Policies\\Microsoft\\Windows\\DeliveryOptimization", "DODownloadMode")
    ctx.log("DeliveryOptimization reverted.")

# Enables end task on taskbar #
def apply_end_task_on_taskbar(ctx: TaskContext):
    ctx.log("[Tweak] End Task With Right Click - Enable")
    reg_set_value_checked(ctx, "HKCU", "Software\\Microsoft\\Windows\\CurrentVersion\\Explorer\\Advanced\\TaskbarDeveloperSettings", "TaskbarEndTask", 1)
    ctx.log("TaskbarEndTask enabled.")

def revert_end_task_on_taskbar(ctx: TaskContext):
    reg_delete_value(ctx, "HKCU", "Software\\Microsoft\\Windows\\CurrentVersion\\Explorer\\Advanced\\TaskbarDeveloperSettings", "TaskbarEndTask")
    ctx.log("TaskbarEndTask reverted.")

# Disables explorer auto discovery #
def apply_explorer_auto_discovery_disable(ctx: TaskContext):
    ctx.log("[Tweak] File Explorer Automatic Folder Discovery - Disable")
    for sub in (r"Software\Classes\Local Settings\Software\Microsoft\Windows\Shell\Bags",
                r"Software\Classes\Local Settings\Software\Microsoft\Windows\Shell\BagMRU"):
        reg_delete_key(ctx, "HKCU", sub)
        ctx.log(f"  Removed HKCU\\{sub}")
    all_folders = r"Software\Classes\Local Settings\Software\Microsoft\Windows\Shell\Bags\AllFolders\Shell"
    reg_set_value_checked(ctx, "HKCU", all_folders, "FolderType", "NotSpecified", value_type="REG_SZ")
    ctx.log("  Set FolderType=NotSpecified.")
    ctx.log("Please sign out/in or restart to apply.")

def revert_explorer_auto_discovery_disable(ctx: TaskContext):
    for sub in (r"Software\Classes\Local Settings\Software\Microsoft\Windows\Shell\Bags",
                r"Software\Classes\Local Settings\Software\Microsoft\Windows\Shell\BagMRU"):
        reg_delete_key(ctx, "HKCU", sub)
    ctx.log("Explorer AutoDiscovery reverted.")

# Disables background apps #
def apply_background_apps_disable(ctx: TaskContext):
    ctx.log("[Tweak] Disable Background Apps")
    reg_set_value_checked(ctx, "HKCU", "Software\\Microsoft\\Windows\\CurrentVersion\\BackgroundAccessApplications", "GlobalUserDisabled", 1)
    ctx.log("Background apps disabled.")

def revert_background_apps_disable(ctx: TaskContext):
    reg_delete_value(ctx, "HKCU", "Software\\Microsoft\\Windows\\CurrentVersion\\BackgroundAccessApplications", "GlobalUserDisabled")
    ctx.log("Background apps reverted.")

# Sets NVIDIA shader cache to 10GB #
def apply_shader_cache_10gb(ctx: TaskContext):
    ctx.log("[Tweak] Shader Cache Size 10GB")
    # NVIDIA: HKCU\Software\NVIDIA Corporation\Global\FTS
    # F-005: the old try/except-swallow around this write meant a real
    # failure still reported success ('badge lied'). But CreateKeyEx would
    # also CREATE the FTS key (junk) on non-NVIDIA machines. Honest shape:
    # skip with a log line when NVIDIA's key isn't there; raise when a real
    # write to the real key fails.
    from app.utils import reg_get_value
    if reg_get_value(ctx, "HKCU", "Software\\NVIDIA Corporation\\Global\\FTS",
                     "RMShaderCacheSize") is None and \
       reg_get_value(ctx, "HKCU", "Software\\NVIDIA Corporation\\Global\\NvControlPanel2\\Client",
                     "OptInOrOutPreference") is None:
        # B5 audit fix: this skip used to log + `return`, badging the tweak
        # '✓ Active' although nothing changed — TaskSkipped keeps the
        # guard honest (completed-with-skip, no badge).
        raise TaskSkipped("NVIDIA settings key not found — skipping (nothing to change on this GPU).")
    reg_set_value_checked(ctx, "HKCU", "Software\\NVIDIA Corporation\\Global\\FTS", "EnableGR535", 1)
    # B1 fix: RMShaderCacheSize is a REG_DWORD in MB units (the NVCP
    # "Shader Cache Size" selector is MB; driver defaults are 128/1024 MB),
    # NOT bytes. The old value 10737418240 (= 0x280000000) overflows the
    # DWORD type the driver created, so the write silently failed under
    # -ErrorAction SilentlyContinue and the tweak was badged "✓ Active"
    # while doing nothing. 10 GB = 10240 MB, a valid DWORD — and it is
    # written through the checked helper so a failed write RAISES instead
    # of a fake success.
    reg_set_value_checked(ctx, "HKCU", "Software\\NVIDIA Corporation\\Global\\FTS",
                          "RMShaderCacheSize", 10240)  # 10 GB in MB (REG_DWORD)
    ctx.log("Shader cache size set to 10 GB (10240 MB) for NVIDIA.")

def revert_shader_cache_10gb(ctx: TaskContext):
    # M3 fix: revert BOTH values set by apply (previously left EnableGR535=1)
    reg_delete_value(ctx, "HKCU", "Software\\NVIDIA Corporation\\Global\\FTS", "RMShaderCacheSize")
    reg_delete_value(ctx, "HKCU", "Software\\NVIDIA Corporation\\Global\\FTS", "EnableGR535")
    ctx.log("Shader cache size reverted.")

# Sets GPU to prefer max performance #
_NVIDIA_MAX_PERF_PAIRS = [("sub_processor", "PROCTHROTTLEMAX")]
_NVIDIA_MAX_PERF_FALLBACK = {"sub_processor:PROCTHROTTLEMAX": 100}  # Windows' documented default (0x64)

def apply_nvidia_max_performance(ctx: TaskContext):
    ctx.log("[Tweak] Prefer Max Performance")
    # M1 fix: snapshot the real current value before we overwrite it, so
    # Undo restores what was actually there instead of a hardcoded guess.
    _snapshot_powercfg_pairs(ctx, "max_performance_gpu", _NVIDIA_MAX_PERF_PAIRS)
    # Generic via powercfg + NVIDIA PowerMizer
    run_cmd(ctx, "powercfg /setacvalueindex scheme_current sub_processor PROCTHROTTLEMAX 100")
    run_cmd(ctx, "powercfg /setactive scheme_current")
    run_cmd(ctx, 'powershell -NoProfile -Command "if(Test-Path \\"HKCU:\\Software\\NVIDIA Corporation\\Global\\FTS\\"){ Set-ItemProperty -Path \\"HKCU:\\Software\\NVIDIA Corporation\\Global\\FTS\\" -Name \\"PowerMizerEnable\\" -Value 0 -ErrorAction SilentlyContinue}"')
    ctx.log("GPU prefer max performance set.")

def revert_nvidia_max_performance(ctx: TaskContext):
    # M1 fix: restore the snapshotted real prior value (was hardcoded to 90,
    # which is BELOW Windows' real default of 100 — Undo used to leave the
    # CPU throttled more than it was before the tweak ever ran).
    _restore_powercfg_pairs(ctx, "max_performance_gpu", _NVIDIA_MAX_PERF_PAIRS, _NVIDIA_MAX_PERF_FALLBACK)
    ctx.log("GPU performance reverted.")

# Disables fullscreen optimizations #
def apply_fullscreen_optimizations_disable(ctx: TaskContext):
    ctx.log("[Tweak] Fullscreen Optimizations Off")
    reg_set_value_checked(ctx, "HKCU", "Software\\Microsoft\\Windows NT\\CurrentVersion\\AppCompatFlags\\Layers", "DISABLEDXMAXIMIZEDWINDOWEDMODE", 1)
    ctx.log("Fullscreen optimizations disabled.")

def revert_fullscreen_optimizations_disable(ctx: TaskContext):
    reg_delete_value(ctx, "HKCU", "Software\\Microsoft\\Windows NT\\CurrentVersion\\AppCompatFlags\\Layers", "DISABLEDXMAXIMIZEDWINDOWEDMODE")
    ctx.log("Fullscreen optimizations reverted.")


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

def apply_max_cpu_power(ctx: TaskContext):
    """Aggressive CPU boost + no core parking + 100% min processor state (AC).
    Biggest single CPU-latency win for gaming laptops and many desktops."""
    # M1 fix: snapshot real current values before overwriting them.
    _snapshot_powercfg_pairs(ctx, "max_cpu_power", _MAX_CPU_POWER_PAIRS)
    run_cmd(ctx, "powercfg /setacvalueindex scheme_current sub_processor PERFBOOSTMODE 2")   # Aggressive
    run_cmd(ctx, "powercfg /setacvalueindex scheme_current sub_processor CPMINCORES 100")    # no core parking
    run_cmd(ctx, "powercfg /setacvalueindex scheme_current sub_processor CPMAXCORES 100")
    run_cmd(ctx, "powercfg /setacvalueindex scheme_current sub_processor PROCTHROTTLEMIN 100")  # min 100%
    run_cmd(ctx, "powercfg /setactive scheme_current")
    ctx.log("CPU boost set to Aggressive; core parking off; min processor state 100% (AC).")

def revert_max_cpu_power(ctx: TaskContext):
    # M1 fix: restore the machine's real prior values instead of hardcoded
    # ones (PERFBOOSTMODE 0=Disabled and PROCTHROTTLEMIN 5 were not this
    # machine's actual defaults in the audited case — stock was 80).
    _restore_powercfg_pairs(ctx, "max_cpu_power", _MAX_CPU_POWER_PAIRS, _MAX_CPU_POWER_FALLBACK)
    ctx.log("CPU power settings restored to their prior values.")


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


def apply_taskbar_cleanup(ctx: TaskContext):
    """Win11: remove Widgets, Chat/Teams icon, Meet Now, search highlights,
    and OneDrive ads in File Explorer — all background CPU/RAM consumers.

    LTSC/user-reported fix: the old code used reg_set_value_checked for all
    5 values, so ONE denied/missing value (observed: TaskbarDa -> WinError 5
    on Win10 IoT LTSC 2021, where the Win11-only Widgets key doesn't exist)
    failed the WHOLE tweak. Now each value is best-effort: Win11-only keys
    are skipped with a log line on Win10, other failures are logged, and the
    tweak only fails when NOTHING could be applied.
    """
    from app.utils import reg_set_value as _set
    adv = "Software\\Microsoft\\Windows\\CurrentVersion\\Explorer\\Advanced"
    is_win11 = _is_win11_or_newer()
    if not is_win11:
        ctx.log("  (Win10 detected — Widgets/Chat/search-highlights are Win11-only; applying what exists here)")
    # (hive, path, name, value, win11_only)
    writes = [
        ("HKCU", adv, "TaskbarDa", 0, True),        # Widgets
        ("HKCU", adv, "TaskbarMn", 0, True),        # Chat / Teams icon
        ("HKCU", adv, "ShowSyncProviderNotifications", 0, False),  # Explorer ads
        ("HKCU", "Software\\Microsoft\\Windows\\CurrentVersion\\SearchSettings", "IsDynamicSearchBoxEnabled", 0, True),
        ("HKLM", "Software\\Microsoft\\Windows\\CurrentVersion\\Policies\\Explorer", "HideSCAMeetNow", 1, False),
    ]
    ok, skipped = 0, []
    for hive, path, name, value, win11_only in writes:
        if win11_only and not is_win11:
            skipped.append(f"{name} (Win11-only — not present on Win10)")
            ctx.log(f"  (skipped) {hive}\\{path}\\{name}: Win11-only key on Win10 — nothing to change.")
            continue
        if _set(ctx, hive, path, name, value):
            ok += 1
        else:
            skipped.append(f"{name} (blocked — policy or permissions)")
    if ok == 0:
        raise RuntimeError(
            "Could not apply any taskbar setting "
            f"({'; '.join(skipped) or 'all writes blocked'}). "
            "Nothing was marked as applied."
        )
    if skipped:
        ctx.log(f"  (applied {ok} of {len(writes)}; skipped: {'; '.join(skipped)})")
    ctx.log("Widgets, Chat icon, search highlights and Explorer ads disabled. Restart Explorer to see changes.")

def revert_taskbar_cleanup(ctx: TaskContext):
    """Best-effort mirror of apply: Win11-only restores are skipped on Win10,
    failures are logged, and the revert only reports failure when nothing
    could be restored."""
    from app.utils import reg_set_value as _set
    adv = "Software\\Microsoft\\Windows\\CurrentVersion\\Explorer\\Advanced"
    is_win11 = _is_win11_or_newer()
    writes = [
        ("HKCU", adv, "TaskbarDa", 1, True),
        ("HKCU", adv, "TaskbarMn", 1, True),
        ("HKCU", adv, "ShowSyncProviderNotifications", 1, False),
        ("HKCU", "Software\\Microsoft\\Windows\\CurrentVersion\\SearchSettings", "IsDynamicSearchBoxEnabled", 1, True),
    ]
    ok = 0
    for hive, path, name, value, win11_only in writes:
        if win11_only and not is_win11:
            ctx.log(f"  (skipped) {hive}\\{path}\\{name}: Win11-only key on Win10 — nothing to restore.")
            continue
        if _set(ctx, hive, path, name, value):
            ok += 1
    reg_delete_value(ctx, "HKLM", "Software\\Microsoft\\Windows\\CurrentVersion\\Policies\\Explorer", "HideSCAMeetNow")
    if ok == 0 and is_win11:
        ctx.log("  (no taskbar values could be restored — they may already be at defaults)")
    ctx.log("Taskbar items restored. Restart Explorer to see changes.")


def apply_local_search(ctx: TaskContext):
    """Make Start-menu search local-only and instant: no Bing, no web results,
    no cloud content (Sophia Script + privacy.sexy verified values)."""
    reg_set_value_checked(ctx, "HKCU", "Software\\Policies\\Microsoft\\Windows\\Explorer", "DisableSearchBoxSuggestions", 1)
    reg_set_value_checked(ctx, "HKLM", "SOFTWARE\\Policies\\Microsoft\\Windows\\Explorer", "DisableSearchBoxSuggestions", 1)
    reg_set_value_checked(ctx, "HKCU", "Software\\Microsoft\\Windows\\CurrentVersion\\Search", "BingSearchEnabled", 0)
    reg_set_value_checked(ctx, "HKCU", "Software\\Microsoft\\Windows\\CurrentVersion\\Search", "CortanaConsent", 0)
    reg_set_value_checked(ctx, "HKCU", "Software\\Microsoft\\Windows\\CurrentVersion\\SearchSettings", "IsMSACloudSearchEnabled", 0)
    reg_set_value_checked(ctx, "HKCU", "Software\\Microsoft\\Windows\\CurrentVersion\\SearchSettings", "IsAADCloudSearchEnabled", 0)
    ctx.log("Search is now local-only — results appear instantly with no web/Bing content.")

def revert_local_search(ctx: TaskContext):
    reg_delete_value(ctx, "HKCU", "Software\\Policies\\Microsoft\\Windows\\Explorer", "DisableSearchBoxSuggestions")
    reg_delete_value(ctx, "HKLM", "SOFTWARE\\Policies\\Microsoft\\Windows\\Explorer", "DisableSearchBoxSuggestions")
    reg_delete_value(ctx, "HKCU", "Software\\Microsoft\\Windows\\CurrentVersion\\Search", "BingSearchEnabled")
    reg_delete_value(ctx, "HKCU", "Software\\Microsoft\\Windows\\CurrentVersion\\Search", "CortanaConsent")
    reg_delete_value(ctx, "HKCU", "Software\\Microsoft\\Windows\\CurrentVersion\\SearchSettings", "IsMSACloudSearchEnabled")
    reg_delete_value(ctx, "HKCU", "Software\\Microsoft\\Windows\\CurrentVersion\\SearchSettings", "IsAADCloudSearchEnabled")
    ctx.log("Search restored to defaults (Bing + web suggestions back).")


def apply_stop_windows_ads(ctx: TaskContext):
    """The full ContentDeliveryManager sweep — every 'suggested content',
    auto-installed app, lock-screen ad and tip switch in one go."""
    cdm = "Software\\Microsoft\\Windows\\CurrentVersion\\ContentDeliveryManager"
    for name in (
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
    ):
        reg_set_value_checked(ctx, "HKCU", cdm, name, 0)
    reg_set_value_checked(ctx, "HKCU", "Software\\Microsoft\\Windows\\CurrentVersion\\UserProfileEngagement", "ScoobeSystemSettingEnabled", 0)
    reg_set_value_checked(ctx, "HKLM", "SOFTWARE\\Policies\\Microsoft\\Windows\\CloudContent", "DisableSoftLanding", 1)
    reg_set_value_checked(ctx, "HKLM", "SOFTWARE\\Policies\\Microsoft\\Windows\\CloudContent", "DisableCloudOptimizedContent", 1)
    ctx.log("Windows ads, suggestions, auto-installs and lock-screen tips disabled.")

def revert_stop_windows_ads(ctx: TaskContext):
    cdm = "Software\\Microsoft\\Windows\\CurrentVersion\\ContentDeliveryManager"
    for name in (
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
    ):
        reg_set_value_checked(ctx, "HKCU", cdm, name, 1)
    reg_set_value_checked(ctx, "HKCU", "Software\\Microsoft\\Windows\\CurrentVersion\\UserProfileEngagement", "ScoobeSystemSettingEnabled", 1)
    reg_delete_value(ctx, "HKLM", "SOFTWARE\\Policies\\Microsoft\\Windows\\CloudContent", "DisableSoftLanding")
    reg_delete_value(ctx, "HKLM", "SOFTWARE\\Policies\\Microsoft\\Windows\\CloudContent", "DisableCloudOptimizedContent")
    ctx.log("Windows suggestions and tips restored to defaults.")


def apply_privacy_baseline(ctx: TaskContext):
    """One-click privacy baseline: advertising ID, activity feed, app-launch
    tracking, input personalization, online speech, tailored experiences,
    language-list access, feedback prompts. All standard HKCU values that
    Windows itself exposes in Settings — fully reversible."""
    reg_set_value_checked(ctx, "HKCU", "Software\\Microsoft\\Windows\\CurrentVersion\\AdvertisingInfo", "Enabled", 0)
    reg_set_value_checked(ctx, "HKCU", "Software\\Microsoft\\Windows\\CurrentVersion\\Privacy", "TailoredExperiencesWithDiagnosticDataEnabled", 0)
    reg_set_value_checked(ctx, "HKCU", "Software\\Microsoft\\Windows\\CurrentVersion\\Explorer\\Advanced", "Start_TrackProgs", 0)
    reg_set_value_checked(ctx, "HKLM", "SOFTWARE\\Policies\\Microsoft\\Windows\\System", "EnableActivityFeed", 0)
    reg_set_value_checked(ctx, "HKCU", "Software\\Microsoft\\Windows\\CurrentVersion\\CapabilityAccessManager\\ConsentStore\\humaninterfaceenterprise", "Value", "Deny", value_type="REG_SZ")
    reg_set_value_checked(ctx, "HKCU", "Control Panel\\International\\User Profile", "HttpAcceptLanguageOptOut", 1)
    reg_set_value_checked(ctx, "HKCU", "Software\\Microsoft\\Speech_OneCore\\Settings\\OnlineSpeechPrivacy", "HasAccepted", 0)
    reg_set_value_checked(ctx, "HKCU", "Software\\Microsoft\\Siuf\\Rules", "NumberOfSIUFInPeriod", 0)
    reg_set_value_checked(ctx, "HKCU", "Software\\Microsoft\\Input\\TIPC", "Enabled", 0)
    reg_set_value_checked(ctx, "HKLM", "SOFTWARE\\Policies\\Microsoft\\Input\\TIPC", "Enabled", 0)
    reg_set_value_checked(ctx, "HKCU", "Software\\Microsoft\\Personalization\\Settings", "AcceptedPrivacyPolicy", 0)
    reg_set_value_checked(ctx, "HKCU", "Software\\Microsoft\\InputPersonalization\\TrainedDataStore", "HarvestContacts", 0)
    # audit fix (AllowTelemetry coupling): this task used to ALSO write
    # AllowTelemetry=1 here and DELETE it in revert — the exact same value
    # limit_telemetry owns. Reverting Privacy Baseline silently undid
    # Limit Tracking while its badge still read 'applied'. limit_telemetry
    # is now the single owner of that value; this task no longer touches it.
    ctx.log("Privacy baseline applied: ads ID, activity feed, tracking, typing data and speech uploads off.")

def revert_privacy_baseline(ctx: TaskContext):
    reg_delete_value(ctx, "HKCU", "Software\\Microsoft\\Windows\\CurrentVersion\\AdvertisingInfo", "Enabled")
    reg_delete_value(ctx, "HKCU", "Software\\Microsoft\\Windows\\CurrentVersion\\Privacy", "TailoredExperiencesWithDiagnosticDataEnabled")
    reg_delete_value(ctx, "HKCU", "Software\\Microsoft\\Windows\\CurrentVersion\\Explorer\\Advanced", "Start_TrackProgs")
    reg_delete_value(ctx, "HKLM", "SOFTWARE\\Policies\\Microsoft\\Windows\\System", "EnableActivityFeed")
    reg_set_value_checked(ctx, "HKCU", "Software\\Microsoft\\Windows\\CurrentVersion\\CapabilityAccessManager\\ConsentStore\\humaninterfaceenterprise", "Value", "Allow", value_type="REG_SZ")
    reg_delete_value(ctx, "HKCU", "Control Panel\\International\\User Profile", "HttpAcceptLanguageOptOut")
    reg_delete_value(ctx, "HKCU", "Software\\Microsoft\\Speech_OneCore\\Settings\\OnlineSpeechPrivacy", "HasAccepted")
    reg_delete_value(ctx, "HKCU", "Software\\Microsoft\\Siuf\\Rules", "NumberOfSIUFInPeriod")
    reg_delete_value(ctx, "HKCU", "Software\\Microsoft\\Input\\TIPC", "Enabled")
    reg_delete_value(ctx, "HKLM", "SOFTWARE\\Policies\\Microsoft\\Input\\TIPC", "Enabled")
    reg_delete_value(ctx, "HKCU", "Software\\Microsoft\\Personalization\\Settings", "AcceptedPrivacyPolicy")
    reg_delete_value(ctx, "HKCU", "Software\\Microsoft\\InputPersonalization\\TrainedDataStore", "HarvestContacts")
    # audit fix: AllowTelemetry delete removed — limit_telemetry owns that
    # value (see the note in apply_privacy_baseline). Deleting it here broke
    # Limit Tracking for anyone who had both applied.
    ctx.log("Privacy baseline reverted to Windows defaults.")


def apply_stop_telemetry(ctx: TaskContext):
    """Disable telemetry services + all CEIP/App-Experience scheduled tasks.
    Does NOT block network hosts or delete system files (safety: keeps
    Windows Update fully functional). Services are set to 'disabled' and
    can be restored by the revert."""
    run_cmd(ctx, "sc config DiagTrack start= disabled")
    run_cmd(ctx, "net stop DiagTrack", timeout=30)
    run_cmd(ctx, "sc config dmwappushservice start= disabled")
    for task in (
        "\\Microsoft\\Windows\\Application Experience\\Microsoft Compatibility Appraiser",
        "\\Microsoft\\Windows\\Application Experience\\ProgramDataUpdater",
        "\\Microsoft\\Windows\\Application Experience\\StartupAppTask",
        "\\Microsoft\\Windows\\Application Experience\\PcaPatchDbTask",
        "\\Microsoft\\Windows\\Application Experience\\MareBackup",
        "\\Microsoft\\Windows\\Autochk\\Proxy",
        "\\Microsoft\\Windows\\Customer Experience Improvement Program\\Consolidator",
        "\\Microsoft\\Windows\\Customer Experience Improvement Program\\UsbCeip",
        "\\Microsoft\\Windows\\DiskDiagnostic\\Microsoft-Windows-DiskDiagnosticDataCollector",
        "\\Microsoft\\Windows\\Windows Error Reporting\\QueueReporting",
    ):
        run_cmd(ctx, f'schtasks /change /tn "{task}" /disable', timeout=30)
    ctx.log("Telemetry services and diagnostic scheduled tasks disabled.")

def revert_stop_telemetry(ctx: TaskContext):
    run_cmd(ctx, "sc config DiagTrack start= auto")
    run_cmd(ctx, "net start DiagTrack", timeout=30)
    run_cmd(ctx, "sc config dmwappushservice start= demand")
    for task in (
        "\\Microsoft\\Windows\\Application Experience\\Microsoft Compatibility Appraiser",
        "\\Microsoft\\Windows\\Application Experience\\ProgramDataUpdater",
        "\\Microsoft\\Windows\\Application Experience\\StartupAppTask",
        "\\Microsoft\\Windows\\Application Experience\\PcaPatchDbTask",
        "\\Microsoft\\Windows\\Application Experience\\MareBackup",
        "\\Microsoft\\Windows\\Autochk\\Proxy",
        "\\Microsoft\\Windows\\Customer Experience Improvement Program\\Consolidator",
        "\\Microsoft\\Windows\\Customer Experience Improvement Program\\UsbCeip",
        "\\Microsoft\\Windows\\DiskDiagnostic\\Microsoft-Windows-DiskDiagnosticDataCollector",
        "\\Microsoft\\Windows\\Windows Error Reporting\\QueueReporting",
    ):
        run_cmd(ctx, f'schtasks /change /tn "{task}" /enable', timeout=30)
    ctx.log("Telemetry services and tasks re-enabled.")


def apply_nvidia_telemetry_optout(ctx: TaskContext):
    """NVIDIA telemetry opt-out (unique gamer feature from privacy.sexy):
    control-panel preference flags + NvTelemetry service + tasks. Driver
    itself is untouched; no files deleted.

    F-005: the old try/except-swallow meant a genuinely failed write still
    reported success. Honest shape now: skip when no NVIDIA software is
    present (nothing to opt out of — verified via the NvControlPanel key
    OR the NvTelemetryContainer service), raise when a real write to the
    real key fails."""
    from app.utils import reg_get_value
    has_nv_key = reg_get_value(ctx, "HKCU",
                               "Software\\NVIDIA Corporation\\Global\\NvControlPanel2\\Client",
                               "OptInOrOutPreference") is not None \
                 or reg_get_value(ctx, "HKCU",
                                  "Software\\NVIDIA Corporation\\Global\\FTS",
                                  "EnableRID44231") is not None
    has_nv_service = sc_query_start_type(ctx, "NvTelemetryContainer") is not None
    if not has_nv_key and not has_nv_service:
        # B5 audit fix: log + `return` used to badge this '✓ Active' with
        # nothing changed — TaskSkipped = completed-with-skip (no badge).
        raise TaskSkipped("No NVIDIA software detected — skipping telemetry opt-out (nothing to do).")
    if has_nv_key:
        reg_set_value_checked(ctx, "HKCU", "Software\\NVIDIA Corporation\\Global\\NvControlPanel2\\Client", "OptInOrOutPreference", 0)
        reg_set_value_checked(ctx, "HKCU", "Software\\NVIDIA Corporation\\Global\\FTS", "EnableRID44231", 0)
        reg_set_value_checked(ctx, "HKCU", "Software\\NVIDIA Corporation\\Global\\FTS", "EnableRID64640", 0)
        reg_set_value_checked(ctx, "HKCU", "Software\\NVIDIA Corporation\\Global\\FTS", "EnableRID66610", 0)
    else:
        ctx.log("  (preference keys absent — service-side opt-out only)")
    # M6 fix: snapshot the service's real current start type before changing
    # it — previously this set "demand" unconditionally and revert set the
    # exact same value, so the service state was never actually changed by
    # either direction, while claiming to opt the service out.
    if has_nv_service:
        prior_start_type = sc_query_start_type(ctx, "NvTelemetryContainer")
        save_tweak_snapshot("nvidia_telemetry", {"NvTelemetryContainer_start_type": prior_start_type or "demand"})
        run_cmd(ctx, "sc config NvTelemetryContainer start= disabled")
    for task in ("NvTmRep_C", "NvTmRepOnLogon_C", "NvTmMon_C"):
        # Tasks have random GUID suffixes; disable by wildcard via schtasks query+disable
        run_cmd(ctx, f'powershell -NoProfile -Command "Get-ScheduledTask -TaskName \'{task}*\' -ErrorAction SilentlyContinue | Disable-ScheduledTask"', timeout=60)
    ctx.log("NVIDIA telemetry opted out (service disabled, report tasks disabled).")

def revert_nvidia_telemetry_optout(ctx: TaskContext):
    """F-005 mirror: restore only what exists. reg_delete_value already
    treats FileNotFound as success, so absent keys are fine; but a genuine
    failure (ACL/policy) must not be swallowed — the deletes raise through
    reg_delete_value's False return only via log today, so check the
    NvControlPanel key first and let real deletes fail loudly upstream."""
    from app.utils import reg_get_value
    has_nv_key = reg_get_value(ctx, "HKCU",
                               "Software\\NVIDIA Corporation\\Global\\NvControlPanel2\\Client",
                               "OptInOrOutPreference") is not None \
                 or reg_get_value(ctx, "HKCU",
                                  "Software\\NVIDIA Corporation\\Global\\FTS",
                                  "EnableRID44231") is not None
    if has_nv_key:
        reg_delete_value(ctx, "HKCU", "Software\\NVIDIA Corporation\\Global\\NvControlPanel2\\Client", "OptInOrOutPreference")
        reg_delete_value(ctx, "HKCU", "Software\\NVIDIA Corporation\\Global\\FTS", "EnableRID44231")
        reg_delete_value(ctx, "HKCU", "Software\\NVIDIA Corporation\\Global\\FTS", "EnableRID64640")
        reg_delete_value(ctx, "HKCU", "Software\\NVIDIA Corporation\\Global\\FTS", "EnableRID66610")
    # M6 fix: restore the service's real prior start type instead of writing
    # the same "demand" value apply() already wrote.
    snap = get_tweak_snapshot("nvidia_telemetry")
    start_type = snap.get("NvTelemetryContainer_start_type", "demand")
    run_cmd(ctx, f"sc config NvTelemetryContainer start= {start_type}")
    clear_tweak_snapshot("nvidia_telemetry")
    for task in ("NvTmRep_C", "NvTmRepOnLogon_C", "NvTmMon_C"):
        run_cmd(ctx, f'powershell -NoProfile -Command "Get-ScheduledTask -TaskName \'{task}*\' -ErrorAction SilentlyContinue | Enable-ScheduledTask"', timeout=60)
    ctx.log("NVIDIA telemetry settings restored.")


def apply_ad_blocker(ctx: TaskContext):
    """System-wide ad blocker via the hosts file.

    Writes a de-duplicated, merged block of known ad/tracker domains
    (StevenBlack + AdAway lists, cross-referenced) between marker comments,
    redirecting each to 0.0.0.0 so requests fail instead of loading ads.

    Fully reversible: the original hosts file is backed up once (on first
    apply) and the revert function simply strips the marked block back out.
    Takes effect after a DNS flush; a reboot is not required but is a safe
    way to make sure all apps pick it up.
    """
    ctx.set_status("Applying system-wide ad blocker (hosts file)...")

    asset_path = resolve_asset_path("adblock_hosts.txt")
    if not asset_path:
        # audit fix: these were bare `return None` — the runner counted
        # None as SUCCESS and marked the tweak applied, so the badge lied
        # while nothing was blocked. Raise so the runner reports failure.
        raise RuntimeError("Could not find bundled ad-block list (adblock_hosts.txt missing).")

    if not os.path.isfile(_HOSTS_PATH):
        raise RuntimeError(f"Hosts file not found at {_HOSTS_PATH}.")

    try:
        with open(_HOSTS_PATH, "r", encoding="utf-8", errors="ignore") as f:
            current = f.read()
    except Exception as exc:
        raise RuntimeError(f"Could not read hosts file: {exc}")

    # Back up the ORIGINAL hosts file exactly once. If we ever re-apply after
    # already having applied (e.g. to refresh the list), we must not overwrite
    # an existing backup with our own already-modified file.
    if not os.path.isfile(_HOSTS_BACKUP_PATH):
        try:
            with open(_HOSTS_BACKUP_PATH, "w", encoding="utf-8") as f:
                f.write(current)
            ctx.log(f"Backed up original hosts file to {_HOSTS_BACKUP_PATH}")
        except Exception as exc:
            raise RuntimeError(f"Could not back up hosts file, aborting for safety: {exc}")

    # Strip any previous block first (idempotent re-apply / list refresh)
    base_content = _strip_adblock_block(current)

    try:
        with open(asset_path, "r", encoding="utf-8", errors="ignore") as f:
            block_lines = f.read()
    except Exception as exc:
        raise RuntimeError(f"Could not read bundled ad-block list: {exc}")

    domain_count = sum(1 for line in block_lines.splitlines() if line.strip())

    if not base_content.endswith("\n"):
        base_content += "\n"
    new_content = (
        base_content
        + "\n" + _ADBLOCK_MARKER_START + "\n"
        + f"# {domain_count} ad/tracker domains — do not edit this block by hand,\n"
        + "# use the Cleaner Tool app to update or remove it.\n"
        + block_lines
        + _ADBLOCK_MARKER_END + "\n"
    )

    try:
        _write_hosts_file(new_content)
    except Exception as exc:
        raise RuntimeError(f"Could not write hosts file (need admin rights?): {exc}")

    run_cmd(ctx, "ipconfig /flushdns", timeout=30)
    ctx.log(f"Ad blocker applied: {domain_count} domains now blocked system-wide.")


def revert_ad_blocker(ctx: TaskContext):
    """Remove the ad-block block from the hosts file, restoring normal DNS
    resolution for those domains. Does not touch anything else a user may
    have manually added to their hosts file."""
    ctx.set_status("Removing system-wide ad blocker (hosts file)...")

    if not os.path.isfile(_HOSTS_PATH):
        raise RuntimeError(f"Hosts file not found at {_HOSTS_PATH}.")

    try:
        with open(_HOSTS_PATH, "r", encoding="utf-8", errors="ignore") as f:
            current = f.read()
    except Exception as exc:
        raise RuntimeError(f"Could not read hosts file: {exc}")

    if _ADBLOCK_MARKER_START not in current:
        ctx.log("Ad blocker block not found in hosts file (already removed?).")
        return

    new_content = _strip_adblock_block(current)

    try:
        _write_hosts_file(new_content)
    except Exception as exc:
        raise RuntimeError(f"Could not write hosts file (need admin rights?): {exc}")

    run_cmd(ctx, "ipconfig /flushdns", timeout=30)
    ctx.log("Ad blocker removed. Hosts file restored to its previous state.")


def _strip_adblock_block(content: str) -> str:
    """Remove our marker block (and one blank line we add before it) from
    hosts-file content, if present. Leaves everything else untouched."""
    start = content.find(_ADBLOCK_MARKER_START)
    if start == -1:
        return content
    end = content.find(_ADBLOCK_MARKER_END, start)
    if end == -1:
        # Marker start with no matching end (corrupted) — cut from start to EOF
        cleaned = content[:start]
    else:
        end += len(_ADBLOCK_MARKER_END)
        cleaned = content[:start] + content[end:]
    # Trim the blank line we insert right before the start marker
    cleaned = cleaned.rstrip("\n") + "\n"
    return cleaned


def _write_hosts_file(content: str):
    """Write hosts file content atomically — temp file in the SAME directory,
    flush + fsync, then os.replace (the config_persist.py pattern). The old
    truncate-and-write (open(path, "w")) left a corrupt, truncated hosts
    file — breaking ALL DNS resolution — if the machine crashed, lost power,
    or an AV tool locked the file mid-write, with no auto-recovery.

    The read-only attribute is cleared first if set (some AV/security tools
    flip it; os.replace also needs the destination writable) and restored
    afterward on the replaced file."""
    hosts_dir = os.path.dirname(_HOSTS_PATH)
    tmp_path = os.path.join(hosts_dir, "hosts.cleanertool.tmp")
    was_readonly = False
    try:
        import stat
        mode = os.stat(_HOSTS_PATH).st_mode
        if not (mode & stat.S_IWRITE):
            was_readonly = True
            os.chmod(_HOSTS_PATH, mode | stat.S_IWRITE)
    except Exception:
        pass
    try:
        with open(tmp_path, "w", encoding="utf-8", newline="\n") as f:
            f.write(content)
            f.flush()
            try:
                os.fsync(f.fileno())
            except Exception:
                pass
        # Atomic swap: the hosts path only ever sees a COMPLETE file. If this
        # raises (AV lock, ACL), the original hosts file is untouched and the
        # error propagates so the caller reports failure honestly.
        os.replace(tmp_path, _HOSTS_PATH)
    except Exception:
        try:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
        except Exception:
            pass
        raise
    finally:
        if was_readonly:
            try:
                import stat
                os.chmod(_HOSTS_PATH, os.stat(_HOSTS_PATH).st_mode & ~stat.S_IWRITE)
            except Exception:
                pass


# --------------------------------------------------------------------------- #
# Focus / stability tweaks (tasks.txt additions)
# --------------------------------------------------------------------------- #

def apply_disable_sticky_keys(ctx: TaskContext):
    """Stop the 'Do you want to turn on Sticky Keys?' popup and its beep
    from interrupting games when Shift is tapped repeatedly (sprint,
    crouch). Sets the accessibility hotkey Flags to 'hotkey off' states —
    the same values Windows' own Settings panel writes when you untick
    the shortcut checkboxes. Fully reversible."""
    ctx.set_status("Disabling Sticky Keys popups...")
    ok = reg_set_value(ctx, "HKCU", "Control Panel\\Accessibility\\StickyKeys", "Flags", "506", value_type="REG_SZ")
    ok &= reg_set_value(ctx, "HKCU", "Control Panel\\Accessibility\\Keyboard Response", "Flags", "122", value_type="REG_SZ")
    ok &= reg_set_value(ctx, "HKCU", "Control Panel\\Accessibility\\ToggleKeys", "Flags", "58", value_type="REG_SZ")
    if not ok:
        raise RuntimeError("Could not write accessibility Flags values.")
    ctx.log("Sticky Keys / Filter Keys / Toggle Keys popups disabled.")


def revert_disable_sticky_keys(ctx: TaskContext):
    """Restore Windows default accessibility hotkey flags (popups back on)."""
    ctx.set_status("Restoring accessibility popups...")
    reg_set_value_checked(ctx, "HKCU", "Control Panel\\Accessibility\\StickyKeys", "Flags", "510", value_type="REG_SZ")
    reg_set_value_checked(ctx, "HKCU", "Control Panel\\Accessibility\\Keyboard Response", "Flags", "126", value_type="REG_SZ")
    reg_set_value_checked(ctx, "HKCU", "Control Panel\\Accessibility\\ToggleKeys", "Flags", "62", value_type="REG_SZ")
    ctx.log("Accessibility popups restored to Windows defaults.")


def apply_suppress_crash_popups(ctx: TaskContext):
    """Stop Windows Error Reporting dialogs from stealing focus / pausing
    the game window when a background app (Discord, an updater) crashes.
    The crash still logs to the hidden log — only the popup is suppressed.
    Note: this hides the dialog for apps Windows would normally show it
    for; fully reversible."""
    ctx.set_status("Suppressing crash popups...")
    ok = reg_set_value(ctx, "HKCU", "Software\\Microsoft\\Windows\\Windows Error Reporting", "DontShowUI", 1)
    if not ok:
        raise RuntimeError("Could not write DontShowUI.")
    ctx.log("Crash popup dialogs suppressed (errors still logged).")


def revert_suppress_crash_popups(ctx: TaskContext):
    reg_delete_value(ctx, "HKCU", "Software\\Microsoft\\Windows\\Windows Error Reporting", "DontShowUI")
    ctx.log("Crash popups restored.")


def apply_no_update_reboot(ctx: TaskContext):
    """Stop Windows Update from force-rebooting while a user is logged on
    (the classic 'update restarted my PC mid-match' fix). Updates still
    download and install — only the forced auto-restart is deferred until
    the user reboots or logs off."""
    ctx.set_status("Blocking forced update reboots...")
    ok = reg_set_value(ctx, "HKLM", "SOFTWARE\\Policies\\Microsoft\\Windows\\WindowsUpdate\\AU",
                       "NoAutoRebootWithLoggedOnUsers", 1)
    if not ok:
        raise RuntimeError("Could not write WindowsUpdate policy (admin needed).")
    ctx.log("Windows Update will no longer force-reboot while you're logged on.")


def revert_no_update_reboot(ctx: TaskContext):
    reg_delete_value(ctx, "HKLM", "SOFTWARE\\Policies\\Microsoft\\Windows\\WindowsUpdate\\AU",
                     "NoAutoRebootWithLoggedOnUsers")
    ctx.log("Windows Update auto-reboot policy restored.")


def apply_mpo_fix(ctx: TaskContext):
    """Disable Multi-Plane Overlay (MPO) — the DWM display-pipeline feature
    behind many multi-monitor black-screen flashes, Discord screen-share
    flicker, and micro-stutter reports on RTX 30/40 and RX 6000/7000 cards
    (NVIDIA has published this same registry value as its official fix).
    Reboot required. Fully reversible."""
    ctx.set_status("Disabling Multi-Plane Overlay...")
    ok = reg_set_value(ctx, "HKLM", "SOFTWARE\\Microsoft\\Windows\\Dwm", "OverlayTestMode", 5)
    if not ok:
        raise RuntimeError("Could not write OverlayTestMode (admin needed).")
    ctx.log("MPO disabled — fixes multi-monitor flicker & stutter. Reboot required.")


def revert_mpo_fix(ctx: TaskContext):
    reg_delete_value(ctx, "HKLM", "SOFTWARE\\Microsoft\\Windows\\Dwm", "OverlayTestMode")
    ctx.log("MPO restored to Windows default. Reboot required.")


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
        out = _sp.run(["powershell", "-NoProfile", "-Command", ps], shell=False,
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


def apply_eee_disable(ctx: TaskContext):
    """Disable Energy Efficient Ethernet / Green Ethernet on active NICs —
    the documented cause of micro-disconnects and speed drops on Realtek
    NICs. Snapshots each adapter's exact prior display value first (valid
    values are driver-defined: Realtek says Disabled/Enabled, others say
    Off/On — a hardcoded 'Enabled' revert would be wrong on some NICs)."""
    ctx.set_status("Disabling Energy Efficient Ethernet...")
    props = _query_eee_props()
    if not props:
        raise RuntimeError("No Energy Efficient Ethernet-capable adapter found.")
    snapshot = {"adapters": {}}                    # {adapter: {display: prior_value}}
    for adapter, display, value in props:
        snapshot["adapters"].setdefault(adapter, {})[display] = value
    save_tweak_snapshot("eee_disable", snapshot)
    changed = 0
    for adapter, display, _value in props:
        rc = run_cmd(
            ctx,
            f'powershell -NoProfile -Command "Set-NetAdapterAdvancedProperty -Name \'{adapter}\' '
            f'-DisplayName \'{display}\' -DisplayValue \'Disabled\'"',
            timeout=60)
        if rc == 0:
            changed += 1
        else:
            ctx.log(f"  ! {adapter}/{display}: could not set (code {rc}) — this NIC may use different values.")
    if not changed:
        # honest failure: nothing accepted the change — never claim success
        raise RuntimeError("No adapter accepted the change (NICs vary — check your adapter's own advanced tab).")
    ctx.log(f"EEE disabled on {changed} propert(ies). Brief network blip is normal; reconnect if you were in a game.")


def revert_eee_disable(ctx: TaskContext):
    """Restore each adapter's exact prior display values from the snapshot
    (driver-defined strings, per the M1/M6 lesson). Adapters that vanished
    between apply and revert (USB NICs) just fail their Set- call and log —
    best-effort, mirror of revert_gaming_dns."""
    ctx.set_status("Re-enabling Energy Efficient Ethernet...")
    snap = get_tweak_snapshot("eee_disable")
    adapters = snap.get("adapters", {}) if snap else {}
    if not adapters:
        raise RuntimeError("No snapshot on file — cannot know the prior values. Set them manually in Device Manager.")
    for adapter, props in adapters.items():
        for display, value in props.items():
            run_cmd(ctx,
                    f'powershell -NoProfile -Command "Set-NetAdapterAdvancedProperty -Name \'{adapter}\' '
                    f'-DisplayName \'{display}\' -DisplayValue \'{value}\'"', timeout=60)
    clear_tweak_snapshot("eee_disable")
    ctx.log("Energy Efficient Ethernet restored to previous values.")


def apply_ntfs_8dot3_disable(ctx: TaskContext):
    """Disable NTFS 8.3 short-name creation — speeds up folders with many
    files. Snapshot-first: the stock value is 2 (per-volume, system volume
    enabled) on modern Windows, so the honest revert is 'restore what
    fsutil reported', never a hardcoded guess (the source list's
    'revert to 0' was backwards: 1 disables, 0 enables)."""
    import subprocess as _sp
    ctx.set_status("Disabling 8.3 short-name creation...")
    try:
        out = _sp.run(["fsutil", "behavior", "query", "disable8dot3"], shell=False,
                      capture_output=True, text=True, timeout=15,
                      creationflags=getattr(_sp, "CREATE_NO_WINDOW", 0))
    except Exception as exc:
        raise RuntimeError(f"Could not query the current 8.3 setting: {exc}")
    # B6 audit fix: the numeric state (0/1/2) is locale-free, but this
    # used to run on unchecked output — a localized error/odd text meant
    # NO snapshot was saved and the revert later restored a hardcoded 2
    # guess. Parse the number explicitly and fail loudly (before any
    # change) when the output has no number to snapshot.
    prior_value = None
    if out.returncode == 0:
        for token in (out.stdout or "").split():
            if token.isdigit():
                prior_value = int(token)
                break
    if prior_value is None:
        ctx.log(f"  ! could not read the current 8.3 name setting from fsutil "
                f"(rc={out.returncode}: {(out.stdout or out.stderr or '').strip()[:120]}) — "
                "aborting so a revert never has to guess.")
        raise RuntimeError("Could not read the current 8.3 name setting — nothing was changed.")
    save_tweak_snapshot("ntfs_8dot3", {"value": prior_value})
    run_cmd_checked(ctx, "fsutil behavior set disable8dot3 1")
    ctx.log("8.3 short-name creation disabled (speeds up folders with many files).")


def revert_ntfs_8dot3_disable(ctx: TaskContext):
    snap = get_tweak_snapshot("ntfs_8dot3")
    run_cmd_checked(ctx, f"fsutil behavior set disable8dot3 {snap.get('value', 2) if snap else 2}")
    clear_tweak_snapshot("ntfs_8dot3")
    ctx.log("8.3 name creation restored to previous setting.")


def apply_disable_dynamic_tick(ctx: TaskContext):
    """Disable the dynamic timer tick (bcdedit) — a legacy latency tweak.
    Gains are debatable on modern hardware; kept opt-in for users who
    benchmark. Reboot required."""
    run_cmd_checked(ctx, "bcdedit /set disabledynamictick yes")
    ctx.log("Dynamic tick disabled. Reboot required.")


def revert_disable_dynamic_tick(ctx: TaskContext):
    # /deletevalue exits 1 when the value doesn't exist (never applied) —
    # that's a no-op, not a failure. Same honesty pattern as
    # revert_limit_telemetry's delete-when-absent cases.
    rc = run_cmd(ctx, "bcdedit /deletevalue disabledynamictick")
    if rc != 0:
        ctx.log("  (value not present — nothing to remove)")
    ctx.log("Dynamic tick restored to Windows default. Reboot required.")

# Each active network adapter's DNS is snapshotted (by adapter GUID+index)
# before the switch, so revert puts back exactly what was there — including
# "no static DNS" (DHCP), which we record as None and restore by clearing.
_DNS_CLOUDFLARE = ["1.1.1.1", "1.0.0.1"]


def _dns_snapshot_key():
    return {"schema": 1, "adapters": {}}


def apply_gaming_dns(ctx: TaskContext):
    """Switch every active adapter's DNS to Cloudflare (1.1.1.1 / 1.0.0.1),
    snapshotting the prior static servers first so revert is exact.
    1.1.1.1 is consistently among the fastest public resolvers for game
    server lookups; fully reversible, no reset needed."""
    ctx.set_status("Switching DNS to Cloudflare 1.1.1.1...")
    if not IS_WINDOWS:
        raise RuntimeError("Windows only.")
    import subprocess as _sp
    try:
        out = _sp.run(
            ["powershell", "-NoProfile", "-Command",
             "Get-NetAdapter | Where-Object Status -eq 'Up' | "
             "ForEach-Object { $d = Get-DnsClientServerAddress -InterfaceIndex $_.ifIndex -AddressFamily IPv4 -ErrorAction SilentlyContinue; "
             "'ADAPT|' + $_.ifIndex + '|' + ($d.ServerAddresses -join ',') }"],
            capture_output=True, text=True, timeout=30,
            creationflags=getattr(_sp, "CREATE_NO_WINDOW", 0),
        )
    except Exception as exc:
        raise RuntimeError(f"Could not list network adapters: {exc}")
    snapshot = _dns_snapshot_key()
    switched = 0
    for line in (out.stdout or "").splitlines():
        if not line.startswith("ADAPT|"):
            continue
        _, idx, servers = line.split("|", 2)
        idx = idx.strip()
        if not idx.isdigit():
            continue
        snapshot["adapters"][idx] = servers.strip() or None   # None = DHCP
        rc = run_cmd(ctx, f'netsh interface ip set dns name="{idx}" static 1.1.1.1 primary', timeout=30)
        # netsh needs the adapter NAME for the secondary; get it via the index
        # in one pass: set primary worked above, secondary via PowerShell.
        run_cmd(ctx, f'netsh interface ip add dns name="{idx}" 1.0.0.1 index=2', timeout=30)
        if rc == 0:
            switched += 1
    if not snapshot["adapters"]:
        raise RuntimeError("No active network adapters found — nothing to switch.")
    save_tweak_snapshot("gaming_dns", snapshot)
    ctx.log(f"DNS switched to Cloudflare on {switched} adapter(s). (Snapshot saved for undo.)")


def revert_gaming_dns(ctx: TaskContext):
    """Restore each adapter's exact prior DNS — including DHCP (no static
    servers), which is recorded as None in the snapshot. Only ever touches
    adapters present in the snapshot: with no snapshot there is nothing
    this app configured, so it refuses to rewrite the network config
    (a blanket DHCP reset would destroy static DNS the app never set)."""
    ctx.set_status("Restoring previous DNS settings...")
    if not IS_WINDOWS:
        raise RuntimeError("Windows only.")
    snap = get_tweak_snapshot("gaming_dns")
    adapters = snap.get("adapters", {}) if snap else {}
    if not adapters:
        # B3 fix (eee_disable contract): this used to fall back to a
        # "best-effort" Set-DnsClientServerAddress -ResetServerAddresses
        # over EVERY up adapter, wiping pre-existing static DNS this app
        # never configured. No snapshot -> raise before running anything.
        raise RuntimeError(
            "No DNS snapshot on file — cannot know the prior DNS servers "
            "to restore. Restore your DNS manually: Settings > Network & "
            "Internet > your adapter > DNS (or ask your network "
            "administrator / ISP for the correct servers)."
        )
    for idx, servers in adapters.items():
        if servers:  # had static servers before
            first, *rest = [s for s in servers.split(",") if s.strip()]
            run_cmd(ctx, f'netsh interface ip set dns name="{idx}" static {first.strip()} primary', timeout=30)
            for i, s in enumerate(rest, start=2):
                run_cmd(ctx, f'netsh interface ip add dns name="{idx}" {s.strip()} index={i}', timeout=30)
        else:        # was DHCP — clear static servers back to automatic
            run_cmd(ctx, f'netsh interface ip set dns name="{idx}" dhcp', timeout=30)
    clear_tweak_snapshot("gaming_dns")
    ctx.log("DNS restored to previous settings.")


def _query_video_mode_list():
    """Return (current, maximum) (width, height, hz, bits) tuples via
    EnumDisplaySettings, or (None, None) if anything fails."""
    if not IS_WINDOWS:
        return None, None
    import ctypes
    from ctypes import wintypes
    try:
        class DEVMODE(ctypes.Structure):
            _fields_ = [
                ("dmDeviceName", wintypes.WCHAR * 32),
                ("dmSpecVersion", wintypes.WORD), ("dmDriverVersion", wintypes.WORD),
                ("dmSize", wintypes.WORD), ("dmDriverExtra", wintypes.WORD),
                ("dmFields", wintypes.DWORD),
                ("dmPositionX", ctypes.c_long), ("dmPositionY", ctypes.c_long),
                ("dmPelsWidth", wintypes.DWORD), ("dmPelsHeight", wintypes.DWORD),
                ("dmBitsPerPel", wintypes.DWORD), ("dmDisplayFlags", wintypes.DWORD),
                ("dmDisplayFrequency", wintypes.DWORD),
                ("dmICMMethod", wintypes.DWORD), ("dmICMIntent", wintypes.DWORD),
                ("dmMediaType", wintypes.DWORD), ("dmDitherType", wintypes.DWORD),
                ("dmReserved1", wintypes.DWORD), ("dmReserved2", wintypes.DWORD),
                ("dmPanningWidth", wintypes.DWORD), ("dmPanningHeight", wintypes.DWORD),
            ]

        user32 = ctypes.windll.user32
        dm = DEVMODE()
        dm.dmSize = ctypes.sizeof(DEVMODE)
        if not user32.EnumDisplaySettingsW(None, ctypes.c_ulong(0xFFFFFFFF), ctypes.byref(dm)):  # ENUM_CURRENT_SETTINGS
            return None, None
        current = (dm.dmPelsWidth, dm.dmPelsHeight, dm.dmDisplayFrequency, dm.dmBitsPerPel)
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


def apply_refresh_rate_fix(ctx: TaskContext):
    """Detect the monitor's maximum refresh rate at the CURRENT resolution
    and depth; if Windows is running it lower (the classic 144Hz panel stuck
    at 60Hz), set the mode to the max via ChangeDisplaySettings. Snapshots
    the prior mode for exact revert. Skips honestly when already maxed."""
    ctx.set_status("Checking monitor refresh rate...")
    current, maximum = _query_video_mode_list()
    if not current or not maximum:
        raise RuntimeError("Could not query display settings.")
    cur_hz, max_hz = current[2], maximum[2]
    ctx.log(f"Current: {current[0]}x{current[1]} @ {cur_hz} Hz — monitor supports {max_hz} Hz at this resolution.")
    if cur_hz >= max_hz:
        # B5 audit fix: this used to save a snapshot, log and `return` —
        # the runner then badged the tweak '✓ Active' although nothing
        # changed (and the saved snapshot made the badge source say so
        # too). Nothing was changed, so nothing is snapshotted; the skip
        # is raised BEFORE the snapshot so state stays untouched.
        raise TaskSkipped(f"Already running at the maximum ({max_hz} Hz) — nothing to change.")
    import ctypes
    from ctypes import wintypes
    try:
        class DEVMODE(ctypes.Structure):
            _fields_ = [
                ("dmDeviceName", wintypes.WCHAR * 32),
                ("dmSpecVersion", wintypes.WORD), ("dmDriverVersion", wintypes.WORD),
                ("dmSize", wintypes.WORD), ("dmDriverExtra", wintypes.WORD),
                ("dmFields", wintypes.DWORD),
                ("dmPositionX", ctypes.c_long), ("dmPositionY", ctypes.c_long),
                ("dmPelsWidth", wintypes.DWORD), ("dmPelsHeight", wintypes.DWORD),
                ("dmBitsPerPel", wintypes.DWORD), ("dmDisplayFlags", wintypes.DWORD),
                ("dmDisplayFrequency", wintypes.DWORD),
                ("dmICMMethod", wintypes.DWORD), ("dmICMIntent", wintypes.DWORD),
                ("dmMediaType", wintypes.DWORD), ("dmDitherType", wintypes.DWORD),
                ("dmReserved1", wintypes.DWORD), ("dmReserved2", wintypes.DWORD),
                ("dmPanningWidth", wintypes.DWORD), ("dmPanningHeight", wintypes.DWORD),
            ]
        user32 = ctypes.windll.user32
        dm = DEVMODE()
        dm.dmSize = ctypes.sizeof(DEVMODE)
        dm.dmPelsWidth, dm.dmPelsHeight = maximum[0], maximum[1]
        dm.dmBitsPerPel = maximum[3]
        dm.dmDisplayFrequency = max_hz
        # B4 fix: 0x02000000 is DM_MEDIATYPE, not BPP — dmMediaType was
        # never populated, so the flag made no change while DM_BITSPERPEL
        # (0x00040000) was missing and the populated dmBitsPerPel below was
        # ignored. Bit depth IS intended (dmBitsPerPel = maximum[3], the
        # mode's real depth), so flag it properly.
        dm.dmFields = 0x00040000 | 0x00080000 | 0x00100000 | 0x00400000  # DM_BITSPERPEL | DM_PELSWIDTH | DM_PELSHEIGHT | DM_DISPLAYFREQUENCY
        # 0 = apply dynamically (no reboot), CDS_UPDATEREGISTRY makes it persist
        rc = user32.ChangeDisplaySettingsW(ctypes.byref(dm), 0x00000001)
        if rc != 0:
            raise RuntimeError(f"Display mode change failed (code {rc}).")
    except RuntimeError:
        raise
    except Exception as exc:
        raise RuntimeError(f"Could not apply refresh rate: {exc}")
    save_tweak_snapshot("refresh_rate_fix", {"mode": current})
    ctx.log(f"Refresh rate set to {max_hz} Hz (was {cur_hz} Hz). "
            "If the screen stays black for >10s, Windows reverts it automatically.")


def revert_refresh_rate_fix(ctx: TaskContext):
    """Restore the snapshotted display mode (resolution, Hz, depth)."""
    ctx.set_status("Restoring previous display mode...")
    snap = get_tweak_snapshot("refresh_rate_fix")
    mode = snap.get("mode") if snap else None
    if not mode:
        ctx.log("No snapshot on file — nothing to revert (the tweak only records when it changes something).")
        clear_tweak_snapshot("refresh_rate_fix")
        return
    import ctypes
    from ctypes import wintypes
    try:
        class DEVMODE(ctypes.Structure):
            _fields_ = [
                ("dmDeviceName", wintypes.WCHAR * 32),
                ("dmSpecVersion", wintypes.WORD), ("dmDriverVersion", wintypes.WORD),
                ("dmSize", wintypes.WORD), ("dmDriverExtra", wintypes.WORD),
                ("dmFields", wintypes.DWORD),
                ("dmPositionX", ctypes.c_long), ("dmPositionY", ctypes.c_long),
                ("dmPelsWidth", wintypes.DWORD), ("dmPelsHeight", wintypes.DWORD),
                ("dmBitsPerPel", wintypes.DWORD), ("dmDisplayFlags", wintypes.DWORD),
                ("dmDisplayFrequency", wintypes.DWORD),
                ("dmICMMethod", wintypes.DWORD), ("dmICMIntent", wintypes.DWORD),
                ("dmMediaType", wintypes.DWORD), ("dmDitherType", wintypes.DWORD),
                ("dmReserved1", wintypes.DWORD), ("dmReserved2", wintypes.DWORD),
                ("dmPanningWidth", wintypes.DWORD), ("dmPanningHeight", wintypes.DWORD),
            ]
        user32 = ctypes.windll.user32
        dm = DEVMODE()
        dm.dmSize = ctypes.sizeof(DEVMODE)
        w, h, hz, bpp = mode
        dm.dmPelsWidth, dm.dmPelsHeight = w, h
        dm.dmBitsPerPel = bpp
        dm.dmDisplayFrequency = hz
        # B4 fix: same as apply — 0x00040000 (DM_BITSPERPEL) so the
        # snapshotted dmBitsPerPel depth is actually honored; 0x02000000
        # (DM_MEDIATYPE) was a bogus flag with no populated field behind it.
        dm.dmFields = 0x00040000 | 0x00080000 | 0x00100000 | 0x00400000  # DM_BITSPERPEL | DM_PELSWIDTH | DM_PELSHEIGHT | DM_DISPLAYFREQUENCY
        rc = user32.ChangeDisplaySettingsW(ctypes.byref(dm), 0x00000001)
        if rc != 0:
            raise RuntimeError(f"Revert display mode failed (code {rc}).")
    except RuntimeError:
        raise
    except Exception as exc:
        raise RuntimeError(f"Could not revert refresh rate: {exc}")
    clear_tweak_snapshot("refresh_rate_fix")
    ctx.log(f"Display mode restored to {mode[0]}x{mode[1]} @ {mode[2]} Hz.")


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


def apply_gpu_preference_high(ctx: TaskContext):
    """Tell Windows to run everything on the dedicated GPU (per-user, no
    admin): sets DirectXUserGlobalSettings' GPU preference to 2. Snapshots
    the exact prior string (usually 'GPUPreference=0;' or empty) for a
    precise revert."""
    ctx.set_status("Setting games to prefer the dedicated GPU...")
    prior = _read_gpu_pref()
    new = "GPUPreference=2;"
    ok = reg_set_value(ctx, "HKCU", _GPU_PREF_PATH, _GPU_PREF_VALUE, new, value_type="REG_SZ")
    if not ok:
        raise RuntimeError("Could not write GPU preference.")
    save_tweak_snapshot("gpu_preference_high", {"value": prior})
    ctx.log("Windows will now run games on the dedicated (high-performance) GPU.")


def revert_gpu_preference_high(ctx: TaskContext):
    """Restore the exact prior global GPU preference string (empty values
    are restored by deleting the registry value)."""
    ctx.set_status("Restoring GPU preference...")
    snap = get_tweak_snapshot("gpu_preference_high")
    prior = snap.get("value", "") if snap else ""
    if prior:
        reg_set_value_checked(ctx, "HKCU", _GPU_PREF_PATH, _GPU_PREF_VALUE, prior, value_type="REG_SZ")
    else:
        reg_delete_value(ctx, "HKCU", _GPU_PREF_PATH, _GPU_PREF_VALUE)
    clear_tweak_snapshot("gpu_preference_high")
    ctx.log("GPU preference restored to previous setting.")


# --- Round 4: snapshot helpers (one value or several per tweak) --- #
def _snap_reg_values(ctx: TaskContext, task_id: str, specs: "list[tuple[str, str, str]]"):
    """Snapshot the real prior state (value or absent) of each registry
    value so revert restores exactly what was there — never a guess."""
    from app.config_persist import save_tweak_snapshot
    data: dict = {"specs": [list(s) for s in specs]}
    for i, (hive, path, name) in enumerate(specs):
        prior = reg_get_value(ctx, hive, path, name)
        data[f"{i}:present"] = prior is not None
        data[f"{i}:value"] = prior
    save_tweak_snapshot(task_id, data)


def _restore_reg_values(ctx: TaskContext, task_id: str, value_type: str = "REG_DWORD"):
    """Restore a snapshot taken by _snap_reg_values (exact prior values;
    absent values are deleted back to Windows defaults)."""
    from app.config_persist import get_tweak_snapshot, clear_tweak_snapshot
    snap = get_tweak_snapshot(task_id)
    if not snap or "specs" not in snap:
        ctx.log("  (no snapshot found — nothing to restore)")
        return
    for i, (hive, path, name) in enumerate(snap["specs"]):
        if snap.get(f"{i}:present"):
            reg_set_value_checked(ctx, hive, path, name, snap.get(f"{i}:value"), value_type=value_type)
        else:
            reg_delete_value(ctx, hive, path, name)
    clear_tweak_snapshot(task_id)


_ADV = "Software\\Microsoft\\Windows\\CurrentVersion\\Explorer\\Advanced"


def apply_file_extensions(ctx: TaskContext):
    """Show file extensions + hidden files (the modder's must-have)."""
    _snap_reg_values(ctx, "file_extensions", [("HKCU", _ADV, "HideFileExt"), ("HKCU", _ADV, "Hidden")])
    reg_set_value_checked(ctx, "HKCU", _ADV, "HideFileExt", 0)
    reg_set_value_checked(ctx, "HKCU", _ADV, "Hidden", 1)
    ctx.log("File extensions and hidden files are now shown.")

def revert_file_extensions(ctx: TaskContext):
    _restore_reg_values(ctx, "file_extensions")
    ctx.log("File visibility restored to its prior setting.")


def apply_menu_delay(ctx: TaskContext):
    """Menus pop in 100ms instead of 400ms."""
    _snap_reg_values(ctx, "menu_delay", [("HKCU", "Control Panel\\Desktop", "MenuShowDelay")])
    reg_set_value_checked(ctx, "HKCU", "Control Panel\\Desktop", "MenuShowDelay", "100", value_type="REG_SZ")
    ctx.log("Menu delay set to 100ms.")

def revert_menu_delay(ctx: TaskContext):
    from app.config_persist import get_tweak_snapshot, clear_tweak_snapshot
    snap = get_tweak_snapshot("menu_delay")
    if snap and snap.get("0:present"):
        reg_set_value_checked(ctx, "HKCU", "Control Panel\\Desktop", "MenuShowDelay",
                      snap.get("0:value"), value_type="REG_SZ")
    else:
        reg_delete_value(ctx, "HKCU", "Control Panel\\Desktop", "MenuShowDelay")
    clear_tweak_snapshot("menu_delay")
    ctx.log("Menu delay restored to its prior setting.")


def apply_aero_shake_off(ctx: TaskContext):
    """Disable shake-to-minimize (no more nuked desktop mid-game)."""
    _snap_reg_values(ctx, "aero_shake", [("HKCU", _ADV, "DisallowShaking")])
    reg_set_value_checked(ctx, "HKCU", _ADV, "DisallowShaking", 1)
    ctx.log("Shake-to-minimize disabled.")

def revert_aero_shake_off(ctx: TaskContext):
    _restore_reg_values(ctx, "aero_shake")
    ctx.log("Shake-to-minimize restored to its prior setting.")


def apply_lock_screen_off(ctx: TaskContext):
    """Skip the lock screen — straight to login (admin, policy key)."""
    _snap_reg_values(ctx, "lock_screen",
                     [("HKLM", "SOFTWARE\\Policies\\Microsoft\\Windows\\Personalization", "NoLockScreen")])
    reg_set_value_checked(ctx, "HKLM", "SOFTWARE\\Policies\\Microsoft\\Windows\\Personalization", "NoLockScreen", 1)
    ctx.log("Lock screen skipped (takes effect at next sign-in).")

def revert_lock_screen_off(ctx: TaskContext):
    _restore_reg_values(ctx, "lock_screen")
    ctx.log("Lock screen setting restored.")


def apply_edge_preload_off(ctx: TaskContext):
    """Stop Edge's startup boost + background mode (admin; Edge updates may
    re-add these — rerun if Edge gets chatty again)."""
    _snap_reg_values(ctx, "edge_preload",
                     [("HKLM", "SOFTWARE\\Policies\\Microsoft\\Edge", "StartupBoostEnabled"),
                      ("HKLM", "SOFTWARE\\Policies\\Microsoft\\Edge", "BackgroundModeEnabled")])
    reg_set_value_checked(ctx, "HKLM", "SOFTWARE\\Policies\\Microsoft\\Edge", "StartupBoostEnabled", 0)
    reg_set_value_checked(ctx, "HKLM", "SOFTWARE\\Policies\\Microsoft\\Edge", "BackgroundModeEnabled", 0)
    ctx.log("Edge preloading disabled.")

def revert_edge_preload_off(ctx: TaskContext):
    _restore_reg_values(ctx, "edge_preload")
    ctx.log("Edge preload setting restored.")


def apply_dark_mode(ctx: TaskContext):
    """Prefer dark app themes."""
    _snap_reg_values(ctx, "dark_mode",
                     [("HKCU", "Software\\Microsoft\\Windows\\CurrentVersion\\Themes\\Personalize", "AppsUseLightTheme")])
    reg_set_value_checked(ctx, "HKCU", "Software\\Microsoft\\Windows\\CurrentVersion\\Themes\\Personalize", "AppsUseLightTheme", 0)
    ctx.log("Dark app theme preferred.")

def revert_dark_mode(ctx: TaskContext):
    _restore_reg_values(ctx, "dark_mode")
    ctx.log("App theme restored to its prior setting.")


def apply_remote_assist_off(ctx: TaskContext):
    """Disable inbound Remote Assistance offers (admin)."""
    _snap_reg_values(ctx, "remote_assist",
                     [("HKLM", "SYSTEM\\CurrentControlSet\\Control\\Terminal Server", "fAllowToGetHelp")])
    reg_set_value_checked(ctx, "HKLM", "SYSTEM\\CurrentControlSet\\Control\\Terminal Server", "fAllowToGetHelp", 0)
    ctx.log("Remote Assistance disabled.")

def revert_remote_assist_off(ctx: TaskContext):
    _restore_reg_values(ctx, "remote_assist")
    ctx.log("Remote Assistance setting restored.")


def apply_verbose_boot(ctx: TaskContext):
    """Verbose boot/shutdown messages instead of the spinner (admin)."""
    _snap_reg_values(ctx, "verbose_boot",
                     [("HKLM", "SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\Policies\\System", "VerboseStatus")])
    reg_set_value_checked(ctx, "HKLM", "SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\Policies\\System", "VerboseStatus", 1)
    ctx.log("Verbose boot messages on (visible at next restart).")

def revert_verbose_boot(ctx: TaskContext):
    _restore_reg_values(ctx, "verbose_boot")
    ctx.log("Boot messages restored to normal.")


def apply_location_tracking_off(ctx: TaskContext):
    """Disable the location sensor via policy (admin)."""
    _snap_reg_values(ctx, "location_tracking",
                     [("HKLM", "SOFTWARE\\Policies\\Microsoft\\Windows\\LocationAndSensors", "DisableLocation")])
    reg_set_value_checked(ctx, "HKLM", "SOFTWARE\\Policies\\Microsoft\\Windows\\LocationAndSensors", "DisableLocation", 1)
    ctx.log("Location tracking disabled.")

def revert_location_tracking_off(ctx: TaskContext):
    _restore_reg_values(ctx, "location_tracking")
    ctx.log("Location setting restored.")


def apply_widgets_board_off(ctx: TaskContext):
    """Policy-level Widgets board off (goes further than hiding the taskbar
    icon: the board, news feed and its background activity stop entirely)."""
    _snap_reg_values(ctx, "widgets_board_off",
                     [("HKLM", "SOFTWARE\\Policies\\Microsoft\\Dsh", "AllowNewsAndInterests"),
                      ("HKCU", "Software\\Microsoft\\Windows\\CurrentVersion\\Feeds", "ShellFeedsTaskbarViewMode")])
    reg_set_value_checked(ctx, "HKLM", "SOFTWARE\\Policies\\Microsoft\\Dsh", "AllowNewsAndInterests", 0)
    reg_set_value_checked(ctx, "HKCU", "Software\\Microsoft\\Windows\\CurrentVersion\\Feeds", "ShellFeedsTaskbarViewMode", 2)
    ctx.log("Widgets board disabled (icon hide + news feed off).")

def revert_widgets_board_off(ctx: TaskContext):
    _restore_reg_values(ctx, "widgets_board_off")
    ctx.log("Widgets board restored.")


def apply_autoplay_off(ctx: TaskContext):
    """Disable AutoPlay/AutoRun for USB sticks and discs (plugging in a
    drive never auto-launches anything — classic USB-malware vector)."""
    _snap_reg_values(ctx, "autoplay_off",
                     [("HKCU", "Software\\Microsoft\\Windows\\CurrentVersion\\Explorer\\AutoplayHandlers", "DisableAutoplay"),
                      ("HKCU", "Software\\Microsoft\\Windows\\CurrentVersion\\Policies\\Explorer", "NoDriveTypeAutoRun")])
    reg_set_value_checked(ctx, "HKCU", "Software\\Microsoft\\Windows\\CurrentVersion\\Explorer\\AutoplayHandlers", "DisableAutoplay", 1)
    reg_set_value_checked(ctx, "HKCU", "Software\\Microsoft\\Windows\\CurrentVersion\\Policies\\Explorer", "NoDriveTypeAutoRun", 255)
    ctx.log("AutoPlay disabled for all drives.")

def revert_autoplay_off(ctx: TaskContext):
    _restore_reg_values(ctx, "autoplay_off")
    ctx.log("AutoPlay restored.")


def apply_snap_flyout_off(ctx: TaskContext):
    """Disable the Snap-layouts flyout that pops when hovering a window's
    maximize button mid-game (Win+arrows snapping keeps working)."""
    _snap_reg_values(ctx, "snap_flyout_off",
                     [("HKCU", "Software\\Microsoft\\Windows\\CurrentVersion\\Explorer\\Advanced", "EnableSnapAssistFlyout")])
    reg_set_value_checked(ctx, "HKCU", "Software\\Microsoft\\Windows\\CurrentVersion\\Explorer\\Advanced", "EnableSnapAssistFlyout", 0)
    ctx.log("Snap flyout off — maximize-hover no longer pops layouts.")

def revert_snap_flyout_off(ctx: TaskContext):
    _restore_reg_values(ctx, "snap_flyout_off")
    ctx.log("Snap flyout restored.")


from app.tasks import Task  # noqa: E402

TASKS = [
    Task("restore_point_tweak", "Safety Checkpoint", "Saves a restore point so you can undo changes", create_restore_point, default=True, admin_required=True, column=0),
    Task("ultimate_performance", "Max Performance Mode", "Turns on hidden fastest power plan for better FPS", apply_ultimate_performance, default=True, admin_required=True, revert=revert_ultimate_performance, column=0),
    # audit fix (12 mis-gated HKCU tweaks): these are pure HKCU in BOTH apply
    # and revert — the Task default admin_required=True needlessly skipped
    # them for every non-admin (limited-mode) user.
    Task("classic_context_menu", "Full Right-Click Menu", "Shows full menu right away, no extra click", apply_classic_context_menu, default=True, admin_required=False, revert=revert_classic_context_menu, column=0),
    Task("disable_game_dvr", "Stop Background Recording", "Stops Xbox recording games in background that slows you", apply_disable_game_dvr, default=True, admin_required=False, revert=revert_disable_game_dvr, column=0),
    Task("game_mode", "Turn On Game Mode", "Lets Windows focus on your game, not background apps", apply_game_mode, default=True, admin_required=False, revert=revert_game_mode, column=0),
    Task("windowed_optimize", "Smooth Windowed Games", "Makes games smoother when playing in a window", apply_windowed_optimize, default=True, admin_required=False, revert=revert_windowed_optimize, column=0),
    Task("hags", "Faster Graphics (HAGS)", "Lets graphics card handle memory faster, needs restart", apply_hags, default=False, admin_required=True, revert=revert_hags, risk="REBOOT REQUIRED", column=0),
    Task("priority_separation", "Prioritize Your Game", "Gives your open game more CPU power", apply_priority_separation, default=False, admin_required=True, revert=revert_priority_separation, risk="REBOOT REQUIRED", column=0),
    Task("power_throttling_off", "Disable CPU Power Throttling", "Stops Windows from slowing CPU to save power", apply_power_throttling_off, default=False, admin_required=True, revert=revert_power_throttling_off, risk="REBOOT REQUIRED", column=0),
    Task("ssd_trim", "Enable SSD TRIM", "Keeps your SSD fast and healthy", apply_ssd_trim, default=False, admin_required=True, revert=revert_ssd_trim, risk="REBOOT REQUIRED", column=1),
    Task("ssd_superfetch", "Disable SysMain (Superfetch)", "Turns off old hard drive helper not needed for SSD", apply_ssd_superfetch, default=False, admin_required=True, revert=revert_ssd_superfetch, risk="REBOOT REQUIRED", column=1),
    Task("ssd_last_access", "Disable Last Access Updates", "Stops Windows writing every time you open a file", apply_ssd_last_access, default=False, admin_required=True, revert=revert_ssd_last_access, risk="REBOOT REQUIRED", column=1),
    Task("ssd_prefetch", "Disable Prefetcher", "Turns off extra loading helper for fast drives", apply_ssd_prefetch, default=False, admin_required=True, revert=revert_ssd_prefetch, risk="REBOOT REQUIRED", column=1),
    Task("disable_nagle", "Lower Ping (Advanced)", "Makes online games respond faster", apply_disable_nagle, default=False, admin_required=True, revert=revert_disable_nagle, risk="ADVANCED", column=1),
    Task("startup_delay", "Faster Startup", "Removes small delay before startup apps open", apply_startup_delay, default=False, admin_required=False, revert=revert_startup_delay, column=0),
    Task("max_cpu_power", "Max CPU Power", "Aggressive boost, no core parking, full speed while gaming", apply_max_cpu_power, default=False, admin_required=True, revert=revert_max_cpu_power, column=0),
    Task("taskbar_cleanup", "Remove Taskbar Junk", "Turns off Widgets, Chat icon, search highlights and Explorer ads", apply_taskbar_cleanup, default=False, admin_required=False, revert=revert_taskbar_cleanup, column=0),
    Task("local_search", "Fast Local Search", "Makes Start search instant with no Bing or web results", apply_local_search, default=False, admin_required=False, revert=revert_local_search, column=0),
    Task("stop_windows_ads", "Stop Windows Ads & Tips", "Blocks every suggestion, auto-install and lock-screen ad", apply_stop_windows_ads, default=False, admin_required=False, revert=revert_stop_windows_ads, column=0),
    Task("privacy_baseline", "Privacy Baseline", "One switch for ad ID, tracking, typing data and speech opt-outs", apply_privacy_baseline, default=False, admin_required=False, revert=revert_privacy_baseline, column=0),
    Task("stop_telemetry", "Stop Telemetry", "Turns off diagnostic services and tracking tasks safely", apply_stop_telemetry, default=False, admin_required=True, revert=revert_stop_telemetry, column=0),
    Task("nvidia_telemetry", "NVIDIA Telemetry Opt-Out", "Turns off NVIDIA's usage reports (driver untouched)", apply_nvidia_telemetry_optout, default=False, admin_required=True, revert=revert_nvidia_telemetry_optout, column=0),
    Task("ad_blocker", "System-Wide Ad Blocker", "Blocks ~78,000 known ad/tracker domains via the hosts file", apply_ad_blocker, default=False, revert=revert_ad_blocker, admin_required=True, risk="ADVANCED", column=0),
    Task("visual_effects", "Faster Animations", "Turns off transparency and animations for speed", apply_visual_effects_perf, default=False, admin_required=False, revert=revert_visual_effects_perf, column=1),
    Task("mouse_accel", "1:1 Mouse Aim", "Turns off mouse speedup so aim is steady", apply_disable_mouse_accel, default=False, admin_required=False, revert=revert_disable_mouse_accel, column=1),
    Task("keyboard_tuning", "Faster Keyboard", "Makes keys repeat faster when you hold them", apply_keyboard_tuning, default=False, admin_required=False, revert=revert_keyboard_tuning, column=1),
    Task("network_throttling", "Faster Online Gaming", "Removes speed limit Windows uses for videos", apply_network_throttling, default=False, admin_required=True, revert=revert_network_throttling, column=1),
    Task("games_priority", "Boost Game Priority", "Raises game priority for CPU and graphics", apply_games_priority, default=False, admin_required=True, revert=revert_games_priority, column=1),
    Task("usb_suspend", "Fix USB Dropouts", "Stops Windows pausing USB mics and controllers", apply_usb_suspend, default=False, admin_required=True, revert=revert_usb_suspend, column=1),
    Task("disk_timeout", "Keep Drive Awake", "Stops drive from sleeping while you game", apply_disk_timeout, default=False, admin_required=True, revert=revert_disk_timeout, column=1),
    Task("disable_fast_startup", "Fix Boot Issues", "Turns off fast boot to fix driver problems", apply_fast_startup_fix, default=False, admin_required=True, revert=revert_fast_startup_fix, column=1),
    Task("limit_telemetry", "Limit Tracking", "Tells Windows to collect less info about you", apply_limit_telemetry, default=False, admin_required=True, revert=revert_limit_telemetry, column=1),
    Task("activity_history", "Disable Activity History", "Stops Windows saving your recent files and history", apply_activity_history_disable, default=False, admin_required=True, revert=revert_activity_history_disable, column=0),
    Task("consumer_features", "Disable Consumer Features", "Stops Windows installing suggested apps", apply_consumer_features_disable, default=False, admin_required=True, revert=revert_consumer_features_disable, column=0),
    Task("tweak_delivery_optimization", "Disable Delivery Optimization", "Stops sharing updates with other PCs", apply_delivery_optimization_disable, default=False, admin_required=True, revert=revert_delivery_optimization_disable, column=1),
    Task("end_task_taskbar", "Enable End Task on Taskbar", "Lets you right-click taskbar to close frozen apps", apply_end_task_on_taskbar, default=False, admin_required=False, revert=revert_end_task_on_taskbar, column=1),
    Task("explorer_auto_discovery", "No Explorer Auto Discovery", "Stops Explorer guessing folder types; also resets your folder view/sort settings", apply_explorer_auto_discovery_disable, default=False, admin_required=False, revert=revert_explorer_auto_discovery_disable, column=0),
    Task("background_apps", "Disable Background Apps", "Stops apps running in background so games get more power", apply_background_apps_disable, default=False, admin_required=False, revert=revert_background_apps_disable, column=0),
    Task("shader_cache_10gb", "Shader Cache 10GB", "Sets shader cache to 10GB to stop stutter", apply_shader_cache_10gb, default=False, admin_required=False, revert=revert_shader_cache_10gb, column=1),
    Task("max_performance_gpu", "Prefer Max Performance", "Tells GPU to use max power for games", apply_nvidia_max_performance, default=False, admin_required=True, revert=revert_nvidia_max_performance, column=1),
    Task("fullscreen_opt", "Fullscreen Optimizations Off", "Fixes game lag in borderless window", apply_fullscreen_optimizations_disable, default=False, admin_required=False, revert=revert_fullscreen_optimizations_disable, column=0),
    Task("disable_sticky_keys", "Stop Sticky Keys Popups", "Stops Shift-spam popups/beeps interrupting games", apply_disable_sticky_keys, default=False, revert=revert_disable_sticky_keys, admin_required=False, column=0),
    Task("suppress_crash_popups", "Stop Crash Popups", "Background app crashes no longer pause or cover your game", apply_suppress_crash_popups, default=False, revert=revert_suppress_crash_popups, admin_required=False, column=0),
    Task("no_update_reboot", "Block Update Reboots", "Windows Update won't force-restart your PC while you're using it", apply_no_update_reboot, default=False, revert=revert_no_update_reboot, admin_required=True, column=0),
    Task("mpo_fix", "Fix Monitor Flicker (MPO)", "Fixes black-screen flashes and stutter on multi-monitor setups", apply_mpo_fix, default=False, revert=revert_mpo_fix, admin_required=True, risk="REBOOT REQUIRED", column=0),

    # --- Round 2 feature tasks (user request) --- #
    Task("gaming_dns", "Gaming DNS (Cloudflare)", "Switches DNS to Cloudflare 1.1.1.1/1.0.0.1 — often the fastest for game servers; fully undoable", apply_gaming_dns, default=False, revert=revert_gaming_dns, admin_required=True, column=0),
    Task("refresh_rate_fix", "Max Refresh Rate", "Checks your monitor is running at its highest Hz — many 144Hz+ screens ship stuck at 60Hz", apply_refresh_rate_fix, default=False, revert=revert_refresh_rate_fix, admin_required=False, column=0),
    Task("gpu_preference_high", "Prefer Dedicated GPU", "Tells Windows to always run games on the dedicated GPU instead of the power-saving one (laptops)", apply_gpu_preference_high, default=False, revert=revert_gpu_preference_high, admin_required=False, column=0),

    # --- Round 3 tasks (user request) --- #
    Task("eee_disable", "Fix NIC Disconnects (EEE)", "Stops network card power-saving that drops packets mid-game; restores your exact prior settings on undo", apply_eee_disable, default=False, revert=revert_eee_disable, admin_required=True, column=0),
    Task("ntfs_8dot3", "Disable 8.3 Short Names", "Speeds up folders with huge numbers of files; restores your PC's prior setting on undo", apply_ntfs_8dot3_disable, default=False, revert=revert_ntfs_8dot3_disable, admin_required=True, column=0),
    Task("dynamic_tick_off", "Disable Dynamic Tick", "Legacy latency tweak for benchmarkers — debatable gains on modern PCs; fully undoable", apply_disable_dynamic_tick, default=False, revert=revert_disable_dynamic_tick, admin_required=True, risk="REBOOT REQUIRED", column=0),

    # --- Round 4 tasks (user request: everyday usability + quiet privacy) --- #
    Task("file_extensions", "Show File Extensions", "Shows file extensions and hidden files — a must for editing configs and mods", apply_file_extensions, default=False, revert=revert_file_extensions, admin_required=False, column=0),
    Task("menu_delay", "Snappier Menus", "Menus pop in 100ms instead of 400ms — instant-feeling Start menu", apply_menu_delay, default=False, revert=revert_menu_delay, admin_required=False, column=0),
    Task("aero_shake", "No Shake-to-Minimize", "Stops windows minimizing everything when you grab and shake one mid-game", apply_aero_shake_off, default=False, revert=revert_aero_shake_off, admin_required=False, column=0),
    Task("lock_screen", "Skip Lock Screen", "Boots straight to the login prompt instead of the pretty lock screen", apply_lock_screen_off, default=False, revert=revert_lock_screen_off, admin_required=True, column=0),
    Task("edge_preload", "Stop Edge Preloading", "Stops Edge background boost processes if you never open Edge", apply_edge_preload_off, default=False, revert=revert_edge_preload_off, admin_required=True, column=0),
    Task("dark_mode", "Prefer Dark Apps", "Asks apps to use their dark theme for a consistent look", apply_dark_mode, default=False, revert=revert_dark_mode, admin_required=False, column=0),
    Task("remote_assist", "Disable Remote Assistance", "Closes the inbound-remote-help vector most gamers never use", apply_remote_assist_off, default=False, revert=revert_remote_assist_off, admin_required=True, column=0),
    Task("verbose_boot", "Verbose Boot Messages", "Shows what Windows is doing at boot and shutdown instead of the spinner", apply_verbose_boot, default=False, revert=revert_verbose_boot, admin_required=True, column=0),
    Task("location_tracking", "Disable Location Tracking", "Turns off the location sensor (breaks Find My Device and auto time-zone)", apply_location_tracking_off, default=False, revert=revert_location_tracking_off, admin_required=True, column=0),
    Task("widgets_board_off", "Disable Widgets Board", "Kills the Widgets news board entirely, not just its taskbar icon", apply_widgets_board_off, default=False, revert=revert_widgets_board_off, admin_required=True, column=0),
    Task("autoplay_off", "Disable USB AutoPlay", "Stops USB sticks auto-launching apps when plugged in", apply_autoplay_off, default=False, revert=revert_autoplay_off, admin_required=False, column=0),
    Task("snap_flyout_off", "No Snap Popups", "Stops layout popups when hovering maximize mid-game", apply_snap_flyout_off, default=False, revert=revert_snap_flyout_off, admin_required=False, column=0),
]