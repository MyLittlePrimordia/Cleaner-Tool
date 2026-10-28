"""The command line `relaunch_as_admin()` builds for PowerShell.

Two layers of quoting have to survive at once: the Windows command line for the
child (`subprocess.list2cmdline`), and the PowerShell string literal that carries
it (`Start-Process -ArgumentList '...'`). The failure this guards against is
real: an install under `C:\\Program Files (x86)\\...` used to die because the
bare path was parsed as PowerShell syntax, and a folder named `It's` would end
the single-quoted literal early and hand the rest to PowerShell as code.

So the assertions here are literal: un-double the PS quoting and compare against
`list2cmdline` of the argv the app intended, plus a quote count that would catch
any extra unescaped `'` slipping into the command.

PowerShell itself is stubbed (its return code is all the caller sees), the token
is fixed, and the cookie/token files are in memory — nothing is launched and
nothing is written.
"""
from __future__ import annotations

import subprocess
from types import SimpleNamespace

import pytest

TOKEN = "0123456789abcdef0123456789abcdef"


@pytest.fixture
def launch(monkeypatch):
    """Stubbed PowerShell, fixed token, in-memory handshake files."""
    import app.elevation as el

    calls = SimpleNamespace(runs=[], cleared=[], written=[], log=[], order=[])

    class Result:
        returncode = 0
        stdout = ""
        stderr = ""

    def fake_run(cmd, **kwargs):
        calls.runs.append((cmd, kwargs))
        calls.order.append("run")
        return Result()

    monkeypatch.setattr(el, "subprocess", SimpleNamespace(
        run=fake_run,
        list2cmdline=subprocess.list2cmdline,
        CREATE_NO_WINDOW=0x08000000,
    ))
    monkeypatch.setattr("secrets.token_hex", lambda n: TOKEN)
    def clear(what):
        calls.cleared.append(what)
        calls.order.append("clear:" + what)

    monkeypatch.setattr(el, "_clear_elevation_cookie", lambda: clear("cookie"))
    monkeypatch.setattr(el, "_clear_pending_token", lambda: clear("token"))
    calls.original_write = el._write_pending_token

    def write(t):
        calls.written.append(t)
        calls.order.append("write")

    monkeypatch.setattr(el, "_write_pending_token", write)
    monkeypatch.setattr(el, "_log_or_swallow_fallback",
                        lambda msg, exc=None: calls.log.append(msg))
    calls.Result = Result
    return calls


@pytest.fixture
def frozen(monkeypatch, tmp_path):
    """A PyInstaller-style run: sys.executable is the app, argv has no script."""
    import app.elevation as el

    exe = str(tmp_path / "Apps" / "CleanerTool.exe")

    def setup(argv=(), executable=None):
        monkeypatch.setattr(el.sys, "frozen", True, raising=False)
        monkeypatch.setattr(el.sys, "executable", executable or exe)
        monkeypatch.setattr(el.sys, "argv", ["CleanerTool.exe", *argv])
        return executable or exe

    return setup


def _file_path_literal(ps_cmd: str) -> str:
    prefix = "Start-Process -FilePath "
    tail = " -ArgumentList "
    assert ps_cmd.startswith(prefix), ps_cmd
    end = ps_cmd.index(tail)
    return ps_cmd[len(prefix):end]


def _argument_list_literal(ps_cmd: str) -> str:
    head = " -ArgumentList "
    tail = " -Verb RunAs"
    assert ps_cmd.endswith(tail), ps_cmd
    start = ps_cmd.index(head) + len(head)
    return ps_cmd[start:-len(tail)]


def _unquote(literal: str) -> str:
    """Undo the PowerShell single-quote escaping the code applied."""
    assert literal.startswith("'") and literal.endswith("'"), literal
    return literal[1:-1].replace("''", "'")


def _ps_cmd(calls) -> str:
    assert calls.runs, "PowerShell was never called"
    return calls.runs[0][0][4]


def test_frozen_mode_asks_powershell_to_start_the_app_elevated(launch, frozen):
    exe = frozen()

    from app.elevation import relaunch_as_admin
    assert relaunch_as_admin() is True

    cmd, kwargs = launch.runs[0]
    assert cmd[0].endswith("powershell.exe")
    assert cmd[1:4] == ["-NoProfile", "-NonInteractive", "-Command"]
    assert kwargs["shell"] is False
    assert kwargs["capture_output"] is True and kwargs["text"] is True
    assert kwargs["timeout"] == 30
    assert kwargs["creationflags"] == 0x08000000

    ps = cmd[4]
    assert _unquote(_file_path_literal(ps)) == exe
    assert ps.endswith(" -Verb RunAs")


def test_the_argument_list_is_the_windows_command_line_of_the_child(launch, frozen):
    """Not the argv joined by hand: `list2cmdline` is what the child will
    parse, so that is what has to be inside the PowerShell string."""
    exe = frozen(argv=["--tab=Clean", "--note=hello world"])

    from app.elevation import relaunch_as_admin
    assert relaunch_as_admin() is True

    expected_argv = ["--tab=Clean", "--note=hello world", f"--elevation-token={TOKEN}"]
    args = _unquote(_argument_list_literal(_ps_cmd(launch)))
    assert args == subprocess.list2cmdline(expected_argv)
    assert _unquote(_file_path_literal(_ps_cmd(launch))) == exe


def test_a_path_with_spaces_stays_one_argument(launch, frozen, tmp_path):
    """The install that broke: `Program Files (x86)` is both a space and a
    parenthesis, either of which PowerShell would parse as syntax if the path
    were not inside one quoted string."""
    exe = frozen(executable=str(tmp_path / "Program Files (x86)" / "Cleaner Tool"
                                / "CleanerTool.exe"))

    from app.elevation import relaunch_as_admin
    assert relaunch_as_admin() is True

    ps = _ps_cmd(launch)
    assert _file_path_literal(ps) == f"'{exe}'"
    assert _unquote(_file_path_literal(ps)) == exe


