"""BUG-015: a snapshot must never outlive a failed apply.

`apply_prefer_ipv4` wrote its snapshot and THEN performed the registry
change, with no failure branch. If the change raised, the snapshot survived —
and `get_tweak_state()` unions `tweak_snapshots` into the applied set, so the
GUI showed a green "Active" badge for a change that never happened. Worse,
`save_tweak_snapshot` refuses to overwrite an existing non-empty snapshot, so
re-running the tweak could not repair it either.

The fix is the idiom ~30 sibling apply_* functions already use:

    had_snapshot = bool(get_tweak_snapshot(id))
    <snapshot>
    try:
        <the change>
    except Exception:
        if not had_snapshot:
            clear_tweak_snapshot(id)
        raise

Note the guard is conditional on `had_snapshot` on purpose: if a snapshot was
already on file the tweak was already applied, so a failed RE-apply must not
destroy the record of the apply that is still in effect.

This checks two things, because either alone is insufficient:

  1. RUNTIME — drive apply_prefer_ipv4 with an injected failure and assert the
     registry snapshot is gone and the tweak is no longer reported applied.
  2. STATIC, ORDERING-AWARE — every apply_* that writes a snapshot BEFORE its
     mutating call must have the clear+raise guard. A naive "has a snapshot
     but no clear_tweak_snapshot" test produces false positives: several
     functions snapshot only AFTER a successful change, which is the correct
     order and needs no cleanup at all. That distinction is the whole point.
"""
from __future__ import annotations

import ast
import io
import os
import sys

sys.path.insert(0, r"C:\Users\User\Desktop\Cleaner Tool")

from app.tasks import tweak_tasks as tt
from app.config_persist import (clear_tweak_snapshot, get_tweak_snapshot,
                                get_tweak_state, save_tweak_snapshot)

FAILS = []
N = [0]


def ck(label, cond, detail=""):
    N[0] += 1
    if cond:
        print("  PASS  %s" % label)
    else:
        FAILS.append(label)
        print("  FAIL  %s%s" % (label, ("  <- " + repr(detail)) if detail else ""))


TASK = "prefer_ipv4"


class Ctx:
    """Minimal TaskContext stand-in — apply_prefer_ipv4 only logs."""
    def __init__(self):
        self.logs = []
        self.statuses = []

    def log(self, m, *a, **k):
        self.logs.append(str(m))

    def set_status(self, m, *a, **k):
        self.statuses.append(str(m))

    def cancelled(self):
        return False


# --------------------------------------------------------------------------- #
print("\n[1] runtime: a failed apply leaves nothing behind")
# make sure we start clean, and restore whatever was there afterwards
_pre = get_tweak_snapshot(TASK)
_was_applied = TASK in (get_tweak_state() or {})
try:
    clear_tweak_snapshot(TASK)
    ck("precondition: no snapshot before the test",
       get_tweak_snapshot(TASK) == {} or not get_tweak_snapshot(TASK),
       get_tweak_snapshot(TASK))
    ck("precondition: not reported as applied",
       TASK not in (get_tweak_state() or {}))

    # a snapshot written by the apply itself, so the guard has something to clear
    orig_set = tt.reg_set_value_checked
    calls = {"n": 0}

    def boom(ctx, hive, path, name, value, **kw):
        calls["n"] += 1
        raise OSError("injected registry failure")

    tt.reg_set_value_checked = boom
    try:
        raised = False
        try:
            tt.apply_prefer_ipv4(Ctx())
        except Exception:
            raised = True
    finally:
        tt.reg_set_value_checked = orig_set

    ck("the injected failure propagated (apply did not swallow it)", raised)
    ck("the registry write was actually attempted", calls["n"] == 1, calls)
    snap = get_tweak_snapshot(TASK)
    ck("NO snapshot survives the failure", not snap, snap)
    ck("...so the tweak is NOT badged applied",
       TASK not in (get_tweak_state() or {}),
       sorted(k for k in (get_tweak_state() or {}) if "ipv4" in k))

    # a second, stricter case: a pre-existing snapshot must SURVIVE a failed
    # re-apply, because the earlier apply is still in effect.
    save_tweak_snapshot(TASK, {"DisabledComponents": "0x0"})
    tt.reg_set_value_checked = boom
    try:
        try:
            tt.apply_prefer_ipv4(Ctx())
        except Exception:
            pass
    finally:
        tt.reg_set_value_checked = orig_set
    kept = get_tweak_snapshot(TASK)
    ck("a PRE-EXISTING snapshot is NOT cleared by a failed re-apply",
       bool(kept.get("DisabledComponents")), kept)
    clear_tweak_snapshot(TASK)

    # and the success path still snapshots
    calls2 = {"n": 0}
    def fine(ctx, hive, path, name, value, **kw):
        calls2["n"] += 1
        return True
    tt.reg_set_value_checked = fine
    try:
        tt.apply_prefer_ipv4(Ctx())
    finally:
        tt.reg_set_value_checked = orig_set
    ck("a successful apply DOES leave a snapshot (revert stays possible)",
       bool(get_tweak_snapshot(TASK)), get_tweak_snapshot(TASK))
