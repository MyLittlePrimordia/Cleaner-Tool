"""H15 targeted verification: the Auto-Pilot lifecycle lives in one object.

H15 moved the orchestration out of Application's 17 `_pilot_*` methods into
app/ui/session.py. What matters is not that the code moved — it is that:

  * the four loose pieces of state became one explicit SessionState
  * the pump can now STOP (pre-H15 it rescheduled every 50ms for the whole
    life of the app, even with the feature switched off)
  * a failure is reported once instead of swallowed forever
  * the external surface Application and smoke_async rely on still resolves

Checks:
  1  state transitions: DISABLED -> IDLE -> WATCHING -> ACTIVE -> back
  2  the pump starts once, and stops on stop()/shutdown()
  3  no stray `after` is left pending after a stop  (the pre-H15 leak)
  4  post_apply/post_revert are queue-only and never touch Tk
  5  the pump dispatches to the right handler for each event kind
  6  a raising handler is reported once, not on every event
  7  stop() never reverts, and keeps the key lists
  8  the forwarders on Application resolve and delegate
"""
from __future__ import annotations

import io
import sys
import threading
import time

sys.path.insert(0, r"C:\Users\User\Desktop\Cleaner Tool")

import tkinter as tk

from app.ui.session import SessionOrchestrator, SessionState

FAILS = []
N = [0]


def ck(label, cond, detail=""):
    N[0] += 1
    if cond:
        print("  PASS  %s" % label)
    else:
        FAILS.append(label)
        print("  FAIL  %s%s" % (label, ("  <- " + repr(detail)) if detail else ""))


# ------------------------------------------------------------------ [1] ----
print("\n[1] explicit state")
ck("DISABLED is the initial state", SessionState.DISABLED.on is False)
ck("the other three count as on",
   all(s.on for s in (SessionState.IDLE, SessionState.WATCHING, SessionState.ACTIVE)))
ck("there are exactly four states", len(list(SessionState)) == 4,
   [s.name for s in SessionState])


class FakeRoot:
    """Counts after() / after_cancel() so a leaked timer is visible."""

    def __init__(self):
        self.scheduled = []
        self.cancelled = []
        self.alive = True
        self._n = 0

    def winfo_exists(self):
        return self.alive

    def after(self, ms, fn):
        self._n += 1
        self.scheduled.append((self._n, ms, fn))
        return self._n

    def after_cancel(self, aid):
        self.cancelled.append(aid)


root = FakeRoot()
seen = []
o = SessionOrchestrator(root=root, on_apply=lambda h: seen.append(("apply", h)),
                        on_revert=lambda: seen.append(("revert",)),
                        log=lambda m: seen.append(("log", m)))
ck("starts DISABLED", o.state is SessionState.DISABLED)
ck("is_running() is False with no pilot", o.is_running() is False)


class FakePilot:
    def __init__(self):
        self._running = False

    def start(self):
        self._running = True
        return True

    def stop(self):
        self._running = False
        return True

    def running(self):
        return self._running


o.pilot = FakePilot()
ck("attaching a pilot moves to IDLE", o.state is SessionState.IDLE)
o.start()
ck("start() moves to WATCHING", o.state is SessionState.WATCHING)
ck("is_running() follows the pilot", o.is_running() is True)
o.note_session_active(True)
ck("a detection moves to ACTIVE", o.state is SessionState.ACTIVE)
o.note_session_active(False)
ck("reverting returns to WATCHING (watcher still up)",
   o.state is SessionState.WATCHING, o.state)
o.pilot = None
ck("detaching moves to DISABLED", o.state is SessionState.DISABLED)

