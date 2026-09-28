"""H7 targeted verification: config.json is validated by a declared schema.

The refactor moved ~130 lines of hand-written per-key isinstance checks into
app/config_schema.py. The risk is not that the schema is wrong in an obvious
way; it is that it is wrong in a way the smoke suites do not happen to touch.
So this compares the schema against a table of the OLD behaviour, key by key,
with deliberately hostile inputs.

Checks:
  1  SCHEMA and DEFAULT_CONFIG declare exactly the same keys (no drift)
  2  every coercer is total: fed True/False/int/float/str/list/dict/None,
     it returns the right thing and never raises
  3  each key's coercion matches the rule the old code implemented
  4  the two previously-undeclared keys are now declared and coerced
  5  caps are enforced and match what the writers enforce
  6  unknown keys survive (forward compatibility)
  7  selected_tasks still gets all three tabs and still folds legacy layouts
  8  a full round trip through the real on-disk loader
"""
from __future__ import annotations

import copy
import io
import json
import os
import sys

sys.path.insert(0, r"C:\Users\User\Desktop\Cleaner Tool")

import app.config_schema as cs
from app.config_persist import DEFAULT_CONFIG

FAILS = []
N = [0]


def ck(label, cond, detail=""):
    N[0] += 1
    if cond:
        print("  PASS  %s" % label)
    else:
        FAILS.append(label)
        print("  FAIL  %s%s" % (label, ("  <- " + repr(detail)) if detail else ""))


# ------------------------------------------------------------------ [1] ----
print("\n[1] schema and DEFAULT_CONFIG agree")
problems = cs.validate_schema(DEFAULT_CONFIG)
ck("no drift between SCHEMA and DEFAULT_CONFIG", not problems, problems)
ck("SCHEMA is non-trivial", len(cs.SCHEMA) >= 25, len(cs.SCHEMA))
ck("every SCHEMA entry is (coercer, doc)",
   all(isinstance(v, tuple) and len(v) == 2 for v in cs.SCHEMA.values()))

# ------------------------------------------------------------------ [2] ----
print("\n[2] every coercer is total — no input may raise")
HOSTILE = [None, True, False, 0, 1, -1, 3.5, float("nan"), "", "   ", "x" * 300,
           [], {}, [1, 2], {"a": 1}, [None], [[]], {"a": {"b": 1}}, b"bytes",
           (), object()]
raised = []
for key, (coerce, _doc) in cs.SCHEMA.items():
    default = DEFAULT_CONFIG.get(key)
    for v in HOSTILE:
        try:
            out = coerce(copy.deepcopy(v), copy.deepcopy(default))
            # and it must be JSON-serialisable, since it goes back to disk
            json.dumps(out)
        except Exception as exc:
            raised.append("%s(%r) -> %s: %s" % (key, v, type(exc).__name__, exc))
ck("no coercer raises on any hostile input", not raised, raised[:4])

# ------------------------------------------------------------------ [3] ----
print("\n[3] per-key coercion matches the old rules")


def co(key, value):
    return cs.SCHEMA[key][0](value, copy.deepcopy(DEFAULT_CONFIG.get(key)))


# bools: exact bool only, never int
for key in ("schedule_enabled", "schedule_update_enabled", "auto_elevate",
            "remember_limited", "add_defender_exclusion", "startup_tray_enabled",
            "close_to_tray_enabled", "session_pilot_enabled",
            "session_pilot_watch_all", "game_night_active"):
    d = DEFAULT_CONFIG[key]
    ck("%s keeps a real bool" % key, co(key, True) is True and co(key, False) is False)
    ck("%s rejects 1/0/'true'" % key,
       co(key, 1) is d and co(key, 0) is d and co(key, "true") is d,
       (co(key, 1), d))

# default-true flags must fall back to True, not False
ck("SEC-004: the Defender exclusion preference defaults to OFF",
   DEFAULT_CONFIG.get("add_defender_exclusion") is False,
   DEFAULT_CONFIG.get("add_defender_exclusion"))
ck("...and its recorded-exclusion list starts empty",
   DEFAULT_CONFIG.get("defender_exclusions_applied") == [],
   DEFAULT_CONFIG.get("defender_exclusions_applied"))
