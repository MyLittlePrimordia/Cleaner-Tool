"""
Config persistence for scheduled runs and user preferences.
"""

import copy
import json
import os
import sys
import threading
from pathlib import Path

# H2/H3: path resolution and the event log now live in leaf modules, and are
# re-exported here so every existing importer
# (`from app.config_persist import CONFIG_DIR, log_security_event`) keeps
# working untouched. config_persist is now a consumer of these rather than
# their owner, which is what breaks the utils -> config_persist -> ... edge
# of the repository's only import cycle.
from app.config_paths import (  # noqa: F401  (re-exported on purpose)
    CONFIG_DIR,
    CONFIG_FILE,
    EVENTS_LOG_FILE,
    get_config_dir,
)
from app.config_events import log_security_event  # noqa: F401  (re-exported)

#: LOW-007: refuse to json.load() a config larger than this. A real config is a
#: few KB; 32 MB is ~1000x headroom and still bounds what a truncated write, a
#: runaway append, or a stray large file can cost us in RAM. update_checker
#: already caps its input the same way.
_MAX_CONFIG_BYTES = 32 * 1024 * 1024

# Legacy alias: the old private name is still referenced by name in the
# quarantine path's history; keep it importable so nothing external breaks.
_get_config_dir = get_config_dir

DEFAULT_CONFIG = {
    "schedule_enabled": False,
    # H7: this key was written by app/scheduler.py and read by the Auto
    # Maintenance dialog, but was never declared here — so it was never
    # defaulted and never validated, and a hand-edited "yes" rendered the
    # dialog's update checkbox as ON. Two features, one scheduled task each.
    "schedule_update_enabled": False,
    "schedule_frequency": "weekly",  # daily, weekly, monthly
    "schedule_time": "03:00",
    # Phase 2 (#12): 5 tabs merged to 3 — Games folded into Clean, Advanced
    # into Tweak. Kept as an explicit dict so load_config can migrate old
    # saved configs (see _migrate_selected_tasks).
    "selected_tasks": {
        "Clean": [],
        "Repair": [],
        "Tweak": [],
    },
    # Real pre-tweak values captured at apply-time, keyed by task id, so
    # revert can restore the machine's actual prior setting instead of a
    # hardcoded guess. Each entry is {"subgroup:setting": index, ...}.
    "tweak_snapshots": {},
    # Phase 2 (#14): tweaks the app has applied and not yet reverted —
    # the source for the UI's "Applied" badges, kept distinct from the
    # on/off state of any toggle. Snapshots (above) also imply "applied".
    "applied_tweaks": [],
    # Game Session Auto-Pilot (user-approved feature 6, 2026-09): all
    # off/empty by default. enabled = watcher thread may run while the
    # app is open; games = extra .exe names the user added (lowercase,
    # with extension); preset = which Tweak preset to apply per session.
    # Phase-5 keys: paths = full exe paths of picked games (watch-matched
    # by parent dir, immune to same-name exes elsewhere); watch_all =
    # auto-watch every installed game found by app.game_catalog at
    # watcher start (OFF by default — explicit consent, no magic).
    # Additive keys — old configs merge them on load (see below).
    "session_pilot_enabled": False,
    "session_pilot_games": [],
    "session_pilot_preset": "Game Session",
    "session_pilot_paths": [],
    # Default-ON for NEW configs (user ruling 2026-09-12): watch-all gives
    # wide, path-verified detection with zero setup. The flip is a
    # DEFAULT only — any config that already carries the key (every
    # config touched by a prior app version) keeps the user's explicit
    # choice, because the merge below only fills MISSING keys.
    "session_pilot_watch_all": True,
    # display names for path picks (path -> "Baldur's Gate 3"): pure UI
    # sugar, never consulted by matching. Dict coerced like the others.
    "session_pilot_names": {},
    # Game Server Ping custom targets ("hostname" = ICMP ping,
    # "hostname:port" = TCP handshake). Same coercion contract as the
    # pilot path picks above.
    "gameping_hosts": [],
    # Feature 10 ("How is my PC doing?"): a tiny rolling log of finished
    # Clean runs — [{"t": unix_seconds, "b": bytes_freed}], newest last,
    # capped at _RUN_HISTORY_MAX. Only used to print one friendly
    # sentence on the scorecard ("You've freed 12 GB this month"). No
    # paths, no task names, nothing identifying — just a date and a size.
    "run_history": [],
    # Feature 7 (Install Profiles / "My Setups"): {name: [package ids]}.
    # Saved app picks the user can restore in one click.
    "install_profiles": {},
    # Feature 9 (Game Night): active = the user pressed Start and has not
    # pressed End; keys = the tweaks Game Night itself turned on, so End
    # can put back exactly those and nothing the user had already set.
    # close_apps = which optional background apps to quiet (all off).
    # Survives a restart on purpose: closing the app mid-session must not
    # strand the machine in game settings with no way back.
    "game_night_active": False,
    "game_night_keys": [],
    "game_night_close_apps": [],
    # Startup Manager: items the user has disabled, enough data to put
    # each one back exactly as it was. Registry items store {source,
    # name, command, value_type}; Startup-folder items store {source,
    # name, command} where command is the held file's path under this
    # app's own config directory (never the user's Startup folder).
    "startup_disabled": [],
    # Auto-elevate ("stop asking me"): opt-in once on the Admin Gate, then
    # an on-demand elevated scheduled task relaunches the app without UAC.
    # remember_limited is the opposite crowd (never show the Gate). Both
    # merge onto old configs like every other additive key below.
    "auto_elevate": False,
    "remember_limited": False,
    # SEC-004: this used to default to True and to exclude the executable's
    # PARENT DIRECTORY. Two problems, both real:
    #   * a security-weakening preference should not be opt-OUT, especially
    #     when the app is portable - running it from Desktop, Downloads or any
    #     shared folder silently excluded everything else in that folder from
    #     Defender scanning, and it was reapplied on every elevated launch;
    #   * there was NO removal path anywhere in the codebase, so a user who
    #     wanted it gone had to open an elevated PowerShell themselves.
    # It is now opt-in (default False), excludes only the exact verified
    # executable, records what it added, and removes exactly those when the
    # user turns the setting back off. See app.elevation.
    "add_defender_exclusion": False,
    "defender_exclusions_applied": [],
    "defender_migration_notice_shown": False,
    # Startup tray (Phase 5): boot hidden with --tray at logon. Same
    # additive-merge + junk-coercion contract as its neighbors.
    "startup_tray_enabled": False,
    # Close to tray: window X hides to the tray instead of quitting.
    # Default True preserves the existing behavior for upgrades.
    "close_to_tray_enabled": True,
    # H7: also undeclared. has_seen_tip/mark_tip_seen read and write it with
    # a local default, so a non-dict value here made every tip reappear
    # forever instead of being ignored.
    "seen_tips": {},
}

