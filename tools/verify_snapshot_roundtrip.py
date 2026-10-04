"""Every tweak snapshot must survive a config.json round trip.

Run:  python -u tools/verify_snapshot_roundtrip.py
Exit 0 = clean, 1 = at least one failure.

Why this exists. `apply_schema` runs on EVERY config load
(app/config_persist.py:383). The `_tweak_snapshots` validator used to keep
only flat scalars for snapshots that carry no "specs" key, so on the very
next load it deleted:

    stop_telemetry   -> "task_priors"  (the whole per-task undo record)
    nvidia_telemetry -> "task_priors"
    gaming_dns       -> "adapters"     -> revert then RAISED, unundoable
    eee_disable      -> "adapters"     -> revert then RAISED, unundoable
    refresh_rate_fix -> "mode"         -> entire snapshot gone

Five of the 82 tweaks lost their undo history. Worse, the loss fired after
every SAVE rather than only at startup, because the cache-refresh block in
save_config sat after a `raise` and was therefore unreachable.

Nothing caught it: verify_config_schema only asserted that an empty
registry stayed empty, and verify_med009_telemetry_snapshot never
mentioned `_tweak_snapshots` at all.

This test writes each real snapshot shape through the REAL save/load path
against a temp config, then asserts every key came back. It also pins the
rejections that must keep happening, so "preserve more" cannot quietly
become "accept anything".
"""

from __future__ import annotations

import copy
import io
import json
import os
import pathlib
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

FAILS = []


def ck(cond, label, detail=""):
    if cond:
        print("  PASS  %s" % label)
    else:
        FAILS.append(label)
        print("  FAIL  %s%s" % (label, ("  <- " + str(detail)) if detail else ""))


# The shapes tweak_tasks.py actually writes, keyed by the task id that owns
# them. Every one of these was silently destroyed before the fix.
REAL_SHAPES = {
    "stop_telemetry": {"task_priors": {
        "\\Microsoft\\Windows\\Application Experience\\Microsoft Compatibility Appraiser": True,
        "\\Microsoft\\Windows\\Application Experience\\ProgramDataUpdater": False}},
    "nvidia_telemetry": {
        "NvTelemetryContainer_start_type": "2",
        "task_priors": {"\\NVIDIA\\NvTelemetryContainer": True}},
    "gaming_dns": {"adapters": {"Ethernet": {
        "servers": ["1.1.1.1", "1.0.0.1"], "dnssec": False}}},
    "eee_disable": {"adapters": {"eth0": {"eee": False, "prior": True}}},
    "refresh_rate_fix": {"mode": [1920, 1080, 60, 32]},
    "usb_suspend": {"2a737441-1930-4402-8d77-b2bebbaa308a3:0:4": 2},
    "max_performance_gpu": {"sub_processor:PROCTHROTTLEMAX": 99},
    "ultimate_performance": {"active_scheme": "{e9a42e02-e50f-4fbf-a865-5f48c51a2b1f}"},
    "max_cpu_power": {"sub_processor:PROCTHROTTLEMIN:0": 100},
    # the _snap_reg_values shape, which takes the "specs" branch
    "file_extensions": {
        "specs": [["HKCU", "Software\\Microsoft\\Windows\\CurrentVersion\\Explorer\\Advanced",
                   "HideFileExt"]],
        "0:type": "REG_DWORD",
        "0:present": True,
        "0:value": 1},
    "some_binary_thing": {"0:__bytes_hex__": "deadbeef"},
}


def _flatten(value, prefix=""):
    """Every leaf key path in a nested structure, for exact comparison."""
    out = set()
    if isinstance(value, dict):
        for k, v in value.items():
            out |= _flatten(v, "%s.%s" % (prefix, k))
    elif isinstance(value, list):
        out.add("%s[]" % prefix)
    else:
        out.add(prefix)
    return out


def _reset_config_module():
    from app import config_persist
    return config_persist


print("SNAPSHOT ROUND-TRIP VERIFY")

# ------------------------------------------------------- real save/load path --
print("\n[1] every real snapshot shape survives save_config -> load_config")
cfgmod = _reset_config_module()
real_file = cfgmod.CONFIG_FILE
probe = pathlib.Path(tempfile.gettempdir()) / "snapshot_roundtrip_probe.json"
try:
    cfgmod.CONFIG_FILE = probe
    cfgmod._config_cache = None

    from app.config_persist import (
        clear_tweak_snapshot, get_tweak_snapshot, save_tweak_snapshot,
    )

    for task_id, shape in REAL_SHAPES.items():
        try:
            save_tweak_snapshot(task_id, copy.deepcopy(shape))
            # Force a genuine re-read from disk -- this is the exact path
            # that used to drop the nested values.
            cfgmod._config_cache = None
            got = get_tweak_snapshot(task_id)
            want = _flatten(shape)
            have = _flatten(got) if isinstance(got, dict) else set()
            missing = sorted(want - have)
            ck(not missing and isinstance(got, dict),
               "%-22s round-trips (%d leaf keys)" % (task_id, len(want)),
               "missing %s" % (missing,))
        except Exception as exc:                # noqa: BLE001
            ck(False, "%s round-trips" % task_id, repr(exc))
        finally:
            try:
                clear_tweak_snapshot(task_id)
            except Exception:                   # noqa: BLE001
                pass