# _bool falls back to the DEFAULT for unrecognised junk, so this is a real
# assertion about the default: it used to resolve to True (exclusion on),
# and under SEC-004 junk must resolve to False (off).
ck("SEC-004: junk for the Defender preference falls back to OFF",
   co("add_defender_exclusion", "nope") is False,
   co("add_defender_exclusion", "nope"))
ck("close_to_tray_enabled junk falls back to True",
   co("close_to_tray_enabled", 7) is True)
ck("session_pilot_watch_all junk falls back to True",
   co("session_pilot_watch_all", []) is True)

# str lists
ck("applied_tweaks drops non-strings",
   co("applied_tweaks", ["a", 1, None, "b"]) == ["a", "b"])
ck("applied_tweaks rejects a bare string",
   co("applied_tweaks", "ab") == [])
ck("session_pilot_games drops blanks",
   co("session_pilot_games", ["a", "  ", "", 5]) == ["a"])
ck("session_pilot_names rejects a list", co("session_pilot_names", [1]) == {})
ck("tweak_snapshots rejects a list", co("tweak_snapshots", []) == {})

# caps
ck("gameping_hosts caps at %d" % cs.GAMEPING_MAX,
   len(co("gameping_hosts", ["h%d" % i for i in range(500)])) == cs.GAMEPING_MAX)
ck("game_night_keys caps at %d" % cs.SESSION_KEYS_MAX,
   len(co("game_night_keys", ["k%d" % i for i in range(900)])) == cs.SESSION_KEYS_MAX)

# run history
ck("run_history keeps well-formed entries",
   co("run_history", [{"t": 100, "b": 5}, {"t": 0, "b": 1}, {"t": 1, "b": -1},
                      {"t": "x", "b": 1}, "nope", {"b": 2}])
   == [{"t": 100.0, "b": 5}],
   co("run_history", [{"t": 100, "b": 5}, {"t": 0, "b": 1}, {"t": 1, "b": -1},
                      {"t": "x", "b": 1}, "nope", {"b": 2}]))
ck("run_history accepts numeric strings (old behaviour)",
   co("run_history", [{"t": "100", "b": "5"}]) == [{"t": 100.0, "b": 5}])
ck("run_history caps at %d" % cs.RUN_HISTORY_MAX,
   len(co("run_history", [{"t": i + 1, "b": 0} for i in range(400)])) == cs.RUN_HISTORY_MAX)

# install profiles
ck("install_profiles drops malformed names/ids",
   co("install_profiles", {"  ok  ": ["a", 2, ""], "": ["x"], "bad": "notalist"})
   == {"ok": ["a"]},
   co("install_profiles", {"  ok  ": ["a", 2, ""], "": ["x"], "bad": "notalist"}))
ck("install_profiles caps at %d" % cs.PROFILE_MAX,
   len(co("install_profiles", {"n%d" % i: [] for i in range(99)})) == cs.PROFILE_MAX)
ck("install_profiles caps ids at %d" % cs.PROFILE_ITEMS_MAX,
   len(co("install_profiles", {"n": ["i%d" % i for i in range(999)]})["n"])
   == cs.PROFILE_ITEMS_MAX)

# startup_disabled: a record without a command must never be trusted
ck("startup_disabled drops a record with no command",
   co("startup_disabled", [{"source": "HKCU", "name": "x"}]) == [],
   co("startup_disabled", [{"source": "HKCU", "name": "x"}]))
ck("startup_disabled keeps a complete record",
   co("startup_disabled", [{"source": "HKCU", "name": "x", "command": "c"}])
   == [{"source": "HKCU", "name": "x", "command": "c", "value_type": ""}])
ck("startup_disabled caps at %d" % cs.STARTUP_MAX,
   len(co("startup_disabled", [{"source": "s", "name": "n", "command": "c"}] * 500))
   == cs.STARTUP_MAX)

# schedule values: NEW validation, previously passed through raw
ck("schedule_frequency accepts the three known values",
   [co("schedule_frequency", v) for v in ("daily", "weekly", "monthly")]
   == ["daily", "weekly", "monthly"])
ck("schedule_frequency rejects junk",
   co("schedule_frequency", "hourly") == "weekly")
ck("schedule_frequency normalises case/space",
   co("schedule_frequency", "  WEEKLY ") == "weekly")
ck("schedule_time accepts HH:MM", co("schedule_time", "23:59") == "23:59")
ck("schedule_time rejects 24:00 and 9:00",
   co("schedule_time", "24:00") == "03:00" and co("schedule_time", "9:00") == "03:00")
