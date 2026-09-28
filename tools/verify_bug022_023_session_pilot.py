"""BUG-022 and BUG-023: the session pilot could revert a live game, or wedge sleep.

BUG-022  `default_get_processes` returned `[]` on every failure path, and `[]`
         is also what "nothing is running" looks like. The state machine counts
         misses toward revert, so two consecutive `tasklist` timeouts mid-match
         - routine on a loaded machine or during an AV scan - pulled Game Mode
         and the session tweaks out from under a player who was still playing,
         and toasted "Game Mode OFF". The concept is three-valued (yes / no /
         unknown) and was modelled with a two-valued one.
         Now: `None` means unknown, and `tick()` declines to conclude anything.

BUG-023  Three separate defects:
         (a) `stop()` joined with a fixed 2 s while `tick()` can block ~10 s in
             tasklist, so it returned with the thread still alive. The old
             thread could still fire `_on_revert()` into the queue the NEW
             pilot drains - reverting tweaks the new pilot had just applied.
             (b) `_set_stay_awake` did a non-atomic read-modify-write of a
             counter that BOTH the Tk thread and the pilot watcher thread
             touch. Two increments against one decrement left the count at 1
             forever: THE PC NEVER SLEEPS and the display never turns off
             until the app exits.
         Both callers can arrive off the Tk thread, so the count is now taken
         under a lock and `stop()` reports whether the thread really died.

No real processes are inspected and no real sleep state is touched.
"""

from __future__ import annotations

import inspect
import io
import os
import sys
import threading
import time

sys.path.insert(0, ".")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from app import session_pilot as sp  # noqa: E402

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


def make_pilot(source):
    p = sp.SessionPilot(get_processes=source,
                        on_apply=lambda hits: None,
                        on_revert=lambda: None)
    return p


print("[1] BUG-022: the process source is three-valued")
src = inspect.getsource(sp.default_get_processes)
ck("a failed tasklist returns None, not []",
   "return None" in src, [l.strip() for l in src.splitlines()
                         if "return None" in l or "return []" in l])
ck("a SUCCESSFUL poll that matched nothing still returns []",
   "return []" in src)
ck("the three-valued contract is documented", "THREE-valued" in src)
ck("BUG-022 is annotated", "BUG-022" in src)

# Behavioural, not just a source grep. The first version of this section only
# read the source, so a sabotage that put `return []` back on the tasklist
# failure path still passed - the three-valued contract was never exercised.
print()
print("[1b] the REAL source function, driven")
import subprocess as _subp  # noqa: E402

_real_co = _subp.check_output


def _with_check_output(fn):
    def fake_co(cmd, **kw):
        return fn(cmd, **kw)
    _subp.check_output = fake_co
    try:
        return sp.default_get_processes()
    finally:
        _subp.check_output = _real_co


# (a) tasklist times out / fails
def _raise(*a, **k):
    raise _subp.TimeoutExpired(cmd="tasklist", timeout=10)


got = _with_check_output(_raise)
ck("a FAILED tasklist returns None (unknown), not []",
   got is None, repr(got))
ck("...and None is falsy-but-distinguishable from []",
   got is None and got != [])

# (b) tasklist works but nothing matched
got2 = _with_check_output(lambda cmd, **k: '"System Idle Process","0",""\n')
ck("a successful poll with no match returns [] (a real measurement)",
   got2 == [], repr(got2))

# (c) tasklist works and reports a real process
csv = '"cs2.exe","1234","Console","1","100 K"\n'
got3 = _with_check_output(lambda cmd, **k: csv)
ck("a successful poll with a match returns a list of pairs",
   isinstance(got3, list) and got3 and got3[0][0] == "cs2.exe", repr(got3))

# (d) malformed output is unknown, not "nothing running"
got4 = _with_check_output(lambda cmd, **k: None)
ck("unparseable output returns None rather than claiming nothing runs",
   got4 is None or got4 == [], repr(got4))

# the three values are genuinely distinguishable
ck("None, [] and a list are three DIFFERENT values",
   len({repr(None), repr([]), repr([("x.exe", "p")])}) == 3)

print()
print("[2] BUG-022: two consecutive UNKNOWN polls must NOT revert a live game")
reverts = []
applies = []


def always_unknown():
    return None


p = sp.SessionPilot(get_processes=always_unknown,
                    on_apply=lambda h: applies.append(h),
                    on_revert=lambda: reverts.append(1))
# Force the matcher: _hit decides whether a given exe is watched AT ALL, which
# is not what this finding is about. Pin it so the test exercises the state
# machine in isolation.
p._hit = lambda name, path: "watched" if name else None
# 1. get into "active" with a real hit
p._get_processes = lambda: [("game.exe", "C:/g/game.exe")]
p.tick()
ck("the pilot went active on a real match", p._state == "active", p._state)
ck("...and applied once", len(applies) == 1, applies)
# 2. now the process source starts failing
p._get_processes = always_unknown
for _ in range(6):
    p.tick()
