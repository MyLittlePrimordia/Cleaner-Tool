"""
Phase 2 data layer — single source of truth for the 3-tab layout and the
preset tiers shown in the UI.

Responsibilities:
  * Merge the old 5 tabs into 3 (Games -> Clean, Advanced -> Tweak) with
    dedupe, per punch-list items #10 and #12.
  * Remove from the app entirely (per punch-list #13): Disable Memory
    Integrity (HVCI), Disable VMP/Hyper-V, Disable WPBT.
  * Define the preset tiers per punch-list #13 (Clean: Quick/Deep; Repair:
    Quick/Deep; Tweak: Minimal/Recommended) plus what's Custom-only.
  * Self-validate on import: every preset key must resolve to a real Task,
    merged tabs must not contain duplicate keys, and presets must not leak
    cut tasks back in. Raises at import time so a typo can never ship.

The GUI imports TABS / PRESETS from here and never builds task lists itself.
"""

from app.tasks import clean_tasks, repair_tasks, tweak_tasks, game_tasks, advanced_tasks, install_tasks

# --------------------------------------------------------------------------- #
# Cuts (punch-list #13: "Remove from app")
# --------------------------------------------------------------------------- #

CUT_TASK_KEYS = {"adv_memory_integrity", "adv_vmp", "wpbt_disable"}

# --------------------------------------------------------------------------- #
# Merge: Games -> Clean (dedupe)
# --------------------------------------------------------------------------- #
# Games tab tasks and their Clean-tab twins:
#   gamer_launchers  == launcher_cache   (identical path list since M5 fix)
#   gpu_shader_caches == shader_cache     (identical path list since M2 fix)
#   game_files       (unique — 100+ per-game junk table)  -> kept
#   game_captures    (unique — Xbox Game Bar clips)       -> kept
#
# The dedup rule: keep the CLEAN-tab twin (it's already referenced by saved
# configs and the scheduler), absorb the unique Games tasks into Clean.

_GAMES_TO_CLEAN_DEDUPE = {"gamer_launchers": "launcher_cache", "gpu_shader_caches": "shader_cache"}

def _merge_clean():
    merged = list(clean_tasks.TASKS)  # Phase-1 list, M2/M5 dedupe already applied
    seen = {t.key for t in merged}
    for t in game_tasks.TASKS:
        if t.key in _GAMES_TO_CLEAN_DEDUPE:
            continue
        if t.key not in seen:
            merged.append(t)
            seen.add(t.key)
    return merged

# --------------------------------------------------------------------------- #
# Merge: Advanced -> Tweak (custom-only, with cuts)
# --------------------------------------------------------------------------- #

def _merge_tweak():
    merged = list(tweak_tasks.TASKS)
    seen = {t.key for t in merged}
    for t in advanced_tasks.TASKS:
        if t.key in CUT_TASK_KEYS:
            continue  # punch-list #13: removed from the app
        if t.key not in seen:
            merged.append(t)
            seen.add(t.key)
    return merged

# --------------------------------------------------------------------------- #
# The three tabs
# --------------------------------------------------------------------------- #

TAB_NAMES = ["Clean", "Repair", "Tweak", "Install"]

TABS = {
    "Clean": _merge_clean(),
    "Repair": list(repair_tasks.TASKS),
    "Tweak": _merge_tweak(),
    "Install": list(install_tasks.TASKS),
}

# --------------------------------------------------------------------------- #
# Preset tiers (punch-list #13)
# --------------------------------------------------------------------------- #

