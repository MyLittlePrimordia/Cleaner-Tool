"""Declarative schema for config.json.

H7. Before this, `_load_config_from_disk` validated the config with ~130
lines of hand-written `if not isinstance(...)` checks — one per key, in the
order the features landed, with the rules, the caps and the defaults living
in three different places (the check, DEFAULT_CONFIG, and a comment). The
failure mode of that shape is quiet and slow: a new key gets a default but no
validation, and a new cap gets a check in the writer but not in the reader.

Two keys were in exactly that state when this module was written.
`schedule_update_enabled` is written by the scheduler and read by the
Auto-Maint Maintenance dialog, but was never in DEFAULT_CONFIG, so it was
never defaulted and never validated — a hand-edited "yes" is truthy, and the
dialog renders its checkbox on. `seen_tips` is read by `has_seen_tip` with
the same gap. Neither is visible in the old code because nothing declared
what the file is supposed to contain.

So the contract is declared here, once:

  * `DEFAULT_CONFIG` stays the single source of truth for DEFAULT VALUES.
    It is not duplicated. The schema supplies the COERCERS, and reads each
    default back out of DEFAULT_CONFIG by key.
  * One rule for every key: absent, or present with the wrong type, means
    the default. That is exactly what the old per-key code did — every branch
    fell back to DEFAULT_CONFIG — it just said so 25 times.
  * A key with a coercer and no DEFAULT_CONFIG entry is a bug, and
    `validate_schema()` raises. So is the reverse.

Import direction: stdlib only, nothing from `app`. config_persist imports
this; nothing here imports config_persist.
"""
from __future__ import annotations

import re

# --------------------------------------------------------------------------- #
# Caps. These moved here from config_persist because they are schema
# constraints, not persistence details — they bound what a hand-edited config
# can put on disk. config_persist re-exports them for its writers.
# --------------------------------------------------------------------------- #

#: Rolling run history. A friendly summary, not an audit trail.
RUN_HISTORY_MAX = 120
#: Bounds so a hand-edited config can never balloon the file.
PROFILE_MAX = 20
PROFILE_ITEMS_MAX = 400
PROFILE_NAME_MAX = 60
#: Startup Manager: enough records to restore, not a registry dump.
STARTUP_MAX = 200
STARTUP_NAME_MAX = 200
STARTUP_COMMAND_MAX = 4096
#: Game-ping custom targets. Well past any real use; a bound, not a quota.
GAMEPING_MAX = 64
#: Game Night / Session Pilot key lists.
SESSION_KEYS_MAX = 200

#: Tabs the pre-merge UI persisted. Preserved (and normalised) by the
#: selected_tasks coercer so _migrate_selected_tasks can still fold them.
LEGACY_TABS = ("Games", "Game", "Advanced", "Install")

SCHEDULE_FREQUENCIES = ("daily", "weekly", "monthly")
_TIME_RE = re.compile(r"([01]\d|2[0-3]):[0-5]\d")


# --------------------------------------------------------------------------- #
# Coercers. Each takes (value, default) and returns the value to store.
# The contract is total: none of these raise, whatever they are handed.
# --------------------------------------------------------------------------- #

def _bool(value, default):
    """Exact bool check. `isinstance(1, bool)` is False but `isinstance(True,
    int)` is True, so int/float must not be accepted as flags."""
    return value if isinstance(value, bool) else default


def _str(value, default, choices=None, lower=False):
    if not isinstance(value, str):
        return default
    out = value.strip()
    if not out:
        return default
    if lower:
        out = out.lower()
    if choices is not None and out not in choices:
        return default
    return out


def _str_list(value, default, cap=None, nonblank=True):
    if not isinstance(value, list):
        return list(default)
    out = [v for v in value if isinstance(v, str) and (v.strip() or not nonblank)]
    return out[:cap] if cap else out


def _dict(value, default):
    if not isinstance(value, dict):
        return dict(default)
    return value


