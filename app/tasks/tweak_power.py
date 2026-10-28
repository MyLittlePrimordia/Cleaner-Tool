"""Tweak tab — Power, storage and boot tweaks: the Ultimate Performance plan, CPU/USB/disk power,
SSD prefetch/TRIM, Fast Startup, startup delay, keyboard repeat.

Split out of app/tasks/tweak_tasks.py (audit 2026-10, R-002 stage 2). Every
function here is one Task's apply/revert/verify implementation; the shared
constants and primitives live in tweak_common, and `TASKS` is assembled in
tweak_tasks.py so the registry still reads as one literal list.
"""
from __future__ import annotations

from app.tasks.tweak_common import (  # noqa: F401 - re-exported
    BALANCED_GUID,
    TaskContext,
    _HIBERBOOT_SPECS,
    _KEYBOARD_SPECS,
    _MAX_CPU_POWER_FALLBACK,
    _MAX_CPU_POWER_PAIRS,
    _POWER_THROTTLE_SPECS,
    _SSD_PREFETCH_PATH,
    _SSD_PREFETCH_SPECS,
    _USB_SUSPEND_FALLBACK,
    _USB_SUSPEND_PAIRS,
    _USB_SUSPEND_SETTING,
    _USB_SUSPEND_SUBGROUP,
    __verify_read,
    _has_ssd,
    _regn_apply,
    _regn_revert,
    _reserved_storage_enabled,
    _restore_reg_values,
    _snap_reg_values,
    _verify_all_values,
    _verify_value,
    clear_tweak_snapshot,
    get_tweak_snapshot,
    reg_delete_value,
    reg_set_value_checked,
)
from app.utils import (  # noqa: F401 - re-exported
    TaskSkipped,
    run_cmd,
    run_cmd_checked,
    sc_query_start_type,
)
from app.tasks.tweak_snapshots import (  # noqa: F401 - re-exported
    ULTIMATE_PERF_GUID,
    _active_scheme_guid,
    _find_ultimate_scheme_guid,
    _guids_equal,
    _restore_powercfg_pairs,
    _snapshot_powercfg_pairs,
    _snapshot_validation_error,
    _valid_snapshot_guid,
    _valid_snapshot_start_type,
)
from app.config_persist import save_tweak_snapshot  # noqa: F401 - re-exported



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
    # Snapshot-failure hygiene (C5): the snapshot is taken before the
    # mutating commands, so a later failure must NOT leave it behind —
    # get_tweak_state() treats any snapshot as "✓ Active". Only clear what
    # WE created (a pre-existing snapshot from an earlier success stays).
    prior_guid = _active_scheme_guid()
    had_snapshot = bool(get_tweak_snapshot("ultimate_performance"))
    if prior_guid:
        save_tweak_snapshot("ultimate_performance", {"active_scheme": prior_guid})

    try:
        guid = _find_ultimate_scheme_guid()
        if guid is None:
            # No scheme yet -> duplicate the hidden template, then re-detect
            # via the registry (language- and overlay-safe).
            rc = run_cmd(ctx, ["powercfg", "-duplicatescheme", ULTIMATE_PERF_GUID],
                   shell=False)
            guid = _find_ultimate_scheme_guid()
            if rc != 0 or guid is None:
                raise RuntimeError(
                    "Could not create the Ultimate Performance power plan on this "
                    f"PC (powercfg returned {rc}). The plan may not be supported "
                    "by this Windows edition."
                )
        else:
            ctx.log("Ultimate Performance plan already present — reusing it (no duplicate created).")

        rc2 = run_cmd(ctx, ["powercfg", "-setactive", guid], shell=False)
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
    except Exception:
        if not had_snapshot:
            clear_tweak_snapshot("ultimate_performance")
        raise
    ctx.log(f"Verified: active power plan is now Ultimate Performance ({guid}).")


