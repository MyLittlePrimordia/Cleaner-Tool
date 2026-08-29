"""
Tweak tab — all reversible. Short gamer-friendly labels + tooltip details.
Fixed: Fast Startup uses HiberbootEnabled, not hibernate off; added safe new tweaks.
"""

import subprocess
import time

from app.utils import (
    TaskContext, reg_set_value, reg_delete_value, reg_delete_key, reg_get_value,
    run_cmd, create_restore_point, IS_WINDOWS,
)

if IS_WINDOWS:
    import winreg

ULTIMATE_PERF_GUID = "e9a42b02-d5df-448d-aa00-03f14749eb61"
BALANCED_GUID = "381b4222-f694-41f0-9685-ff5bb260df2e"
CONTEXT_MENU_CLSID = "{86ca1aa0-34aa-4e8b-a509-50c905bae2a2}"


def _has_ssd() -> bool:
    """Check if system has at least one SSD drive."""
    if not IS_WINDOWS:
        return False
    try:
        # Use PowerShell to check for SSDs
        cmd = (
            'powershell -NoProfile -Command '
            '"Get-PhysicalDisk | Where-Object {$_.MediaType -eq \"SSD\"} | Select-Object -First 1"'
        )
        result = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=10,
                               creationflags=subprocess.CREATE_NO_WINDOW)
        return result.returncode == 0 and result.stdout.strip() != ""
    except Exception:
        return False


def apply_ultimate_performance(ctx: TaskContext):
    ctx.set_status("Enabling fastest power plan...")
    run_cmd(ctx, f"powercfg -duplicatescheme {ULTIMATE_PERF_GUID}")
    run_cmd(ctx, f"powercfg -setactive {ULTIMATE_PERF_GUID}")


def revert_ultimate_performance(ctx: TaskContext):
    ctx.set_status("Reverting to Balanced power plan...")
    run_cmd(ctx, f"powercfg -setactive {BALANCED_GUID}")


def _restart_explorer(ctx: TaskContext) -> bool:
    """Restart Explorer gracefully. Returns True on success."""
    ctx.log("Restarting Explorer...")
    # Try graceful shutdown first
    run_cmd(ctx, "taskkill /im explorer.exe", timeout=10)
    time.sleep(1)
    # Force if still running
    run_cmd(ctx, "taskkill /f /im explorer.exe", timeout=5)
    time.sleep(0.5)
    # Start Explorer
    rc = run_cmd(ctx, "explorer.exe", timeout=10)
    if rc == 0:
        ctx.log("Explorer restarted successfully.")
        return True
    ctx.log("  ! Explorer may not have restarted properly.")
    return False


def apply_classic_context_menu(ctx: TaskContext):
    reg_set_value(ctx, "HKCU", f"Software\\Classes\\CLSID\\{CONTEXT_MENU_CLSID}\\InprocServer32",
                  "", "", value_type="REG_SZ")
    _restart_explorer(ctx)


def revert_classic_context_menu(ctx: TaskContext):
    reg_delete_key(ctx, "HKCU", f"Software\\Classes\\CLSID\\{CONTEXT_MENU_CLSID}")
    _restart_explorer(ctx)


def apply_disable_game_dvr(ctx: TaskContext):
    reg_set_value(ctx, "HKCU", "System\\GameConfigStore", "GameDVR_Enabled", 0)
    reg_set_value(ctx, "HKCU", "Software\\Microsoft\\Windows\\CurrentVersion\\GameDVR", "AppCaptureEnabled", 0)
    reg_set_value(ctx, "HKLM", "SOFTWARE\\Policies\\Microsoft\\Windows\\GameDVR", "AllowGameDVR", 0)


def revert_disable_game_dvr(ctx: TaskContext):
    reg_set_value(ctx, "HKCU", "System\\GameConfigStore", "GameDVR_Enabled", 1)
    reg_set_value(ctx, "HKCU", "Software\\Microsoft\\Windows\\CurrentVersion\\GameDVR", "AppCaptureEnabled", 1)
    reg_delete_value(ctx, "HKLM", "SOFTWARE\\Policies\\Microsoft\\Windows\\GameDVR", "AllowGameDVR")


