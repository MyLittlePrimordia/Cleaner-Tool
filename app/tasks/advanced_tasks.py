"""
Advanced tab — kernel security, hypervisors, deep storage.
All unchecked by default. Warning header displayed in GUI.

Every task here is reversible via its revert function or an exact documented
default value — nothing deletes system files.

UNDO-003: the registry-backed tasks below used to have a revert that
hardcoded Windows' default value, or deleted the key outright, with NO
snapshot anywhere. That silently destroyed whatever the user actually had:

  adv_memory_integrity  wrote 0,0 and reverted to 1,1 — so undoing a machine
                        that already had VBS off (or a vendor-tuned value)
                        turned Memory Integrity ON.
  adv_copilot           reverted with reg_delete_value, destroying any
                        pre-existing TurnOffWindowsCopilot policy.
  adv_disable_ai        deleted four policies, same problem.
  wpbt_disable          deleted DisableWpbtExecution.

These now use the same _snap_reg_values / _restore_reg_values pair as the
rest of the Tweak tab, which records each value's real prior state AND its
prior TYPE, and restores absence back to absence. The SEC-001 allowlist in
tweak_tasks.py has entries for all four, so the restore path is allowlisted
rather than falling through to the denylist-only default.

The three non-registry tasks (bcdedit, powercfg -h, MMAgent) still cannot
snapshot their prior state and keep their documented-default reverts; each
one logs which value it is assuming.
"""

import os

from app.utils import (TaskContext, reg_set_value_checked, reg_delete_value,
                       run_cmd_checked, IS_WINDOWS)
from app.tasks.ps import ps_argv

# The snapshot primitives live with the tweaks that own them. tweak_tasks
# does not import this module, so sharing them here cannot create a cycle.
from app.tasks.tweak_tasks import (  # noqa: E402
    _snap_reg_values, _restore_reg_values,
    get_tweak_snapshot, clear_tweak_snapshot, save_tweak_snapshot,
)

if IS_WINDOWS:
    pass


def _apply_registry(ctx: TaskContext, task_id: str, specs, writes):
    """Snapshot `specs`, then apply `writes`, dropping the snapshot on failure.

    The standard shape, factored out because four tasks here needed it and
    they each had their own subtly-wrong version.

    writes: iterable of (hive, path, name, value, value_type_or_None)
    """
    had_snapshot = bool(get_tweak_snapshot(task_id))
    _snap_reg_values(ctx, task_id, specs)
    landed = 0
    try:
        for hive, path, name, value, vtype in writes:
            reg_set_value_checked(ctx, hive, path, name, value, value_type=vtype)
            landed += 1
    except BaseException:
        # B-028: only drop the snapshot when NOTHING landed. For a multi-write
        # task a later write can fail after earlier ones succeeded; clearing
        # the snapshot then discarded the real prior state and revert fell back
        # to documented defaults (e.g. turning HVCI back on for a user who had
        # it legitimately off — the UNDO-003 regression).
        if not had_snapshot and landed == 0:
            try:
                clear_tweak_snapshot(task_id)
            except Exception:
                pass
        raise


def _revert_registry(ctx: TaskContext, task_id: str, defaults, absent=()):
    """Restore every snapshotted value, then default anything uncovered.

    defaults / absent describe the documented Windows state for keys an older
    build (or a cleared config) left uncovered, so those keys are never left
    sitting at this tweak's output.
    """
    snap = get_tweak_snapshot(task_id) or {}
    covered = set()
    for spec in (snap.get("specs") or []):
        if isinstance(spec, (list, tuple)) and len(spec) >= 3:
            covered.add((spec[0], spec[1], spec[2]))
    if covered:
        _restore_reg_values(ctx, task_id)
    for key in absent:
        if key not in covered:
            reg_delete_value(ctx, key[0], key[1], key[2])
    uncovered = [k for k in defaults if k not in covered]
    for hive, path, name in uncovered:
        value, vtype = defaults[(hive, path, name)]
        reg_set_value_checked(ctx, hive, path, name, value, value_type=vtype)
    if uncovered:
        ctx.log("  (%d value(s) had no snapshot — restored the documented "
                "Windows default instead)" % len(uncovered))