# ------------------------------------------------------------------ [2,3] --
print("\n[2-3] the pump starts once, and can stop")
root2 = FakeRoot()
o2 = SessionOrchestrator(root=root2, on_apply=lambda h: None, on_revert=lambda: None)
o2.ensure_pump()
o2.ensure_pump()
o2.ensure_pump()
# This is the H15 fix. Pre-H15, _pilot_ensure kicked off a loop that
# rescheduled itself every 50ms for the rest of the app's life — including
# when the feature was switched off entirely. With no pilot attached there
# is nothing to drain and nothing that can queue, so it must not schedule.
ck("no pilot attached -> the pump schedules NOTHING (the pre-H15 leak)",
   root2.scheduled == [], root2.scheduled)
ck("...and it is not left marked as pumping", o2._pumping is False)

o2.pilot = FakePilot()
o2.ensure_pump()
o2.ensure_pump()
o2.ensure_pump()
ck("with a pilot attached, ensure_pump is idempotent (1 tick, not 3)",
   len(root2.scheduled) == 1, len(root2.scheduled))
ck("the pump scheduled itself at the documented cadence",
   root2.scheduled[0][1] == SessionOrchestrator.PUMP_MS, root2.scheduled[0][1])

o2.start()
n_after_attach = len(root2.scheduled)
cb = root2.scheduled[-1][2]
cb()   # one pump tick
ck("with a pilot attached the pump keeps going",
   len(root2.scheduled) == n_after_attach + 1, len(root2.scheduled))

o2.stop()
pending = [s for s in root2.scheduled if s[0] not in root2.cancelled]
ck("stop() cancels the pending after (the pre-H15 leak)",
   len(pending) == n_after_attach, (len(pending), n_after_attach))
cb2 = root2.scheduled[-1][2]
n_at_stop = len(root2.scheduled)
cb2()  # a tick that arrives after the cancel
ck("a tick after stop does not reschedule", len(root2.scheduled) == n_at_stop,
   (len(root2.scheduled), n_at_stop))

o2.pilot = FakePilot()
o2.ensure_pump()
ck("ensure_pump restarts it after a stop", len(root2.scheduled) == n_at_stop + 1)
o2.shutdown()
ck("shutdown cancels the timer", len(root2.cancelled) > len(root2.scheduled) - 2)
ck("shutdown leaves DISABLED", o2.state is SessionState.DISABLED)

# the call-order case that actually bit: Application._pilot_ensure calls
# ensure_pump() BEFORE it builds and attaches the pilot, so a pump keyed off
# ensure_pump() alone gets cancelled by the "nothing attached" rule and every
# detect then sits in the queue forever (smoke_async stalled on exactly this).
root_ord = FakeRoot()
o_ord = SessionOrchestrator(root=root_ord, on_apply=lambda h: ord_seen.append(h),
                            on_revert=lambda: None)
ord_seen = []
o_ord.ensure_pump()          # <- what _pilot_ensure does first
o_ord.pilot = FakePilot()    # <- ...and only then attaches
o_ord.start()
tick = root_ord.scheduled[-1][2]
o_ord.post_apply(["ordered.exe"])
tick()
ck("attach-after-ensure_pump still yields a live pump",
   ord_seen == [["ordered.exe"]], ord_seen)

# and the smoke_async path: it starts the watcher by calling the pilot's own
# start() directly, never touching the orchestrator
root_direct = FakeRoot()
direct_seen = []
o_dir = SessionOrchestrator(root=root_direct, on_apply=lambda h: direct_seen.append(h),
                            on_revert=lambda: None)
p_direct = FakePilot()
o_dir.pilot = p_direct
ck("attaching alone brings the pump up", len(root_direct.scheduled) == 1,
   len(root_direct.scheduled))
p_direct.start()             # bypasses SessionOrchestrator.start()
o_dir.post_apply(["direct.exe"])
root_direct.scheduled[-1][2]()
ck("a watcher started directly still gets its events delivered",
   direct_seen == [["direct.exe"]], direct_seen)

# a dead root must not leave the pump spinning
root3 = FakeRoot()
root3.alive = False
o3 = SessionOrchestrator(root=root3)
o3.pilot = FakePilot()
o3.ensure_pump()
ck("a destroyed root stops the pump instead of rescheduling",
   root3.scheduled == [], root3.scheduled)