# H7: the caps are schema constraints, so they live in app/config_schema.py
# with the rules that apply them. Re-exported here because the writers below
# still enforce them on the way OUT, and both ends must agree on the number.
from app.config_schema import (  # noqa: E402
    PROFILE_ITEMS_MAX as _PROFILE_ITEMS_MAX,
    PROFILE_MAX as _PROFILE_MAX,
    RUN_HISTORY_MAX as _RUN_HISTORY_MAX,
    STARTUP_MAX as _STARTUP_MAX,
    apply_schema,
    validate_schema,
)

# Phase 2 (#12): mapping used to migrate configs saved by the old 5-tab UI.
# Games-tab twins -> the Clean-tab task that cleans the identical paths
# (see tab_presets.GAMES_TO_CLEAN_DEDUPE); unique Games tasks move to
# Clean as-is. Advanced tasks move to Tweak; the three cut tasks
# (punch-list #13) are dropped, not migrated.
# F3-4: the rules are tab_presets' PUBLIC constants — imported lazily in
# _migrate_selected_tasks below, never duplicated here (a private copy
# could drift out of sync with the tabs). Lazy because config_persist is
# imported by tweak_tasks and tab_presets imports tweak_tasks, so a
# module-level import would be circular.


def _legacy_key_resolves(key: str) -> "bool | None":
    """True/False when the live task tables can decide whether `key` is a
    real task; None when the tables can't be imported (import-time
    circularity guard — config_persist must stay import-light)."""
    try:
        from app.tasks import clean_tasks, repair_tasks, tweak_tasks, game_tasks, advanced_tasks
        known = ({t.key for t in clean_tasks.TASKS}
                 | {t.key for t in game_tasks.TASKS}
                 | {t.key for t in repair_tasks.TASKS}
                 | {t.key for t in tweak_tasks.TASKS}
                 | {t.key for t in advanced_tasks.TASKS})
        return key in known
    except Exception:
        return None


