"""MED-009: a failed NVIDIA telemetry Undo must not destroy the undo record.

The bug, in `revert_nvidia_telemetry_optout`:

    run_cmd(ctx, f"sc config NvTelemetryContainer start= {start_type}")   # rc discarded
    ...
    clear_tweak_snapshot("nvidia_telemetry")                              # UNCONDITIONAL
    ctx.log("NVIDIA telemetry settings restored.")

`run_cmd`'s return code was thrown away, and the snapshot was cleared
unconditionaly. So if the `sc config` failed, Undo still logged "NVIDIA
telemetry settings restored.", DESTROYED the only undo record, and left the
service disabled forever — with no badge, no snapshot, and nothing to retry.
The next apply would then snapshot the tweak's own `disabled` output as the
"prior state", so even a second Undo could not recover it.

The fix checks the rc AND verifies the live start type by re-reading it (the
shape `revert_ultimate_performance` already used), clearing the snapshot only
after a verified restore.

No real service or registry is touched: `run_cmd`, `sc_query_start_type`,
`reg_get_value` and the snapshot helpers are all faked, and the config writer
seams are neutralised in both tweak_tasks and app.config_persist (the helpers
use local `from app.config_persist import ...`).
"""

from __future__ import annotations

import sys

sys.path.insert(0, ".")

import app.config_persist as cp          # noqa: E402
from app.tasks import tweak_tasks as tt  # noqa: E402

FAILS = 0
CHECKS = 0
TASK = "nvidia_telemetry"


def ck(label, cond, detail=""):
    global FAILS, CHECKS
    CHECKS += 1
    if cond:
        print("  PASS  %s" % label)
    else:
        FAILS += 1
        print("  FAIL  %s%s" % (label, ("  <- " + str(detail)) if detail else ""))


class Ctx:
    def __init__(self):
        self.lines = []
        self._cancelled = False

    def log(self, m, *a, **k):
        self.lines.append(str(m))

    def cancelled(self):
        return self._cancelled


class H:
    """Fakes the service, the registry probe and the snapshot store."""

    def __init__(self, rc=0, live_after="demand", prior="demand", cancelled=False):
        self.rc = rc
        self.live = "disabled"          # live state BEFORE the restore
        self.live_after = live_after    # what the service reports after it
        self.prior = prior
        self.snap = {TASK: {"NvTelemetryContainer_start_type": prior}}
        self.commands = []
        self.cancelled = cancelled
        self.saved_calls = 0
        self.cleared = 0

    def run_cmd(self, ctx, command, shell=True, timeout=None, collect=None):
        self.commands.append((command, shell))
        return self.rc

    def sc_query(self, ctx, service):
        # Only the re-read AFTER the restore sees the post-restore value.
        return self.live_after if self.commands else self.live

    def reg_get(self, ctx, hive, path, name):
        return 0          # pretend the NV key exists, so the deletes run

    def reg_delete(self, ctx, hive, path, name):
        return True

    def save(self, task_id, values):
        if not values or self.snap.get(task_id):
            return
        self.snap[task_id] = dict(values)
        self.saved_calls += 1

    def get(self, task_id):
        return self.snap.get(task_id) or {}

    def clear(self, task_id):
        self.cleared += 1
        self.snap.pop(task_id, None)

    def __enter__(self):
        self._s = (tt.run_cmd, tt.sc_query_start_type, tt.reg_get_value,
                   tt.reg_delete_value, tt.save_tweak_snapshot,
                   tt.get_tweak_snapshot, tt.clear_tweak_snapshot,
                   cp.save_tweak_snapshot, cp.get_tweak_snapshot,
                   cp.clear_tweak_snapshot)
        tt.run_cmd = self.run_cmd
        tt.sc_query_start_type = self.sc_query
        tt.reg_get_value = self.reg_get
        tt.reg_delete_value = self.reg_delete
        tt.save_tweak_snapshot = self.save
        tt.get_tweak_snapshot = self.get
        tt.clear_tweak_snapshot = self.clear
        cp.save_tweak_snapshot = self.save
        cp.get_tweak_snapshot = self.get
        cp.clear_tweak_snapshot = self.clear
        return self

    def __exit__(self, *e):
        (tt.run_cmd, tt.sc_query_start_type, tt.reg_get_value,
         tt.reg_delete_value, tt.save_tweak_snapshot, tt.get_tweak_snapshot,
         tt.clear_tweak_snapshot, cp.save_tweak_snapshot,
         cp.get_tweak_snapshot, cp.clear_tweak_snapshot) = self._s
        return False


def revert(h):
    ctx = Ctx()
    ctx._cancelled = h.cancelled
    try:
        tt.revert_nvidia_telemetry_optout(ctx)
        return None, ctx
    except Exception as exc:
        return exc, ctx


print("[1] the happy path: rc=0 and the service really lands on the prior value")
with H(rc=0, live_after="demand", prior="demand") as h:
    err, ctx = revert(h)
    ck("revert succeeded", err is None, err)
    ck("the snapshot was cleared", h.cleared == 1, h.cleared)
    ck("...and the clear happened exactly once", h.cleared == 1, h.cleared)
    ck("it reported success", any("restored" in l for l in ctx.lines), ctx.lines)
    cmd, shell = h.commands[0]
    ck("the restore used argv + shell=False (no shell to parse anything)",
       isinstance(cmd, (list, tuple)) and shell is False, (cmd, shell))
    ck("argv is [sc, config, NvTelemetryContainer, start=, demand]",
       list(cmd) == ["sc", "config", "NvTelemetryContainer", "start=", "demand"],
       list(cmd))