def apply_disable_mouse_accel(ctx: TaskContext):
    reg_set_value(ctx, "HKCU", "Control Panel\\Mouse", "MouseSpeed", "0", value_type="REG_SZ")
    reg_set_value(ctx, "HKCU", "Control Panel\\Mouse", "MouseThreshold1", "0", value_type="REG_SZ")
    reg_set_value(ctx, "HKCU", "Control Panel\\Mouse", "MouseThreshold2", "0", value_type="REG_SZ")


def revert_disable_mouse_accel(ctx: TaskContext):
    reg_set_value(ctx, "HKCU", "Control Panel\\Mouse", "MouseSpeed", "1", value_type="REG_SZ")
    reg_set_value(ctx, "HKCU", "Control Panel\\Mouse", "MouseThreshold1", "6", value_type="REG_SZ")
    reg_set_value(ctx, "HKCU", "Control Panel\\Mouse", "MouseThreshold2", "10", value_type="REG_SZ")


def apply_visual_effects_perf(ctx: TaskContext):
    reg_set_value(ctx, "HKCU", "Software\\Microsoft\\Windows\\CurrentVersion\\Themes\\Personalize",
                  "EnableTransparency", 0)
    reg_set_value(ctx, "HKCU", "Control Panel\\Desktop\\WindowMetrics", "MinAnimate", "0", value_type="REG_SZ")
    reg_set_value(ctx, "HKCU", "Software\\Microsoft\\Windows\\CurrentVersion\\Explorer\\Advanced",
                  "TaskbarAnimations", 0)
    reg_set_value(ctx, "HKCU", "Control Panel\\Desktop", "MenuShowDelay", "0", value_type="REG_SZ")


def revert_visual_effects_perf(ctx: TaskContext):
    reg_set_value(ctx, "HKCU", "Software\\Microsoft\\Windows\\CurrentVersion\\Themes\\Personalize",
                  "EnableTransparency", 1)
    reg_set_value(ctx, "HKCU", "Control Panel\\Desktop\\WindowMetrics", "MinAnimate", "1", value_type="REG_SZ")
    reg_set_value(ctx, "HKCU", "Software\\Microsoft\\Windows\\CurrentVersion\\Explorer\\Advanced",
                  "TaskbarAnimations", 1)
    reg_set_value(ctx, "HKCU", "Control Panel\\Desktop", "MenuShowDelay", "400", value_type="REG_SZ")


def apply_network_throttling(ctx: TaskContext):
    reg_set_value(ctx, "HKLM",
                  "SOFTWARE\\Microsoft\\Windows NT\\CurrentVersion\\Multimedia\\SystemProfile",
                  "NetworkThrottlingIndex", 0xFFFFFFFF)
    # SystemResponsiveness: 10 = reserve 10% CPU for background (values <10 treated as 20%)
    reg_set_value(ctx, "HKLM",
                  "SOFTWARE\\Microsoft\\Windows NT\\CurrentVersion\\Multimedia\\SystemProfile",
                  "SystemResponsiveness", 10)


def revert_network_throttling(ctx: TaskContext):
    reg_set_value(ctx, "HKLM",
                  "SOFTWARE\\Microsoft\\Windows NT\\CurrentVersion\\Multimedia\\SystemProfile",
                  "NetworkThrottlingIndex", 0xA)
    reg_set_value(ctx, "HKLM",
                  "SOFTWARE\\Microsoft\\Windows NT\\CurrentVersion\\Multimedia\\SystemProfile",
                  "SystemResponsiveness", 20)


def apply_games_priority(ctx: TaskContext):
    """Boost CPU/GPU/IO priority for games via Multimedia System Profile."""
    base = "SOFTWARE\\Microsoft\\Windows NT\\CurrentVersion\\Multimedia\\SystemProfile\\Tasks\\Games"
    reg_set_value(ctx, "HKLM", base, "Scheduling Category", "High", value_type="REG_SZ")
    reg_set_value(ctx, "HKLM", base, "GPU Priority", 8)
    reg_set_value(ctx, "HKLM", base, "Priority", 6)
    reg_set_value(ctx, "HKLM", base, "SFIO Priority", 8)


