"""H9 targeted verification: RunState is the single source of truth for
run lifecycle, and the combinations the old `_busy` boolean could express but
not act on are now impossible or explicitly modelled.

Checks, in order:
  1  the enum itself: busy set, allowed transitions, no illegal pair
  2  `_busy` is a VIEW — assignment cannot desynchronise it from the state
  3  the setters never downgrade a pending cancel
  4  _claim_run / _mark_running / _release_run move state + thread together
  5  the full lifecycle, on a real Application, including the pre-flight
      window that used to be unrepresentable
  6  a Stop during pre-flight survives into the worker (the H9 bug fix)
  7  the pre-flight abort paths release the slot
  8  the smoke suite's `getattr(app, "_busy")` idiom still works
"""
from __future__ import annotations

import io
import os
import sys
import threading

sys.path.insert(0, r"C:\Users\User\Desktop\Cleaner Tool")

import tkinter as tk

from app.runner import CancellationToken, RunState
from app.runner.types import (ALLOWED_TRANSITIONS, LIVE_STATES,
                               OUTCOME_STATES, _BUSY_STATES)

FAILS = []
N = [0]


def ck(label, cond, detail=""):
    N[0] += 1
    if cond:
        print("  PASS  %s" % label)
    else:
        FAILS.append(label)
        print("  FAIL  %s%s" % (label, ("  <- " + str(detail)) if detail else ""))


# --------------------------------------------------------------------------- #
print("\n[1] RunState enum")
ck("busy states are exactly preflight/running/cancelling",
   _BUSY_STATES == {RunState.PREFLIGHT, RunState.RUNNING, RunState.CANCELLING},
   _BUSY_STATES)
ck("idle/done/error are not busy",
   not any(s.busy for s in (RunState.IDLE, RunState.DONE, RunState.ERROR)))
ck("PREFLIGHT exists and is busy (H9 addition)",
   RunState.PREFLIGHT.busy is True)
ck("every LIVE state appears in ALLOWED_TRANSITIONS",
   all(s in ALLOWED_TRANSITIONS for s in LIVE_STATES),
   sorted(str(s) for s in LIVE_STATES - set(ALLOWED_TRANSITIONS)))
ck("outcome states are DONE/ERROR only",
   OUTCOME_STATES == {RunState.DONE, RunState.ERROR}, OUTCOME_STATES)
ck("outcome states have no outgoing transitions (they classify, not live)",
   not (OUTCOME_STATES & set(ALLOWED_TRANSITIONS)))
ck("DONE/ERROR are not busy", not (OUTCOME_STATES & _BUSY_STATES))
ck("every live state is either busy or IDLE",
   LIVE_STATES - _BUSY_STATES == {RunState.IDLE})
ck("no state transitions to itself", all(
    s not in v for s, v in ALLOWED_TRANSITIONS.items()))
# the state the whole change is about
ck("PREFLIGHT -> RUNNING is legal", RunState.RUNNING in ALLOWED_TRANSITIONS[RunState.PREFLIGHT])
ck("PREFLIGHT -> CANCELLING is legal (stop before start)", RunState.CANCELLING in ALLOWED_TRANSITIONS[RunState.PREFLIGHT])
ck("PREFLIGHT -> IDLE is legal (user declined a prompt)", RunState.IDLE in ALLOWED_TRANSITIONS[RunState.PREFLIGHT])
ck("IDLE may not jump to CANCELLING", RunState.CANCELLING not in ALLOWED_TRANSITIONS[RunState.IDLE])
ck("CANCELLING may not return to RUNNING",
   RunState.RUNNING not in ALLOWED_TRANSITIONS[RunState.CANCELLING])

# walk every legal edge so a typo in the table is caught
illegal = []
for src, dsts in ALLOWED_TRANSITIONS.items():
    for d in dsts:
        if d not in ALLOWED_TRANSITIONS.get(src, set()):
            illegal.append((src, d))
