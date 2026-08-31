"""
Dangerous combo detection and warnings.
"""

from typing import List, Tuple
from app.tasks import Task


# Define dangerous combinations: (task_keys, warning_message)
# These are combos that could cause issues when run together
DANGEROUS_COMBOS = [
    # ResetBase + any task that might need rollback
    (
        ["dism_resetbase", "dism_restorehealth", "dism_cleanup"],
        "⚠️ DISM ResetBase permanently removes the ability to uninstall current Windows updates. "
        "Running RestoreHealth or Cleanup after ResetBase is redundant. "
        "Consider running ResetBase alone, or only if you're certain you won't need to roll back updates."
    ),
    
    # Network reset + other network tweaks
    (
        ["network_reset", "disable_nagle", "network_throttling"],
        "⚠️ Network Reset will reset all network adapters to defaults, "
        "which will undo Nagle's Algorithm disable and Network Throttling tweaks. "
        "Run Network Reset first, then re-apply tweaks if needed."
    ),
    
    # Fast Startup + HAGS (both need reboot)
    (
        ["disable_fast_startup", "hags"],
        "⚠️ Both 'Fix Boot Issues' (disable Fast Startup) and 'Faster Graphics (HAGS)' require a reboot. "
        "You'll need to reboot twice to apply both. Consider enabling one, rebooting, then the other."
    ),
    
    # Multiple reboot-required tweaks
    (
        ["hags", "priority_separation", "disable_fast_startup", "ssd_trim", "ssd_superfetch", "ssd_last_access", "ssd_prefetch"],
        "⚠️ Multiple selected tweaks require a reboot (HAGS, Priority Separation, Fast Startup, SSD tweaks). "
        "You only need ONE reboot after all changes. The app will remind you at the end."
    ),
    
    # Prefetch is rarely needed — warn whenever it is selected
    (
        ["prefetch"],
        "⚠️ Clearing Prefetch is rarely needed and may slow down next app launches. "
        "Windows manages this automatically. Consider leaving it unchecked unless troubleshooting."
    ),
    
    # Driver Store cleanup warning
    (
        ["driver_store", "driver_junk"],
        "⚠️ Driver Store cleanup removes old driver packages via pnputil. "
        "Only remove drivers you're certain aren't needed. 'Remove Old Drivers' (C:\\NVIDIA/AMD folders) is safer."
    ),
    
    # Browser cache + active browser warning (can't detect easily, generic)
    (
        ["browser_cache"],
        "ℹ️ Browser cache cleaning requires browsers to be closed. "
        "Make sure Chrome/Edge/Firefox are not running for best results."
    ),
]


def check_dangerous_combos(selected_tasks: List[Task]) -> List[str]:
    """Check selected tasks for dangerous combinations.
    
    Accepts List[Task] or List[str] (keys) for scheduler use.
    Returns list of warning messages.
    """
    # Support both Task objects and raw key strings
    selected_keys = set()
    for t in selected_tasks:
        if isinstance(t, str):
            selected_keys.add(t)
        else:
            try:
                selected_keys.add(t.key)
            except AttributeError:
                # Fallback: treat as string
                selected_keys.add(str(t))
    warnings = []
    
    for combo_keys, message in DANGEROUS_COMBOS:
        # Check if any of the combo keys are in the selection
        # For single-key combos, warn if that key is present
        # For multi-key combos, warn if 2+ keys are present
        present = [k for k in combo_keys if k in selected_keys]
        if len(present) >= (1 if len(combo_keys) == 1 else 2):
            warnings.append(message)
    
    return warnings


def format_warnings(warnings: List[str]) -> str:
    """Format warnings for display."""
    if not warnings:
        return ""
    return "\n\n".join(warnings)