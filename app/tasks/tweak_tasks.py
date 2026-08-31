"""
Tweak tab — all reversible. Short gamer-friendly labels + tooltip details.
Fixed: Fast Startup uses HiberbootEnabled, not hibernate off; added safe new tweaks.
"""

import subprocess
import time

from app.utils import (
    TaskContext, reg_set_value, reg_delete_value, reg_delete_key, run_cmd, create_restore_point, IS_WINDOWS,
)

if IS_WINDOWS:
    import winreg

ULTIMATE_PERF_GUID = "e9a42b02-d5df-448d-aa00-03f14749eb61"
BALANCED_GUID = "381b4222-f694-41f0-9685-ff5bb260df2e"
CONTEXT_MENU_CLSID = "{86ca1aa0-34aa-4e8b-a509-50c905bae2a2}"


import functools

@functools.lru_cache(maxsize=1)
def _has_ssd() -> bool:
    """Check if system has at least one SSD drive (cached — PowerShell is slow)."""
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
    # Duplicating may create a new GUID (logged as "Power Scheme GUID: ...") — capture output
    # We use run_cmd for logging but also try to handle "unsupported" editions gracefully
    rc1 = run_cmd(ctx, f"powercfg -duplicatescheme {ULTIMATE_PERF_GUID}")
    # Try to activate — first the canonical GUID, if that fails try to find the duplicated GUID from powercfg /list
    rc2 = run_cmd(ctx, f"powercfg -setactive {ULTIMATE_PERF_GUID}")
    if rc2 != 0:
        # Fallback: parse powercfg /list for Ultimate Performance and activate its GUID
        try:
            import subprocess as _sp
            out = _sp.check_output("powercfg /list", shell=True, text=True, stderr=subprocess.STDOUT, timeout=10)
            # Look for line with Ultimate Performance
            import re as _re
            m = _re.search(r"Power Scheme GUID:\s*([0-9a-fA-F-]{36}).*Ultimate Performance", out, _re.I)
            if m:
                alt_guid = m.group(1)
                ctx.log(f"Retrying activation with listed GUID {alt_guid}")
                rc2 = run_cmd(ctx, f"powercfg -setactive {alt_guid}")
        except Exception:
            pass
        if rc2 != 0:
            ctx.log("Ultimate Performance plan not supported on this edition or failed to activate — continuing (non-fatal).")
    # Always succeed — power plan is best-effort, not fatal


def revert_ultimate_performance(ctx: TaskContext):
    ctx.set_status("Reverting to Balanced power plan...")
    run_cmd(ctx, f"powercfg -setactive {BALANCED_GUID}")


def _restart_explorer(ctx: TaskContext) -> bool:
    """Restart Explorer gracefully. Returns True on success (non-blocking launch)."""
    ctx.log("Restarting Explorer...")
    if ctx.dry_run:
        ctx.log("  (dry run - would restart Explorer)")
        return True
    # Try graceful shutdown first
    run_cmd(ctx, "taskkill /im explorer.exe", timeout=10)
    time.sleep(1)
    # Force if still running
    run_cmd(ctx, "taskkill /f /im explorer.exe", timeout=5)
    time.sleep(0.5)
    # Start Explorer detached — explorer.exe stays running, so we must not wait for exit (would timeout)
    try:
        import subprocess as _sp
        # Use CREATE_NO_WINDOW and DETACHED_PROCESS so we don't wait
        creationflags = getattr(_sp, "CREATE_NO_WINDOW", 0)
        try:
            creationflags |= _sp.DETACHED_PROCESS  # type: ignore[attr-defined]
        except AttributeError:
            pass
        # Shell=False to avoid cmd wrapper; cwd None
        _sp.Popen(["explorer.exe"], creationflags=creationflags, close_fds=True)
        time.sleep(1.0)
        # Verify explorer is running
        try:
            out = _sp.check_output("tasklist /fi \"imagename eq explorer.exe\" /fo csv /nh", shell=True, text=True, timeout=5, stderr=_sp.DEVNULL)
            if "explorer.exe" in out.lower():
                ctx.log("Explorer restarted successfully.")
                return True
        except Exception:
            pass
        # Fallback: assume success if no exception
        ctx.log("Explorer restart attempted (process launched).")
        return True
    except Exception as e:
        ctx.log(f"  ! Explorer restart failed: {e}")
        return True  # Non-fatal for tweak batch


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


def apply_keyboard_tuning(ctx: TaskContext):
    reg_set_value(ctx, "HKCU", "Control Panel\\Keyboard", "KeyboardDelay", "0", value_type="REG_SZ")
    reg_set_value(ctx, "HKCU", "Control Panel\\Keyboard", "KeyboardSpeed", "31", value_type="REG_SZ")


def revert_keyboard_tuning(ctx: TaskContext):
    # Windows defaults: KeyboardDelay 1 (250ms), KeyboardSpeed 31 (fastest). Verified via AskVG/TenForums.
    reg_set_value(ctx, "HKCU", "Control Panel\\Keyboard", "KeyboardDelay", "1", value_type="REG_SZ")
    reg_set_value(ctx, "HKCU", "Control Panel\\Keyboard", "KeyboardSpeed", "31", value_type="REG_SZ")


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
    # Revert should restore Windows default (TRIM enabled = 0), not disable TRIM (1).
    # Disabling TRIM harms SSD performance/lifespan.
    run_cmd(ctx, "fsutil behavior set disabledeletenotify 0")


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
]