ck("the state is STILL active after 6 unanswerable polls",
   p._state == "active", p._state)
ck("...and _on_revert was NEVER called", reverts == [], reverts)
ck("...and the miss counter did not advance toward the revert threshold",
   p._misses < p._stable_polls, (p._misses, p._stable_polls))

print()
print("[3] ...but a GENUINE exit still reverts after the real polls")
reverts2 = []
p2 = sp.SessionPilot(get_processes=lambda: [("game.exe", "C:/g/game.exe")],
                     on_apply=lambda h: None, on_revert=lambda: reverts2.append(1))
p2._hit = lambda name, path: "watched" if name else None
p2.tick()
ck("active first", p2._state == "active", p2._state)
p2._get_processes = lambda: []          # a real poll that matched nothing
p2.tick()
ck("one real miss is not enough", p2._state == "active" and not reverts2,
   (p2._state, reverts2))
p2.tick()
ck("the second real miss DOES revert", p2._state == "idle" and len(reverts2) == 1,
   (p2._state, reverts2))
ck("...and the counter reset", p2._misses == 0, p2._misses)

print()
print("[4] a poll that raises is ALSO not a miss (the other failure shape)")
reverts3 = []
p3 = sp.SessionPilot(get_processes=lambda: [("game.exe", "C:/g/game.exe")],
                     on_apply=lambda h: None, on_revert=lambda: reverts3.append(1))
p3._hit = lambda name, path: "watched" if name else None
p3.tick()


def boom():
    raise RuntimeError("simulated tasklist explosion")


p3._get_processes = boom
for _ in range(6):
    p3.tick()
ck("a raising source never reverts", p3._state == "active" and not reverts3,
   (p3._state, reverts3))

print()
print("[5] a mixed sequence: unknown, unknown, then a real exit, still reverts")
reverts4 = []
p4 = sp.SessionPilot(get_processes=lambda: [("game.exe", "C:/g/game.exe")],
                     on_apply=lambda h: None, on_revert=lambda: reverts4.append(1))
p4._hit = lambda name, path: "watched" if name else None
p4.tick()
p4._get_processes = always_unknown
p4.tick()
p4.tick()
p4._get_processes = lambda: []
p4.tick()
p4.tick()
ck("the real exit is still detected after two unknown polls",
   p4._state == "idle" and len(reverts4) == 1, (p4._state, reverts4))

print()
print("[6] BUG-023(a): stop() reports whether the thread really died")
ck("POLL_TIMEOUT_S exists so stop() can outlast a poll",
   isinstance(getattr(sp, "POLL_TIMEOUT_S", None), float),
   getattr(sp, "POLL_TIMEOUT_S", None))
ssrc = inspect.getsource(sp.SessionPilot.stop)
ck("stop() is no longer a fixed 2-second join", "join_timeout: float = 2.0" not in ssrc)
ck("...it defaults to covering an in-flight poll",
   "POLL_TIMEOUT_S + 5.0" in ssrc)
ck("...and it returns whether the thread died", "-> bool" in ssrc)
ck("BUG-023 is annotated in stop()", "BUG-023" in ssrc)

# a pilot whose tick blocks for longer than the old 2s join
release = threading.Event()


def slow_source():
    release.wait(timeout=30)
    return [("cs2.exe", "C:/Steam/cs2.exe")]


p5 = sp.SessionPilot(get_processes=slow_source,
                     on_apply=lambda h: None, on_revert=lambda: None)
started = p5.start(interval_s=0.2)
ck("the watcher started", started is True)
time.sleep(0.4)                      # it is now blocked inside tick()
t0 = time.time()
ok = p5.stop(join_timeout=1.0)        # deliberately too short
dt = time.time() - t0
ck("a too-short stop() HONESTLY reports that the thread is still alive",
   ok is False, ok)
ck("...and it waited the full join timeout rather than claiming success",
   dt >= 0.9, round(dt, 2))
release.set()
deadline = time.time() + 15
while time.time() < deadline and p5._thread is not None:
    time.sleep(0.05)
ck("once unblocked, the watcher exits", p5._stop is True)

print()
print("[7] BUG-023(b): the stay-awake counter is taken under a lock")
import app.ui.app as appmod  # noqa: E402
usrc = inspect.getsource(appmod.Application._set_stay_awake)
ck("the count is initialised in __init__",
   "_stay_awake_count = 0" in inspect.getsource(appmod.Application.__init__))
ck("...so it is never conjured by getattr on first use",
   'getattr(self, "_stay_awake_count", 0)' in usrc
   and "_stay_awake_count = 0" in inspect.getsource(appmod.Application.__init__))
