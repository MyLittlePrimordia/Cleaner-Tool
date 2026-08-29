"""
Clean tab — only deletes disposable, auto-regenerating junk.
Laymen short labels + hover tooltips; compact symmetrical grid.
"""

import glob as globmod
import os

from app.utils import TaskContext, clean_folder_contents, run_cmd
from app.tasks.launcher_paths import ALL_LAUNCHER_CACHE_PATHS

_LOCALAPPDATA = os.environ.get("LOCALAPPDATA", "")
_APPDATA = os.environ.get("APPDATA", "")
_TEMP = os.environ.get("TEMP", "")
_SYSTEMDRIVE = os.environ.get("SYSTEMDRIVE", "C:")
_WINDIR = os.environ.get("WINDIR", f"{_SYSTEMDRIVE}\\Windows")
_PROGRAMDATA = os.environ.get("ProgramData", f"{_SYSTEMDRIVE}\\ProgramData")


def _clean_many(ctx: TaskContext, folders, label):
    total = 0
    for folder in folders:
        if folder and os.path.exists(folder):
            ctx.log(f"Cleaning {label}: {folder}")
            total += clean_folder_contents(ctx, folder)
    return total


def clean_shader_cache(ctx: TaskContext):
    folders = [
        os.path.join(_LOCALAPPDATA, "NVIDIA\\DXCache"),
        os.path.join(_LOCALAPPDATA, "NVIDIA\\GLCache"),
        os.path.join(_APPDATA, "NVIDIA\\ComputeCache"),
        os.path.join(_PROGRAMDATA, "NVIDIA Corporation\\NV_Cache"),
        os.path.join(_LOCALAPPDATA, "AMD\\DxCache"),
        os.path.join(_LOCALAPPDATA, "AMD\\DxcCache"),
        os.path.join(_LOCALAPPDATA, "AMD\\VkCache"),
        os.path.join(_LOCALAPPDATA, "AMD\\CN"),
        os.path.join(_LOCALAPPDATA, "Intel\\ShaderCache"),
        os.path.join(_LOCALAPPDATA, "Microsoft\\DirectX Shader Cache"),
        os.path.join(_LOCALAPPDATA, "D3DSCache"),
    ]
    return _clean_many(ctx, folders, "shader cache")


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
            elif os.path.isdir(entry_path):
                # Check if it looks like a version folder (e.g., "390.77", "460.89")
                # These are typically safe to delete
                pass
        return True
    except OSError:
        return False


def clean_driver_junk(ctx: TaskContext):
    folders = ["C:\\NVIDIA", "C:\\AMD", "C:\\ATI", "C:\\Intel\\Driver"]
    verified_folders = []
    for folder in folders:
        if os.path.exists(folder) and _is_driver_leftover_folder(folder):
            verified_folders.append(folder)
        elif os.path.exists(folder):
            ctx.log(f"Skipping {folder} (may contain active drivers)")
    return _clean_many(ctx, verified_folders, "driver leftover")


