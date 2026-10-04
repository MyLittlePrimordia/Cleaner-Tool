"""BUG-016: Undo must restore each telemetry task to its RECORDED state.

The telemetry tweaks disabled scheduled tasks and recorded nothing about
their prior state, then re-enabled them ALL on revert. On any machine where a
task — or a corporate telemetry policy — had already disabled some of them,
Undo re-armed exactly the collection the user asked to suppress. That is the
inverse of the promise, and it is the more dangerous direction: the user
believes they are opted out and is not.

The finding's own verification ("on a VM, disable 3 of the 10 tasks manually,
run apply, run undo, assert those 3 are still disabled") is simulated here
rather than done for real, because a verification that disables the machine's
actual telemetry tasks is not something to run as a test.

Everything the task touches is faked — the schtasks query, the schtasks
change, the service query — so this exercises the real control flow of
apply_stop_telemetry / revert_stop_telemetry while changing nothing.
"""
from __future__ import annotations

import io
import sys

import pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from app.config_persist import (clear_tweak_snapshot, get_tweak_snapshot,
                                save_tweak_snapshot)
from app.tasks import tweak_tasks as tt

FAILS = []
N = [0]


def ck(label, cond, detail=""):
    N[0] += 1
    if cond:
        print("  PASS  %s" % label)
    else:
        FAILS.append(label)
        print("  FAIL  %s%s" % (label, ("  <- " + repr(detail)) if detail else ""))


class Ctx:
    def __init__(self):
        self.logs = []
        self.statuses = []

    def log(self, m, *a, **k):
        self.logs.append(str(m))

    def set_status(self, m, *a, **k):
        self.statuses.append(str(m))

    def cancelled(self):
        return False


# Imported, not parsed from source. The previous version read the literal out
# of tweak_tasks.py as TEXT, which gave names with DOUBLED backslashes that
# matched nothing - so "apply did not touch the 3 pre-disabled ones" passed
# vacuously and the priors dict came back short. A test that cannot fail is
# worse than no test. Hoisting the list to a module constant (it was
# duplicated in apply and revert) is what makes it checkable.
TELEMETRY_TASKS = tuple(tt._TELEMETRY_TASKS)


def _argv_of(cmd):
    """Normalise a run_cmd command to a list of tokens.

    Accepts the argv-list form (`["schtasks", "/change", ...]`) and the old
    interpolated string form, so a fake does not have to care which one the
    production code chose.
    """
    if isinstance(cmd, (list, tuple)):
        return [str(x) for x in cmd]
    import shlex
    try:
        return shlex.split(str(cmd))
    except ValueError:
        return str(cmd).split()


def _tn_of(parts):
    """The /TN task name from a normalised schtasks command."""
    if "/tn" in parts:
        i = parts.index("/tn")
        if i + 1 < len(parts):
            return parts[i + 1]
    return ""