def revert_games_priority(ctx: TaskContext):
    base = "SOFTWARE\\Microsoft\\Windows NT\\CurrentVersion\\Multimedia\\SystemProfile\\Tasks\\Games"
    reg_set_value(ctx, "HKLM", base, "Scheduling Category", "Medium", value_type="REG_SZ")
    reg_set_value(ctx, "HKLM", base, "GPU Priority", 2)
    reg_set_value(ctx, "HKLM", base, "Priority", 2)
    reg_set_value(ctx, "HKLM", base, "SFIO Priority", 2)


def apply_disable_nagle(ctx: TaskContext):
    base = "SYSTEM\\CurrentControlSet\\Services\\Tcpip\\Parameters\\Interfaces"
    if not IS_WINDOWS:
        return
    try:
        key = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, base)
        try:
            i = 0
            while True:
                try:
                    sub = winreg.EnumKey(key, i)
                except OSError:
                    break
                i += 1
                reg_set_value(ctx, "HKLM", f"{base}\\{sub}", "TcpAckFrequency", 1)
                reg_set_value(ctx, "HKLM", f"{base}\\{sub}", "TCPNoDelay", 1)
        finally:
            winreg.CloseKey(key)
    except FileNotFoundError:
        ctx.log("No network interfaces found.")


def revert_disable_nagle(ctx: TaskContext):
    base = "SYSTEM\\CurrentControlSet\\Services\\Tcpip\\Parameters\\Interfaces"
    if not IS_WINDOWS:
        return
    try:
        key = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, base)
        try:
            i = 0
            while True:
                try:
                    sub = winreg.EnumKey(key, i)
                except OSError:
                    break
                i += 1
                # Restore Windows defaults: TcpAckFrequency=1, TCPNoDelay=0
                reg_set_value(ctx, "HKLM", f"{base}\\{sub}", "TcpAckFrequency", 1)
                reg_set_value(ctx, "HKLM", f"{base}\\{sub}", "TCPNoDelay", 0)
        finally:
            winreg.CloseKey(key)
    except FileNotFoundError:
        pass


def apply_hags(ctx: TaskContext):
    reg_set_value(ctx, "HKLM", "SYSTEM\\CurrentControlSet\\Control\\GraphicsDrivers", "HwSchMode", 2)
    ctx.log("Reboot required for graphics scheduling to take effect.")


def revert_hags(ctx: TaskContext):
    reg_set_value(ctx, "HKLM", "SYSTEM\\CurrentControlSet\\Control\\GraphicsDrivers", "HwSchMode", 1)
    ctx.log("Reboot required for graphics scheduling to take effect.")


def apply_game_mode(ctx: TaskContext):
    reg_set_value(ctx, "HKCU", "Software\\Microsoft\\GameBar", "AutoGameModeEnabled", 1)
    reg_set_value(ctx, "HKCU", "Software\\Microsoft\\GameBar", "AllowAutoGameMode", 1)


def revert_game_mode(ctx: TaskContext):
    reg_set_value(ctx, "HKCU", "Software\\Microsoft\\GameBar", "AutoGameModeEnabled", 0)
    reg_set_value(ctx, "HKCU", "Software\\Microsoft\\GameBar", "AllowAutoGameMode", 0)


def apply_windowed_optimize(ctx: TaskContext):
    # Optimizations for windowed games — supported Windows 11 setting
    reg_set_value(ctx, "HKCU", "System\\GameConfigStore", "GameDVR_FSEBehaviorMode", 2)
    reg_set_value(ctx, "HKCU", "System\\GameConfigStore", "GameDVR_DXGIHonorFSEWindowsCompatible", 1)
    reg_set_value(ctx, "HKCU", "System\\GameConfigStore", "GameDVR_HonorUserFSEBehaviorMode", 1)


