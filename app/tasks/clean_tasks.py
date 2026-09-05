"""
Clean tab — only deletes disposable, auto-regenerating junk.
Laymen short labels + hover tooltips; compact symmetrical grid.
"""

import glob as globmod
import os
import subprocess

from app.utils import TaskContext, clean_folder_contents, run_cmd, restart_explorer
from app.tasks.launcher_paths import ALL_LAUNCHER_CACHE_PATHS, GPU_SHADER_CACHE_ALL

_LOCALAPPDATA = os.environ.get("LOCALAPPDATA", "")
_APPDATA = os.environ.get("APPDATA", "")
_TEMP = os.environ.get("TEMP", "")
_SYSTEMDRIVE = os.environ.get("SYSTEMDRIVE", "C:")
# Bare "C:" is not a root — os.path.join("C:", "*.chk") -> "C:*.chk" (current dir on C:), not "C:\*.chk"
if len(_SYSTEMDRIVE) == 2 and _SYSTEMDRIVE[1] == ":":
    _SYSTEMDRIVE_ROOT = _SYSTEMDRIVE + "\\"
elif _SYSTEMDRIVE.endswith("\\"):
    _SYSTEMDRIVE_ROOT = _SYSTEMDRIVE
else:
    _SYSTEMDRIVE_ROOT = _SYSTEMDRIVE + "\\"
_WINDIR = os.environ.get("WINDIR", f"{_SYSTEMDRIVE_ROOT}Windows")
_PROGRAMDATA = os.environ.get("ProgramData", f"{_SYSTEMDRIVE_ROOT}ProgramData")


def _clean_many(ctx: TaskContext, folders, label):
    total = 0
    for folder in folders:
        # Guard: if env var was missing, os.path.join("", "X") yields relative path — skip to avoid deleting CWD fixtures
        if not folder or not os.path.isabs(folder):
            continue
        if os.path.exists(folder):
            ctx.log(f"Cleaning {label}: {folder}")
            total += clean_folder_contents(ctx, folder)
    return total


def clean_shader_cache(ctx: TaskContext):
    # M2 fix: now sourced from launcher_paths.GPU_SHADER_CACHE_ALL — the
    # single shared list also used by game_tasks.clean_gpu_shader_caches —
    # instead of a separate hardcoded copy that pointed at the wrong
    # NVIDIA ComputeCache location (APPDATA instead of LOCALAPPDATA) and was
    # missing the PerDriverVersion\ cache paths.
    return _clean_many(ctx, GPU_SHADER_CACHE_ALL, "shader cache")


def clean_launcher_cache(ctx: TaskContext):
    return _clean_many(ctx, ALL_LAUNCHER_CACHE_PATHS, "launcher cache")


def clean_engine_cache(ctx: TaskContext):
    folders = [
        os.path.join(_LOCALAPPDATA, "UnrealEngine\\Common\\DerivedDataCache"),
        os.path.join(_LOCALAPPDATA, "Unity\\cache"),
        os.path.join(_LOCALAPPDATA, "Unity\\caches"),
        os.path.join(_LOCALAPPDATA, "Temp\\UnrealEngine"),
    ]
    return _clean_many(ctx, folders, "engine cache")


def _is_driver_leftover_folder(folder_path: str) -> bool:
    """Check if a driver folder appears to be leftover installation files."""
    if not os.path.isdir(folder_path):
        return False
    try:
        # Check if folder contains only safe-to-delete content:
        # - Log files (*.log, *.txt)
        # - Temp files (*.tmp, *.temp)
        # - Old version folders (e.g., 390.77, 460.89)
        # - Installer caches
        # If it has executable drivers (.sys, .dll in root), it might be active
        for entry in os.listdir(folder_path):
            entry_path = os.path.join(folder_path, entry)
            if os.path.isfile(entry_path):
                ext = os.path.splitext(entry)[1].lower()
                # Skip if there are driver binaries in root (might be active)
                if ext in (".sys", ".dll", ".inf", ".cat"):
                    return False
            # Subdirectories are inspected only for name pattern; version folders (e.g., "390.77") are safe
            # No extra check needed — top-level .sys/.dll guard is the safety gate
        return True
    except OSError:
        return False


def clean_driver_junk(ctx: TaskContext):
    folders = [
        os.path.join(_SYSTEMDRIVE_ROOT, "NVIDIA"),
        os.path.join(_SYSTEMDRIVE_ROOT, "AMD"),
        os.path.join(_SYSTEMDRIVE_ROOT, "ATI"),
        os.path.join(_SYSTEMDRIVE_ROOT, "Intel", "Driver"),
    ]
    verified_folders = []
    for folder in folders:
        if os.path.exists(folder) and _is_driver_leftover_folder(folder):
            verified_folders.append(folder)
        elif os.path.exists(folder):
            ctx.log(f"Skipping {folder} (may contain active drivers)")
    return _clean_many(ctx, verified_folders, "driver leftover")