#: LOW-010: the value types `_snap_reg_values` is allowed to record, and which
#: `_restore_reg_values` will accept back. Kept here so the schema and the
#: restore path agree on one list instead of two.
_SNAPSHOT_VALUE_TYPES = ("REG_DWORD", "REG_SZ", "REG_QWORD", "REG_BINARY",
                        "REG_MULTI_SZ", "REG_EXPAND_SZ")

#: Bounds so a hand-edited or runaway file cannot grow without limit. The app
#: has well under 100 tweaks and the largest snapshot is a handful of values.
_MAX_SNAPSHOT_TASKS = 256
_MAX_SNAPSHOT_SPECS = 64
_MAX_SEEN_TIPS = 256


def _tweak_snapshots(value, default):
    """Validate the tweak_snapshots registry - the UNDO data.

    LOW-010: this was `_dict`, i.e. "is it a dict" and nothing else, even
    though these values are what ~95 tweak reverts replay into the registry.
    config.json lives in the user's profile and is hand-editable, so the shape
    is declared once and enforced on load rather than being rediscovered as a
    confusing failure during an Undo.

    A snapshot that does not match the shape `_snap_reg_values` produces is
    DROPPED, not repaired: a half-understood snapshot is worse than none,
    because a partial restore is a machine in a state nobody chose. Dropping it
    leaves the tweak badged applied, which is the honest outcome - the user
    sees "Active" and can decide.

    The restore path is still defended independently (MED-008/MED-013 added
    fail-closed hive, path, NUL, persistence-location and type checks). This is
    the earlier, cheaper line of defence, not a replacement for it.
    """
    if not isinstance(value, dict):
        return dict(default)
    out = {}
    for task_id, snap in list(value.items())[:_MAX_SNAPSHOT_TASKS]:
        if not isinstance(task_id, str) or not task_id.strip():
            continue
        if not isinstance(snap, dict):
            continue
        specs = snap.get("specs")
        if specs is None:
            # NOT a _snap_reg_values snapshot. Several legitimate shapes have no
            # "specs" key at all and each is validated at its own restore site,
            # which already fails closed:
            #   _snapshot_powercfg_pairs -> {"sub:setting": int, "...:dc": int}
            #   nvidia_telemetry        -> {"NvTelemetryContainer_start_type": str}
            #   max_performance_gpu     -> {"sub_processor:PROCTHROTTLEMAX": int}
            #
            # This branch was WRONG at first: requiring "specs" made the
            # validator DROP every one of those, silently destroying the undo
            # history for USB suspend, GPU max-performance and NVIDIA telemetry
            # on the very next config load. Caught by the BUG-015 test, which
            # asserts a pre-existing snapshot survives a failed re-apply.
            #
            # So: keep the non-specs shape, but still bound it and keep only
            # JSON-ish scalar values.
            clean = {}
            for k, v in list(snap.items())[:_MAX_SNAPSHOT_SPECS]:
                if isinstance(v, dict):
                    if not isinstance(v.get("__bytes_hex__"), str):
                        continue
                elif not isinstance(v, (str, int, float, bool, type(None))):
                    continue
                clean[k] = v
            if clean:
                out[task_id] = clean
            continue
        if not isinstance(specs, (list, tuple)) or not specs:
            continue
        clean_specs = []
        bad = False
        for spec in list(specs)[:_MAX_SNAPSHOT_SPECS]:
            if not isinstance(spec, (list, tuple)) or len(spec) < 3:
                bad = True
                break
            hive, path, name = spec[0], spec[1], spec[2]
            if not (isinstance(hive, str) and hive in ("HKCU", "HKLM", "HKCR")):
                bad = True
                break
            if not (isinstance(path, str) and path
                    and isinstance(name, str) and name):
                bad = True
                break
            if "\x00" in path or "\x00" in name:
                bad = True
                break
            clean_specs.append([hive, path, name])
        if bad:
            continue
        kept = {"specs": clean_specs}
        for i in range(len(clean_specs)):
            # bind the recorded type up front: the value rule below depends on
            # it (a REG_BINARY value MUST be the hex envelope)
            vtype = snap.get("%d:type" % i)
            for suffix, want in (("present", bool), ("denied", bool),
                                 ("type", str), ("value", object)):
                key = "%d:%s" % (i, suffix)
                if key not in snap:
                    continue
                val = snap[key]
                if want is bool:
                    if not isinstance(val, bool):
                        bad = True
                        break
                    kept[key] = val
                elif want is str:
                    if val not in _SNAPSHOT_VALUE_TYPES:
                        bad = True
                        break
                    kept[key] = val
                else:
                    # REG_BINARY priors are stored hex-encoded as a dict
                    # envelope. A REG_BINARY value that is NOT in that
                    # envelope is malformed - the restore path would raise
                    # "corrupt snapshot value" on it - so it is rejected
                    # here rather than kept and rediscovered mid-Undo.
                    if isinstance(val, dict):
                        if not isinstance(val.get("__bytes_hex__"), str):
                            bad = True
                            break
                    elif vtype == "REG_BINARY":
                        bad = True
                        break
                    elif not isinstance(val, (str, int, float, bool, type(None))):
                        bad = True
                        break
                    kept[key] = val
            if bad:
                break
        if bad:
            continue
        out[task_id] = kept
    return out