def revert_windowed_optimize(ctx: TaskContext):
    reg_set_value(ctx, "HKCU", "System\\GameConfigStore", "GameDVR_FSEBehaviorMode", 0)
    reg_delete_value(ctx, "HKCU", "System\\GameConfigStore", "GameDVR_DXGIHonorFSEWindowsCompatible")
    reg_delete_value(ctx, "HKCU", "System\\GameConfigStore", "GameDVR_HonorUserFSEBehaviorMode")


def apply_fast_startup_fix(ctx: TaskContext):
    # Correct: disable Fast Startup via HiberbootEnabled, not hibernate off
    reg_set_value(ctx, "HKLM", "SYSTEM\\CurrentControlSet\\Control\\Session Manager\\Power", "HiberbootEnabled", 0)


def revert_fast_startup_fix(ctx: TaskContext):
    reg_set_value(ctx, "HKLM", "SYSTEM\\CurrentControlSet\\Control\\Session Manager\\Power", "HiberbootEnabled", 1)


def apply_limit_telemetry(ctx: TaskContext):
    reg_set_value(ctx, "HKLM", "SOFTWARE\\Policies\\Microsoft\\Windows\\DataCollection", "AllowTelemetry", 1)


def revert_limit_telemetry(ctx: TaskContext):
    reg_delete_value(ctx, "HKLM", "SOFTWARE\\Policies\\Microsoft\\Windows\\DataCollection", "AllowTelemetry")


def apply_priority_separation(ctx: TaskContext):
    # Foreground app gets more CPU — 0x26 = gaming bias
    reg_set_value(ctx, "HKLM", "SYSTEM\\CurrentControlSet\\Control\\PriorityControl", "Win32PrioritySeparation", 38)


def revert_priority_separation(ctx: TaskContext):
    reg_set_value(ctx, "HKLM", "SYSTEM\\CurrentControlSet\\Control\\PriorityControl", "Win32PrioritySeparation", 2)


def apply_power_throttling_off(ctx: TaskContext):
    """Disable CPU power throttling for consistent performance."""
    reg_set_value(ctx, "HKLM", "SYSTEM\\CurrentControlSet\\Control\\Power\\PowerThrottling", "PowerThrottlingOff", 1)


def revert_power_throttling_off(ctx: TaskContext):
    reg_delete_value(ctx, "HKLM", "SYSTEM\\CurrentControlSet\\Control\\Power\\PowerThrottling", "PowerThrottlingOff")


def apply_startup_delay(ctx: TaskContext):
    reg_set_value(ctx, "HKCU", "Software\\Microsoft\\Windows\\CurrentVersion\\Explorer\\Serialize", "StartupDelayInMSec", 0)


def revert_startup_delay(ctx: TaskContext):
    reg_delete_value(ctx, "HKCU", "Software\\Microsoft\\Windows\\CurrentVersion\\Explorer\\Serialize", "StartupDelayInMSec")


def apply_usb_suspend(ctx: TaskContext):
    run_cmd(ctx, "powercfg /change usb-selective-suspend-setting 0")
    run_cmd(ctx, "powercfg /setacvalueindex scheme_current 2a737441-1930-4402-8d77-b2bbe5a308a3 48e6b7a6-50f5-4782-a5d4-53bb8fcc84df 0")
    run_cmd(ctx, "powercfg /setactive scheme_current")


def revert_usb_suspend(ctx: TaskContext):
    run_cmd(ctx, "powercfg /change usb-selective-suspend-setting 1")
    run_cmd(ctx, "powercfg /setacvalueindex scheme_current 2a737441-1930-4402-8d77-b2bbe5a308a3 48e6b7a6-50f5-4782-a5d4-53bb8fcc84df 1")
    run_cmd(ctx, "powercfg /setactive scheme_current")


def apply_disk_timeout(ctx: TaskContext):
    run_cmd(ctx, "powercfg /change disk-timeout-ac 0")
    run_cmd(ctx, "powercfg /change disk-timeout-dc 0")


def revert_disk_timeout(ctx: TaskContext):
    run_cmd(ctx, "powercfg /change disk-timeout-ac 20")
    run_cmd(ctx, "powercfg /change disk-timeout-dc 20")


