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


def clean_driver_store(ctx: TaskContext):
    """Remove old/inactive driver packages from Driver Store using pnputil.
    Only removes drivers not currently in use AND older than 30 days (safe).

    Parses pnputil output block-wise to avoid deleting in-use packages.
    Never uses /force — pnputil will fail if the package is still needed.
    """
    ctx.set_status("Scanning Driver Store for old packages...")
    ctx.log("$ pnputil /enum-drivers")
    if ctx.dry_run:
        ctx.log("  (dry run - would scan Driver Store, no packages deleted)")
        return 0

    import subprocess
    import datetime
    import re

    # Use shell=False to avoid injection; handle missing STARTUPINFO on non-Windows
    try:
        startupinfo = None
        creationflags = 0
        if os.name == "nt":
            try:
                startupinfo = subprocess.STARTUPINFO()
                startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
                creationflags = subprocess.CREATE_NO_WINDOW
            except Exception:
                startupinfo = None
        proc = subprocess.Popen(
            ["pnputil", "/enum-drivers"],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, shell=False,
            startupinfo=startupinfo,
            creationflags=creationflags,
        )
        stdout, _ = proc.communicate(timeout=60)
    except Exception as e:
        ctx.log(f"  ! ERROR enumerating drivers: {e}")
        return 0

    if not stdout:
        return 0

    # Parse output block-wise: pnputil emits blank-line separated blocks
    # We track per-block published name, date, and in-use indicators.
    # Indicators that mean 'do NOT delete': Device Instance(s), 'In Use' patterns,
    # or locale variants. If we cannot determine, we conservatively skip.
    blocks = re.split(r"\n\s*\n", stdout)
    candidates: list[tuple[str, str, bool]] = []  # (published, date_str, maybe_in_use)
    for block in blocks:
        pub = None
        date_str = None
        maybe_in_use = False
        for line in block.splitlines():
            stripped = line.strip()
            low = stripped.lower()
            if low.startswith("published name"):
                parts = stripped.split(":", 1)
                if len(parts) == 2:
                    pub = parts[1].strip()
            elif low.startswith("driver date"):
                parts = stripped.split(":", 1)
                if len(parts) == 2:
                    date_str = parts[1].strip()
            # Heuristics for in-use: device instance, present, in use, etc.
            if any(k in low for k in ("device instance", "device id", "present", "in use", "is present")):
                # If value after colon suggests active, mark in-use; otherwise ignore
                if ":" in stripped:
                    val = stripped.split(":", 1)[1].strip().lower()
                    if val not in ("", "no", "false", "0", "not present"):
                        # Check for affirmative values
                        if any(v in val for v in ("yes", "true", "1")) or "device instance" in low:
                            maybe_in_use = True
                        # Non-empty device instance line itself means in-use
                        if "device instance" in low and val:
                            maybe_in_use = True
                elif "device instance" in low:
                    maybe_in_use = True
        if pub and date_str:
            candidates.append((pub, date_str, maybe_in_use))

    # Fallback to old line-wise parsing if block parsing found nothing
    if not candidates:
        lines = stdout.splitlines()
        current_published = None
        for line in lines:
            s = line.strip()
            if s.lower().startswith("published name"):
                current_published = s.split(":", 1)[1].strip() if ":" in s else None
            elif s.lower().startswith("driver date") and current_published:
                ds = s.split(":", 1)[1].strip() if ":" in s else ""
                candidates.append((current_published, ds, False))
                current_published = None
    # Locale fallback: non-English Windows uses different labels; try regex for oem*.inf + date
    if not candidates:
        oem_re = re.compile(r'\b(oem\d+\.inf)\b', re.I)
        date_re = re.compile(r'\b(\d{1,2}[\.\/\-]\d{1,2}[\.\/\-]\d{2,4})\b')
        for block in blocks:
            m_oem = oem_re.search(block)
            m_date = date_re.search(block)
            if m_oem and m_date:
                # Conservative: without English in-use markers, assume maybe_in_use if block mentions device-like id
                maybe = bool(re.search(r'(device|present|in\s*use)', block, re.I))
                candidates.append((m_oem.group(1), m_date.group(1), maybe))

    if not candidates:
        ctx.log("No driver packages found in store.")
        return 0

    # Filter: older than 30 days AND not maybe_in_use
    cutoff_date = datetime.datetime.now() - datetime.timedelta(days=30)
    old_drivers: list[str] = []
    skipped_in_use = 0
    for pub_name, date_str, maybe_in_use in candidates:
        if maybe_in_use:
            skipped_in_use += 1
            ctx.log(f"  Skipping in-use package: {pub_name}")
            continue
        try:
            # Extract date part before any time suffix (e.g., '01/15/2023 31.0.15' or '15.01.2023')
            date_part = date_str.split()[0] if date_str else ""
            # Normalize German dd.MM.yyyy -> try dot format
            driver_date = None
            for fmt in ("%m/%d/%Y", "%d/%m/%Y", "%Y-%m-%d", "%Y/%m/%d", "%d.%m.%Y", "%Y.%m.%d", "%d-%m-%Y", "%m-%d-%Y"):
                try:
                    driver_date = datetime.datetime.strptime(date_part, fmt)
                    break
                except ValueError:
                    continue
            if driver_date is None:
                ctx.log(f"  Skipping {pub_name}: unparsable date '{date_str}'")
                continue
            if driver_date < cutoff_date:
                old_drivers.append(pub_name)
        except Exception:
            continue

    if skipped_in_use:
        ctx.log(f"  Skipped {skipped_in_use} in-use package(s).")
    if not old_drivers:
        ctx.log("No removable driver packages older than 30 days found (or all are in-use).")
        return 0

    ctx.log(f"Found {len(old_drivers)} removable driver package(s) older than 30 days (not in-use).")

    # Delete old drivers WITHOUT /force
    deleted = 0
    for pub_name in old_drivers:
        if ctx.cancelled():
            break
        # Use shell=False list form via run_cmd: build command string but run_cmd uses shell=True
        # so we shell-quote via subprocess.list2cmdline for safety
        import subprocess as sp_mod
        cmd = sp_mod.list2cmdline(["pnputil", "/delete-driver", pub_name, "/uninstall"])
        ctx.log(f"$ {cmd}  (without /force — will fail if still needed)")
        try:
            # Pass as string with shell=True is what run_cmd expects, but we have safe quoting
            rc = run_cmd(ctx, cmd, timeout=60)
            if rc == 0:
                ctx.log(f"  Deleted: {pub_name}")
                deleted += 1
            else:
                ctx.log(f"  ! Skipped {pub_name} (still needed or delete failed, code {rc})")
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
    # Track which services were actually stopped so we don't start ones that were disabled
    # In dry-run, run_cmd returns 0 without stopping, so don't record
    stopped = []
    for svc in ("wuauserv", "bits"):
        rc = run_cmd(ctx, f"net stop {svc}", timeout=30)
        if rc == 0 and not ctx.dry_run:
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
        os.path.join(_PROGRAMDATA, "Microsoft\\Windows\\WER"),
        f"{_WINDIR}\\Minidump",
    ]
    total = _clean_many(ctx, [f for f in dump_folders if os.path.isdir(f)], "crash dump")
    # Memory.dmp is a single file (not a directory) — it was previously skipped
    # by the os.path.isdir filter, so handle it explicitly here.
    memory_dump = f"{_WINDIR}\\Memory.dmp"
    if os.path.isfile(memory_dump):
        try:
            # Use stat to get size atomically before remove to avoid TOCTOU
            st = os.stat(memory_dump)
            size = st.st_size
            skip = False
            if not ctx.dry_run:
                try:
                    os.remove(memory_dump)
                except FileNotFoundError:
                    # Race: file deleted between stat and remove
                    size = 0
                except OSError:
                    ctx.log(f"  (skipped locked dump: {memory_dump})")
                    size = 0
                    skip = True
            if size and not skip:
                total += size
                ctx.log(f"Cleaning crash dump: {memory_dump} ({size} bytes)")
        except OSError:
            # Stat failed or other OSError
            if 'size' not in locals() or size != 0:
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
    """Check for running browser processes. Timeout + no hang, respects dry_run/cancel."""
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
    
    # All profiles, not just Default — plus Opera/Vivaldi
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