def _get_uwp_package_folders() -> list[str]:
    """Enumerate UWP package folders dynamically from LocalAppData\\Packages."""
    packages_root = os.path.join(_LOCALAPPDATA, "Packages")
    if not os.path.isdir(packages_root):
        return []
    
    folders = []
    # Known Microsoft app prefixes we want to clean
    target_prefixes = (
        "Microsoft.Windows.Photos",
        "Microsoft.WindowsMaps",
        "Microsoft.XboxApp",
        "Microsoft.WindowsFeedbackHub",
        "Microsoft.GetHelp",
        "Microsoft.MicrosoftEdge",
        "Microsoft.YourPhone",
        "Microsoft.ZuneMusic",
        "Microsoft.ZuneVideo",
        "Microsoft.GamingApp",  # Xbox Game Bar / PC app
    )
    
    try:
        for pkg_name in os.listdir(packages_root):
            if any(pkg_name.startswith(prefix) for prefix in target_prefixes):
                pkg_path = os.path.join(packages_root, pkg_name)
                for sub in ("LocalCache", "TempState", "AC"):
                    sub_path = os.path.join(pkg_path, sub)
                    if os.path.isdir(sub_path):
                        folders.append(sub_path)
    except OSError:
        pass
    
    return folders


def clean_uwp_cache(ctx: TaskContext):
    """Clear UWP/Modern app caches (Photos, Maps, Xbox, Feedback Hub, etc.)."""
    folders = _get_uwp_package_folders()
    return _clean_many(ctx, folders, "UWP app cache")


def clean_user_temp_files(ctx: TaskContext):
    return _clean_many(ctx, [_TEMP], "user temp folder")


def clean_system_temp_files(ctx: TaskContext):
    return _clean_many(ctx, [f"{_WINDIR}\\Temp"], "system temp folder")


def clean_windows_update_cache(ctx: TaskContext):
    ctx.set_status("Clearing update download cache...")
    # Track which services were actually stopped so we don't start ones that were disabled
    # In dry-run, run_cmd returns 0 without stopping, so don't record
    stopped = []
    for svc in ("wuauserv", "bits"):
        rc = run_cmd(ctx, f"net stop {svc}", timeout=30)
        if rc == 0:
            stopped.append(svc)
    try:
        total = clean_folder_contents(ctx, f"{_WINDIR}\\SoftwareDistribution\\Download")
    finally:
        for svc in reversed(stopped):
            run_cmd(ctx, f"net start {svc}", timeout=30)
    return total


def clean_delivery_optimization(ctx: TaskContext):
    return clean_folder_contents(ctx, f"{_WINDIR}\\SoftwareDistribution\\DeliveryOptimization")


def clean_recycle_bin_and_dumps(ctx: TaskContext):
    dump_folders = [
        os.path.join(_LOCALAPPDATA, "CrashDumps"),
        # L4 fix: removed ProgramData\Microsoft\Windows\WER here — it's the
        # parent of WER\ReportQueue and WER\ReportArchive, which the
        # separate (also default-on) clean_error_reports() already walks.
        # Both tasks run by default in Quick Clean, so this was walking +
        # deleting the same report files twice on every run. Crash dumps
        # specifically live under CrashDumps/Minidump, not loose in WER
        # root, so nothing is lost by dropping this entry.
        f"{_WINDIR}\\Minidump",
    ]
    total = _clean_many(ctx, [f for f in dump_folders if os.path.isdir(f)], "crash dump")
    # Memory.dmp is a single file (not a directory) — it was previously skipped
    # by the os.path.isdir filter, so handle it explicitly here.
    memory_dump = f"{_WINDIR}\\Memory.dmp"
    if os.path.isfile(memory_dump):
        try:
            st = os.stat(memory_dump)
            size = st.st_size
            try:
                os.remove(memory_dump)
            except FileNotFoundError:
                # Race: file deleted between stat and remove
                size = 0
            except OSError:
                ctx.log(f"  (skipped locked dump: {memory_dump})")
                size = 0
            if size:
                total += size
                ctx.log(f"Cleaning crash dump: {memory_dump} ({size} bytes)")
        except OSError:
            ctx.log(f"  (skipped locked dump: {memory_dump})")
    ctx.log("Emptying Recycle Bin (silent)...")
    run_cmd(ctx, 'powershell -NoProfile -Command "Clear-RecycleBin -Force -ErrorAction SilentlyContinue"')
    return total


def clean_error_reports(ctx: TaskContext):
    folders = [
        os.path.join(_PROGRAMDATA, "Microsoft\\Windows\\WER\\ReportQueue"),
        os.path.join(_PROGRAMDATA, "Microsoft\\Windows\\WER\\ReportArchive"),
        os.path.join(_LOCALAPPDATA, "Microsoft\\Windows\\WER\\ReportQueue"),
    ]
    return _clean_many(ctx, folders, "error report")


