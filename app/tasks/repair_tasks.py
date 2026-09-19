"""
Repair tab tasks — Microsoft-documented, non-destructive.
Fixed: search index path, return-code checking, added safe new tasks.
"""

import os
import shutil

from app.utils import TaskContext, run_cmd, run_cmd_checked, create_restore_point, clean_folder_contents, restart_explorer, reg_get_value, reg_delete_value, known_folder, atomic_write_text, cmd_arg

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
    # F-5 audit fix: the exit code used to be discarded, so a drive WITH
    # filesystem errors (chkdsk rc=3: "errors found, not fixed" — observed
    # live) reported as a 'succeeded' task. smart_verdict/gpu_driver_age
    # already raise on negative verdicts (this module's own honesty
    # contract); chkdsk now does the same with a plain-language verdict.
    # rc 0 = clean. rc 3 = errors found by the read-only scan. Anything
    # else (or -1 cancel/timeout) is surfaced honestly below.
    # F06: _sd_root derives from %SYSTEMDRIVE% (user-writable env) — route
    # it through cmd_arg so metacharacters travel as data, never as command
    # separators when elevated.
    rc = run_cmd(ctx, f"chkdsk {cmd_arg(_sd_root)} /scan", timeout=900)
    if ctx.cancelled():
        from app.utils import TaskCancelled
        raise TaskCancelled(f"chkdsk scan of {_sd_root} was cancelled by user.")
    if rc == 0:
        ctx.log(f"VERDICT: {_sd_root} scanned clean — no filesystem errors found.")
        return
    if rc == 3:
        raise RuntimeError(
            f"Filesystem errors were FOUND on {_sd_root} (chkdsk exit code 3, "
            "read-only scan). Back up important saves/files NOW, then fix them: "
            "right-click the drive in Explorer > Properties > Tools > Check "
            f"(or run 'chkdsk {_sd_root} /f' from an Administrator prompt and "
            "reboot when it asks)."
        )
    raise RuntimeError(
        f"chkdsk scan of {_sd_root} did not complete (exit code {rc}) — "
        "close open files and retry, or run it from an Administrator prompt."
    )


def _wsus_folder_has_content(folder: str) -> bool:
    # F2-4: extracted from the inline branch soup below. The five panes are
    # preserved EXACTLY:
    #   >1 top-level entry        -> True
    #   exactly 1 entry (M11 stub guard):
    #       not a file OR larger than 1MB -> True; else False
    #   stat failure on the paine -> True (can't tell — be safe, don't skip)
    #   scandir failure           -> True (can't tell — be safe, don't skip)
    #   0 entries (M4 stub)       -> False (a just-recreated EMPTY live
    #                                  folder must never clobber the backup)
    try:
        with os.scandir(folder) as it:
            entries = list(it)
    except Exception:
        return True
    if len(entries) > 1:
        return True
    if len(entries) == 1:
        try:
            e = entries[0]
            return (not e.is_file()) or (e.stat().st_size > 1048576)
        except Exception:
            return True
    return False


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
                # M11: a single small stub file (Windows' just-recreated
                # placeholder) also counts as "content" for any() — and
                # would still clobber a good backup. Require SUBSTANTIAL
                # content: >1 top-level entry, or one entry larger than 1MB.
                # F2-4: the five-pane check now lives in the named helper
                # (it never raises — every failure pane resolves to True so
                # the "can't tell — be safe, don't skip" stance is intact).
                has_content = _wsus_folder_has_content(folder)
                if not has_content and os.path.exists(backup):
                    ctx.log(f"  {folder} holds only a stub — keeping existing backup at {backup} untouched.")
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
        # F03: surface the orphan cost — log each .bak path + size so the
        # disk waste is visible, with the manual-delete path. No new
        # deletion timing (safety invariant unchanged).
        if renamed:
            for _orig, _bak in renamed:
                try:
                    _size = 0
                    for _root, _dirs, _files in os.walk(_bak):
                        if ctx.cancelled():
                            break
                        for _f in _files:
                            try:
                                _size += os.path.getsize(os.path.join(_root, _f))
                            except OSError:
                                continue
                    from app.utils import format_bytes as _fmt
                    ctx.log(f"Backup kept: {_bak} ({_fmt(_size)}) — Windows rebuilds "
                            f"the live folder on next update check.")
                except Exception:
                    ctx.log(f"Backup kept: {_bak} (size unknown).")
            ctx.log("These .bak folders are removed automatically on the next run of "
                    "this task (once the live folder has real content again).")
            ctx.log("To reclaim the space sooner, delete the .bak folder(s) manually "
                    "only after Windows Update succeeds again.")


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
    # H5: the old unchecked stop/start logged success either way. A failed
    # stop usually means the service is already stopped/disabled — clean
    # anyway, but restart honestly.
    run_cmd(ctx, "net stop spooler", timeout=30)
    spool_dir = f"{_WINDIR}\\System32\\spool\\PRINTERS"
    if os.path.exists(spool_dir):
        clean_folder_contents(ctx, spool_dir)
    if run_cmd(ctx, "net start spooler", timeout=30) != 0:
        raise RuntimeError("Print Spooler jobs cleared but the service would not start — reboot, then check Services.")


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
    # H5: a timed-out/failed 10-minute re-register used to log nothing and
    # count as success. Non-zero (or -1 cancel/timeout) raises honestly.
    run_cmd_checked(ctx, ps_cmd, timeout=600)


def repair_time_sync(ctx: TaskContext):
    ctx.set_status("Syncing system clock (fixes update/cert errors)...")
    # H5: unchecked resync reported "synced" on failure (no NTP reach).
    run_cmd(ctx, "net start w32time", timeout=30)
    if run_cmd(ctx, "w32tm /resync", timeout=60) != 0:
        raise RuntimeError("Clock resync failed (no NTP response?) — check internet/timezone, then retry.")


def repair_gpupdate(ctx: TaskContext):
    ctx.set_status("Refreshing Group Policy (fixes policy-locked updates)...")
    # H5: gpupdate fails without admin/domain — raise instead of silencing.
    run_cmd_checked(ctx, "gpupdate /force", timeout=180)


def repair_bits_reset(ctx: TaskContext):
    ctx.set_status("Resetting BITS queue...")
    # H5: bitsadmin fails when BITS is disabled — raise honestly.
    run_cmd_checked(ctx, "bitsadmin /reset /allusers", timeout=60)


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
    # H5/H1: the old shell=True strings embedded literal \" sequences
    # (PowerShell ParserError on every package, rc ignored, unconditional
    # "Re-registered" log). shell=False argv + PS single quotes; per-package
    # rc counted honestly — absent packages simply no-op under
    # SilentlyContinue, but a total failure raises.
    _ok = 0
    for pkg in packages:
        rc = run_cmd(
            ctx,
            ["powershell", "-NoProfile", "-Command",
             f"Get-AppxPackage -Name '{pkg}' | "
             f"ForEach-Object {{ Add-AppxPackage -DisableDevelopmentMode -Register "
             f"($_.InstallLocation + '\\AppXManifest.xml') }}"],
            shell=False,
            timeout=120,
        )
        if rc == 0:
            _ok += 1
            ctx.log(f"  Re-registered {pkg}")
        else:
            ctx.log(f"  ! re-register failed for {pkg} (exit {rc})")
    # GamingServices also installs a Win32 service pair; re-register its appx explicitly
    rc = run_cmd(
        ctx,
        ["powershell", "-NoProfile", "-Command",
         "Get-AppxPackage -Name 'Microsoft.GamingServices' -AllUsers | "
         "ForEach-Object { Add-AppxPackage -DisableDevelopmentMode -Register ($_.InstallLocation + '\\AppXManifest.xml') }"],
        shell=False,
        timeout=120,
    )
    if rc == 0:
        _ok += 1
    else:
        ctx.log(f"  ! re-register failed for Microsoft.GamingServices (-AllUsers, exit {rc})")
    if _ok == 0:
        raise RuntimeError("No Xbox package could be re-registered — see the log.")
    ctx.log("Xbox / Game Pass apps repaired. Reboot if the Xbox app still misbehaves.")