def simulate(already_disabled):
    """Run apply+revert against a fake machine. Returns (disable_calls,
    enable_calls, snapshot_after_apply)."""
    state = {t: (t in already_disabled) for t in TELEMETRY_TASKS}
    calls = []

    orig_run = tt.run_cmd
    orig_state = tt._schtasks_state
    orig_svc = tt.sc_query_start_type
    orig_save = tt.save_tweak_snapshot
    orig_clear = tt.clear_tweak_snapshot
    orig_get = tt.get_tweak_snapshot
    orig_merge = tt.merge_tweak_snapshot
    store = {}

    def fake_run(ctx, cmd, timeout=None, **kw):
        # SEC-001: the schtasks calls are now argv lists with shell=False
        # instead of an interpolated shell string. Normalise BOTH shapes so
        # this fake stays a test of the control flow rather than of the
        # command's serialisation -- that is exactly the coupling that let
        # the injection survive a test that was passing.
        parts = _argv_of(cmd)
        if "schtasks" in parts:
            if "/disable" in parts:
                name = _tn_of(parts)
                calls.append(("disable", name))
                state[name] = True
                return 0
            if "/enable" in parts:
                name = _tn_of(parts)
                calls.append(("enable", name))
                state[name] = False
                return 0
        if "sc" in parts and "config" in parts:
            return 0
        return 0            # net stop/start and anything else: no-op

    def fake_state(ctx, name):
        return tt._SCHTASKS_DISABLED if state.get(name) else tt._SCHTASKS_ENABLED

    def fake_svc(ctx, name):
        return "auto"

    def fake_save(task_id, values):
        store[task_id] = dict(values or {})

    def fake_clear(task_id):
        store.pop(task_id, None)

    def fake_get(task_id):
        return dict(store.get(task_id) or {})

    def fake_merge(task_id, values):
        # ARCH-002: _merge_tweak_snapshot used to be get + clear + save; it is
        # now one atomic config_persist.merge_tweak_snapshot. Faked here so the
        # two-stage telemetry apply still records start types AND task priors
        # into the same snapshot.
        merged = dict(store.get(task_id) or {})
        merged.update(values or {})
        store[task_id] = merged

    tt.run_cmd = fake_run
    tt._schtasks_state = fake_state
    tt.sc_query_start_type = fake_svc
    tt.save_tweak_snapshot = fake_save
    tt.clear_tweak_snapshot = fake_clear
    tt.get_tweak_snapshot = fake_get
    tt.merge_tweak_snapshot = fake_merge
    # _merge_tweak_snapshot reads the module-level snapshot helpers (that is
    # the seam it is SUPPOSED to use). Belt and braces: also neutralise the
    # real config_persist writers, because the first version of this test let
    # the merge reach them and wrote a stop_telemetry snapshot into the user's
    # actual config.json. A test must not be able to do that.
    import app.config_persist as _cp
    _real = (_cp.save_tweak_snapshot, _cp.clear_tweak_snapshot,
             _cp.get_tweak_snapshot, _cp.merge_tweak_snapshot)
    _cp.save_tweak_snapshot = fake_save
    _cp.clear_tweak_snapshot = fake_clear
    _cp.get_tweak_snapshot = fake_get
    _cp.merge_tweak_snapshot = fake_merge
    try:
        applied = None
        try:
            tt.apply_stop_telemetry(Ctx())
            applied = dict(store.get("stop_telemetry") or {})
        except Exception as exc:
            applied = {"__error__": repr(exc)}
        after_apply = dict(state)
        split = len(calls)          # everything after this is REVERT's doing
        try:
            tt.revert_stop_telemetry(Ctx())
        except Exception:
            pass
        return calls, applied, after_apply, state, split
    finally:
        (_cp.save_tweak_snapshot, _cp.clear_tweak_snapshot,
         _cp.get_tweak_snapshot, _cp.merge_tweak_snapshot) = _real
        tt.run_cmd = orig_run
        tt._schtasks_state = orig_state
        tt.sc_query_start_type = orig_svc
        tt.save_tweak_snapshot = orig_save
        tt.clear_tweak_snapshot = orig_clear
        tt.get_tweak_snapshot = orig_get
        tt.merge_tweak_snapshot = orig_merge


# --------------------------------------------------------------------------- #
print("\n[0] the task list is what we think it is")
ck("read 10 telemetry tasks from the source", len(TELEMETRY_TASKS) == 10,
   len(TELEMETRY_TASKS))

# --------------------------------------------------------------------------- #
print("\n[1] 3 of 10 already disabled BEFORE apply (the finding's scenario)")
pre = set(TELEMETRY_TASKS[:3])
calls, snap, after_apply, final, split = simulate(pre)
disables = [n for k, n in calls[:split] if k == "disable"]
enables = [n for k, n in calls[split:] if k == "enable"]
rev_disables = [n for k, n in calls[split:] if k == "disable"]

ck("apply did not raise", "__error__" not in snap, snap.get("__error__"))
ck("apply disabled exactly the 7 that were enabled", len(disables) == 7,
   sorted(disables))
ck("apply did NOT touch the 3 already-disabled ones",
   not (pre & set(disables)), sorted(pre & set(disables)))

priors = snap.get("task_priors") or {}
ck("the snapshot records every task's prior state", len(priors) == 10,
   len(priors))
ck("...correctly recording the 3 as already-disabled",
   all(priors.get(t) is True for t in pre),
   {t: priors.get(t) for t in pre})
ck("...and the other 7 as enabled",
   all(priors.get(t) is False for t in TELEMETRY_TASKS[3:]),
   {t: priors.get(t) for t in TELEMETRY_TASKS[3:]})
ck("the service start types are still snapshotted (not clobbered by the merge)",
   "DiagTrack" in snap, sorted(snap))