def clean_inet_cache(ctx: TaskContext):
    folders = [
        os.path.join(_LOCALAPPDATA, "Microsoft\\Windows\\INetCache"),
        os.path.join(_LOCALAPPDATA, "Microsoft\\Windows\\WebCache"),
    ]
    return _clean_many(ctx, folders, "internet cache")


def clean_office_cache(ctx: TaskContext):
    folders = [
        os.path.join(_LOCALAPPDATA, "Microsoft\\Office\\16.0\\OfficeFileCache"),
        os.path.join(_LOCALAPPDATA, "Microsoft\\Office\\15.0\\OfficeFileCache"),
    ]
    return _clean_many(ctx, folders, "Office cache")


def clean_chk_fragments(ctx: TaskContext):
    # .CHK fragments from chkdsk — must use drive root (C:\), not bare "C:"
    total = 0
    patterns = [
        os.path.join(_SYSTEMDRIVE_ROOT, "*.chk"),
        os.path.join(_SYSTEMDRIVE_ROOT, "Found.*"),
    ]
    for pattern in patterns:
        for path in globmod.glob(pattern):
            try:
                if os.path.isfile(path):
                    # L2 fix: size must be added to `total` only AFTER
                    # os.remove() succeeds. The old order added it first —
                    # if remove() then raised (e.g. a locked file), the
                    # except below still swallowed it, but the size had
                    # already been counted, inflating the "space freed"
                    # total for files that were never actually deleted.
                    # (Matches the correct order already used in
                    # game_tasks._clean_files and utils.clean_folder_contents.)
                    size = os.path.getsize(path)
                    os.remove(path)
                    total += size
                elif os.path.isdir(path):
                    # Only clean Found.XXX directories (chkdsk fragment directories)
                    dirname = os.path.basename(path)
                    if dirname.startswith("Found."):
                        total += clean_folder_contents(ctx, path, remove_root=True)
            except OSError:
                continue
    return total


def clean_thumbnail_icon_cache(ctx: TaskContext):
    """Clear thumbcache/iconcache .db files. They are locked while Explorer
    runs, so Explorer is restarted around the clean.

    C3 fix: this used to kill Explorer, clean, then call
    run_cmd(ctx, "explorer.exe", timeout=10) to bring it back. run_cmd waits
    for the process to exit — but explorer.exe is meant to stay running as
    the shell, so it never exits, the call blocks for the full 10s, times
    out, and run_cmd's timeout-cleanup kills the process tree — which kills
    the Explorer we just relaunched. Net effect: this default-on task could
    leave the user with no taskbar/desktop about 10 seconds after "fixing"
    their icons. Fix: use the shared restart_explorer() helper, which
    launches Explorer detached and never waits on it.
    """
    explorer_dir = os.path.join(_LOCALAPPDATA, "Microsoft\\Windows\\Explorer")
    run_cmd(ctx, "taskkill /f /im explorer.exe", timeout=10)
    total = clean_folder_contents(ctx, explorer_dir, extensions=[".db"])
    # Always bring Explorer back — detached, non-blocking (see docstring above)
    restart_explorer(ctx)
    return total


def _check_browser_running(ctx: TaskContext) -> list[str]:
    """Check for running browser processes. Timeout + no hang, respects cancel."""
    import subprocess
    browsers = {
        "chrome.exe": "Chrome",
        "msedge.exe": "Edge",
        "brave.exe": "Brave",
        "vivaldi.exe": "Vivaldi",
        "opera.exe": "Opera",
        "firefox.exe": "Firefox",
    }
    running: list[str] = []
    if ctx.cancelled():
        return running
    try:
        # Use timeout to avoid indefinite hang if tasklist/WMI stalls; not routed via run_cmd deliberately
        # but we add timeout and handle TimeoutExpired gracefully.
        output = subprocess.check_output(
            "tasklist /fo csv /nh", shell=True, text=True, stderr=subprocess.DEVNULL, timeout=10
        )
        for line in output.splitlines():
            if ctx.cancelled():
                break
            parts = line.split(",")
            if parts and parts[0].strip('"').lower() in browsers:
                name = browsers[parts[0].strip('"').lower()]
                if name not in running:
                    running.append(name)
    except subprocess.TimeoutExpired:
        ctx.log("  (browser check timed out — skipping warning)")
    except Exception:
        pass
    return running