_KEYBOARD_BACKUP_PATH = "Control Panel\\Keyboard"


def apply_keyboard_tuning(ctx: TaskContext):
    # Capture whatever the user's current values are (they vary by machine/
    # prior customization) before overwriting, so revert restores the exact
    # prior state instead of a guessed "default".
    from app.config_persist import load_config, save_config
    prev_delay = reg_get_value("HKCU", _KEYBOARD_BACKUP_PATH, "KeyboardDelay", "1")
    prev_speed = reg_get_value("HKCU", _KEYBOARD_BACKUP_PATH, "KeyboardSpeed", "0")
    config = load_config()
    config["_keyboard_tuning_backup"] = {"KeyboardDelay": str(prev_delay), "KeyboardSpeed": str(prev_speed)}
    save_config(config)

    reg_set_value(ctx, "HKCU", "Control Panel\\Keyboard", "KeyboardDelay", "0", value_type="REG_SZ")
    reg_set_value(ctx, "HKCU", "Control Panel\\Keyboard", "KeyboardSpeed", "31", value_type="REG_SZ")


def revert_keyboard_tuning(ctx: TaskContext):
    from app.config_persist import load_config
    config = load_config()
    backup = config.get("_keyboard_tuning_backup") or {"KeyboardDelay": "1", "KeyboardSpeed": "0"}
    reg_set_value(ctx, "HKCU", "Control Panel\\Keyboard", "KeyboardDelay", backup["KeyboardDelay"], value_type="REG_SZ")
    reg_set_value(ctx, "HKCU", "Control Panel\\Keyboard", "KeyboardSpeed", backup["KeyboardSpeed"], value_type="REG_SZ")


def apply_ssd_trim(ctx: TaskContext):
    """Enable TRIM/DisableDeleteNotify for SSD."""
    if not _has_ssd():
        ctx.log("No SSD detected — skipping TRIM tweak (only applies to SSDs).")
        return
    run_cmd(ctx, "fsutil behavior set disabledeletenotify 0")


def revert_ssd_trim(ctx: TaskContext):
    if not _has_ssd():
        ctx.log("No SSD detected — skipping TRIM revert.")
        return
    run_cmd(ctx, "fsutil behavior set disabledeletenotify 1")


def apply_ssd_superfetch(ctx: TaskContext):
    """Disable SysMain (Superfetch) — unnecessary on SSD/NVMe."""
    if not _has_ssd():
        ctx.log("No SSD detected — skipping SysMain tweak (only applies to SSDs).")
        return
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
        ctx.log("No SSD detected — skipping last access tweak (only applies to SSDs).")
        return
    reg_set_value(ctx, "HKLM", "SYSTEM\\CurrentControlSet\\Control\\FileSystem",
                  "NtfsDisableLastAccessUpdate", 1)


def revert_ssd_last_access(ctx: TaskContext):
    if not _has_ssd():
        ctx.log("No SSD detected — skipping last access revert.")
        return
    reg_set_value(ctx, "HKLM", "SYSTEM\\CurrentControlSet\\Control\\FileSystem",
                  "NtfsDisableLastAccessUpdate", 0)


def apply_ssd_prefetch(ctx: TaskContext):
    """Disable Prefetcher and Superfetch for SSD."""
    if not _has_ssd():
        ctx.log("No SSD detected — skipping prefetch tweak (only applies to SSDs).")
        return
    reg_set_value(ctx, "HKLM", "SYSTEM\\CurrentControlSet\\Control\\Session Manager\\Memory Management\\PrefetchParameters",
                  "EnablePrefetcher", 0)
    reg_set_value(ctx, "HKLM", "SYSTEM\\CurrentControlSet\\Control\\Session Manager\\Memory Management\\PrefetchParameters",
                  "EnableSuperfetch", 0)


