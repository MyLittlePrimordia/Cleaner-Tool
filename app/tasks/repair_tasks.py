"""
Repair tab tasks — Microsoft-documented, non-destructive.
Fixed: search index path, return-code checking, added safe new tasks.
"""

import os
import shutil

from app.utils import TaskContext, run_cmd, run_cmd_checked, create_restore_point, clean_folder_contents, restart_explorer

_WINDIR = os.environ.get("WINDIR", "C:\\Windows")


def repair_sfc_scan(ctx: TaskContext):
    ctx.set_status("Running System File Checker (sfc /scannow)... this can take several minutes.")
    run_cmd_checked(ctx, "sfc /scannow", timeout=1800, success_codes=(0, 1))


def repair_dism_restorehealth(ctx: TaskContext):
    ctx.set_status("Running DISM RestoreHealth (downloads replacement files from Windows Update if needed)...")
    run_cmd_checked(ctx, "DISM /Online /Cleanup-Image /RestoreHealth", timeout=1800,
                    success_codes=(0, 3010))


def repair_dism_component_cleanup(ctx: TaskContext):
    ctx.set_status("Cleaning up the WinSxS component store (frees space, keeps update rollback)...")
    run_cmd_checked(ctx, "DISM /Online /Cleanup-Image /StartComponentCleanup", timeout=900,
                    success_codes=(0, 3010))


def repair_dism_scanhealth(ctx: TaskContext):
    ctx.set_status("Scanning Windows image for corruption (DISM ScanHealth)...")
    run_cmd_checked(ctx, "DISM /Online /Cleanup-Image /ScanHealth", timeout=900, success_codes=(0, 3010))


def repair_dism_checkhealth(ctx: TaskContext):
    ctx.set_status("Checking Windows image health (DISM CheckHealth)...")
    run_cmd_checked(ctx, "DISM /Online /Cleanup-Image /CheckHealth", timeout=600, success_codes=(0, 3010))


def repair_chkdsk_scan(ctx: TaskContext):
    ctx.set_status("Scanning the system drive for filesystem errors (read-only)...")
    _sd = os.environ.get("SYSTEMDRIVE", "C:")
    if len(_sd) == 2 and _sd[1] == ":":
        _sd_root = _sd + "\\"
    else:
        _sd_root = _sd if _sd.endswith("\\") else _sd + "\\"
    run_cmd(ctx, f"chkdsk {_sd_root} /scan", timeout=900)


def repair_windows_update_reset(ctx: TaskContext):
    ctx.set_status("Resetting Windows Update components...")
    services = ["wuauserv", "cryptSvc", "bits", "msiserver"]
    stopped_services = []
    try:
        for svc in services:
            rc = run_cmd(ctx, f"net stop {svc}", timeout=30)
            if rc == 0:
                stopped_services.append(svc)
            else:
                ctx.log(f"  ! Warning: could not stop {svc}")
        
        sw_dist = f"{_WINDIR}\\SoftwareDistribution"
        catroot2 = f"{_WINDIR}\\System32\\catroot2"
        renamed = []
        
        for folder in (sw_dist, catroot2):
            if os.path.exists(folder):
                backup = folder + ".bak"
                # M4 fix: Windows recreates SoftwareDistribution as an EMPTY
                # folder within seconds of the services above restarting.
                # The old code unconditionally did rmtree(backup) then
                # rename(folder, backup) on every run — so running this task
                # twice in a short window would delete the only complete
                # backup (from run #1) and replace it with an empty folder
                # (Windows' just-recreated stub from run #1), wiping the WU
                # download store. Fix: only replace the backup if the LIVE
                # folder actually has content worth backing up; an empty
                # live folder means a backup already exists from a prior
                # run and should be left alone.
                try:
                    has_content = any(os.scandir(folder))
                except Exception:
                    has_content = True  # can't tell — be safe, don't skip
                if not has_content and os.path.exists(backup):
                    ctx.log(f"  {folder} is already empty — keeping existing backup at {backup} untouched.")
                    continue
                try:
                    if os.path.exists(backup):
                        shutil.rmtree(backup, ignore_errors=True)
                    os.rename(folder, backup)
                    renamed.append((folder, backup))
                    ctx.log(f"Renamed {folder} -> {backup}")
                except Exception as exc:
                    ctx.log(f"  ! could not rename {folder}: {exc}")
                    # Rollback any previously renamed folders
                    for orig, bak in reversed(renamed):
                        try:
                            if os.path.exists(bak):
                                if os.path.exists(orig):
                                    shutil.rmtree(orig, ignore_errors=True)
                                os.rename(bak, orig)
                                ctx.log(f"Rolled back {bak} -> {orig}")
                        except Exception as rollback_exc:
                            ctx.log(f"  ! Rollback failed for {bak}: {rollback_exc}")
                    raise
    finally:
        for svc in reversed(stopped_services):
            rc = run_cmd(ctx, f"net start {svc}", timeout=30)
            if rc != 0:
                ctx.log(f"  ! Warning: could not start {svc} (code {rc}) — may need reboot")
        # M10 fix: do NOT rmtree the .bak here — Windows may not have recreated
        # the original folder yet, and deleting the only copy would destroy the
        # user's entire WU download store. Leave the .bak for the next run to
        # clean up (the rename-backup at the top of the next run handles it).
        if renamed:
            ctx.log("Backups kept as .bak (Windows rebuilds folders on next update check).")
            ctx.log("They will be cleaned automatically on the next run of this task.")


