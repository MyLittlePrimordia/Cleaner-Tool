"""Tweak tab — Shell and appearance tweaks: the classic context menu, taskbar cleanup and layout,
local search, recent files, menu delay, file extensions, Aero Shake, lock screen,
Edge preload, dark mode, widgets, autoplay, snap flyout, Storage Sense, Alt-Tab,
NumLock, clock seconds and the classic shortcut icons.

Split out of app/tasks/tweak_tasks.py (audit 2026-10, R-002 stage 2). Every
function here is one Task's apply/revert/verify implementation; the shared
constants and primitives live in tweak_common, and `TASKS` is assembled in
tweak_tasks.py so the registry still reads as one literal list.
"""
from __future__ import annotations

from app.tasks.tweak_common import (  # noqa: F401 - re-exported
    CONTEXT_MENU_CLSID,
    TaskContext,
    _ADV,
    _DESKTOP,
    _HIDE_RECENT_SPECS,
    _LINK_PATH,
    _LOCAL_SEARCH_SPECS,
    _PERSONALIZE,
    _SENSE,
    _SHELL_ICONS,
    _TaskbarDevSettings,
    __verify_read,
    _is_win11_or_newer,
    _prune_key_if_empty,
    _regn_apply,
    _regn_revert,
    _restart_explorer,
    _restore_reg_values,
    _snap_reg_values,
    _verify_all_values,
    _verify_value,
    clear_tweak_snapshot,
    get_tweak_snapshot,
    reg_delete_value,
    reg_set_value_checked,
)



def apply_classic_context_menu(ctx: TaskContext):
    # MED-007: the old revert called reg_delete_key on the WHOLE
    # Software\Classes\CLSID\{86ca1aa0-...} key. That destroys every other
    # value and subkey under it, including a pre-existing InprocServer32 the
    # user (or another tool) had put there. Undo is now a VALUE-level restore,
    # and the key itself is only removed if apply is what created it.
    _regn_apply(ctx, "classic_context_menu",
                [("HKCU",
                  "Software\\Classes\\CLSID\\%s\\InprocServer32" % CONTEXT_MENU_CLSID,
                  "", "", "REG_SZ")],
                error="Could not write the classic-menu registry value.")
    # audit fix: restart_explorer's False return was ignored — the tweak
    # reported success while the taskbar might never have come back.
    if not _restart_explorer(ctx):
        raise RuntimeError(
            "Registry value set, but Explorer did not restart cleanly — "
            "your taskbar may be missing. Press Ctrl+Shift+Esc > File > Run "
            "new task > explorer.exe."
        )


def revert_classic_context_menu(ctx: TaskContext):
    # Restore just the value apply wrote, then drop the key ONLY if it is now
    # empty - i.e. only if this tweak is what created it. Never delete a key
    # that holds anything else.
    _regn_revert(ctx, "classic_context_menu", "Classic context menu")
    _prune_key_if_empty(ctx, "HKCU",
                        "Software\\Classes\\CLSID\\%s" % CONTEXT_MENU_CLSID)
    if not _restart_explorer(ctx):
        ctx.log("  ! Explorer did not restart cleanly — your taskbar may be missing. "
                "Press Ctrl+Shift+Esc > File > Run new task > explorer.exe.")


def apply_taskbar_cleanup(ctx: TaskContext):
    """Win11: remove Widgets, Chat/Teams icon, Meet Now, search highlights,
    and OneDrive ads in File Explorer — all background CPU/RAM consumers.

    LTSC/user-reported fix: the old code used reg_set_value_checked for all
    5 values, so ONE denied/missing value (observed: TaskbarDa -> WinError 5
    on Win10 IoT LTSC 2021, where the Win11-only Widgets key doesn't exist)
    failed the WHOLE tweak. Now each value is best-effort: Win11-only keys
    are skipped with a log line on Win10, other failures are logged, and the
    tweak only fails when NOTHING could be applied.

    BUG-017: the prior states are now SNAPSHOTTED. Revert used to hardcode a
    restore value of 1 for every key, but the Windows default for
    ShowSyncProviderNotifications and IsDynamicSearchBoxEnabled is 0/absent.
    So Undo turned Explorer sync-provider ads ON for a user who never had them
    on - the opposite of what they clicked. Every key this apply writes is
    snapshotted so Undo restores what was actually there.
    """
    from app.utils import reg_set_value as _set
    adv = "Software\\Microsoft\\Windows\\CurrentVersion\\Explorer\\Advanced"
    is_win11 = _is_win11_or_newer()
    if not is_win11:
        ctx.log("  (Win10 detected — Widgets/Chat/search-highlights are Win11-only; applying what exists here)")
    # (hive, path, name, value, win11_only)
    writes = [
        ("HKCU", adv, "TaskbarDa", 0, True),        # Widgets
        ("HKCU", adv, "TaskbarMn", 0, True),        # Chat / Teams icon
        ("HKCU", adv, "ShowSyncProviderNotifications", 0, False),  # Explorer ads
        ("HKCU", "Software\\Microsoft\\Windows\\CurrentVersion\\SearchSettings", "IsDynamicSearchBoxEnabled", 0, True),
        ("HKLM", "Software\\Microsoft\\Windows\\CurrentVersion\\Policies\\Explorer", "HideSCAMeetNow", 1, False),
    ]
    # Snapshot ONLY the keys we are actually going to write. On Win10 the
    # Win11-only keys are never touched, so recording them would widen the
    # revert's blast radius for no reason.
    active = [w for w in writes if not (w[4] and not is_win11)]
    had_snapshot = bool(get_tweak_snapshot("taskbar_cleanup"))
    _snap_reg_values(ctx, "taskbar_cleanup",
                     [(h, p, n) for h, p, n, _v, _w in active])
    ok, skipped = 0, []
    for hive, path, name, value, win11_only in writes:
        if win11_only and not is_win11:
            skipped.append(f"{name} (Win11-only — not present on Win10)")
            ctx.log(f"  (skipped) {hive}\\{path}\\{name}: Win11-only key on Win10 — nothing to change.")
            continue
        if _set(ctx, hive, path, name, value):
            ok += 1
        else:
            skipped.append(f"{name} (blocked — policy or permissions)")
    if ok == 0:
        # Nothing was applied, so there is nothing to undo - drop the snapshot
        # we just took rather than leaving a badge the user cannot clear.
        if not had_snapshot:
            clear_tweak_snapshot("taskbar_cleanup")
        raise RuntimeError(
            "Could not apply any taskbar setting "
            f"({'; '.join(skipped) or 'all writes blocked'}). "
            "Nothing was marked as applied."
        )
    if skipped:
        ctx.log(f"  (applied {ok} of {len(writes)}; skipped: {'; '.join(skipped)})")
    ctx.log("Widgets, Chat icon, search highlights and Explorer ads disabled. Restart Explorer to see changes.")