def repair_ssd_maintenance(ctx: TaskContext):
    """SSD maintenance: retrim all SSDs (defrag /L) + SMART health report."""
    ctx.set_status("Running SSD retrim and reporting drive health...")
    # H5: defrag fails on HDD-only machines / without admin — the old
    # unchecked call still logged "Retrim complete".
    if run_cmd(ctx, "defrag /C /L /U /V", timeout=1800) != 0:
        ctx.log("  ! retrim pass reported an issue (HDD-only PC or access denied?) — continuing to health report.")
    run_cmd(
        ctx,
        'powershell -NoProfile -Command "Get-PhysicalDisk | Select-Object FriendlyName, MediaType, HealthStatus | Format-Table -AutoSize"',
        timeout=60,
    )
    ctx.log("Retrim pass done. Any drive above shows OK health or needs attention.")


def repair_vss_restore_points(ctx: TaskContext):
    """Restart the Volume Shadow Copy service and list writers — fixes
    failing System Restore point creation (complements the Safety Checkpoint task)."""
    ctx.set_status("Restarting Volume Shadow Copy (VSS) service...")
    # H5: VSS stop often fails (already stopped / access denied) — don't
    # claim a restart that never happened; the writers list is the proof.
    run_cmd(ctx, "net stop VSS", timeout=60)
    if run_cmd(ctx, "net start VSS", timeout=60) != 0:
        raise RuntimeError("VSS would not start — System Restore stays broken; reboot, then re-run.")
    if run_cmd(ctx, "vssadmin list writers", timeout=120) != 0:
        ctx.log("  ! could not list VSS writers (service restarted anyway).")
    ctx.log("VSS restarted and writers listed above (look for [x] Stable).")


def repair_network_stack_defaults(ctx: TaskContext):
    """Reset network stack to Microsoft defaults + normalize TCP autotuning/RSS.
    Fixes damage left by other 'optimizers' (autotuning disabled causes slow
    downloads on Steam/Epic)."""
    ctx.set_status("Resetting network stack to Windows defaults...")
    # H5: every step checked — the old fire-and-forget run_cmd calls logged
    # success even when the stack was untouched.
    failures: list = []
    for cmd, to in (("netsh winsock reset", 60),
                    ("netsh int ip reset", 60),
                    ("netsh int tcp set global autotuninglevel=normal", 60),
                    ("netsh int tcp set global rss=enabled", 60)):
        if run_cmd(ctx, cmd, timeout=to) != 0:
            failures.append(cmd)
    # H5-offline risk: release+renew is the dangerous pair — if release
    # succeeds and renew fails (router down, remote session), the machine
    # is left without an IP. Check each side; abort loudly on renew failure.
    if ctx.cancelled():
        from app.utils import TaskCancelled
        raise TaskCancelled("Network reset cancelled by user before DHCP renew.")
    if run_cmd(ctx, "ipconfig /release", timeout=60) != 0:
        failures.append("ipconfig /release")
        ctx.log("  ! DHCP release failed — skipping renew (address left as-is).")
    else:
        if run_cmd(ctx, "ipconfig /renew", timeout=120) != 0:
            raise RuntimeError(
                "DHCP renew FAILED after release — this PC may be offline. "
                "Check your router/cable, then run `ipconfig /renew` manually. "
                "Earlier steps: "
                + ("all ok" if not failures else f"also failed: {', '.join(failures)}")
            )
    if run_cmd(ctx, "ipconfig /flushdns", timeout=30) != 0:
        failures.append("ipconfig /flushdns")
    if failures:
        raise RuntimeError(
            "Network reset partially failed: " + ", ".join(failures)
            + ". A reboot is recommended, then re-run this task."
        )
    ctx.log("Network stack reset to defaults. A reboot is recommended.")


def repair_network_light(ctx: TaskContext):
    """Lighter internet fix: flush DNS + reset Winsock — no IP release/renew.

    repair_network_stack_defaults (Fix Internet Connection) above resets
    the whole stack, and its riskiest moment is the release/renew pair:
    if release succeeds and renew then fails (router down, remote/VPN
    session), the machine is left with no IP at all until the user runs
    `ipconfig /renew` manually. That risk buys real value for a genuinely
    broken stack, but most "my internet feels off" cases are actually
    stale DNS or a corrupted Winsock catalog — this covers exactly those,
    with no step that can end in "no IP address."

    Winsock reset still needs a restart to fully take effect (Windows
    itself says so) — the log tells the user that rather than implying
    the fix is complete the moment the command returns.
    """
    ctx.set_status("Running the light internet fix...")
    failures: list = []
    if run_cmd(ctx, "ipconfig /flushdns", timeout=30) != 0:
        failures.append("flush DNS")
    if ctx.cancelled():
        from app.utils import TaskCancelled
        raise TaskCancelled("Light internet fix cancelled by user.")
    if run_cmd(ctx, "netsh winsock reset", timeout=60) != 0:
        failures.append("reset Winsock")
    if failures:
        raise RuntimeError(
            "Light internet fix partially failed: " + ", ".join(failures)
            + ". Your IP address was never touched either way.")
    ctx.log("DNS flushed and Winsock reset. Restart your PC for the "
           "Winsock reset to fully take effect; your IP address was left "
           "exactly as it was.")


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
    import csv as _csv
    import io as _io
    bad = []
    ok_n = 0
    seen_any = False
    # csv module: FriendlyName may itself contain a comma (e.g.
    # "Samsung SSD 870, EVO") which ConvertTo-Csv quotes — a naive
    # split(",") would misalign the HealthStatus column.
    try:
        reader = _csv.reader(_io.StringIO("\n".join(lines)))
        header = next(reader, None)
        rows = list(reader)
    except Exception:
        rows = []
    for parts in rows:
        if len(parts) < 3:
            continue
        seen_any = True
        name, media, health = parts[0].strip(), parts[1].strip(), parts[2].strip()
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
    if not seen_any:
        raise RuntimeError("Could not parse any drive health rows — check the log.")
    ctx.log(f"VERDICT: all {ok_n} drive(s) Healthy — no action needed.")