def revert_ultimate_performance(ctx: TaskContext):
    """Restore the power plan that was active BEFORE this tweak ran (from
    the M1 snapshot), falling back to Balanced only if no snapshot exists."""
    snapshot = get_tweak_snapshot("ultimate_performance")
    target = snapshot.get("active_scheme") if snapshot else None
    # F-1: the snapshot value flows into a shell command string — only a
    # canonical GUID this app could have parsed from powercfg may run.
    if target is not None and not _valid_snapshot_guid(target):
        raise _snapshot_validation_error("ultimate_performance", "active_scheme", target)
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
    # Revert-failure hygiene (C6): only drop the snapshot after a VERIFIED
    # restore. Clearing unconditionally would destroy the true original on
    # a failed undo, and the next apply would snapshot the tweak's own
    # output as "prior state".
    rc = run_cmd(ctx, ["powercfg", "-setactive", target], shell=False)
    active = _active_scheme_guid()
    if rc != 0 or active is None or not _guids_equal(active, target):
        raise RuntimeError(
            f"Could not restore power plan (active={active}, wanted {target}, "
            f"exit {rc}) — snapshot kept so Undo can be retried. If this "
            "persists, switch plans manually in Settings > Power."
        )
    ctx.log(f"Restored power plan {target}.")
    clear_tweak_snapshot("ultimate_performance")


def apply_fast_startup_fix(ctx: TaskContext):
    # Correct: disable Fast Startup via HiberbootEnabled, not hibernate off
    # M3: snapshot priors — the old revert hardcoded 1.
    had_snapshot = bool(get_tweak_snapshot("disable_fast_startup"))
    _snap_reg_values(ctx, "disable_fast_startup", _HIBERBOOT_SPECS)
    try:
        reg_set_value_checked(ctx, "HKLM", "SYSTEM\\CurrentControlSet\\Control\\Session Manager\\Power", "HiberbootEnabled", 0)
    except Exception:
        if not had_snapshot:
            clear_tweak_snapshot("disable_fast_startup")
        raise


def revert_fast_startup_fix(ctx: TaskContext):
    snap = get_tweak_snapshot("disable_fast_startup")
    if snap and "specs" in snap:
        _restore_reg_values(ctx, "disable_fast_startup")
    else:
        reg_set_value_checked(ctx, "HKLM", "SYSTEM\\CurrentControlSet\\Control\\Session Manager\\Power", "HiberbootEnabled", 1)
        ctx.log("  (no snapshot found — restored to documented Windows default instead)")


def apply_power_throttling_off(ctx: TaskContext):
    """Disable CPU power throttling for consistent performance."""
    # M3: snapshot priors — the old revert deleted unconditionally.
    had_snapshot = bool(get_tweak_snapshot("power_throttling_off"))
    _snap_reg_values(ctx, "power_throttling_off", _POWER_THROTTLE_SPECS)
    try:
        reg_set_value_checked(ctx, "HKLM", "SYSTEM\\CurrentControlSet\\Control\\Power\\PowerThrottling", "PowerThrottlingOff", 1)
    except Exception:
        if not had_snapshot:
            clear_tweak_snapshot("power_throttling_off")
        raise


def revert_power_throttling_off(ctx: TaskContext):
    snap = get_tweak_snapshot("power_throttling_off")
    if snap and "specs" in snap:
        _restore_reg_values(ctx, "power_throttling_off")
    else:
        reg_delete_value(ctx, "HKLM", "SYSTEM\\CurrentControlSet\\Control\\Power\\PowerThrottling", "PowerThrottlingOff")
        ctx.log("  (no snapshot found — removed the value; Windows then uses its default)")


def apply_startup_delay(ctx: TaskContext):
    ctx.set_status("Startup delay...")
    # MED-007: Undo used to blind-delete this value, destroying a
    # pre-existing different setting. The real prior state is
    # snapshotted now, so Undo restores what was actually there,
    # "absent" included.
    _regn_apply(ctx, 'startup_delay', [("HKCU", "Software\\Microsoft\\Windows\\CurrentVersion\\Explorer\\Serialize", "StartupDelayInMSec", 0)])
    ctx.log("Startup delay applied.")