def revert_taskbar_cleanup(ctx: TaskContext):
    """Restore every value this tweak wrote to the state it was ACTUALLY in
    before, from the snapshot apply took.

    BUG-017, two halves:
      1. It used to write a hardcoded 1 for all four HKCU values and blind-delete
         HideSCAMeetNow. The Windows default for ShowSyncProviderNotifications
         and IsDynamicSearchBoxEnabled is 0 (absent), so Undo switched Explorer
         sync-provider ads ON for a user who never had them on.
      2. It never raised. `if ok == 0: ctx.log(...)` was immediately followed by
         an unconditional "Taskbar items restored.", so a total write failure
         still told the user their taskbar had been restored. Restoring through
         _restore_reg_values makes each failed write raise; the count it returns
         lets the "nothing happened" case be reported as the failure it is.
    """
    had_snapshot = bool(get_tweak_snapshot("taskbar_cleanup"))
    restored = _restore_reg_values(ctx, "taskbar_cleanup")
    if restored <= 0:
        raise RuntimeError(
            "Taskbar Undo could not restore anything"
            + ("" if had_snapshot else
               " — no snapshot was recorded, so the prior values are unknown "
               "and guessing would re-enable settings you never had.")
            + " Nothing was changed."
        )
    ctx.log("Taskbar items restored to their prior settings. Restart Explorer to see changes.")


def apply_local_search(ctx: TaskContext):
    """Make Start-menu search local-only and instant: no Bing, no web results,
    no cloud content (Sophia Script + privacy.sexy verified values)."""
    # M3: snapshot priors — the old revert deleted unconditionally.
    # F09: best-effort (HKLM half needs admin; limited-mode keeps HKCU half).
    from app.utils import reg_set_value as _set
    had_snapshot = bool(get_tweak_snapshot("local_search"))
    _snap_reg_values(ctx, "local_search", _LOCAL_SEARCH_SPECS)
    writes = [
        ("HKCU", "Software\\Policies\\Microsoft\\Windows\\Explorer", "DisableSearchBoxSuggestions", 1),
        ("HKLM", "SOFTWARE\\Policies\\Microsoft\\Windows\\Explorer", "DisableSearchBoxSuggestions", 1),
        ("HKCU", "Software\\Microsoft\\Windows\\CurrentVersion\\Search", "BingSearchEnabled", 0),
        ("HKCU", "Software\\Microsoft\\Windows\\CurrentVersion\\Search", "CortanaConsent", 0),
        ("HKCU", "Software\\Microsoft\\Windows\\CurrentVersion\\SearchSettings", "IsMSACloudSearchEnabled", 0),
        ("HKCU", "Software\\Microsoft\\Windows\\CurrentVersion\\SearchSettings", "IsAADCloudSearchEnabled", 0),
    ]
    ok, skipped = 0, []
    for hive, path, name, val in writes:
        if _set(ctx, hive, path, name, val):
            ok += 1
        else:
            skipped.append(f"{hive}\\{name}")
    if ok == 0:
        if not had_snapshot:
            clear_tweak_snapshot("local_search")
        raise RuntimeError("Could not apply local-search settings (all writes blocked). Nothing was marked as applied.")
    if skipped:
        ctx.log(f"  (applied {ok} of {len(writes)}; skipped: {'; '.join(skipped)})")
    ctx.log("Search is now local-only — results appear instantly with no web/Bing content.")