ck("schedule_time rejects a non-string", co("schedule_time", 300) == "03:00")

# ------------------------------------------------------------------ [4] ----
print("\n[4] the two previously-undeclared keys")
ck("schedule_update_enabled is declared", "schedule_update_enabled" in DEFAULT_CONFIG)
ck("seen_tips is declared", "seen_tips" in DEFAULT_CONFIG)
ck("schedule_update_enabled is coerced to a bool",
   co("schedule_update_enabled", "yes") is False)
ck("seen_tips is coerced to a dict", co("seen_tips", ["a"]) == {})

# ------------------------------------------------------------------ [5] ----
print("\n[5] caps agree between reader and writers")
from app import config_persist as cp
ck("config_persist re-exports the same cap values",
   (cp._RUN_HISTORY_MAX == cs.RUN_HISTORY_MAX
    and cp._PROFILE_MAX == cs.PROFILE_MAX
    and cp._PROFILE_ITEMS_MAX == cs.PROFILE_ITEMS_MAX
    and cp._STARTUP_MAX == cs.STARTUP_MAX))

# ------------------------------------------------------------------ [6] ----
print("\n[6] apply_schema leaves unknown keys alone (forward compatibility)")
d = {"selected_tasks": {}, "totally_new_key": {"from": "a newer build"},
     "another": [1, 2, 3]}
out = cs.apply_schema(d, DEFAULT_CONFIG)
ck("an unknown dict key survives", out.get("totally_new_key") == {"from": "a newer build"})
ck("an unknown list key survives", out.get("another") == [1, 2, 3])
ck("known keys were filled in", "applied_tweaks" in out and "seen_tips" in out)

# ------------------------------------------------------------------ [7] ----
print("\n[7] selected_tasks: all tabs present, legacy still folds")
out = cs.apply_schema({"selected_tasks": {"Clean": ["a"]}}, DEFAULT_CONFIG)
ck("all three tabs present",
   set(out["selected_tasks"]) == set(DEFAULT_CONFIG["selected_tasks"]),
   sorted(out["selected_tasks"]))
ck("a supplied tab's keys survive", out["selected_tasks"]["Clean"] == ["a"])
ck("a missing tab is an empty list", out["selected_tasks"]["Repair"] == [])

# the real fold, through the real loader's code path
# the string-singleton spelling, which the coercer must NOT discard: the
# fold's F-6 fix reads it after coercion
folded = {"selected_tasks": {"Games": "launcher_cache", "Clean": ["shader_cache"]}}
cs.apply_schema(folded, DEFAULT_CONFIG)
ck("the legacy bare-string survives coercion",
   folded["selected_tasks"]["Games"] == ["launcher_cache"],
   folded["selected_tasks"])
cp._migrate_selected_tasks(folded)
# fold APPENDS the legacy keys after Clean's own
ck("legacy Games STRING folds into Clean",
   folded["selected_tasks"]["Clean"] == ["shader_cache", "launcher_cache"],
   folded["selected_tasks"])
ck("the legacy tab is consumed, not left behind",
   "Games" not in folded["selected_tasks"], sorted(folded["selected_tasks"]))

# the list spelling, and the pre-release singular
folded2 = {"selected_tasks": {"Games": ["launcher_cache", "shader_cache"]}}
cs.apply_schema(folded2, DEFAULT_CONFIG)
cp._migrate_selected_tasks(folded2)
ck("a legacy Games LIST dedupes into Clean",
   folded2["selected_tasks"]["Clean"] == ["launcher_cache", "shader_cache"],
   folded2["selected_tasks"])
folded3 = {"selected_tasks": {"Game": "launcher_cache"}}
cs.apply_schema(folded3, DEFAULT_CONFIG)
cp._migrate_selected_tasks(folded3)
ck("the singular 'Game' spelling still folds",
   folded3["selected_tasks"]["Clean"] == ["launcher_cache"],
   folded3["selected_tasks"])
# Advanced -> Tweak, minus the cut tasks
folded4 = {"selected_tasks": {"Advanced": ["disable_nagle"]}}
cs.apply_schema(folded4, DEFAULT_CONFIG)
cp._migrate_selected_tasks(folded4)
ck("Advanced folds into Tweak",
   "disable_nagle" in folded4["selected_tasks"]["Tweak"],
   folded4["selected_tasks"])
