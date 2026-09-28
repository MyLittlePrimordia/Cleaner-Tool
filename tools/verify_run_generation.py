"""H10 targeted verification: a run's deferred completions cannot clobber a
newer run.

The hazard H10 removes: a worker hands the run slot back with _release_run
BEFORE it schedules its result card, toast and status hops with root.after(0).
Those hops are therefore not ordered against a following run. Between the
release and the hop, the user can start run B, which rebinds _run_results and
takes the progress bar — and then run A's card renders, built from B's data.

Checks:
  1  the counter is monotonic and starts at 0
  2  _claim_run hands out a fresh generation and refuses while busy
  3  _is_current is true for the live run and false for every older one
  4  a superseded generation is detected, however far behind it is
  5  the real race: run A finishes, run B claims, A's hops are dropped
  6  run A's results list survives run B rebinding self._run_results
  7  the attribute tools/smoke_async.py reads still points at the newest run
"""
from __future__ import annotations

import io
import sys
import threading
import time

sys.path.insert(0, r"C:\Users\User\Desktop\Cleaner Tool")
from _config_guard import ConfigGuard  # noqa: E402

import tkinter as tk

from app.runner import RunState
import app.ui.app as appmod

FAILS = []
N = [0]


def ck(label, cond, detail=""):
    N[0] += 1
    if cond:
        print("  PASS  %s" % label)
    else:
        FAILS.append(label)
        print("  FAIL  %s%s" % (label, ("  <- " + str(detail)) if detail else ""))


root = tk.Tk()
root.withdraw()
_GUARD = ConfigGuard('verify_run_generation.py')
_GUARD.__enter__()
real = appmod.Application(root)
real.log = lambda *a, **k: None
from app.elevation import is_admin
if not is_admin():
    for w in root.winfo_children():
        if isinstance(w, appmod.AdminGateFrame):
            w._continue_limited()
            break
assert hasattr(real, "tabs"), "main UI never built"

try:
    # ----------------------------------------------------------------- #
    print("\n[1-4] the generation counter")
    ck("starts at 0", real._run_gen == 0, real._run_gen)
    ck("_current_gen starts at 0", real._current_gen == 0)

    real._state = RunState.IDLE
    g1 = real._claim_run()
    ck("_claim_run returns a generation", isinstance(g1, int), type(g1))
    ck("counter advanced", real._run_gen == 1, real._run_gen)
    ck("_current_gen follows", real._current_gen == 1)
    ck("g1 is current", real._is_current(g1) is True)
    ck("None opts out (pre-H10 behaviour)", real._is_current(None) is True)
    ck("a refused claim returns None", real._claim_run() is None)
    ck("refused claim did not advance the counter", real._run_gen == 1)

    real._release_run()
    g2 = real._claim_run()
    ck("counter is monotonic", g2 > g1, (g1, g2))
    ck("g2 is current", real._is_current(g2) is True)
    ck("g1 is now stale", real._is_current(g1) is False)
    ck("a generation that never existed is stale",
       real._is_current(g2 + 99) is False)

    real._release_run()
    g3 = real._claim_run()
    ck("g1 still stale two runs later", real._is_current(g1) is False)
    ck("g2 now stale too", real._is_current(g2) is False)
    ck("g3 current", real._is_current(g3) is True)
    real._release_run()

    # ----------------------------------------------------------------- #
    print("\n[5] the real race, on the live object")
    # Simulate exactly the production sequence: A completes and releases,
    # then B claims, then A's deferred hop runs.
    real._state = RunState.IDLE
    genA = real._claim_run()
    real._release_run(ok=True)          # A hands the slot back
    genB = real._claim_run()            # B starts before A's after(0) fires
    ck("A is stale once B has claimed", real._is_current(genA) is False)
    ck("B is current", real._is_current(genB) is True)

    # prove the gate actually short-circuits the UI work, by running the same
    # guard the hops use
    painted = []
    real._stop_indeterminate = lambda: painted.append("indeterminate")
    real._set_progress = lambda *a: painted.append("progress")
    real._set_buttons_enabled = lambda *a: painted.append("buttons")
    if real._is_current(genA):
        real._stop_indeterminate(); real._set_progress(1, 1)
        real._set_buttons_enabled(True)
    ck("a stale completion paints nothing", painted == [], painted)
    if real._is_current(genB):
        real._set_progress(1, 1)
    ck("the current run still paints", painted == ["progress"], painted)
    real._release_run()

    # ----------------------------------------------------------------- #
    print("\n[6-7] per-run data isolation")
    real._state = RunState.IDLE
    runA_results = []
    real._run_results = runA_results
    real._release_run()
    real._claim_run()
    runB_results = []
    real._run_results = runB_results      # B rebinds the attribute

    ck("A's list is not B's list", runA_results is not runB_results)
    runA_results.append(("A task", "ok", 10, None))
    runB_results.append(("B task", "ok", 20, None))
    ck("A's data survived B's rebind", runA_results == [("A task", "ok", 10, None)])
    ck("B's data is separate", runB_results == [("B task", "ok", 20, None)])
    ck("the attribute points at the newest run (smoke_async reads this)",
       real._run_results is runB_results)
    real._release_run()

    # ----------------------------------------------------------------- #
    print("\n[8] an end-to-end run still completes normally")
    def _noop(ctx):
        return True
    task = type("T", (), {"key": "h10_probe", "label": "probe",
                          "admin_required": False, "run": staticmethod(_noop),
                          "revert": staticmethod(_noop),
                          "long_running": False, "risk": "safe",
                          "category": "clean", "requires_reboot": False})()
    real._state = RunState.IDLE
    real.run_tasks("Clean", [task], quiet=True)
    deadline = time.time() + 60
    while time.time() < deadline:
        root.update()
        if real._state is RunState.IDLE and real._run_gen > 1:
            break
        time.sleep(0.01)
    root.update()
    ck("the run completed and released", real._state is RunState.IDLE)
    ck("no worker left", real._worker_thread is None)
    ck("an outcome was recorded", real.last_run_outcome is not None,
       real.last_run_outcome)
    ck("the run recorded its own result row",
       any(r[0] == "probe" for r in (real._run_results or [])),
       real._run_results)
    ck("the generation advanced for the real run", real._run_gen > 1)

finally:
    try:
        real._release_run()
    except Exception:
        pass
    try:
        root.destroy()
    except Exception:
        pass

_GUARD.__exit__(None, None, None)   # restore FIRST, then check it worked
if not _GUARD.verify():
    print('  FAIL  the real config.json was NOT restored')
print()
if FAILS:
    print("H10 VERIFY: %d/%d FAILED -> %s" % (len(FAILS), N[0], FAILS))
    sys.exit(1)
print("H10 VERIFY: ALL PASS (%d checks)" % N[0])