def revert_local_search(ctx: TaskContext):
    snap = get_tweak_snapshot("local_search")
    if snap and "specs" in snap:
        _restore_reg_values(ctx, "local_search")
    else:
        reg_delete_value(ctx, "HKCU", "Software\\Policies\\Microsoft\\Windows\\Explorer", "DisableSearchBoxSuggestions")
        reg_delete_value(ctx, "HKLM", "SOFTWARE\\Policies\\Microsoft\\Windows\\Explorer", "DisableSearchBoxSuggestions")
        reg_delete_value(ctx, "HKCU", "Software\\Microsoft\\Windows\\CurrentVersion\\Search", "BingSearchEnabled")
        reg_delete_value(ctx, "HKCU", "Software\\Microsoft\\Windows\\CurrentVersion\\Search", "CortanaConsent")
        reg_delete_value(ctx, "HKCU", "Software\\Microsoft\\Windows\\CurrentVersion\\SearchSettings", "IsMSACloudSearchEnabled")
        reg_delete_value(ctx, "HKCU", "Software\\Microsoft\\Windows\\CurrentVersion\\SearchSettings", "IsAADCloudSearchEnabled")
        ctx.log("  (no snapshot found — removed the values; Windows then uses its defaults)")
    ctx.log("Search restored to defaults (Bing + web suggestions back).")


def apply_hide_recent(ctx: TaskContext):
    """Stop Explorer Home from showing recent files and frequent folders —
    the switch that stops re-creating what the 'Clear Activity Traces'
    clean removes. Same values Settings writes when you untick both boxes
    (Folder Options > Privacy). Fully reversible via snapshot."""
    had_snapshot = bool(get_tweak_snapshot("hide_recent"))
    _snap_reg_values(ctx, "hide_recent", _HIDE_RECENT_SPECS)
    try:
        reg_set_value_checked(ctx, "HKCU", "Software\\Microsoft\\Windows\\CurrentVersion\\Explorer",
                              "ShowRecent", 0)
        reg_set_value_checked(ctx, "HKCU", "Software\\Microsoft\\Windows\\CurrentVersion\\Explorer",
                              "ShowFrequent", 0)
    except Exception:
        if not had_snapshot:
            clear_tweak_snapshot("hide_recent")
        raise
    if not _restart_explorer(ctx):
        raise RuntimeError(
            "Registry values set, but Explorer did not restart cleanly — "
            "your taskbar may be missing. Press Ctrl+Shift+Esc > File > Run "
            "new task > explorer.exe.")


def revert_hide_recent(ctx: TaskContext):
    snap = get_tweak_snapshot("hide_recent")
    if snap and "specs" in snap:
        _restore_reg_values(ctx, "hide_recent")
    else:
        # MED-007: the legacy fallback wrote 1, claiming that was "the
        # documented Windows default". It is not: the default is 0/absent, so
        # a user who had never enabled recent files got them ENABLED by
        # clicking Undo - the opposite of the change they were undoing. With
        # no snapshot the honest move is to write the value apply wrote's
        # opposite-of-default, i.e. 0, and to SAY the prior value is unknown
        # rather than pretending a guess is a restore.
        reg_set_value_checked(ctx, "HKCU", "Software\\Microsoft\\Windows\\CurrentVersion\\Explorer",
                              "ShowRecent", 0)
        reg_set_value_checked(ctx, "HKCU", "Software\\Microsoft\\Windows\\CurrentVersion\\Explorer",
                              "ShowFrequent", 0)
        ctx.log("  (no snapshot found — this was applied by an older version, so "
                "your previous setting is unknown. Set both to 0, the Windows "
                "default, rather than the 1 the old Undo used to write.)")
    if not _restart_explorer(ctx):
        ctx.log("  ! Explorer did not restart cleanly — your taskbar may be missing. "
                "Press Ctrl+Shift+Esc > File > Run new task > explorer.exe.")


def apply_file_extensions(ctx: TaskContext):
    """Show file extensions + hidden files (the modder's must-have)."""
    had_snapshot = bool(get_tweak_snapshot("file_extensions"))
    _snap_reg_values(ctx, "file_extensions", [("HKCU", _ADV, "HideFileExt"), ("HKCU", _ADV, "Hidden")])
    try:
        reg_set_value_checked(ctx, "HKCU", _ADV, "HideFileExt", 0)
        reg_set_value_checked(ctx, "HKCU", _ADV, "Hidden", 1)
    except Exception:
        if not had_snapshot:
            clear_tweak_snapshot("file_extensions")
        raise
    ctx.log("File extensions and hidden files are now shown.")


def revert_file_extensions(ctx: TaskContext):
    _restore_reg_values(ctx, "file_extensions")
    ctx.log("File visibility restored to its prior setting.")


def apply_menu_delay(ctx: TaskContext):
    """Menus pop in 100ms instead of 400ms."""
    had_snapshot = bool(get_tweak_snapshot("menu_delay"))
    _snap_reg_values(ctx, "menu_delay", [("HKCU", "Control Panel\\Desktop", "MenuShowDelay")])
    try:
        reg_set_value_checked(ctx, "HKCU", "Control Panel\\Desktop", "MenuShowDelay", "100", value_type="REG_SZ")
    except Exception:
        if not had_snapshot:
            clear_tweak_snapshot("menu_delay")
        raise
    ctx.log("Menu delay set to 100ms.")


def revert_menu_delay(ctx: TaskContext):
    # B-023: route through the shared restore pipeline (SEC-001 allowlist,
    # persistence denylist, \x00/type validation) instead of hand-rolling a
    # raw write from user-writable config.json while elevated.
    _restore_reg_values(ctx, "menu_delay")
    ctx.log("Menu delay restored to its prior setting.")


def apply_aero_shake_off(ctx: TaskContext):
    """Disable shake-to-minimize (no more nuked desktop mid-game)."""
    had_snapshot = bool(get_tweak_snapshot("aero_shake"))
    _snap_reg_values(ctx, "aero_shake", [("HKCU", _ADV, "DisallowShaking")])
    try:
        reg_set_value_checked(ctx, "HKCU", _ADV, "DisallowShaking", 1)
    except Exception:
        if not had_snapshot:
            clear_tweak_snapshot("aero_shake")
        raise
    ctx.log("Shake-to-minimize disabled.")