def clean_browser_caches(ctx: TaskContext):
    # Check for running browsers
    running = _check_browser_running(ctx)
    if running:
        ctx.log(f"Warning: The following browsers appear to be running: {', '.join(running)}")
        ctx.log("  Cache cleaning may be incomplete or cause issues. Close browsers for best results.")
    
    # All profiles, not just Default — plus root-level GPU shader caches
    total = 0
    base_patterns = [
        os.path.join(_LOCALAPPDATA, "Google\\Chrome\\User Data"),
        os.path.join(_LOCALAPPDATA, "Microsoft\\Edge\\User Data"),
        os.path.join(_LOCALAPPDATA, "BraveSoftware\\Brave-Browser\\User Data"),
        os.path.join(_LOCALAPPDATA, "Vivaldi\\User Data"),
    ]
    for base in base_patterns:
        if not base or not os.path.isabs(base) or not os.path.isdir(base):
            continue
        try:
            profiles = os.listdir(base)
        except OSError as e:
            ctx.log(f"  (skipped {base}: {e})")
            continue
        # Root-level GPU caches (shared across profiles)
        for sub in ("ShaderCache", "GrShaderCache", "GraphiteDawnCache"):
            p = os.path.join(base, sub)
            if os.path.isdir(p):
                ctx.log(f"Cleaning browser cache: {p}")
                total += clean_folder_contents(ctx, p)
        for profile in profiles:
            if ctx.cancelled():
                break
            for sub in ("Cache", "Code Cache", "GPUCache"):
                p = os.path.join(base, profile, sub)
                if os.path.isdir(p):
                    ctx.log(f"Cleaning browser cache: {p}")
                    total += clean_folder_contents(ctx, p)
    # Opera
    for op in [os.path.join(_APPDATA, "Opera Software\\Opera Stable\\Cache"),
               os.path.join(_APPDATA, "Opera Software\\Opera Stable\\Code Cache")]:
        if op and os.path.isabs(op) and os.path.isdir(op):
            ctx.log(f"Cleaning browser cache: {op}")
            total += clean_folder_contents(ctx, op)
    # Firefox
    ff_base = os.path.join(_LOCALAPPDATA, "Mozilla\\Firefox\\Profiles")
    if ff_base and os.path.isabs(ff_base) and os.path.isdir(ff_base):
        try:
            ff_profiles = os.listdir(ff_base)
        except OSError as e:
            ctx.log(f"  (skipped {ff_base}: {e})")
            ff_profiles = []
        for profile in ff_profiles:
            if ctx.cancelled():
                break
            cache2 = os.path.join(ff_base, profile, "cache2")
            if os.path.isdir(cache2):
                ctx.log(f"Cleaning Firefox cache: {cache2}")
                total += clean_folder_contents(ctx, cache2)
    return total


def clean_font_cache(ctx: TaskContext):
    ctx.set_status("Rebuilding font cache...")
    run_cmd(ctx, "net stop FontCache", timeout=30)
    total = clean_folder_contents(
        ctx, f"{_WINDIR}\\ServiceProfiles\\LocalService\\AppData\\Local\\FontCache"
    )
    run_cmd(ctx, "net start FontCache", timeout=30)
    return total


def clean_store_cache(ctx: TaskContext):
    ctx.log("Resetting Microsoft Store cache...")
    run_cmd(ctx, "wsreset.exe", timeout=60)
    return 0


def flush_dns(ctx: TaskContext):
    run_cmd(ctx, "ipconfig /flushdns")
    return 0


def purge_ram_working_sets(ctx: TaskContext):
    """Trim working sets of running processes so Windows frees unused RAM.

    Uses SetProcessWorkingSetSize(-1, -1) via P/Invoke — the same trim Windows
    does under memory pressure. Safe: trimmed pages just get paged back in on
    next use. Excludes system-critical pseudo processes.

    C2 fix history: the original hand-escaped the script into a shell=True
    string, and cmd.exe's quote-stripping broke on the mid-script `|` —
    it ran 'Idle' as a program and failed with rc=255, unchecked, while
    logging success.

    C2b fix (user-reported from a real run log): the shell=False rewrite
    still embedded literal `\\"` sequences in the PS SOURCE. PowerShell
    does not use backslash escapes — `\\"` ends the string early and the
    script dies with a ParserError (exit 1) on every run, which the
    rc check dutifully logged but the task still returned 0 and reported
    success. Fix: write the P/Invoke signature with PowerShell SINGLE
    quotes (no escaping needed at all), keep shell=False, and return
    None on failure so the runner counts it as an honest failure instead
    of a fake success.
    """
    ps_script = (
        "$sig = '[DllImport(\"kernel32.dll\")] public static extern bool "
        "SetProcessWorkingSetSize(IntPtr h, int min, int max);';"
        "$t = Add-Type -MemberDefinition $sig -Name Trim -Namespace Cleaner -PassThru;"
        "Get-Process | Where-Object { $_.ProcessName -notmatch "
        "'^(powershell|Idle|Registry|Memory Compression|csrss|dwm|explorer)$' } |"
        "ForEach-Object { try { $h = $_.Handle; $t::SetProcessWorkingSetSize($h, -1, -1) } catch {} }"
    )
    ctx.log("Trimming working sets of running processes (releases unused RAM)...")
    rc = run_cmd(
        ctx,
        ["powershell", "-NoProfile", "-Command", ps_script],
        shell=False,
        timeout=120,
    )
    if rc == 0:
        ctx.log("Memory trim complete — Windows will reclaim pages as needed.")
        return 0
    ctx.log(f"  ! Memory trim failed (exit code {rc}) — reporting as a failed task.")
    raise RuntimeError(f"Memory trim command failed (exit code {rc}).")