PRESETS = {
    "Clean": {
        "Quick Clean": [
            # Fast, no-explorer-restart, zero-risk junk that accumulates daily.
            # audit fix: ram_purge moved out (anti-performance in the everyday
            # preset — trimming working sets causes immediate page-in stalls;
            # it stays available in Custom and Deep Clean).
            "shader_cache", "launcher_cache", "engine_cache", "driver_junk",
            "user_temp_files", "inet_cache", "recycle_bin", "error_reports",
            "old_logs", "dns_flush", "game_files",
        ],
        "Deep Clean": [
            # Everything Quick plus the heavier / admin / rarely-needed passes.
            # audit fix (dedupe): temp_deep_clean removed — Quick+Deep already
            # run user_temp_files+system_temp_files, and temp_deep_clean's PS
            # pass + fallbacks walked both SAME dirs a second time (5/5 path
            # overlap verified); it stays available in Custom.
            "shader_cache", "launcher_cache", "engine_cache", "driver_junk",
            "user_temp_files", "system_temp_files", "win_update_cache",
            "delivery_optimization", "inet_cache", "recycle_bin", "error_reports",
            "thumbnail_cache", "chk_fragments", "old_logs", "dns_flush", "ram_purge",
            "game_files", "game_captures", "update_leftovers", "activity_traces",
            "browser_cache", "office_cache", "uwp_cache", "font_cache", "store_cache",
            "remove_bloat", "prefetch", "disk_cleanup_deep",
        ],
    },
    "Repair": {
        "Quick Repair": [
            # Cheap, minutes-or-less checks that catch the common breakage.
            "restore_point", "ssd_maintenance", "dism_checkhealth", "chkdsk_scan",
            "smart_verdict", "gpu_driver_age", "bits_reset", "time_sync", "gpupdate",
        ],
        "Deep Repair": [
            # The heavy end: full corruption repair stack. DISM rebuilds the
            # component store BEFORE SFC runs, so SFC pulls from fresh files.
            "restore_point", "ssd_maintenance", "dism_checkhealth", "dism_scanhealth",
            "dism_restorehealth", "sfc_scan", "dism_cleanup", "chkdsk_scan",
            "wu_reset", "bits_reset", "network_reset", "time_sync", "gpupdate",
            "search_index", "print_spooler", "wmi_repair", "store_apps_reregister",
            "xbox_apps", "vss_repair", "restart_audio",
        ],
    },
    "Tweak": {
        "Minimal": [
            "restore_point_tweak", "ultimate_performance", "classic_context_menu",
            "disable_game_dvr", "game_mode", "windowed_optimize", "local_search",
            "taskbar_cleanup", "mouse_accel", "keyboard_tuning", "usb_suspend",
            "disk_timeout", "end_task_taskbar",
        ],
        "Recommended": [
            # Minimal + privacy/telemetry/ads + perf polish, per punch-list #13,
            # plus the focus/stability additions (Sticky Keys, crash popups,
            # update-reboot blocker).
            "restore_point_tweak", "ultimate_performance", "classic_context_menu",
            "disable_game_dvr", "game_mode", "windowed_optimize", "local_search",
            "taskbar_cleanup", "mouse_accel", "keyboard_tuning", "usb_suspend",
            "disk_timeout", "end_task_taskbar",
            "privacy_baseline", "stop_telemetry", "nvidia_telemetry",
            "stop_windows_ads", "limit_telemetry", "visual_effects",
            "games_priority", "background_apps", "shader_cache_10gb",
            "fullscreen_opt", "explorer_auto_discovery", "ad_blocker",
            "disable_sticky_keys", "suppress_crash_popups", "no_update_reboot",
        ],
        "Game Session": [
            # One click right before launching a game: fastest power plan,
            # game-first scheduling, distractions off. All reversible; run
            # 'Undo Tweaks' after your session to put everything back.
            "ultimate_performance", "game_mode", "games_priority",
            "max_cpu_power", "background_apps", "no_update_reboot",
            "disable_sticky_keys", "suppress_crash_popups", "fullscreen_opt",
        ],
    },
    # Install tab: no curated presets — its 'preset card' row is the catalog
    # browser (categories + checkboxes) per the Install.txt spec; the LTSC
    # prerequisite installers above are the Task list (Custom-mode grid).
    "Install": {},
}

# --------------------------------------------------------------------------- #
# Custom-mode groups (user request: group the Custom toggles by what the
# tasks DO, so non-technical users can find things). The GUI renders one
# collapsible-free section header per group, in this order; every task key
# on the tab must appear in exactly one group (validated below, so a new
# task without a group can never ship silently).
# --------------------------------------------------------------------------- #