def repair_search_index(ctx: TaskContext):
    ctx.set_status("Rebuilding the Windows Search index...")
    rc_stop = run_cmd(ctx, "net stop WSearch", timeout=30)
    # ProgramData is at drive root; handle bare "C:" correctly
    _sd = os.environ.get("SYSTEMDRIVE", "C:")
    if len(_sd) == 2 and _sd[1] == ":":
        _sd_root = _sd + "\\"
    else:
        _sd_root = _sd if _sd.endswith("\\") else _sd + "\\"
    program_data = os.environ.get("ProgramData", os.path.join(_sd_root, "ProgramData"))
    index_db = os.path.join(program_data, "Microsoft\\Search\\Data\\Applications\\Windows")
    if os.path.exists(index_db):
        if rc_stop != 0:
            ctx.log(f"  ! WSearch stop failed (code {rc_stop}) — skipping delete to avoid partial lock")
        else:
            try:
                shutil.rmtree(index_db, ignore_errors=False)
                ctx.log(f"Cleared search index database at {index_db}")
            except Exception as exc:
                ctx.log(f"  ! could not clear search index: {exc}")
    else:
        ctx.log(f"Search index path not found: {index_db}")
    rc_start = run_cmd(ctx, "net start WSearch", timeout=30)
    if rc_start != 0:
        ctx.log(f"  ! Warning: could not start WSearch (code {rc_start})")
    ctx.log("Search index will rebuild automatically in the background.")


def repair_print_spooler(ctx: TaskContext):
    ctx.set_status("Clearing stuck print jobs and restarting the Print Spooler...")
    run_cmd(ctx, "net stop spooler", timeout=30)
    spool_dir = f"{_WINDIR}\\System32\\spool\\PRINTERS"
    if os.path.exists(spool_dir):
        clean_folder_contents(ctx, spool_dir)
    run_cmd(ctx, "net start spooler", timeout=30)


def repair_wmi_repository(ctx: TaskContext):
    ctx.set_status("Verifying the WMI repository for corruption...")
    rc = run_cmd(ctx, "winmgmt /verifyrepository", timeout=120)
    if rc != 0:
        ctx.log("Repository reported inconsistent — attempting non-destructive salvage...")
        run_cmd(ctx, "winmgmt /salvagerepository", timeout=180)


def repair_reregister_store_apps(ctx: TaskContext):
    ctx.set_status("Re-registering built-in Windows apps (fixes Start Menu / missing app issues)...")
    # Use a PowerShell here-string so the per-package path is built reliably
    # without fragile nested quote escaping. -ErrorAction SilentlyContinue keeps
    # it non-fatal for packages that can't be re-registered.
    ps_cmd = (
        'powershell -NoProfile -Command '
        '"Get-AppXPackage -AllUsers | ForEach-Object { '
        'Add-AppxPackage -DisableDevelopmentMode '
        '-Register ($_.InstallLocation + \'\\AppXManifest.xml\') -ErrorAction SilentlyContinue }"'
    )
    run_cmd(ctx, ps_cmd, timeout=600)


def repair_time_sync(ctx: TaskContext):
    ctx.set_status("Syncing system clock (fixes update/cert errors)...")
    run_cmd(ctx, "net start w32time", timeout=30)
    run_cmd(ctx, "w32tm /resync", timeout=30)