def revert_startup_delay(ctx: TaskContext):
    # MED-007: was a blind `reg_delete_value(...)`, which destroyed
    # a pre-existing different value. Now restores the snapshot.
    _regn_revert(ctx, 'startup_delay', "Startup delay")


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
    # MED-007: the old revert hardcoded index 1, which happens to be the
    # Windows default - but "the default" is not "what this user had". A
    # machine that had already disabled selective suspend (or set some other
    # index) had that value silently overwritten by Undo. Snapshot the real
    # AC and DC indexes first, like every other powercfg tweak here.
    had_snapshot = bool(get_tweak_snapshot("usb_suspend"))
    _snapshot_powercfg_pairs(ctx, "usb_suspend", _USB_SUSPEND_PAIRS)
    rc_ac = run_cmd(ctx, ["powercfg", "/setacvalueindex", "scheme_current",
                              _USB_SUSPEND_SUBGROUP, _USB_SUSPEND_SETTING, "0"],
                   shell=False)
    rc_dc = run_cmd(ctx, ["powercfg", "/setdcvalueindex", "scheme_current",
                              _USB_SUSPEND_SUBGROUP, _USB_SUSPEND_SETTING, "0"],
                   shell=False)
    if rc_ac != 0 and rc_dc != 0:
        # L-1 fix: had_snapshot was captured BEFORE the snapshot was taken. The
        # old guard read get_tweak_snapshot() AFTER taking it, so it was always
        # truthy and a stale Undo record was never dropped.
        if not had_snapshot:
            clear_tweak_snapshot("usb_suspend")
        raise TaskSkipped(
            "USB selective suspend setting not present in this power plan — "
            "skipping (nothing to change on this PC)."
        )
    if rc_ac != 0 or rc_dc != 0:
        ctx.log("  (one power side accepted the change, the other is not present — applied what exists)")
    # B-0xx: honor the activate result. The written index only takes effect once
    # the scheme is re-activated; a silent failure used to report success.
    rc_act = run_cmd(ctx, "powercfg /setactive scheme_current")
    if rc_act != 0:
        raise RuntimeError(
            f"USB suspend setting was written but the power plan could not be "
            f"re-activated (powercfg /setactive exit {rc_act}).")


def revert_usb_suspend(ctx: TaskContext):
    # MED-007: was a hardcoded `... 1` on both sides. Now restores the
    # snapshotted AC and DC indexes, falling back to the documented default
    # only when no snapshot exists (and saying so).
    _restore_powercfg_pairs(ctx, "usb_suspend", _USB_SUSPEND_PAIRS,
                            _USB_SUSPEND_FALLBACK)
    # B-0xx: _restore_powercfg_pairs already checked /setactive and raised on
    # failure, so its own call covers the revert path. This trailing activate is
    # now checked too rather than silently discarded.
    rc_act = run_cmd(ctx, "powercfg /setactive scheme_current")
    if rc_act != 0:
        ctx.log(f"  ! could not re-activate the power plan (code {rc_act}) — "
                "the restored values may not take effect until you re-open "
                "Power Options.")


def apply_disk_timeout(ctx: TaskContext):
    # NOTE: powercfg /change aliases have no per-machine snapshot (the
    # underlying GUID pair differs by scheme); apply/reset use documented
    # Windows defaults (0 = never spin down on AC/DC; revert = 20 min).
    # Return codes are honored so LTSC/stripped schemes fail honestly.
    rc_ac = run_cmd(ctx, "powercfg /change disk-timeout-ac 0")
    rc_dc = run_cmd(ctx, "powercfg /change disk-timeout-dc 0")
    if rc_ac != 0 and rc_dc != 0:
        raise TaskSkipped(
            "Disk-timeout setting not present in this power plan — "
            "skipping (nothing to change on this PC)."
        )
    if rc_ac != 0 or rc_dc != 0:
        ctx.log("  (one power side accepted the change, the other is not present — applied what exists)")