CUSTOM_GROUPS = {
    "Clean": [
        ("Game files & caches",
         ["shader_cache", "launcher_cache", "engine_cache", "driver_junk",
          "game_files", "game_captures", "steam_stuck", "steam_depot"]),
        ("Saves safety net",
         ["backup_saves"]),
        ("Windows temp & system junk",
         ["user_temp_files", "system_temp_files", "temp_deep_clean",
          "recycle_bin", "chk_fragments", "old_logs", "error_reports",
          "prefetch", "event_logs", "defender_history"]),
        ("Windows Update leftovers",
         ["win_update_cache", "delivery_optimization", "update_leftovers",
          "disk_cleanup_deep"]),
        ("App, browser & Store caches",
         ["inet_cache", "browser_cache", "office_cache", "uwp_cache",
          "font_cache", "store_cache", "thumbnail_cache", "winget_cache",
          "dev_caches"]),
        ("Privacy traces",
         ["activity_traces"]),
        ("Quick fixes & bloat removal",
         ["dns_flush", "ram_purge", "remove_bloat"]),
    ],
    "Repair": [
        ("Safety & drives",
         ["restore_point", "ssd_maintenance", "chkdsk_scan", "vss_repair",
          "smart_verdict", "enable_restore"]),
        ("Windows image & system files",
         ["dism_checkhealth", "dism_scanhealth", "sfc_scan",
          "dism_restorehealth", "dism_cleanup"]),
        ("Updates & downloads",
         ["wu_reset", "bits_reset", "time_sync", "gpupdate"]),
        ("Network & firewall",
         ["network_reset", "firewall_reset", "arp_flush"]),
        ("Apps, Xbox, devices & drivers",
         ["xbox_apps", "store_apps_reregister", "search_index",
          "print_spooler", "wmi_repair", "restart_audio",
          "gpu_driver_age", "restart_bluetooth", "gpu_reset",
          "anticheat_repair", "icon_cache", "wsreset_store"]),
    ],
    "Tweak": [
        ("Safety net",
         ["restore_point_tweak"]),
        ("Gaming & CPU/GPU performance",
         ["ultimate_performance", "max_cpu_power", "max_performance_gpu",
          "game_mode", "games_priority", "priority_separation",
          "power_throttling_off", "disable_game_dvr", "windowed_optimize",
          "fullscreen_opt", "hags", "shader_cache_10gb", "background_apps",
          "mpo_fix", "adv_memory_compression", "gpu_preference_high",
          "refresh_rate_fix"]),
        ("Network & online ping",
         ["disable_nagle", "network_throttling", "gaming_dns",
          "tweak_delivery_optimization", "eee_disable"]),
        ("SSD & drive behavior",
         ["ssd_trim", "ssd_superfetch", "ssd_last_access", "ssd_prefetch",
          "disk_timeout", "adv_hibernation", "ntfs_8dot3"]),
        ("Privacy, telemetry & ads",
         ["privacy_baseline", "stop_telemetry", "limit_telemetry",
          "nvidia_telemetry", "activity_history", "adv_copilot",
          "adv_disable_ai", "consumer_features", "stop_windows_ads",
          "ad_blocker", "remote_assist", "location_tracking"]),
        ("Windows look, search & startup",
         ["classic_context_menu", "taskbar_cleanup", "local_search",
          "visual_effects", "explorer_auto_discovery", "end_task_taskbar",
          "startup_delay", "disable_fast_startup", "file_extensions",
          "menu_delay", "aero_shake", "lock_screen", "edge_preload",
          "dark_mode", "verbose_boot"]),
        ("Input, USB & interruptions",
         ["mouse_accel", "keyboard_tuning", "usb_suspend",
          "disable_sticky_keys", "suppress_crash_popups",
          "no_update_reboot"]),
        ("Benchmark-only extras",
         ["dynamic_tick_off"]),
    ],
}

# --------------------------------------------------------------------------- #
# Self-validation — runs on import; a bad key can never ship silently.
# --------------------------------------------------------------------------- #

def _validate():
    for name in TAB_NAMES:
        keys = [t.key for t in TABS[name]]
        dupes = {k for k in keys if keys.count(k) > 1}
        if dupes:
            raise RuntimeError(f"tab_presets: duplicate task keys in {name}: {sorted(dupes)}")
        for key in CUT_TASK_KEYS:
            if key in keys:
                raise RuntimeError(f"tab_presets: cut task {key} leaked into {name}")

    for tab, presets in PRESETS.items():
        valid_keys = {t.key for t in TABS[tab]}
        for preset, keys in presets.items():
            unknown = [k for k in keys if k not in valid_keys]
            if unknown:
                raise RuntimeError(f"tab_presets: {tab}/{preset} has unknown keys {unknown}")
            if len(set(keys)) != len(keys):
                raise RuntimeError(f"tab_presets: {tab}/{preset} has duplicate keys")

    for tab, groups in CUSTOM_GROUPS.items():
        valid_keys = {t.key for t in TABS[tab]}
        grouped: list = []
        for title, keys in groups:
            unknown = [k for k in keys if k not in valid_keys]
            if unknown:
                raise RuntimeError(f"tab_presets: {tab}/{title} has unknown keys {unknown}")
            if len(set(keys)) != len(keys):
                raise RuntimeError(f"tab_presets: {tab}/{title} has duplicate keys")
            grouped.extend(keys)
        if len(set(grouped)) != len(grouped):
            raise RuntimeError(f"tab_presets: {tab} has a task in two groups")
        missing = valid_keys - set(grouped)
        if missing:
            raise RuntimeError(f"tab_presets: {tab} has ungrouped tasks: {sorted(missing)}")

_validate()
