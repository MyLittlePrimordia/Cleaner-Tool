"""MED-010 and MED-011: the powercfg revert path could half-apply itself.

MED-011 — `_restore_powercfg_pairs` validated INSIDE its write loop, so a
snapshot with a bad value at position 1 left position 0 already restored and
1..n untouched. A half-reverted machine is worse than an un-reverted one: the
user cannot tell which half moved. Its sibling `_restore_reg_values` already
did validate-then-write correctly; the finding asked for the twin to match.

MED-010 — `revert_nvidia_max_performance` ran `_restore_powercfg_pairs` first
and `_restore_reg_values` second with nothing between them. powercfg shells out
several times and can fail; when it raised, `PowerMizerEnable` was left at 0
PERMANENTLY with no path back. Now both halves are always attempted, registry
first, and the failures are reported together.

Nothing real is touched: `run_cmd`, the snapshot helpers and the config-persist
seams are all faked.
"""

from __future__ import annotations

import sys

sys.path.insert(0, ".")

import app.config_persist as cp          # noqa: E402
from app.tasks import tweak_tasks as tt  # noqa: E402

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


class Ctx:
    def __init__(self):
        self.lines = []

    def log(self, m, *a, **k):
        self.lines.append(str(m))

    def set_status(self, *a, **k):
        pass

    def cancelled(self):
        return False


def _argv_of(cmd):
    """Normalise a run_cmd command to a list of tokens.

    Accepts the argv-list form and the old interpolated string form, so this
    harness is a test of the restore logic rather than of how the command
    happens to be serialised.
    """
    if isinstance(cmd, (list, tuple)):
        return [str(x) for x in cmd]
    import shlex
    try:
        return shlex.split(str(cmd))
    except ValueError:
        return str(cmd).split()


class H:
    """Fakes run_cmd (recording every powercfg write) and the snapshot store."""

    def __init__(self, snap, fail_on=(), reg_count=1, reg_raises=False):
        self.snap = dict(snap or {})
        # Every command, normalised to a token list.
        #
        # The powercfg writes are argv lists with shell=False now (SEC-001
        # removed the interpolated command strings), so recording the raw
        # object made `"/setacvalueindex" in c` a list-membership test and
        # `" 12" in c` -- which relied on the old string spacing -- silently
        # false. Normalising here keeps every assertion below an exact-token
        # check that behaves the same for both call shapes.
        self.writes = []
        self.fail_on = set(fail_on)
        self.reg_count = reg_count
        self.reg_raises = reg_raises
        self.cleared = []

    def run_cmd(self, ctx, command, shell=True, timeout=None, collect=None):
        tokens = (_argv_of(command))
        self.writes.append(tokens)
        for needle in self.fail_on:
            if needle in tokens:
                return 1
        return 0

    def reg_restore(self, ctx, task_id, value_type="REG_DWORD"):
        if self.reg_raises:
            raise RuntimeError("simulated registry restore failure")
        self.cleared.append(task_id)
        return self.reg_count

    def save(self, task_id, values):
        if not values or self.snap.get(task_id):
            return
        self.snap[task_id] = dict(values)

    def get(self, task_id):
        return self.snap.get(task_id) or {}

    def clear(self, task_id):
        self.cleared.append(task_id)
        self.snap.pop(task_id, None)

    def __enter__(self):
        self._s = (tt.run_cmd, tt._restore_reg_values, tt.save_tweak_snapshot,
                   tt.get_tweak_snapshot, tt.clear_tweak_snapshot,
                   cp.save_tweak_snapshot, cp.get_tweak_snapshot,
                   cp.clear_tweak_snapshot)
        tt.run_cmd = self.run_cmd
        tt._restore_reg_values = self.reg_restore
        tt.save_tweak_snapshot = self.save
        tt.get_tweak_snapshot = self.get
        tt.clear_tweak_snapshot = self.clear
        cp.save_tweak_snapshot = self.save
        cp.get_tweak_snapshot = self.get
        cp.clear_tweak_snapshot = self.clear
        return self

    def __exit__(self, *e):
        (tt.run_cmd, tt._restore_reg_values, tt.save_tweak_snapshot,
         tt.get_tweak_snapshot, tt.clear_tweak_snapshot, cp.save_tweak_snapshot,
         cp.get_tweak_snapshot, cp.clear_tweak_snapshot) = self._s
        return False


