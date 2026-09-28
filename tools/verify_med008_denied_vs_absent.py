"""MED-008: a read failure must never be mistaken for "the value is absent".

`reg_get_value_typed` collapsed every non-ACCESS_DENIED OSError into
"absent" - only winerror 5 / errno 5,13 produced the `_REG_DENIED` sentinel.
That distinction is load-bearing, because the caller cannot recover it:

    _snap_reg_values   records `present: False` for an absent read
    _restore_reg_values sees present=False and DELETES the value

So an ordinary sharing violation (winerror 32, file open by another process),
a busy hive (33 / 234) or a transient I/O error was enough to make Undo delete
a registry value the user actually had set. The `_REG_DENIED` sentinel existed
precisely to prevent this, and the F13 comment even says so - it just did not
cover the common cases.

The rule now: only a DEFINITIVE not-found (winerror 2/3) is reported as absent.
Everything else - including a bare `except Exception` - is denied/unknown, and a
denied value is left in place on revert with a log line.

No real registry is touched: `winreg.QueryValueEx` is patched to raise each
error, and `_snap_reg_values` / `_restore_reg_values` run against that.
"""

from __future__ import annotations

import sys
import winreg

sys.path.insert(0, ".")

from app import utils as u                      # noqa: E402
from app.tasks import tweak_tasks as tt         # noqa: E402

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


HKCU = "HKCU"
PATH = r"Software\Microsoft\Windows"
NAME = "Med008_ProbeValue"


class _patch:
    def __init__(self, exc):
        self.exc = exc
        self._real = None

    def __enter__(self):
        self._real = winreg.QueryValueEx
        exc = self.exc

        def boom(key, name, _e=exc):
            raise _e

        winreg.QueryValueEx = boom
        return self

    def __exit__(self, *a):
        winreg.QueryValueEx = self._real
        return False


def oserr(winerror, errno_):
    e = OSError(errno_, "simulated")
    if winerror is not None:
        try:
            e.winerror = winerror
        except AttributeError:
            pass
    return e


print("[1] only a definitive not-found is reported as ABSENT")
ABSENT = [("ERROR_FILE_NOT_FOUND", oserr(2, 2)),
          ("ERROR_PATH_NOT_FOUND", oserr(3, 3))]
for label, exc in ABSENT:
    with _patch(exc):
        v, t = u.reg_get_value_typed(Ctx(), HKCU, PATH, NAME)
    ck("%s -> absent" % label, v is None and v is not u._REG_DENIED, v)

print()
print("[2] every other failure is DENIED/unknown, never absent (MED-008)")
DENIED = [
    ("ERROR_ACCESS_DENIED (5)", oserr(5, 13)),
    ("PermissionError", PermissionError(13, "denied")),
    ("ERROR_SHARING_VIOLATION (32) - the finding's example", oserr(32, 32)),
    ("ERROR_LOCK_FAILED (33)", oserr(33, 33)),
    ("ERROR_WRITE_PROTECT (19)", oserr(19, 19)),
    ("ERROR_HIVE_BUSY (234)", oserr(234, 234)),
    ("a generic OSError with no code", oserr(None, 9999)),
    ("an unexpected non-OSError exception", ValueError("surprise")),
]
for label, exc in DENIED:
    with _patch(exc):
        v, t = u.reg_get_value_typed(Ctx(), HKCU, PATH, NAME)
    ck("%-46s -> denied" % label, v is u._REG_DENIED, repr(v))

print()
print("[3] THE CONSEQUENCE: a denied read must not make Undo delete the value")
# Snapshot under a sharing violation: the value must be recorded as UNKNOWN,
# not as absent.
snap_box = {}


