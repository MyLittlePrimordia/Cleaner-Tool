"""`_run_mp_script`: the one place a Defender preference script is executed.

Every SEC-004 removal and the read-only exclusion listing go through here, so
the contract is narrow and load-bearing:

* the script is ONE `-Command` argument — never split, so a path with spaces or
  a semicolon-joined script cannot leak into PowerShell's own parsing;
* `shell=False`, captured text output, a 30 s timeout and no console window;
* the returned message is stdout followed by stderr, stripped, whether the exit
  code was zero or not (callers show it to the user as the reason a removal
  failed);
* the full `%SystemRoot%\\System32\\...\\powershell.exe` path is preferred so a
  `PATH` entry cannot shadow it, falling back to the bare name only when that
  file is not there.

Timeouts are deliberately NOT caught here: callers turn them into
`(False, "TimeoutExpired: ...")`. That is asserted so nobody "helpfully" starts
swallowing them into a bare `False` and loses the reason.

subprocess is stubbed; nothing is launched and no Defender call is made.
"""
from __future__ import annotations

import os
import subprocess
from types import SimpleNamespace

import pytest


class _Result:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


@pytest.fixture
def runner(monkeypatch):
    """A stubbed subprocess.run that records the command and returns `result`."""
    import app.elevation as el

    calls = SimpleNamespace(runs=[], result=_Result())
    calls.window_flag = 0x08000000

    def fake_run(cmd, **kwargs):
        calls.runs.append((cmd, kwargs))
        return calls.result

    monkeypatch.setattr(el, "subprocess", SimpleNamespace(
        run=fake_run, CREATE_NO_WINDOW=calls.window_flag))
    return calls


@pytest.fixture
def bare_powershell(monkeypatch):
    """No powershell.exe at the SystemRoot path: the bare name is used."""
    import app.elevation as el

    monkeypatch.setattr(el, "os", SimpleNamespace(
        environ={},
        path=SimpleNamespace(join=os.path.join, isfile=lambda p: False),
    ))


def test_the_command_is_powershell_with_the_script_as_one_argument(runner,
                                                                 bare_powershell):
    script = "Remove-MpPreference -ExclusionPath 'C:\\Program Files\\App.exe'"

    from app.elevation import _run_mp_script
    ok, out = _run_mp_script(script)

    assert ok is True and out == ""
    cmd, kwargs = runner.runs[0]
    assert cmd == [
        "powershell.exe",
        "-NoProfile",
        "-NonInteractive",
        "-ExecutionPolicy", "Bypass",
        "-Command", script,
    ], "the script is the last element, verbatim"
    assert kwargs["shell"] is False
    assert kwargs["capture_output"] is True and kwargs["text"] is True
    assert kwargs["timeout"] == 30
    assert kwargs["creationflags"] == runner.window_flag


def test_no_console_window_is_flagged_when_the_platform_has_it(runner,
                                                              bare_powershell):
    """CREATE_NO_WINDOW exists on Windows; where it does not, the flag must
    degrade to 0 rather than raising."""
    import app.elevation as el

    el.subprocess = SimpleNamespace(run=el.subprocess.run)   # no CREATE_NO_WINDOW

    from app.elevation import _run_mp_script
    _run_mp_script("Get-MpPreference")

    assert runner.runs[0][1]["creationflags"] == 0


def test_stdout_and_stderr_are_combined_and_stripped(runner, bare_powershell):
    """Callers show this string as the failure reason, so both streams matter."""
    runner.result = _Result(returncode=1,
                            stdout="line one\r\n",
                            stderr="  Remove-MpPreference : Access denied\r\n")

    from app.elevation import _run_mp_script
    ok, out = _run_mp_script("Remove-MpPreference -ExclusionPath 'C:\\Apps'")

    assert ok is False
    assert out == "line one\r\n  Remove-MpPreference : Access denied", (
        "stdout first, then stderr, with only the outer whitespace trimmed")


def test_a_zero_exit_with_no_output_is_an_empty_message(runner, bare_powershell):
    from app.elevation import _run_mp_script
    ok, out = _run_mp_script("Get-MpPreference")

    assert ok is True
    assert out == ""