def clean_old_logs(ctx: TaskContext):
    folders = [f"{_WINDIR}\\Logs\\CBS", f"{_WINDIR}\\Logs\\DISM",
               f"{_WINDIR}\\Logs\\WindowsUpdate"]
    return _clean_many(ctx, folders, "system log")


def clean_prefetch(ctx: TaskContext):
    return clean_folder_contents(ctx, f"{_WINDIR}\\Prefetch")


def clean_windows_update_leftovers(ctx: TaskContext):
    """Remove Windows Update / upgrade leftovers (Sophia Script's list).

    Targets: Windows.old, $Windows.~BT, $WinREAgent, $GetCurrent, $SysReset,
    $Windows.~WS, ESD, C:\\Intel, C:\\PerfLogs. These are installer debris and
    safe to delete AFTER an upgrade completed (Windows itself offers to remove
    most of them via Storage Sense after 10 days).
    """
    folders = [
        os.path.join(_SYSTEMDRIVE_ROOT, "Windows.old"),
        os.path.join(_SYSTEMDRIVE_ROOT, "$Windows.~BT"),
        os.path.join(_SYSTEMDRIVE_ROOT, "$Windows.~WS"),
        os.path.join(_SYSTEMDRIVE_ROOT, "$WinREAgent"),
        os.path.join(_SYSTEMDRIVE_ROOT, "$GetCurrent"),
        os.path.join(_SYSTEMDRIVE_ROOT, "$SysReset"),
        os.path.join(_SYSTEMDRIVE_ROOT, "ESD"),
        os.path.join(_SYSTEMDRIVE_ROOT, "Intel"),
        os.path.join(_SYSTEMDRIVE_ROOT, "PerfLogs"),
    ]
    total = 0
    for folder in folders:
        if not folder or not os.path.isabs(folder):
            continue
        if os.path.exists(folder):
            ctx.log(f"Cleaning update leftover: {folder}")
            total += clean_folder_contents(ctx, folder, remove_root=True)
    if total > 0:
        ctx.log(f"Removed {total / 1024 / 1024:.0f} MB of Windows Update leftovers.")
    else:
        ctx.log("No update leftovers found.")
    return total


def clean_activity_traces(ctx: TaskContext):
    """Clear user activity traces (Privacy.sexy-style cleanup).

    Removes jump lists (recent files), Run-dialog MRU, typed paths, and recent
    docs. Purely local privacy hygiene — no functionality lost beyond history.
    """
    import subprocess
    total = 0
    # Jump lists (AutomaticDestinations / CustomDestinations)
    recent = os.path.join(_APPDATA, "Microsoft\\Windows\\Recent")
    for sub in ("AutomaticDestinations", "CustomDestinations"):
        d = os.path.join(recent, sub)
        if os.path.isdir(d):
            ctx.log(f"Clearing jump lists: {d}")
            total += clean_folder_contents(ctx, d)
    # Run-dialog MRU + typed paths + recent docs (registry)
    from app.utils import reg_delete_key
    reg_delete_key(ctx, "HKCU", "Software\\Microsoft\\Windows\\CurrentVersion\\Explorer\\RunMRU")
    reg_delete_key(ctx, "HKCU", "Software\\Microsoft\\Windows\\CurrentVersion\\Explorer\\TypedPaths")
    # Recent items shortcuts
    if os.path.isdir(recent):
        ctx.log(f"Clearing recent items: {recent}")
        total += clean_folder_contents(ctx, recent, extensions=[".lnk"])
    ctx.log("Activity traces cleared.")
    return total


def clean_winget_cache(ctx: TaskContext):
    """winget's own log + temp dirs — they grow forever on update-heavy
    machines. (New task: 'Clear WinGet Cache'.) Only touches the App
    Installer package's TempState/diag logs and %TEMP%\\WinGet — never
    C:\\Windows\\Installer (deleting from there breaks MSI uninstall/repair;
    even $PatchCache$ is deliberately left alone in a layman tool)."""
    pkg = os.path.join(_LOCALAPPDATA, "Packages", "Microsoft.DesktopAppInstaller_8wekyb3d8bbwe")
    folders = [
        os.path.join(pkg, "TempState"),
        os.path.join(pkg, "LocalState", "DiagOutputDir"),   # winget's ever-growing text logs
        os.path.join(_TEMP, "WinGet"),
    ]
    return _clean_many(ctx, folders, "winget cache")