def revert_ssd_prefetch(ctx: TaskContext):
    reg_set_value(ctx, "HKLM", "SYSTEM\\CurrentControlSet\\Control\\Session Manager\\Memory Management\\PrefetchParameters",
                  "EnablePrefetcher", 3)
    reg_set_value(ctx, "HKLM", "SYSTEM\\CurrentControlSet\\Control\\Session Manager\\Memory Management\\PrefetchParameters",
                  "EnableSuperfetch", 3)


# --------------------------------------------------------------------------- #
# Defender exclusions for known game install folders
# --------------------------------------------------------------------------- #

def apply_defender_game_exclusions(ctx: TaskContext):
    """Exclude auto-detected Steam/Epic/GOG/Battle.net/Riot/Ubisoft/EA game
    install folders from Windows Defender real-time scanning. This only
    touches folders we can verify exist on disk under a known launcher's
    install location — never a whole drive, never Downloads or a user
    profile root. Reduces AV-scan-induced stutter during asset streaming,
    at the cost of Defender no longer scanning those specific folders."""
    from app.tasks.game_install_dirs import discover_all_game_install_dirs
    from app.config_persist import load_config, save_config

    ctx.set_status("Detecting installed game folders...")
    paths = discover_all_game_install_dirs()
    if not paths:
        ctx.log("No known game install folders were found — nothing to exclude.")
        return

    ctx.log(f"Found {len(paths)} game folder(s) to exclude from Defender scanning:")
    for p in paths:
        ctx.log(f"  {p}")

    if ctx.dry_run:
        ctx.log("  (dry run - Defender exclusions not applied)")
        return

    # Build a single Add-MpPreference call with all paths at once.
    ps_paths = ",".join(f"'{p}'" for p in paths)
    ps_cmd = f'powershell -NoProfile -Command "Add-MpPreference -ExclusionPath @({ps_paths})"'
    rc = run_cmd(ctx, ps_cmd, timeout=60)
    if rc == 0:
        config = load_config()
        config["_defender_exclusions_added"] = paths
        save_config(config)
        ctx.log("Defender exclusions applied.")
    else:
        ctx.log("  ! Could not apply Defender exclusions (Defender may be managed by policy/another AV).")


def revert_defender_game_exclusions(ctx: TaskContext):
    """Remove exactly the exclusions this app added (tracked in config), not
    any exclusions the user configured themselves."""
    from app.config_persist import load_config, save_config

    config = load_config()
    paths = config.get("_defender_exclusions_added") or []
    if not paths:
        ctx.log("No Defender exclusions were recorded as added by this app — nothing to revert.")
        return
    ps_paths = ",".join(f"'{p}'" for p in paths)
    ps_cmd = f'powershell -NoProfile -Command "Remove-MpPreference -ExclusionPath @({ps_paths})"'
    run_cmd(ctx, ps_cmd, timeout=60)
    config["_defender_exclusions_added"] = []
    save_config(config)
    ctx.log("Defender exclusions removed.")


# --------------------------------------------------------------------------- #
# Defender scheduled scan off-hours
# --------------------------------------------------------------------------- #

def apply_scan_offhours(ctx: TaskContext):
    """Move Defender's scheduled (not real-time) scan to 3 AM daily instead
    of whatever randomized time it currently uses, so it doesn't kick off
    mid-session."""
    run_cmd(ctx, 'powershell -NoProfile -Command '
                 '"Set-MpPreference -ScanScheduleDay 0 -ScanScheduleTime 03:00:00"', timeout=30)


def revert_scan_offhours(ctx: TaskContext):
    """Restore Defender's typical out-of-box schedule (scan every day,
    around 2 AM). Windows normally randomizes this; this is a reasonable
    approximation of default, not a captured original value."""
    run_cmd(ctx, 'powershell -NoProfile -Command '
                 '"Set-MpPreference -ScanScheduleDay 0 -ScanScheduleTime 02:00:00"', timeout=30)


# --------------------------------------------------------------------------- #
# Windows Update Active Hours (avoid forced restarts during gaming)
# --------------------------------------------------------------------------- #