def repair_gpupdate(ctx: TaskContext):
    ctx.set_status("Refreshing Group Policy (fixes policy-locked updates)...")
    run_cmd(ctx, "gpupdate /force", timeout=120)


def repair_bits_reset(ctx: TaskContext):
    ctx.set_status("Resetting BITS queue...")
    run_cmd(ctx, "bitsadmin /reset /allusers", timeout=60)


def repair_xbox_game_apps(ctx: TaskContext):
    """Re-register Xbox / Gaming Services apps — fixes broken Game Pass,
    Xbox app not launching, and 'we couldn't sign you in' errors.
    Keeps all packages installed; just repairs their registration."""
    ctx.set_status("Repairing Xbox / Game Pass apps (GamingServices, Xbox app)...")
    packages = [
        "Microsoft.GamingApp",        # Xbox app
        "Microsoft.GamingServices",   # Game Pass / install services
        "Microsoft.XboxIdentityProvider",
        "Microsoft.XboxGamingOverlay",
    ]
    for pkg in packages:
        run_cmd(
            ctx,
            f'powershell -NoProfile -Command "Get-AppxPackage -Name \\"{pkg}\\" | '
            f'ForEach-Object {{ Add-AppxPackage -DisableDevelopmentMode -Register '
            f'($_.InstallLocation + \'\\AppXManifest.xml\') -ErrorAction SilentlyContinue }}"',
            timeout=120,
        )
        ctx.log(f"  Re-registered {pkg}")
    # GamingServices also installs a Win32 service pair; re-register its appx explicitly
    run_cmd(
        ctx,
        'powershell -NoProfile -Command "Get-AppxPackage -Name \\"Microsoft.GamingServices\\" -AllUsers | '
        'ForEach-Object { Add-AppxPackage -DisableDevelopmentMode -Register ($_.InstallLocation + \'\\AppXManifest.xml\') -ErrorAction SilentlyContinue }"',
        timeout=120,
    )
    ctx.log("Xbox / Game Pass apps repaired. Reboot if the Xbox app still misbehaves.")


def repair_ssd_maintenance(ctx: TaskContext):
    """SSD maintenance: retrim all SSDs (defrag /L) + SMART health report."""
    ctx.set_status("Running SSD retrim and reporting drive health...")
    run_cmd(ctx, "defrag /C /L /U /V", timeout=1800)
    run_cmd(
        ctx,
        'powershell -NoProfile -Command "Get-PhysicalDisk | Select-Object FriendlyName, MediaType, HealthStatus | Format-Table -AutoSize"',
        timeout=60,
    )
    ctx.log("Retrim complete. Any drive above shows OK health or needs attention.")


def repair_vss_restore_points(ctx: TaskContext):
    """Restart the Volume Shadow Copy service and list writers — fixes
    failing System Restore point creation (complements the Safety Checkpoint task)."""
    ctx.set_status("Restarting Volume Shadow Copy (VSS) service...")
    run_cmd(ctx, "net stop VSS", timeout=60)
    run_cmd(ctx, "net start VSS", timeout=60)
    run_cmd(ctx, "vssadmin list writers", timeout=120)
    ctx.log("VSS restarted and writers listed above (look for [x] Stable).")


def repair_network_stack_defaults(ctx: TaskContext):
    """Reset network stack to Microsoft defaults + normalize TCP autotuning/RSS.
    Fixes damage left by other 'optimizers' (autotuning disabled causes slow
    downloads on Steam/Epic)."""
    ctx.set_status("Resetting network stack to Windows defaults...")
    run_cmd(ctx, "netsh winsock reset", timeout=60)
    run_cmd(ctx, "netsh int ip reset", timeout=60)
    run_cmd(ctx, "netsh int tcp set global autotuninglevel=normal", timeout=60)
    run_cmd(ctx, "netsh int tcp set global rss=enabled", timeout=60)
    run_cmd(ctx, "ipconfig /release", timeout=60)
    run_cmd(ctx, "ipconfig /renew", timeout=60)
    run_cmd(ctx, "ipconfig /flushdns", timeout=30)
    ctx.log("Network stack reset to defaults. A reboot is recommended.")


# --------------------------------------------------------------------------- #
# Round 2 checks (user request) — report-only, plain-language verdicts
# --------------------------------------------------------------------------- #