_DG = "SYSTEM\\CurrentControlSet\\Control\\DeviceGuard"
# Spelled out in full rather than concatenated onto _DG: the SEC-001 allowlist
# drift guard resolves these spec lists with a static evaluator, and a BinOp
# defeats it -- the guard then reads the path as a fragment and reports the
# allowlist entry as invented.
_DG_CI = ("SYSTEM\\CurrentControlSet\\Control\\DeviceGuard\\Scenarios"
          "\\HypervisorEnforcedCodeIntegrity")
_MEMORY_INTEGRITY_SPECS = [
    ("HKLM", _DG, "EnableVirtualizationBasedSecurity"),
    ("HKLM", _DG_CI, "Enabled"),
]


def disable_memory_integrity(ctx: TaskContext):
    ctx.log("[Advanced] Disable Memory Integrity (HVCI / Core Isolation) [REBOOT REQUIRED]")
    # F-005: both writes raise on failure — a half-applied HVCI change
    # must not be counted as 'succeeded' by the runner.
    _apply_registry(ctx, "adv_memory_integrity", _MEMORY_INTEGRITY_SPECS, [
        ("HKLM", _DG, "EnableVirtualizationBasedSecurity", 0, None),
        ("HKLM", _DG_CI, "Enabled", 0, None),
    ])
    ctx.log("Memory Integrity disabled (Enabled=0). Reboot required.")


def revert_memory_integrity(ctx: TaskContext):
    ctx.log("Re-enabling Memory Integrity (HVCI)...")
    # M3 fix: restore BOTH values the apply set (it previously left
    # EnableVirtualizationBasedSecurity=0 behind, so HVCI stayed half-off).
    # UNDO-003: and restore what the user actually had, rather than forcing
    # 1/1 — the old revert turned Memory Integrity ON on a machine that had
    # legitimately turned it off.
    _revert_registry(ctx, "adv_memory_integrity", {
        ("HKLM", _DG, "EnableVirtualizationBasedSecurity"): ("1", None),
        ("HKLM", _DG_CI, "Enabled"): ("1", None),
    })
    ctx.log("Memory Integrity restored. Reboot required.")


def disable_vmp(ctx: TaskContext):
    ctx.log("[Advanced] Disable Virtual Machine Platform (VMP / Hyper-V) [REBOOT REQUIRED]")
    # H5: bcdedit fails without admin/on LTSC — the old unchecked run_cmd
    # logged success either way (security-relevant lie). Raise honestly.
    run_cmd_checked(ctx, "bcdedit /set hypervisorlaunchtype off", timeout=60)
    ctx.log("Virtual Machine Platform disabled (hypervisorlaunchtype off). Reboot required.")


def revert_vmp(ctx: TaskContext):
    ctx.log("Re-enabling Virtual Machine Platform...")
    run_cmd_checked(ctx, "bcdedit /set hypervisorlaunchtype auto", timeout=60)
    ctx.log("VMP re-enabled (hypervisorlaunchtype auto). Reboot required.")


def disable_memory_compression(ctx: TaskContext):
    ctx.log("[Advanced] Disable Windows Memory Compression (For 32GB+ RAM PCs)")
    # H5: same honest-failure treatment; shell=False argv (no cmd.exe
    # quote mangling of the PS command).
    run_cmd_checked(ctx, ps_argv("Disable-MMAgent -MemoryCompression"),
                    shell=False, timeout=120)
    ctx.log("Windows Memory Compression disabled. For 32GB+ RAM, improves latency.")


def revert_memory_compression(ctx: TaskContext):
    ctx.log("Re-enabling Windows Memory Compression...")
    run_cmd_checked(ctx, ps_argv("Enable-MMAgent -MemoryCompression"),
                    shell=False, timeout=120)
    ctx.log("Memory Compression re-enabled.")