def revert_aero_shake_off(ctx: TaskContext):
    _restore_reg_values(ctx, "aero_shake")
    ctx.log("Shake-to-minimize restored to its prior setting.")


def apply_lock_screen_off(ctx: TaskContext):
    """Skip the lock screen — straight to login (admin, policy key)."""
    had_snapshot = bool(get_tweak_snapshot("lock_screen"))
    _snap_reg_values(ctx, "lock_screen",
                     [("HKLM", "SOFTWARE\\Policies\\Microsoft\\Windows\\Personalization", "NoLockScreen")])
    try:
        reg_set_value_checked(ctx, "HKLM", "SOFTWARE\\Policies\\Microsoft\\Windows\\Personalization", "NoLockScreen", 1)
    except Exception:
        if not had_snapshot:
            clear_tweak_snapshot("lock_screen")
        raise
    ctx.log("Lock screen skipped (takes effect at next sign-in).")


def revert_lock_screen_off(ctx: TaskContext):
    _restore_reg_values(ctx, "lock_screen")
    ctx.log("Lock screen setting restored.")


def apply_edge_preload_off(ctx: TaskContext):
    """Stop Edge's startup boost + background mode (admin; Edge updates may
    re-add these — rerun if Edge gets chatty again)."""
    had_snapshot = bool(get_tweak_snapshot("edge_preload"))
    _snap_reg_values(ctx, "edge_preload",
                     [("HKLM", "SOFTWARE\\Policies\\Microsoft\\Edge", "StartupBoostEnabled"),
                      ("HKLM", "SOFTWARE\\Policies\\Microsoft\\Edge", "BackgroundModeEnabled")])
    try:
        reg_set_value_checked(ctx, "HKLM", "SOFTWARE\\Policies\\Microsoft\\Edge", "StartupBoostEnabled", 0)
        reg_set_value_checked(ctx, "HKLM", "SOFTWARE\\Policies\\Microsoft\\Edge", "BackgroundModeEnabled", 0)
    except Exception:
        if not had_snapshot:
            clear_tweak_snapshot("edge_preload")
        raise
    ctx.log("Edge preloading disabled.")


def revert_edge_preload_off(ctx: TaskContext):
    _restore_reg_values(ctx, "edge_preload")
    ctx.log("Edge preload setting restored.")


def apply_dark_mode(ctx: TaskContext):
    """Prefer dark app themes."""
    had_snapshot = bool(get_tweak_snapshot("dark_mode"))
    _snap_reg_values(ctx, "dark_mode",
                     [("HKCU", "Software\\Microsoft\\Windows\\CurrentVersion\\Themes\\Personalize", "AppsUseLightTheme")])
    try:
        reg_set_value_checked(ctx, "HKCU", "Software\\Microsoft\\Windows\\CurrentVersion\\Themes\\Personalize", "AppsUseLightTheme", 0)
    except Exception:
        if not had_snapshot:
            clear_tweak_snapshot("dark_mode")
        raise
    ctx.log("Dark app theme preferred.")


def revert_dark_mode(ctx: TaskContext):
    _restore_reg_values(ctx, "dark_mode")
    ctx.log("App theme restored to its prior setting.")


def apply_verbose_boot(ctx: TaskContext):
    """Verbose boot/shutdown messages instead of the spinner (admin)."""
    had_snapshot = bool(get_tweak_snapshot("verbose_boot"))
    _snap_reg_values(ctx, "verbose_boot",
                     [("HKLM", "SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\Policies\\System", "VerboseStatus")])
    try:
        reg_set_value_checked(ctx, "HKLM", "SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\Policies\\System", "VerboseStatus", 1)
    except Exception:
        if not had_snapshot:
            clear_tweak_snapshot("verbose_boot")
        raise
    ctx.log("Verbose boot messages on (visible at next restart).")


def revert_verbose_boot(ctx: TaskContext):
    _restore_reg_values(ctx, "verbose_boot")
    ctx.log("Boot messages restored to normal.")


def apply_widgets_board_off(ctx: TaskContext):
    """Policy-level Widgets board off (goes further than hiding the taskbar
    icon: the board, news feed and its background activity stop entirely)."""
    had_snapshot = bool(get_tweak_snapshot("widgets_board_off"))
    _snap_reg_values(ctx, "widgets_board_off",
                     [("HKLM", "SOFTWARE\\Policies\\Microsoft\\Dsh", "AllowNewsAndInterests"),
                      ("HKCU", "Software\\Microsoft\\Windows\\CurrentVersion\\Feeds", "ShellFeedsTaskbarViewMode")])
    try:
        reg_set_value_checked(ctx, "HKLM", "SOFTWARE\\Policies\\Microsoft\\Dsh", "AllowNewsAndInterests", 0)
        reg_set_value_checked(ctx, "HKCU", "Software\\Microsoft\\Windows\\CurrentVersion\\Feeds", "ShellFeedsTaskbarViewMode", 2)
    except Exception:
        if not had_snapshot:
            clear_tweak_snapshot("widgets_board_off")
        raise
    ctx.log("Widgets board disabled (icon hide + news feed off).")