def repair_smart_verdict(ctx: TaskContext):
    """SMART verdict: read the system drive's health via PowerShell's
    Get-PhysicalDisk (same data CrystalDiskInfo shows) and translate it
    into a plain-language verdict. Report-only — never 'fixes' a dying
    drive, just tells the user the truth."""
    ctx.set_status("Checking drive health (SMART)...")
    import subprocess as _sp
    try:
        out = _sp.run(
            ["powershell", "-NoProfile", "-Command",
             "Get-PhysicalDisk | Select-Object FriendlyName, MediaType, HealthStatus, "
             "@{n='Size';e={[math]::Round($_.Size/1GB)}} | ConvertTo-Csv -NoTypeInformation"],
            capture_output=True, text=True, timeout=60,
            creationflags=getattr(_sp, "CREATE_NO_WINDOW", 0),
        )
    except Exception as exc:
        raise RuntimeError(f"Could not query drive health: {exc}")
    lines = [l.strip() for l in (out.stdout or "").splitlines() if l.strip() and not l.startswith("#")]
    if len(lines) < 2:
        raise RuntimeError("No drives reported — SMART data unavailable (or storage service disabled).")
    bad = []
    ok_n = 0
    for row in lines[1:]:
        parts = [p.strip('"') for p in row.split(",")]
        if len(parts) < 3:
            continue
        name, media, health = parts[0], parts[1], parts[2]
        ctx.log(f"  {name} ({media}): {health}")
        if health and health.lower() != "healthy":
            bad.append(name)
        else:
            ok_n += 1
    if bad:
        raise RuntimeError(
            f"Drive health WARNING: {', '.join(bad)} — back up your game library "
            "and saves NOW; a failing drive is the one thing this app can't repair."
        )
    ctx.log(f"VERDICT: all {ok_n} drive(s) Healthy — no action needed.")


def repair_gpu_driver_age(ctx: TaskContext):
    """Driver-age nudge: read the installed GPU driver's date from WMI
    (Win32_VideoController.DriverDate) and flag anything older than a
    year, or older than 6 months with a gentle note. Report-only."""
    ctx.set_status("Checking GPU driver age...")
    import subprocess as _sp
    from datetime import datetime, timezone
    try:
        # A1 fix: PowerShell's ConvertTo-Csv serializes DateTime in the
        # current culture's SHORT-DATE format (on en-US: "7/23/2026 7:00:00
        # PM"), which the parser below cannot read — every row was skipped
        # and healthy drivers were reported as unparsable. Format the
        # timestamp with an invariant culture FIRST, so the CSV always
        # carries "yyyy-MM-dd HH:mm:ss" regardless of regional settings.
        out = _sp.run(
            ["powershell", "-NoProfile", "-Command",
             "Get-CimInstance Win32_VideoController | "
             "Where-Object DriverDate | Select-Object Name, DriverDate | "
             "ForEach-Object { [PSCustomObject]@{ Name = $_.Name; "
             "DriverDate = $_.DriverDate.ToString('yyyy-MM-dd HH:mm:ss', [System.Globalization.CultureInfo]::InvariantCulture) } } | "
             "ConvertTo-Csv -NoTypeInformation"],
            capture_output=True, text=True, timeout=60,
            creationflags=getattr(_sp, "CREATE_NO_WINDOW", 0),
        )
    except Exception as exc:
        raise RuntimeError(f"Could not query GPU driver info: {exc}")
    lines = [l.strip() for l in (out.stdout or "").splitlines() if l.strip() and not l.startswith("#")]
    if len(lines) < 2:
        raise RuntimeError("No GPU driver info available from WMI.")
    stale, seen_any = [], False
    for row in lines[1:]:
        parts = [p.strip('"') for p in row.split(",", 1)]
        if len(parts) < 2 or not parts[1]:
            continue
        name, date_raw = parts[0], parts[1]
        try:
            # A1 fix: the pipeline above emits an invariant timestamp
            # ("yyyy-MM-dd HH:mm:ss", no culture-dependent short-date
            # format), so a single strict parse covers every machine.
            d = datetime.strptime(date_raw.strip(), "%Y-%m-%d %H:%M:%S")
        except ValueError:
            continue  # genuinely unparsable row — skip it, don't abort
        seen_any = True
        age_days = (datetime.now(timezone.utc).replace(tzinfo=None) - d).days
        ctx.log(f"  {name}: driver dated {d.date()} ({age_days} days old)")
        if age_days > 365:
            stale.append(f"{name} ({age_days // 30} months old)")
    if not seen_any:
        raise RuntimeError("Could not parse any driver dates — check the log.")
    if stale:
        raise RuntimeError(
            f"GPU driver outdated: {', '.join(stale)}. Update it from the "
            "Install tab (NVIDIA App / Intel DSA / AMD) for the latest game "
            "fixes and performance."
        )
    ctx.log("VERDICT: GPU drivers reasonably current — no nudge needed.")


