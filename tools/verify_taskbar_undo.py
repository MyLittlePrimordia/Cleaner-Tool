"""BUG-017: Undo of "Remove Taskbar Junk" must not enable what it disabled.

The bug, two halves, both confirmed in app/tasks/tweak_tasks.py:

  1. `revert_taskbar_cleanup` wrote a HARDCODED 1 for all four HKCU values and
     blind-deleted HideSCAMeetNow. The Windows default for
     ShowSyncProviderNotifications and IsDynamicSearchBoxEnabled is 0/absent,
     so Undo turned Explorer sync-provider ads ON for a user who never had
     them on - the exact opposite of the change they were undoing.
  2. It never raised: `if ok == 0: ctx.log(...)` was followed by an
     unconditional `ctx.log("Taskbar items restored.")`, so a total write
     failure still reported success.

The fix routes both directions through the existing `_snap_reg_values` /
`_restore_reg_values` idiom (~30 sibling tweaks already do this), and makes
`_restore_reg_values` return a count so "restored nothing" is reportable.

No real registry or real config is touched: every winreg and config-persist
entry point is faked, and the config writer seam is neutralised in
app.config_persist as well as in tweak_tasks, because the helpers use LOCAL
`from app.config_persist import ...` statements that module-level patching
does not reach.
"""

from __future__ import annotations

import sys

sys.path.insert(0, ".")

import app.config_persist as cp          # noqa: E402
import app.utils as u                     # noqa: E402
from app.tasks import tweak_tasks as tt   # noqa: E402

FAILS = 0
CHECKS = 0


def ck(label, cond, detail=""):
    global FAILS, CHECKS
    CHECKS += 1
    if cond:
        print("  PASS  %s" % label)
    else:
        FAILS += 1
        print("  FAIL  %s%s" % (label, ("  <- " + str(detail)) if detail else ""))


TASK = "taskbar_cleanup"
ADV = "Software\\Microsoft\\Windows\\CurrentVersion\\Explorer\\Advanced"
SEARCH = "Software\\Microsoft\\Windows\\CurrentVersion\\SearchSettings"
POL = "Software\\Microsoft\\Windows\\CurrentVersion\\Policies\\Explorer"

ADS = ("HKCU", ADV, "ShowSyncProviderNotifications")
DYN = ("HKCU", SEARCH, "IsDynamicSearchBoxEnabled")
WID = ("HKCU", ADV, "TaskbarDa")
MN = ("HKCU", ADV, "TaskbarMn")
MEET = ("HKLM", POL, "HideSCAMeetNow")
ALLFIVE = (WID, MN, ADS, DYN, MEET)


class Ctx:
    def __init__(self):
        self.lines = []

    def log(self, m, *a, **k):
        self.lines.append(str(m))

    def cancelled(self):
        return False

    def set_status(self, *a, **k):
        pass