def revert_disk_timeout(ctx: TaskContext):
    # Restores the documented Windows default (20 min), not necessarily the
    # exact prior value — see apply_disk_timeout note.
    rc_ac = run_cmd(ctx, "powercfg /change disk-timeout-ac 20")
    rc_dc = run_cmd(ctx, "powercfg /change disk-timeout-dc 20")
    if rc_ac != 0 and rc_dc != 0:
        raise RuntimeError("Could not restore disk timeout (powercfg rejected both sides).")


def apply_keyboard_tuning(ctx: TaskContext):
    # Snapshot first: the old revert wrote Speed=31 (identical to apply),
    # so a non-default prior Speed was never restored (M4).
    had_snapshot = bool(get_tweak_snapshot("keyboard_tuning"))
    _snap_reg_values(ctx, "keyboard_tuning", _KEYBOARD_SPECS)
    try:
        reg_set_value_checked(ctx, "HKCU", "Control Panel\\Keyboard", "KeyboardDelay", "0", value_type="REG_SZ")
        reg_set_value_checked(ctx, "HKCU", "Control Panel\\Keyboard", "KeyboardSpeed", "31", value_type="REG_SZ")
    except Exception:
        if not had_snapshot:
            clear_tweak_snapshot("keyboard_tuning")
        raise


def revert_keyboard_tuning(ctx: TaskContext):
    # Exact prior values when snapshotted; documented Windows defaults
    # (Delay 1 = 250ms, Speed 31) only when applied by an older version
    # without a snapshot.
    snap = get_tweak_snapshot("keyboard_tuning")
    if snap and "specs" in snap:
        _restore_reg_values(ctx, "keyboard_tuning", value_type="REG_SZ")
    else:
        reg_set_value_checked(ctx, "HKCU", "Control Panel\\Keyboard", "KeyboardDelay", "1", value_type="REG_SZ")
        reg_set_value_checked(ctx, "HKCU", "Control Panel\\Keyboard", "KeyboardSpeed", "31", value_type="REG_SZ")
        ctx.log("  (no snapshot found — restored to documented Windows defaults instead)")


def apply_ssd_trim(ctx: TaskContext):
    """Enable TRIM/DisableDeleteNotify for SSD."""
    # B5 audit fix: these skip paths used to log + `return` — the runner
    # counted them as applied and the GUI badged '✓ Active' although
    # nothing changed. TaskSkipped = completed-with-skip (no badge).
    if not _has_ssd():
        raise TaskSkipped("No SSD detected — skipping TRIM tweak (only applies to SSDs).")
    rc = run_cmd(ctx, "fsutil behavior set disabledeletenotify 0")
    if rc != 0:
        raise RuntimeError(f"Could not enable TRIM (fsutil exited {rc}).")


def revert_ssd_trim(ctx: TaskContext):
    # M5: restore regardless of current _has_ssd() (cached, and the disk
    # may have changed since apply) — `disabledeletenotify 0` (TRIM
    # enabled) is the safe Windows default on any disk. Revert restores
    # the default (TRIM enabled = 0), never disables TRIM (1).
    # Disabling TRIM harms SSD performance/lifespan.
    rc = run_cmd(ctx, "fsutil behavior set disabledeletenotify 0")
    if rc != 0:
        raise RuntimeError(f"Could not restore TRIM default (fsutil exited {rc}).")