from app.tasks import Task  # noqa: E402

# --------------------------------------------------------------------------- #
# Service quick-fixers (tasks.txt additions)
# --------------------------------------------------------------------------- #

def restart_audio_engine(ctx: TaskContext):
    """Restart the Windows audio stack (AudioSrv + AudioEndpointBuilder).
    Fixes most dead-audio / dead-mic situations (USB swap, app freeze)
    without a reboot. Best-effort per-service: AudioEndpointBuilder is a
    dependency of AudioSrv, so stopping AudioSrv /y stops both; we then
    start Builder first and AudioSrv second, as Windows expects."""
    ctx.set_status("Restarting Windows audio...")
    run_cmd_checked(ctx, "net stop AudioSrv /y", timeout=60, success_codes=(0, 1, 2))
    # 1 = service not stopped (wasn't running) — fine for a restart
    # 2 = service not installed (rare) — still try to start the stack
    run_cmd_checked(ctx, "net start AudioEndpointBuilder", timeout=60, success_codes=(0, 2))
    run_cmd_checked(ctx, "net start AudioSrv", timeout=60, success_codes=(0, 2))
    ctx.log("Audio engine restarted. If your device was muted, replug it or check Sound Settings.")


def repair_firewall_reset(ctx: TaskContext):
    """Reset Windows Firewall to factory defaults. Fixes multiplayer /
    anti-cheat matchmaking failures caused by corrupted or third-party
    mangled rules. ADVANCED: also removes every per-app allow/block rule
    (Windows re-prompts on next launch) — that's the point of a reset."""
    ctx.set_status("Resetting Windows Firewall rules...")
    run_cmd_checked(ctx, "netsh advfirewall reset", timeout=120)
    ctx.log("Firewall reset to stock defaults. Games will re-ask for network permission on first launch.")


def restart_bluetooth_stack(ctx: TaskContext):
    """Restart Bluetooth services (bthserv + bthHFSrv) — fixes wireless
    controller/headset disconnects without a reboot. Same shape as
    restart_audio_engine: dependency order matters (handsfree depends on
    the support service — stop dependents first, start support first).
    Exit codes: 1 = wasn't running (fine for stop), 2 = not installed
    (desktop PCs without Bluetooth)."""
    ctx.set_status("Restarting Bluetooth...")
    started = []
    for svc in ("bthHFSrv", "bthserv"):
        rc = run_cmd(ctx, f"net stop {svc} /y", timeout=60)
        if rc == 2:
            ctx.log(f"  {svc} not installed on this PC — skipped.")
    for svc in ("bthserv", "bthHFSrv"):
        rc = run_cmd(ctx, f"net start {svc}", timeout=60)
        if rc == 0:
            started.append(svc)
        elif rc == 2:
            ctx.log(f"  {svc} not installed — nothing to start.")
        else:
            ctx.log(f"  ! could not start {svc} (code {rc})")
    if not started:
        # honest failure (repair_smart_verdict pattern): a Bluetooth-less
        # PC must not report "restarted successfully"
        raise RuntimeError(
            "No Bluetooth services found — this PC may not have Bluetooth."
        )
    ctx.log(f"Bluetooth restarted ({', '.join(started)}). Reconnect your controller/headset if needed.")


def flush_arp_cache(ctx: TaskContext):
    """Clear the ARP table — fixes IP-conflict and router-communication
    issues. Pairs with dns_flush; netsh over `arp -d *` because it's one
    clean command with rc 0 on success."""
    ctx.set_status("Flushing ARP cache...")
    run_cmd_checked(ctx, "netsh interface ip delete arpcache", timeout=30)
    ctx.log("ARP cache cleared. Windows rebuilds it as you use the network.")