def _tip_flags(value, default):
    """LOW-010: `seen_tips` grew without bound and its values were unchecked.

    `has_seen_tip` already fails closed on a non-dict, but nothing stopped the
    dict accumulating keys forever, and nothing stopped a value being a string
    or a list. Cap the size and keep only genuine booleans.
    """
    if not isinstance(value, dict):
        return dict(default)
    out = {}
    for tip_id, seen in list(value.items())[-_MAX_SEEN_TIPS:]:
        if isinstance(tip_id, str) and tip_id.strip() and isinstance(seen, bool):
            out[tip_id] = seen
    return out


def _schedule_frequency(value, default):
    """daily/weekly/monthly, case- and space-insensitive.

    The scheduler already refuses to WRITE anything else
    (scheduler._validate_frequency_str raises), so a bad value here can only
    come from a hand edit — and the reader used to pass it straight through
    to the dialog and on to the task builder.
    """
    return _str(value, default, choices=SCHEDULE_FREQUENCIES, lower=True)


def _schedule_time(value, default):
    """HH:MM, 24h. Mirrors scheduler._validate_time_str's rule."""
    out = _str(value, default)
    return out if _TIME_RE.fullmatch(out) else default


def _run_history(value, default):
    """[{t: unix_seconds, b: bytes_freed}], newest last, capped.

    Accepts numeric strings for t/b, matching the previous behaviour, so an
    older hand-edited file still loads.
    """
    if not isinstance(value, list):
        return []
    clean = []
    for entry in value:
        if not isinstance(entry, dict):
            continue
        try:
            t = float(entry.get("t", 0))
            b = int(entry.get("b", 0))
        except (TypeError, ValueError):
            continue
        if t > 0 and b >= 0:
            clean.append({"t": t, "b": b})
    return clean[-RUN_HISTORY_MAX:]


def _install_profiles(value, default):
    """{name: [package ids]} — junk entries dropped, everything bounded."""
    if not isinstance(value, dict):
        return {}
    clean = {}
    for name, ids in list(value.items())[:PROFILE_MAX]:
        if not isinstance(name, str) or not name.strip():
            continue
        if not isinstance(ids, list):
            continue
        clean[name.strip()[:PROFILE_NAME_MAX]] = [
            i for i in ids if isinstance(i, str) and i.strip()
        ][:PROFILE_ITEMS_MAX]
    return clean