# Cleans disk via cleanmgr (no ResetBase — keeps your ability to roll back updates) #
def run_disk_cleanup(ctx: TaskContext):
    ctx.log("[Clean] Disk Cleanup - Run")
    _sd = os.environ.get("SYSTEMDRIVE", "C:")
    if len(_sd) == 2 and _sd[1] == ":":
        _sd_root = _sd + "\\"
    else:
        _sd_root = _sd if _sd.endswith("\\") else _sd + "\\"
    ctx.log(f"$ cleanmgr.exe /d {_sd_root[:-1]} /VERYLOWDISK")
    run_cmd(ctx, f"cleanmgr.exe /d {_sd_root[:-1]} /VERYLOWDISK", timeout=120)
    ctx.log("Disk Cleanup complete.")
    return 0

# Removes temp files via PowerShell #
def remove_temp_files_deep(ctx: TaskContext):
    ctx.log("[Clean] Temporary Files - Remove")
    ps_cmd1 = 'powershell -NoProfile -Command "Remove-Item -Path \\"$Env:Temp\\*\\" -Recurse -Force -ErrorAction SilentlyContinue"'
    ps_cmd2 = 'powershell -NoProfile -Command "Remove-Item -Path \\"$Env:SystemRoot\\Temp\\*\\" -Recurse -Force -ErrorAction SilentlyContinue"'
    ctx.log("$ Remove-Item -Path \"$Env:Temp\\*\" -Recurse -Force")
    run_cmd(ctx, ps_cmd1)
    ctx.log("$ Remove-Item -Path \"$Env:SystemRoot\\Temp\\*\" -Recurse -Force")
    run_cmd(ctx, ps_cmd2)
    # audit fix: both _clean_many return values were discarded and the task
    # reported 0 bytes freed. The fallback walkers do the real accounting
    # (they stat+remove and count) — the PS pass above only catches what
    # escaped between runs.
    total = _clean_many(ctx, [_TEMP], "user temp folder (fallback)")
    total += _clean_many(ctx, [f"{_WINDIR}\\Temp"], "system temp folder (fallback)")
    return total

# Removes Windows bloat #
def remove_windows_bloat(ctx: TaskContext):
    """Remove common preinstalled bloat. Gamer-safe: keeps all Xbox/GamingServices
    packages (Game Pass) and codec/media extensions (video playback).
    Package names verified: Clipchamp family is 'Clipchamp.Clipchamp' (store id 9P1J8S7CCWWT)."""
    ctx.log("[Clean] Remove Windows Bloat")
    bloat = [
        "Clipchamp.Clipchamp",             # Clipchamp (verified real name — old 'Microsoft.Clipchamp' matched nothing)
        "Clipchamp.Clipchamp.ShellExtension",
        "Microsoft.BingWeather",           # MSN Weather
        "Microsoft.BingNews",              # MSN News
        "Microsoft.Microsoft3DViewer",     # 3D Viewer
        "Microsoft.MicrosoftSolitaireCollection",
        "Microsoft.MixedReality.Portal",
        "Microsoft.Todos",
        "Microsoft.People",
        "Microsoft.Getstarted",            # Microsoft Tips
        "Microsoft.WindowsFeedbackHub",   # (only app users report issues with)
    ]
    for pkg in bloat:
        run_cmd(ctx, f'powershell -NoProfile -Command "Get-AppxPackage -Name \\"{pkg}\\" -AllUsers | Remove-AppxPackage -ErrorAction SilentlyContinue"')
        ctx.log(f"  Checked {pkg}")
    # TikTok registers under different publisher names; catch-all fallback
    run_cmd(ctx, 'powershell -NoProfile -Command "Get-AppxPackage -AllUsers | Where-Object {$_.Name -like \\"*TikTok*\\"} | Remove-AppxPackage -ErrorAction SilentlyContinue"')
    # Note: blocking auto-reinstall of consumer suggestions (DisableWindowsConsumerFeatures)
    # lives in the Tweak tab's "Stop Windows Ads & Tips" task instead, since that task has
    # a working revert. This Clean-tab task only removes apps (Store-reinstallable, no
    # one-way registry writes left behind).
    ctx.log("Bloat removal complete.")
    return 0


def clean_event_logs(ctx: TaskContext):
    """Clear Windows Event Viewer logs (diagnostic history only — the logs
    start fresh; fixes bloated evtx files, admin)."""
    collected: "list[str]" = []
    run_cmd(ctx, 'wevtutil el', shell=True, timeout=120, collect=collected)
    cleared = 0
    for name in collected:
        name = name.strip()
        if not name:
            continue
        rc = run_cmd(ctx, f'wevtutil cl "{name}"', shell=True, timeout=120)
        if rc == 0:
            cleared += 1
        else:
            ctx.log(f"  (skipped in-use log: {name})")
    ctx.log(f"Cleared {cleared} event logs.")
    return 0


def clean_defender_history(ctx: TaskContext):
    """Clear Defender's protection-history leftovers (stale threat entries
    that haunt the Security UI; the engine rescans cleanly, admin)."""
    folders = [
        os.path.join(_PROGRAMDATA, "Microsoft\\Windows Defender\\Scans\\History\\Service"),
    ]
    return _clean_many(ctx, folders, "Defender history")