class Harness:
    """In-memory registry + snapshot store, with every seam recorded."""

    def __init__(self, is_win11=True, denied=frozenset(), set_fails=False,
                 set_checked_fails=False, delete_fails=False):
        self.reg = {}                 # (hive, path, name) -> value
        self.denied = set(denied)     # reads that come back unreadable
        self.is_win11 = is_win11
        self.set_fails = set_fails
        self.set_checked_fails = set_checked_fails
        self.delete_fails = delete_fails
        self.snap = {}                # task_id -> snapshot dict
        self.writes = []              # every successful set, in order
        self.deletes = []
        self.saved = []

    # ---- the fake registry -------------------------------------------------
    def get_typed(self, ctx, hive, path, name):
        key = (hive, path, name)
        if key in self.denied:
            return u._REG_DENIED, None
        if key in self.reg:
            return self.reg[key], "REG_DWORD"
        return None, None

    def set(self, ctx, hive, path, name, value, value_type="REG_DWORD"):
        if self.set_fails:
            return False
        self.reg[(hive, path, name)] = value
        self.writes.append(((hive, path, name), value))
        return True

    def set_checked(self, ctx, hive, path, name, value, value_type="REG_DWORD"):
        if self.set_checked_fails:
            raise RuntimeError("simulated SetValueEx failure")
        self.reg[(hive, path, name)] = value
        self.writes.append(((hive, path, name), value))

    def delete(self, ctx, hive, path, name):
        if self.delete_fails:
            return False
        self.reg.pop((hive, path, name), None)
        self.deletes.append((hive, path, name))
        return True

    # ---- the fake config ---------------------------------------------------
    def save_snap(self, task_id, values):
        # Must mirror app.config_persist.save_tweak_snapshot's real guard:
        # "only if none exist yet ... keep the ORIGINAL pre-tweak value, not
        # the value from the last apply (which would just re-snapshot the
        # tweak's own output)". A fake that overwrote unconditionally would
        # make a correct apply look like it destroys the prior snapshot - and
        # would have hidden the real re-apply behaviour entirely.
        if not values:
            return
        self.saved.append(task_id)
        if self.snap.get(task_id):
            return
        self.snap[task_id] = dict(values)

    def get_snap(self, task_id):
        return self.snap.get(task_id)

    def clear_snap(self, task_id):
        self.snap.pop(task_id, None)

    # ---- patching ----------------------------------------------------------
    def __enter__(self):
        self._saved = {
            "u_reg_set": u.reg_set_value,
            "u_get_typed": u.reg_get_value_typed,
            "tt_set_checked": tt.reg_set_value_checked,
            "tt_delete": tt.reg_delete_value,
            "tt_save": tt.save_tweak_snapshot,
            "tt_get": tt.get_tweak_snapshot,
            "tt_clear": tt.clear_tweak_snapshot,
            "tt_win11": tt._is_win11_or_newer,
            "cp_save": cp.save_tweak_snapshot,
            "cp_get": cp.get_tweak_snapshot,
            "cp_clear": cp.clear_tweak_snapshot,
        }
        u.reg_set_value = self.set
        u.reg_get_value_typed = self.get_typed
        tt.reg_set_value_checked = self.set_checked
        tt.reg_delete_value = self.delete
        tt.save_tweak_snapshot = self.save_snap
        tt.get_tweak_snapshot = self.get_snap
        tt.clear_tweak_snapshot = self.clear_snap
        tt._is_win11_or_newer = lambda: self.is_win11
        # the helpers use LOCAL `from app.config_persist import ...`, so
        # patching only tweak_tasks would let them reach the REAL config
        cp.save_tweak_snapshot = self.save_snap
        cp.get_tweak_snapshot = self.get_snap
        cp.clear_tweak_snapshot = self.clear_snap
        return self

    def __exit__(self, *exc):
        for name, val in self._saved.items():
            if name.startswith("u_"):
                setattr(u, name[2:], val)
            elif name.startswith("cp_"):
                setattr(cp, name[3:], val)
            else:
                setattr(tt, name[3:], val)
        return False


def run(h, fn, *a):
    ctx = Ctx()
    try:
        fn(ctx, *a)
        return "ok", ctx
    except Exception as exc:
        return exc, ctx


print("[1] THE FINDING: a default install. All five values ABSENT before apply.")
with Harness() as h:
    st, _ = run(h, tt.apply_taskbar_cleanup)
    ck("apply succeeded", isinstance(st, str), st)
    ck("a snapshot was recorded", TASK in h.snap, sorted(h.snap))
    ck("the snapshot covers all five values",
       len(h.snap.get(TASK, {}).get("specs", [])) == 5,
       h.snap.get(TASK, {}).get("specs"))
    ck("every prior was recorded as ABSENT (not a guessed default)",
       all(h.snap[TASK].get("%d:present" % i) is False
           for i in range(len(h.snap[TASK]["specs"]))),
       {k: v for k, v in h.snap[TASK].items() if k.endswith(":present")})
    ck("apply actually wrote the four HKCU keys",
       all(k in h.reg for k in (WID, MN, ADS, DYN)), sorted(h.reg))

    # phase boundary: everything recorded from here on is the REVERT's doing
    _mark_w, _mark_d = len(h.writes), len(h.deletes)
    st, ctx = run(h, tt.revert_taskbar_cleanup)
    # Slice AFTER the revert, not before: computed up front these are empty
    # by construction, which made "the revert wrote nothing" pass vacuously.
    revert_writes = h.writes[_mark_w:]
    revert_deletes = h.deletes[_mark_d:]
    ck("revert succeeded", isinstance(st, str), st)
    ck("ShowSyncProviderNotifications is ABSENT again, not 1",
       ADS not in h.reg, h.reg.get(ADS))
    ck("IsDynamicSearchBoxEnabled is ABSENT again, not 1",
       DYN not in h.reg, h.reg.get(DYN))
    ck("TaskbarDa is ABSENT again, not 1", WID not in h.reg, h.reg.get(WID))
    ck("TaskbarMn is ABSENT again, not 1", MN not in h.reg, h.reg.get(MN))
    ck("HideSCAMeetNow is ABSENT again", MEET not in h.reg, h.reg.get(MEET))
    ck("the whole registry is back to empty (nothing was invented)",
       h.reg == {}, h.reg)
    # Count only what the REVERT did. The first version of this check looked at
    # h.writes[-5:], which here is the APPLY's own writes (HideSCAMeetNow=1 is
    # apply's target value), so it asserted against the wrong phase - and would
    # have failed for a correct fix.
    ck("the revert performed NO writes at all (it deleted back to absent)",
       revert_writes == [], revert_writes)
    ck("the revert deleted exactly the five values apply created",
       set(revert_deletes) == set(ALLFIVE),
       [k[2] for k in revert_deletes])
    # This is the improvement over the old revert, which blind-deleted only
    # HideSCAMeetNow and WROTE 1 for the other four. All five priors were
    # absent, so all five must go back to absent - net zero.
    ck("every restored value went back to absent, none became 1",
       h.reg == {} and all(k not in h.reg for k in ALLFIVE), h.reg)
    ck("the snapshot was cleared after a successful revert", TASK not in h.snap)