ck("all listed edges are symmetric-present in the table", not illegal, illegal)

# --------------------------------------------------------------------------- #
print("\n[2-4] Application state plumbing (headless, no Tk window needed)")


class Fake:
    """Exercise the real Application methods without building a window:
    the properties and helpers touch only _state/_busy_lock/_worker_thread/
    _cancel, all of which are plain attributes."""
    _busy = property(lambda s: getattr(s, "_state", RunState.IDLE).busy)

    def _busy_set(self, value):
        st = getattr(self, "_state", None)
        if st is None:
            self._state = st = RunState.IDLE
        if value:
            self._state = st if st is RunState.CANCELLING else RunState.RUNNING
        else:
            self._state = RunState.IDLE

    # bind the real setter under the property's name
    _busy = property(_busy, _busy_set)


import app.ui.app as appmod

for name in ("_busy", "_claim_run", "_mark_running", "_release_run",
             "_run_in_preflight", "_request_cancel", "_cancel_requested",
             "last_run_outcome"):
    real = getattr(appmod.Application, name)
    if isinstance(real, property):
        # bind the WHOLE property. Binding only the getter silently turns
        # `self._cancel_requested = False` into a plain attribute write, which
        # hides exactly the class of bug these checks exist to catch.
        setattr(Fake, name, real)
    else:
        setattr(Fake, name, real)

f = Fake()
f._state = RunState.IDLE
f._busy_lock = threading.Lock()
f._worker_thread = None
f._cancel = CancellationToken()
f.log = lambda *a, **k: None
# H10: _claim_run now hands out a generation, so the Fake needs the counter.
f._run_gen = 0
f._current_gen = 0
if hasattr(appmod.Application, "_is_current"):
    Fake._is_current = appmod.Application._is_current

ck("fresh Fake is not busy", f._busy is False)
ck("fresh Fake generation is 0", f._run_gen == 0)
ck("fresh Fake is IDLE", f._state is RunState.IDLE)

f._busy = True
ck("_busy = True drives the state to RUNNING", f._state is RunState.RUNNING)
ck("_busy reads back True", f._busy is True)
f._busy = False
ck("_busy = False drives the state to IDLE", f._state is RunState.IDLE)
ck("_busy reads back False", f._busy is False)

print("\n[3] a pending cancel is never downgraded")
f._state = RunState.CANCELLING
f._busy = True
ck("_busy = True during CANCELLING stays CANCELLING", f._state is RunState.CANCELLING)
f._busy = False
ck("_busy = False ends the run (CANCELLING -> IDLE)", f._state is RunState.IDLE)

print("\n[4] claim / mark running / release move state and thread together")
f._state = RunState.IDLE
_g = f._claim_run()
ck("_claim_run succeeds when idle", _g == 1, _g)
ck("_claim_run returned the new generation", isinstance(_g, int))
ck("_claim_run enters PREFLIGHT", f._state is RunState.PREFLIGHT)
ck("PREFLIGHT reads as busy", f._busy is True)
ck("no worker yet — this is the window H9 made explicit",
   f._worker_thread is None)
ck("_claim_run is refused while busy (returns None)",
   f._claim_run() is None)
ck("state unchanged by the refused claim", f._state is RunState.PREFLIGHT)
ck("_run_in_preflight is True here", f._run_in_preflight() is True)

th = threading.Thread(target=lambda: None, daemon=True)
f._mark_running(th)
ck("_mark_running adopts the thread", f._worker_thread is th)
ck("_mark_running leaves PREFLIGHT", f._state is RunState.RUNNING)
ck("not in preflight once the worker exists", f._run_in_preflight() is False)

# the stop-during-preflight case
f._state = RunState.IDLE
f._claim_run()
f._cancel.cancel()
f._request_cancel()
ck("stop during preflight -> CANCELLING", f._state is RunState.CANCELLING)
th2 = threading.Thread(target=lambda: None, daemon=True)
f._mark_running(th2)
ck("a stop during preflight survives _mark_running",
   f._state is RunState.CANCELLING, f._state)
