"""
Config persistence for scheduled runs and user preferences.
"""

import copy
import json
import os
import sys
import threading
from pathlib import Path

def _get_config_dir() -> Path:
    base = os.environ.get("LOCALAPPDATA", "")
    if not base or not os.path.isdir(base):
        # Fallback to home or temp if LOCALAPPDATA missing (service / test context)
        base = os.path.expanduser("~") or os.environ.get("TEMP", "") or "."
    return Path(base) / "CleanerTool"

CONFIG_DIR = _get_config_dir()
CONFIG_FILE = CONFIG_DIR / "config.json"

DEFAULT_CONFIG = {
    "schedule_enabled": False,
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
}

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
    # Lazy import: config_persist must stay import-light (it is imported by
    # tweak_tasks, and tab_presets imports tweak_tasks).
    from app.tab_presets import CUT_TASK_KEYS as _cut_keys, GAMES_TO_CLEAN_DEDUPE as _dedupe_map
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


def _quarantine_corrupt_config(exc: Exception) -> None:
    """Move an unreadable/corrupt config.json aside so its contents are
    not lost and the next save doesn't silently overwrite the only copy.
    Best-effort: a failed rename must never crash startup. A visible
    warning goes to stderr (config_persist is imported by every entry
    point including the headless scheduler; GUI users see the effects as
    a reset to defaults, hence the console note)."""
    target = None
    try:
        from datetime import datetime
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        target = CONFIG_FILE.with_name(f"config.json.corrupt-{stamp}")
        os.replace(CONFIG_FILE, target)
    except Exception:
        target = None
    where = f"quarantined to {target}" if target else "could NOT be moved aside (keeping it in place)"
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


def _load_config_from_disk() -> dict:
    """Load config from disk (internal use). No side-effect dir creation for read-only queries."""
    if CONFIG_FILE.exists():
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, dict):
                raise ValueError("config.json root is not a JSON object")
            # Merge with defaults for any missing keys (deep merge for selected_tasks)
            for k, v in DEFAULT_CONFIG.items():
                if k not in data:
                    data[k] = copy.deepcopy(v)
                elif isinstance(v, dict) and isinstance(data[k], dict):
                    for sub_k, sub_v in v.items():
                        data[k].setdefault(sub_k, copy.deepcopy(sub_v))
            # Ensure selected_tasks has all tabs
            if "selected_tasks" in data and isinstance(data["selected_tasks"], dict):
                for tab in DEFAULT_CONFIG["selected_tasks"]:
                    data["selected_tasks"].setdefault(tab, [])
                _migrate_selected_tasks(data)
            # H8/H9: validate the tweak-state containers — a hand-edited
            # string here would scatter per-character badge keys in
            # get_tweak_state(). Coerce junk back to empty defaults.
            if not isinstance(data.get("applied_tweaks"), list):
                data["applied_tweaks"] = []
            else:
                data["applied_tweaks"] = [t for t in data["applied_tweaks"] if isinstance(t, str)]
            # Session Pilot (feature 6): same hand-edited-config hazards —
            # a string here would scatter per-character exe names into the
            # watchlist; coerce junk back to defaults.
            if not isinstance(data.get("session_pilot_games"), list):
                data["session_pilot_games"] = []
            else:
                data["session_pilot_games"] = [
                    g for g in data["session_pilot_games"]
                    if isinstance(g, str) and g.strip()]
            if not isinstance(data.get("session_pilot_preset"), str):
                data["session_pilot_preset"] = copy.deepcopy(
                    DEFAULT_CONFIG["session_pilot_preset"])
            if not isinstance(data.get("session_pilot_enabled"), bool):
                data["session_pilot_enabled"] = False
            # Phase-5 keys: same coercion contract (strings scatter;
            # junk degrades to defaults, never raises)
            if not isinstance(data.get("session_pilot_paths"), list):
                data["session_pilot_paths"] = []
            else:
                data["session_pilot_paths"] = [
                    p for p in data["session_pilot_paths"]
                    if isinstance(p, str) and p.strip()]
            if "session_pilot_watch_all" in data:
                # explicit user choice (or legacy default) — validate only
                if not isinstance(data["session_pilot_watch_all"], bool):
                    data["session_pilot_watch_all"] = bool(
                        DEFAULT_CONFIG["session_pilot_watch_all"])
            else:
                # key absent = brand-new install (or never touched pilot):
                # the new-config default applies (True — see DEFAULT_CONFIG)
                data["session_pilot_watch_all"] = copy.deepcopy(
                    DEFAULT_CONFIG["session_pilot_watch_all"])
            if not isinstance(data.get("session_pilot_names"), dict):
                data["session_pilot_names"] = {}
            if not isinstance(data.get("gameping_hosts"), list):
                data["gameping_hosts"] = []
            else:
                data["gameping_hosts"] = [
                    h for h in data["gameping_hosts"]
                    if isinstance(h, str) and h.strip()][:64]
            if not isinstance(data.get("tweak_snapshots"), dict):
                data["tweak_snapshots"] = {}
            return data
        except FileNotFoundError:
            pass  # file vanished between exists() and open() — treat as missing
        except Exception as exc:
            # M5 audit fix: this swallowed EVERY exception (incl. a corrupt
            # JSON body) and silently returned defaults, wiping the user's
            # applied-tweak registry / snapshots / selections in memory.
            # Only a genuinely missing file returns defaults silently; a
            # corrupt file is quarantined (rename aside) with a warning.
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
        # Write to temp
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(config, f, indent=2)
            try:
                f.flush()
                os.fsync(f.fileno())
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
        # Directory fsync for durability (Windows: ensure directory entry flushed)
        try:
            # On Windows, opening directory for fsync is not supported; ignore
            dir_fd = os.open(str(CONFIG_DIR), os.O_RDONLY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        except Exception:
            pass
        # Store a deepcopy to avoid aliasing with caller's mutable dict
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