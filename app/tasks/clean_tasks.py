"""
Clean tab — only deletes disposable, auto-regenerating junk.
Laymen short labels + hover tooltips; compact symmetrical grid.
"""

import glob as globmod
import os
import time
# audit fix (hygiene): module-level `subprocess` was unused — every caller
# that needs it already does its own local `import subprocess`.

from app.utils import TaskContext, clean_folder_contents, run_cmd, restart_explorer, resolve_exe
from app.tasks.launcher_paths import (
    ALL_LAUNCHER_CACHE_PATHS, GPU_SHADER_CACHE_ALL, refresh_dynamic_paths,
)

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
    # M12: re-run the glob/listdir discoveries now (not import time) so
    # launchers installed/updated since app start are covered.
    refresh_dynamic_paths()
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
    """True if the folder is safe to delete as a leftover driver install.

    Safety gate (the only thing inspected is the folder ROOT): an actively
    referenced driver keeps its .sys/.dll/.inf/.cat binaries at the root of
    these staging folders (C:\\NVIDIA, C:\\AMD, C:\\ATI, C:\\Intel\\Driver) —
    if ANY driver binary appears at root level we refuse and the folder is
    skipped. Logs, temp files, and version-numbered subfolders are all
    disposable install debris; clean_folder_contents walks them safely."""
    if not os.path.isdir(folder_path):
        return False
    try:
        for entry in os.listdir(folder_path):
            entry_path = os.path.join(folder_path, entry)
            if os.path.isfile(entry_path):
                ext = os.path.splitext(entry)[1].lower()
                # Skip if there are driver binaries in root (might be active)
                if ext in (".sys", ".dll", ".inf", ".cat"):
                    return False
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
        # user request 2026-09-12: Game Bar overlay + Mixed Reality Portal
        # keep their own LocalCache/TempState (clips stay in Videos\Captures,
        # handled by the separate game_captures task — never touched here)
        "Microsoft.XboxGamingOverlay",
        "Microsoft.MixedReality.Portal",
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
    # M13: stop DoSvc first (same stopped[] pattern as the update-cache
    # sibling) — otherwise its files stay locked and the clean under-delivers.
    stopped = run_cmd(ctx, "net stop DoSvc", timeout=30) == 0
    try:
        return clean_folder_contents(ctx, f"{_WINDIR}\\SoftwareDistribution\\DeliveryOptimization")
    finally:
        if stopped:
            run_cmd(ctx, "net start DoSvc", timeout=30)


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
            from app.utils import _is_reparse_point as _is_rp
            # CAND-01: never delete through a junction/symlink, honor dry-run
            # and Stop — same guard as game_tasks._clean_files.
            if _is_rp(memory_dump):
                ctx.log(f"  (skipped reparse-point file: {memory_dump})")
            elif bool(getattr(ctx, "dry_run", False)):
                try:
                    size = os.path.getsize(memory_dump)
                except OSError:
                    size = 0
                if size:
                    total += size
                    ctx.log(f"[dry-run] would clean crash dump: {memory_dump} ({size} bytes)")
            elif ctx.cancelled():
                ctx.log("  ! stopped — skipping Memory.dmp.")
            else:
                st = os.stat(memory_dump)
                size = st.st_size
                # Re-verify immediately before removal (defense in depth).
                if _is_rp(memory_dump):
                    ctx.log(f"  (skipped reparse-point file: {memory_dump})")
                    size = 0
                else:
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