def revert_widgets_board_off(ctx: TaskContext):
    _restore_reg_values(ctx, "widgets_board_off")
    ctx.log("Widgets board restored.")


def apply_autoplay_off(ctx: TaskContext):
    """Disable AutoPlay/AutoRun for USB sticks and discs (plugging in a
    drive never auto-launches anything — classic USB-malware vector)."""
    had_snapshot = bool(get_tweak_snapshot("autoplay_off"))
    _snap_reg_values(ctx, "autoplay_off",
                     [("HKCU", "Software\\Microsoft\\Windows\\CurrentVersion\\Explorer\\AutoplayHandlers", "DisableAutoplay"),
                      ("HKCU", "Software\\Microsoft\\Windows\\CurrentVersion\\Policies\\Explorer", "NoDriveTypeAutoRun")])
    try:
        reg_set_value_checked(ctx, "HKCU", "Software\\Microsoft\\Windows\\CurrentVersion\\Explorer\\AutoplayHandlers", "DisableAutoplay", 1)
        reg_set_value_checked(ctx, "HKCU", "Software\\Microsoft\\Windows\\CurrentVersion\\Policies\\Explorer", "NoDriveTypeAutoRun", 255)
    except Exception:
        if not had_snapshot:
            clear_tweak_snapshot("autoplay_off")
        raise
    ctx.log("AutoPlay disabled for all drives.")


def revert_autoplay_off(ctx: TaskContext):
    _restore_reg_values(ctx, "autoplay_off")
    ctx.log("AutoPlay restored.")


def apply_snap_flyout_off(ctx: TaskContext):
    """Disable the Snap-layouts flyout that pops when hovering a window's
    maximize button mid-game (Win+arrows snapping keeps working)."""
    had_snapshot = bool(get_tweak_snapshot("snap_flyout_off"))
    _snap_reg_values(ctx, "snap_flyout_off",
                     [("HKCU", "Software\\Microsoft\\Windows\\CurrentVersion\\Explorer\\Advanced", "EnableSnapAssistFlyout")])
    try:
        reg_set_value_checked(ctx, "HKCU", "Software\\Microsoft\\Windows\\CurrentVersion\\Explorer\\Advanced", "EnableSnapAssistFlyout", 0)
    except Exception:
        if not had_snapshot:
            clear_tweak_snapshot("snap_flyout_off")
        raise
    ctx.log("Snap flyout off — maximize-hover no longer pops layouts.")


def revert_snap_flyout_off(ctx: TaskContext):
    _restore_reg_values(ctx, "snap_flyout_off")
    ctx.log("Snap flyout restored.")


def apply_storage_sense(ctx: TaskContext):
    """Turn ON Windows' built-in Storage Sense (Settings > System >
    Storage) so junk gets cleaned automatically — the machine maintains
    itself between Cleaner Tool runs.

    Writes the documented StoragePolicy values (same ones the Settings
    app writes): '01'=Sense enabled, '04'=clean temp files, '2048'=clean
    recycle bin — with a monthly cadence ('256'=0x30 days, the default
    Windows picks). Deliberately does NOT touch Downloads cleanup
    ('08'/'512' family) — auto-deleting user Downloads can destroy
    someone's files; Sense keeps to temp/bin debris only.

    HKCU-only (per-user setting) — no admin needed, undoable."""
    had_snapshot = bool(get_tweak_snapshot("storage_sense"))
    _snap_reg_values(ctx, "storage_sense",
                     [(_SENSE[0], _SENSE[1], "01"),
                      (_SENSE[0], _SENSE[1], "04"),
                      (_SENSE[0], _SENSE[1], "256")])
    try:
        reg_set_value_checked(ctx, _SENSE[0], _SENSE[1], "01", 1)
        reg_set_value_checked(ctx, _SENSE[0], _SENSE[1], "04", 1)
        reg_set_value_checked(ctx, _SENSE[0], _SENSE[1], "256", 0)  # 0 = cadence picks default
    except Exception:
        if not had_snapshot:
            clear_tweak_snapshot("storage_sense")
        raise
    ctx.log("Storage Sense ON — Windows now auto-cleans temp junk (monthly, default cadence).")


def revert_storage_sense(ctx: TaskContext):
    _restore_reg_values(ctx, "storage_sense")
    ctx.log("Storage Sense restored to its prior setting.")


def apply_fast_app_close(ctx: TaskContext):
    """Fast shutdown: Windows waits up to 20 seconds (20000ms) for apps
    to close on their own before force-killing them — that is the
    'Shutting down…' hang after quitting a game. Cuts the waits:
      * WaitToKillAppTimeout  20000 -> 2000  (grace period per app)
      * HungAppTimeout        5000  -> 1000  (before 'not responding')
    Plus AutoEndTasks=1 so Windows actually ends them instead of
    sitting on the 'These apps are preventing shutdown' screen.

    HKCU-only, snapshot + revert, no admin. These values only take
    effect at the next sign-out/shutdown."""
    had_snapshot = bool(get_tweak_snapshot("fast_app_close"))
    _snap_reg_values(ctx, "fast_app_close",
                     [("HKCU", _DESKTOP, "WaitToKillAppTimeout"),
                      ("HKCU", _DESKTOP, "HungAppTimeout"),
                      ("HKCU", _DESKTOP, "AutoEndTasks")])
    try:
        reg_set_value_checked(ctx, "HKCU", _DESKTOP, "WaitToKillAppTimeout", "2000", value_type="REG_SZ")
        reg_set_value_checked(ctx, "HKCU", _DESKTOP, "HungAppTimeout", "1000", value_type="REG_SZ")
        reg_set_value_checked(ctx, "HKCU", _DESKTOP, "AutoEndTasks", "1", value_type="REG_SZ")
    except Exception:
        if not had_snapshot:
            clear_tweak_snapshot("fast_app_close")
        raise
    ctx.log("Fast app close on: shutdown waits drop from 20s to 2s before apps are ended.")


