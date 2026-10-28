"""Task execution + outcome normalization. Tk-free, and the ONLY place it lives.

H8 / ARCH-004. Before this module the same taxonomy was written twice:

  * `Application._invoke_single_task` (app/gui.py) returned a
    `(status, bytes, exc)` tuple with status in ok|skip|stop|fail, and
  * `run_auto_clean`'s task loop (app/scheduler.py) re-implemented the same
    classification inline while incrementing its own counters.

Both were written to "mirror the GUI runner" (their own comments say so), and
both drifted: a sixth outcome, or a changed reading of a task's return value,
could be implemented in one path and forgotten in the other. That is how the
six silent-success tasks (audit MED-001) could report a clean run through one
path while the other told the truth.

This module is deliberately small and has no app dependencies beyond
`app.utils`' exception types, so both the GUI worker and the headless
scheduler import it and cannot diverge.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Iterable, Optional

from app.utils import TaskCancelled, TaskSkipped, TaskContext

# The four outcomes. Strings rather than an Enum because the GUI's existing
# call sites compare against literals (the tuple of all four went in the
# 2026-10 sweep — nothing read it).
OK = "ok"
SKIP = "skip"
STOP = "stop"
FAIL = "fail"


@dataclass
class TaskOutcome:
    """What happened to one task. `bytes_freed` is the task's own accounting
    (an int return), never a guess.

    `changes` is R-005's typed "what changed" record: the `app.changes.Change`
    entries the shared write layer appended while THIS task ran. The context's
    ledger spans the whole run, so `invoke_task` slices its own window rather
    than handing every outcome the same list.
    """
    status: str
    bytes_freed: int = 0
    error: Optional[BaseException] = None
    changes: tuple = ()

    @property
    def ok(self) -> bool:
        return self.status == OK

    @property
    def skipped(self) -> bool:
        return self.status == SKIP

    @property
    def stopped(self) -> bool:
        return self.status == STOP

    @property
    def failed(self) -> bool:
        return self.status == FAIL

    def as_tuple(self):
        """The legacy `(status, bytes, exc)` shape the GUI call sites use."""
        return self.status, self.bytes_freed, self.error


def invoke_task(ctx, task, mode: str = "run", strict_ok: bool = True) -> TaskOutcome:
    """Run ONE Task and normalize its outcome. Never raises for a task failure.

    A task with no callable for `mode` is a FAIL ("No revert/run for
    '<label>'" is logged by the caller).

    Return-value reading, identical on both paths now:
      * int that is not a bool  -> OK, and that int is bytes freed
      * bool False              -> FAIL under strict_ok=True (a task that says
                                   "I did not do it" must not be counted as a
                                   success); a benign OK under strict_ok=False,
                                   because the Install Essentials tasks use
                                   booleans as their own fine-grained outcome
      * anything else (None, True) -> OK, 0 bytes

    Exceptions:
      TaskSkipped   -> SKIP  (nothing to do on this machine; completed, not failed)
      TaskCancelled -> STOP  (a Stop, never a failure)
      anything else -> FAIL
    """
    func = task.revert if mode == "revert" else task.run
    if func is None:
        return TaskOutcome(FAIL, 0, None)
    start = change_cursor(ctx)

    def _done(status, nbytes=0, exc=None):
        return TaskOutcome(status, nbytes, exc, changes_since(ctx, start))

    try:
        result = func(ctx)
        if isinstance(result, int) and not isinstance(result, bool):
            return _done(OK, result)
        if isinstance(result, bool) and not result:
            if strict_ok:
                raise RuntimeError("Task returned False")
            return _done(OK)
        return _done(OK)
    except TaskSkipped as exc:
        return _done(SKIP, 0, exc)
    except TaskCancelled as exc:
        return _done(STOP, 0, exc)
    except Exception as exc:
        return _done(FAIL, 0, exc)


def change_cursor(ctx) -> Optional[int]:
    """Where a task starts in the run's change ledger, or None.

    Public because the GUI worker also calls a task through a thin wrapper
    (for testability) and still has to slice that task's own changes out of the
    shared context ledger.
    """
    bucket = getattr(ctx, "changes", None)
    return len(bucket) if isinstance(bucket, list) else None


def changes_since(ctx, start) -> tuple:
    """The ledger entries added since `start`. Works for failed/stopped tasks
    too: a task that raised after two writes DID change those two values."""
    if start is None:
        return ()
    bucket = getattr(ctx, "changes", None)
    if not isinstance(bucket, list):
        return ()
    return tuple(bucket[start:])


@dataclass
class RunSummary:
    """Aggregate over a batch. `succeeded` deliberately excludes skips, which
    is what both callers have always reported."""
    succeeded: int = 0
    skipped: int = 0
    failed: int = 0
    stopped: bool = False
    bytes_freed: int = 0
    changes: list = field(default_factory=list)

    def add(self, outcome: TaskOutcome) -> None:
        self.changes.extend(getattr(outcome, "changes", ()) or ())
        if outcome.ok:
            self.succeeded += 1
            self.bytes_freed += outcome.bytes_freed
        elif outcome.skipped:
            self.skipped += 1
        elif outcome.stopped:
            self.stopped = True
        else:
            self.failed += 1

    @property
    def clean(self) -> bool:
        """True when nothing failed. A stop is NOT a failure (run_cmd_checked's
        H6 contract: a user pressing Stop must not produce an error exit)."""
        return self.failed == 0


def run_batch(ctx, tasks: Iterable, mode: str = "run", strict_ok: bool = True,
              log: Optional[Callable[[str], None]] = None,
              before: Optional[Callable[[object], None]] = None) -> RunSummary:
    """Run a sequence of tasks, stopping at the first TaskCancelled.

    `before` is called with each task just before it runs (the scheduler uses
    it for its "Running: <label>" progress line). `log` receives one line per
    non-OK outcome; the wording is supplied by the caller so each surface
    keeps its own voice. Tk-free: nothing here touches a widget, which is
    what lets the headless scheduler share it.
    """
    summary = RunSummary()
    for task in tasks:
        if ctx.cancelled():
            # B-006: a Stop pressed BEFORE the first task (or between two)
            # used to leave `stopped` False, because only a TaskCancelled
            # RAISED BY A TASK set it. The closing block then called a run the
            # user had just cancelled "clean", and the exit code said success.
            summary.stopped = True
            if log:
                log("Cancelled — remaining tasks were skipped.")
            break
        if before is not None:
            before(task)
        outcome = invoke_task(ctx, task, mode=mode, strict_ok=strict_ok)
        summary.add(outcome)
        if log:
            if outcome.skipped:
                log(f"Skipped {task.label}: {outcome.error}")
            elif outcome.stopped:
                log(f"Stopped {task.label}: {outcome.error}")
            elif outcome.failed:
                log(f"ERROR in {task.label}: {outcome.error}")
        if outcome.stopped:
            break
    return summary


def log_block(log: Callable[[str], None], summary: str) -> None:
    """Write a finished run's verdict between the transcript's rules.

    Shared by every surface that produces a run transcript - the GUI's task
    runner, its install runner and the headless scheduler - so a log reads the
    same whichever kind of run wrote it: banner, then the block. It lives here
    rather than in app/ui/app.py because a scheduled run has no Tk at all, and
    its stdout is the only record of the run; it must end the way a windowed
    run does, not with a bare sentence.
    """
    log("=" * 48)
    log(summary)
    log("=" * 48)


def print_context(log_prefix: str = "[AUTO]",
                  status_prefix: str = "[AUTO STATUS]") -> TaskContext:
    """The headless scheduler's TaskContext: logs print to stdout.

    ARCH-004 (3.4): `run_auto_clean` and `run_auto_update` each defined their own
    `AutoCtx` with identical `_log`/`_set_status` bodies (plus `log`/`set_status`
    overrides the dataclass's instance attributes already shadowed, so they were
    dead). One definition lives here now so the two headless paths cannot drift;
    only the print prefixes differ. `cancelled` is always False — a scheduled run
    has no user to press Stop.
    """
    def _log(msg: str) -> None:
        print(f"{log_prefix} {msg}")

    def _set_status(msg: str) -> None:
        print(f"{status_prefix} {msg}")

    return TaskContext(log=_log, set_status=_set_status, cancelled=lambda: False)