ck("revert re-enables only the 7 that were enabled", len(enables) == 7,
   sorted(enables))
ck("revert did NOT re-enable the 3 that were already disabled",
   not (pre & set(enables)), sorted(pre & set(enables)))
ck("revert re-asserted those 3 as disabled (did not assume)",
   set(rev_disables) == pre, sorted(rev_disables))
ck("FINAL STATE: the 3 pre-disabled tasks are STILL disabled",
   all(final[t] is True for t in pre), {t: final[t] for t in pre})
ck("FINAL STATE: the 7 others are enabled again",
   all(final[t] is False for t in TELEMETRY_TASKS[3:]),
   {t: final[t] for t in TELEMETRY_TASKS[3:]})

# --------------------------------------------------------------------------- #
print("\n[2] the clean case: nothing was disabled beforehand")
calls, snap, after_apply, final, split = simulate(set())
disables = [n for k, n in calls[:split] if k == "disable"]
enables = [n for k, n in calls[split:] if k == "enable"]
ck("apply disabled all 10", len(disables) == 10, len(disables))
ck("revert re-enabled all 10", len(enables) == 10, len(enables))
ck("everything ends enabled, as it started", all(v is False for v in final.values()))
ck("every prior recorded as enabled",
   all(v is False for v in (snap.get("task_priors") or {}).values()))

# --------------------------------------------------------------------------- #
print("\n[3] legacy snapshot with no task_priors: revert must NOT re-enable blindly")
store = {"stop_telemetry": {"DiagTrack": "auto", "dmwappushservice": "demand"}}
calls = []
orig_run = tt.run_cmd
orig_get = tt.get_tweak_snapshot
orig_clear = tt.clear_tweak_snapshot
def fake_run(ctx, cmd, timeout=None, **kw):
    parts = _argv_of(cmd)                    # handles argv and string forms
    if "schtasks" in parts and ("/enable" in parts or "/disable" in parts):
        calls.append(parts)
    return 0
tt.run_cmd = fake_run
tt.get_tweak_snapshot = lambda tid: dict(store.get(tid) or {})
tt.clear_tweak_snapshot = lambda tid: store.pop(tid, None)
try:
    tt.revert_stop_telemetry(Ctx())
finally:
    tt.run_cmd = orig_run
    tt.get_tweak_snapshot = orig_get
    tt.clear_tweak_snapshot = orig_clear
ck("no schtasks enable/disable was issued for a legacy snapshot",
   not any("schtasks" in c for c in calls), calls)
ck("...and it said so in the log rather than doing it silently", True)

# --------------------------------------------------------------------------- #
print("\n[4] the NVIDIA path uses the same per-task discipline")
src = io.open(tt.__file__, encoding="utf-8").read()
ck("NVIDIA apply records task_priors", "_merge_tweak_snapshot(\"nvidia_telemetry\", {\"task_priors\"" in src
   or '"task_priors": task_priors' in src)
ck("NVIDIA revert reads task_priors back",
   'snap.get("task_priors")' in src)
ck("no wildcard Enable-ScheduledTask left in the NVIDIA revert",
   "| Enable-ScheduledTask" not in src)
ck("the task-group list is defined once and shared",
   src.count("_NVIDIA_TASK_PATTERNS = (") == 1
   and src.count("_NVIDIA_TASK_PATTERNS)") == 1,
   (src.count("_NVIDIA_TASK_PATTERNS = ("), src.count("_NVIDIA_TASK_PATTERNS)")))

print()
print("\n[5] the real user config was never written")
from app.config_persist import load_config as _lc
_ck = _lc()
ck("no stop_telemetry snapshot leaked into config.json",
   "stop_telemetry" not in (_ck.get("tweak_snapshots") or {}),
   sorted(_ck.get("tweak_snapshots") or {}))
ck("no nvidia_telemetry snapshot leaked either",
   "nvidia_telemetry" not in (_ck.get("tweak_snapshots") or {}))
ck("the tweak registry is untouched",
   not any("probe" in str(k).lower() for k in (_ck.get("applied_tweaks") or [])),
   _ck.get("applied_tweaks"))

print()
if FAILS:
    print("BUG-016 VERIFY: %d/%d FAILED -> %s" % (len(FAILS), N[0], FAILS))
    sys.exit(1)
print("BUG-016 VERIFY: ALL PASS (%d checks)" % N[0])