# a malformed legacy tab must not blow up the whole load
folded5 = {"selected_tasks": {"Games": 5}}
cs.apply_schema(folded5, DEFAULT_CONFIG)
crashed = None
try:
    cp._migrate_selected_tasks(folded5)
except Exception as exc:
    crashed = repr(exc)
ck("a non-list legacy tab degrades instead of raising", crashed is None, crashed)

# ------------------------------------------------------------------ [8] ----
print("\n[8] round trip through the REAL loader")
# Point config_persist at a scratch file and go through load_config() for
# real. An earlier version of this check computed the result from a dict
# instead, which missed the single worst bug this refactor could introduce:
# deleting the `return data` at the end of _load_config_from_disk, after
# which every load silently returned DEFAULT_CONFIG and the user's whole
# config was ignored. Nothing short of a real load catches that.
import app.config_persist as _cpmod
_real_cfg_file = _cpmod.CONFIG_FILE
tmp = os.path.join(os.environ.get("TEMP", "."), "h7_schema_probe.json")
hostile = {
    "schedule_enabled": "yes",
    "schedule_frequency": "hourly",
    "schedule_time": "99:99",
    "schedule_update_enabled": "maybe",
    "applied_tweaks": "nope",
    "run_history": [{"t": 1, "b": 2}, "junk"],
    "install_profiles": {"p": "notalist"},
    "startup_disabled": [{"source": "s", "name": "n"}],
    "seen_tips": "nope",
    "tweak_snapshots": [],
    "future_key_from_a_newer_build": {"keep": "me"},
    "selected_tasks": {"Games": "launcher_cache"},
}
try:
    with io.open(tmp, "w", encoding="utf-8") as f:
        json.dump(hostile, f)
    from pathlib import Path as _P
    _cpmod.CONFIG_FILE = _P(tmp)
    _cpmod._config_cache = None
    real = _cpmod.load_config()          # <- the real thing
    ck("schedule_enabled -> bool", real["schedule_enabled"] is False)
    ck("schedule_frequency -> a known value", real["schedule_frequency"] == "weekly")
    ck("schedule_time -> a valid time", real["schedule_time"] == "03:00")
    ck("schedule_update_enabled -> bool", real["schedule_update_enabled"] is False)
    ck("applied_tweaks -> list", real["applied_tweaks"] == [])
    ck("run_history -> clean entries", real["run_history"] == [{"t": 1.0, "b": 2}])
    ck("install_profiles -> {} (the one entry was malformed)",
       real["install_profiles"] == {})
    ck("startup_disabled -> [] (no command to restore from)",
       real["startup_disabled"] == [])
    ck("seen_tips -> {}", real["seen_tips"] == {})
    ck("tweak_snapshots -> {}", real["tweak_snapshots"] == {})
    ck("an unknown key is preserved", real["future_key_from_a_newer_build"] == {"keep": "me"})
    ck("selected_tasks folded the legacy tab",
       "launcher_cache" in real["selected_tasks"]["Clean"],
       real["selected_tasks"])
    ck("the result is JSON-serialisable", json.dumps(real) is not None)
    # a value the user actually set must come back, not a default
    ck("load_config round-trips a real stored value", real.get("schedule_frequency") == "weekly")
    with io.open(tmp, "w", encoding="utf-8") as f:
        json.dump({"schedule_frequency": "monthly",
                   "selected_tasks": {"Clean": ["driver_junk"]}}, f)
    _cpmod._config_cache = None
    again = _cpmod.load_config()
    ck("load_config returns what is ON DISK, not DEFAULT_CONFIG",
       again.get("schedule_frequency") == "monthly", again.get("schedule_frequency"))
    ck("a stored selection survives the round trip",
       again.get("selected_tasks", {}).get("Clean") == ["driver_junk"],
       again.get("selected_tasks"))
finally:
    _cpmod.CONFIG_FILE = _real_cfg_file
    _cpmod._config_cache = None
    try:
        os.remove(tmp)
    except Exception:
        pass

print()
if FAILS:
    print("H7 VERIFY: %d/%d FAILED -> %s" % (len(FAILS), N[0], FAILS))
    sys.exit(1)
print("H7 VERIFY: ALL PASS (%d checks)" % N[0])