def clean_gpu_watchdog_dumps(ctx: TaskContext):
    """Empty C:\\Windows\\LiveKernelReports — the GPU watchdog dump folder.

    When a display driver hangs and recovers ('display driver stopped
    responding and has recovered' / a black-screen flash), Windows can
    drop multi-GB kernel dumps here (WATCHDOG subfolder is the classic
    NVIDIA/AMD case). They are pure post-mortem debugging artifacts:
    nothing reads them back, they accumulate silently, and they are NOT
    covered by clean_recycle_bin_and_dumps (Minidump/Memory.dmp) or
    clean_error_reports (WER queues) — this folder is the gap.

    Same safety contract as every dump cleaner: locked files are skipped
    honestly, junction-guarded clean_folder_contents walks the tree."""
    folders = [
        os.path.join(_WINDIR, "LiveKernelReports"),
    ]
    total = _clean_many(ctx, folders, "GPU watchdog dump")
    if not os.path.isdir(folders[0]):
        ctx.log("No LiveKernelReports folder — no GPU watchdog dumps on this PC.")
    return total


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
                if ctx.cancelled():
                    ctx.log("  ! stopped — skipping remaining disk fragments.")
                    break
                if os.path.isfile(path):
                    from app.utils import _is_reparse_point as _is_rp2
                    # CAND-01: same reparse-point + dry-run guard as _clean_files.
                    try:
                        if _is_rp2(path):
                            continue
                    except Exception:
                        continue
                    if bool(getattr(ctx, "dry_run", False)):
                        try:
                            size = os.path.getsize(path)
                        except OSError:
                            continue
                        total += size
                        continue
                    # L2 fix: size must be added to `total` only AFTER
                    # os.remove() succeeds. The old order added it first —
                    # if remove() then raised (e.g. a locked file), the
                    # except below still swallowed it, but the size had
                    # already been counted, inflating the "space freed"
                    # total for files that were never actually deleted.
                    # (Matches the correct order already used in
                    # game_tasks._clean_files and utils.clean_folder_contents.)
                    size = os.path.getsize(path)
                    try:
                        if _is_rp2(path):
                            continue
                    except Exception:
                        continue
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
    # M13: never kill the shell after the user pressed Stop, and never
    # report success when the shell failed to come back.
    if ctx.cancelled():
        ctx.log("  ! cancelled — leaving Explorer running.")
        return 0
    explorer_dir = os.path.join(_LOCALAPPDATA, "Microsoft\\Windows\\Explorer")
    run_cmd(ctx, "taskkill /f /im explorer.exe", timeout=10)
    total = clean_folder_contents(ctx, explorer_dir, extensions=[".db"])
    # Always bring Explorer back — detached, non-blocking (see docstring above)
    if not restart_explorer(ctx):
        raise RuntimeError(
            "Explorer did not come back after the thumbnail clean — press "
            "Ctrl+Shift+Esc, File > Run new task, type explorer.exe.")
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
        # user request 2026-09-12: Arc uses the same Chromium layout
        os.path.join(_LOCALAPPDATA, "Arc\\User Data"),
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
    # Opera (Stable + GX — GX keeps its own "Opera GX Stable" profile dir)
    for op_branch in ("Opera Stable", "Opera GX Stable"):
        for op in [os.path.join(_APPDATA, f"Opera Software\\{op_branch}\\Cache"),
                   os.path.join(_APPDATA, f"Opera Software\\{op_branch}\\Code Cache")]:
            if op and os.path.isabs(op) and os.path.isdir(op):
                ctx.log(f"Cleaning browser cache: {op}")
                total += clean_folder_contents(ctx, op)
    # Firefox + its forks (identical Profiles\*\cache2 layout, different roots)
    for ff_root, ff_label in (
            (os.path.join(_LOCALAPPDATA, "Mozilla\\Firefox\\Profiles"), "Firefox"),
            (os.path.join(_APPDATA, "librewolf\\Profiles"), "LibreWolf"),
            (os.path.join(_APPDATA, "Floorp\\Profiles"), "Floorp")):
        ff_base = ff_root
        if not (ff_base and os.path.isabs(ff_base) and os.path.isdir(ff_base)):
            continue
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
                ctx.log(f"Cleaning {ff_label} cache: {cache2}")
                total += clean_folder_contents(ctx, cache2)
    return total


def clean_font_cache(ctx: TaskContext):
    ctx.set_status("Rebuilding font cache...")
    # M13: track the stop like clean_windows_update_cache does — the old
    # code unconditionally ran `net start`, which would START a service the
    # admin deliberately disabled. Restart only what WE stopped, in a
    # finally so a clean error can't skip it.
    stopped = run_cmd(ctx, "net stop FontCache", timeout=30) == 0
    try:
        total = clean_folder_contents(
            ctx, f"{_WINDIR}\\ServiceProfiles\\LocalService\\AppData\\Local\\FontCache"
        )
    finally:
        if stopped:
            run_cmd(ctx, "net start FontCache", timeout=30)
    return total