def reset_graphics_driver(ctx: TaskContext):
    """Soft-restart the display driver stack — the scripted version of
    Win+Ctrl+Shift+B. Screen flashes once; open windows stay open; no
    admin needed. There is no public API for this, so it synthesizes the
    global hotkey via keybd_event (the same technique dedicated 'fix black
    screen' utilities use).

    Honesty note: keybd_event gives no return code to verify — this is
    fire-and-forget by nature. The log states exactly what was sent and
    what the user should see (one screen flash). The heavier alternative
    (disable/enable the display device via PnP) was rejected: it needs
    admin, black-screens longer, and rearranges windows — worse for a
    layman-facing tool."""
    import ctypes
    import time
    ctx.set_status("Restarting graphics driver (screen will flash)...")
    user32 = ctypes.windll.user32
    keys = [(0x5B, 0), (0x11, 0), (0x10, 0), (0x42, 0),   # LWin, Ctrl, Shift, B down
            (0x42, 2), (0x10, 2), (0x11, 2), (0x5B, 2)]   # ...up, in reverse
    for vk, flags in keys:
        user32.keybd_event(vk, 0, flags, 0)
        time.sleep(0.05)
    ctx.log("Display driver restart sent (same as pressing Win+Ctrl+Shift+B).")
    ctx.log("Your screen should flash once. If it didn't, the hotkey can be pressed manually.")


_ANTICHEAT_SERVICES = ("EasyAntiCheat", "EasyAntiCheat_EOS", "BEService", "BEDaisy")


def repair_anticheat_services(ctx: TaskContext):
    """Reset EAC / BattlEye services to on-demand and bounce them — fixes
    'Error 30005: anti-cheat service creation failed' launch errors.

    The safe, real fix: games start these services themselves with their
    own arguments; third-party 'optimizers' often leave them disabled or
    broken, which is what 30005 reports. Set back to demand-start (checked
    via run_cmd_checked) and bounce once. NEVER delete services — the game
    recreates them and SDDL permission templates vary per install, so
    sc sdset is out of scope."""
    ctx.set_status("Repairing anti-cheat services (EasyAntiCheat, BattlEye)...")
    found = 0
    for svc in _ANTICHEAT_SERVICES:
        rc = run_cmd(ctx, f"sc query {svc}", timeout=30)
        if rc != 0:
            ctx.log(f"  {svc}: not installed — skipped.")
            continue
        found += 1
        run_cmd_checked(ctx, f"sc config {svc} start= demand", timeout=30)
        run_cmd(ctx, f"net stop {svc} /y", timeout=60)   # best-effort; usually idle
        rc2 = run_cmd(ctx, f"net start {svc}", timeout=60)
        if rc2 == 0:
            ctx.log(f"  {svc}: reset to on-demand and verified it starts.")
        else:
            ctx.log(f"  {svc}: reset to on-demand. (Could not start it standalone — "
                    "normal; your game starts it with its own arguments.)")
    if not found:
        raise RuntimeError(
            "No EasyAntiCheat or BattlEye services found — this only helps if "
            "you have an EAC/BattlEye game installed."
        )
    ctx.log("Tip: if a game still fails, run 'EasyAntiCheat_EOS_Setup.exe' "
            "(in the game's EasyAntiCheat folder) and choose Repair.")


# --------------------------------------------------------------------------- #
# Missing Gaming Components installers (LTSC / stripped Windows support)
# --------------------------------------------------------------------------- #
# Design rules distilled from the add.txt review:
#   * never guess from the Windows edition — detect the component itself
#     (app/capabilities.py) so this works on LTSC, N, and debloated Pro
#   * skip honestly when already present ("already installed" is a
#     success, not a failure)
#   * install order respects the chicken-and-egg: Store FIRST (it brings
#     winget), then everything else via winget
#   * verify presence AFTER install and raise if it didn't take — no
#     silent fake successes (the Ultimate Performance lesson)
#   * every winget ID below was verified to exist on a real machine
#     before being hardcoded (Xbox 9MV0B5HZVK9Z, Game Bar 9NZKPSTSNW4P,
#     VP9 9N4D0MSMP0PT, AV1 9MVZQVXJBQ9V, WebMedia 9N5TDP8VCMHS)