PAIRS = [("sub_a", "SET_A"), ("sub_b", "SET_B"), ("sub_c", "SET_C")]
FALLBACK = {"sub_a:SET_A": 1, "sub_b:SET_B": 2, "sub_c:SET_C": 3}


def ac_writes(h):
    return [c for c in h.writes if "/setacvalueindex" in c]


print("[1] MED-011: a bad value at position 1 writes NOTHING AT ALL")
# position 0 is perfectly valid; position 1 is poisoned.
POISONED = {"sub_a:SET_A": 11, "sub_b:SET_B": "evil", "sub_c:SET_C": 33}
with H({"t": POISONED}) as h:
    err = None
    try:
        tt._restore_powercfg_pairs(Ctx(), "t", PAIRS, FALLBACK)
    except Exception as exc:
        err = exc
    ck("it raised", err is not None, "returned normally")
    ck("the error names the bad value's key", "sub_b:SET_B" in str(err), err)
    ck("NOTHING was written - no half-reverted machine",
       ac_writes(h) == [], ac_writes(h))
    ck("not even the VALID position 0 was written",
       not any("SET_A" in c for c in h.writes), h.writes)
    ck("the snapshot was kept so Undo can be retried after repair",
       "t" in h.snap, sorted(h.snap))

print()
print("[2] MED-011: a bad DC value also blocks every write")
POISON_DC = {"sub_a:SET_A": 11, "sub_a:SET_A:dc": "evil"}
with H({"t": POISON_DC}) as h:
    err = None
    try:
        tt._restore_powercfg_pairs(Ctx(), "t", PAIRS, FALLBACK)
    except Exception as exc:
        err = exc
    ck("it raised", err is not None, "returned normally")
    ck("the error is about the DC index", "DC" in str(err), err)
    ck("nothing at all was written", ac_writes(h) == [], ac_writes(h))

print()
print("[3] the happy path still writes every value, AC and DC")
GOOD = {"sub_a:SET_A": 11, "sub_a:SET_A:dc": 12,
        "sub_b:SET_B": 21, "sub_c:SET_C": 31}
with H({"t": GOOD}) as h:
    tt._restore_powercfg_pairs(Ctx(), "t", PAIRS, FALLBACK)
    ck("all three AC values written", len(ac_writes(h)) == 3, ac_writes(h))
    ck("the DC value was written too",
       any("/setdcvalueindex" in c and "12" in c for c in h.writes), h.writes)
    ck("setactive was called last-ish",
       any("/setactive" in c for c in h.writes), h.writes)
    ck("the snapshot was cleared on success", "t" not in h.snap)

print()
print("[4] a failed powercfg WRITE keeps the snapshot (C6 contract)")
with H({"t": GOOD}, fail_on=("sub_b",)) as h:
    err = None
    try:
        tt._restore_powercfg_pairs(Ctx(), "t", PAIRS, FALLBACK)
    except Exception as exc:
        err = exc
    ck("it raised", err is not None, "returned normally")
    ck("the snapshot survived for a retry", "t" in h.snap, sorted(h.snap))
    ck("the OTHER values were still written (best effort)",
       any("SET_A" in c for c in h.writes), h.writes)

print()
print("[5] MED-010: a failing powercfg restore must NOT strand PowerMizerEnable")
# The exact finding scenario: powercfg fails, so the old code raised before
# ever touching the registry value, leaving it at 0 forever.
MAXP = {"sub_processor:PROCTHROTTLEMAX": 100}
with H({"max_performance_gpu": MAXP}, fail_on=("powercfg",)) as h:
    err = None
    try:
        tt.revert_nvidia_max_performance(Ctx())
    except Exception as exc:
        err = exc
    ck("Undo reported failure (honest)", err is not None, "returned normally")
    ck("THE REGISTRY VALUE WAS STILL RESTORED despite powercfg failing",
       "max_performance_gpu_pmz" in h.cleared, h.cleared)
    ck("the error names the FAILING half", "power plan" in str(err), err)
    ck("...and does not invent a problem with the half that worked",
       "PowerMizerEnable:" not in str(err), err)
    ck("...and says a retry is safe", "again" in str(err).lower(), err)