def _wait_and_close_store_app(ctx: TaskContext, attempts: int = 10,
                              interval: float = 0.5):
    """wsreset.exe launches the Store app itself once the reset lands —
    that's by design (no switch suppresses it), and the exact moment it
    appears on screen isn't deterministic. Poll quietly for the process
    (no per-attempt log noise) and close it the instant it shows up,
    instead of leaving it sitting open over the scan."""
    import subprocess as _sp
    import time as _time
    image = "WinStore.App.exe"
    for _ in range(attempts):
        try:
            out = _sp.check_output(
                f'tasklist /fi "imagename eq {image}" /fo csv /nh',
                shell=True, text=True, timeout=5, stderr=_sp.DEVNULL,
                creationflags=getattr(_sp, "CREATE_NO_WINDOW", 0),
            )
            if image.lower() in (out or "").lower():
                run_cmd(ctx, f"taskkill /f /im {image}", timeout=10)
                return
        except Exception:
            pass
        _time.sleep(interval)


def clean_store_cache(ctx: TaskContext):
    ctx.log("Resetting Microsoft Store cache...")
    run_cmd(ctx, "wsreset.exe", timeout=60)
    _wait_and_close_store_app(ctx)
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


# Days Windows.old must sit untouched before this task will remove it.
# Windows itself keeps the "go back to your previous version of Windows"
# option alive for up to 10 days after an upgrade; giving 4 extra days of
# margin means this task is never the reason someone loses that option a
# day or two before they would have decided to use it. The other upgrade-
# debris folders below ($Windows.~BT etc.) don't carry that rollback
# implication — they are only ever present mid-upgrade or are tiny by
# comparison — so the age gate applies to Windows.old specifically rather
# than slowing down cleanup of the others.
_WINDOWS_OLD_MIN_AGE_DAYS = 14


def clean_windows_update_leftovers(ctx: TaskContext):
    """Remove Windows Update / upgrade leftovers (Sophia Script's list).

    Targets: Windows.old, $Windows.~BT, $WinREAgent, $GetCurrent, $SysReset,
    $Windows.~WS, ESD, C:\\Intel, C:\\PerfLogs. These are installer debris and
    safe to delete AFTER an upgrade completed (Windows itself offers to remove
    most of them via Storage Sense after 10 days).

    Windows.old specifically is age-gated (see _WINDOWS_OLD_MIN_AGE_DAYS):
    even though this task is opt-in and its own title already tells the
    user to wait a few days, a folder-age check catches the case where
    someone runs it the same day they upgraded, on top of — not instead
    of — that warning.
    """
    # F02: upgrade debris (Windows.old, $Windows.~*, $WinREAgent,
    # $GetCurrent, $SysReset) are whole staged installs — removing the
    # root is correct. ESD/Intel/PerfLogs are vendor/system containers
    # Windows and tools expect to exist, so empty them but keep the dir.
    remove_root_folders = [
        os.path.join(_SYSTEMDRIVE_ROOT, "Windows.old"),
        os.path.join(_SYSTEMDRIVE_ROOT, "$Windows.~BT"),
        os.path.join(_SYSTEMDRIVE_ROOT, "$Windows.~WS"),
        os.path.join(_SYSTEMDRIVE_ROOT, "$WinREAgent"),
        os.path.join(_SYSTEMDRIVE_ROOT, "$GetCurrent"),
        os.path.join(_SYSTEMDRIVE_ROOT, "$SysReset"),
    ]
    keep_root_folders = [
        os.path.join(_SYSTEMDRIVE_ROOT, "ESD"),
        os.path.join(_SYSTEMDRIVE_ROOT, "Intel"),
        os.path.join(_SYSTEMDRIVE_ROOT, "PerfLogs"),
    ]
    windows_old = os.path.join(_SYSTEMDRIVE_ROOT, "Windows.old")
    total = 0
    for folder in remove_root_folders:
        if not folder or not os.path.isabs(folder):
            continue
        if not os.path.exists(folder):
            continue
        if folder == windows_old:
            try:
                age_days = (time.time() - os.path.getmtime(folder)) / 86400.0
            except OSError:
                ctx.log("Could not check Windows.old's age — "
                       "leaving it alone to be safe.")
                continue
            if age_days < _WINDOWS_OLD_MIN_AGE_DAYS:
                ctx.log(f"Windows.old is only {age_days:.1f} day(s) old — "
                       f"leaving it for now (needs {_WINDOWS_OLD_MIN_AGE_DAYS}+ "
                       "days, in case you want to go back to your previous "
                       "version of Windows).")
                continue
        ctx.log(f"Cleaning update leftover: {folder}")
        total += clean_folder_contents(ctx, folder, remove_root=True)
    for folder in keep_root_folders:
        if not folder or not os.path.isabs(folder):
            continue
        if os.path.exists(folder):
            ctx.log(f"Cleaning update leftover (keeping folder): {folder}")
            total += clean_folder_contents(ctx, folder, remove_root=False)
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


