"""Run-state types for the central run engine.

Originally written as `app/services/runner.py` and never wired to anything
(ARCH-006: an orphaned "STATE-001 remediation foundation" whose docstring
claimed a migration that had not happened). Adopted here instead of deleted,
because the types are correct and the run engine genuinely needs them — the
alternative was leaving a second, unused source of truth for "is a run
active".

Import direction: this module depends on NOTHING in the app, so anything may
import it. In particular `app.utils` and `app.config_persist` can, which is
what makes it safe as a base for both the GUI and the headless scheduler.
"""
from __future__ import annotations

from enum import Enum, auto
import threading
from typing import Optional


class RunState(Enum):
    """The single value that says what the run engine is doing.

    Replaces the boolean soup that Application used to carry
    (`_busy` + `_cancel_requested` + "is `_worker_thread` None?"), which
    could represent combinations that are not real states — most notably
    `_busy=True` with no worker thread, the pre-flight window that made a
    Stop request get silently discarded and left an invisible modal on a
    withdrawn window.

    Note that RUNNING -> CANCELLING is a legal, DESIGNED transition, not a
    bug: it is the steady state after the user presses Stop and before the
    in-flight task finishes. That is exactly why a single enum beats a set
    of independent booleans here.

    H9 added PREFLIGHT. Previously Application set `_busy = True` and only
    later spawned the worker thread, so for the whole pre-flight window —
    which includes TWO modal askyesno prompts (the hazard-combo confirm and
    the user-requested pre-flight check) — the object was in a state the
    boolean could describe as "busy" but nothing could act on: a Stop
    request set the cancel token, no worker existed to observe it, and the
    run then started anyway. Making the window a real state means the
    engine knows a run is claimed but not yet executing, and
    Application._request_cancel can refuse honestly instead of silently
    dropping the request.
    """
    IDLE = auto()
    PREFLIGHT = auto()
    RUNNING = auto()
    CANCELLING = auto()
    DONE = auto()
    ERROR = auto()

    @property
    def busy(self) -> bool:
        """True while the app owes the user an end-of-run signal, i.e. a run
        is claimed or executing. This is the ONE definition of "busy" that
        Application._busy delegates to."""
        return self in _BUSY_STATES


_BUSY_STATES = frozenset({RunState.PREFLIGHT, RunState.RUNNING,
                          RunState.CANCELLING})

#: States that classify a FINISHED run rather than a live one. They are
#: what `Application.last_run_outcome` reports and are never parked in
#: `Application._state`, so they have no outgoing transitions.
OUTCOME_STATES = frozenset({RunState.DONE, RunState.ERROR})

#: Every state that `_state` can actually hold.
LIVE_STATES = frozenset(RunState) - OUTCOME_STATES

#: Legal transitions of the LIVE state, i.e. of `Application._state`.
#: Anything not listed here is a bug, and tools/verify_run_state.py walks
#: every edge.
#:
#: DONE and ERROR are deliberately absent as targets: they are terminal
#: classifications of a finished run, reported by `Application.last_run_outcome`
#: rather than parked in `_state` for the two instructions between being set
#: and being overwritten with IDLE. A state nobody can observe is a race, not
#: a state.
ALLOWED_TRANSITIONS = {
    RunState.IDLE: {RunState.PREFLIGHT, RunState.RUNNING},
    RunState.PREFLIGHT: {RunState.RUNNING, RunState.CANCELLING, RunState.IDLE},
    RunState.RUNNING: {RunState.CANCELLING, RunState.IDLE},
    RunState.CANCELLING: {RunState.IDLE},
}


class CancellationToken:
    """Thread-safe cooperative-cancel flag.

    Replaces the bare `_cancel_requested` bool that Application shared
    between the Tk thread (which sets it) and the worker thread (which polls
    it via TaskContext.cancelled). A plain bool read from another thread is
    atomic under CPython but carries no ordering or memory-visibility
    guarantee; this makes the handoff explicit and is the same object the
    session-pilot watcher already threads through its queue.
    """
    _cancelled: bool = False
    _lock: threading.Lock = None  # type: ignore[assignment]

    def __init__(self) -> None:
        self._cancelled = False
        self._lock = threading.Lock()

    def cancel(self) -> None:
        with self._lock:
            self._cancelled = True

    def is_cancelled(self) -> bool:
        with self._lock:
            return self._cancelled

    def reset(self) -> None:
        with self._lock:
            self._cancelled = False

    def __bool__(self) -> bool:  # allow `if token:`
        return self.is_cancelled()