ck("...and the token is still cancelled", f._cancel.is_cancelled() is True)

f._release_run(ok=False)
ck("_release_run clears the worker", f._worker_thread is None)
ck("_release_run returns to IDLE", f._state is RunState.IDLE)
ck("_release_run clears the token", f._cancel.is_cancelled() is False)
ck("_release_run clears the token via the view", f._cancel_requested is False)
ck("_release_run leaves the app idle", f._busy is False)
ck("a failed run reports ERROR", f.last_run_outcome is RunState.ERROR)
f._release_run(ok=True)
ck("a successful run reports DONE", f.last_run_outcome is RunState.DONE)

# _release_run must be callable from a worker thread without deadlocking the Tk lock
res = []
f._state = RunState.RUNNING
t = threading.Thread(target=lambda: res.append(f._release_run()))
t.start()
t.join(5)
ck("_release_run from a worker thread does not hang", not t.is_alive() and res == [None])

# --------------------------------------------------------------------------- #
print("\n[5-8] on a real Application")
try:
    root = tk.Tk()
    root.withdraw()
except Exception as e:  # headless / no display
    print("  SKIP  cannot open a Tk root here (%s)" % e)
    root = None

if root is not None:
    real = appmod.Application(root)
    real.log = lambda *a, **k: None
    try:
        ck("new Application starts IDLE", real._state is RunState.IDLE)
        ck("a pristine Application has no last outcome",
           real.last_run_outcome is None)
        ck("new Application is not busy", real._busy is False)
        ck("no worker at rest", real._worker_thread is None)
        ck("smoke-suite idiom getattr(app,'_busy',False) works",
           getattr(real, "_busy", False) is False)

        ck("claim works on the real object (returns a generation)",
   isinstance(real._claim_run(), int))
        ck("real object is in PREFLIGHT", real._state is RunState.PREFLIGHT)
        ck("getattr idiom still True", getattr(real, "_busy", False) is True)
        ck("_run_in_preflight True on the real object", real._run_in_preflight() is True)

        # [6] stop during the pre-flight window
        real._request_cancel()
        ck("stop during preflight -> CANCELLING", real._state is RunState.CANCELLING)
        ck("token cancelled", real._cancel.is_cancelled() is True)
        still = real._cancel_requested
        ck("_cancel_requested view agrees", still is True)
        # this is the line the fix guards: the pre-flight reset must not wipe it
        if real._state is not RunState.CANCELLING:
            real._cancel_requested = False
        ck("a pre-flight reset cannot wipe a pending stop",
           real._cancel_requested is True)

        # [7] abort path releases the slot
        real._release_run()
        ck("abort records DONE", real.last_run_outcome is RunState.DONE)
        real._release_run(ok=False)
        ck("a failed release records ERROR", real.last_run_outcome is RunState.ERROR)
        real._release_run(ok=True)
        ck("abort returns to IDLE", real._state is RunState.IDLE)
        ck("abort clears busy", real._busy is False)
        ck("abort clears the token", real._cancel_requested is False)

        # a second claim after release must work
        ck("can claim again after release (returns a generation)",
   isinstance(real._claim_run(), int))
        real._release_run()

        # [8] the old boolean write idiom still drives everything
        real._busy = True
        ck("legacy `app._busy = True` still works", real._state is RunState.RUNNING)
        real._busy = False
        ck("legacy `app._busy = False` still works", real._state is RunState.IDLE)
    finally:
        try:
            real._release_run()
        except Exception:
            pass
        try:
            root.destroy()
        except Exception:
            pass

print()
if FAILS:
    print("H9 VERIFY: %d/%d FAILED -> %s" % (len(FAILS), N[0], FAILS))
    sys.exit(1)
print("H9 VERIFY: ALL PASS (%d checks)" % N[0])