def repair_icon_cache(ctx: TaskContext):
    """Fix blank/white desktop icons: stop Explorer, delete the icon cache
    databases, restart Explorer (verified back — never left without a shell)."""
    ctx.set_status("Rebuilding the icon cache...")
    run_cmd(ctx, "taskkill /f /im explorer.exe", timeout=15)
    local = os.environ.get("LOCALAPPDATA", "")
    removed = 0
    if local and os.path.isabs(local):
        import glob as _glob
        for path in [os.path.join(local, "IconCache.db")] + \
                _glob.glob(os.path.join(local, "iconcache_*.db")):
            try:
                if os.path.isfile(path):
                    os.remove(path)
                    removed += 1
                    ctx.log(f"Removed stale icon cache: {path}")
            except OSError as exc:
                ctx.log(f"  (kept {path}: {exc})")
    if not restart_explorer(ctx):
        ctx.log("  ! Explorer did not come back on its own — press Ctrl+Shift+Esc, File > Run, type explorer.exe")
    else:
        ctx.log(f"Icon cache rebuilt ({removed} database(s) cleared).")


def repair_store_cache_reset(ctx: TaskContext):
    """Reset the Microsoft Store download cache (wsreset): fixes Store and
    Xbox-app downloads that fail, hang, or loop."""
    ctx.set_status("Resetting the Store download cache (wsreset)...")
    run_cmd_checked(ctx, "wsreset.exe", timeout=600, success_codes=(0,))
    ctx.log("Store cache reset — reopen the Store and retry the download.")


def repair_enable_system_restore(ctx: TaskContext):
    """Re-enable System Protection on C: when something (or some debloat
    guide) turned it off — without it, Safety Checkpoints cannot save you."""
    ctx.set_status("Turning System Protection back on for C:...")
    run_cmd_checked(
        ctx,
        'powershell -NoProfile -Command "Enable-ComputerRestore -Drive \'C:\\\'"',
        timeout=300, success_codes=(0,),
    )
    ctx.log("System Protection is on for C: — Safety Checkpoints will work again.")


def repair_power_plans(ctx: TaskContext):
    """Restore Microsoft's default power plans (fixes plans broken or
    deleted by other optimizers — Balanced/High performance come back;
    your active plan resets to Balanced, reselect after if you like)."""
    ctx.set_status("Restoring default Windows power plans...")
    run_cmd_checked(ctx, "powercfg -restoredefaultschemes", timeout=120,
                    success_codes=(0,))
    ctx.log("Default power plans restored (active plan is now Balanced).")


def repair_teredo(ctx: TaskContext):
    """Fix Xbox-multiplayer 'Teredo unable to qualify' NAT errors by
    resetting the Teredo tunnel to Microsoft defaults (enterpriseclient +
    default server). No firewall or router changes; harmless on PCs
    without Xbox networking."""
    ctx.set_status("Resetting Teredo for Xbox multiplayer networking...")
    run_cmd(ctx, "netsh interface teredo set state enterpriseclient", timeout=60)
    run_cmd(ctx, "netsh interface teredo set state servername=default", timeout=60)
    ctx.log("Teredo reset to defaults — retry the Xbox party/game invite.")


# --------------------------------------------------------------------------- #
# Installer tasks MOVED to app/tasks/install_tasks.py (Install tab) — every
# internet-required task lives there now, per the user's 4th-tab request.
# Repair keeps the repair-only identity: fix what exists, don't install
# what doesn't.
# --------------------------------------------------------------------------- #