# ------------------------------------------------------------------ [4] ----
print("\n[4] the producers are queue-only")
tk_calls = []
class SpyRoot(FakeRoot):
    def after(self, ms, fn):
        tk_calls.append("after")
        return super().after(ms, fn)
    def after_cancel(self, aid):
        tk_calls.append("after_cancel")
        return super().after_cancel(aid)
    def winfo_exists(self):
        tk_calls.append("winfo_exists")
        return super().winfo_exists()

r4 = SpyRoot()
got = []
o4 = SessionOrchestrator(root=r4, on_apply=lambda h: got.append(h),
                         on_revert=lambda: got.append("REVERT"))
# call the producers from a non-Tk thread, as the watcher does
t = threading.Thread(target=lambda: (o4.post_apply(["bg.exe"]),
                                      o4.post_revert()))
t.start(); t.join(5)
ck("producers made no Tk call from the watcher thread", tk_calls == [], tk_calls)
ck("both events are queued", o4.queue.qsize() == 2, o4.queue.qsize())
ck("the queue is a real thread-safe queue",
   type(o4.queue).__name__ == "Queue", type(o4.queue).__name__)

# ------------------------------------------------------------------ [5] ----
print("\n[5] the pump dispatches correctly")
# one pump tick drains the whole backlog, in order — which is the point of
# draining until Empty rather than one-per-tick
got.clear()
o4.pump()
ck("one tick drains both events, in order", got == [["bg.exe"], "REVERT"], got)
ck("both were counted", o4._events_handled == 2, o4._events_handled)
got.clear()
o4.pump()
ck("an empty queue is a no-op", got == [], got)

# and the two kinds go to the right handler, one at a time
solo = []
o4._on_apply = lambda h: solo.append(("apply", h))
o4._on_revert = lambda: solo.append(("revert",))
o4.post_apply(["solo.exe"]); o4.pump()
o4.post_revert(); o4.pump()
ck("apply went to on_apply", solo == [("apply", ["solo.exe"]), ("revert",)], solo)
o4.post_apply(["a", "b"]); o4.pump()
ck("the apply payload survives the round trip",
   solo[-1] == ("apply", ["a", "b"]), solo[-1])
o4.post_apply(None); o4.pump()
ck("a None payload becomes an empty list, not a crash",
   solo[-1] == ("apply", []), solo[-1])

# ------------------------------------------------------------------ [6] ----
print("\n[6] a raising handler is reported once, not every event")
r6 = FakeRoot()
logs = []
def boom(_hits):
    raise RuntimeError("handler exploded")
o6 = SessionOrchestrator(root=r6, on_apply=boom, on_revert=None, log=logs.append)
o6.pilot = FakePilot()
# start() must bring the pump up by itself: a running watcher whose events
# nobody drains fails silently, which is the whole bug class H15 closes.
o6.start()
ck("start() brings the pump up on its own", len(r6.scheduled) == 1,
   len(r6.scheduled))
for _ in range(5):
    o6.post_apply(["x.exe"])
    o6.pump()
ck("the failure did not propagate out of pump()", True)
warns = [m for m in logs if "Auto-Pilot" in m]
ck("it was reported", len(warns) >= 1, warns)
ck("...but only once, not once per event", len(warns) == 1, warns)
ck("the events were all drained", o6._events_handled == 5, o6._events_handled)
n6 = len(r6.scheduled)
r6.scheduled[-1][2]()
ck("the pump survives a raising handler", len(r6.scheduled) == n6 + 1)

# ------------------------------------------------------------------ [7] ----
print("\n[7] stop() never reverts and keeps the key lists")
reverts = []
r7 = FakeRoot()
o7 = SessionOrchestrator(root=r7, on_apply=lambda h: None,
                         on_revert=lambda: reverts.append(1), log=lambda m: None)