def revert_fast_app_close(ctx: TaskContext):
    from app.config_persist import get_tweak_snapshot, clear_tweak_snapshot
    snap = get_tweak_snapshot("fast_app_close")
    if not snap:
        ctx.log("  (no snapshot found — restoring Windows defaults)")
        reg_set_value_checked(ctx, "HKCU", _DESKTOP, "WaitToKillAppTimeout", "20000", value_type="REG_SZ")
        reg_set_value_checked(ctx, "HKCU", _DESKTOP, "HungAppTimeout", "5000", value_type="REG_SZ")
        reg_delete_value(ctx, "HKCU", _DESKTOP, "AutoEndTasks")
    else:
        _restore_reg_values(ctx, "fast_app_close", value_type="REG_SZ")
    clear_tweak_snapshot("fast_app_close")
    ctx.log("Shutdown waits restored.")


def apply_instant_alt_tab(ctx: TaskContext):
    """Instant window focus: Windows enforces a 200000ms (!) lockout
    during which a background window may NOT steal focus — that is the
    lag when alt-tabbing back into a fullscreen game (the click 'eats'
    for a moment, the shell feels sticky). Setting ForegroundLockTimeout
    to 0 removes the lockout delay so the switch is immediate. The
    official 'prevent focus stealing' config pairs this with
    ForegroundFlashCount=0 (no orange taskbar flashing fight).

    HKCU-only, snapshot + revert, no admin, takes effect at next
    sign-in (Explorer reads it at logon)."""
    had_snapshot = bool(get_tweak_snapshot("instant_alt_tab"))
    _snap_reg_values(ctx, "instant_alt_tab",
                     [("HKCU", _DESKTOP, "ForegroundLockTimeout"),
                      ("HKCU", _DESKTOP, "ForegroundFlashCount")])
    try:
        reg_set_value_checked(ctx, "HKCU", _DESKTOP, "ForegroundLockTimeout", 0)   # REG_DWORD needs an int, not "0"
        reg_set_value_checked(ctx, "HKCU", _DESKTOP, "ForegroundFlashCount", 0)
    except Exception:
        if not had_snapshot:
            clear_tweak_snapshot("instant_alt_tab")
        raise
    ctx.log("Alt-tab focus delay removed — windows switch instantly at next sign-in.")


def revert_instant_alt_tab(ctx: TaskContext):
    from app.config_persist import get_tweak_snapshot, clear_tweak_snapshot
    snap = get_tweak_snapshot("instant_alt_tab")
    if not snap:
        ctx.log("  (no snapshot found — restoring Windows defaults)")
        reg_set_value_checked(ctx, "HKCU", _DESKTOP, "ForegroundLockTimeout", 200000)
        reg_delete_value(ctx, "HKCU", _DESKTOP, "ForegroundFlashCount")
    else:
        _restore_reg_values(ctx, "instant_alt_tab")
    clear_tweak_snapshot("instant_alt_tab")
    ctx.log("Focus lockout restored.")


def apply_numlock_boot(ctx: TaskContext):
    """NumLock ON at every boot — for MMO/RPG players whose muscle memory
    expects the numpad. Windows remembers the numlock state per user,
    but fast startup, some BIOSes and certain keyboards reset it, and on
    fresh sign-ins it lands off. InitialKeyboardIndicators = "2" is the
    documented 'always on at logon' value (2147483648 variants exist for
    scroll lock combos; 2 is the standard numlock-only form).

    HKCU-only (per-user), REG_SZ, snapshot + revert, no admin.
    Takes effect at the next sign-in/boot."""
    had_snapshot = bool(get_tweak_snapshot("numlock_boot"))
    _snap_reg_values(ctx, "numlock_boot",
                     [("HKCU", _DESKTOP, "InitialKeyboardIndicators")])
    try:
        reg_set_value_checked(ctx, "HKCU", _DESKTOP, "InitialKeyboardIndicators", "2", value_type="REG_SZ")
    except Exception:
        if not had_snapshot:
            clear_tweak_snapshot("numlock_boot")
        raise
    ctx.log("NumLock will be ON at every boot from the next sign-in.")


def revert_numlock_boot(ctx: TaskContext):
    from app.config_persist import get_tweak_snapshot, clear_tweak_snapshot
    snap = get_tweak_snapshot("numlock_boot")
    if not snap:
        ctx.log("  (no snapshot found — restoring the documented default)")
        # B-025: the docstring itself says "2" is the standard numlock-only
        # form; 2147483648 (0x80000000) is the "preserve prior state" bit-set
        # variant and flipped boot keyboard behaviour to an undocumented state.
        reg_set_value_checked(ctx, "HKCU", _DESKTOP, "InitialKeyboardIndicators", "2", value_type="REG_SZ")
    else:
        _restore_reg_values(ctx, "numlock_boot", value_type="REG_SZ")
    clear_tweak_snapshot("numlock_boot")
    ctx.log("NumLock boot behavior restored.")


