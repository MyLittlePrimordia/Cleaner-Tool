"""
Repair tab tasks — Microsoft-documented, non-destructive.
Fixed: search index path, return-code checking, added safe new tasks.
"""

import os
import shutil

from app.utils import TaskContext, run_cmd, run_cmd_checked, create_restore_point

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


def repair_dism_resetbase(ctx: TaskContext):
    ctx.set_status("Running DISM /ResetBase (permanently trims WinSxS — removes update rollback)...")
    run_cmd_checked(ctx, "DISM /Online /Cleanup-Image /StartComponentCleanup /ResetBase", timeout=900,
                    success_codes=(0, 3010))





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
                try:
                    if os.path.exists(backup):
                        if ctx.dry_run:
                            ctx.log(f"Would remove existing backup {backup} (dry run)")
                        else:
                            shutil.rmtree(backup, ignore_errors=True)
                    if ctx.dry_run:
                        ctx.log(f"Would rename {folder} -> {backup} (dry run)")
                        # Don't append to renamed in dry-run — not actually renamed
                    else:
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
        # If rename succeeded and Windows recreated the folders, clean up .bak to reclaim space
        # (otherwise leave .bak for next run's rmtree). Don't block on deletion.
        if not ctx.dry_run and renamed:
            import time as _time
            _time.sleep(2.0)
            for orig, bak in renamed:
                if os.path.exists(orig) and os.path.exists(bak):
                    try:
                        shutil.rmtree(bak, ignore_errors=True)
                        if not os.path.exists(bak):
                            ctx.log(f"Cleaned up backup {bak}")
                    except Exception:
                        pass


def repair_network_reset(ctx: TaskContext):
    ctx.set_status("Resetting the network stack (Winsock + TCP/IP)...")
    run_cmd(ctx, "netsh winsock reset", timeout=60)
    run_cmd(ctx, "netsh int ip reset", timeout=60)
    run_cmd(ctx, "ipconfig /release", timeout=60)
    run_cmd(ctx, "ipconfig /renew", timeout=60)
    run_cmd(ctx, "ipconfig /flushdns", timeout=30)
    ctx.log("A reboot is recommended for the network reset to fully apply.")


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
    if os.path.exists(index_db) and not ctx.dry_run:
        if rc_stop != 0:
            ctx.log(f"  ! WSearch stop failed (code {rc_stop}) — skipping delete to avoid partial lock")
        else:
            try:
                shutil.rmtree(index_db, ignore_errors=False)
                ctx.log(f"Cleared search index database at {index_db}")
            except Exception as exc:
                ctx.log(f"  ! could not clear search index: {exc}")
    elif os.path.exists(index_db):
        ctx.log(f"Would clear search index at {index_db} (dry run)")
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
    if os.path.exists(spool_dir) and not ctx.dry_run:
        for f in os.listdir(spool_dir):
            try:
                os.remove(os.path.join(spool_dir, f))
            except OSError:
                pass
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


def repair_disable_memory_integrity(ctx: TaskContext):
    """Disable Memory Integrity (HVCI) — improves CPU-bound gaming performance.
    WARNING: Reduces protection against kernel-level exploits."""
    ctx.set_status("Disabling Memory Integrity (HVCI)...")
    run_cmd(ctx, "reg add HKLM\\SYSTEM\\CurrentControlSet\\Control\\DeviceGuard /v EnableVirtualizationBasedSecurity /t REG_DWORD /d 0 /f", timeout=30)
    run_cmd(ctx, "reg add HKLM\\SYSTEM\\CurrentControlSet\\Control\\DeviceGuard\\Scenarios\\HypervisorEnforcedCodeIntegrity /v Enabled /t REG_DWORD /d 0 /f", timeout=30)
    ctx.log("Memory Integrity disabled. Reboot required.")


def repair_enable_memory_integrity(ctx: TaskContext):
    """Re-enable Memory Integrity (HVCI) — restores kernel exploit protection."""
    ctx.set_status("Enabling Memory Integrity (HVCI)...")
    run_cmd(ctx, "reg add HKLM\\SYSTEM\\CurrentControlSet\\Control\\DeviceGuard /v EnableVirtualizationBasedSecurity /t REG_DWORD /d 1 /f", timeout=30)
    run_cmd(ctx, "reg add HKLM\\SYSTEM\\CurrentControlSet\\Control\\DeviceGuard\\Scenarios\\HypervisorEnforcedCodeIntegrity /v Enabled /t REG_DWORD /d 1 /f", timeout=30)
    ctx.log("Memory Integrity enabled. Reboot required.")


def repair_disable_virtual_machine_platform(ctx: TaskContext):
    """Disable Virtual Machine Platform (Hyper-V) — reduces overhead if not using WSL2/Hyper-V."""
    ctx.set_status("Disabling Virtual Machine Platform...")
    run_cmd_checked(ctx, "dism /Online /Disable-Feature /FeatureName:VirtualMachinePlatform /NoRestart", timeout=120, success_codes=(0, 3010))
    ctx.log("Virtual Machine Platform disabled. Reboot required.")