def clean_onedrive_logs(ctx: TaskContext):
    """Clear OneDrive's diagnostic logs (user request 2026-09-12).

    OneDrive writes verbose sync/troubleshooting logs under
    %LOCALAPPDATA%\\Microsoft\\OneDrive\\logs that never rotate on their
    own. Pure logs — sync state, settings and files are elsewhere and
    untouched. Skips honestly when OneDrive isn't installed."""
    folders = [os.path.join(_LOCALAPPDATA, "Microsoft\\OneDrive\\logs")]
    if not any(os.path.isdir(f) for f in folders):
        ctx.log("OneDrive logs not found (OneDrive not installed?) — nothing to do.")
        return 0
    return _clean_many(ctx, folders, "OneDrive logs")


# Exact dirname + exact cache leaves for the WebView2 sweep below. Storage,
# Local Storage, Session Storage, IndexedDB and friends are DELIBERATELY
# absent — website data is user data; only regenerable browser caches go.
_WEBVIEW_ROOT_NAME = "EBWebView"
_WEBVIEW_CACHE_SUBS = ("Cache", "Code Cache", "GPUCache", "GrShaderCache",
                       "GraphiteDawnCache", "DawnGraphiteCache",
                       "DawnWebGPUCache")
_WEBVIEW_MAX_DEPTH = 6


def _find_webview_roots(base_dir: str, max_depth: int = _WEBVIEW_MAX_DEPTH) -> list:
    """Every directory named exactly EBWebView under base_dir (bounded depth,
    junctions never followed, unreadable folders skipped). Pure function of
    the tree (unit-tested with fixtures) — the caller cleans only the known
    cache leaves inside each root."""
    from app.utils import _is_reparse_point
    found: list = []
    if not base_dir or not os.path.isdir(base_dir):
        return found
    stack = [(base_dir, 0)]
    while stack:
        directory, depth = stack.pop()
        if depth > max_depth:
            continue
        try:
            with os.scandir(directory) as it:
                entries = list(it)
        except OSError:
            continue
        for entry in entries:
            try:
                if not entry.is_dir(follow_symlinks=False):
                    continue
                if _is_reparse_point(entry.path):
                    continue
                if entry.name == _WEBVIEW_ROOT_NAME:
                    found.append(entry.path)
                    # still descend: an app can nest runtimes (x86/x64 side
                    # by side), and each nesting is its own cache
                    stack.append((entry.path, depth + 1))
                else:
                    stack.append((entry.path, depth + 1))
            except OSError:
                continue
    return found


def clean_webview_caches(ctx: TaskContext):
    """Clear embedded-browser (WebView2) caches — user request 2026-09-12.

    EA App, CurseForge/Overwolf, Battle.net and dozens of other launchers
    embed Chromium (WebView2), each keeping its own Cache/Code Cache/GPUCache
    under an EBWebView folder. Same junk class as the main-browser caches,
    but no per-app table could ever cover them all — hence the bounded exact-
    name sweep (mirrors the Unity Player.log sweep design). Website storage
    (cookies, local storage, logins) is never in the cleaned leaves."""
    if not _LOCALAPPDATA or not os.path.isdir(_LOCALAPPDATA):
        ctx.log("LocalAppData not found — nothing to do.")
        return 0
    total = 0
    roots = _find_webview_roots(_LOCALAPPDATA)
    if not roots:
        ctx.log("No embedded-browser (WebView2) caches found.")
        return 0
    for eb_root in roots:
        if ctx.cancelled():
            break
        for sub in _WEBVIEW_CACHE_SUBS:
            p = os.path.join(eb_root, sub)
            if os.path.isdir(p):
                ctx.log(f"Cleaning embedded browser cache: {p}")
                total += clean_folder_contents(ctx, p)
    if not total:
        ctx.log("Embedded-browser caches already empty.")
    return total