def _migrate_selected_tasks(data: dict) -> dict:
    """Fold legacy 5-tab selected_tasks into the 3-tab layout, in place."""
    st = data.get("selected_tasks")
    if not isinstance(st, dict):
        return data
    # F3-4: the migration rules are the SAME public constants tab_presets
    # uses to build the live tabs — one source of truth, no private copies.
    # H2: they now come from the leaf module `app.task_keys`, which
    # `app.tab_presets` itself imports, so this edge no longer closes the
    # utils -> config_persist -> tab_presets -> tasks -> utils cycle. The
    # import stays function-local to keep config_persist import-light.
    from app.task_keys import CUT_TASK_KEYS as _cut_keys, GAMES_TO_CLEAN_DEDUPE as _dedupe_map
    clean = st.setdefault("Clean", [])
    tweak = st.setdefault("Tweak", [])
    # audit fix (probe-confirmed): a hand-edited config could store a task
    # list as a plain string ("gamer_launchers") — iterating it scattered
    # per-character keys ['g','a','m','e','r',...] into the list. Coerce
    # every tab's list to an actual list of strings first (H8: Repair was
    # missing, so a Repair string still scattered).
    for tab_name in ("Clean", "Repair", "Tweak"):
        if isinstance(st.get(tab_name), str):
            st[tab_name] = [st[tab_name]]
    # F-6 audit fix: 'Game' is the pre-release singular spelling of the old
    # Games tab (observed in a live config) — fold it exactly like 'Games'.
    # 'Install' keys were persisted by an older GUI but resolve in no tab
    # the scheduler knows; keep them (scheduler tolerates unknown keys)
    # unless the live task tables say the key is dead — then drop the cruft
    # instead of silently carrying it forever. When the tables cannot be
    # imported (import-time migration), leave the list untouched — the
    # scheduler's tolerant resolver keeps behavior identical to before.
    if "Game" in st and "Games" not in st:
        st["Games"] = st.pop("Game")
    elif "Game" in st:
        merged = st.pop("Game") or []
        existing = st.get("Games") or []
        st["Games"] = list(existing) + [k for k in merged if k not in existing]
    for legacy_tab in ("Games", "Advanced"):
        keys = st.pop(legacy_tab, []) or []
        if isinstance(keys, str):
            keys = [keys]
        if legacy_tab == "Games":
            for key in keys:
                mapped = _dedupe_map.get(key, key)
                if mapped not in clean:
                    clean.append(mapped)
        else:
            for key in keys:
                if key in _cut_keys:
                    continue
                if key not in tweak:
                    tweak.append(key)
    if "Install" in st:
        kept = []
        for key in st.pop("Install") or []:
            if isinstance(key, str) and key:
                resolves = _legacy_key_resolves(key)
                if resolves is not False:
                    kept.append(key)
        # Keys that resolve (or can't be checked — import-time migration)
        # go to Tweak so a live scheduler can still run them via its
        # Advanced-alias table; keys the live task tables say are dead are
        # dropped instead of silently carried forever.
        for key in kept:
            if key not in tweak:
                tweak.append(key)
    return data

# In-memory cache
_config_cache: dict | None = None
_config_lock = threading.RLock()
_config_mtime: float | None = None


def _disk_mtime():
    try:
        return os.path.getmtime(CONFIG_FILE)
    except OSError:
        return None


