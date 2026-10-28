"""Tweak tab — General system tweaks: accessibility (sticky keys), crash popups, the
no-update-reboot policy, NTFS 8.3 names and Explorer auto-discovery.

Split out of app/tasks/tweak_tasks.py (audit 2026-10, R-002 stage 2). Every
function here is one Task's apply/revert/verify implementation; the shared
constants and primitives live in tweak_common, and `TASKS` is assembled in
tweak_tasks.py so the registry still reads as one literal list.
"""
from __future__ import annotations

from app.tasks.tweak_common import (  # noqa: F401 - re-exported
    TaskContext,
    _ACCESSIBILITY_FLAGS,
    _regn_apply,
    _regn_revert,
    _verify_all_values,
    _verify_value,
    clear_tweak_snapshot,
    get_tweak_snapshot,
    reg_delete_value,
    reg_set_value_checked,
)
from app.tasks.tweak_snapshots import _snapshot_validation_error  # noqa: F401 - re-exported
from app.utils import (  # noqa: F401 - re-exported
    reg_delete_key,
    run_cmd_checked,
)
from app.config_persist import save_tweak_snapshot  # noqa: F401 - re-exported



# Disables explorer auto discovery #
def apply_explorer_auto_discovery_disable(ctx: TaskContext):
    ctx.log("[Tweak] File Explorer Automatic Folder Discovery - Disable")
    for sub in (r"Software\Classes\Local Settings\Software\Microsoft\Windows\Shell\Bags",
                r"Software\Classes\Local Settings\Software\Microsoft\Windows\Shell\BagMRU"):
        reg_delete_key(ctx, "HKCU", sub)
        ctx.log(f"  Removed HKCU\\{sub}")
    all_folders = r"Software\Classes\Local Settings\Software\Microsoft\Windows\Shell\Bags\AllFolders\Shell"
    reg_set_value_checked(ctx, "HKCU", all_folders, "FolderType", "NotSpecified", value_type="REG_SZ")
    ctx.log("  Set FolderType=NotSpecified.")
    ctx.log("Please sign out/in or restart to apply.")


def revert_explorer_auto_discovery_disable(ctx: TaskContext):
    # C7: the old revert deleted Bags/BagMRU AGAIN (wiping views created
    # after apply) while leaving FolderType=NotSpecified in place — Undo
    # made things worse. Per-folder views deleted by apply cannot be
    # restored (no backup was taken), so revert only removes the
    # FolderType value this tweak set and says so honestly.
    reg_delete_value(ctx, "HKCU",
                     r"Software\Classes\Local Settings\Software\Microsoft\Windows\Shell\Bags\AllFolders\Shell",
                     "FolderType")
    ctx.log("Explorer AutoDiscovery undone (FolderType reset). Note: per-folder "
            "views cleared by apply cannot be restored — they rebuild as you browse.")


def apply_disable_sticky_keys(ctx: TaskContext):
    """Stop the 'Do you want to turn on Sticky Keys?' popup and its beep
    from interrupting games when Shift is tapped repeatedly (sprint,
    crouch). Sets the accessibility hotkey Flags to 'hotkey off' states —
    the same values Windows' own Settings panel writes when you untick
    the shortcut checkboxes. Fully reversible."""
    ctx.set_status("Disabling Sticky Keys popups...")
    # MED-007: the old revert wrote hardcoded "Windows defaults" ("510",
    # "126", "62"), which are NOT what a user who had customised their
    # accessibility hotkeys had. Undo silently overwrote their settings.
    # The real prior values are snapshotted now.
    _regn_apply(ctx, "disable_sticky_keys",
                [w + ("REG_SZ",) for w in _ACCESSIBILITY_FLAGS],
                error="Could not write accessibility Flags values.")
    ctx.log("Sticky Keys / Filter Keys / Toggle Keys popups disabled.")


def revert_disable_sticky_keys(ctx: TaskContext):
    """Restore the accessibility hotkey flags to their previous values."""
    ctx.set_status("Restoring accessibility popups...")
    # MED-007: was three hardcoded writes of the "Windows default", not a
    # restore. Now puts back what the user actually had.
    _regn_revert(ctx, "disable_sticky_keys", "Accessibility popups")


def apply_suppress_crash_popups(ctx: TaskContext):
    ctx.set_status("Crash popups...")
    # MED-007: Undo used to blind-delete this value, destroying a
    # pre-existing different setting. The real prior state is
    # snapshotted now, so Undo restores what was actually there,
    # "absent" included.
    _regn_apply(ctx, 'suppress_crash_popups', [("HKCU", "Software\\Microsoft\\Windows\\Windows Error Reporting", "DontShowUI", 1)])
    ctx.log("Crash popups applied.")