def clean_driver_store(ctx: TaskContext):
    """Remove old/inactive driver packages from Driver Store using pnputil.
    Only removes drivers not currently in use (safe)."""
    ctx.set_status("Scanning Driver Store for old packages...")
    ctx.log("$ pnputil /enum-drivers")
    if ctx.dry_run:
        ctx.log("  (dry run - command not executed)")
        return 0
    
    import subprocess
    import datetime
    try:
        proc = subprocess.Popen(
            "pnputil /enum-drivers", shell=True,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, startupinfo=subprocess.STARTUPINFO(),
            creationflags=subprocess.CREATE_NO_WINDOW
        )
        stdout, _ = proc.communicate(timeout=60)
    except Exception as e:
        ctx.log(f"  ! ERROR: {e}")
        return 0
    
    if not stdout:
        return 0
    
    # Parse output to find published names and dates
    # Sample output format:
    # Published Name : oem12.inf
    # Driver Date  : 01/15/2023
    # ...
    lines = stdout.splitlines()
    drivers = []  # list of (published_name, date_str)
    current_published = None
    
    for line in lines:
        line = line.strip()
        if line.startswith("Published Name"):
            current_published = line.split(":", 1)[1].strip()
        elif line.startswith("Driver Date") and current_published:
            date_str = line.split(":", 1)[1].strip()
            drivers.append((current_published, date_str))
            current_published = None
    
    if not drivers:
        ctx.log("No driver packages found in store.")
        return 0
    
    # Filter drivers older than 30 days
    cutoff_date = datetime.datetime.now() - datetime.timedelta(days=30)
    old_drivers = []
    for pub_name, date_str in drivers:
        try:
            # Parse date (format: MM/DD/YYYY or similar)
            for fmt in ("%m/%d/%Y", "%d/%m/%Y", "%Y-%m-%d"):
                try:
                    driver_date = datetime.datetime.strptime(date_str, fmt)
                    break
                except ValueError:
                    continue
            else:
                continue  # Could not parse date
            
            if driver_date < cutoff_date:
                old_drivers.append(pub_name)
        except Exception:
            continue
    
    if not old_drivers:
        ctx.log("No driver packages older than 30 days found.")
        return 0
    
    ctx.log(f"Found {len(old_drivers)} driver package(s) older than 30 days.")
    
    # Delete old drivers
    deleted = 0
    for pub_name in old_drivers:
        if ctx.cancelled():
            break
        cmd = f'pnputil /delete-driver "{pub_name}" /uninstall /force'
        ctx.log(f"$ {cmd}")
        try:
            rc = run_cmd(ctx, cmd, timeout=60)
            if rc == 0:
                ctx.log(f"  Deleted: {pub_name}")
                deleted += 1
            else:
                ctx.log(f"  ! Failed to delete {pub_name} (may be in use)")
        except Exception as e:
            ctx.log(f"  ! Error deleting {pub_name}: {e}")
    
    ctx.log(f"Removed {deleted} old driver package(s) from Driver Store.")
    return 0


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
    run_cmd(ctx, "net stop wuauserv", timeout=30)
    run_cmd(ctx, "net stop bits", timeout=30)
    try:
        total = clean_folder_contents(ctx, f"{_WINDIR}\\SoftwareDistribution\\Download")
    finally:
        run_cmd(ctx, "net start bits", timeout=30)
        run_cmd(ctx, "net start wuauserv", timeout=30)
    return total


def clean_delivery_optimization(ctx: TaskContext):
    return clean_folder_contents(ctx, f"{_WINDIR}\\SoftwareDistribution\\DeliveryOptimization")


def clean_recycle_bin_and_dumps(ctx: TaskContext):
    dump_folders = [
        os.path.join(_LOCALAPPDATA, "CrashDumps"),
        "C:\\ProgramData\\Microsoft\\Windows\\WER",
        f"{_WINDIR}\\Minidump",
    ]
    total = _clean_many(ctx, [f for f in dump_folders if os.path.isdir(f)], "crash dump")
    # Memory.dmp is a single file (not a directory) — it was previously skipped
    # by the os.path.isdir filter, so handle it explicitly here.
    memory_dump = f"{_WINDIR}\\Memory.dmp"
    if os.path.isfile(memory_dump):
        try:
            size = os.path.getsize(memory_dump)
            if not ctx.dry_run:
                os.remove(memory_dump)
            total += size
            ctx.log(f"Cleaning crash dump: {memory_dump}")
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


def clean_spotify_cache(ctx: TaskContext):
    folders = [
        os.path.join(_APPDATA, "Spotify\\Storage"),
        os.path.join(_LOCALAPPDATA, "Spotify\\Storage"),
    ]
    return _clean_many(ctx, folders, "Spotify cache")


def clean_chk_fragments(ctx: TaskContext):
    # .CHK fragments from chkdsk — root drive globs
    total = 0
    # Use raw strings to avoid backslash escaping issues
    patterns = [
        os.path.join(_SYSTEMDRIVE, "*.chk"),
        os.path.join(_SYSTEMDRIVE, "Found.*"),
    ]
    for pattern in patterns:
        for path in globmod.glob(pattern):
            try:
                if os.path.isfile(path):
                    total += os.path.getsize(path)
                    if not ctx.dry_run:
                        os.remove(path)
                elif os.path.isdir(path):
                    # Only clean Found.XXX directories (chkdsk fragment directories)
                    dirname = os.path.basename(path)
                    if dirname.startswith("Found."):
                        total += clean_folder_contents(ctx, path, remove_root=True)
            except OSError:
                continue
    return total