def _restore_from_bak() -> "dict | None":
    """MED-013: the UX-001 comment promised "a later quarantine can restore
    applied_tweaks / snapshots instead of wiping undo history" - but nothing
    ever read the .bak. This does.

    Only used when the primary is genuinely CORRUPT (not merely unreadable),
    and the backup must itself parse. Returns the recovered dict, or None.
    """
    bak = CONFIG_FILE.with_suffix(CONFIG_FILE.suffix + ".bak")
    try:
        if not bak.exists():
            return None
        with open(bak, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return None
        apply_schema(data, DEFAULT_CONFIG)
        _migrate_selected_tasks(data)
        return data
    except Exception:
        return None


def _quarantine_corrupt_config(exc: Exception) -> None:
    """Move an unreadable/corrupt config.json aside so its contents are
    not lost and the next save doesn't silently overwrite the only copy.
    Best-effort: a failed rename must never crash startup. A visible
    warning goes to stderr (config_persist is imported by every entry
    point including the headless scheduler; GUI users see the effects as
    a reset to defaults, hence the console note).

    REL-002 fix: stderr is null in the windowed frozen exe (and pythonw),
    so on a GUI run this warning was completely invisible — the user just
    saw their tweak badges/selections silently reset with zero
    explanation. A small sibling marker file is now also written; GUI
    startup calls consume_quarantine_notice() once to surface the same
    message in the visible run log, then deletes the marker."""
    target = None
    try:
        from datetime import datetime
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        target = CONFIG_FILE.with_name(f"config.json.corrupt-{stamp}")
        os.replace(CONFIG_FILE, target)
    except Exception:
        target = None
    where = f"quarantined to {target}" if target else "could NOT be moved aside (keeping it in place)"
    message = (f"Settings were reset to defaults because the saved file was "
               f"corrupt ({type(exc).__name__}: {exc}) — the original was "
               f"{where}.")
    try:
        marker = CONFIG_FILE.with_name("config.json.quarantine-notice")
        with open(marker, "w", encoding="utf-8") as f:
            f.write(message)
    except Exception:
        pass
    log_security_event("config_quarantine", message)
    # M15: sys.stderr can be None/closed in pythonw or the windowed frozen
    # exe — an unguarded print would raise out of the corrupt-config handler
    # and crash startup on exactly the case it was meant to survive.
    try:
        stream = sys.stderr if getattr(sys, "stderr", None) is not None else None
        if stream is None:
            return
        print(f"[CleanerTool] config_persist: {CONFIG_FILE} is corrupt or unreadable "
              f"({type(exc).__name__}: {exc}) — {where}. Starting with defaults.", file=stream)
    except Exception:
        pass


def consume_quarantine_notice() -> "str | None":
    """One-shot read of the quarantine marker (REL-002): returns the
    honest reset explanation and deletes the marker, or None if nothing
    was quarantined since the last check. Call once from GUI startup and
    log the result — never raises."""
    try:
        marker = CONFIG_FILE.with_name("config.json.quarantine-notice")
        if not marker.exists():
            return None
        try:
            with open(marker, "r", encoding="utf-8") as f:
                message = f.read().strip()
        except Exception:
            message = None
        try:
            marker.unlink()
        except Exception:
            pass
        return message or None
    except Exception:
        return None


def _load_config_from_disk() -> dict:
    """Load config from disk (internal use). No side-effect dir creation for read-only queries."""
    if CONFIG_FILE.exists():
        try:
            # LOW-007: json.load() parses the WHOLE file into memory, so a
            # multi-GB config.json (truncated write, runaway append, someone
            # copying a huge file into place) is fully materialised before a
            # single key is validated. update_checker already caps its input
            # this way; the config reader should too. Refusing an oversized
            # file is also the correct response rather than trying to parse it.
            try:
                size = CONFIG_FILE.stat().st_size
            except OSError:
                size = None
            if size is not None and size > _MAX_CONFIG_BYTES:
                raise ValueError(
                    f"config.json is {size} bytes, over the "
                    f"{_MAX_CONFIG_BYTES}-byte limit - refusing to parse it")
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, dict):
                raise ValueError("config.json root is not a JSON object")
            # H7: validation is declared, not hand-written. Every key gets
            # either its stored value or its DEFAULT_CONFIG default, and
            # anything not in the schema is left untouched so a config written
            # by a NEWER build does not lose data. This replaces ~130 lines of
            # per-key isinstance checks whose rules, caps and defaults had
            # drifted into three places; see app/config_schema.py.
            apply_schema(data, DEFAULT_CONFIG)
            # The legacy 5-tab -> 3-tab fold needs app.tab_presets, which
            # imports app.tweak_tasks, which imports this module. So it runs
            # here, immediately after selected_tasks has been coerced to have
            # all three tabs, rather than inside the schema.
            _migrate_selected_tasks(data)
            return data
        except FileNotFoundError:
            pass  # file vanished between exists() and open() — treat as missing
        except (json.JSONDecodeError, ValueError) as exc:
            # GENUINELY CORRUPT: the bytes are there but the body is not a
            # config. This is the only case that should quarantine.
            #
            # MED-013: before quarantining, try the last-good .bak that
            # save_config has been maintaining all along. Wiping a user's
            # tweak_snapshots - the entire undo history - to recover from one
            # truncated write is a terrible trade when a known-good copy is
            # sitting right next to it.
            recovered = _restore_from_bak()
            if recovered is not None:
                log_security_event(
                    "config_recovered",
                    f"config.json was unreadable ({type(exc).__name__}); "
                    "restored the last known-good backup instead of resetting "
                    "to defaults.")
                return recovered
            _quarantine_corrupt_config(exc)
        except OSError as exc:
            # MED-013: NOT corrupt - merely UNREADABLE RIGHT NOW. A
            # PermissionError, an AV sharing violation, a file locked by
            # another process, a transient I/O error: none of these mean the
            # bytes are bad. The old code funnelled all of them into
            # _quarantine_corrupt_config, which meant a momentary lock
            # produced a false "config_quarantine" entry in the app's own
            # SECURITY EVENT LOG, wiped every tweak badge in the UI, and then
            # had the next update_config OVERWRITE the real config.json with
            # defaults - destroying the tweak_snapshots undo registry.
            #
            # Serve the cached copy if we have one and leave the file alone.
            cached = _config_cache
            if isinstance(cached, dict) and cached:
                return copy.deepcopy(cached)
            log_security_event(
                "config_unreadable",
                f"config.json could not be read ({type(exc).__name__}: {exc}); "
                "it was left in place and defaults are in use for this "
                "session. It is NOT corrupt and was not reset.")
            return copy.deepcopy(DEFAULT_CONFIG)
        except Exception as exc:
            # Anything else (a schema bug, a migration failure) is treated as
            # corrupt, but we still prefer the backup.
            recovered = _restore_from_bak()
            if recovered is not None:
                log_security_event(
                    "config_recovered",
                    f"config.json failed to load ({type(exc).__name__}: {exc}); "
                    "restored the last known-good backup.")
                return recovered
            _quarantine_corrupt_config(exc)
    return copy.deepcopy(DEFAULT_CONFIG)


