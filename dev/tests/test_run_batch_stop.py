"""B-006: a cancelled batch must say it was cancelled.

`RunSummary.stopped` is what the GUI and the headless scheduler read to decide
whether a run was a clean success. It was only set when a TASK raised
TaskCancelled, so pressing Stop before the first task (or between two) left it
False and the run was reported as clean even though nothing ran.
"""
from __future__ import annotations

from types import SimpleNamespace

from app.runner.task_runner import RunSummary, run_batch
from app.utils import TaskCancelled, TaskContext


class _Flag:
    def __init__(self, value=False):
        self.value = value

    def __call__(self):
        return self.value


def _task(label, ran, result=None, raises=None):
    task = SimpleNamespace(label=label, run=None, revert=None)

    def run(ctx):
        ran.append(label)
        if raises is not None:
            raise raises
        return result

    task.run = run
    return task


def _ctx(cancelled):
    return TaskContext(log=lambda m: None, set_status=lambda m: None,
                       cancelled=cancelled)


def test_stop_before_the_first_task_marks_the_run_stopped():
    flag = _Flag(True)
    ran, lines = [], []
    summary = run_batch(_ctx(flag), [_task("Clean", ran)], log=lines.append)

    assert ran == []
    assert summary.stopped is True
    assert lines == ["Cancelled — remaining tasks were skipped."]


def test_stop_between_tasks_marks_the_run_stopped():
    flag = _Flag(False)
    ran = []

    def first(ctx):
        ran.append("A")
        flag.value = True          # the user presses Stop during task A
        return 10

    task_a = SimpleNamespace(label="A", run=first, revert=None)
    summary = run_batch(_ctx(flag), [task_a, _task("B", ran)])

    assert ran == ["A"]
    assert summary.stopped is True
    assert summary.succeeded == 1 and summary.failed == 0
    assert summary.clean is True       # a stop is not a failure


def test_a_task_raising_cancelled_still_marks_stopped():
    summary = run_batch(_ctx(_Flag()), [
        _task("A", [], raises=TaskCancelled("stopped"))])
    assert summary.stopped is True


def test_a_completed_batch_is_not_stopped():
    ran = []
    summary = run_batch(_ctx(_Flag()), [_task("A", ran, result=5),
                                        _task("B", ran)])
    assert summary.stopped is False
    assert summary.succeeded == 2 and summary.bytes_freed == 5
    assert ran == ["A", "B"]


def test_the_default_summary_is_not_stopped():
    assert RunSummary().stopped is False