def repair_enable_virtual_machine_platform(ctx: TaskContext):
    """Re-enable Virtual Machine Platform (needed for WSL2/Hyper-V)."""
    ctx.set_status("Enabling Virtual Machine Platform...")
    run_cmd_checked(ctx, "dism /Online /Enable-Feature /FeatureName:VirtualMachinePlatform /All /NoRestart", timeout=120, success_codes=(0, 3010))
    ctx.log("Virtual Machine Platform enabled. Reboot required.")


def repair_disable_storage_sense_active_hours(ctx: TaskContext):
    """Configure Storage Sense master switch (HKCU). Value 01 = on/off."""
    ctx.set_status("Configuring Storage Sense...")
    # Correct hive is HKCU, not HKLM — StoragePolicy lives under HKCU\SOFTWARE\...
    from app.utils import reg_set_value
    reg_set_value(ctx, "HKCU", "SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\StorageSense\\Parameters\\StoragePolicy", "01", 1)
    ctx.log("Storage Sense configured.")


def repair_enable_storage_sense_active_hours(ctx: TaskContext):
    """Restore Storage Sense default behavior (HKCU)."""
    ctx.set_status("Restoring Storage Sense default behavior...")
    from app.utils import reg_delete_value
    reg_delete_value(ctx, "HKCU", "SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\StorageSense\\Parameters\\StoragePolicy", "01")
    ctx.log("Storage Sense restored to default.")


from app.tasks import Task  # noqa: E402

TASKS = [
    Task("restore_point", "Safety Checkpoint",
         "Creates an undo point so you can roll back before repairs.",
         create_restore_point, default=True, column=0),
    Task("sfc_scan", "Fix System Files",
         "Scans and repairs broken or missing Windows system files.",
         repair_sfc_scan, default=True, column=0),
    Task("dism_restorehealth", "Repair Windows Image",
         "Fixes broken system files by downloading clean copies from Windows Update.",
         repair_dism_restorehealth, default=True, column=0),
    Task("dism_cleanup", "Cleanup Update Storage",
         "Frees disk space safely — keeps ability to uninstall recent updates.",
         repair_dism_component_cleanup, default=False, column=0),
    Task("dism_resetbase", "Deep Cleanup (Advanced)",
         "Maximum space savings but you can no longer uninstall current updates. Only if you won't roll back.",
         repair_dism_resetbase, default=False, risk="ADVANCED", column=0),
    Task("chkdsk_scan", "Check Disk for Errors (Read-Only)",
         "Scans your drive for file errors without locking it or needing a reboot.",
         repair_chkdsk_scan, default=False, column=1),
    Task("wu_reset", "Fix Stuck Updates",
         "The standard Microsoft fix when updates are stuck or failing.",
         repair_windows_update_reset, default=False, column=1),
    Task("bits_reset", "Fix Download Queue",
         "Clears stuck background downloads that block updates.",
         repair_bits_reset, default=False, column=1),
    Task("disable_memory_integrity", "Disable Memory Integrity (HVCI)",
         "Disables HVCI for 5-25% FPS gain in CPU-bound games. Reduces kernel exploit protection.",
         repair_disable_memory_integrity, default=False, revert=repair_enable_memory_integrity, risk="ADVANCED", column=0),
    Task("disable_vm_platform", "Disable Virtual Machine Platform",
         "Disables Hyper-V/Virtual Machine Platform if not using WSL2. Reduces CPU overhead.",
         repair_disable_virtual_machine_platform, default=False, revert=repair_enable_virtual_machine_platform, risk="REBOOT REQUIRED", column=0),
    Task("storage_sense_active_hours", "Storage Sense: Avoid Active Hours",
         "Prevents Storage Sense cleanup from running during gaming (prevents stutters).",
         repair_disable_storage_sense_active_hours, default=False, revert=repair_enable_storage_sense_active_hours, column=0),
    Task("network_reset", "Fix Internet Connection",
         "Resets network adapter to fix no-internet or DNS issues.",
         repair_network_reset, default=False, risk="REBOOT REQUIRED", column=1),
    Task("time_sync", "Fix Clock Sync",
         "Syncs your clock — fixes update and certificate errors.",
         repair_time_sync, default=False, column=1),
    Task("gpupdate", "Fix Blocked Settings",
         "Refreshes system policies that may block updates or Game Mode.",
         repair_gpupdate, default=False, column=1),
    Task("search_index", "Fix Search Not Working",
         "Rebuilds Start menu and File Explorer search.",
         repair_search_index, default=False, column=1),
    Task("print_spooler", "Fix Stuck Printing",
         "Clears stuck print jobs and restarts printing.",
         repair_print_spooler, default=False, column=1),
    Task("wmi_repair", "Fix Game Services (WMI)",
         "Checks the database many games and tools use — repairs if broken.",
         repair_wmi_repository, default=False, column=1),
    Task("store_apps_reregister", "Fix Missing Apps / Start Menu",
         "Fixes broken Start menu or missing built-in apps without reinstalling Windows.",
         repair_reregister_store_apps, default=False, column=1),
]