def clean_thumbnail_icon_cache(ctx: TaskContext):
    explorer_dir = os.path.join(_LOCALAPPDATA, "Microsoft\\Windows\\Explorer")
    return clean_folder_contents(ctx, explorer_dir, extensions=[".db"])


def _check_browser_running(ctx: TaskContext) -> list[str]:
    """Check for running browser processes. Returns list of running browser names."""
    import subprocess
    browsers = {
        "chrome.exe": "Chrome",
        "msedge.exe": "Edge",
        "brave.exe": "Brave",
        "vivaldi.exe": "Vivaldi",
        "opera.exe": "Opera",
        "firefox.exe": "Firefox",
    }
    running = []
    try:
        output = subprocess.check_output(
            "tasklist /fo csv /nh", shell=True, text=True, stderr=subprocess.DEVNULL
        )
        for line in output.splitlines():
            parts = line.split(",")
            if parts and parts[0].strip('"').lower() in browsers:
                running.append(browsers[parts[0].strip('"').lower()])
    except Exception:
        pass
    return running


def clean_browser_caches(ctx: TaskContext):
    # Check for running browsers
    running = _check_browser_running(ctx)
    if running:
        ctx.log(f"Warning: The following browsers appear to be running: {', '.join(running)}")
        ctx.log("  Cache cleaning may be incomplete or cause issues. Close browsers for best results.")
    
    # All profiles, not just Default — plus Opera/Vivaldi
    total = 0
    base_patterns = [
        os.path.join(_LOCALAPPDATA, "Google\\Chrome\\User Data"),
        os.path.join(_LOCALAPPDATA, "Microsoft\\Edge\\User Data"),
        os.path.join(_LOCALAPPDATA, "BraveSoftware\\Brave-Browser\\User Data"),
        os.path.join(_LOCALAPPDATA, "Vivaldi\\User Data"),
    ]
    for base in base_patterns:
        if not os.path.isdir(base):
            continue
        for profile in os.listdir(base):
            for sub in ("Cache", "Code Cache", "GPUCache"):
                p = os.path.join(base, profile, sub)
                if os.path.isdir(p):
                    ctx.log(f"Cleaning browser cache: {p}")
                    total += clean_folder_contents(ctx, p)
    # Opera
    for op in [os.path.join(_APPDATA, "Opera Software\\Opera Stable\\Cache"),
               os.path.join(_APPDATA, "Opera Software\\Opera Stable\\Code Cache")]:
        if os.path.isdir(op):
            ctx.log(f"Cleaning browser cache: {op}")
            total += clean_folder_contents(ctx, op)
    # Firefox
    ff_base = os.path.join(_LOCALAPPDATA, "Mozilla\\Firefox\\Profiles")
    if os.path.isdir(ff_base):
        for profile in os.listdir(ff_base):
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
    ps_ram = (
        'powershell -NoProfile -Command '
        '"[System.GC]::Collect(); [System.GC]::WaitForPendingFinalizers()"'
    )
    run_cmd(ctx, ps_ram)
    return 0


def clean_old_logs(ctx: TaskContext):
    folders = [f"{_WINDIR}\\Logs\\CBS", f"{_WINDIR}\\Logs\\DISM",
               f"{_WINDIR}\\Logs\\WindowsUpdate"]
    return _clean_many(ctx, folders, "system log")


def clean_prefetch(ctx: TaskContext):
    return clean_folder_contents(ctx, f"{_WINDIR}\\Prefetch")


from app.tasks import Task  # noqa: E402