def load_config() -> dict:
    """Load config, using cached version if available. Returns a fresh copy so caller mutations never touch the cache without save_config. Thread-safe."""
    global _config_cache, _config_mtime
    with _config_lock:
        # F08: re-stat the file — a long-lived GUI session must see
        # external changes (scheduler, elevated twin process, hand edits)
        # instead of serving a stale cache forever.
        if _config_cache is not None:
            try:
                mt = _disk_mtime()
                if mt is None or (_config_mtime is not None and mt != _config_mtime):
                    if mt is None and not CONFIG_FILE.exists():
                        return copy.deepcopy(_config_cache)
                    _config_cache = _load_config_from_disk()
                    _config_mtime = _disk_mtime()
            except Exception:
                pass
            return copy.deepcopy(_config_cache)
        _config_cache = _load_config_from_disk()
        _config_mtime = _disk_mtime()
        return copy.deepcopy(_config_cache)


def _prune_stale_config_temps(max_age_s: int = 3600) -> int:
    """LOW-008: remove leftover config.json.<pid>.<token>.tmp files.

    save_config writes to a unique temp name and then os.replace()s it, so a
    healthy run leaves nothing behind. A crash or a kill between the write and
    the replace leaves one, and nothing ever cleaned them up - they accumulate
    in the config directory forever. Only files older than `max_age_s` are
    removed, so a temp belonging to a write happening right now (from the GUI
    or a concurrent --auto-clean) is never touched.
    """
    import time
    removed = 0
    try:
        now = time.time()
        prefix = CONFIG_FILE.name + "."
        for entry in CONFIG_FILE.parent.iterdir():
            if not entry.name.startswith(prefix) or not entry.name.endswith(".tmp"):
                continue
            try:
                if now - entry.stat().st_mtime < max_age_s:
                    continue
                entry.unlink()
                removed += 1
            except OSError:
                pass
    except Exception:
        pass
    return removed


def save_config(config: dict) -> None:
    """Save config to disk atomically (write to temp then replace). Thread-safe. Raises on failure."""
    global _config_cache, _config_mtime
    # (audit fix: the old `global` statement also named _config_dirty, a
    # variable that never existed anywhere else — removed.)
    with _config_lock:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        # audit fix (H9): a fixed name (config.json.tmp) let the GUI process
        # and a concurrent --auto-clean/--auto-update process interleave —
        # A writes its tmp, B overwrites the same tmp path, then A's
        # os.replace wins with B's bytes (or vice versa), silently dropping
        # whichever write lost the race. Make the tmp name unique per
        # writer (pid + a random token) so two processes never touch the
        # same file; only the final os.replace (atomic on the same volume)
        # decides what lands in config.json.
        import secrets
    tmp = CONFIG_FILE.with_name(f"{CONFIG_FILE.name}.{os.getpid()}.{secrets.token_hex(4)}.tmp")
    # LOW-008: json.dump() raising (a TypeError from a non-serializable value
    # reaches here, because it escapes the `with` BEFORE the os.replace
    # try/except below) used to leave the temp file on disk forever, and
    # nothing ever pruned config.json.*.tmp. The dump is wrapped so a failure
    # removes its own temp file, and a cheap sweep clears any left by a
    # previous crash.
    _prune_stale_config_temps()
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(config, f, indent=2)
            try:
                f.flush()
                os.fsync(f.fileno())
            except Exception:
                pass
    except Exception:
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except OSError:
            pass
        raise
    # UX-001: keep a single last-good backup so a later quarantine
    # can restore applied_tweaks / snapshots instead of wiping undo history.
    try:
        if CONFIG_FILE.exists():
            bak = CONFIG_FILE.with_suffix(CONFIG_FILE.suffix + ".bak")
            import shutil
            shutil.copy2(CONFIG_FILE, bak)
    except Exception:
        pass
    # Atomic replace
    try:
        os.replace(tmp, CONFIG_FILE)
    except Exception as e:
        # Cleanup temp on failure and propagate
        try:
            if tmp.exists():
                tmp.unlink()
        except Exception:
            pass
        raise

    # The file on disk now holds `config`, so refresh the cache HERE.
    #
    # This block used to sit AFTER the `raise` above, which made it
    # unreachable: `_config_cache` and `_config_mtime` were never updated on
    # a successful write. Two consequences, both observed in production:
    #
    #   1. `_config_mtime` kept pointing at the pre-write file, so
    #      load_config() took the mismatch branch on the very next read and
    #      re-ran `_load_config_from_disk` -- and therefore apply_schema --
    #      after EVERY save rather than only at startup. That is what made
    #      the _tweak_snapshots validator's data loss fire on every apply
    #      instead of only at restart.
    #   2. Anything that read the config back before the mtime granularity
    #      caught up could be served the previous contents.
    #
    # deepcopy, not the caller's dict: callers mutate the object they passed
    # in, and the cache must not alias it.
    _config_cache = copy.deepcopy(config)
    _config_mtime = _disk_mtime()


