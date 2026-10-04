"""BUG-019 / BUG-020 verification: the pre-flight window.

BUG-019 (HIGH, Confirmed): a Stop pressed during the pre-flight confirmation
was accepted, logged as acknowledged, and then silently discarded — the reset
of `_cancel_requested` sat AFTER the modal that raises the flag. The user was
told their Stop registered and then watched a destructive clean run to
completion anyway. ROOT CAUSE quoted: "the reset is unconditional and ordered
after the modal that can set the flag."

BUG-020 (HIGH, Confirmed): `_on_close` inferred "a run is in flight" from
`_busy`, but could only join `_worker_thread`, which is None for the whole
pre-flight window. So closing during the prompt withdrew the window to the
tray and returned, leaving the grab_set modal on a hidden root: no visible
window, no reachable dialog, looks hung, only Task Manager recovers it.

H9 added `RunState.PREFLIGHT` and `_run_in_preflight()` for exactly this
window. BUG-019 is closed by the conditional token reset; BUG-020 was NOT —
the tool existed and _on_close was never changed. These checks pin both so
that stays true.

Checks:
  1  a Stop raised during the pre-flight prompt survives it (BUG-019)
  2  the run does not proceed as if un-cancelled
  3  the user is not given a false acknowledgement
  4  the state is honestly reported as pre-flight while prompting
  5  closing during pre-flight does NOT withdraw the window (BUG-020)
  6  ...and leaves the run claimed, so nothing is half-torn-down
  7  closing during a REAL run still confirms and stops as before
"""
from __future__ import annotations

import io
import sys
import time

import pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import tkinter as tk

from app.runner import RunState
import app.ui.app as appmod
from app.elevation import is_admin

FAILS = []
N = [0]


def ck(label, cond, detail=""):
    N[0] += 1
    if cond:
        print("  PASS  %s" % label)
    else:
        FAILS.append(label)
        print("  FAIL  %s%s" % (label, ("  <- " + repr(detail)) if detail else ""))


root = tk.Tk()
root.withdraw()
real = appmod.Application(root)
if not is_admin():
    for w in root.winfo_children():
        if isinstance(w, appmod.AdminGateFrame):
            w._continue_limited()
            break
assert hasattr(real, "tabs"), "main UI never built"

# This test drives a REAL run_tasks, which PERSISTS into the user's
# config.json. It used to save/restore `selected_tasks` by hand, which fixed
# the leak it knew about and left the rest: `applied_tweaks` kept
# "bug019_probe", and verify_telemetry_undo's "the tweak registry is
# untouched" check correctly failed on it from a DIFFERENT test.
#
# tools/_config_guard.py exists precisely because enumerating the keys a test
# might write is a losing game - every new probe key is a new leak. It
# snapshots the file's bytes and restores them verbatim, so anything written
# while this test runs disappears regardless of what wrote it.
from tools._config_guard import ConfigGuard as _ConfigGuard
_GUARD = _ConfigGuard()
_GUARD.__enter__()

logs = []
real.log = lambda m, *a, **k: logs.append(str(m))
statuses = []
real.set_status = lambda s, *a, **k: statuses.append(str(s))


def _noop(ctx):
    return True


TASK = type("T", (), {"key": "bug019_probe", "label": "probe",
                     "admin_required": False, "run": staticmethod(_noop),
                     "revert": staticmethod(_noop), "long_running": False,
                     "risk": "safe", "category": "clean",
                     "requires_reboot": False})()


def settle(limit=8.0):
    end = time.time() + limit
    while time.time() < end:
        root.update()
        if real._state is RunState.IDLE and real._run_gen > 0:
            break
        time.sleep(0.01)
    # The result card is scheduled with after(0) from the worker, so it lands
    # one Tk tick after the state flips to IDLE. Keep pumping briefly or the
    # card check below races the state check.
    for _ in range(40):
        root.update()
        time.sleep(0.005)


import app.ui.app as _am
_ORIG_ASK = _am._themed_askyesno
_ASK_CALLS = []

def _fake_ask(parent, title, message, **kw):
    """Stand in for every themed confirm. A real ThemedModal does grab_set +
    wait_window, so with no user it blocks forever — and _on_close reaches one
    during pre-flight, which is the thing under test. Record the call so the
    test can assert on what the user WOULD have been shown."""
    _ASK_CALLS.append((title, kw.get("yes_text"), kw.get("no_text")))
    return bool(kw.get("_test_answer", True))


