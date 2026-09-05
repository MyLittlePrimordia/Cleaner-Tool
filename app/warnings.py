"""
Dangerous combo detection and warnings.
"""

from typing import List


# Define dangerous combinations: (task_keys, warning_message)
# These are combos that could cause issues when run together
DANGEROUS_COMBOS = [
    # Network reset + other network tweaks — network_reset is the ANCHOR
    # (it wipes the adapters); the other two are its victims.
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

    # Multiple reboot-required tweaks — any 2 of these together means the
    # "one reboot covers all" note matters. No single anchor: any pair.
    # Phase 2 #13: adv_vmp / adv_memory_integrity removed from the app, so
    # their keys are dropped from this combo (they can never be selected
    # again — the dead keys were harmless but misleading).
    # tasks.txt: MPO fix also requires a reboot.
    (
        ["hags", "priority_separation", "disable_fast_startup", "ssd_trim", "ssd_superfetch", "ssd_last_access", "ssd_prefetch", "mpo_fix"],
        "⚠️ Multiple selected tweaks require a reboot (HAGS, Priority Separation, Fast Startup, SSD tweaks, MPO). "
        "You only need ONE reboot after all changes. The app will remind you at the end."
    ),

    # Firewall reset wipes per-app allow rules (games re-prompt) — FYI
    (
        ["firewall_reset"],
        "ℹ️ Resetting the firewall removes every app's network permission. "
        "Games and launchers will ask again the first time they go online — that's normal."
    ),

    # Prefetch is rarely needed — warn whenever it is selected
    (
        ["prefetch"],
        "⚠️ Clearing Prefetch is rarely needed and may slow down next app launches. "
        "Windows manages this automatically. Consider leaving it unchecked unless troubleshooting."
    ),

    # Browser cache + active browser warning (can't detect easily, generic)
    (
        ["browser_cache"],
        "ℹ️ Browser cache cleaning requires browsers to be closed. "
        "Make sure Chrome/Edge/Firefox are not running for best results."
    ),

    # Ad blocker — large domain list, occasional false positives
    (
        ["ad_blocker"],
        "ℹ️ The System-Wide Ad Blocker blocks ~78,000 known ad/tracker domains via the hosts file. "
        "It's fully reversible, but very rarely a domain used by a game or app you like may be caught by mistake. "
        "If something stops working after enabling it, just use the same checkbox to revert."
    ),

    # Restore point + VSS repair — vss_repair is the ANCHOR (restarting VSS
    # mid-checkpoint is what can skip one); either restore-point task is the
    # second member.
    (
        ["vss_repair", "restore_point", "restore_point_tweak"],
        "⚠️ Restore point creation is limited by Windows to once per 24 hours. "
        "If a checkpoint was made today, extra restore-point tasks will be skipped by Windows (shown as info, not failure)."
    ),

    # Several telemetry reducers — any 2 of the 3 (no anchor: pure overlap FYI)
    (
        ["stop_telemetry", "privacy_baseline", "nvidia_telemetry"],
        "⚠️ Several telemetry-reduction tweaks are selected (Stop Telemetry, Privacy Baseline, NVIDIA Opt-Out). "
        "They overlap somewhat but are safe together — just know some switches do the same thing."
    ),

    # AllowTelemetry is owned by BOTH tweaks — limit_telemetry is the anchor
    # of the *revert* hazard (undoing either strips the shared value while
    # the other tweak's badge still reads applied). Warn when both are on.
    (
        ["limit_telemetry", "privacy_baseline"],
        "⚠️ 'Limit Tracking' and 'Privacy Baseline' both write the same AllowTelemetry value. "
        "They're safe to run together, but if you later undo just one of them, "
        "the tracking limit is removed for both — re-apply the other if you still want it."
    ),
]


def check_dangerous_combos(selected_tasks) -> List[str]:
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
        present = [k for k in combo_keys if k in selected_keys]
        # Audit fix (probe-confirmed false positive): the old rule fired for
        # ANY 2 members of a multi-key combo — e.g. Nagle + Throttling
        # warned about "Network Reset will undo them" when Network Reset
        # wasn't even selected. The first key in each multi-key combo is the
        # ANCHOR (the act that causes the harm); the rest are the victims.
        # A combo fires only when the anchor is present AND at least one
        # other member is too. Single-key combos fire on that key alone.
        if len(combo_keys) == 1:
            if present:
                warnings.append(message)
        elif present and combo_keys[0] in present and len(present) >= 2:
            warnings.append(message)

    return warnings