print()
print("[2] the user HAD ads on: Undo must put them back ON, not off")
with Harness() as h:
    h.reg[ADS] = 1
    h.reg[DYN] = 1
    h.reg[MEET] = 0
    run(h, tt.apply_taskbar_cleanup)
    ck("apply turned the ads off", h.reg.get(ADS) == 0, h.reg.get(ADS))
    st, _ = run(h, tt.revert_taskbar_cleanup)
    ck("revert succeeded", isinstance(st, str), st)
    ck("ShowSyncProviderNotifications is back to 1 (their prior value)",
       h.reg.get(ADS) == 1, h.reg.get(ADS))
    ck("IsDynamicSearchBoxEnabled is back to 1", h.reg.get(DYN) == 1, h.reg.get(DYN))
    ck("HideSCAMeetNow is back to 0, NOT deleted (it existed!)",
       h.reg.get(MEET) == 0 and MEET in h.reg, h.reg.get(MEET))

print()
print("[3] non-default priors survive byte-exact")
with Harness() as h:
    h.reg[WID] = 1
    h.reg[MN] = 0
    h.reg[ADS] = 0
    h.reg[DYN] = 1
    before = dict(h.reg)
    run(h, tt.apply_taskbar_cleanup)
    run(h, tt.revert_taskbar_cleanup)
    for k in (WID, MN, ADS, DYN):
        ck("%s restored to its exact prior value" % k[2],
           h.reg.get(k) == before.get(k), "%r -> %r" % (before.get(k), h.reg.get(k)))

print()
print("[4] a legacy snapshot-less Undo must NOT claim success (half 2)")
with Harness() as h:
    st, ctx = run(h, tt.revert_taskbar_cleanup)
    ck("revert RAISED instead of reporting success", not isinstance(st, str), st)
    ck("the message admits the prior values are unknown",
       "unknown" in str(st).lower() or "no snapshot" in str(st).lower(), st)
    ck("nothing was written to the registry", h.writes == [] and h.deletes == [],
       (h.writes, h.deletes))
    ck("it did NOT print the old false claim",
       not any("Taskbar items restored." in l for l in ctx.lines), ctx.lines)

print()
print("[5] a total restore failure is reported, not swallowed (half 2)")
with Harness(delete_fails=True) as h:
    h.reg[ADS] = 1            # present prior -> restore writes, not deletes
    run(h, tt.apply_taskbar_cleanup)
    h.set_checked_fails = True
    st, ctx = run(h, tt.revert_taskbar_cleanup)
    ck("revert RAISED when every write failed", not isinstance(st, str), st)
    ck("it did NOT also print 'Taskbar items restored.'",
       not any("Taskbar items restored." in l for l in ctx.lines), ctx.lines)

print()
print("[6] an unreadable prior is left alone and NOT counted as restored")
with Harness(denied={ADS}) as h:
    h.reg[ADS] = 0
    run(h, tt.apply_taskbar_cleanup)
    # Don't hardcode the index: ADS is spec #2 in the active list, and a
    # future reordering would silently turn this into a vacuous pass.
    _specs = [tuple(s) for s in h.snap[TASK]["specs"]]
    _idx = _specs.index(ADS)
    ck("the denied value is recorded as denied, not as absent",
       h.snap[TASK].get("%d:denied" % _idx) is True,
       {"idx": _idx, **{k: v for k, v in h.snap[TASK].items()
                        if k.startswith("%d:" % _idx)}})
    h.writes.clear()
    st, ctx = run(h, tt.revert_taskbar_cleanup)
    ck("the other four were still restored", isinstance(st, str), st)
    ck("the unreadable one was NOT overwritten",
       not any(k == ADS for k, _v in h.writes), h.writes)
    ck("and the log says it was kept", any("unreadable" in l for l in ctx.lines),
       ctx.lines)

with Harness(denied=set(ALLFIVE)) as h:
    for k in ALLFIVE:
        h.reg[k] = 0
    run(h, tt.apply_taskbar_cleanup)
    st, ctx = run(h, tt.revert_taskbar_cleanup)
    ck("a snapshot of ONLY denied priors restores nothing, so it RAISES",
       not isinstance(st, str), st)