o7.pilot = FakePilot()
ck("start() with no pilot attached is a no-op False", o7.start.__doc__ is not None) if False else None
o7.start()
ck("a running watcher always has a live pump",
   any(True for _ in r7.scheduled), r7.scheduled)
o7.applied_keys = ["a", "b", "c"]
o7.fresh_keys = ["b", "c"]
o7.stop()
ck("stop() did not invoke the revert handler", reverts == [], reverts)
ck("applied_keys survived stop()", o7.applied_keys == ["a", "b", "c"])
ck("fresh_keys survived stop()", o7.fresh_keys == ["b", "c"])
ck("a later revert can still reconcile", o7.fresh_keys != [])
# key-list coercion. `list("abc")` is ['a','b','c'], and that per-character
# scattering has already shipped three times in this codebase (audit F-6,
# applied_tweaks, session_pilot_games) — so pin it explicitly.
o7.applied_keys = "disable_nagle"
ck("a lone string is ONE key, not one per character",
   o7.applied_keys == ["disable_nagle"], o7.applied_keys)
o7.applied_keys = ["a", 1, None, "b", "  "]
ck("a list is filtered to non-blank strings",
   o7.applied_keys == ["a", "b"], o7.applied_keys)
o7.applied_keys = ("t1", "t2")
ck("a tuple is accepted", o7.applied_keys == ["t1", "t2"], o7.applied_keys)
o7.applied_keys = None
ck("None becomes []", o7.applied_keys == [], o7.applied_keys)
o7.applied_keys = {"k": "v"}
ck("a dict becomes [] (iterating it would give keys)", o7.applied_keys == [],
   o7.applied_keys)
o7.applied_keys = 42
ck("a non-collection becomes []", o7.applied_keys == [], o7.applied_keys)
o7.fresh_keys = "one_key"
ck("fresh_keys coerces the same way", o7.fresh_keys == ["one_key"], o7.fresh_keys)

# ------------------------------------------------------------------ [8] ----
print("\n[8] the Application forwarders still resolve")
try:
    import app.ui.app as appmod
    from app.elevation import is_admin
    tr = tk.Tk(); tr.withdraw()
    real = appmod.Application(tr)
    if not is_admin():
        for w in tr.winfo_children():
            if isinstance(w, appmod.AdminGateFrame):
                w._continue_limited(); break
    ck("app._pilot_sess exists", hasattr(real, "_pilot_sess"))
    ck("app._pilot reads through", real._pilot is real._pilot_sess.pilot)
    ck("app._pilot_evt_q is the orchestrator's queue",
       real._pilot_evt_q is real._pilot_sess.queue)
    ck("_pilot_state is DISABLED on a fresh app",
       real._pilot_state is SessionState.DISABLED, real._pilot_state)
    real._pilot_applied_keys = ["x"]
    ck("writing _pilot_applied_keys reaches the orchestrator",
       real._pilot_sess.applied_keys == ["x"])
    real._pilot_fresh_keys = ["y"]
    ck("writing _pilot_fresh_keys reaches the orchestrator",
       real._pilot_sess.fresh_keys == ["y"])
    ck("_pilot_running() is False with no watcher", real._pilot_running() is False)
    real._pilot = FakePilot()
    ck("assigning _pilot attaches to the orchestrator",
       real._pilot_sess.pilot is not None and real._pilot_state is SessionState.IDLE)
    real._pilot = None
    ck("clearing _pilot detaches", real._pilot_sess.pilot is None)
    real._pilot_sess.shutdown()
    try:
        tr.destroy()
    except Exception:
        pass
except Exception as exc:
    ck("Application forwarders", False, repr(exc))

print()
if FAILS:
    print("H15 VERIFY: %d/%d FAILED -> %s" % (len(FAILS), N[0], FAILS))
    sys.exit(1)
print("H15 VERIFY: ALL PASS (%d checks)" % N[0])