# Cleans disk via cleanmgr (no ResetBase — keeps your ability to roll back updates) #

# Arbitrary, unused sageset slot reserved for Cleaner Tool's own profile
# (0-65535; Microsoft doesn't reserve any range, so any free number works).
_CLEANMGR_SAGE_NUM = 65432

# Old Windows update / servicing leftovers only — the stuff plain file
# deletion can't safely reach because it's tracked by the component store.
# Recycle Bin / temp files are already handled by our own direct-delete
# tasks elsewhere, so they're deliberately left off this list.
_CLEANMGR_SAGE_CATEGORIES = (
    "Windows Update Cleanup",
    "Windows Upgrade Log Files",
    "Windows ESD installation files",
    "Delivery Optimization Files",
    "Previous Installations",
    "Service Pack Cleanup",
)


def run_disk_cleanup(ctx: TaskContext):
    ctx.log("[Clean] Disk Cleanup - Run")
    # cleanmgr's /VERYLOWDISK switch is meant for the OS's own low-disk
    # nudge, not scripted use — it can still land a completion dialog
    # that needs a manual OK. /sagerun is Microsoft's documented silent
    # path instead: flag the categories we want once via the registry
    # (StateFlags under each VolumeCaches handler), then /sagerun applies
    # them with zero UI, every time. reg add creates the key path if a
    # given handler isn't present on this edition, so this is safe to run
    # unconditionally on any Windows version.
    key_base = (r"HKLM\SOFTWARE\Microsoft\Windows\CurrentVersion\Explorer"
                r"\VolumeCaches")
    state_flag = f"StateFlags{_CLEANMGR_SAGE_NUM:04d}"
    for cat in _CLEANMGR_SAGE_CATEGORIES:
        run_cmd(
            ctx,
            f'reg add "{key_base}\\{cat}" /v {state_flag} '
            f'/t REG_DWORD /d 2 /f',
            timeout=15,
        )
    ctx.log(f"$ cleanmgr.exe /sagerun:{_CLEANMGR_SAGE_NUM}")
    run_cmd(ctx, f"cleanmgr.exe /sagerun:{_CLEANMGR_SAGE_NUM}", timeout=900)
    ctx.log("Disk Cleanup complete.")
    return 0

# Removes temp files via PowerShell #
def remove_temp_files_deep(ctx: TaskContext):
    ctx.log("[Clean] Temporary Files - Remove")
    # shell=False argv + PowerShell double quotes (variables expand in PS
    # double-quoted strings; no backslash escaping — \" would end the PS
    # string early with a ParserError). Timeouts + cancel checks so Stop works.
    for script in (
        'Remove-Item -Path "$Env:Temp\\*" -Recurse -Force -ErrorAction SilentlyContinue',
        'Remove-Item -Path "$Env:SystemRoot\\Temp\\*" -Recurse -Force -ErrorAction SilentlyContinue',
    ):
        if ctx.cancelled():
            break
        run_cmd(ctx, ["powershell", "-NoProfile", "-Command", script],
                shell=False, timeout=120)
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
        if ctx.cancelled():
            ctx.log("  ! cancelled — stopping bloat removal.")
            break
        # shell=False argv + PS single quotes (package names need no
        # expansion; backslash-escaped double quotes would ParserError).
        run_cmd(ctx, ["powershell", "-NoProfile", "-Command",
                      f"Get-AppxPackage -Name '{pkg}' -AllUsers | Remove-AppxPackage -ErrorAction SilentlyContinue"],
                shell=False, timeout=60)
        ctx.log(f"  Checked {pkg}")
    # TikTok registers under different publisher names; catch-all fallback
    if not ctx.cancelled():
        run_cmd(ctx, ["powershell", "-NoProfile", "-Command",
                      "Get-AppxPackage -AllUsers | Where-Object {$_.Name -like '*TikTok*'} | Remove-AppxPackage -ErrorAction SilentlyContinue"],
                shell=False, timeout=60)
    # Note: blocking auto-reinstall of consumer suggestions (DisableWindowsConsumerFeatures)
    # lives in the Tweak tab's "Stop Windows Ads & Tips" task instead, since that task has
    # a working revert. This Clean-tab task only removes apps (Store-reinstallable, no
    # one-way registry writes left behind).
    ctx.log("Bloat removal complete.")
    return 0