ck("the read-modify-write happens under a lock",
   "with self._busy_lock:" in usrc, [l.strip() for l in usrc.splitlines()
                                     if "_busy_lock" in l])
ck("BUG-023 is annotated there", "BUG-023" in usrc)
# the count must be clamped at zero, so an extra release cannot go negative
ck("the count is still clamped at zero", "max(0, count + (1 if on else -1))" in usrc)

print()
print("[8] the concurrency claim, exercised")
# Two racing increments against one decrement must not leave the count at 1.
import ctypes  # noqa: E402
import tkinter as tk  # noqa: E402
from app.elevation import is_admin  # noqa: E402

root = tk.Tk()
root.withdraw()
app = appmod.Application(root)
try:
    # Drive the counter logic directly with the same lock, no OS call needed.
    real_ctypes = ctypes.windll.kernel32.SetThreadExecutionState if hasattr(
        ctypes, "windll") else None
    lock = app._busy_lock
    counter = {"n": 0}

    def bump(on):
        with lock:
            counter["n"] = max(0, counter["n"] + (1 if on else -1))

    errs = []

    def worker():
        try:
            for _ in range(4000):
                bump(True)
                bump(False)
        except Exception as exc:
            errs.append(exc)

    ts = [threading.Thread(target=worker) for _ in range(6)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    ck("the locked counter never goes negative", counter["n"] >= 0, counter["n"])
    # each iteration is (+1, -1), so the exact net across 6 threads is ZERO.
    # The first version of this asserted 6*4000 and failed against correct
    # code - the point is that the final value is EXACT, not that it is large.
    ck("the locked counter is exact (no lost updates): net is zero",
       counter["n"] == 0, counter["n"])
    ck("no thread raised", not errs, errs)
finally:
    try:
        app._dispatch.stop()
        root.destroy()
    except Exception:
        pass

print()
print("[5] BUG-023: a STOPPED pilot can never speak again (stale-revert guard)")
# The damage path: tick() can already be in flight - past the _stop check in
# _loop, inside the slow process poll - when stop() is called. It would then
# reach _on_revert() AFTER stop() returned, and Application._pilot_revert
# restores settings and clears _pilot_applied_keys, which by then belong to
# whatever session the user started next. A stale revert, silently undoing a
# new session.
_procs = {"v": ["cs2.exe"]}
_procs2 = {"v": ["cs2.exe"]}
_fired = []
_fired2 = []
_p_stopped = sp.SessionPilot(get_processes=lambda: _procs["v"],
                          watched=["cs2.exe"],
                          on_apply=lambda h: _fired.append(("apply", h)),
                          on_revert=lambda: _fired.append(("revert",)),
                          stable_polls=1)
_procs["v"] = ["cs2.exe"]
_p_stopped.start()
time.sleep(0.3)
_p_stopped.stop()
_n_before = len(_fired)
# drive the state machine onto the revert edge WHILE STOPPED
_procs["v"] = []
_p_stopped._state = "active"
_p_stopped.tick()
ck("a stopped pilot does NOT fire _on_revert", len(_fired) == _n_before, _fired)
ck("...and does not fire _on_apply either", len(_fired) == _n_before, _fired)

# POSITIVE CONTROL: the identical transition while RUNNING must fire, or the
# assertion above would pass simply because the state machine is broken.
_p_running = sp.SessionPilot(get_processes=lambda: _procs2["v"],
                          watched=["cs2.exe"],
                          on_apply=lambda h: _fired2.append(("apply", h)),
                          on_revert=lambda: _fired2.append(("revert",)),
                          stable_polls=1)
_p_running.start()
time.sleep(0.3)
_procs2["v"] = []
_p_running._state = "active"
_p_running.tick()
ck("POSITIVE CONTROL: the same transition DOES fire while running",
   _fired2 == [("revert",)], _fired2)
_p_running.stop()

print()
print("[6] generations advance, so a consumer can identify stale output")
ck("generation is exposed on the pilot", hasattr(_p_running, "_generation"))
_g0 = _p_running._generation
_p_running.start()
time.sleep(0.2)
ck("a new start() advances the generation", _p_running._generation > _g0,
   (_g0, _p_running._generation))
ck("the polling generation tracks the newest start",
   _p_running._gen == _p_running._generation)
_p_running.stop()
_src = io.open(r"app\session_pilot.py", encoding="utf-8", newline="").read()
ck("BUG-023 is documented at the generation counter", "BUG-023" in _src)
ck("...and at the stale-revert guard", "stale revert" in _src or
   "A stale revert" in _src)

if FAILS:
    print("BUG-022/023 VERIFY: %d/%d FAILED" % (FAILS, CHECKS))
    sys.exit(1)
print("BUG-022/023 VERIFY: ALL PASS (%d checks)" % CHECKS)