def apply_ssd_superfetch(ctx: TaskContext):
    """Disable SysMain (Superfetch) — unnecessary on SSD/NVMe."""
    if not _has_ssd():
        raise TaskSkipped("No SSD detected — skipping SysMain tweak (only applies to SSDs).")
    # M5: snapshot the real prior start type (auto/demand/disabled/...) so
    # revert restores it instead of assuming `auto`.
    had_snapshot = bool(get_tweak_snapshot("ssd_superfetch"))
    prior = sc_query_start_type(ctx, "SysMain")
    if prior is not None:
        save_tweak_snapshot("ssd_superfetch", {"start_type": prior})
    rc = run_cmd(ctx, "sc config SysMain start= disabled")
    if rc != 0:
        if not had_snapshot:
            clear_tweak_snapshot("ssd_superfetch")
        raise RuntimeError(f"Could not disable SysMain (sc exited {rc}).")
    run_cmd(ctx, "net stop SysMain")


def revert_ssd_superfetch(ctx: TaskContext):
    # M5: restore regardless of current _has_ssd() — the disk may have
    # changed since apply, and the service exists on HDD machines too.
    snap = get_tweak_snapshot("ssd_superfetch")
    target = snap.get("start_type") if snap else None
    if not target:
        target = "auto"
        ctx.log("  (no saved prior SysMain start type — restoring `auto`, the Windows default)")
    elif not _valid_snapshot_start_type(target):
        # F-1: the snapshot value flows into a shell command string — only
        # a start-type keyword this app could have snapshotted may run.
        raise _snapshot_validation_error("ssd_superfetch", "start_type", target)
    rc = run_cmd(ctx, ["sc", "config", "SysMain", "start=", target], shell=False)
    if rc != 0:
        raise RuntimeError(f"Could not restore SysMain start type (sc exited {rc}) — snapshot kept.")
    run_cmd(ctx, "net start SysMain")
    clear_tweak_snapshot("ssd_superfetch")


def apply_ssd_last_access(ctx: TaskContext):
    """Disable last access timestamp updates (NtfsDisableLastAccessUpdate)."""
    if not _has_ssd():
        raise TaskSkipped("No SSD detected — skipping last access tweak (only applies to SSDs).")
    # M-8 fix: snapshot the prior (value OR absence) so Undo restores what the
    # user actually had, instead of forcing 0 — the documented default is not
    # necessarily "what this user had".
    _regn_apply(ctx, "ssd_last_access",
                [("HKLM", "SYSTEM\\CurrentControlSet\\Control\\FileSystem",
                  "NtfsDisableLastAccessUpdate", 1)])


def revert_ssd_last_access(ctx: TaskContext):
    # P7-01: no _has_ssd() gate — same contract as revert_ssd_prefetch /
    # revert_ssd_superfetch / revert_ssd_trim: restore regardless of the
    # current disk (the probe is cached, and the disk may have changed
    # since apply). M-8: restore the snapshotted prior value, not a forced 0.
    _regn_revert(ctx, "ssd_last_access", "Last-access updates")


def apply_ssd_prefetch(ctx: TaskContext):
    """Disable Prefetcher and Superfetch for SSD."""
    if not _has_ssd():
        raise TaskSkipped("No SSD detected — skipping prefetch tweak (only applies to SSDs).")
    # M5: snapshot exact priors — the old revert hardcoded 3/3, losing
    # custom values like 1/2.
    had_snapshot = bool(get_tweak_snapshot("ssd_prefetch"))
    _snap_reg_values(ctx, "ssd_prefetch", _SSD_PREFETCH_SPECS)
    try:
        reg_set_value_checked(ctx, "HKLM", _SSD_PREFETCH_PATH,
                      "EnablePrefetcher", 0)
        reg_set_value_checked(ctx, "HKLM", _SSD_PREFETCH_PATH,
                      "EnableSuperfetch", 0)
    except Exception:
        if not had_snapshot:
            clear_tweak_snapshot("ssd_prefetch")
        raise