def clean_event_logs(ctx: TaskContext):
    """Clear Windows Event Viewer logs (diagnostic history only — the logs
    start fresh; fixes bloated evtx files, admin).
    F04: the Security channel is never touched (audit/forensic trail);
    failures log the real exit code instead of claiming 'in-use'."""
    collected: "list[str]" = []
    # shell=False argv lists: channel names are system output and must never
    # be interpolated into a shell=True string (injection via crafted
    # channel name when elevated). Per-log timeout is short so ~200 logs
    # can't stall for hours; the loop honors Stop.
    rc = run_cmd(ctx, [resolve_exe("wevtutil"), "el"], shell=False, timeout=60, collect=collected)
    if rc != 0 and not collected:
        raise RuntimeError(f"Could not list event logs (wevtutil el exited {rc}).")
    cleared = 0
    for name in collected:
        if ctx.cancelled():
            ctx.log("  ! cancelled — stopping event-log clear.")
            break
        name = name.strip()
        if not name:
            continue
        if name.lower() == "security":
            ctx.log("  (skipped Security log — audit trail preserved; clear it only from Event Viewer.)")
            continue
        rc = run_cmd(ctx, [resolve_exe("wevtutil"), "cl", name], shell=False, timeout=30)
        if rc == 0:
            cleared += 1
        else:
            ctx.log(f"  (skipped {name}: wevtutil cl exited {rc} — in use or access denied)")
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
    def _looks_like_steam(path: str) -> bool:
        # F16: HKCU SteamPath is user-writable — require Steam fingerprints
        # before trusting it as an elevated delete root; refuse system roots.
        try:
            norm = os.path.normpath(path or "")
            if not norm or not os.path.isdir(norm):
                return False
            root = os.path.splitdrive(norm)[0] + os.sep
            windir = os.environ.get("WINDIR", "")
            profile = os.environ.get("USERPROFILE", "")
            if norm.lower() in (root.lower(), windir.lower() if windir else "", profile.lower() if profile else ""):
                return False
            return (os.path.isfile(os.path.join(norm, "steam.exe"))
                    or os.path.isdir(os.path.join(norm, "steamapps")))
        except Exception:
            return False
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam") as k:
            path, _ = winreg.QueryValueEx(k, "SteamPath")
            if path and _looks_like_steam(path):
                return path
    except Exception:
        pass
    return os.path.join(os.environ.get("ProgramFiles(x86)", "C:\\Program Files (x86)"), "Steam")


def _steam_running() -> bool:
    """True if steam.exe is currently running (active downloads may be writing)."""
    try:
        import subprocess as _sp
        out = _sp.check_output(
            'tasklist /fi "imagename eq steam.exe" /fo csv /nh',
            shell=True, text=True, timeout=10, stderr=_sp.DEVNULL,
            creationflags=getattr(_sp, "CREATE_NO_WINDOW", 0),
        )
        return "steam.exe" in (out or "").lower()
    except Exception:
        return False