def test_a_single_quote_in_the_exe_path_is_doubled(launch, frozen, tmp_path):
    """A folder named `It's` must not end the PS string literal early — the
    classic way this kind of command injection happens by accident."""
    exe = frozen(executable=str(tmp_path / "It's Tools" / "CleanerTool.exe"))

    from app.elevation import relaunch_as_admin
    assert relaunch_as_admin() is True

    ps = _ps_cmd(launch)
    literal = _file_path_literal(ps)
    assert literal == ("'" + exe.replace("'", "''") + "'")
    assert literal.count("''") == 1, "the quote is escaped, not dropped"
    assert _unquote(literal) == exe


def test_arguments_with_spaces_and_quotes_come_through_unchanged(launch, frozen):
    """Both quoting layers at once: a value with a space, one with a single
    quote, one with a double quote, and one ending in a backslash."""
    tricky = ["--path=C:\\Some Folder\\file.txt",
              "--name=It's mine",
              '--filter="quoted"',
              "--dir=C:\\trailing\\"]
    frozen(argv=tricky)

    from app.elevation import relaunch_as_admin
    assert relaunch_as_admin() is True

    ps = _ps_cmd(launch)
    expected_argv = list(tricky) + [f"--elevation-token={TOKEN}"]
    expected_args = subprocess.list2cmdline(expected_argv)

    args_literal = _argument_list_literal(ps)
    assert _unquote(args_literal) == expected_args
    assert args_literal == ("'" + expected_args.replace("'", "''") + "'")

    # No unescaped quote can have leaked into the command: the only `'` in it
    # are the two wrapping pairs plus the doubled ones from the paths/args.
    assert ps.count("'") == 4 + 2 * (expected_args.count("'"))


def test_script_mode_relaunches_the_interpreter_with_the_script(launch, monkeypatch,
                                                               tmp_path):
    """`python main.py` has to come back as `python.exe <abs main.py>`, not as
    the script's path alone (which Windows would not execute)."""
    import app.elevation as el

    script = tmp_path / "My Folder" / "main.py"
    monkeypatch.setattr(el.sys, "frozen", False, raising=False)
    monkeypatch.setattr(el.sys, "executable", "python.exe")
    monkeypatch.setattr(el.sys, "argv", [str(script), "--tab=Clean"])

    from app.elevation import relaunch_as_admin
    assert relaunch_as_admin() is True

    ps = _ps_cmd(launch)
    assert _unquote(_file_path_literal(ps)) == "python.exe"
    expected = subprocess.list2cmdline(
        [str(script), "--tab=Clean", f"--elevation-token={TOKEN}"])
    assert _unquote(_argument_list_literal(ps)) == expected


def test_the_token_is_cleaned_then_written_then_handed_to_the_child(launch, frozen):
    """The elevated side echoes this token back in the cookie, so the one
    written before the prompt must be the one on the command line — and the
    stale pair from a previous attempt must be gone first."""
    frozen()

    from app.elevation import relaunch_as_admin
    assert relaunch_as_admin() is True

    assert launch.order == ["clear:cookie", "clear:token", "write", "run"], (
        "stale files first, then the token, then the prompt — a token written "
        "after the launch is one the elevated side never sees")
    assert launch.written == [TOKEN]
    assert f"--elevation-token={TOKEN}" in _unquote(_argument_list_literal(_ps_cmd(launch)))


def test_a_refused_start_returns_false_and_drops_the_token(launch, frozen):
    """PowerShell exits non-zero when the UAC prompt is declined or the command
    is malformed; the caller must not then wait 60 s for a process that will
    never appear."""
    exe = frozen()
    launch.Result.returncode = 1
    launch.Result.stderr = "The operation was canceled by the user."

    from app.elevation import relaunch_as_admin
    assert relaunch_as_admin() is False

    assert launch.cleared[-1] == "token", "the pending token is dropped"
    assert any("rc=1" in msg for msg in launch.log)
    assert _unquote(_file_path_literal(_ps_cmd(launch))) == exe


def test_a_powershell_that_hangs_or_raises_returns_false_and_drops_the_token(
        launch, frozen, monkeypatch):
    """`subprocess.run(..., timeout=30)` raises TimeoutExpired, not a result;
    that must not escape into the gate's click handler."""
    import app.elevation as el

    frozen()

    def boom(cmd, **kwargs):
        raise subprocess.TimeoutExpired(cmd, 30)

    monkeypatch.setattr(el.subprocess, "run", boom)

    from app.elevation import relaunch_as_admin
    assert relaunch_as_admin() is False

    assert launch.cleared[-1] == "token"
    assert any("exception" in msg for msg in launch.log)


def test_a_token_that_cannot_be_written_does_not_block_the_prompt(launch, frozen,
                                                                 monkeypatch, tmp_path):
    """The real `_write_pending_token` swallows its own failure, so this runs it
    rather than a stub. The token file is what the wait loop fails closed on;
    the UAC prompt the user asked for should still appear."""
    import app.elevation as el

    frozen()
    monkeypatch.setattr(el, "_write_pending_token", launch.original_write)
    monkeypatch.setattr(el, "_get_elevation_token_path",
                        lambda: str(tmp_path / "no-such-dir" / "token"))

    from app.elevation import relaunch_as_admin
    assert relaunch_as_admin() is True, "a failed token write must not cancel the prompt"

    assert any("could not write pending elevation token" in msg for msg in launch.log)
    assert f"--elevation-token={TOKEN}" in _unquote(_argument_list_literal(_ps_cmd(launch)))