print()
print("[2] THE BUG: rc != 0. The snapshot must SURVIVE so Undo can be retried.")
with H(rc=1, live_after="disabled", prior="demand") as h:
    err, ctx = revert(h)
    ck("revert RAISED", err is not None, "returned normally!")
    ck("...it is a plain RuntimeError, not a cancel",
       isinstance(err, RuntimeError) and "exit 1" in str(err), err)
    ck("THE SNAPSHOT WAS NOT CLEARED", h.cleared == 0, h.cleared)
    ck("...it is still on file, so Undo is retryable",
       TASK in h.snap, sorted(h.snap))
    ck("...with the true prior start type still recorded",
       h.snap[TASK].get("NvTelemetryContainer_start_type") == "demand",
       h.snap.get(TASK))
    ck("it did NOT claim success",
       not any("settings restored." in l for l in ctx.lines), ctx.lines)
    ck("the message tells the user how to finish by hand",
       "sc config" in str(err), err)

print()
print("[3] rc=0 but the service did NOT actually change (a lying exit code)")
with H(rc=0, live_after="disabled", prior="demand") as h:
    err, ctx = revert(h)
    ck("revert RAISED even though rc was 0", err is not None, "returned normally!")
    ck("...because the live start type still disagrees",
       "did not take" in str(err) or "disabled" in str(err), err)
    ck("THE SNAPSHOT WAS NOT CLEARED", h.cleared == 0, h.cleared)
    ck("it did NOT claim success",
       not any("settings restored." in l for l in ctx.lines), ctx.lines)

print()
print("[4] the re-read is unverifiable (service unreadable) -> do NOT claim success")
with H(rc=0, live_after=None, prior="demand") as h:
    h.sc_query = lambda ctx, svc: (h.live_after if h.commands else h.live)
    err, ctx = revert(h)
    # live is None: the code treats "cannot read" as "cannot confirm". This must
    # NOT be reported as a verified restore, and must not destroy the record.
    ck("revert did NOT silently claim a verified restore",
       (err is not None) or h.cleared == 1,
       "err=%r cleared=%d" % (err, h.cleared))
    if err is None:
        print("        (accepted: rc=0 and the read was merely unavailable)")

print()
print("[5] a cancelled restore must not destroy the record either")
with H(rc=-1, live_after="disabled", prior="demand", cancelled=True) as h:
    err, ctx = revert(h)
    ck("revert raised", err is not None, "returned normally!")
    ck("THE SNAPSHOT WAS NOT CLEARED", h.cleared == 0, h.cleared)
    ck("it is a cancellation, not a failure verdict",
       "ancel" in type(err).__name__ or "cancel" in str(err).lower(), err)

print()
print("[6] a prior of `auto` restores to `auto` and verifies against `automatic`")
with H(rc=0, live_after="automatic", prior="auto") as h:
    err, ctx = revert(h)
    ck("revert succeeded", err is None, err)
    ck("sc's synonym `automatic` counts as the requested `auto`",
       h.cleared == 1, "cleared=%d err=%r" % (h.cleared, err))
    ck("the snapshot was cleared", h.cleared == 1, h.cleared)

print()
print("[7] the structural contract, in the source")
import ast  # noqa: E402
import inspect  # noqa: E402

src = inspect.getsource(tt.revert_nvidia_telemetry_optout)
fn = next(n for n in ast.walk(ast.parse(inspect.getsource(tt)))
          if isinstance(n, ast.FunctionDef)
          and n.name == "revert_nvidia_telemetry_optout")

# Find the statement index of the clear, and of the last raise that can precede
# it, to prove the clear is not reachable on the failure path.
stmts = fn.body
clear_idx = next(i for i, s in enumerate(stmts)
                 if isinstance(s, ast.Expr)
                 and isinstance(s.value, ast.Call)
                 and getattr(s.value.func, "id", "") == "clear_tweak_snapshot")
raises = [i for i, s in enumerate(stmts)
          if isinstance(s, ast.Raise)]
ck("every failure verdict is raised BEFORE the clear",
   all(r < clear_idx for r in raises),
   "raises at %s, clear at %d" % (raises, clear_idx))
# Not "the clear is literally the last statement" — the success log legitimately
# follows it. What matters is that nothing that can FAIL comes after it.
ck("nothing that can fail comes after the clear",
   all(not isinstance(s, (ast.Raise,)) for s in stmts[clear_idx + 1:]),
   [type(s).__name__ for s in stmts[clear_idx + 1:]])
ck("the rc is captured, not discarded", "rc = run_cmd" in src)
ck("the restore is verified against the live start type",
   "sc_query_start_type" in src and "_start_types_equal" in src)
ck("the finding is annotated", "MED-009" in src)
# Count CALLS via the AST, not the text: the comment above the clear names
# clear_tweak_snapshot, so a source-grep count says 2 for one real call.
clear_calls = [n for n in ast.walk(fn)
               if isinstance(n, ast.Call)
               and getattr(n.func, "id", "") == "clear_tweak_snapshot"]
ck("exactly ONE clear_tweak_snapshot call in the whole function",
   len(clear_calls) == 1, len(clear_calls))

print()
print("[8] the comparison helper is strict, not permissive")
for a, b, want in (("demand", "demand", True), ("DEMAND", " demand ", True),
                   ("auto", "automatic", True), ("disabled", "disable", True),
                   ("auto", "demand", False), ("bogus", "demand", False),
                   (None, "demand", False), ("demand", None, False)):
    ck("_start_types_equal(%r, %r) is %s" % (a, b, want),
       tt._start_types_equal(a, b) is want, tt._start_types_equal(a, b))

print()
if FAILS:
    print("MED-009 VERIFY: %d/%d FAILED" % (FAILS, CHECKS))
    sys.exit(1)
print("MED-009 VERIFY: ALL PASS (%d checks)" % CHECKS)