def _startup_disabled(value, default):
    """Records the Startup Manager needs to put an item back.

    A malformed record is dropped rather than half-restored: trusting a
    record with no `command` would mean writing an empty value to the user's
    registry on "restore".
    """
    if not isinstance(value, list):
        return []
    clean = []
    for rec in value:
        if not isinstance(rec, dict):
            continue
        src = rec.get("source")
        nm = rec.get("name")
        cmd = rec.get("command")
        if not (isinstance(src, str) and src.strip()
                and isinstance(nm, str) and nm.strip()
                and isinstance(cmd, str)):
            continue
        vtype = rec.get("value_type", "")
        clean.append({
            "source": src.strip(), "name": nm.strip()[:STARTUP_NAME_MAX],
            "command": cmd[:STARTUP_COMMAND_MAX],
            "value_type": vtype if isinstance(vtype, str) else "",
        })
    return clean[:STARTUP_MAX]


def _gameping_hosts(value, default):
    """'host' (ICMP) or 'host:port' (TCP handshake), bounded."""
    return _str_list(value, default, cap=GAMEPING_MAX)


def _game_night_keys(value, default):
    """Tweaks Game Night itself turned on, so End puts back exactly those."""
    return _str_list(value, default, cap=SESSION_KEYS_MAX)


def _legacy_tab_list(value, default):
    """A legacy tab's task keys, keeping the bare-string form INTACT.

    `_migrate_selected_tasks` has an explicit fix for it: a config that
    stored `"Games": "launcher_cache"` is folded as `["launcher_cache"]`
    (audit F-6 — the same per-character scattering that hit the current
    tabs). That fix reads the value AFTER the coercer runs, so this must
    wrap a lone string rather than discard it the way _str_list would.

    Everything else still normalises: a hand-edited `"Games": 5` becomes []
    instead of reaching the fold's `for key in keys` and raising TypeError,
    which the loader's outer handler turned into a quarantine of the user's
    entire config.
    """
    if isinstance(value, str):
        return [value] if value.strip() else []
    return _str_list(value, default)


def _selected_tasks(value, default):
    """Ensure every current tab is present with a real list of strings.

    LEGACY TABS ARE PRESERVED, and that is load-bearing.
    config_persist._migrate_selected_tasks folds the old 5-tab layout by
    `st.pop("Games")` / `st.pop("Advanced")`, so a coercer that rebuilt the
    dict from `default` alone would silently delete the legacy tab before
    the migration could read it — and a user upgrading from the 5-tab build
    would come back to an empty Clean tab. They are normalised here and
    consumed by the fold immediately afterwards.
    """
    if not isinstance(value, dict):
        value = {}
    out = dict(value)
    for tab in default:
        out[tab] = _str_list(value.get(tab), [])
    for tab in LEGACY_TABS:
        if tab in value:
            out[tab] = _legacy_tab_list(value.get(tab), [])
    return out


# --------------------------------------------------------------------------- #
# The schema. key -> (coercer, one-line intent)
# --------------------------------------------------------------------------- #