_COPILOT_PATH = "Software\\Policies\\Microsoft\\Windows\\WindowsCopilot"
_COPILOT_SPECS = [("HKCU", _COPILOT_PATH, "TurnOffWindowsCopilot")]


def disable_copilot(ctx: TaskContext):
    ctx.log("[Advanced] Disable Windows Copilot & AI Telemetry")
    ctx.log("HKCU\\Software\\Policies\\Microsoft\\Windows\\WindowsCopilot -> TurnOffWindowsCopilot = 1 (DWORD)")
    # F-005: raise on failure — the old log-only branch still counted as
    # success in the runner.
    _apply_registry(ctx, "adv_copilot", _COPILOT_SPECS, [
        ("HKCU", _COPILOT_PATH, "TurnOffWindowsCopilot", 1, None),
    ])
    ctx.log("Windows Copilot disabled (TurnOffWindowsCopilot=1).")


def revert_copilot(ctx: TaskContext):
    ctx.log("Re-enabling Windows Copilot...")
    # UNDO-003: snapshot first. The old revert deleted the value outright,
    # destroying any TurnOffWindowsCopilot policy the user (or an org policy)
    # had already set — the classic "undo made it less safe" bug.
    _revert_registry(ctx, "adv_copilot", {},
                     absent=[("HKCU", _COPILOT_PATH, "TurnOffWindowsCopilot")])
    ctx.log("Copilot policy restored.")


def _hibernation_enabled() -> bool:
    """True when hibernation is on, approximated by hiberfil.sys existing.

    Powercfg has no query-free way to read this; the hibernation file is the
    documented on/off artifact Windows creates and removes with `powercfg -h`.
    """
    drive = os.environ.get("SYSTEMDRIVE", "C:")
    return os.path.isfile(os.path.join(drive + os.sep, "hiberfil.sys"))


def disable_hibernation(ctx: TaskContext):
    ctx.log("[Advanced] Disable Windows Hibernation (Reclaims disk space equal to RAM size)")
    # H-014: record the prior state so Undo never enables hibernation on a
    # machine that never had it (which would silently authorize a RAM-sized
    # hiberfil.sys).
    save_tweak_snapshot("adv_hibernation", {"enabled": bool(_hibernation_enabled())})
    run_cmd_checked(ctx, "powercfg -h off", timeout=60)
    ctx.log("Hibernation disabled (powercfg -h off). Disk space reclaimed equal to RAM.")


def revert_hibernation(ctx: TaskContext):
    snap = get_tweak_snapshot("adv_hibernation") or {}
    prior = snap.get("enabled")
    if prior is False:
        ctx.log("Hibernation was already OFF before this tweak ran — leaving it "
                "off (not creating a RAM-sized hiberfil.sys).")
        clear_tweak_snapshot("adv_hibernation")
        return
    if prior is None:
        ctx.log("  (no snapshot — assuming hibernation was on; running powercfg -h on)")
    ctx.log("Re-enabling Windows Hibernation...")
    run_cmd_checked(ctx, "powercfg -h on", timeout=60)
    clear_tweak_snapshot("adv_hibernation")
    ctx.log("Hibernation re-enabled (powercfg -h on).")


# Blocks WPBT (Windows Platform Binary Table) — stops motherboard OEM
# bloatware (ASUS Armoury Crate, Lenovo Vantage, etc.) from auto-installing
# itself at boot via the ACPI WPBT table. (Comment fixed per external
# review: this has nothing to do with location tracking.)
_WPBT_PATH = "SYSTEM\\CurrentControlSet\\Control\\Session Manager"
_WPBT_SPECS = [("HKLM", _WPBT_PATH, "DisableWpbtExecution")]