def apply_active_hours_gaming(ctx: TaskContext):
    """Widen Active Hours to 8 AM - 11 PM so Windows Update won't force a
    restart during a typical gaming session."""
    reg_set_value(ctx, "HKLM", "SOFTWARE\\Microsoft\\WindowsUpdate\\UX\\Settings", "ActiveHoursStart", 8)
    reg_set_value(ctx, "HKLM", "SOFTWARE\\Microsoft\\WindowsUpdate\\UX\\Settings", "ActiveHoursEnd", 23)


def revert_active_hours_gaming(ctx: TaskContext):
    """Remove the explicit values so Windows goes back to automatically
    detecting active hours from usage patterns."""
    reg_delete_value(ctx, "HKLM", "SOFTWARE\\Microsoft\\WindowsUpdate\\UX\\Settings", "ActiveHoursStart")
    reg_delete_value(ctx, "HKLM", "SOFTWARE\\Microsoft\\WindowsUpdate\\UX\\Settings", "ActiveHoursEnd")


# --------------------------------------------------------------------------- #
# Delivery Optimization: stop uploading update chunks to the internet
# --------------------------------------------------------------------------- #

def apply_delivery_optimization_lan(ctx: TaskContext):
    """Set Delivery Optimization to LAN-only (mode 1): Windows still gets
    updates from Microsoft and can share/receive them with PCs on your own
    network, but stops uploading your bandwidth to PCs elsewhere on the
    internet."""
    reg_set_value(ctx, "HKLM", "SOFTWARE\\Policies\\Microsoft\\Windows\\DeliveryOptimization",
                  "DODownloadMode", 1)


def revert_delivery_optimization_lan(ctx: TaskContext):
    reg_delete_value(ctx, "HKLM", "SOFTWARE\\Policies\\Microsoft\\Windows\\DeliveryOptimization",
                     "DODownloadMode")


from app.tasks import Task  # noqa: E402

