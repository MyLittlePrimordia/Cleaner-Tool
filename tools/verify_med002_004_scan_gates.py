"""MED-002, MED-003, MED-004: the read-only scan gates that lied.

MED-002  `storage_scan.measure_clean_tasks` swallowed every task exception and
         recorded 0, while its docstring claimed the failure was "logged via
         ctx.set_status" - nothing logged it, and `make_measure_ctx` set
         `log=lambda _m: None`, so even a real log would have been discarded.
         A never-measured category was therefore painted as "0 bytes" and
         graded "A - already empty" by health_scan.grade_junk: the app asserted
         a clean bill of health for something it never looked at.
         Now: None (not 0) for unmeasured, the exception is logged, and the
         0-vs-None distinction survives to the UI.

MED-003  `_looks_like_residual_folder` used `os.path.islink` as its junction
         test. Verified on this platform: islink(<junction>) is False, and
         os.walk(<junction>, followlinks=False) DESCENDS. So the prune was a
         no-op for exactly the case it existed to stop, and a real program
         folder could be scored "high-confidence orphan leftover" - deletable.
         Verified empirically here, with a real `mklink /J`.

MED-004  `_is_driver_leftover_folder` inspected only the folder ROOT, and its
         docstring listed "version-numbered subfolders" as disposable - which
         is backwards, because that is exactly where modern NVIDIA/AMD
         extraction stages nvlddmkm.sys and friends. The gate passed and
         clean_folder_contents deleted a driver package the user might need for
         an offline reinstall. Now it recurses.

Only TEMP directories are created. No real driver, registry or config.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile

sys.path.insert(0, ".")

from app.health_scan import grade_junk            # noqa: E402
from app.storage_scan import (count_unmeasured, make_measure_ctx,  # noqa: E402
                              measure_clean_tasks, sum_measured)
from app.tasks.clean_tasks import (_is_driver_leftover_folder,  # noqa: E402
                                   _looks_like_residual_folder)

FAILS = 0
CHECKS = 0
BASE = os.path.join(tempfile.gettempdir(), "opencode", "verify_med002_004")


def ck(label, cond, detail=""):
    global FAILS, CHECKS
    CHECKS += 1
    if cond:
        print("  PASS  %s" % label)
    else:
        FAILS += 1
        print("  FAIL  %s%s" % (label, ("  <- " + str(detail)) if detail else ""))


def fresh(name):
    p = os.path.join(BASE, name)
    shutil.rmtree(p, ignore_errors=True)
    os.makedirs(p, exist_ok=True)
    return p


def touch(path, data=b"x"):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(data)


def junction(link, target):
    r = subprocess.run(["cmd", "/c", "mklink", "/J", link, target],
                       capture_output=True, text=True, errors="replace")
    return r.returncode == 0 and os.path.isdir(link)


# ======================================================================= #
print("[1] the premise: islink is the WRONG test for a junction")
real = fresh("RealProgram")
touch(os.path.join(real, "game.exe"))
jdir = os.path.join(BASE, "jdir")
jdir = fresh("jdir")
jlink = os.path.join(jdir, "Data")
ok = junction(jlink, real)
ck("a real junction was created for this test", ok, jlink)
if ok:
    ck("os.path.islink(junction) is FALSE (MED-003's premise)",
       os.path.islink(jlink) is False, os.path.islink(jlink))
    ck("os.walk(junction, followlinks=False) DOES descend",
       any(files for _r, _d, files in os.walk(jlink, followlinks=False)))
    ck("so an islink-based prune is a no-op for junctions", True)

# ======================================================================= #
print()
print("[2] MED-003: a folder whose only subdir is a junction is NOT deletable")
victim = fresh("Residual_App")
ok2 = junction(os.path.join(victim, "Data"), real)
ck("the probe folder really contains just the junction",
   ok2 and os.listdir(victim) == ["Data"], os.listdir(victim))
ck("_looks_like_residual_folder refuses it", not _looks_like_residual_folder(victim),
   _looks_like_residual_folder(victim))
ck("the real files behind the junction are still there",
   os.path.exists(os.path.join(real, "game.exe")))

print()
print("[3] MED-003: a folder with a junction AND log files is still not deletable")
mixed = fresh("Mixed_App")
touch(os.path.join(mixed, "logs", "x.log"))
junction(os.path.join(mixed, "Data"), real)
ck("_looks_like_residual_folder refuses it", not _looks_like_residual_folder(mixed),
   _looks_like_residual_folder(mixed))

print()
print("[4] MED-003: genuine leftovers are STILL detected (no over-blocking)")
empty = fresh("Empty_App")
ck("an empty folder is still residual", _looks_like_residual_folder(empty))
logs = fresh("Logs_App")
touch(os.path.join(logs, "logs", "a.log"))
ck("a logs-only folder is still residual", _looks_like_residual_folder(logs))
prog = fresh("RealProgram2")
touch(os.path.join(prog, "app.exe"))
ck("a real program folder is still not residual",
   not _looks_like_residual_folder(prog))

# ======================================================================= #
print()
print("[5] MED-004: a versioned SUBDIRECTORY driver package is NOT deletable")
nv = fresh("NVIDIA")
touch(os.path.join(nv, "setup.cfg"))                        # debris at root
touch(os.path.join(nv, "Display.Nv.Container", "560.94", "nvlddmkm.sys"))
ck("the root holds no driver binary (this is why the old gate passed)",
   not any(os.path.splitext(f)[1].lower() in (".sys", ".dll", ".inf", ".cat")
           for f in os.listdir(nv)))
ck("_is_driver_leftover_folder now REFUSES it",
   not _is_driver_leftover_folder(nv), "returned True - the driver would be deleted")

print()
print("[6] MED-004: the old root-level case is still caught, plus .inf subdirs")
amd = fresh("AMD")
touch(os.path.join(amd, "amdkmdag.sys"))
ck("a root-level .sys is still refused", not _is_driver_leftover_folder(amd))
intel = fresh("Intel")
touch(os.path.join(intel, "Driver", "13.0", "igdlk64.inf"))
ck("a .inf in a versioned subdir is refused", not _is_driver_leftover_folder(intel))
debris = fresh("ATI")
touch(os.path.join(debris, "logs", "install.log"))
touch(os.path.join(debris, "readme.txt"))
ck("pure debris is still deletable", _is_driver_leftover_folder(debris))
ck("a nonexistent folder is refused",
   not _is_driver_leftover_folder(os.path.join(BASE, "does_not_exist")))

print()
print("[7] MED-004: the walk is bounded and reparse-safe")
big = fresh("BigDriver")
for i in range(30):
    touch(os.path.join(big, "d%d" % i, "readme.txt"))
ck("a huge debris tree with no binaries is still deletable",
   _is_driver_leftover_folder(big, max_entries=20000))
ck("...but it is REFUSED when the entry cap is hit (cannot certify)",
   not _is_driver_leftover_folder(big, max_entries=5))
esc = fresh("EscapingDriver")
touch(os.path.join(esc, "logs", "a.log"))
junction(os.path.join(esc, "escape"), real)
ck("a junction inside the tree is not followed",
   _is_driver_leftover_folder(esc),
   "the walk escaped through a junction and saw game.exe")

# ======================================================================= #
print()
print("[8] MED-002: 0 and None mean different things downstream")
ck("grade_junk(0) says 'A - already empty' (a REAL measurement)",
   grade_junk(0) == "A", grade_junk(0))
ck("grade_junk(None) says '?' (never measured)",
   grade_junk(None) == "?", grade_junk(None))
ck("so returning 0 for a failure fakes a healthy machine",
   grade_junk(0) != grade_junk(None))
ck("sum_measured propagates unknownness", sum_measured({"a": 5, "b": None}) is None)
ck("sum_measured totals when everything is known",
   sum_measured({"a": 5, "b": 7}) == 12)
ck("count_unmeasured counts them", count_unmeasured({"a": 5, "b": None}) == 1)
ck("sum_measured of an empty scan is 0, not None",
   sum_measured({}) == 0)

print()
print("[9] MED-002: measure_clean_tasks returns None for a raising task, and LOGS")


class _Task:
    def __init__(self, key, fn):
        self.key, self.run = key, fn


def _boom(ctx):
    raise RuntimeError("simulated scan failure")


_real_by_key = None
import app.storage_scan as ss  # noqa: E402

_real_by_key = ss._clean_task_by_key
_real_allow = ss.SCAN_ALLOWLIST
try:
    ss._clean_task_by_key = lambda: {
        "good": _Task("good", lambda ctx: 4096),
        "bad": _Task("bad", _boom),
    }
    ss.SCAN_ALLOWLIST = {"good", "bad"}
    seen = []
    ctx = make_measure_ctx(set_status=seen.append)
    sizes = measure_clean_tasks(ctx, ["good", "bad"])
    ck("the healthy task measured normally", sizes.get("good") == 4096, sizes)
    ck("the failing task is None, NOT 0", sizes.get("bad") is None, sizes)
    ck("...which is what turns grade 'A' into '?'",
       grade_junk(sum_measured(sizes)) == "?", grade_junk(sum_measured(sizes)))
    ck("the failure was actually LOGGED (the docstring's old false claim)",
       any("could not measure" in m for m in seen), seen)
    ck("...and the log names the failing key and the reason",
       any("bad" in m and "simulated scan failure" in m for m in seen), seen)
finally:
    ss._clean_task_by_key = _real_by_key
    ss.SCAN_ALLOWLIST = _real_allow

print()
print("[10] MED-002: make_measure_ctx no longer discards the log")
msgs = []
ctx = make_measure_ctx(set_status=msgs.append)
ctx.log("this used to be swallowed by log=lambda _m: None")
ck("ctx.log now reaches the status line", msgs == ["this used to be swallowed by log=lambda _m: None"], msgs)
quiet = make_measure_ctx()
try:
    quiet.log("no status wired up")
    ck("it still works with no status callback at all", True)
except Exception as exc:
    ck("it still works with no status callback at all", False, exc)

print()
print("[11] structural: the 0-vs-None distinction reaches the UI")
import ast  # noqa: E402
import inspect  # noqa: E402

src = inspect.getsource(ss.measure_clean_tasks)
ck("measure_clean_tasks assigns None (not 0) on failure",
   "out[key] = None" in src, [l for l in src.splitlines() if "out[key]" in l])
ck("...and logs the exception", "could not measure" in src)
ck("MED-002 is documented", "MED-002" in src)
ck("make_measure_ctx forwards log", "log=lambda m: _status(m)" in inspect.getsource(ss.make_measure_ctx))

import app.ui.tabs.tasktab as tt  # noqa: E402
import app.ui.dialogs.storage_insight_dialog as sid  # noqa: E402
_tt_src = inspect.getsource(tt)
ck("tasktab sums via sum_measured, not a bare sum() over the sizes",
   "total = sum_measured(sizes)" in _tt_src and "total = sum(sizes.values())" not in _tt_src,
   [l.strip() for l in _tt_src.splitlines() if "sizes" in l and "sum" in l])
ck("tasktab counts the unmeasured categories", "count_unmeasured(sizes)" in _tt_src)
ck("tasktab's Preview reports an incomplete measurement",
   "Preview is incomplete" in inspect.getsource(tt.TaskTab._preview_done))
ck("storage insight never paints an unknown category as clean",
   "unknown" in inspect.getsource(sid.StorageInsightDialog._paint_category))
ck("storage insight still shows 'clean' for a real zero",
   '"✓ clean"' in inspect.getsource(sid.StorageInsightDialog._paint_category))

ct = inspect.getsource(_looks_like_residual_folder)
ck("MED-003 is documented", "MED-003" in ct)
ck("...and uses _is_reparse_point, not os.path.islink",
   "_is_reparse_point" in ct and "os.path.islink(os.path.join(root" not in ct)
dl = inspect.getsource(_is_driver_leftover_folder)
ck("MED-004 is documented", "MED-004" in dl)
ck("...and the gate now recurses with os.walk", "os.walk" in dl)

print()
shutil.rmtree(BASE, ignore_errors=True)
if FAILS:
    print("MED-002/003/004 VERIFY: %d/%d FAILED" % (FAILS, CHECKS))
    sys.exit(1)
print("MED-002/003/004 VERIFY: ALL PASS (%d checks)" % CHECKS)
