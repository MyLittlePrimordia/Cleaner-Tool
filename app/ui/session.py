"""Game Session Auto-Pilot: lifecycle, event plumbing, and session bookkeeping.

H15. This used to be 17 `_pilot_*` methods spread across a 4,300-line
Application, with four pieces of independent state — the SessionPilot object,
a `_pilot_pump_started` bool, an `after`-id nobody kept, and two key lists —
and a body wrapped in `except Exception: pass` at every level, so a pilot
that failed simply never applied anything and said nothing.

What actually moved here is the ORCHESTRATION, not the work. Applying a preset
still lives on Application (`_pilot_apply`), because it needs run_tasks, the
busy lock, the modal registry and the tab widgets; the same is true of
`_pilot_revert`. Those are passed in as callbacks. What is here is the part
that had no business being methods on the window:

  * an explicit state (DISABLED/IDLE/WATCHING/ACTIVE) instead of inferring
    "is it on?" from `_pilot is not None and pilot.running()`
  * the watcher->Tk handoff queue and the pump that drains it
  * the applied/fresh key bookkeeping that revert reconciles against

Three behaviour changes, all deliberate:

  1. THE PUMP CAN STOP. It used to reschedule itself every 50ms for the entire
     life of the app once `_pilot_ensure` had run, draining an empty queue
     forever — including when the feature was off. It now runs only while a
     pilot is attached, and `shutdown()` cancels the pending `after` outright.
     `_pilot_ensure` restarts it, so re-enabling still works.

  2. FAILURES ARE VISIBLE, ONCE. The old code swallowed every exception at
     every level. `_warn` reports a failure the first time it happens and then
     stays quiet, so a broken pilot is diagnosable from the log without
     spamming it 20 times a second.

  3. `stop()` does not revert, and never did. The user may still be playing;
     app close is side-effect free by design. Applied tweaks stay badged
     until Undo puts them back.

Threading contract, unchanged and load-bearing: everything here runs on the Tk
thread EXCEPT `post_apply` / `post_revert`, which are called by the watcher
thread and do nothing but `queue.put`. The watcher must never call
`root.after()` — an occasional cross-thread Tcl notifier race silently dropped
those calls and left apply/revert pending forever.
"""
from __future__ import annotations

import queue
import threading
from enum import Enum, auto


def _as_key_list(value) -> list:
    """Coerce a task-key collection to a clean list of strings.

    `list(value or [])` is the obvious thing to write and it is wrong: given
    a bare string it iterates per CHARACTER, so "disable_nagle" becomes
    ['d','i','s','a',...]. That is not hypothetical — this exact failure
    already shipped three times in this codebase (audit F-6's `"Games":
    "launcher_cache"`, `applied_tweaks`, `session_pilot_games`), and the
    result is a revert that tries to put back 13 nonexistent tweaks while
    quietly leaving the real one applied.
    """
    if isinstance(value, str):
        return [value] if value.strip() else []
    if isinstance(value, (list, tuple, set, frozenset)):
        return [v for v in value if isinstance(v, str) and v.strip()]
    return []


class SessionState(Enum):
    """What the Auto-Pilot is doing. One value, so "is it on?" is never a
    guess across several loosely-related attributes."""

    #: no watchlist built / feature off
    DISABLED = auto()
    #: watchlist built, watcher not started
    IDLE = auto()
    #: watcher thread running, no game detected yet
    WATCHING = auto()
    #: a game was detected and the session preset was applied
    ACTIVE = auto()

    @property
    def on(self) -> bool:
        return self is not SessionState.DISABLED