TASKS = [
    Task("restore_point_tweak", "Safety Checkpoint",
         "Creates an undo point before changing settings.",
         create_restore_point, default=True, column=0),
    Task("ultimate_performance", "Max Performance Mode",
         "Enables the hidden fastest power plan for gaming — more power use.",
         apply_ultimate_performance, default=True, revert=revert_ultimate_performance, column=0),
    Task("classic_context_menu", "Full Right-Click Menu",
         "Shows full menu right away — no 'Show more options' click.",
         apply_classic_context_menu, default=True, revert=revert_classic_context_menu, column=0),
    Task("disable_game_dvr", "Stop Background Recording",
         "Stops Xbox background recording that can slow games.",
         apply_disable_game_dvr, default=True, revert=revert_disable_game_dvr, column=0),
    Task("game_mode", "Turn On Game Mode",
         "Lets Windows prioritize your game over background apps.",
         apply_game_mode, default=True, revert=revert_game_mode, column=0),
    Task("windowed_optimize", "Smooth Windowed Games",
         "Turns on Windows 11 optimization for games in windowed mode.",
         apply_windowed_optimize, default=True, revert=revert_windowed_optimize, column=0),
    Task("hags", "Faster Graphics (HAGS)",
         "Lets your GPU handle its own memory — needs reboot, may help on newer cards.",
         apply_hags, default=False, revert=revert_hags, risk="REBOOT REQUIRED", column=0),
    Task("priority_separation", "Prioritize Your Game",
         "Gives your active game more CPU power.",
         apply_priority_separation, default=False, revert=revert_priority_separation, risk="REBOOT REQUIRED", column=0),
    Task("power_throttling_off", "Disable CPU Power Throttling",
         "Prevents Windows from lowering CPU frequency to save power.",
         apply_power_throttling_off, default=False, revert=revert_power_throttling_off, risk="REBOOT REQUIRED", column=0),
    Task("startup_delay", "Faster Startup",
         "Removes the short delay Windows adds before starting startup apps.",
         apply_startup_delay, default=False, revert=revert_startup_delay, column=0),
    Task("visual_effects", "Faster Animations",
         "Turns off transparency and window animations.",
         apply_visual_effects_perf, default=False, revert=revert_visual_effects_perf, column=1),
    Task("mouse_accel", "1:1 Mouse Aim",
         "Turns off pointer acceleration for consistent aim — great for shooters.",
         apply_disable_mouse_accel, default=False, revert=revert_disable_mouse_accel, column=1),
    Task("keyboard_tuning", "Faster Keyboard",
         "Makes keys repeat faster — less delay when holding a key.",
         apply_keyboard_tuning, default=False, revert=revert_keyboard_tuning, column=1),
    Task("network_throttling", "Faster Online Gaming",
         "Removes network speed limit Windows uses during video playback.",
         apply_network_throttling, default=False, revert=revert_network_throttling, column=1),
    Task("disable_nagle", "Lower Ping (Advanced)",
         "Reduces delay for online games — touches each network adapter.",
         apply_disable_nagle, default=False, revert=revert_disable_nagle, risk="ADVANCED", column=1),
    Task("games_priority", "Boost Game Priority",
         "Raises CPU/GPU/IO scheduling priority for games in Multimedia System Profile.",
         apply_games_priority, default=False, revert=revert_games_priority, column=1),
    Task("usb_suspend", "Fix USB Dropouts",
         "Stops Windows from pausing USB devices — fixes mic/controller cutouts.",
         apply_usb_suspend, default=False, revert=revert_usb_suspend, column=1),
    Task("disk_timeout", "Keep Drive Awake",
         "Stops your drive from sleeping during gaming — helps on desktops.",
         apply_disk_timeout, default=False, revert=revert_disk_timeout, column=1),
    Task("disable_fast_startup", "Fix Boot Issues",
         "Turns off fast startup to fix driver or dual-boot problems — boot is slightly slower.",
         apply_fast_startup_fix, default=False, revert=revert_fast_startup_fix, column=1),
    Task("limit_telemetry", "Limit Tracking",
         "Sets data collection to the minimum level.",
         apply_limit_telemetry, default=False, revert=revert_limit_telemetry, column=1),
    Task("ssd_trim", "Enable SSD TRIM",
         "Ensures TRIM is enabled for SSD performance/longevity.",
         apply_ssd_trim, default=False, revert=revert_ssd_trim, risk="REBOOT REQUIRED", column=1),
    Task("ssd_superfetch", "Disable SysMain (Superfetch)",
         "Disables Superfetch — not needed on SSD/NVMe.",
         apply_ssd_superfetch, default=False, revert=revert_ssd_superfetch, risk="REBOOT REQUIRED", column=1),
    Task("ssd_last_access", "Disable Last Access Updates",
         "Stops NTFS from updating file access times — reduces SSD writes.",
         apply_ssd_last_access, default=False, revert=revert_ssd_last_access, risk="REBOOT REQUIRED", column=1),
    Task("ssd_prefetch", "Disable Prefetcher",
         "Disables Prefetch/Superfetch — not needed on fast storage.",
         apply_ssd_prefetch, default=False, revert=revert_ssd_prefetch, risk="REBOOT REQUIRED", column=1),
    Task("defender_game_exclusions", "Exclude Game Folders (Defender)",
         "Stops antivirus scanning from causing stutter in Steam/Epic/GOG/etc. game folders. Reduces AV coverage there — only use for folders you trust.",
         apply_defender_game_exclusions, default=False, revert=revert_defender_game_exclusions,
         risk="ADVANCED", column=1),
    Task("scan_offhours", "Move Antivirus Scans to 3 AM",
         "Schedules Defender's full scan for 3 AM instead of a random time, so it won't start mid-session.",
         apply_scan_offhours, default=False, revert=revert_scan_offhours, column=1),
    Task("active_hours_gaming", "Block Update Restarts (8am-11pm)",
         "Tells Windows Update not to force a restart between 8 AM and 11 PM.",
         apply_active_hours_gaming, default=True, revert=revert_active_hours_gaming, column=0),
    Task("delivery_optimization_lan", "Stop Uploading Updates to Strangers",
         "Keeps update sharing on your own network only — stops Windows uploading update files to other PCs over the internet.",
         apply_delivery_optimization_lan, default=True, revert=revert_delivery_optimization_lan, column=0),
]