def update_config(mutate) -> dict:
    """Atomically load, mutate, and save the config under a single lock hold.

    audit fix (H9): every read-modify-write helper below used to call
    load_config() (acquire lock, copy, release) and save_config() (acquire
    lock again) as two separate steps, with the lock released in between.
    The GUI's main thread and its worker thread call these concurrently
    (e.g. mark_tweak_applied after a run finishes while another tweak's
    apply/revert is snapshotting), so a load-mutate-save on one thread
    could be clobbered by the other's save landing in between — a lost
    update. `mutate` receives the live config dict and mutates it in
    place (or returns a replacement); the load, mutate, and save all
    happen while _config_lock is held, so no other config read/write can
    interleave. Returns the config actually saved.
    """
    with _config_lock:
        cfg = load_config()
        result = mutate(cfg)
        if result is not None:
            cfg = result
        save_config(cfg)
        return cfg


# --------------------------------------------------------------------------- #
# Tweak snapshot helpers — real pre-tweak values for accurate revert
# --------------------------------------------------------------------------- #

def get_tweak_snapshot(task_id: str) -> dict:
    """Return the saved pre-apply values for a tweak task, or {} if none."""
    cfg = load_config()
    return cfg.get("tweak_snapshots", {}).get(task_id, {})


def save_tweak_snapshot(task_id: str, values: dict) -> None:
    """Save pre-apply values for a tweak task, but only if none exist yet —
    if the tweak is applied again while an unreverted snapshot is already
    on file, keep the ORIGINAL pre-tweak value, not the value from the last
    apply (which would just re-snapshot the tweak's own output)."""
    if not values:
        return

    def _mutate(cfg):
        snapshots = cfg.setdefault("tweak_snapshots", {})
        if task_id in snapshots and snapshots[task_id]:
            return None
        snapshots[task_id] = values

    update_config(_mutate)


def merge_tweak_snapshot(task_id: str, values: dict) -> None:
    """Merge `values` into an existing snapshot under a SINGLE lock hold.

    Some applies record their snapshot in two stages: the service start type
    first, then the per-scheduled-task priors a moment later. The second write
    must not erase the first, which is why `save_tweak_snapshot` refuses to
    overwrite — but a read-then-clear-then-save sequence does that by hand,
    and it is NOT atomic:

        existing = get_tweak_snapshot(task_id)   # lock acquired + released
        clear_tweak_snapshot(task_id)            # lock acquired + released
        save_tweak_snapshot(task_id, existing)    # lock acquired + released

    Between the clear and the save the snapshot does not exist. A crash, a
    concurrent --auto-clean process, or a second writer in that window
    destroys the task's ENTIRE undo history — the one thing the user cannot
    get back. update_config holds _config_lock across load-mutate-save
    precisely to close that class of hole, so this is one call.

    Fails closed: if the mutation cannot be applied the existing snapshot is
    left exactly as it was.
    """
    if not values:
        return

    def _mutate(cfg):
        snapshots = cfg.setdefault("tweak_snapshots", {})
        existing = snapshots.get(task_id)
        merged = dict(existing) if isinstance(existing, dict) else {}
        merged.update(values)
        snapshots[task_id] = merged

    update_config(_mutate)


def clear_tweak_snapshot(task_id: str) -> None:
    """Drop a tweak's saved snapshot after a successful revert, so the next
    apply starts capturing fresh again."""
    def _mutate(cfg):
        snapshots = cfg.get("tweak_snapshots", {})
        if task_id in snapshots:
            del snapshots[task_id]

    update_config(_mutate)


# --------------------------------------------------------------------------- #
# Applied-tweak registry — Phase 2 (#14)
# --------------------------------------------------------------------------- #
# The "Applied" badge must show what is ACTUALLY ACTIVE on this PC, not the
# toggle's on/off state. Two sources imply "applied":
#   * tweak_snapshots — powercfg tweaks snapshot their prior value on apply
#     (only max_performance_gpu / max_cpu_power use this today), and the
#     snapshot is cleared on successful revert;
#   * applied_tweaks — the explicit registry the GUI marks after a tweak
#     run succeeds and clears after its revert succeeds.
# get_tweak_state() merges both in a single config load.