def clean_steam_download_cache(ctx: TaskContext):
    """Clear Steam's depot manifest cache + stale appinfo (fixes phantom
    'update required' states; Steam re-downloads them on launch)."""
    if _steam_running():
        ctx.log("Steam is running — skipping to avoid touching active downloads.")
        return 0
    root = _steam_root()
    total = _clean_many(ctx, [os.path.join(root, "depotcache")], "Steam depot cache")
    appinfo = os.path.join(root, "appcache", "appinfo.vdf")
    try:
        if os.path.isfile(appinfo):
            from app.utils import _is_reparse_point as _is_rp3
            try:
                _is_rp = _is_rp3(appinfo)
            except Exception:
                _is_rp = True
            if _is_rp:
                ctx.log(f"  (skipped reparse-point file: {appinfo})")
            elif bool(getattr(ctx, "dry_run", False)):
                size = os.path.getsize(appinfo)
                total += size
                ctx.log(f"[dry-run] would remove stale app manifest: {appinfo}")
            elif ctx.cancelled():
                ctx.log("  ! stopped — keeping appinfo.vdf.")
            else:
                # Count only AFTER a successful remove (a locked file must not
                # inflate the freed-space total).
                size = os.path.getsize(appinfo)
                try:
                    _is_rp2 = _is_rp3(appinfo)
                except Exception:
                    _is_rp2 = True
                if _is_rp2:
                    ctx.log(f"  (skipped reparse-point file: {appinfo})")
                else:
                    os.remove(appinfo)
                    total += size
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
    from app.utils import _is_reparse_point as _is_rp4
    dry = bool(getattr(ctx, "dry_run", False))
    for path in files:
        try:
            if ctx.cancelled():
                ctx.log("  ! stopped — keeping remaining history files.")
                break
            if os.path.isfile(path):
                try:
                    if _is_rp4(path):
                        ctx.log(f"  (skipped reparse-point file: {path})")
                        continue
                except Exception:
                    continue
                if dry:
                    try:
                        size = os.path.getsize(path)
                    except OSError as exc:
                        ctx.log(f"  (kept history file: {exc})")
                        continue
                    total += size
                    ctx.log(f"[dry-run] would remove terminal history: {path}")
                    continue
                size = os.path.getsize(path)
                try:
                    if _is_rp4(path):
                        ctx.log(f"  (skipped reparse-point file: {path})")
                        continue
                except Exception:
                    continue
                os.remove(path)
                total += size
                ctx.log(f"Removed terminal history: {path}")
        except OSError as exc:
            ctx.log(f"  (kept history file: {exc})")
    if not total:
        ctx.log("No terminal history found.")
    return total


def clean_stream_app_logs(ctx: TaskContext):
    """Clear Medal / Overwolf (Outplayed) / SteelSeries GG diagnostic logs
    and web caches (clips and settings untouched — logs only)."""
    folders = [
        os.path.join(_LOCALAPPDATA, "Medal", "logs"),
        os.path.join(_LOCALAPPDATA, "Medal", "Cache"),
        os.path.join(_LOCALAPPDATA, "Overwolf", "Log"),
        os.path.join(_LOCALAPPDATA, "Overwolf", "Cache"),
        os.path.join(_LOCALAPPDATA, "SteelSeries", "GG", "logs"),
    ]
    return _clean_many(ctx, [f for f in folders if f], "stream app logs")


from app.tasks import Task  # noqa: E402