SCHEMA = {
    # --- schedule ------------------------------------------------------- #
    "schedule_enabled": (_bool, "opt-in; written by the scheduler only"),
    "schedule_update_enabled": (_bool,
                                "separate opt-in for the --auto-update task"),
    "schedule_frequency": (_schedule_frequency, "daily | weekly | monthly"),
    "schedule_time": (_schedule_time, "HH:MM 24h"),

    # --- task selection ------------------------------------------------- #
    "selected_tasks": (_selected_tasks, "per-tab list of task keys; folded "
                       "from the legacy 5-tab layout after coercion"),

    # --- tweak registry (the applied-badge source) ---------------------- #
    "tweak_snapshots": (_tweak_snapshots, "task_id -> snapshot of pre-tweak values; LOW-010: shape-validated because these replay into the registry in ~95 reverts"),
    "applied_tweaks": (_str_list, "task_ids applied and not yet reverted"),

    # --- session pilot -------------------------------------------------- #
    "session_pilot_enabled": (_bool, "watcher may run while the app is open"),
    "session_pilot_games": (_str_list, "extra .exe names (lowercase)"),
    "session_pilot_preset": (_str, "which Tweak preset a session applies"),
    "session_pilot_paths": (_str_list, "picked exe paths, watch-matched by dir"),
    "session_pilot_watch_all": (_bool, "auto-watch every catalogued game"),
    "session_pilot_names": (_dict, "path -> display name; pure UI sugar"),

    # --- misc features -------------------------------------------------- #
    "gameping_hosts": (_gameping_hosts, "'host' or 'host:port' ping targets"),
    "run_history": (_run_history, "rolling [{t, b}] of finished clean runs"),
    "install_profiles": (_install_profiles, "name -> [package ids]"),
    "game_night_active": (_bool, "a Game Night session is in progress"),
    "game_night_keys": (_game_night_keys, "tweaks Game Night itself turned on"),
    "game_night_close_apps": (_game_night_keys, "optional apps to quiet"),
    "startup_disabled": (_startup_disabled, "records to restore disabled items"),

    # --- admin gate / tray ---------------------------------------------- #
    "auto_elevate": (_bool, "'stop asking me': relaunch elevated unattended"),
    "remember_limited": (_bool, "never show the Admin Gate again"),
    "add_defender_exclusion": (_bool, "SEC-004: opt-in (default off); excludes only this app's exact executable, never its folder"),
   "defender_exclusions_applied": (_str_list, "SEC-004: exclusions this app added, so it can remove exactly those when the preference is turned off"),
    "defender_migration_notice_shown": (_bool, "SEC-004: the one-time notice about a folder-wide exclusion left by an older build has been shown"),
    "startup_tray_enabled": (_bool, "boot hidden with --tray"),
    "close_to_tray_enabled": (_bool, "window X hides to the tray"),

    # --- one-shot tips -------------------------------------------------- #
    "seen_tips": (_tip_flags, "tip_id -> True; bounded bool map, read by has_seen_tip"),
}


def schema_keys():
    return set(SCHEMA)


def apply_schema(data, defaults, extra=()):
    """Coerce `data` in place against `defaults` and return it.

    `defaults` is DEFAULT_CONFIG. Every key in SCHEMA gets either its stored
    value or its default; keys not in SCHEMA are left alone, because the file
    is allowed to carry keys this version does not know about (forward
    compatibility — a config written by a newer build must not lose data).

    `extra` is an iterable of (key, coercer) for callers that need a
    key-specific rule this module cannot know about. None today.
    """
    table = dict(SCHEMA)
    for key, coerce in extra:
        table[key] = (coerce, "")
    for key, entry in table.items():
        coerce = entry[0]
        if key not in defaults:
            # A declared key with no default can never be filled in. Treat it
            # as absent rather than inventing a value, and let
            # validate_schema() complain in the test suite.
            if key not in data:
                continue
            data[key] = coerce(data[key], None)
            continue
        data[key] = coerce(data.get(key), defaults[key])
    return data


def validate_schema(defaults):
    """Return a list of drift problems between SCHEMA and DEFAULT_CONFIG.

    Called by tools/verify_config_schema.py. An empty list means the two
    declarations agree, which is the property that makes DEFAULT_CONFIG the
    single source of truth rather than two lists that rot apart.
    """
    problems = []
    for key in sorted(set(SCHEMA) - set(defaults)):
        problems.append("SCHEMA declares %r but DEFAULT_CONFIG has no default "
                        "for it" % key)
    for key in sorted(set(defaults) - set(SCHEMA)):
        problems.append("DEFAULT_CONFIG has %r but SCHEMA declares no coercer, "
                        "so it is defaulted but never validated" % key)
    for key, entry in SCHEMA.items():
        if not callable(entry[0]):
            problems.append("SCHEMA[%r] coercer is not callable" % key)
    return problems
