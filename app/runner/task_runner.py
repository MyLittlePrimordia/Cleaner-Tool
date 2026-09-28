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

from dataclasses import dataclass
from typing import Callable, Iterable, Optional

from app.utils import TaskCancelled, TaskSkipped

# The five outcomes. Strings rather than an Enum because the GUI's existing
# call sites compare against literals and the tuple shape is part of its
# (tested) contract.
OK = "ok"
SKIP = "skip"
STOP = "stop"
FAIL = "fail"

ALL_OUTCOMES = (OK, SKIP, STOP, FAIL)


@dataclass
class TaskOutcome:
    """What happened to one task. `bytes_freed` is the task's own accounting
    (an int return), never a guess."""
    status: str
    bytes_freed: int = 0
    error: Optional[BaseException] = None

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
    try:
        result = func(ctx)
        if isinstance(result, int) and not isinstance(result, bool):
            return TaskOutcome(OK, result, None)
        if isinstance(result, bool) and not result:
            if strict_ok:
                raise RuntimeError("Task returned False")
            return TaskOutcome(OK, 0, None)
        return TaskOutcome(OK, 0, None)
    except TaskSkipped as exc:
        return TaskOutcome(SKIP, 0, exc)
    except TaskCancelled as exc:
        return TaskOutcome(STOP, 0, exc)
    except Exception as exc:
        return TaskOutcome(FAIL, 0, exc)


@dataclass
class RunSummary:
    """Aggregate over a batch. `succeeded` deliberately excludes skips, which
    is what both callers have always reported."""
    succeeded: int = 0
    skipped: int = 0
    failed: int = 0
    stopped: bool = False
    bytes_freed: int = 0

    def add(self, outcome: TaskOutcome) -> None:
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
