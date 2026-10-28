"""R-008: repair/clean share one PowerShell runner.

The flags and the exe lookup used to be copy-pasted (and had drifted) across
both task modules; these tests pin the shapes that replaced them, plus the
call sites that were migrated from shell-string to argv-word form.
"""
from __future__ import annotations

import pytest

from app.tasks import ps


class _Ctx:
    def __init__(self):
        self.lines = []

    def log(self, text):
        self.lines.append(text)


def test_argv_is_one_word_after_the_flags():
    argv = ps.ps_argv("Get-Date")

    assert argv[0].lower().endswith("powershell.exe") or argv[0].lower() == "powershell"
    assert "-NoProfile" in argv
    assert argv[-2:] == ["-Command", "Get-Date"]
    # The script must stay a single argv word: no shell re-quoting.
    assert argv.count("Get-Date") == 1


def test_non_interactive_adds_the_flag_before_command():
    argv = ps.ps_argv("Get-Volume", non_interactive=True)

    assert "-NonInteractive" in argv
    assert argv.index("-NonInteractive") < argv.index("-Command")


@pytest.mark.parametrize("script", [
    "Enable-ComputerRestore -Drive 'C:\\'",
    "Get-AppXPackage -AllUsers | ForEach-Object { $_.Name }",
])
def test_quotes_in_a_script_survive_untouched(script):
    assert ps.ps_argv(script)[-1] == script


def test_run_ps_forwards_script_timeout_and_collect(monkeypatch):
    seen = {}

    def fake_run_cmd(ctx, command, shell=False, timeout=None, collect=None):
        seen.update(command=command, shell=shell, timeout=timeout, collect=collect)
        return 0

    monkeypatch.setattr(ps, "run_cmd", fake_run_cmd)
    out: list = []

    assert ps.run_ps(_Ctx(), "dir", timeout=7, collect=out) == 0
    assert seen["timeout"] == 7
    assert seen["collect"] is out
    assert seen["shell"] is False
    assert seen["command"][-1] == "dir"


def test_run_ps_checked_forwards_success_codes(monkeypatch):
    seen = {}

    def fake_checked(ctx, command, shell=False, timeout=None, success_codes=(0,)):
        seen.update(command=command, success_codes=success_codes)
        return 0

    monkeypatch.setattr(ps, "run_cmd_checked", fake_checked)

    ps.run_ps_checked(_Ctx(), "sfc", timeout=600, success_codes=(0, 1))
    assert seen["success_codes"] == (0, 1)
    assert seen["command"][-1] == "sfc"


def test_no_repair_or_clean_task_builds_a_powershell_string_by_hand():
    """The migrated sites must not creep back to a pasted shell string."""
    import inspect

    from app.tasks import clean_tasks, repair_tasks

    for module in (clean_tasks, repair_tasks):
        source = inspect.getsource(module)
        assert "'powershell -NoProfile -Command" not in source
        assert '"powershell -NoProfile -Command' not in source