finally:
    cfgmod.CONFIG_FILE = real_file
    cfgmod._config_cache = None
    try:
        probe.unlink()
    except OSError:
        pass

# ----------------------------------------------- the cache must not go stale --
print("\n[2] a just-written snapshot reads back without a stale cache")
cfgmod = _reset_config_module()
real_file = cfgmod.CONFIG_FILE
probe = pathlib.Path(tempfile.gettempdir()) / "snapshot_cache_probe.json"
try:
    cfgmod.CONFIG_FILE = probe
    cfgmod._config_cache = None
    from app.config_persist import get_tweak_snapshot, save_tweak_snapshot

    save_tweak_snapshot("cache_probe", {"task_priors": {"\\Some\\Task": True}})
    # Deliberately do NOT clear the cache: save_config must have refreshed
    # it. Before the fix its refresh block sat after a `raise`, so this read
    # returned the pre-write contents.
    back = get_tweak_snapshot("cache_probe")
    ck(isinstance(back, dict) and back.get("task_priors") == {"\\Some\\Task": True},
       "read-after-write sees the new value", back)
    ck(cfgmod._config_mtime == cfgmod._disk_mtime(),
       "save_config refreshed _config_mtime (so apply_schema does not re-run "
       "on every subsequent read)")
finally:
    cfgmod.CONFIG_FILE = real_file
    cfgmod._config_cache = None
    try:
        probe.unlink()
    except OSError:
        pass

# ------------------------------------------------------------- still rejected --
print("\n[3] junk is still rejected (preserve more != accept anything)")
from app.config_schema import apply_schema  # noqa: E402

base = {"tweak_snapshots": {}}


def survives(snapshot):
    out = apply_schema({"tweak_snapshots": {"t": snapshot}}, base)
    return out.get("tweak_snapshots", {}).get("t")


ck(survives({"k": "v"}) == {"k": "v"}, "a plain scalar survives")

# a nesting bomb must be cut, not followed forever
bomb = cur = {}
for _ in range(40):
    cur["n"] = {}
    cur = cur["n"]
ck(isinstance(survives(bomb), (dict, type(None))),
   "a 40-deep nesting bomb does not crash the validator")
ck(survives(bomb) is None or len(json.dumps(survives(bomb))) < 4096,
   "the bomb is bounded in size",
   len(json.dumps(survives(bomb))) if survives(bomb) is not None else "dropped")

# non-JSON types must not survive
ck(survives({"k": {1, 2, 3}}) in (None, {}), "a set value is dropped")
ck(survives({"k": object()}) in (None, {}), "an arbitrary object is dropped")
ck(survives({"k": float("nan")}) in (None, {}), "NaN is dropped (invalid JSON)")
ck(survives({"k": float("inf")}) in (None, {}), "Infinity is dropped")
ck(survives({1: "v"}) in (None, {}), "a non-str dict key is dropped")
# a malformed REG_BINARY envelope is still refused. The real shape (see
# _snap_reg_values, tweak_tasks.py:3344) is a nested dict under "<i>:value"
# whose inner "__bytes_hex__" must be a str.
ck(survives({"0:value": {"__bytes_hex__": 12345}}) in (None, {}),
   "a non-str __bytes_hex__ envelope is dropped")
ck(survives({"0:value": {"__bytes_hex__": "deadbeef"}})
   == {"0:value": {"__bytes_hex__": "deadbeef"}},
   "a well-formed __bytes_hex__ envelope is kept")

# the malformed-specs rejection must be untouched
ck(survives({"specs": []}) is None, "an empty specs list is dropped")
ck(survives({"specs": [["HKCU", "p"]]}) is None, "a short spec tuple is dropped")
ck(survives({"specs": [["NOPE", "p", "n"]]}) is None, "a bad hive is dropped")
ck(survives({"specs": [["HKCU", "p\x00x", "n"]]}) is None,
   "a NUL in the path is dropped")

print("\n[4] the real registry is still reachable and intact")
cfgmod = _reset_config_module()
try:
    from app.tasks.tweak_tasks import TASKS
    from app.config_persist import DEFAULT_CONFIG, get_tweak_state

    ck(len(TASKS) == len({t.key for t in TASKS}),
       "TASKS has no duplicate keys", len(TASKS))
    no_revert = [t.key for t in TASKS if t.revert is None]
    ck(isinstance(get_tweak_state(), dict), "get_tweak_state returns a dict")
    ck("tweak_snapshots" in DEFAULT_CONFIG,
       "DEFAULT_CONFIG declares tweak_snapshots")
    print("        (%d tasks, %d without a revert: %s)"
          % (len(TASKS), len(no_revert), ", ".join(no_revert) or "none"))
except Exception as exc:                        # noqa: BLE001
    ck(False, "registry imports", repr(exc))

print()
if FAILS:
    print("SNAPSHOT ROUND-TRIP VERIFY: %d FAILURE(S)" % len(FAILS))
    sys.exit(1)
print("SNAPSHOT ROUND-TRIP VERIFY: ALL PASS (%d shapes)"
      % len(REAL_SHAPES))
sys.exit(0)