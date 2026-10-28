"""R-008 (finished): the write paths and the remaining tools use one runner.

These sites decide what an apply actually does, so the point of these tests is
not that the code looks tidy - it is that the script still arrives as ONE argv
word (no cmd.exe in between), that the security-relevant quoting still happens
before it does, and that no module quietly goes back to building its own
`"powershell"` argv.
"""
from __future__ import annotations

import inspect

import pytest

#: Modules that must never spell the exe themselves again. `app.update_checker`
#: is deliberately absent: it builds `-EncodedCommand` argv, which is a
#: different mechanism on purpose and already uses resolve_exe.
MIGRATED = [
    "app.svcs",
    "app.restorepts",
    "app.filelocks",
    "app.dns_test",
    "app.toast",
    "app.tasks.winget",
    "app.tasks.tweak_common",
    "app.tasks.tweak_gaming",
    "app.tasks.tweak_network",
    "app.tasks.tweak_snapshots",
    "app.tasks.advanced_tasks",
    "app.tasks.install_tasks",
    "app.tasks.repair_tasks",
]


class _Ctx:
    def __init__(self):
        self.lines = []

    def log(self, text):
        self.lines.append(text)


@pytest.mark.parametrize("name", MIGRATED)
def test_no_module_builds_its_own_powershell_argv(name):
    source = inspect.getsource(__import__(name, fromlist=["x"]))
    assert '"powershell"' not in source
    assert "'powershell'" not in source


# --------------------------------------------------------------------------- #
# The script that carries a config-derived value
# --------------------------------------------------------------------------- #

def test_the_dns_command_keeps_the_script_in_one_arg_word():
    """`_dns_set_command` is the argv the DNS apply hands to run_cmd. A name
    or IP that arrived from config must not be able to end the command."""
    from app.tasks.tweak_common import _dns_set_command

    argv = _dns_set_command("12", ("1.1.1.1", "1.0.0.1"))

    assert argv.count("-Command") == 1
    assert argv[-1] == ("Set-DnsClientServerAddress -InterfaceIndex 12 "
                        "-ServerAddresses ('1.1.1.1','1.0.0.1')")


def test_enabling_a_scheduled_task_passes_the_name_as_data(monkeypatch):
    from app.tasks import tweak_snapshots

    seen = {}

    def fake_run_ps(ctx, script, **kwargs):
        seen["script"] = script
        seen["kwargs"] = kwargs
        return 0

    monkeypatch.setattr(tweak_snapshots, "run_ps", fake_run_ps)

    ok = tweak_snapshots._set_task_enabled(
        _Ctx(), r"\Microsoft\Windows\Application Experience\Foo", False)

    assert ok is True
    assert seen["script"] == ("Disable-ScheduledTask -TaskName "
                              r"'\Microsoft\Windows\Application Experience\Foo' "
                              "-ErrorAction SilentlyContinue")
    assert seen["kwargs"]["timeout"] == 60


def test_an_implausible_task_name_never_reaches_the_runner(monkeypatch):
    """The shape check runs BEFORE the command is built, so a name carrying
    shell syntax is refused rather than merely quoted."""
    from app.tasks import tweak_snapshots

    called = []
    monkeypatch.setattr(tweak_snapshots, "run_ps",
                        lambda *a, **k: called.append(a) or 0)

    ctx = _Ctx()
    ok = tweak_snapshots._set_task_enabled(ctx, "x'; calc.exe; '", True)

    assert ok is False
    assert called == []
    assert any("refusing" in line for line in ctx.lines)


# --------------------------------------------------------------------------- #
# The tools that carry a TaskContext-free argv
# --------------------------------------------------------------------------- #

def test_the_service_tool_wraps_its_scripts_with_the_runner():
    from app import svcs

    argv = svcs._ps_argv("Get-Service")

    assert argv[-2:] == ["-Command", "Get-Service"]
    assert "-NonInteractive" in argv        # a dialog must never wait on a prompt
    assert argv.index("-NonInteractive") < argv.index("-Command")


def test_the_restore_point_tool_wraps_its_scripts_with_the_runner():
    from app import restorepts

    argv = restorepts._ps_argv("Get-ComputerRestorePoint")

    assert argv[-2:] == ["-Command", "Get-ComputerRestorePoint"]
    assert "-NoProfile" in argv