def revert_suppress_crash_popups(ctx: TaskContext):
    # MED-007: was a blind `reg_delete_value(...)`, which destroyed
    # a pre-existing different value. Now restores the snapshot.
    _regn_revert(ctx, 'suppress_crash_popups', "Crash popups")


def apply_no_update_reboot(ctx: TaskContext):
    ctx.set_status("Windows Update auto-reboot...")
    # MED-007: Undo used to blind-delete this value, destroying a
    # pre-existing different setting. The real prior state is
    # snapshotted now, so Undo restores what was actually there,
    # "absent" included.
    _regn_apply(ctx, 'no_update_reboot', [("HKLM", "SOFTWARE\\Policies\\Microsoft\\Windows\\WindowsUpdate\\AU", "NoAutoRebootWithLoggedOnUsers", 1)])
    ctx.log("Windows Update auto-reboot applied.")


def revert_no_update_reboot(ctx: TaskContext):
    # MED-007: was a blind `reg_delete_value(...)`, which destroyed
    # a pre-existing different value. Now restores the snapshot.
    _regn_revert(ctx, 'no_update_reboot', "Windows Update auto-reboot")


def apply_ntfs_8dot3_disable(ctx: TaskContext):
    """Disable NTFS 8.3 short-name creation — speeds up folders with many
    files. Snapshot-first: the stock value is 2 (per-volume, system volume
    enabled) on modern Windows, so the honest revert is 'restore what
    fsutil reported', never a hardcoded guess (the source list's
    'revert to 0' was backwards: 1 disables, 0 enables)."""
    import subprocess as _sp
    ctx.set_status("Disabling 8.3 short-name creation...")
    try:
        out = _sp.run(["fsutil", "behavior", "query", "disable8dot3"], shell=False,
                      capture_output=True, text=True, timeout=15,
                      creationflags=getattr(_sp, "CREATE_NO_WINDOW", 0))
    except Exception as exc:
        raise RuntimeError(f"Could not query the current 8.3 setting: {exc}")
    # B6 audit fix: the numeric state (0/1/2) is locale-free, but this
    # used to run on unchecked output — a localized error/odd text meant
    # NO snapshot was saved and the revert later restored a hardcoded 2
    # guess. Parse the number explicitly and fail loudly (before any
    # change) when the output has no number to snapshot.
    prior_value = None
    if out.returncode == 0:
        for token in (out.stdout or "").split():
            if token.isdigit():
                prior_value = int(token)
                break
    if prior_value is None:
        ctx.log(f"  ! could not read the current 8.3 name setting from fsutil "
                f"(rc={out.returncode}: {(out.stdout or out.stderr or '').strip()[:120]}) — "
                "aborting so a revert never has to guess.")
        raise RuntimeError("Could not read the current 8.3 name setting — nothing was changed.")
    # C5: a failed set must not leave this snapshot behind (false badge).
    had_snapshot = bool(get_tweak_snapshot("ntfs_8dot3"))
    save_tweak_snapshot("ntfs_8dot3", {"value": prior_value})
    try:
        run_cmd_checked(ctx, "fsutil behavior set disable8dot3 1")
    except Exception:
        if not had_snapshot:
            clear_tweak_snapshot("ntfs_8dot3")
        raise
    ctx.log("8.3 short-name creation disabled (speeds up folders with many files).")


def revert_ntfs_8dot3_disable(ctx: TaskContext):
    snap = get_tweak_snapshot("ntfs_8dot3")
    prior = snap.get("value", 2) if snap else 2
    # F-1: the snapshot value flows into a shell command string — only a
    # numeric fsutil state (0/1/2/3) this app could have parsed may run.
    if not (isinstance(prior, int) and not isinstance(prior, bool) and 0 <= prior <= 3):
        raise _snapshot_validation_error("ntfs_8dot3", "value", prior)
    run_cmd_checked(ctx, f"fsutil behavior set disable8dot3 {prior}")
    clear_tweak_snapshot("ntfs_8dot3")
    ctx.log("8.3 name creation restored to previous setting.")


def verify_disable_sticky_keys() -> "bool | None":
    return _verify_all_values([
        ("HKCU", "Control Panel\\Accessibility\\StickyKeys", "Flags", "506"),
        ("HKCU", "Control Panel\\Accessibility\\Keyboard Response", "Flags", "122"),
        ("HKCU", "Control Panel\\Accessibility\\ToggleKeys", "Flags", "58"),
    ])


def verify_suppress_crash_popups() -> "bool | None":
    return _verify_value("HKCU", "Software\\Microsoft\\Windows\\Windows Error Reporting", "DontShowUI", 1)


def verify_no_update_reboot() -> "bool | None":
    return _verify_value("HKLM", "SOFTWARE\\Policies\\Microsoft\\Windows\\WindowsUpdate\\AU",
                         "NoAutoRebootWithLoggedOnUsers", 1)