def disable_wpbt(ctx: TaskContext):
    ctx.log("[Advanced] WPBT - Disable")
    _apply_registry(ctx, "wpbt_disable", _WPBT_SPECS, [
        ("HKLM", _WPBT_PATH, "DisableWpbtExecution", 1, None),
    ])
    ctx.log("WPBT disabled.")


def revert_wpbt(ctx: TaskContext):
    # UNDO-003: snapshot first — the old revert deleted the value outright.
    _revert_registry(ctx, "wpbt_disable", {},
                     absent=[("HKLM", _WPBT_PATH, "DisableWpbtExecution")])
    ctx.log("WPBT reverted.")


_AI_BASE = "SOFTWARE\\Policies\\Microsoft\\Windows\\WindowsAI"
_AI_NAMES = ("AllowRecallEnablement", "DisableAIDataAnalysis",
             "DisableClickToDo", "RemoveMicrosoftCopilotApp")
_AI_SPECS = [("HKLM", _AI_BASE, n) for n in _AI_NAMES]


def disable_ai_features(ctx: TaskContext):
    """Disable Windows AI features: Recall snapshots, Click to Do, Copilot
    app auto-install (Win11 24H2+). Pure policy values — fully reversible."""
    ctx.log("[Advanced] Disable Recall / Copilot / AI features")
    writes = [("HKLM", _AI_BASE, "AllowRecallEnablement", 0, None),
              ("HKLM", _AI_BASE, "DisableAIDataAnalysis", 1, None),
              ("HKLM", _AI_BASE, "DisableClickToDo", 1, None),
              ("HKLM", _AI_BASE, "RemoveMicrosoftCopilotApp", 1, None)]
    _apply_registry(ctx, "adv_disable_ai", _AI_SPECS, writes)
    ctx.log("AI policy values set (Recall + Click to Do off, Copilot app blocked).")
    ctx.log("Note: on Windows versions without these features the values simply have no effect.")


def revert_ai_features(ctx: TaskContext):
    # UNDO-003: snapshot first. The old revert deleted all four policies,
    # which silently removed any that were already set by the user or by an
    # organisation before this tweak ran.
    _revert_registry(ctx, "adv_disable_ai", {},
                     absent=[("HKLM", _AI_BASE, n) for n in _AI_NAMES])
    ctx.log("AI policies restored — Recall/Copilot back to their prior state.")


# --------------------------------------------------------------------------- #
# TASKS list — ALL UNCHECKED BY DEFAULT per spec
# --------------------------------------------------------------------------- #
from app.tasks import Task  # noqa: E402

TASKS = [
    Task("adv_memory_integrity", "Disable Memory Integrity (HVCI)", "Makes games a bit faster but less safe from viruses", disable_memory_integrity, default=False, admin_required=True, risk="REBOOT REQUIRED", revert=revert_memory_integrity),
    Task("adv_vmp", "Disable VMP / Hyper-V", "Turns off Hyper-V helper to save CPU if you don't use WSL", disable_vmp, default=False, admin_required=True, risk="REBOOT REQUIRED", revert=revert_vmp),
    Task("adv_memory_compression", "Disable Memory Compression", "Saves CPU if you have 32GB or more RAM", disable_memory_compression, default=False, admin_required=True, revert=revert_memory_compression),
    Task("adv_copilot", "Disable Copilot & Telemetry", "Stops Windows AI tracking and suggestions", disable_copilot, default=False, admin_required=False, revert=revert_copilot),
    Task("adv_disable_ai", "Disable Recall / Copilot / AI", "Turns off Recall snapshots and Click to Do on Win11 24H2+", disable_ai_features, default=False, admin_required=True, revert=revert_ai_features),
    Task("adv_hibernation", "Disable Hibernation", "Turns off hibernation to free disk space", disable_hibernation, default=False, admin_required=True, revert=revert_hibernation),
    Task("wpbt_disable", "Disable WPBT", "Blocks vendor apps from starting at boot", disable_wpbt, default=False, admin_required=True, revert=revert_wpbt),
]