def mark_tweak_applied(task_id: str) -> None:
    def _mutate(cfg):
        applied = cfg.setdefault("applied_tweaks", [])
        if task_id not in applied:
            applied.append(task_id)

    update_config(_mutate)


def mark_tweak_reverted(task_id: str) -> None:
    def _mutate(cfg):
        applied = cfg.get("applied_tweaks", [])
        if task_id in applied:
            applied.remove(task_id)

    update_config(_mutate)


def get_tweak_state() -> dict:
    """One-shot read of which tweak ids are currently applied. Returns
    {task_id: True}. Kept as a plain dict (not a set) so callers can cheaply
    re-read after a run; loads config once, not per tweak."""
    cfg = load_config()
    state = {tid: True for tid in cfg.get("tweak_snapshots", {})}
    state.update({tid: True for tid in cfg.get("applied_tweaks", [])})
    return state


# --------------------------------------------------------------------------- #
# Feature 10 — "How is my PC doing?" run history
# --------------------------------------------------------------------------- #

def record_run(bytes_freed: int) -> None:
    """Append one finished Clean run to the rolling history.

    Called on the Tk thread right after a run lands. Best-effort: a
    failure here must never disturb the run's own outcome, so every
    error is swallowed (same contract as mark_tweak_applied)."""
    try:
        n = int(bytes_freed or 0)
    except (TypeError, ValueError):
        return
    if n <= 0:
        return          # nothing freed = nothing worth remembering
    try:
        import time as _time
        stamp = _time.time()

        def _mut(cfg):
            hist = cfg.get("run_history")
            if not isinstance(hist, list):
                hist = []
            hist.append({"t": stamp, "b": n})
            cfg["run_history"] = hist[-_RUN_HISTORY_MAX:]

        update_config(_mut)
    except Exception:
        pass


def has_seen_tip(tip_id: str) -> bool:
    """One-time-tip flags (audit fix: Auto-Maintenance discoverability —
    the scheduler only lives behind a small corner icon; a one-time tip
    after the first successful Clean run points new users at it). Generic
    by tip_id so any future one-time tip can reuse this instead of adding
    a dedicated config field each time."""
    try:
        return bool(load_config().get("seen_tips", {}).get(tip_id))
    except Exception:
        return True  # fail closed: never nag on a read error


def mark_tip_seen(tip_id: str) -> None:
    try:
        def _mut(cfg):
            seen = cfg.get("seen_tips")
            if not isinstance(seen, dict):
                seen = {}
            seen[tip_id] = True
            cfg["seen_tips"] = seen
        update_config(_mut)
    except Exception:
        pass


def freed_since(days: int = 30) -> "tuple[int, int]":
    """(total_bytes_freed, run_count) over the last `days` days.

    Returns (0, 0) when there is no history yet — the caller then shows
    no sentence at all rather than a zero."""
    try:
        import time as _time
        cutoff = _time.time() - (max(1, int(days)) * 86400)
        total, runs = 0, 0
        for entry in load_config().get("run_history", []) or []:
            try:
                if float(entry.get("t", 0)) >= cutoff:
                    total += int(entry.get("b", 0))
                    runs += 1
            except (TypeError, ValueError):
                continue
        return total, runs
    except Exception:
        return 0, 0


# --------------------------------------------------------------------------- #
# Feature 7 — Install profiles ("My Setups")
# --------------------------------------------------------------------------- #

def get_install_profiles() -> dict:
    """{name: [package ids]} — always a plain dict, never None."""
    try:
        profiles = load_config().get("install_profiles")
        return dict(profiles) if isinstance(profiles, dict) else {}
    except Exception:
        return {}


def save_install_profile(name: str, ids: "list[str]") -> bool:
    """Create or overwrite one profile. True when it was stored.

    Single-lock read-modify-write (H9 contract) so a save from the dialog
    can't clobber a concurrent one."""
    try:
        name = str(name or "").strip()[:60]
        if not name:
            return False
        clean = [str(i) for i in (ids or []) if str(i).strip()][:_PROFILE_ITEMS_MAX]

        def _mut(cfg):
            profiles = cfg.get("install_profiles")
            if not isinstance(profiles, dict):
                profiles = {}
            if name not in profiles and len(profiles) >= _PROFILE_MAX:
                # full: drop nothing silently — refuse instead (the dialog
                # tells the user to delete one first)
                raise ValueError("profile limit reached")
            profiles[name] = clean
            cfg["install_profiles"] = profiles

        update_config(_mut)
        return True
    except Exception:
        return False


