"""The read-only hardware/licence probes now use the shared PowerShell runner.

These are the probes that run on a user's machine without a TaskContext — the PC
Specs fallback, drive toolkit, game readiness, health sensors and the SMART /
GPU / battery / RAM / activation checks — so they each used to build the argv
themselves. Eight copies had drifted to a bare `"powershell"`, which is not
resolvable on a machine where PowerShell is not on PATH.

Two guards per module: no hand-built literal may come back, and the argv that
actually reaches the process must be the runner's.
"""
from __future__ import annotations

import inspect
import subprocess

import pytest

PROBE_MODULES = [
    "app.pc_specs",
    "app.drive_toolkit",
    "app.game_readiness",
    "app.health_scan",
    "app.health_sensors",
    "app.tasks.repair_tasks",
]


@pytest.mark.parametrize("name", PROBE_MODULES)
def test_no_probe_builds_its_own_powershell_argv(name):
    source = inspect.getsource(__import__(name, fromlist=["x"]))
    assert '"powershell"' not in source
    assert "'powershell'" not in source


class _Result:
    def __init__(self, stdout="", stderr=""):
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = 0


@pytest.fixture
def spawn(monkeypatch):
    """Capture the argv of the next subprocess.run in the process."""
    seen = {}

    def fake_run(argv, **kwargs):
        seen["argv"] = list(argv)
        seen["kwargs"] = kwargs
        return _Result(stdout=seen.get("stdout", ""))

    monkeypatch.setattr(subprocess, "run", fake_run)
    return seen


def _assert_runner_argv(argv, script):
    assert argv[-2:] == ["-Command", script], "script must be one argv word"
    assert "-NoProfile" in argv
    assert argv[0].lower().endswith("powershell.exe") or \
        argv[0].lower().endswith("powershell")


def test_pc_specs_asks_for_cim_through_the_runner(spawn):
    from app.pc_specs import _ps_query

    spawn["stdout"] = "[]"
    _ps_query("Win32_Processor", ["Name", "NumberOfCores"])

    _assert_runner_argv(spawn["argv"],
                        "Get-CimInstance -ClassName Win32_Processor -ErrorAction Stop "
                        "| Select-Object Name,NumberOfCores | ConvertTo-Json -Compress")
    assert "-NonInteractive" in spawn["argv"]       # a dialog path must never prompt


def test_game_readiness_reads_the_driver_date_through_the_runner(spawn):
    from app.game_readiness import _gpu_driver

    spawn["stdout"] = "2024-01-31\n"
    result = _gpu_driver()

    assert spawn["argv"][-1].startswith("Get-CimInstance Win32_VideoController")
    assert "-NoProfile" in spawn["argv"]
    assert result["key"] == "gpu_driver"


def test_health_sensors_runs_its_batch_through_the_runner(monkeypatch):
    """health_sensors had its own `_PS` constant and hand-written flags."""
    import app.health_sensors as hs
    import app.tasks.ps as ps

    seen = {}

    def fake_run_cmd(ctx, command, shell=False, timeout=None, collect=None):
        seen.update(command=command, shell=shell, timeout=timeout)
        if collect is not None:
            collect.extend(["OK|1", "DISK|Ok|", "DISK|Ok|"])
        return 0

    monkeypatch.setattr(ps, "run_cmd", fake_run_cmd)
    data = hs._run_script(object(), "Write-Output 'OK|1'", timeout=20)

    assert data is not None and data["OK"] == ["1"]
    assert seen["command"][-2] == "-Command"
    assert seen["command"][-1] == "Write-Output 'OK|1'"
    assert "-NonInteractive" in seen["command"]
    assert seen["shell"] is False
