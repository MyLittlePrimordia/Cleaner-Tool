"""Run PowerShell from a task: the argv plus a TaskContext-aware wrapper.

The argv builder itself lives in `app/powershell.py`, a leaf module, because
`app/utils.py` needs it for `create_restore_point` and could not import this
one without a cycle. This module keeps what genuinely needs a TaskContext —
running the argv through `run_cmd` so output streams into the log, the process
is killable by the Stop button, and a failure can fail the task:

    run_ps(ctx, "Clear-RecycleBin -Force -ErrorAction SilentlyContinue")
    run_ps(ctx, script, timeout=60, collect=out)      # read what it printed
    run_ps_checked(ctx, script, timeout=600)          # raises on non-zero

`ps_argv` is re-exported, so `from app.tasks.ps import ps_argv` keeps working
for the task modules that already import it from here.

`non_interactive=True` additionally passes `-NonInteractive`, for probes that
must never block on a prompt (a hidden window has nobody to answer it).
"""
from __future__ import annotations

from app.powershell import ps_argv  # noqa: F401  (re-exported for task modules)
from app.utils import run_cmd, run_cmd_checked


def run_ps(ctx, script: str, *, timeout: float = 120,
           collect: "list | None" = None, non_interactive: bool = False) -> int:
    """Run `script`, streaming its output into the log. Returns the exit code."""
    return run_cmd(ctx, ps_argv(script, non_interactive=non_interactive),
                   shell=False, timeout=timeout, collect=collect)


def run_ps_checked(ctx, script: str, *, timeout: float = 120,
                   success_codes=(0,), non_interactive: bool = False) -> int:
    """`run_ps` for steps whose failure must fail the task."""
    return run_cmd_checked(ctx, ps_argv(script, non_interactive=non_interactive),
                           shell=False, timeout=timeout,
                           success_codes=success_codes)