class SessionOrchestrator:
    """Owns the Auto-Pilot lifecycle. Construct once, on the Tk thread."""

    #: pump cadence. 50ms was the pre-H15 value; it is a floor on how long a
    #: queued detect/exit waits, not a poll of anything expensive.
    PUMP_MS = 50

    def __init__(self, root, on_apply=None, on_revert=None, log=None):
        self._root = root
        self._queue = queue.Queue()
        self._pilot = None
        self._state = SessionState.DISABLED
        self._after_id = None
        self._pumping = False
        self._applied_keys = []
        self._fresh_keys = []
        self._on_apply = on_apply
        self._on_revert = on_revert
        self._lock = threading.Lock()
        self._warned = set()
        self._log = log or (lambda *a, **k: None)
        self._pump_ticks = 0
        self._events_handled = 0

    # ------------------------------------------------------------------ #
    # state
    # ------------------------------------------------------------------ #
    @property
    def state(self) -> SessionState:
        return self._state

    @property
    def pilot(self):
        """The SessionPilot object, or None. Application._pilot forwards
        here; tools/smoke_async.py reads it to drive the watcher directly."""
        return self._pilot

    @pilot.setter
    def pilot(self, value):
        with self._lock:
            self._pilot = value
        if value is None:
            self._state = SessionState.DISABLED
            return
        if self._state is SessionState.DISABLED:
            self._state = SessionState.IDLE
        # ATTACHING brings the pump up, not start(). Application._pilot_ensure
        # calls ensure_pump() first and only builds the pilot afterwards, so
        # keying the pump off ensure_pump() alone meant the "no pilot attached
        # -> don't schedule" rule cancelled the loop before it ever began, and
        # every detect sat in the queue forever. Keying it off attachment
        # removes the call-order dependency entirely — and also covers
        # tools/smoke_async.py, which starts the watcher by calling
        # `app._pilot.start(...)` directly and so never goes through here.
        self.ensure_pump()

    @property
    def queue(self) -> queue.Queue:
        return self._queue

    @property
    def applied_keys(self) -> list:
        """Every task key this session claimed to apply.

        Recorded BEFORE the admin filter used to be applied, so it named
        tweaks a limited-mode run never touched and left revert overbroad. It
        is saved for exactly that audit fix — see Application._pilot_apply."""
        return self._applied_keys

    @applied_keys.setter
    def applied_keys(self, value):
        self._applied_keys = _as_key_list(value)

    @property
    def fresh_keys(self) -> list:
        """applied_keys minus what was already applied before the session.

        Reverting a tweak the user had deliberately set up themselves is not
        the pilot's call to make."""
        return self._fresh_keys

    @fresh_keys.setter
    def fresh_keys(self, value):
        self._fresh_keys = _as_key_list(value)

    def is_running(self) -> bool:
        try:
            pilot = self._pilot
            return bool(pilot is not None and pilot.running())
        except Exception as exc:
            self._warn("is_running", exc)
            return False

    def note_session_active(self, active: bool):
        """Called by the apply/revert handlers once they know the outcome."""
        if active:
            if self._state in (SessionState.IDLE, SessionState.WATCHING):
                self._state = SessionState.ACTIVE
        elif self._state is SessionState.ACTIVE:
            self._state = (SessionState.WATCHING if self.is_running()
                           else SessionState.IDLE)

    # ------------------------------------------------------------------ #
    # lifecycle
    # ------------------------------------------------------------------ #
    def ensure_pump(self):
        """Start the drain loop if it is not already going. Idempotent — the
        old `_pilot_pump_started` bool, but it also restarts after a stop."""
        with self._lock:
            if self._pumping:
                return
            self._pumping = True
        self.pump()

    def cancel_pump(self):
        """Stop rescheduling and drop any pending `after`. Safe to call twice."""
        with self._lock:
            self._pumping = False
            aid = self._after_id
            self._after_id = None
        if aid is not None:
            try:
                self._root.after_cancel(aid)
            except Exception:
                pass

    def start(self):
        """Start the watcher thread, if a pilot is attached.

        Also ensures the pump is live. A running watcher whose events nobody
        drains is a silent failure — the game launches, the tweaks never
        apply, and nothing says why. That was the pre-H15 failure mode when
        the two were wired independently, so they are wired together here.
        """
        pilot = self._pilot
        if pilot is None:
            return False
        self.ensure_pump()
        try:
            ok = bool(pilot.start())
        except Exception as exc:
            self._warn("start", exc)
            return False
        if ok and self._state is not SessionState.ACTIVE:
            self._state = SessionState.WATCHING
        return ok

    def stop(self):
        """Stop watching. NEVER reverts — see the module docstring."""
        pilot = self._pilot
        if pilot is not None:
            try:
                pilot.stop()
            except Exception as exc:
                self._warn("stop", exc)
        self.pilot = None
        self._state = SessionState.DISABLED
        # a session left ACTIVE at stop time still has to be revertable, so
        # the key lists are deliberately NOT cleared here
        self.cancel_pump()

    def shutdown(self):
        """App is closing: stop the watcher and kill the pump for good."""
        pilot = self._pilot
        if pilot is not None:
            try:
                pilot.stop()
            except Exception:
                pass
        self.pilot = None
        self._state = SessionState.DISABLED
        self.cancel_pump()

    # ------------------------------------------------------------------ #
    # watcher-thread producers: queue only, never touch Tk
    # ------------------------------------------------------------------ #
    def post_apply(self, hits):
        self._queue.put(("apply", list(hits or [])))

    def post_revert(self):
        self._queue.put(("revert", None))

    # ------------------------------------------------------------------ #
    # Tk-thread consumer
    # ------------------------------------------------------------------ #
    def pump(self):
        """Drain queued events and reschedule. Tk thread only."""
        try:
            while True:
                try:
                    kind, payload = self._queue.get_nowait()
                except queue.Empty:
                    break
                except Exception:
                    break
                self._events_handled += 1
                try:
                    if kind == "apply":
                        if self._on_apply is not None:
                            self._on_apply(payload)
                    elif kind == "revert":
                        if self._on_revert is not None:
                            self._on_revert()
                except Exception as exc:
                    self._warn("pump/%s" % kind, exc)
        except Exception as exc:
            self._warn("pump", exc)

        # Keep going only while a pilot is attached or work is queued. This
        # is the change from pre-H15, where the loop rescheduled itself for
        # the whole life of the app regardless.
        with self._lock:
            pumping = self._pumping
        if not pumping:
            return
        if self._pilot is None and self._queue.empty():
            self.cancel_pump()
            return
        try:
            if self._root.winfo_exists():
                self._pump_ticks += 1
                self._after_id = self._root.after(self.PUMP_MS, self.pump)
            else:
                self.cancel_pump()
        except Exception:
            self.cancel_pump()

    # ------------------------------------------------------------------ #
    def _warn(self, where, exc):
        """Report a failure once per site. The pre-H15 code swallowed all of
        them, which is why a broken pilot looked like a feature that simply
        never fired."""
        key = "%s:%s" % (where, type(exc).__name__)
        if key in self._warned:
            return
        self._warned.add(key)
        try:
            self._log("  ! Auto-Pilot %s failed (%s: %s)"
                      % (where, type(exc).__name__, exc))
        except Exception:
            pass