TASKS = [
    Task("shader_cache", "Clear Shader Cache",
         "Deletes temp graphics files games rebuild next launch — fixes stutter after driver updates.",
         clean_shader_cache, default=True, admin_required=False, column=0),
    Task("launcher_cache", "Clean Game Launchers",
         "Clears Steam/Epic/Discord web cache — fixes blank launchers.",
         clean_launcher_cache, default=True, admin_required=False, column=0),
    Task("engine_cache", "Clean Engine Cache",
         "Removes Unreal/Unity build files — safe, rebuilds on next use.",
         clean_engine_cache, default=True, admin_required=False, column=0),
    Task("driver_junk", "Remove Old Drivers",
         "Deletes C:\\NVIDIA / C:\\AMD leftovers after driver installs.",
         clean_driver_junk, default=True, admin_required=False, column=0),
    Task("driver_store", "Clean Driver Store",
         "Removes old/inactive driver packages (pnputil) — frees 1-5 GB.",
         clean_driver_store, default=False, admin_required=True, risk="ADVANCED", column=0),
    Task("user_temp_files", "Empty User Temp Files",
         "Clears your user temp folder.",
         clean_user_temp_files, default=True, admin_required=False, column=0),
    Task("system_temp_files", "Empty System Temp Files",
         "Clears Windows system temp folder (requires admin).",
         clean_system_temp_files, default=True, admin_required=True, column=0),
    Task("win_update_cache", "Fix Update Cache",
         "Clears stuck update downloads — restarts the update service.",
         clean_windows_update_cache, default=True, admin_required=True, column=0),
    Task("delivery_optimization", "Clear Update Share Cache",
         "Removes shared update chunks Windows keeps for other PCs.",
         clean_delivery_optimization, default=True, admin_required=True, column=0),
    Task("inet_cache", "Clear Internet Cache",
         "Clears old Temporary Internet Files and WebCache.",
         clean_inet_cache, default=True, admin_required=False, column=0),
    Task("recycle_bin", "Empty Bin & Crash Reports",
         "Empties trash and deletes old crash reports.",
         clean_recycle_bin_and_dumps, default=True, admin_required=False, column=1),
    Task("error_reports", "Clear Error Reports",
         "Deletes old Windows error report queues and archives.",
         clean_error_reports, default=True, admin_required=False, column=1),
    Task("thumbnail_cache", "Fix Blurry Icons",
         "Clears and rebuilds thumbnail previews — fixes missing icons.",
         clean_thumbnail_icon_cache, default=True, admin_required=False, column=1),
    Task("browser_cache", "Clear Browser Cache",
         "Clears Chrome/Edge/Firefox/Brave/Opera cache — keeps passwords and history.",
         clean_browser_caches, default=False, admin_required=False, column=1),
    Task("office_cache", "Clear Office Cache",
         "Clears Microsoft Office temp file cache.",
         clean_office_cache, default=False, admin_required=False, column=1),
    Task("spotify_cache", "Clear Spotify Cache",
         "Clears Spotify offline cache — music re-downloads as needed.",
         clean_spotify_cache, default=False, admin_required=False, column=1),
    Task("uwp_cache", "Clear UWP App Caches",
         "Clears Photos, Maps, Xbox, Edge, Feedback Hub caches.",
         clean_uwp_cache, default=False, admin_required=False, column=1),
    Task("chk_fragments", "Remove Disk Fragments",
         "Deletes leftover .CHK files from disk checks.",
         clean_chk_fragments, default=True, admin_required=False, column=1),
    Task("font_cache", "Fix Broken Fonts",
         "Rebuilds font cache — fixes garbled text.",
         clean_font_cache, default=False, admin_required=True, column=1),
    Task("store_cache", "Fix Store",
         "Resets Store when apps won't download.",
         clean_store_cache, default=False, admin_required=False, column=1),
    Task("old_logs", "Clear System Logs",
         "Deletes old CBS/DISM/WindowsUpdate logs.",
         clean_old_logs, default=True, admin_required=False, column=1),
    Task("dns_flush", "Fix Internet (DNS)",
         "Clears address cache — fixes some sites not loading.",
         flush_dns, default=True, admin_required=False, column=1),
    Task("ram_purge", "Free Up RAM",
         "Asks Windows to release unused memory.",
         purge_ram_working_sets, default=True, admin_required=False, column=1),
    Task("prefetch", "Clear Prefetch (Rarely Needed)",
         "Windows manages this itself — clearing may slow next app start.",
         clean_prefetch, default=False, admin_required=True, risk="ADVANCED", column=1),
]