def revert_ssd_prefetch(ctx: TaskContext):
    # Exact priors when snapshotted (e.g. by this version); documented
    # Windows default 3/3 only for pre-snapshot applies. No _has_ssd gate:
    # restore must work even if the disk changed since apply.
    snap = get_tweak_snapshot("ssd_prefetch")
    if snap and "specs" in snap:
        _restore_reg_values(ctx, "ssd_prefetch")
    else:
        reg_set_value_checked(ctx, "HKLM", _SSD_PREFETCH_PATH,
                      "EnablePrefetcher", 3)
        reg_set_value_checked(ctx, "HKLM", _SSD_PREFETCH_PATH,
                      "EnableSuperfetch", 3)
        ctx.log("  (no snapshot found — restored to documented Windows defaults instead)")


def apply_max_cpu_power(ctx: TaskContext):
    """Aggressive CPU boost + no core parking + 100% min processor state (AC).
    Biggest single CPU-latency win for gaming laptops and many desktops."""
    # M1 fix: snapshot real current values before overwriting them.
    # H5/C5: the old unchecked run_cmd calls logged success on LTSC/VMs
    # where powercfg exits non-zero — and left a snapshot behind (false
    # badge). Checked writes + snapshot cleanup on failure.
    had_snapshot = bool(get_tweak_snapshot("max_cpu_power"))
    _snapshot_powercfg_pairs(ctx, "max_cpu_power", _MAX_CPU_POWER_PAIRS)
    try:
        run_cmd_checked(ctx, "powercfg /setacvalueindex scheme_current sub_processor PERFBOOSTMODE 2", timeout=30)   # Aggressive
        run_cmd_checked(ctx, "powercfg /setacvalueindex scheme_current sub_processor CPMINCORES 100", timeout=30)    # no core parking
        run_cmd_checked(ctx, "powercfg /setacvalueindex scheme_current sub_processor CPMAXCORES 100", timeout=30)
        run_cmd_checked(ctx, "powercfg /setacvalueindex scheme_current sub_processor PROCTHROTTLEMIN 100", timeout=30)  # min 100%
        run_cmd_checked(ctx, "powercfg /setactive scheme_current", timeout=30)
    except Exception:
        if not had_snapshot:
            clear_tweak_snapshot("max_cpu_power")
        raise
    ctx.log("CPU boost set to Aggressive; core parking off; min processor state 100% (AC).")


def revert_max_cpu_power(ctx: TaskContext):
    # M1 fix: restore the machine's real prior values instead of hardcoded
    # ones (PERFBOOSTMODE 0=Disabled and PROCTHROTTLEMIN 5 were not this
    # machine's actual defaults in the audited case — stock was 80).
    _restore_powercfg_pairs(ctx, "max_cpu_power", _MAX_CPU_POWER_PAIRS, _MAX_CPU_POWER_FALLBACK)
    ctx.log("CPU power settings restored to their prior values.")


def apply_reserved_storage_off(ctx: TaskContext):
    """Free ~7 GB: Windows sets aside a chunk of your drive ('reserved
    storage') so future updates always have room. If your drive is
    tight, DISM /Set-ReservedStorageState 0 releases it immediately —
    Windows just downloads update payloads to normal free space instead.
    Fully reversible: /Set-ReservedStorageState 1 re-reserves it (it
    needs free space available to do so, which undo guidance logs).

    Admin required (DISM). The state is machine-wide disk config, not a
    per-user value. B-0xx: the prior state IS snapshotted now, so Undo no
    longer force-enables storage on a machine that already had it off."""
    ctx.set_status("Releasing reserved storage (~7 GB)...")
    # B-0xx: record whether it was on before we touched it. Undo used to run
    # /Set-ReservedStorageState 1 unconditionally, forcing ~7 GB back ON on a
    # machine (or policy) that had deliberately disabled it.
    if not get_tweak_snapshot("reserved_storage_off"):
        prior_enabled = _reserved_storage_enabled(ctx)
        if prior_enabled is not None:
            save_tweak_snapshot("reserved_storage_off", {"was_enabled": prior_enabled})
    rc = run_cmd(ctx, "dism /Online /Set-ReservedStorageState 0", timeout=900)
    if rc == 740:
        raise RuntimeError("DISM needs Administrator rights — restart the app as Administrator.")
    if rc != 0:
        # verify the actual state before declaring failure (DISM can be
        # noisy about reboot-pending flags)
        chk = run_cmd(ctx, "dism /Online /Get-ReservedStorageState", timeout=300)
        if chk == 0:
            ctx.log("  (DISM reported an earlier flag, but the state query verified.)")
        else:
            raise RuntimeError(f"Could not release reserved storage (exit code {rc}).")
    ctx.log("Reserved storage released — ~7 GB back on your drive.")