print()
print("[6] MED-010: a failing REGISTRY restore must not skip the powercfg half")
with H({"max_performance_gpu": MAXP}, reg_raises=True) as h:
    err = None
    try:
        tt.revert_nvidia_max_performance(Ctx())
    except Exception as exc:
        err = exc
    ck("Undo reported failure", err is not None, "returned normally")
    ck("the powercfg half STILL ran",
       any("powercfg" in c for c in h.writes), h.writes)
    ck("the error names the registry failure", "PowerMizerEnable" in str(err), err)

print()
print("[7] both halves failing is reported completely, not just the first")
with H({"max_performance_gpu": MAXP}, fail_on=("powercfg",),
       reg_raises=True) as h:
    err = None
    try:
        tt.revert_nvidia_max_performance(Ctx())
    except Exception as exc:
        err = exc
    ck("Undo raised", err is not None, "returned normally")
    ck("BOTH problems are named in the one message",
       "PowerMizerEnable" in str(err) and "power plan" in str(err), err)

print()
print("[8] the clean path: both halves restored, no error")
with H({"max_performance_gpu": MAXP}, reg_count=1) as h:
    err = None
    try:
        tt.revert_nvidia_max_performance(Ctx())
    except Exception as exc:
        err = exc
    ck("Undo succeeded", err is None, err)
    ck("the registry half ran", "max_performance_gpu_pmz" in h.cleared, h.cleared)
    ck("the powercfg half ran",
       any("PROCTHROTTLEMAX" in c for c in h.writes), h.writes)
    ck("the powercfg snapshot was cleared", "max_performance_gpu" not in h.snap)

print()
print("[9] structural: both halves are in their own try, registry first")
import ast  # noqa: E402
import inspect  # noqa: E402

fn = next(n for n in ast.walk(ast.parse(inspect.getsource(tt)))
          if isinstance(n, ast.FunctionDef)
          and n.name == "revert_nvidia_max_performance")
tries = [n for n in ast.walk(fn) if isinstance(n, ast.Try)]
ck("there are exactly two try blocks", len(tries) == 2, len(tries))
protected = set()
for t_ in tries:
    for x in ast.walk(t_):
        if isinstance(x, ast.Call) and isinstance(x.func, ast.Name):
            protected.add(x.func.id)
ck("each restore call is inside a try",
   {"_restore_reg_values", "_restore_powercfg_pairs"} <= protected,
   sorted(protected))
pos = sorted((n.lineno, n.func.id) for n in ast.walk(fn)
             if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
             and n.func.id in ("_restore_reg_values", "_restore_powercfg_pairs"))
ck("the registry restore comes FIRST (it is the cheap local write)",
   pos and pos[0][1] == "_restore_reg_values", pos)
ck("MED-010 is documented", "MED-010" in inspect.getsource(tt.revert_nvidia_max_performance))

pf = inspect.getsource(tt._restore_powercfg_pairs)
ck("MED-011 is documented", "MED-011" in pf)
# The write used to be an f-string command; it is an argv list with
# shell=False now (SEC-001), so match the structural token rather than the
# literal text. /setacvalueindex is the flag that identifies the write.
ck("validation happens before any run_cmd write",
   pf.index("pass 1") < pf.index("pass 2")
   and pf.index("pass 1") < pf.index('"/setacvalueindex"'))
ck("the write is an argv list, not a shell string (SEC-001)",
   'run_cmd(ctx, ["powercfg", "/setacvalueindex"' in pf
   and "shell=False" in pf)
ck("the write loop iterates the pre-validated plan",
   "for key, subgroup, setting, value, dc_value in plan:" in pf)

print()
if FAILS:
    print("MED-010/011 VERIFY: %d/%d FAILED" % (FAILS, CHECKS))
    sys.exit(1)
print("MED-010/011 VERIFY: ALL PASS (%d checks)" % CHECKS)