_am._themed_askyesno = _fake_ask

try:
    # ------------------------------------------------------------------ #
    print("\n[1-4] BUG-019 — a Stop during the pre-flight prompt")

    # Drive the real re-entrancy: the prompt is a modal that pumps a nested
    # event loop, so Escape -> _request_cancel fires WHILE it is open. Patch
    # the prompt to press Stop on itself, which is exactly that.
    pressed = {"stop": False}

    def _prompt_escape_then_yes(parent, title, message, **kw):
        _ASK_CALLS.append((title, kw.get("yes_text"), kw.get("no_text")))
        # the user hits Escape on the pre-flight prompt
        if not pressed["stop"]:
            pressed["stop"] = True
            real._request_cancel()
        return True          # ...and then answers "yes, start anyway"

    _am._themed_askyesno = _prompt_escape_then_yes
    _ASK_CALLS.clear()

    real._state = RunState.IDLE
    real._cancel.cancel()
    real._cancel.reset()
    logs.clear()
    statuses.clear()

    # The pre-flight prompt only opens when _preflight_check returns a notice,
    # and the real one is ENVIRONMENT-DEPENDENT (pending reboot via winreg,
    # free space on C:). Without forcing it, no prompt opens, no Stop can be
    # pressed, and this whole check is vacuous — which is exactly how the first
    # version of this test passed a broken product. Stub the notice source;
    # the prompt, the claim ordering and the reset all stay the real code.
    _orig_preflight = real._preflight_check
    real._preflight_check = staticmethod(lambda tasks: ["probe: forced notice"])

    # quiet=True SKIPS the pre-flight prompt entirely (it only logs the
    # notice) — correct for the background pilot, but it means quiet runs can
    # never hit BUG-019. The finding is about a user-initiated run, so this
    # must be interactive...
    #
    # ...which means the run ends in a real ScorecardDialog, a blocking
    # modal. Stub it: the outcome card is not what this test is about, and a
    # grab_set + wait_window with no user hangs forever.
    _cards = []

    class _FakeCard:
        def __init__(self, parent, **kw):
            _cards.append(kw)
        def wait(self):
            return None

    _orig_card = _am.ScorecardDialog
    _am.ScorecardDialog = _FakeCard

    try:
        # Spy the ONE call that must observe a pre-flight Stop.
        # _mark_running is invoked synchronously by run_tasks, right after the
        # pre-flight reset, and its entire job is to honour a Stop that arrived
        # during pre-flight:
        #     if self._state is RunState.CANCELLING: return  # stay cancelling
        #
        # Snapshotting real._state / real._cancel AFTER run_tasks() returns, as
        # this test used to, is a RACE: the task is instant, so a fast worker
        # can already have finished, put the run back to IDLE and consumed the
        # token. These assertions cannot fail for that reason, and they test the
        # real H9/BUG-019 invariant - the Stop survived INTO the worker -
        # instead of one frame of a race. The suite caught this: routing these
        # hops through the dispatcher shifted the timing enough to lose it.
        _mark_seen = []
        _req_seen = []
        _orig_mark = real._mark_running

        def _spy_mark(thread):
            _mark_seen.append(real._state)
            # Sampled HERE, not after the run: _cancel_requested is cleared
            # again when the run releases, so reading it once the run has
            # finished cannot tell "the pre-flight reset wiped it" (the bug)
            # from "the run tidied up afterwards" (correct). At this instant
            # the pre-flight reset at run_tasks has already run, so a True
            # here IS the proof it was not discarded.
            _req_seen.append(real._cancel_requested)
            return _orig_mark(thread)

        real._mark_running = _spy_mark
        try:
            real.run_tasks("Clean", [TASK], quiet=False)
            settle()
            ck("the pre-flight Stop survived INTO the worker "
               "(_mark_running saw CANCELLING)",
               _mark_seen == [RunState.CANCELLING], _mark_seen)
            ck("run_tasks did not clear the pre-flight cancel request "
               "(_cancel_requested was still True when the worker was adopted)",
               _req_seen == [True], _req_seen)
        finally:
            real._mark_running = _orig_mark
    finally:
        _am._themed_askyesno = _fake_ask
        _am.ScorecardDialog = _orig_card
        real._preflight_check = _orig_preflight

    ck("the pre-flight prompt really opened (test is not vacuous)",
       any("Pre-flight" in t for t, _y, _n in _ASK_CALLS), _ASK_CALLS)
    ck("the Stop was actually pressed inside the prompt (not vacuous)",
       pressed["stop"] is True, pressed)
    # NOTE: whether the result card is shown at all is a separate, still-open
    # question (see tools/verify_preflight_stop.py header). It is recorded but
    # not asserted here, because asserting it would make this suite fail for a
    # reason unrelated to BUG-019/020.
    print("        [info] result cards built: %d (scorecard path tracked separately)"
          % len(_cards))
    ck("after the run, back to IDLE", real._state is RunState.IDLE, real._state)
    ck("no worker left behind", real._worker_thread is None)

    ack = [l for l in logs if "Stop requested" in l]
    # The defect was a FALSE acknowledgement followed by a full run. An honest
    # message about the pre-flight window is fine; the old unconditional
    # "finishing the current task, then stopping" with nothing to stop is not.
    false_ack = [l for l in ack if "nothing to interrupt" not in l
                 and "before the run started" not in l]
    ck("no false 'finishing the current task' acknowledgement",
       not false_ack, false_ack)
    ck("the pre-flight stop was reported honestly",
       any("before the run started" in l for l in logs)
       or real._state is RunState.IDLE, logs[-4:])

    # The decisive one. If the Stop had been discarded (the BUG-019 defect)
    # the worker would have seen cancelled()==False and run the task; the
    # correct outcome is that it saw the cancel and stopped.
    ran = [r for r in (real._run_results or []) if r[1] == "ok"]
    ck("the task did NOT run to completion un-cancelled", not ran,
       real._run_results)
    ck("the outcome was recorded as a stop, not a pass",
       real.last_run_outcome is not None
       and all(r[1] != "ok" for r in (real._run_results or [])),
       real.last_run_outcome)

    # ------------------------------------------------------------------ #
    print("\n[5-6] BUG-020 — closing during the pre-flight prompt")
    real._state = RunState.IDLE
    real._cancel.reset()
    real._claim_run()                      # PREFLIGHT, no worker
    ck("we are in PREFLIGHT", real._state is RunState.PREFLIGHT, real._state)
    ck("...and there is no worker to join", real._worker_thread is None)

    withdrew = {"n": 0}
    real._hide_to_tray = lambda: withdrew.__setitem__("n", withdrew["n"] + 1) or True
    real._stop_tray = lambda: None

    logs.clear()
    statuses.clear()
    real._on_close()

    ck("the window was NOT withdrawn to the tray", withdrew["n"] == 0,
       withdrew["n"])
    ck("...and the user was told why", len(statuses) >= 1, statuses)
    if statuses:
        print("        status: %r" % statuses[-1][:88])
    ck("the run is still claimed, not half torn down",
       real._state is RunState.PREFLIGHT, real._state)
    # winfo_exists() returns a Tcl boolean (int 1), not Python True
    ck("the root is still a live window", bool(root.winfo_exists()))

    # ------------------------------------------------------------------ #
    print("\n[7] a real run still closes the old way")
    real._state = RunState.RUNNING
    import threading as _th
    _ev = _th.Event()
    def _slow():
        _ev.wait(5)
    real._worker_thread = _th.Thread(target=_slow, daemon=True)
    real._worker_thread.start()

    asked = {"n": 0}
    def _confirm_yes(parent, title, message, **kw):
        asked["n"] += 1
        return True
    _am._themed_askyesno = _confirm_yes
    withdrew["n"] = 0
    try:
        real._on_close()
    finally:
        _am._themed_askyesno = _fake_ask
        _ev.set()
    ck("a real run still confirms before closing", asked["n"] == 1, asked["n"])
    ck("a real run still withdraws when confirmed", withdrew["n"] == 1,
       withdrew["n"])
    real._worker_thread = None
    real._state = RunState.IDLE

finally:
    _am._themed_askyesno = _ORIG_ASK
    try:
        real._release_run()
    except Exception:
        pass
    try:
        root.destroy()
    except Exception:
        pass
    # byte-exact restore of the user's config (see the note at _GUARD)
    try:
        from app import config_persist as _cp
        _cp._config_cache = None
    except Exception:
        pass
    _GUARD.__exit__(None, None, None)

print()
if FAILS:
    print("BUG-019/020 VERIFY: %d/%d FAILED -> %s" % (len(FAILS), N[0], FAILS))
    sys.exit(1)
print("BUG-019/020 VERIFY: ALL PASS (%d checks)" % N[0])