def revert_reserved_storage_off(ctx: TaskContext):
    """Re-reserve the update storage. Needs ~7 GB free; logs honestly when
    the drive is too full to restore it. B-0xx: if reserved storage was
    ALREADY disabled before this tweak ran, Undo leaves it disabled instead
    of forcing ~7 GB back on."""
    snap = get_tweak_snapshot("reserved_storage_off") or {}
    if snap.get("was_enabled") is False:
        clear_tweak_snapshot("reserved_storage_off")
        ctx.log("Reserved storage was already disabled before this tweak — "
                "leaving it disabled (nothing to restore).")
        return
    ctx.set_status("Re-reserving update storage...")
    rc = run_cmd(ctx, "dism /Online /Set-ReservedStorageState 1", timeout=900)
    if rc == 0:
        clear_tweak_snapshot("reserved_storage_off")
        ctx.log("Reserved storage re-enabled — updates have their safety pad again.")
        return
    if rc == 740:
        raise RuntimeError("DISM needs Administrator rights — restart the app as Administrator.")
    raise RuntimeError(
        "Could not re-enable reserved storage — your drive may not have "
        "~7 GB free for Windows to set aside. Free up space and run Undo again."
    )


def verify_ultimate_performance() -> "bool | None":
    """Active scheme is an Ultimate Performance duplicate."""
    try:
        guid = _active_scheme_guid()
        if not guid:
            return None
        return _guids_equal(guid, ULTIMATE_PERF_GUID) or _guids_equal(
            guid, _find_ultimate_scheme_guid() or "")
    except Exception:
        return None


def verify_power_throttling_off() -> "bool | None":
    return _verify_value("HKLM", "SYSTEM\\CurrentControlSet\\Control\\Power\\PowerThrottling", "PowerThrottlingOff", 1)


def verify_keyboard_tuning() -> "bool | None":
    return _verify_all_values([
        ("HKCU", "Control Panel\\Keyboard", "KeyboardDelay", "0"),
        ("HKCU", "Control Panel\\Keyboard", "KeyboardSpeed", "31"),
    ])


def verify_startup_delay() -> "bool | None":
    return _verify_value("HKCU", "Software\\Microsoft\\Windows\\CurrentVersion\\Explorer\\Serialize",
                         "StartupDelayInMSec", 0)


def verify_max_cpu_power() -> "bool | None":
    """PERFBOOSTMODE=2 (Aggressive) on the AC side of the current scheme."""
    try:
        class _NoCtx:
            @staticmethod
            def log(_m): pass
            @staticmethod
            def set_status(_m): pass
            @staticmethod
            def cancelled(): return False
        from app.utils import powercfg_query_index as _pqi
        v = _pqi(_NoCtx, "sub_processor", "PERFBOOSTMODE")  # type: ignore[arg-type]
        return None if v is None else v == 2
    except Exception:
        return None


def verify_ssd_superfetch() -> "bool | None":
    """SysMain disabled — service start type read via the registry
    (locale-free, the same source sc_query_start_type uses)."""
    v = __verify_read("HKLM", "SYSTEM\\CurrentControlSet\\Services\\SysMain", "Start")
    if v is None:
        return None
    return v == 4          # 4 = disabled