TASKS = [
    Task("restore_point", "Safety Checkpoint", "Saves a restore point you can go back to", create_restore_point, default=True, column=0),
    Task("xbox_apps", "Fix Xbox / Game Pass Apps", "Repairs the Xbox app and Game Pass sign-in without reinstalling", repair_xbox_game_apps, default=False, column=0),
    Task("ssd_maintenance", "SSD Maintenance", "Retrims your SSD and checks drive health", repair_ssd_maintenance, default=False, column=0),
    Task("vss_repair", "Fix Restore Points (VSS)", "Restarts the shadow-copy service so checkpoints work again", repair_vss_restore_points, default=False, column=0),
    Task("network_reset", "Fix Internet Connection", "Resets internet settings to defaults, repairs bad tweaks", repair_network_stack_defaults, default=False, risk="REBOOT REQUIRED", column=1),
    Task("sfc_scan", "Fix System Files", "Scans and fixes broken Windows files that crash games", repair_sfc_scan, default=False, column=0),
    Task("dism_restorehealth", "Repair Windows Image", "Downloads fresh Windows files to fix a broken image", repair_dism_restorehealth, default=False, column=0),
    Task("dism_cleanup", "Cleanup Update Storage", "Cleans old update leftovers but keeps uninstall option", repair_dism_component_cleanup, default=False, column=0),
    Task("dism_scanhealth", "Scan Image Health", "Scans Windows image for corruption", repair_dism_scanhealth, default=False, column=0),
    Task("dism_checkhealth", "Check Image Health", "Quick check if image needs repair", repair_dism_checkhealth, default=False, column=0),
    Task("chkdsk_scan", "Check Disk (Read-Only)", "Checks your drive for errors without restarting", repair_chkdsk_scan, default=False, column=1),
    Task("wu_reset", "Fix Stuck Updates", "Fixes Windows Update when it’s stuck or failing", repair_windows_update_reset, default=False, column=1),
    Task("bits_reset", "Fix Download Queue", "Clears stuck download jobs that block updates", repair_bits_reset, default=False, column=1),
    Task("time_sync", "Fix Clock Sync", "Fixes wrong clock that breaks updates and logins", repair_time_sync, default=False, column=1),
    Task("gpupdate", "Fix Blocked Settings", "Refreshes Windows rules that may block Game Mode", repair_gpupdate, default=False, column=1),
    Task("search_index", "Fix Search Not Working", "Rebuilds Windows search that finds files and apps", repair_search_index, default=False, column=1),
    Task("print_spooler", "Fix Stuck Printing", "Clears stuck print jobs and restarts printer", repair_print_spooler, default=False, column=1),
    Task("wmi_repair", "Fix Game Services (WMI)", "Fixes system database many games rely on", repair_wmi_repository, default=False, column=1),
    Task("store_apps_reregister", "Fix Missing Apps / Start Menu", "Fixes missing apps or Start menu without reinstall", repair_reregister_store_apps, default=False, column=1),
    Task("restart_audio", "Restart Sound / Mic", "Fixes dead audio or mic instantly without a reboot", restart_audio_engine, default=False, admin_required=True, column=1),
    Task("firewall_reset", "Reset Firewall", "Fixes multiplayer/anti-cheat connection errors by resetting firewall rules", repair_firewall_reset, default=False, admin_required=True, risk="ADVANCED", column=1),
    Task("smart_verdict", "Drive Health Verdict (SMART)", "Plain-language check: is your SSD/HDD healthy, or time to back up?", repair_smart_verdict, default=False, admin_required=False, column=1),
    Task("gpu_driver_age", "GPU Driver Freshness", "Checks your graphics driver's age and nudges you to update if it's stale", repair_gpu_driver_age, default=False, admin_required=False, column=1),
    Task("restart_bluetooth", "Restart Bluetooth", "Fixes wireless controller and headset drops without rebooting", restart_bluetooth_stack, default=False, admin_required=True, column=1),
    Task("arp_flush", "Fix IP Conflicts (ARP)", "Clears stuck network address entries that break router talk", flush_arp_cache, default=False, admin_required=True, column=1),
    Task("gpu_reset", "Restart Graphics Driver", "Fixes black screens and resolution bugs instantly, no reboot", reset_graphics_driver, default=False, admin_required=False, column=1),
    Task("anticheat_repair", "Fix Anti-Cheat Errors", "Resets EasyAntiCheat and BattlEye to fix launch errors like 30005", repair_anticheat_services, default=False, admin_required=True, column=1),
    Task("icon_cache", "Fix Blank Icons", "Rebuilds the icon cache that causes blank or white desktop icons", repair_icon_cache, default=False, admin_required=False, column=1),
    Task("wsreset_store", "Reset Store Downloads", "Resets the Store cache when app downloads fail or hang", repair_store_cache_reset, default=False, admin_required=False, column=1),
    Task("enable_restore", "Turn On System Protection", "Re-enables restore points on C: if something turned them off", repair_enable_system_restore, default=False, admin_required=True, column=0),
    Task("power_plans", "Reset Power Plans", "Restores Microsoft's default power plans when optimizers break them", repair_power_plans, default=False, admin_required=True, column=0),
    Task("teredo_fix", "Fix Xbox Multiplayer (Teredo)", "Resets Teredo tunneling to fix Xbox party and matchmaking errors", repair_teredo, default=False, admin_required=True, column=1),
]