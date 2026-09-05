"""
Advanced tab — kernel security, hypervisors, deep storage.
All unchecked by default. Warning header displayed in GUI.

Every task here is fully reversible via its revert function or an exact
documented default value — nothing deletes system files.
"""

import os
import subprocess

from app.utils import (TaskContext, reg_set_value, reg_set_value_checked, reg_delete_value,
                       reg_delete_key, run_cmd, IS_WINDOWS)

if IS_WINDOWS:
    import winreg


def disable_memory_integrity(ctx: TaskContext):
    ctx.log("[Advanced] Disable Memory Integrity (HVCI / Core Isolation) [REBOOT REQUIRED]")
    # F-005: both writes now raise on failure — a half-applied HVCI change
    # must not be counted as 'succeeded' by the runner.
    reg_set_value_checked(ctx, "HKLM", "SYSTEM\\CurrentControlSet\\Control\\DeviceGuard", "EnableVirtualizationBasedSecurity", 0)
    reg_set_value_checked(ctx, "HKLM", "SYSTEM\\CurrentControlSet\\Control\\DeviceGuard\\Scenarios\\HypervisorEnforcedCodeIntegrity", "Enabled", 0)
    ctx.log("Memory Integrity disabled (Enabled=0). Reboot required.")


def revert_memory_integrity(ctx: TaskContext):
    ctx.log("Re-enabling Memory Integrity (HVCI)...")
    # M3 fix: restore BOTH values the apply function set (previously left
    # EnableVirtualizationBasedSecurity=0 behind, so HVCI stayed half-off)
    reg_set_value_checked(ctx, "HKLM", "SYSTEM\\CurrentControlSet\\Control\\DeviceGuard", "EnableVirtualizationBasedSecurity", 1)
    reg_set_value_checked(ctx, "HKLM", "SYSTEM\\CurrentControlSet\\Control\\DeviceGuard\\Scenarios\\HypervisorEnforcedCodeIntegrity", "Enabled", 1)
    ctx.log("Memory Integrity fully enabled. Reboot required.")


def disable_vmp(ctx: TaskContext):
    ctx.log("[Advanced] Disable Virtual Machine Platform (VMP / Hyper-V) [REBOOT REQUIRED]")
    ctx.log("$ bcdedit /set hypervisorlaunchtype off")
    # Exact spec command
    run_cmd(ctx, "bcdedit /set hypervisorlaunchtype off")
    ctx.log("Virtual Machine Platform disabled (hypervisorlaunchtype off). Reboot required.")


def revert_vmp(ctx: TaskContext):
    ctx.log("Re-enabling Virtual Machine Platform...")
    run_cmd(ctx, "bcdedit /set hypervisorlaunchtype auto")
    ctx.log("VMP re-enabled (hypervisorlaunchtype auto). Reboot required.")


def disable_memory_compression(ctx: TaskContext):
    ctx.log("[Advanced] Disable Windows Memory Compression (For 32GB+ RAM PCs)")
    ctx.log("$ Disable-MMAgent -MemoryCompression")
    # Exact spec string
    run_cmd(ctx, 'powershell -NoProfile -Command "Disable-MMAgent -MemoryCompression"')
    ctx.log("Windows Memory Compression disabled. For 32GB+ RAM, improves latency.")


def revert_memory_compression(ctx: TaskContext):
    ctx.log("Re-enabling Windows Memory Compression...")
    run_cmd(ctx, 'powershell -NoProfile -Command "Enable-MMAgent -MemoryCompression"')
    ctx.log("Memory Compression re-enabled.")


def disable_copilot(ctx: TaskContext):
    ctx.log("[Advanced] Disable Windows Copilot & AI Telemetry")
    ctx.log("HKCU\\Software\\Policies\\Microsoft\\Windows\\WindowsCopilot -> TurnOffWindowsCopilot = 1 (DWORD)")
    spec_path = "Software\\Policies\\Microsoft\\Windows\\WindowsCopilot"
    # F-005: raise on failure — the old log-only branch still counted as
    # success in the runner.
    reg_set_value_checked(ctx, "HKCU", spec_path, "TurnOffWindowsCopilot", 1)
    ctx.log("Windows Copilot disabled (TurnOffWindowsCopilot=1).")


def revert_copilot(ctx: TaskContext):
    ctx.log("Re-enabling Windows Copilot...")
    spec_path = "Software\\Policies\\Microsoft\\Windows\\WindowsCopilot"
    reg_delete_value(ctx, "HKCU", spec_path, "TurnOffWindowsCopilot")
    ctx.log("Copilot policy removed (default enabled).")


def disable_hibernation(ctx: TaskContext):
    ctx.log("[Advanced] Disable Windows Hibernation (Reclaims disk space equal to RAM size)")
    ctx.log("$ powercfg -h off")
    run_cmd(ctx, "powercfg -h off")
    ctx.log("Hibernation disabled (powercfg -h off). Disk space reclaimed equal to RAM.")


def revert_hibernation(ctx: TaskContext):
    ctx.log("Re-enabling Windows Hibernation...")
    run_cmd(ctx, "powercfg -h on")
    ctx.log("Hibernation re-enabled (powercfg -h on).")


# Blocks WPBT (Windows Platform Binary Table) — stops motherboard OEM
# bloatware (ASUS Armoury Crate, Lenovo Vantage, etc.) from auto-installing
# itself at boot via the ACPI WPBT table. (Comment fixed per external
# review: this has nothing to do with location tracking.)
def disable_wpbt(ctx: TaskContext):
    ctx.log("[Advanced] WPBT - Disable")
    reg_set_value_checked(ctx, "HKLM", "SYSTEM\\CurrentControlSet\\Control\\Session Manager", "DisableWpbtExecution", 1)
    ctx.log("WPBT disabled.")

def revert_wpbt(ctx: TaskContext):
    reg_delete_value(ctx, "HKLM", "SYSTEM\\CurrentControlSet\\Control\\Session Manager", "DisableWpbtExecution")
    ctx.log("WPBT reverted.")


def disable_ai_features(ctx: TaskContext):
    """Disable Windows AI features: Recall snapshots, Click to Do, Copilot
    app auto-install (Win11 24H2+). Pure policy values — fully reversible."""
    ctx.log("[Advanced] Disable Recall / Copilot / AI features")
    base = "SOFTWARE\\Policies\\Microsoft\\Windows\\WindowsAI"
    reg_set_value_checked(ctx, "HKLM", base, "AllowRecallEnablement", 0)
    reg_set_value_checked(ctx, "HKLM", base, "DisableAIDataAnalysis", 1)
    reg_set_value_checked(ctx, "HKLM", base, "DisableClickToDo", 1)
    reg_set_value_checked(ctx, "HKLM", base, "RemoveMicrosoftCopilotApp", 1)
    ctx.log("AI policy values set (Recall + Click to Do off, Copilot app blocked).")
    ctx.log("Note: on Windows versions without these features the values simply have no effect.")

def revert_ai_features(ctx: TaskContext):
    base = "SOFTWARE\\Policies\\Microsoft\\Windows\\WindowsAI"
    for name in ("AllowRecallEnablement", "DisableAIDataAnalysis", "DisableClickToDo", "RemoveMicrosoftCopilotApp"):
        reg_delete_value(ctx, "HKLM", base, name)
    ctx.log("AI policies removed — Recall/Copilot back to Windows defaults.")


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