class SnapHarness:
    def __enter__(self):
        self._s = (tt.save_tweak_snapshot, tt.get_tweak_snapshot,
                   tt.clear_tweak_snapshot, tt.reg_set_value_checked,
                   tt.reg_delete_value)
        import app.config_persist as cp
        self._cp = cp
        self._cpo = (cp.save_tweak_snapshot, cp.get_tweak_snapshot,
                     cp.clear_tweak_snapshot)
        self.deleted = []
        self.written = []

        def save(tid, values):
            snap_box[tid] = dict(values)

        def get(tid):
            return snap_box.get(tid) or {}

        def clear(tid):
            snap_box.pop(tid, None)

        def setv(ctx, hive, path, name, value, value_type="REG_DWORD"):
            self.written.append((hive, path, name, value))
            return True

        def dele(ctx, hive, path, name):
            self.deleted.append((hive, path, name))
            return True

        tt.save_tweak_snapshot = save
        tt.get_tweak_snapshot = get
        tt.clear_tweak_snapshot = clear
        tt.reg_set_value_checked = setv
        tt.reg_delete_value = dele
        cp.save_tweak_snapshot = save
        cp.get_tweak_snapshot = get
        cp.clear_tweak_snapshot = clear
        return self

    def __exit__(self, *a):
        (tt.save_tweak_snapshot, tt.get_tweak_snapshot,
         tt.clear_tweak_snapshot, tt.reg_set_value_checked,
         tt.reg_delete_value) = self._s
        (self._cp.save_tweak_snapshot, self._cp.get_tweak_snapshot,
         self._cp.clear_tweak_snapshot) = self._cpo
        return False


with _patch(oserr(32, 32)), SnapHarness() as h:
    tt._snap_reg_values(Ctx(), "med008", [(HKCU, PATH, NAME)])
    snap = snap_box.get("med008") or {}
    ck("a snapshot WAS taken", bool(snap), snap)
    ck("it records the value as DENIED, not absent",
       snap.get("0:denied") is True, {k: v for k, v in snap.items() if ":" in k})
    ck("...and explicitly NOT as absent",
       snap.get("0:present") is not True, snap.get("0:present"))

    ctx = Ctx()
    try:
        tt._restore_reg_values(ctx, "med008")
    except Exception as exc:
        print("        (restore raised: %r)" % (exc,))
    ck("Undo did NOT delete the value", h.deleted == [], h.deleted)
    ck("Undo did not write a guessed value either", h.written == [], h.written)
    ck("Undo says it left the value alone",
       any("unreadable" in l for l in ctx.lines), ctx.lines)

print()
print("[4] CONTRAST: a genuine not-found still restores to absent (by deleting)")
with _patch(oserr(2, 2)), SnapHarness() as h2:
    tt._snap_reg_values(Ctx(), "med008b", [(HKCU, PATH, NAME)])
    snap = snap_box.get("med008b") or {}
    ck("recorded as genuinely absent",
       snap.get("0:present") is False and not snap.get("0:denied"),
       {k: v for k, v in snap.items() if ":" in k})
    ctx = Ctx()
    tt._restore_reg_values(ctx, "med008b")
    ck("Undo DOES delete it, because it really was absent",
       h2.deleted == [(HKCU, PATH, NAME)], h2.deleted)

print()
print("[5] structural: the docstring and the catch-all agree with the code")
import inspect  # noqa: E402

src = inspect.getsource(u.reg_get_value_typed)
ck("MED-008 is documented", "MED-008" in src)
ck("the not-found codes are named as the ONLY absent case",
   "in (2, 3)" in src)
ck("the bare `except Exception` no longer claims absence",
   src.count("return _REG_DENIED, None") >= 3,
   src.count("return _REG_DENIED, None"))
ck("the docstring says absent is only for a genuine non-existence",
   "genuinely does not exist" in inspect.getsource(u.reg_get_value_typed))

print()
if FAILS:
    print("MED-008 VERIFY: %d/%d FAILED" % (FAILS, CHECKS))
    sys.exit(1)
print("MED-008 VERIFY: ALL PASS (%d checks)" % CHECKS)