def _steam_root() -> str:
    """Steam install dir: registry first, default path as fallback."""
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam") as k:
            path, _ = winreg.QueryValueEx(k, "SteamPath")
            if path and os.path.isdir(path):
                return path
    except Exception:
        pass
    return os.path.join(os.environ.get("ProgramFiles(x86)", "C:\\Program Files (x86)"), "Steam")


def clean_steam_download_cache(ctx: TaskContext):
    """Clear Steam's depot manifest cache + stale appinfo (fixes phantom
    'update required' states; Steam re-downloads them on launch)."""
    root = _steam_root()
    total = _clean_many(ctx, [os.path.join(root, "depotcache")], "Steam depot cache")
    appinfo = os.path.join(root, "appcache", "appinfo.vdf")
    try:
        if os.path.isfile(appinfo):
            total += os.path.getsize(appinfo)
            os.remove(appinfo)
            ctx.log(f"Removed stale app manifest: {appinfo}")
    except OSError as exc:
        ctx.log(f"  (kept appinfo.vdf: {exc})")
    return total


def clean_dev_caches(ctx: TaskContext):
    """Clean VS Code / npm / pip caches (regenerable blobs for the
    modding-and-AI crowd; extensions and packages untouched)."""
    folders = [
        os.path.join(_APPDATA, "Code\\Cache"),
        os.path.join(_APPDATA, "Code\\CachedData"),
        os.path.join(_APPDATA, "Code\\GPUCache"),
        os.path.join(_LOCALAPPDATA, "npm-cache\\_cacache"),
        os.path.join(_LOCALAPPDATA, "pip\\Cache"),
    ]
    return _clean_many(ctx, folders, "dev caches")


def clean_package_manager_caches(ctx: TaskContext):
    """Clean NuGet / Cargo / Gradle download caches (regenerable blobs;
    installed packages, toolchains and projects untouched — the next build
    just re-downloads what it needs)."""
    userprofile = os.environ.get("USERPROFILE", "")
    folders = [
        # NuGet HTTP caches (NOT ~/.nuget/packages — that is the package
        # store itself; only the throwaway HTTP layers go here)
        os.path.join(_LOCALAPPDATA, "NuGet\\v3-cache"),
        os.path.join(_LOCALAPPDATA, "NuGet\\http-cache"),
        os.path.join(_LOCALAPPDATA, "NuGet\\plugins-cache"),
        # Cargo registry download cache (extracted sources under
        # registry/src stay, so builds keep working offline)
        os.path.join(userprofile, ".cargo\\registry\\cache") if userprofile else "",
        os.path.join(userprofile, ".cargo\\registry\\index") if userprofile else "",
        # Gradle build cache + transform cache (project files untouched)
        os.path.join(userprofile, ".gradle\\caches\\build-cache-1") if userprofile else "",
        os.path.join(userprofile, ".gradle\\caches\\transforms-4") if userprofile else "",
    ]
    return _clean_many(ctx, [f for f in folders if f], "package manager caches")


def clean_terminal_history(ctx: TaskContext):
    """Clear PowerShell / Terminal command history (privacy hygiene only —
    no settings, profiles or scripts touched)."""
    files = [
        os.path.join(_APPDATA, "Microsoft\\Windows\\PowerShell\\PSReadLine\\ConsoleHost_history.txt"),
        # Legacy per-host history + VS Code's integrated-terminal history
        os.path.join(_APPDATA, "Microsoft\\Windows\\PowerShell\\PSReadLine\\Visual Studio Code Host_history.txt"),
    ]
    total = 0
    for path in files:
        try:
            if os.path.isfile(path):
                total += os.path.getsize(path)
                os.remove(path)
                ctx.log(f"Removed terminal history: {path}")
        except OSError as exc:
            ctx.log(f"  (kept history file: {exc})")
    if not total:
        ctx.log("No terminal history found.")
    return total


from app.tasks import Task  # noqa: E402