TASKS = [
    Task("shader_cache", "Clear Shader Cache", "Removes temp graphics files that help fix stutter", clean_shader_cache, default=True, admin_required=False),
    Task("launcher_cache", "Clean Launchers & Chat", "Clears every game store plus Discord, Slack, Teams, Spotify junk", clean_launcher_cache, default=True, admin_required=False),
    Task("engine_cache", "Clean Engine Cache", "Removes leftover Unreal/Unity build files", clean_engine_cache, default=True, admin_required=False),
    Task("driver_junk", "Remove Old Drivers", "Deletes old NVIDIA/AMD installer leftovers", clean_driver_junk, default=True, admin_required=True),
    Task("user_temp_files", "Empty User Temp Files", "Deletes leftover temp files Windows left behind", clean_user_temp_files, default=True, admin_required=False),
    Task("system_temp_files", "Empty System Temp Files", "Cleans system temp files no longer needed", clean_system_temp_files, default=True, admin_required=True),
    Task("win_update_cache", "Fix Update Cache", "Fixes Windows Update when downloads get stuck", clean_windows_update_cache, default=True, admin_required=True),
    Task("delivery_optimization", "Clear Update Share Cache", "Removes update copies kept to share with other PCs", clean_delivery_optimization, default=True, admin_required=True),
    Task("inet_cache", "Clear Internet Cache", "Clears old internet temp files", clean_inet_cache, default=True, admin_required=False),
    Task("recycle_bin", "Empty Bin & Crash Reports", "Empties trash and removes old crash dumps", clean_recycle_bin_and_dumps, default=True, admin_required=True),
    Task("error_reports", "Clear Error Reports", "Deletes old Windows error reports", clean_error_reports, default=True, admin_required=False),
    Task("gpu_watchdog_dumps", "Clean GPU Watchdog Dumps", "Removes multi-GB driver-hang dumps that pile up after black-screen flashes", clean_gpu_watchdog_dumps, default=False, admin_required=True),
    Task("thumbnail_cache", "Fix Blurry Icons", "Rebuilds icons, fixes missing thumbnails", clean_thumbnail_icon_cache, default=True, admin_required=False),
    Task("chk_fragments", "Remove Disk Fragments", "Deletes leftover files from disk checks", clean_chk_fragments, default=True, admin_required=True),
    Task("old_logs", "Clear System Logs", "Removes old Windows logs", clean_old_logs, default=True, admin_required=True),
    Task("dns_flush", "Fix Internet (DNS)", "Clears internet cache to fix sites not loading", flush_dns, default=True, admin_required=False),
    Task("ram_purge", "Free Up RAM", "Asks Windows to free unused memory", purge_ram_working_sets, default=False, admin_required=False),
    Task("update_leftovers", "Clear Update Leftovers", "Run this only once your PC has been running fine for a few days after a big Windows update", clean_windows_update_leftovers, default=False, admin_required=True, risk="ADVANCED"),
    Task("activity_traces", "Clear Activity Traces", "Clears recent-files and jump-list history for privacy", clean_activity_traces, default=False, admin_required=False),
    Task("prefetch", "Clear Prefetch Files", "Clears prefetch data, usually not needed", clean_prefetch, default=False, admin_required=True, risk="ADVANCED"),
    Task("disk_cleanup_deep", "Deep Disk Cleanup", "Deep cleans old Windows update files", run_disk_cleanup, default=False, admin_required=True, risk="ADVANCED"),
    Task("browser_cache", "Clear Browser Cache", "Clears browser temp files, keeps passwords", clean_browser_caches, default=False, admin_required=False),
    Task("office_cache", "Clear Office Cache", "Removes Office temporary files", clean_office_cache, default=False, admin_required=False),
    Task("uwp_cache", "Clear UWP App Caches", "Clears Windows apps temp files like Photos", clean_uwp_cache, default=False, admin_required=False),
    Task("font_cache", "Fix Broken Fonts", "Rebuilds fonts to fix garbled text", clean_font_cache, default=False, admin_required=True),
    Task("store_cache", "Fix Store", "Resets Store if apps won't download", clean_store_cache, default=False, admin_required=False),
    Task("temp_deep_clean", "Deep Temp Clean", "Deep cleans temp files with PowerShell", remove_temp_files_deep, default=False, admin_required=True),
    Task("remove_bloat", "Remove Windows Bloat", "Removes Clipchamp, MSN apps, TikTok and other preinstalled junk", remove_windows_bloat, default=False, admin_required=True),
    Task("winget_cache", "Clear WinGet Cache", "Removes leftover installer files and logs from Windows' app downloader", clean_winget_cache, default=False, admin_required=False),
    Task("event_logs", "Clear Event Viewer Logs", "Wipes old Windows diagnostic logs so they start fresh (Security log always kept)", clean_event_logs, default=False, admin_required=True, risk="ADVANCED"),
    Task("defender_history", "Clear Defender History", "Removes stale protection-history entries that haunt the Security app", clean_defender_history, default=False, admin_required=True),
    Task("steam_depot", "Clean Steam Download Cache", "Clears Steam's manifest cache that causes phantom update states", clean_steam_download_cache, default=False, admin_required=False),
    Task("dev_caches", "Clean Dev Caches", "Clears VS Code, npm and pip caches for modders and AI tinkerers", clean_dev_caches, default=False, admin_required=False),
    Task("pkg_caches", "Clean Package Caches", "Clears NuGet, Cargo and Gradle download caches; projects untouched", clean_package_manager_caches, default=False, admin_required=False),
    Task("terminal_history", "Clear Terminal History", "Clears PowerShell command history for privacy; settings untouched", clean_terminal_history, default=False, admin_required=False),
    Task("onedrive_logs", "Clear OneDrive Logs", "Removes diagnostic logs OneDrive leaves behind; files and settings untouched", clean_onedrive_logs, default=False, admin_required=False),
    Task("webview_cache", "Clear WebView App Caches", "Clears embedded-browser junk from EA App, CurseForge and similar launchers; logins kept", clean_webview_caches, default=False, admin_required=False),
    Task("stream_logs", "Clear Stream App Logs", "Clears Medal, Outplayed and SteelSeries logs; clips and settings kept", clean_stream_app_logs, default=False, admin_required=False),
]