print()
print("[7] an apply that changes NOTHING leaves no undo badge to clear")
with Harness(set_fails=True) as h:
    st, ctx = run(h, tt.apply_taskbar_cleanup)
    ck("apply raised", not isinstance(st, str), st)
    ck("it said nothing was marked as applied",
       "Nothing was marked as applied" in str(st), st)
    ck("the snapshot it took was dropped", TASK not in h.snap, sorted(h.snap))

print()
print("[8] a PRE-EXISTING snapshot is not destroyed by a failed apply")
with Harness(set_fails=True) as h:
    h.snap[TASK] = {"specs": [list(ADS)], "0:present": True,
                    "0:type": "REG_DWORD", "0:value": 1}
    st, _ = run(h, tt.apply_taskbar_cleanup)
    ck("apply raised", not isinstance(st, str), st)
    ck("the pre-existing snapshot survived", TASK in h.snap, sorted(h.snap))
    ck("...with its value intact", h.snap[TASK]["0:value"] == 1, h.snap[TASK])

print()
print("[9] Win10: Win11-only keys are neither written nor snapshotted")
with Harness(is_win11=False) as h:
    st, ctx = run(h, tt.apply_taskbar_cleanup)
    ck("apply succeeded on Win10", isinstance(st, str), st)
    specs = [tuple(s) for s in h.snap[TASK]["specs"]]
    ck("the snapshot is only the keys Win10 actually writes",
       set(specs) == {ADS, MEET}, specs)
    ck("the Win11-only keys were not written",
       WID not in h.reg and MN not in h.reg and DYN not in h.reg, sorted(h.reg))
    st, _ = run(h, tt.revert_taskbar_cleanup)
    ck("revert succeeded on Win10", isinstance(st, str), st)
    ck("and restored only those two",
       h.reg == {}, h.reg)

print()
print("[10] _restore_reg_values now reports a count")
with Harness() as h:
    ck("no snapshot -> returns 0", tt._restore_reg_values(Ctx(), "nope_at_all") == 0)
with Harness() as h:
    h.reg[ADS] = 1
    run(h, tt.apply_taskbar_cleanup)
    n = tt._restore_reg_values(Ctx(), TASK)
    ck("a 5-value snapshot reports 5", n == 5, n)
with Harness(denied={ADS}) as h:
    h.reg[ADS] = 0
    run(h, tt.apply_taskbar_cleanup)
    n = tt._restore_reg_values(Ctx(), TASK)
    ck("a denied value is not counted (4 of 5)", n == 4, n)

print()
print("[11] the hardcoded 1-reverts are gone from the source")
# AST, not text: the first version grepped the source and matched the
# DOCSTRING that explains HideSCAMeetNow, so a correct fix "failed". Only real
# calls in the function body should count.
import ast  # noqa: E402

_src = open("app/tasks/tweak_tasks.py", encoding="utf-8").read()
_tree = ast.parse(_src)


def _fn_body(name):
    node = next(n for n in _tree.body
                if isinstance(n, ast.FunctionDef) and n.name == name)
    return node


def _called_names(fn):
    out = set()
    for n in ast.walk(fn):
        if isinstance(n, ast.Call):
            f = n.func
            if isinstance(f, ast.Name):
                out.add(f.id)
            elif isinstance(f, ast.Attribute):
                out.add(f.attr)
    return out


_rev = _fn_body("revert_taskbar_cleanup")
_app = _fn_body("apply_taskbar_cleanup")
_rev_calls = _called_names(_rev)
ck("revert makes no registry write or delete call of its own",
   not (_rev_calls & {"reg_set_value", "reg_set_value_checked",
                      "reg_delete_value"}),
   sorted(_rev_calls))
ck("revert goes through the snapshot helper",
   "_restore_reg_values" in _rev_calls, sorted(_rev_calls))
ck("revert raises when nothing was restored",
   any(isinstance(n, ast.Raise) for n in ast.walk(_rev)))
ck("apply snapshots before writing",
   "_snap_reg_values" in _called_names(_app), sorted(_called_names(_app)))
# the hardcoded revert values that caused the bug
_lits = [n.value for n in ast.walk(_rev) if isinstance(n, ast.Constant)
         and isinstance(n.value, int)]
ck("revert contains no hardcoded 1-valued restore", 1 not in _lits, _lits)
ck("the old unconditional success line is gone",
   'ctx.log("Taskbar items restored. Restart Explorer to see changes.")'
   not in _src)

print()
if FAILS:
    print("BUG-017 VERIFY: %d/%d FAILED -> %s" % (FAILS, CHECKS, "see above"))
    sys.exit(1)
print("BUG-017 VERIFY: ALL PASS (%d checks)" % CHECKS)