TASKS = [
    Task("shader_cache", "Clear Shader Cache", "Removes temp graphics files that help fix stutter", clean_shader_cache, default=True, admin_required=False, column=0),
    Task("launcher_cache", "Clean Launchers & Chat", "Clears every game store plus Discord, Slack, Teams, Spotify junk", clean_launcher_cache, default=True, admin_required=False, column=0),
    Task("engine_cache", "Clean Engine Cache", "Removes leftover Unreal/Unity build files", clean_engine_cache, default=True, admin_required=False, column=0),
    Task("driver_junk", "Remove Old Drivers", "Deletes old NVIDIA/AMD installer leftovers", clean_driver_junk, default=True, admin_required=True, column=0),
    Task("user_temp_files", "Empty User Temp Files", "Deletes leftover temp files Windows left behind", clean_user_temp_files, default=True, admin_required=False, column=0),
    Task("system_temp_files", "Empty System Temp Files", "Cleans system temp files no longer needed", clean_system_temp_files, default=True, admin_required=True, column=0),
    Task("win_update_cache", "Fix Update Cache", "Fixes Windows Update when downloads get stuck", clean_windows_update_cache, default=True, admin_required=True, column=0),
    Task("delivery_optimization", "Clear Update Share Cache", "Removes update copies kept to share with other PCs", clean_delivery_optimization, default=True, admin_required=True, column=0),
    Task("inet_cache", "Clear Internet Cache", "Clears old internet temp files", clean_inet_cache, default=True, admin_required=False, column=0),
    Task("recycle_bin", "Empty Bin & Crash Reports", "Empties trash and removes old crash dumps", clean_recycle_bin_and_dumps, default=True, admin_required=True, column=1),
    Task("error_reports", "Clear Error Reports", "Deletes old Windows error reports", clean_error_reports, default=True, admin_required=False, column=1),
    Task("thumbnail_cache", "Fix Blurry Icons", "Rebuilds icons, fixes missing thumbnails", clean_thumbnail_icon_cache, default=True, admin_required=False, column=1),
    Task("chk_fragments", "Remove Disk Fragments", "Deletes leftover files from disk checks", clean_chk_fragments, default=True, admin_required=True, column=1),
    Task("old_logs", "Clear System Logs", "Removes old Windows logs", clean_old_logs, default=True, admin_required=True, column=1),
    Task("dns_flush", "Fix Internet (DNS)", "Clears internet cache to fix sites not loading", flush_dns, default=True, admin_required=False, column=1),
    Task("ram_purge", "Free Up RAM", "Asks Windows to free unused memory", purge_ram_working_sets, default=False, admin_required=False, column=1),
    Task("update_leftovers", "Clear Update Leftovers", "Run this only once your PC has been running fine for a few days after a big Windows update", clean_windows_update_leftovers, default=False, admin_required=True, risk="ADVANCED", column=0),
    Task("activity_traces", "Clear Activity Traces", "Clears recent-files and jump-list history for privacy", clean_activity_traces, default=False, admin_required=False, column=0),
    Task("prefetch", "Clear Prefetch Files", "Clears prefetch data, usually not needed", clean_prefetch, default=False, admin_required=True, risk="ADVANCED", column=1),
    Task("disk_cleanup_deep", "Deep Disk Cleanup", "Deep cleans old Windows update files", run_disk_cleanup, default=False, admin_required=True, risk="ADVANCED", column=0),
    Task("browser_cache", "Clear Browser Cache", "Clears browser temp files, keeps passwords", clean_browser_caches, default=False, admin_required=False, column=1),
    Task("office_cache", "Clear Office Cache", "Removes Office temporary files", clean_office_cache, default=False, admin_required=False, column=1),
    Task("uwp_cache", "Clear UWP App Caches", "Clears Windows apps temp files like Photos", clean_uwp_cache, default=False, admin_required=False, column=1),
    Task("font_cache", "Fix Broken Fonts", "Rebuilds fonts to fix garbled text", clean_font_cache, default=False, admin_required=True, column=1),
    Task("store_cache", "Fix Store", "Resets Store if apps won't download", clean_store_cache, default=False, admin_required=False, column=1),
    Task("temp_deep_clean", "Deep Temp Clean", "Deep cleans temp files with PowerShell", remove_temp_files_deep, default=False, admin_required=True, column=0),
    Task("remove_bloat", "Remove Windows Bloat", "Removes Clipchamp, MSN apps, TikTok and other preinstalled junk", remove_windows_bloat, default=False, admin_required=True, column=1),
    Task("winget_cache", "Clear WinGet Cache", "Removes leftover installer files and logs from Windows' app downloader", clean_winget_cache, default=False, admin_required=False, column=1),
    Task("event_logs", "Clear Event Viewer Logs", "Wipes old Windows diagnostic logs so they start fresh", clean_event_logs, default=False, admin_required=True, column=1),
    Task("defender_history", "Clear Defender History", "Removes stale protection-history entries that haunt the Security app", clean_defender_history, default=False, admin_required=True, column=1),
    Task("steam_depot", "Clean Steam Download Cache", "Clears Steam's manifest cache that causes phantom update states", clean_steam_download_cache, default=False, admin_required=False, column=0),
    Task("dev_caches", "Clean Dev Caches", "Clears VS Code, npm and pip caches for modders and AI tinkerers", clean_dev_caches, default=False, admin_required=False, column=1),
    Task("pkg_caches", "Clean Package Caches", "Clears NuGet, Cargo and Gradle download caches; projects untouched", clean_package_manager_caches, default=False, admin_required=False, column=1),
    Task("terminal_history", "Clear Terminal History", "Clears PowerShell command history for privacy; settings untouched", clean_terminal_history, default=False, admin_required=False, column=0),
]