def repair_gpu_driver_age(ctx: TaskContext):
    """Driver-age nudge: read the installed GPU driver's date from WMI
    (Win32_VideoController.DriverDate) and flag anything older than a
    year, or older than 6 months with a gentle note. Report-only."""
    ctx.set_status("Checking GPU driver age...")
    import subprocess as _sp
    from datetime import datetime
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
        # M6: WMI DriverDate is local wall-time — compare against local
        # now, not UTC (the old mix skewed boundary verdicts by the TZ
        # offset, up to a day near the 365-day threshold).
        age_days = (datetime.now() - d).days
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
    core_failed = False
    for svc in ("bthHFSrv", "bthserv"):
        rc = run_cmd(ctx, f"net stop {svc} /y", timeout=60)
        if rc == 2:
            ctx.log(f"  {svc} not installed on this PC — skipped.")
    for svc in ("bthserv", "bthHFSrv"):
        rc = run_cmd(ctx, f"net start {svc}", timeout=60)
        # rc 1 = "already running" — a legitimate success on a restart
        if rc in (0, 1):
            started.append(svc)
        elif rc == 2:
            ctx.log(f"  {svc} not installed — nothing to start.")
        else:
            ctx.log(f"  ! could not start {svc} (code {rc})")
            if svc == "bthserv":
                core_failed = True
    if not started:
        # honest failure (repair_smart_verdict pattern): a Bluetooth-less
        # PC must not report "restarted successfully"
        raise RuntimeError(
            "No Bluetooth services found — this PC may not have Bluetooth."
        )
    if core_failed:
        # P5-02: bthHFSrv (handsfree) depends on bthserv (the Bluetooth
        # support service) — if the core did not come up, reporting success
        # would be a false positive. The stack is still down.
        raise RuntimeError(
            "bthserv (Bluetooth support) did not come back up — Bluetooth "
            "will not work until it does. Check Services.msc for its state."
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
    local = os.environ.get("LOCALAPPDATA", "")
    # Gather first so a no-op run never kills Explorer for nothing.
    # Win10/11 live in Microsoft\Windows\Explorer (iconcache_*.db +
    # thumbcache_*.db); root IconCache.db is the Win7/8 legacy name.
    candidates: list = []
    if local and os.path.isabs(local):
        import glob as _glob
        explorer_dir = os.path.join(local, "Microsoft", "Windows", "Explorer")
        for path in ([os.path.join(local, "IconCache.db")]
                     + _glob.glob(os.path.join(local, "iconcache_*.db"))
                     + _glob.glob(os.path.join(explorer_dir, "iconcache_*.db"))
                     + _glob.glob(os.path.join(explorer_dir, "thumbcache_*.db"))):
            try:
                if os.path.isfile(path):
                    candidates.append(path)
            except OSError:
                continue
    if not candidates:
        ctx.log("  ! No icon-cache databases found — nothing to clear, Explorer left running.")
        return
    # Files are locked by the running shell, so Explorer must go down first.
    run_cmd(ctx, "taskkill /f /im explorer.exe", timeout=15)
    removed = 0
    for path in candidates:
        try:
            if os.path.isfile(path):
                os.remove(path)
                removed += 1
                ctx.log(f"Removed stale icon cache: {path}")
        except OSError as exc:
            ctx.log(f"  (kept {path}: {exc})")
    if removed == 0:
        ctx.log("  ! Icon-cache files reappeared or stayed locked — restarting Explorer anyway.")
    # P5-01: never leave the machine without a shell AND never report
    # success when the shell did not come back — same contract as
    # repair_restart_explorer / repair_tray_icons (they raise too).
    if not restart_explorer(ctx):
        if ctx.cancelled():
            ctx.log("  ! cancelled — leaving the Explorer restart to you.")
            return
        raise RuntimeError(
            "Explorer did not come back after the icon-cache rebuild. Press "
            "Ctrl+Shift+Esc, click File > Run new task, type explorer.exe."
        )
    ctx.log(f"Icon cache rebuilt ({removed} database(s) cleared).")


def repair_store_cache_reset(ctx: TaskContext):
    """Reset the Microsoft Store download cache (wsreset): fixes Store and
    Xbox-app downloads that fail, hang, or loop."""
    ctx.set_status("Resetting the Store download cache (wsreset)...")
    run_cmd_checked(ctx, "wsreset.exe", timeout=600, success_codes=(0,))
    ctx.log("Store cache reset — reopen the Store and retry the download.")


def repair_restart_explorer(ctx: TaskContext):
    """Restart Explorer (taskbar, desktop, Start menu) — the classic
    fix for a frozen/garbled shell without rebooting. The race-hardened
    restart_explorer() helper (app.utils) kills, WAITS for full exit,
    respawns with retries, and verifies the new shell survives; this
    task just exposes it as a one-click Repair. Open windows stay open
    (they are separate processes); tray icons of dead apps may vanish —
    that is Explorer behavior, not a failure."""
    ctx.set_status("Restarting Explorer (taskbar will disappear and return)...")
    if not restart_explorer(ctx):
        raise RuntimeError(
            "Explorer did not come back on its own. Press Ctrl+Shift+Esc, "
            "click File > Run new task, type explorer.exe and press Enter."
        )
    ctx.log("Explorer restarted — taskbar and desktop are back.")


def restart_camera_devices(ctx: TaskContext):
    """Power-cycle webcam devices — the streamer's fix for a camera that
    shows a black feed, wrong resolution, or 'camera in use' after a
    crash, without rebooting. Disables then re-enables every present
    Camera/Image-class PnP device (that is exactly what 'unplug and
    replug' does electrically).

    Honesty contract (restart_bluetooth_stack pattern): a PC with no
    camera reports 'nothing found' — never a silent fake success. Apps
    must re-open the camera afterwards (they hold the old handle)."""
    ctx.set_status("Restarting camera devices...")
    ps = (
        "$ErrorActionPreference='Stop';"
        "$cams = Get-PnpDevice -PresentOnly | Where-Object "
        "{ ($_.Class -eq 'Camera') -or ($_.Class -eq 'Image') -or "
        "($_.FriendlyName -like '*webcam*') };"
        "if (-not $cams) { Write-Output 'NO_CAMERAS'; exit 0 };"
        "$failed = 0;"
        "foreach ($c in $cams) {"
        "  try {"
        "    Disable-PnpDevice -InstanceId $c.InstanceId -Confirm:$false -ErrorAction Stop;"
        "    Start-Sleep -Milliseconds 800;"
        "    Enable-PnpDevice -InstanceId $c.InstanceId -Confirm:$false -ErrorAction Stop;"
        "    Write-Output ('RESTARTED: ' + $c.FriendlyName);"
        "  } catch { $failed++; Write-Output ('FAILED: ' + $c.FriendlyName) }"
        "};"
        "if ($failed -gt 0) { exit 3 } else { exit 0 }"
    )
    _cam_out: list = []
    rc = run_cmd(ctx, ["powershell", "-NoProfile", "-Command", ps],
                 shell=False, timeout=180, collect=_cam_out)
    if "NO_CAMERAS" in " ".join(_cam_out):
        raise RuntimeError(
            "No camera found on this PC — nothing to restart "
            "(check Settings > Bluetooth & devices > Cameras)."
        )
    if rc == 3:
        raise RuntimeError(
            "One or more cameras could not be power-cycled — they may be in use. "
            "Close Zoom/Discord/OBS and try again."
        )
    if rc != 0:
        raise RuntimeError(f"Camera restart failed (exit code {rc}) — see the log.")
    ctx.log("Cameras power-cycled. Re-open the camera in your app (old handles die with the restart).")


def run_defender_quick_scan(ctx: TaskContext):
    """One-click Defender quick scan via MpCmdRun (the same engine the
    Security app uses, minus the GUI). Catches active malware behind
    'game won't launch' / random-crash symptoms. Quick scan = memory +
    startup items + common hideouts (minutes, not hours). MpCmdRun exit
    codes: 0 = clean, 2 = THREATS FOUND (that is SUCCESS at finding —
    the log says so, never reported as a tool failure). Anything else
    fails honestly (Defender disabled by third-party AV = the usual
    0x800106B9 family)."""
    import os as _os
    ctx.set_status("Scanning for viruses (quick scan, a few minutes)...")
    mpcmd = None
    pf = _os.environ.get("ProgramFiles", r"C:\Program Files")
    for candidate in (
        _os.path.join(pf, "Windows Defender Advanced Threat Protection") + "\\MpCmdRun.exe",
        _os.path.join(pf, "Windows Defender") + "\\MpCmdRun.exe",
        _os.path.join(_os.environ.get("ProgramFiles(x86)", pf), "Windows Defender") + "\\MpCmdRun.exe",
    ):
        if _os.path.isfile(candidate):
            mpcmd = candidate
            break
    if not mpcmd:
        raise RuntimeError(
            "Defender's scanner (MpCmdRun.exe) was not found — Defender is "
            "likely disabled by a third-party antivirus. Use your AV's own scan."
        )
    rc = run_cmd(ctx, f'"{mpcmd}" -Scan -ScanType 1', timeout=3600)
    if rc == 0:
        ctx.log("Quick scan complete — no threats found.")
        return
    if rc == 2:
        ctx.log("  ! Threats were FOUND and handled by Defender.")
        ctx.log("Open Windows Security > Protection history to review and remove them.")
        return
    raise RuntimeError(
        f"Quick scan did not run (exit code {rc}) — a third-party antivirus "
        "is probably managing this PC, or Defender is turned off."
    )


def find_power_drains(ctx: TaskContext):
    """LAPTOPS ONLY (user ruling 2026-09-06): run `powercfg /energy`, the
    60-second diagnostic that reports exactly what keeps the machine
    awake / drains its battery (background apps blocking sleep, power
    requests, timeout misconfigurations). The HTML report lands in %TEMP%
    and is opened in the browser.

    Gated on battery presence via Win32_Battery: desktop PCs (no battery)
    get an honest skip — /energy findings are about battery/sleep, which
    desktops don't have. (A UPS can appear as a battery; harmless — the
    report is still valid.)

    Honesty: powercfg /energy returns non-zero on some builds even when
    the report was written; success is judged by the report FILE existing."""
    import os as _os
    import subprocess as _sp
    ctx.set_status("Checking power drains (60 seconds — leave the PC idle)...")
    # battery gate — TaskSkipped, not an error: a desktop isn't wrong,
    # this check just isn't for it
    try:
        _proc = _sp.run(
            ["powershell", "-NoProfile", "-Command",
             "if (Get-CimInstance Win32_Battery) { 'yes' } else { 'no' }"],
            capture_output=True, text=True, timeout=30,
            creationflags=getattr(_sp, "CREATE_NO_WINDOW", 0),
        )
        has_battery = "yes" in (_proc.stdout or "")
    except Exception:
        has_battery = True  # can't tell — don't block the task on the probe
    if not has_battery:
        from app.utils import TaskSkipped
        raise TaskSkipped(
            "Laptops only — this PC has no battery, so there is nothing "
            "to drain (desktops don't sleep on battery)."
        )
    out_dir = _os.environ.get("TEMP", _os.environ.get("TMP", "."))
    report = _os.path.join(out_dir, "cleaner-energy-report.html")
    try:
        if _os.path.exists(report):
            _os.remove(report)
    except OSError:
        pass
    run_cmd(ctx, f'powercfg /energy /output "{report}"', timeout=180)
    if not _os.path.exists(report):
        # powercfg writes energy-report(1).html when /output is misquoted
        # on localized builds — fall back to the default name
        fallback = _os.path.join(out_dir, "energy-report.html")
        if _os.path.exists(fallback):
            report = fallback
        else:
            raise RuntimeError(
                "powercfg /energy did not produce a report — try running "
                "the app as Administrator."
            )
    ctx.log(f"Energy report saved: {report}")
    _sp.Popen(["cmd", "/c", "start", "", report], creationflags=getattr(_sp, "CREATE_NO_WINDOW", 0))
    ctx.log("Opening the report — see 'Power Efficiency Diagnostics' errors/warnings for your drains.")


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
    # H5: both sets must land — a half-reset Teredo still fails qualification.
    _fails = [c for c in ("netsh interface teredo set state enterpriseclient",
                          "netsh interface teredo set state servername=default")
              if run_cmd(ctx, c, timeout=60) != 0]
    if _fails:
        raise RuntimeError(f"Teredo reset failed ({len(_fails)}/2 commands) — retry as Administrator.")
    ctx.log("Teredo reset to defaults — retry the Xbox party/game invite.")


_HOSTS_PATH = os.path.join(
    os.environ.get("WINDIR", "C:\\Windows"), "System32", "drivers", "etc", "hosts"
)
# NOTE: deliberately NOT hosts.cleanertool_bak — that name belongs to the
# Tweak tab's Ad Blocker backup (the pre-block original). Overwriting it
# would destroy the Ad Blocker's undo. This repair keeps its own backup.
_HOSTS_REPAIR_BACKUP = _HOSTS_PATH + ".cleanertool_repair_bak"


def _prune_hosts_repair_backups(keep: int = 3) -> None:
    """Keep only the `keep` most recent versioned hosts repair backups
    (DATA-001) — old-format single _HOSTS_REPAIR_BACKUP files, if any,
    are left alone rather than deleted."""
    try:
        folder = os.path.dirname(_HOSTS_REPAIR_BACKUP)
        prefix = os.path.basename(_HOSTS_REPAIR_BACKUP) + "-"
        matches = [f for f in os.listdir(folder) if f.startswith(prefix)]
        matches.sort(reverse=True)  # timestamp suffix sorts lexically = chronologically
        for stale in matches[keep:]:
            try:
                os.remove(os.path.join(folder, stale))
            except Exception:
                pass
    except Exception:
        pass

_STOCK_HOSTS_LINES = [
    "# Copyright (c) 1993-2009 Microsoft Corp.",
    "#",
    "# This is a sample HOSTS file used by Microsoft TCP/IP for Windows.",
    "#",
    "# This file contains the mappings of IP addresses to host names. Each",
    "# entry should be kept on an individual line. The IP address should",
    "# be placed in the first column followed by the corresponding host name.",
    "# The IP address and the host name should be separated by at least one",
    "# space.",
    "#",
    "# Additionally, comments (such as these) may be inserted on individual",
    "# lines or following the machine name denoted by a '#' symbol.",
    "#",
    "# For example:",
    "#",
    "#      102.54.94.97     rhino.acme.com          # source server",
    "#       38.25.63.10     x.acme.com              # x client host",
    "",
    "# localhost name resolution is handled within DNS itself.",
    "#\t127.0.0.1       localhost",
    "#\t::1             localhost",
    "",
]


def repair_hosts_file(ctx: TaskContext):
    """Restore the stock Windows hosts file (fixes damage from other
    ad-blockers/optimizers that left stale redirects behind — a common
    cause of 'site won't load' and Game Pass sign-in failures).

    Backs up the current file once (separate name from the Ad Blocker's
    backup, so its undo is never harmed). If our own Ad Blocker tweak is
    currently applied, its badge is cleared too since the block is gone
    with the old file (re-running the tweak re-applies a fresh block)."""
    ctx.set_status("Restoring the default Windows hosts file...")
    if not os.path.isfile(_HOSTS_PATH):
        raise RuntimeError(f"Hosts file not found at {_HOSTS_PATH}.")
    try:
        with open(_HOSTS_PATH, "r", encoding="utf-8", errors="ignore") as f:
            current = f.read()
    except Exception as exc:
        raise RuntimeError(f"Could not read hosts file: {exc}")
    had_block = "Cleaner Tool AdBlock" in current
    if current.strip() == "\r\n".join(_STOCK_HOSTS_LINES).strip() or \
            current.strip() == "\n".join(_STOCK_HOSTS_LINES).strip():
        ctx.log("Hosts file is already stock — nothing to fix.")
        return
    # DATA-001 fix: this used to be write-once (`if not isfile(...)`), so
    # only the very first repair ever got backed up. Any hosts entries the
    # user added after that (work VPN, self-hosted DNS block, etc.) were
    # destroyed on a second repair with no fresh copy to recover from —
    # the repair tool becoming the very damage it claims to fix. Now backs
    # up every run with a timestamp, keeping the last 3.
    try:
        import time as _time
        stamp = _time.strftime("%Y%m%d-%H%M%S")
        versioned_backup = f"{_HOSTS_REPAIR_BACKUP}-{stamp}"
        with open(versioned_backup, "w", encoding="utf-8") as f:
            f.write(current)
        ctx.log(f"Backed up current hosts file to {versioned_backup}")
        _prune_hosts_repair_backups(keep=3)
        try:
            from app.config_persist import log_security_event
            log_security_event("backup_rotation", f"Hosts repair backup created: {versioned_backup}")
        except Exception:
            pass
    except Exception as exc:
        raise RuntimeError(f"Could not back up hosts file, aborting for safety: {exc}")
    try:
        # F07/F3-2: atomic write via the shared helper — never truncate
        # hosts in place; a mid-write crash bricked DNS. The helper's unique
        # temp name also can't collide with an ad-block write mid-flight
        # (the tweak tab previously reused the same fixed tmp name).
        atomic_write_text(_HOSTS_PATH, "\r\n".join(_STOCK_HOSTS_LINES),
                          newline="")
    except Exception as exc:
        raise RuntimeError(f"Could not write hosts file (need admin rights?): {exc}")
    run_cmd(ctx, "ipconfig /flushdns", timeout=30)
    if had_block:
        # Our own Ad Blocker's block went out with the old file — keep the
        # tweak badge honest so it no longer shows Active.
        try:
            from app.config_persist import mark_tweak_reverted, clear_tweak_snapshot
            mark_tweak_reverted("ad_blocker")
            clear_tweak_snapshot("ad_blocker")
            ctx.log("Cleared the Ad Blocker tweak badge (re-run that tweak to re-block).")
        except Exception:
            ctx.log("Note: re-run Undo on the Ad Blocker tweak to clear its badge.")
    ctx.log("Hosts file restored to Windows defaults.")


def _repair_desktop_dir() -> str:
    """Real Desktop path honoring OneDrive/known-folder redirection (backups
    below must land where the user actually sees them). F3-1: shared resolver
    with strict=True — Desktop feeds shell strings (netsh/dism) while
    elevated, so register values carrying cmd metachars are rejected."""
    return known_folder("Desktop",
                        os.path.join(os.environ.get("USERPROFILE", ""), "Desktop"),
                        strict=True)


def _repair_backups_dir() -> str:
    """UX-001 fix: registry/firewall/driver exports used to land straight
    on the Desktop — commonly OneDrive-synced, so multi-hundred-MB
    registry hives synced slowly and cluttered the visible desktop.
    Routes to Documents\\CleanerTool Backups instead (honors a
    OneDrive-redirected Documents the same way Desktop redirection was
    honored). Created on first use; every caller still logs the exact
    path so discoverability isn't lost, just the location."""
    docs = known_folder("Personal",
                        os.path.join(os.environ.get("USERPROFILE", ""), "Documents"),
                        strict=True)
    dest = os.path.join(docs or os.path.join(os.environ.get("USERPROFILE", ""), "Documents"),
                        "CleanerTool Backups")
    try:
        os.makedirs(dest, exist_ok=True)
    except Exception:
        pass
    return dest


def _repair_stamp() -> str:
    from datetime import datetime
    return datetime.now().strftime("%Y-%m-%d_%H%M%S")


def restart_startmenu_search(ctx: TaskContext):
    """Restart the Start Menu + Windows Search processes — the classic fix
    for a frozen Start button or a dead search box, without rebooting.
    StartMenuExperienceHost.exe and SearchHost.exe both auto-respawn when
    killed (Windows relaunches them on demand); the task VERIFIES both are
    back within ~15s and fails honestly otherwise (a missing process binary
    means a deeper problem than a restart can fix). No admin needed — these
    are the user's own processes."""
    ctx.set_status("Restarting the Start menu and Search...")
    import subprocess as _sp
    for proc in ("StartMenuExperienceHost.exe", "SearchHost.exe"):
        run_cmd(ctx, f"taskkill /f /im {proc}", timeout=15)
    deadline = 15
    import time as _time
    for _ in range(deadline * 2):
        if ctx.cancelled():
            from app.utils import TaskCancelled
            raise TaskCancelled("Start menu restart cancelled by user.")
        try:
            out = _sp.check_output(
                'tasklist /fi "imagename eq StartMenuExperienceHost.exe" /fo csv /nh',
                shell=True, text=True, timeout=10, stderr=_sp.DEVNULL,
                creationflags=getattr(_sp, "CREATE_NO_WINDOW", 0),
            )
            start_ok = "startmenuexperiencehost.exe" in (out or "").lower()
            out2 = _sp.check_output(
                'tasklist /fi "imagename eq SearchHost.exe" /fo csv /nh',
                shell=True, text=True, timeout=10, stderr=_sp.DEVNULL,
                creationflags=getattr(_sp, "CREATE_NO_WINDOW", 0),
            )
            search_ok = "searchhost.exe" in (out2 or "").lower()
        except Exception:
            start_ok = search_ok = False
        if start_ok and search_ok:
            ctx.log("Start menu and Search restarted — try the Start button now.")
            return
        _time.sleep(0.5)
    raise RuntimeError(
        "Start Menu / Search did not come back after restart — reboot, "
        "then run 'Fix Missing Apps / Start Menu' if it is still broken.")


_TRAYNOTIFY_PATH = "Software\\Classes\\Local Settings\\Software\\Microsoft\\Windows\\Shell\\Bags\\TrayNotify"


def repair_tray_icons(ctx: TaskContext):
    """Fix blank/missing taskbar corner (tray) icons: delete the icon-stream
    caches and restart Explorer (verified back — never left without a shell).

    Honesty: newer Windows builds removed these values entirely — when both
    are absent there is nothing to rebuild, so the task skips WITHOUT
    restarting Explorer (verified live: the key is gone on current builds)."""
    ctx.set_status("Rebuilding the taskbar tray icons...")
    has_streams = reg_get_value(ctx, "HKCU", _TRAYNOTIFY_PATH, "IconStreams") is not None
    has_past = reg_get_value(ctx, "HKCU", _TRAYNOTIFY_PATH, "PastIconsStream") is not None
    if not has_streams and not has_past:
        ctx.log("No tray icon cache on this Windows build — nothing to rebuild, "
                "Explorer left running.")
        return
    for name in ("IconStreams", "PastIconsStream"):
        reg_delete_value(ctx, "HKCU", _TRAYNOTIFY_PATH, name)
    if not restart_explorer(ctx):
        raise RuntimeError(
            "Tray caches cleared but Explorer did not restart cleanly — "
            "press Ctrl+Shift+Esc, File > Run new task, type explorer.exe.")
    ctx.log("Tray icons rebuilt — missing icons should be back.")


def backup_firewall_rules(ctx: TaskContext):
    """Export the current Windows Firewall rules to a timestamped .wfw file
    in Documents\\CleanerTool Backups (UX-001: moved off the Desktop —
    OneDrive sync churn/clutter) — the safety net that pairs with 'Reset Firewall'
    (which wipes every per-app rule). Backup-only: changes nothing, returns
    None so the runner never counts it as freed space. Admin required
    (firewall policy is machine-wide)."""
    backups_dir = _repair_backups_dir()
    if not backups_dir or not os.path.isdir(backups_dir):
        raise RuntimeError("Could not find/create the backups folder — nothing was backed up.")
    dest = os.path.join(backups_dir, f"FirewallRules_{_repair_stamp()}.wfw")
    ctx.set_status("Backing up firewall rules...")
    run_cmd_checked(ctx, f'netsh advfirewall export "{dest}"', timeout=120)
    if not os.path.isfile(dest):
        raise RuntimeError("Export reported success but no backup file appeared — nothing was backed up.")
    ctx.log(f"Firewall rules backed up to {dest}")
    ctx.log("To restore later: Windows Defender Firewall > Action > Import Policy.")
    return None


def backup_registry_hives(ctx: TaskContext):
    """Save copies of the SYSTEM, SOFTWARE and current-user registry hives
    to a timestamped folder in Documents\\CleanerTool Backups (UX-001:
    moved off the Desktop) — a restore point for settings.
    Backup-only: changes nothing. Admin required (HKLM hives). Verifies
    every file landed before reporting success."""
    backups_dir = _repair_backups_dir()
    if not backups_dir or not os.path.isdir(backups_dir):
        raise RuntimeError("Could not find/create the backups folder — nothing was backed up.")
    dest_dir = os.path.join(backups_dir, f"RegistryBackup_{_repair_stamp()}")
    try:
        os.makedirs(dest_dir, exist_ok=True)
    except OSError as exc:
        raise RuntimeError(f"Could not create the backup folder: {exc}")
    ctx.set_status("Backing up registry hives...")
    hives = [
        ("HKLM\\SYSTEM", "SYSTEM.hiv"),
        ("HKLM\\SOFTWARE", "SOFTWARE.hiv"),
        ("HKCU", "NTUSER.hiv"),
    ]
    saved = []
    for hive, fname in hives:
        dest = os.path.join(dest_dir, fname)
        # shell=False argv (hive names are constants; the path is quoted by
        # argv conventions, never interpolated into a shell string)
        rc = run_cmd(ctx, ["reg", "save", hive, dest, "/y"],
                     shell=False, timeout=120)
        if rc == 0 and os.path.isfile(dest):
            saved.append(fname)
            ctx.log(f"  Saved {hive} -> {fname}")
        else:
            ctx.log(f"  ! could not save {hive} (exit {rc})")
    if not saved:
        raise RuntimeError("No registry hives could be saved — see the log.")
    if len(saved) < len(hives):
        ctx.log(f"  (partial backup: {len(saved)} of {len(hives)} hives — "
                "the missing ones are logged above)")
    else:
        ctx.log(f"Registry backed up to {dest_dir}")
    return None


def backup_drivers(ctx: TaskContext):
    """Export all third-party drivers to a timestamped folder in
    Documents\\CleanerTool Backups (UX-001: moved off the Desktop)
    (DISM driver export) — the safety net before reinstalling GPU drivers
    with DDU or swapping hardware. Backup-only: changes nothing. Admin
    required. Verifies the folder is non-empty before reporting success."""
    backups_dir = _repair_backups_dir()
    if not backups_dir or not os.path.isdir(backups_dir):
        raise RuntimeError("Could not find/create the backups folder — nothing was backed up.")
    dest_dir = os.path.join(backups_dir, f"DriverBackup_{_repair_stamp()}")
    try:
        os.makedirs(dest_dir, exist_ok=True)
    except OSError as exc:
        raise RuntimeError(f"Could not create the backup folder: {exc}")
    ctx.set_status("Backing up installed drivers (a few minutes)...")
    run_cmd_checked(ctx, f'dism /online /export-driver /destination:"{dest_dir}"',
                    timeout=1800, success_codes=(0,))
    try:
        exported = [e for e in os.listdir(dest_dir)
                    if os.path.isdir(os.path.join(dest_dir, e))]
    except OSError:
        exported = []
    if not exported:
        raise RuntimeError("DISM reported success but no drivers landed — nothing was backed up.")
    ctx.log(f"Backed up {len(exported)} driver package(s) to {dest_dir}")
    ctx.log("To use after a reinstall: Device Manager > device > Update driver > "
            "Browse > point at this folder.")
    return None


def audit_startup_impact(ctx: TaskContext):
    """Report-only startup audit: lists what launches at boot (Run registry
    keys for all users + both Startup folders) so a slow boot can be traced
    to real entries. Changes NOTHING — read-only by design (disabling
    startup entries is a user decision the app won't make silently)."""
    ctx.set_status("Listing what starts with Windows...")
    import glob as _glob
    found: list = []
    # Run keys (per-user + machine)
    try:
        import winreg as _wr
        for hive, path in (
                (_wr.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Run"),
                (_wr.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Run")):
            try:
                with _wr.OpenKey(hive, path) as k:
                    i = 0
                    while True:
                        try:
                            name, value, _t = _wr.EnumValue(k, i)
                        except OSError:
                            break
                        i += 1
                        found.append(f"[Run] {name} = {str(value)[:90]}")
            except OSError:
                continue
    except Exception:
        pass
    # Startup folders (per-user + common)
    for folder in (
            os.path.join(os.environ.get("APPDATA", ""),
                         r"Microsoft\Windows\Start Menu\Programs\Startup"),
            os.path.join(os.environ.get("ProgramData", r"C:\ProgramData"),
                         r"Microsoft\Windows\Start Menu\Programs\Startup")):
        try:
            if folder and os.path.isdir(folder):
                for entry in sorted(os.listdir(folder)):
                    found.append(f"[Startup folder] {entry}")
        except OSError:
            continue
    if not found:
        ctx.log("Nothing launches at boot besides Windows itself — startup is clean.")
        return None
    ctx.log(f"{len(found)} startup entr{'y' if len(found) == 1 else 'ies'} found:")
    for line in found:
        ctx.log(f"  {line}")
    ctx.log("Audit only — nothing was changed. To remove one: Task Manager > "
            "Startup apps (right-click > Disable).")
    return None


def read_mic_consent() -> tuple:
    """(consent_value_or_None, [(last_used_filetime, app_key), ...]).

    Shared by the repair audit and the Tools mic dialog — one reader so
    both surfaces agree. Read-only; never raises."""
    try:
        import winreg as _wr
        try:
            with _wr.OpenKey(_wr.HKEY_CURRENT_USER,
                             r"Software\Microsoft\Windows\CurrentVersion\CapabilityAccessManager\ConsentStore\microphone") as k:
                try:
                    value, _ = _wr.QueryValueEx(k, "Value")
                except OSError:
                    value = None
                apps: list = []
                i = 0
                while True:
                    try:
                        sub = _wr.EnumKey(k, i)
                    except OSError:
                        break
                    i += 1
                    try:
                        with _wr.OpenKey(k, sub) as sk:
                            try:
                                last, _ = _wr.QueryValueEx(sk, "LastUsedTimeStop")
                            except OSError:
                                last = 0
                            apps.append((int(last or 0), sub))
                    except OSError:
                        continue
                apps.sort(reverse=True)
                return value, apps
        except OSError:
            return None, []
    except Exception:
        return None, []


def list_capture_devices() -> list:
    """[(friendly_label, state_int), ...] for every capture endpoint.

    state: 1 = active, 2 = disabled, 4 = not present, 8 = unplugged
    (anything else = unknown). Read-only; never raises."""
    out: list = []
    try:
        import winreg as _wr2
        with _wr2.OpenKey(_wr2.HKEY_LOCAL_MACHINE,
                          r"SOFTWARE\Microsoft\Windows\CurrentVersion\MMDevices\Audio\Capture") as ck:
            n = _wr2.QueryInfoKey(ck)[0]
            for i in range(n):
                try:
                    sub = _wr2.EnumKey(ck, i)
                    with _wr2.OpenKey(ck, sub) as ek:
                        try:
                            state, _ = _wr2.QueryValueEx(ek, "DeviceState")
                        except OSError:
                            state = -1
                        name, iface = sub, ""
                        try:
                            with _wr2.OpenKey(ek, "Properties") as pk:
                                try:
                                    name, _ = _wr2.QueryValueEx(
                                        pk, "{a45c254e-df1c-4efd-8020-67d146a850e0},2")
                                except OSError:
                                    pass
                                try:
                                    iface, _ = _wr2.QueryValueEx(
                                        pk, "{b3f8fa53-0004-438e-9003-51a46e139bfc},6")
                                except OSError:
                                    pass
                        except OSError:
                            pass
                        label = name if not iface or iface == name else f"{name} ({iface})"
                        out.append((label, int(state)))
                except OSError:
                    continue
    except Exception:
        pass
    return out


def audit_mic_consent(ctx: TaskContext):
    """Report-only microphone audit: the classic 'nobody hears me in
    Discord/VALORANT' is usually revoked Windows mic privacy, not a
    driver. Reads the user-level mic consent, which apps used the mic
    last, and which capture devices Windows sees right now. Changes
    NOTHING — read-only by design (the fix is the 'Allow Microphone
    For Apps' tweak, which snapshots + reverts)."""
    ctx.set_status("Checking microphone access...")
    lines: list = []
    blocked = False
    value, apps = read_mic_consent()
    if value is None and not apps:
        lines.append("Microphone consent store not found (very old Windows?)")
    else:
        lines.append(f"Microphone privacy for this user: {value}")
        if isinstance(value, str) and value != "Allow":
            blocked = True
        for _last, sub in apps[:5]:
            lines.append(f"  mic app on file: {sub}")
    # capture devices Windows sees (read-only MMDevices scan)
    live = 0
    try:
        devs = list_capture_devices()
    except Exception as exc:
        devs = []
        lines.append(f"Could not list capture devices: {exc}")
    for label, state in devs:
        if state == 1:
            live += 1
            lines.append(f"  mic ready: {label}")
    if devs and live == 0:
        lines.append("  no active capture device seen (mic unplugged or disabled)")
    for line in lines:
        ctx.log(line)
    if blocked:
        raise RuntimeError("Microphone access is OFF for this Windows user — "
                           "no game or chat app can hear you. Fix: Settings > "
                           "Privacy & security > Microphone > turn on 'Microphone "
                           "access', or run the 'Allow Microphone For Apps' tweak.")
    ctx.log("Audit only — nothing was changed.")
    return None


def repair_nvidia_app_services(ctx: TaskContext):
    """Restarts the NVIDIA background service trio behind 'NVIDIA App
    won't open / stuck on splash'. Any of the three may be absent
    depending on which NVIDIA software is installed — that's normal,
    not a failure. Counted honestly per-service, never silently
    swallowed."""
    from app.utils import TaskSkipped, TaskCancelled
    ctx.set_status("Restarting NVIDIA background services...")
    services = ["NvContainerLocalSystem", "NvContainerNetworkService",
                "NVDisplay.ContainerLocalSystem"]
    found = ok = 0
    for svc in services:
        if ctx.cancelled():
            raise TaskCancelled("NVIDIA App repair cancelled by user.")
        if run_cmd(ctx, f"sc query {svc}", timeout=15) != 0:
            continue  # not installed on this machine — no-op, not a failure
        found += 1
        run_cmd(ctx, f"net stop {svc} /y", timeout=30)
        if ctx.cancelled():
            raise TaskCancelled("NVIDIA App repair cancelled by user.")
        rc = run_cmd(ctx, f"net start {svc}", timeout=30)
        if rc == 0:
            ok += 1
        else:
            ctx.log(f"  ! {svc} did not restart cleanly (exit {rc})")
    if found == 0:
        raise TaskSkipped("No NVIDIA container services found — no NVIDIA App installed.")
    if ok == 0:
        raise RuntimeError("Found NVIDIA services but none restarted cleanly — see log.")
    ctx.log(f"Restarted {ok}/{found} NVIDIA background service(s). Relaunch the NVIDIA App.")


# --------------------------------------------------------------------------- #
# Installer tasks MOVED to app/tasks/install_tasks.py (Install tab) — every
# internet-required task lives there now, per the user's 4th-tab request.
# Repair keeps the repair-only identity: fix what exists, don't install
# what doesn't.
# --------------------------------------------------------------------------- #

TASKS = [
    Task("restore_point", "Safety Checkpoint", "Saves a restore point you can go back to", create_restore_point, default=True),
    Task("xbox_apps", "Fix Xbox / Game Pass Apps", "Repairs the Xbox app and Game Pass sign-in without reinstalling", repair_xbox_game_apps, default=False),
    Task("ssd_maintenance", "SSD Maintenance", "Retrims your SSD and checks drive health", repair_ssd_maintenance, default=False),
    Task("vss_repair", "Fix Restore Points (VSS)", "Restarts the shadow-copy service so checkpoints work again", repair_vss_restore_points, default=False),
    Task("network_reset", "Fix Internet Connection", "Resets internet settings to defaults, repairs bad tweaks", repair_network_stack_defaults, default=False, risk="REBOOT REQUIRED"),
    Task("network_light", "Fix Internet (Light)", "Flushes DNS and resets Winsock without touching your IP address — try this first", repair_network_light, default=False),
    Task("sfc_scan", "Fix System Files", "Scans and fixes broken Windows files that crash games", repair_sfc_scan, default=False),
    Task("dism_restorehealth", "Repair Windows Image", "Downloads fresh Windows files to fix a broken image", repair_dism_restorehealth, default=False),
    Task("dism_cleanup", "Cleanup Update Storage", "Cleans old update leftovers but keeps uninstall option", repair_dism_component_cleanup, default=False),
    Task("dism_scanhealth", "Scan Image Health", "Scans Windows image for corruption", repair_dism_scanhealth, default=False),
    Task("dism_checkhealth", "Check Image Health", "Quick check if image needs repair", repair_dism_checkhealth, default=False),
    Task("chkdsk_scan", "Check Disk (Read-Only)", "Checks your drive for errors without restarting", repair_chkdsk_scan, default=False),
    Task("wu_reset", "Fix Stuck Updates", "Fixes Windows Update when it’s stuck or failing", repair_windows_update_reset, default=False),
    Task("bits_reset", "Fix Download Queue", "Clears stuck download jobs that block updates", repair_bits_reset, default=False),
    Task("time_sync", "Fix Clock Sync", "Fixes wrong clock that breaks updates and logins", repair_time_sync, default=False),
    Task("gpupdate", "Fix Blocked Settings", "Refreshes Windows rules that may block Game Mode", repair_gpupdate, default=False),
    Task("search_index", "Fix Search Not Working", "Rebuilds Windows search that finds files and apps", repair_search_index, default=False),
    Task("print_spooler", "Fix Stuck Printing", "Clears stuck print jobs and restarts printer", repair_print_spooler, default=False),
    Task("wmi_repair", "Fix Game Services (WMI)", "Fixes system database many games rely on", repair_wmi_repository, default=False),
    Task("store_apps_reregister", "Fix Missing Apps / Start Menu", "Fixes missing apps or Start menu without reinstall", repair_reregister_store_apps, default=False),
    Task("restart_audio", "Restart Sound / Mic", "Fixes dead audio or mic instantly without a reboot", restart_audio_engine, default=False, admin_required=True),
    Task("firewall_reset", "Reset Firewall", "Fixes multiplayer/anti-cheat connection errors by resetting firewall rules", repair_firewall_reset, default=False, admin_required=True, risk="ADVANCED"),
    Task("smart_verdict", "Drive Health Verdict (SMART)", "Plain-language check: is your SSD/HDD healthy, or time to back up?", repair_smart_verdict, default=False, admin_required=False),
    Task("gpu_driver_age", "GPU Driver Freshness", "Checks your graphics driver's age and nudges you to update if it's stale", repair_gpu_driver_age, default=False, admin_required=False),
    Task("restart_bluetooth", "Restart Bluetooth", "Fixes wireless controller and headset drops without rebooting", restart_bluetooth_stack, default=False, admin_required=True),
    Task("arp_flush", "Fix IP Conflicts (ARP)", "Clears stuck network address entries that break router talk", flush_arp_cache, default=False, admin_required=True),
    Task("gpu_reset", "Restart Graphics Driver", "Fixes black screens and resolution bugs instantly, no reboot", reset_graphics_driver, default=False, admin_required=False),
    Task("anticheat_repair", "Fix Anti-Cheat Errors", "Resets EasyAntiCheat and BattlEye to fix launch errors like 30005", repair_anticheat_services, default=False, admin_required=True),
    Task("icon_cache", "Fix Blank Icons", "Rebuilds the icon cache that causes blank or white desktop icons", repair_icon_cache, default=False, admin_required=False),
    Task("restart_explorer", "Restart Explorer", "Fixes a frozen taskbar, desktop or Start menu without rebooting", repair_restart_explorer, default=False, admin_required=False),
    Task("restart_camera", "Restart Camera (Webcam)", "Power-cycles your webcam to fix black feeds and 'camera in use' errors", restart_camera_devices, default=False, admin_required=True),
    Task("defender_quick_scan", "Run Security Scan", "Quick Defender virus scan for the malware behind game crashes", run_defender_quick_scan, default=False, admin_required=False),
    Task("power_drains", "Find Power Drains (Laptops)", "Finds what's draining your battery and blocking sleep — opens a report", find_power_drains, default=False, admin_required=True),
    Task("wsreset_store", "Reset Store Downloads", "Resets the Store cache when app downloads fail or hang", repair_store_cache_reset, default=False, admin_required=False),
    Task("enable_restore", "Turn On System Protection", "Re-enables restore points on C: if something turned them off", repair_enable_system_restore, default=False, admin_required=True),
    Task("power_plans", "Reset Power Plans", "Restores Microsoft's default power plans when optimizers break them", repair_power_plans, default=False, admin_required=True),
    Task("teredo_fix", "Fix Xbox Multiplayer (Teredo)", "Resets Teredo tunneling to fix Xbox party and matchmaking errors", repair_teredo, default=False, admin_required=True),
    Task("hosts_restore", "Restore Hosts File", "Restores the stock hosts file when blockers break sites or logins", repair_hosts_file, default=False, admin_required=True),
    # User-requested additions 2026-09-12 (layman-safe only): instant fixes
    # without rebooting, safety-net backups, and a report-only startup audit.
    # All default=False (Custom-only); presets untouched by design.
    Task("restart_startmenu", "Restart Start Menu & Search", "Fixes a frozen Start button or dead search box instantly, no reboot", restart_startmenu_search, default=False, admin_required=False),
    Task("tray_icons", "Fix Missing Tray Icons", "Rebuilds the taskbar corner icons that disappear or turn blank", repair_tray_icons, default=False, admin_required=False),
    Task("firewall_backup", "Back Up Firewall Rules", "Saves your firewall rules to the Desktop before you ever reset them", backup_firewall_rules, default=False, admin_required=True),
    Task("registry_backup", "Back Up Registry", "Saves a copy of your system settings to the Desktop as a safety net", backup_registry_hives, default=False, admin_required=True),
    Task("driver_export", "Back Up Drivers", "Saves copies of your working drivers to the Desktop before reinstalling any", backup_drivers, default=False, admin_required=True),
    Task("startup_audit", "Check Startup Programs", "Shows what slows your boot; changes nothing, removes nothing", audit_startup_impact, default=False, admin_required=False),
    Task("micfix_audit", "Why Can't They Hear Me?", "Diagnoses mic-not-heard in games/chat from privacy + device state; changes nothing", audit_mic_consent, default=False, admin_required=False),
    Task("nvidia_app_fix", "Fix NVIDIA App Won't Open", "Restarts NVIDIA background services behind splash-screen hangs", repair_nvidia_app_services, default=False, admin_required=True),
]