finally:
    clear_tweak_snapshot(TASK)
    if _pre:
        save_tweak_snapshot(TASK, _pre)

# --------------------------------------------------------------------------- #
print("\n[2] static, ordering-aware: every snapshot-before-change has the guard")
src = io.open(os.path.join(os.path.dirname(tt.__file__), "tweak_tasks.py"),
              encoding="utf-8").read()
lines = src.splitlines()
tree = ast.parse(src)

SNAP_CALLS = ("save_tweak_snapshot", "_snap_reg_values", "_snap_")
MUTATE = ("reg_set_value_checked", "run_cmd", "reg_set_value",
          "_devmode", "schtasks", "netsh", "powershell", "ctypes.windll")

snap_before, guarded, snap_after = [], [], []
for node in tree.body:
    if not (isinstance(node, ast.FunctionDef) and node.name.startswith("apply_")):
        continue
    body = lines[node.lineno - 1:node.end_lineno]
    snap_ln = next((i for i, l in enumerate(body)
                    if any(c in l for c in SNAP_CALLS)
                    and "clear_tweak_snapshot" not in l), None)
    mut_ln = next((i for i, l in enumerate(body)
                   if any(c in l for c in MUTATE) and not l.strip().startswith("#")), None)
    has_guard = ("clear_tweak_snapshot" in "\n".join(body)
                 and "raise" in "\n".join(body))
    if snap_ln is None:
        continue
    if mut_ln is not None and snap_ln < mut_ln:
        # snapshot FIRST: needs the cleanup branch
        (guarded if has_guard else snap_before).append(
            (node.lineno, node.name))
    else:
        # snapshot only after a successful change: nothing to clean up
        snap_after.append((node.lineno, node.name))

print("        snapshot-before-change WITH the guard : %d" % len(guarded))
print("        snapshot-before-change WITHOUT         : %d" % len(snap_before))
print("        snapshot-after-change (no guard needed): %d" % len(snap_after))
for ln, nm in snap_before:
    print("            L%-6d %s" % (ln, nm))
ck("no apply_* writes a snapshot before its change without a clear+raise guard",
   not snap_before, [n for _l, n in snap_before])
ck("prefer_ipv4 is now in the guarded set",
   any(n == "apply_prefer_ipv4" for _l, n in guarded),
   [n for _l, n in guarded][:6])
ck("there are still snapshotting applies to check (the audit is not vacuous)",
   len(guarded) + len(snap_after) >= 20,
   len(guarded) + len(snap_after))

print()
if FAILS:
    print("BUG-015 VERIFY: %d/%d FAILED -> %s" % (len(FAILS), N[0], FAILS))
    sys.exit(1)
print("BUG-015 VERIFY: ALL PASS (%d checks)" % N[0])