def apply_taskbar_endtask(ctx: TaskContext):
    """Right-click any taskbar icon to End Task — kill a frozen game in
    two clicks instead of opening Task Manager. Windows 11 23H2+ only
    (the value is inert on Win10); per-user, instant, no reboot."""
    had_snapshot = bool(get_tweak_snapshot("taskbar_endtask"))
    _snap_reg_values(ctx, "taskbar_endtask", [("HKCU", _TaskbarDevSettings, "TaskbarEndTask")])
    try:
        reg_set_value_checked(ctx, "HKCU", _TaskbarDevSettings, "TaskbarEndTask", 1)
    except Exception:
        if not had_snapshot:
            clear_tweak_snapshot("taskbar_endtask")
        raise
    ctx.log("Taskbar End Task on: right-click any app to force-close it.")


def revert_taskbar_endtask(ctx: TaskContext):
    _restore_reg_values(ctx, "taskbar_endtask")
    ctx.log("Taskbar End Task restored to its prior setting.")


def verify_taskbar_endtask() -> "bool | None":
    return _verify_value("HKCU", _TaskbarDevSettings, "TaskbarEndTask", 1)


def apply_clock_seconds(ctx: TaskContext):
    """Show seconds in the taskbar clock — handy for cooldown and queue
    timers. Per-user, instant, snapshot + revert."""
    had_snapshot = bool(get_tweak_snapshot("clock_seconds"))
    _snap_reg_values(ctx, "clock_seconds",
                     [("HKCU", _ADV, "ShowSecondsInSystemClock")])
    try:
        reg_set_value_checked(ctx, "HKCU", _ADV, "ShowSecondsInSystemClock", 1)
    except Exception:
        if not had_snapshot:
            clear_tweak_snapshot("clock_seconds")
        raise
    ctx.log("Taskbar clock now shows seconds.")


def revert_clock_seconds(ctx: TaskContext):
    _restore_reg_values(ctx, "clock_seconds")
    ctx.log("Taskbar clock restored to its prior setting.")


def verify_clock_seconds() -> "bool | None":
    return _verify_value("HKCU", _ADV, "ShowSecondsInSystemClock", 1)


def apply_taskbar_left(ctx: TaskContext):
    """Left-align the taskbar Start button (classic feel) instead of
    centered. Per-user, instant, snapshot + revert."""
    had_snapshot = bool(get_tweak_snapshot("taskbar_left"))
    _snap_reg_values(ctx, "taskbar_left", [("HKCU", _ADV, "TaskbarAl")])
    try:
        reg_set_value_checked(ctx, "HKCU", _ADV, "TaskbarAl", 0)
    except Exception:
        if not had_snapshot:
            clear_tweak_snapshot("taskbar_left")
        raise
    ctx.log("Taskbar aligned left — classic Start position.")


def revert_taskbar_left(ctx: TaskContext):
    _restore_reg_values(ctx, "taskbar_left")
    ctx.log("Taskbar alignment restored to its prior setting.")


def verify_taskbar_left() -> "bool | None":
    return _verify_value("HKCU", _ADV, "TaskbarAl", 0)


# --- Round 7 tasks (user request: Winhance-audit picks) --- #
# All snapshot + revert (+verify), Custom-only, presets untouched.


def apply_alt_tab_windows_only(ctx: TaskContext):
    """Alt+Tab flips between real windows and games only — no Microsoft
    Edge tabs crowding the switcher mid-game. Per-user, snapshot + revert."""
    had_snapshot = bool(get_tweak_snapshot("alt_tab_windows_only"))
    _snap_reg_values(ctx, "alt_tab_windows_only",
                     [("HKCU", _ADV, "MultiTaskingAltTabFilter")])
    try:
        reg_set_value_checked(ctx, "HKCU", _ADV, "MultiTaskingAltTabFilter", 3)
    except Exception:
        if not had_snapshot:
            clear_tweak_snapshot("alt_tab_windows_only")
        raise
    ctx.log("Alt+Tab now shows windows only — no browser tabs in the way.")


def revert_alt_tab_windows_only(ctx: TaskContext):
    _restore_reg_values(ctx, "alt_tab_windows_only")
    ctx.log("Alt+Tab filter restored to its prior setting.")


def verify_alt_tab_windows_only() -> "bool | None":
    return _verify_value("HKCU", _ADV, "MultiTaskingAltTabFilter", 3)


def apply_taskbar_never_combine(ctx: TaskContext):
    """Every open window keeps its own labeled taskbar button instead of
    piling up under one icon. Per-user, snapshot + revert (takes effect
    after Explorer restarts)."""
    had_snapshot = bool(get_tweak_snapshot("taskbar_never_combine"))
    _snap_reg_values(ctx, "taskbar_never_combine",
                     [("HKCU", _ADV, "TaskbarGlomLevel")])
    try:
        reg_set_value_checked(ctx, "HKCU", _ADV, "TaskbarGlomLevel", 2)
    except Exception:
        if not had_snapshot:
            clear_tweak_snapshot("taskbar_never_combine")
        raise
    ctx.log("Taskbar buttons never combine now (restart Explorer to see it).")


def revert_taskbar_never_combine(ctx: TaskContext):
    _restore_reg_values(ctx, "taskbar_never_combine")
    ctx.log("Taskbar combining restored to its prior setting.")


def verify_taskbar_never_combine() -> "bool | None":
    return _verify_value("HKCU", _ADV, "TaskbarGlomLevel", 2)