def delete_install_profile(name: str) -> bool:
    """Remove one profile. True when something was removed."""
    try:
        name = str(name or "").strip()
        if not name:
            return False
        removed = {"hit": False}

        def _mut(cfg):
            profiles = cfg.get("install_profiles")
            if isinstance(profiles, dict) and name in profiles:
                profiles.pop(name, None)
                cfg["install_profiles"] = profiles
                removed["hit"] = True

        update_config(_mut)
        return removed["hit"]
    except Exception:
        return False


# --------------------------------------------------------------------------- #
# Feature 9 — Game Night state
# --------------------------------------------------------------------------- #

def get_game_night() -> dict:
    """{"active": bool, "keys": [str], "close_apps": [str]}."""
    try:
        cfg = load_config()
        return {
            "active": bool(cfg.get("game_night_active", False)),
            "keys": list(cfg.get("game_night_keys", []) or []),
            "close_apps": list(cfg.get("game_night_close_apps", []) or []),
        }
    except Exception:
        return {"active": False, "keys": [], "close_apps": []}


def set_game_night(active: bool, keys=None) -> None:
    """Record that Game Night started (with the keys it turned on) or
    ended. Single-lock RMW, same contract as mark_tweak_applied."""
    try:
        want = bool(active)
        klist = [str(k) for k in (keys or []) if str(k).strip()][:200]

        def _mut(cfg):
            cfg["game_night_active"] = want
            cfg["game_night_keys"] = klist if want else []

        update_config(_mut)
    except Exception:
        pass


def set_game_night_close_apps(keys) -> None:
    """Persist which optional background apps the user ticked."""
    try:
        klist = [str(k) for k in (keys or []) if str(k).strip()][:50]
        update_config(lambda cfg: cfg.__setitem__("game_night_close_apps", klist))
    except Exception:
        pass


# --------------------------------------------------------------------------- #
# Startup Manager
# --------------------------------------------------------------------------- #

def get_startup_disabled() -> list:
    """[{source, name, command, value_type}] for every item the user has
    disabled — the data needed to put each one back exactly as it was."""
    try:
        items = load_config().get("startup_disabled")
        return list(items) if isinstance(items, list) else []
    except Exception:
        return []


def add_startup_disabled(record: dict) -> bool:
    """Record one newly-disabled item. True when stored.

    Single-lock RMW (H9 contract): the caller has already made the live
    change (removed the registry value / moved the shortcut) by the time
    this is called, so losing the race here would strand that change
    with no way back — the lock makes that not happen."""
    try:
        source = str(record.get("source", "")).strip()
        name = str(record.get("name", "")).strip()[:200]
        command = str(record.get("command", ""))[:4096]
        value_type = record.get("value_type", "")
        value_type = value_type if isinstance(value_type, str) else ""
        if not source or not name:
            return False
        # BUG-016: held_name was dropped here. This function rebuilds the
        # record from an explicit whitelist and held_name was not on it, so
        # startup_manager's careful os.path.basename(dest) never reached the
        # config -- SEC-002 was silently defeated and EVERY restore fell
        # through to the legacy guess-the-extension path, which can then
        # hard-fail on the tamper check. Carry it through.
        held_name = str(record.get("held_name", "")).strip()
        clean = {"source": source, "name": name, "command": command,
                 "value_type": value_type}
        if held_name:
            clean["held_name"] = os.path.basename(held_name)[:200]

        def _mut(cfg):
            items = cfg.get("startup_disabled")
            if not isinstance(items, list):
                items = []
            items = [r for r in items
                     if not (r.get("source") == source and r.get("name") == name)]
            items.append(clean)
            # BUG-015: `clean` was appended and the list then truncated from
            # the END, so once the log was full the record just written was
            # discarded immediately. The caller has ALREADY removed the
            # registry value / moved the shortcut by this point, so that
            # stranded a live change with no undo -- the exact outcome the
            # single-lock docstring above exists to prevent. This is an undo
            # log, so keep the newest.
            if len(items) > _STARTUP_MAX:
                items = items[-_STARTUP_MAX:]
            cfg["startup_disabled"] = items

        update_config(_mut)
        return True
    except Exception:
        return False


def remove_startup_disabled(source: str, name: str) -> bool:
    """Drop one record once its item has been re-enabled. True when
    something was actually removed."""
    try:
        removed = {"hit": False}

        def _mut(cfg):
            items = cfg.get("startup_disabled")
            if not isinstance(items, list):
                return
            kept = [r for r in items
                    if not (r.get("source") == source and r.get("name") == name)]
            if len(kept) != len(items):
                removed["hit"] = True
            cfg["startup_disabled"] = kept

        update_config(_mut)
        return removed["hit"]
    except Exception:
        return False