def test_missing_streams_are_treated_as_empty(runner, bare_powershell):
    """A redirected/closed pipe can leave stdout or stderr as None."""
    runner.result = _Result(returncode=1, stdout=None, stderr=None)

    from app.elevation import _run_mp_script
    ok, out = _run_mp_script("Get-MpPreference")

    assert ok is False
    assert out == ""


def test_a_non_zero_exit_keeps_its_message(runner, bare_powershell):
    """The message is the whole point on failure: it says whether Defender is
    absent, a policy blocked the call, or the path was wrong."""
    runner.result = _Result(returncode=1, stderr="Add-MpPreference : not found")

    from app.elevation import _run_mp_script
    ok, out = _run_mp_script("Add-MpPreference -ExclusionPath 'C:\\Apps'")

    assert ok is False
    assert out == "Add-MpPreference : not found"


def test_a_timeout_propagates_to_the_caller(runner, bare_powershell, monkeypatch):
    """Callers catch it and say \"TimeoutExpired: ...\". Swallowing it here would
    turn a hung PowerShell into a bare failure with no reason."""
    import app.elevation as el

    def slow(cmd, **kwargs):
        raise subprocess.TimeoutExpired(cmd, kwargs.get("timeout", 30))

    monkeypatch.setattr(el.subprocess, "run", slow)

    from app.elevation import _run_mp_script
    with pytest.raises(subprocess.TimeoutExpired):
        _run_mp_script("Get-MpPreference")


# --------------------------------------------------------------------------- #
# Which powershell.exe is run: the absolute one, so PATH cannot shadow it.
# --------------------------------------------------------------------------- #

def _fake_os(system_root, existing):
    environ = {} if system_root is None else {"SystemRoot": system_root}
    return SimpleNamespace(
        environ=environ,
        path=SimpleNamespace(join=os.path.join, isfile=lambda p: p in existing),
    )


def test_the_full_powershell_path_is_preferred(runner, monkeypatch):
    """A `PATH` entry planted with a `powershell.exe` must not be what runs."""
    import app.elevation as el

    root = r"C:\Windows"
    expected = os.path.join(root, "System32", "WindowsPowerShell", "v1.0",
                            "powershell.exe")
    monkeypatch.setattr(el, "os", _fake_os(root, {expected}))

    from app.elevation import _run_mp_script
    _run_mp_script("Get-MpPreference")

    assert runner.runs[0][0][0] == expected


def test_a_missing_powershell_falls_back_to_the_bare_name(runner, monkeypatch):
    import app.elevation as el

    monkeypatch.setattr(el, "os", _fake_os(r"C:\Windows", set()))

    from app.elevation import _run_mp_script
    _run_mp_script("Get-MpPreference")

    assert runner.runs[0][0][0] == "powershell.exe"


def test_the_default_system_root_is_windows(runner, monkeypatch):
    """No SystemRoot in the environment (a stripped service environment): the
    classic C:\\Windows location is still tried."""
    import app.elevation as el

    expected = os.path.join(r"C:\Windows", "System32", "WindowsPowerShell",
                            "v1.0", "powershell.exe")
    monkeypatch.setattr(el, "os", _fake_os(None, {expected}))

    from app.elevation import _run_mp_script
    _run_mp_script("Get-MpPreference")

    assert runner.runs[0][0][0] == expected


def test_a_real_system_root_directory_is_resolved(runner, monkeypatch, tmp_path):
    """The stub above pins the branch; this one exercises the real join+isfile
    against a directory that actually exists."""
    ps_dir = tmp_path / "System32" / "WindowsPowerShell" / "v1.0"
    ps_dir.mkdir(parents=True)
    (ps_dir / "powershell.exe").write_text("", encoding="utf-8")
    monkeypatch.setitem(os.environ, "SystemRoot", str(tmp_path))

    from app.elevation import _run_mp_script
    _run_mp_script("Get-MpPreference")

    assert runner.runs[0][0][0] == str(ps_dir / "powershell.exe")