def apply_long_paths(ctx: TaskContext):
    """Let games and mods use very long file and folder names without
    errors (32,767 chars instead of 260). Machine-wide, needs admin and
    a reboot to fully take effect. Snapshot + revert."""
    had_snapshot = bool(get_tweak_snapshot("long_paths"))
    _snap_reg_values(ctx, "long_paths",
                     [("HKLM", "SYSTEM\\CurrentControlSet\\Control\\FileSystem",
                       "LongPathsEnabled")])
    try:
        reg_set_value_checked(ctx, "HKLM", "SYSTEM\\CurrentControlSet\\Control\\FileSystem",
                              "LongPathsEnabled", 1)
    except Exception:
        if not had_snapshot:
            clear_tweak_snapshot("long_paths")
        raise
    ctx.log("Long file paths on — reboots to fully take effect for all apps.")


def revert_long_paths(ctx: TaskContext):
    _restore_reg_values(ctx, "long_paths")
    ctx.log("Long paths setting restored (reboot to fully apply).")


def verify_long_paths() -> "bool | None":
    return _verify_value("HKLM", "SYSTEM\\CurrentControlSet\\Control\\FileSystem",
                         "LongPathsEnabled", 1)


def apply_classic_shortcut_icons(ctx: TaskContext):
    """Clean desktop icons: no arrow overlay, no ' - Shortcut' suffix.
    Best-effort (the HKLM arrow half needs admin); the HKCU suffix half
    works everywhere. Snapshot + revert restores exact prior bytes."""
    from app.utils import reg_set_value as _set
    had_snapshot = bool(get_tweak_snapshot("classic_shortcut_icons"))
    _snap_reg_values(ctx, "classic_shortcut_icons",
                     [("HKLM", _SHELL_ICONS, "29", "REG_SZ"),
                      ("HKCU", _LINK_PATH, "link", "REG_BINARY")])
    ok, skipped = 0, []
    if _set(ctx, "HKLM", _SHELL_ICONS, "29", "", value_type="REG_SZ"):
        ok += 1
    else:
        skipped.append("HKLM arrow")
    try:
        from app.utils import reg_get_value as _get
        prior = _get(ctx, "HKCU", _LINK_PATH, "link")
        blank = bytes(len(prior)) if isinstance(prior, bytes) else bytes(16)
    except Exception:
        blank = bytes(16)
    if _set(ctx, "HKCU", _LINK_PATH, "link", blank, value_type="REG_BINARY"):
        ok += 1
    else:
        skipped.append("HKCU suffix")
    if ok == 0:
        if not had_snapshot:
            clear_tweak_snapshot("classic_shortcut_icons")
        raise RuntimeError("Could not clean shortcut icons (all writes blocked). Nothing was marked as applied.")
    if skipped:
        ctx.log(f"  (partially applied; skipped: {'; '.join(skipped)})")
    ctx.log("Shortcut icons cleaned — no arrows, no ' - Shortcut' suffix.")


def revert_classic_shortcut_icons(ctx: TaskContext):
    _restore_reg_values(ctx, "classic_shortcut_icons")
    ctx.log("Shortcut icons restored to their prior setting.")


def verify_classic_shortcut_icons() -> "bool | None":
    # applied state = all-zero bytes of whatever length Windows uses
    # here (4 on some builds, 16 on others) — compare content, not length
    try:
        v = __verify_read("HKCU", _LINK_PATH, "link")
        if v is None:
            return None
        if isinstance(v, bytes) and len(v) and all(b == 0 for b in v):
            return True
        return False
    except Exception:
        return None


def verify_classic_context_menu() -> "bool | None":
    v = __verify_read("HKCU", f"Software\\Classes\\CLSID\\{CONTEXT_MENU_CLSID}\\InprocServer32", "")
    return None if v is None else (str(v) == "")


def verify_hide_recent() -> "bool | None":
    return _verify_all_values([
        ("HKCU", "Software\\Microsoft\\Windows\\CurrentVersion\\Explorer", "ShowRecent", 0),
        ("HKCU", "Software\\Microsoft\\Windows\\CurrentVersion\\Explorer", "ShowFrequent", 0),
    ])


def verify_local_search() -> "bool | None":
    return _verify_value("HKCU", "Software\\Microsoft\\Windows\\CurrentVersion\\Search", "BingSearchEnabled", 0)


def apply_transparency_off(ctx: TaskContext):
    """Drop the see-through acrylic/mica effect on titles and surfaces.

    Cosmetic only, per-user, instantly reversible. Some people find the
    translucency distracting or slightly harder to read; this turns it off
    without touching any other visual effect (animations, colour and font
    smoothing are separate and untouched).
    """
    had_snapshot = bool(get_tweak_snapshot("transparency_off"))
    _snap_reg_values(ctx, "transparency_off",
                     [("HKCU", _PERSONALIZE, "EnableTransparency")])
    try:
        reg_set_value_checked(ctx, "HKCU", _PERSONALIZE,
                              "EnableTransparency", 0)
    except Exception:
        if not had_snapshot:
            clear_tweak_snapshot("transparency_off")
        raise
    ctx.log("Transparency effects turned off (solid backgrounds).")


def revert_transparency_off(ctx: TaskContext):
    _restore_reg_values(ctx, "transparency_off")
    ctx.log("Transparency effects restored to their prior setting.")


def verify_transparency_off() -> "bool | None":
    return _verify_value("HKCU", _PERSONALIZE, "EnableTransparency", 0)
