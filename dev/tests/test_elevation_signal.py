"""The elevated side of the handshake: `signal_elevated_startup()`.

The original (unelevated) window is blocked in the wait loop for up to 60 s.
This function is what ends that wait: the elevated copy writes the cookie
`pid:token` and the waiter accepts it only if the token matches the one it
wrote before the UAC prompt. Everything here is about *when* that cookie may be
written:

* only from a process that is actually elevated — a non-elevated start must
  never claim to be the relaunch, or a stale token left in %TEMP% would let a
  later ordinary launch answer the original window's wait;
* only with a token — from argv when the relaunch supplied one, otherwise from
  the pending token file, and never at all when neither exists.

Both refusals are silent to the user (they leave a debug-log line), so the tests
assert the absence of a write as well as the log entry. Nothing is written to
disk and no admin check touches the OS: the cookie writer, token reader, admin
probe and logger are all stubs.
"""
from __future__ import annotations

import os
from types import SimpleNamespace

import pytest


@pytest.fixture
def signal(monkeypatch):
    """Stubbed handshake ends. `writes` records what would have been written."""
    import app.elevation as el

    calls = SimpleNamespace(
        writes=[], log=[], admin_checks=0,
        pending=None,          # what the token file would read as
        is_admin=True,
        admin_raises=False,
    )

    def fake_admin():
        calls.admin_checks += 1
        if calls.admin_raises:
            raise OSError("cannot query the token")
        return calls.is_admin

    monkeypatch.setattr(el, "is_admin", fake_admin)
    monkeypatch.setattr(el, "_read_pending_token", lambda: calls.pending)
    monkeypatch.setattr(el, "_write_elevation_cookie",
                        lambda pid, token="": calls.writes.append((pid, token)))
    monkeypatch.setattr(el, "_log_or_swallow_fallback",
                        lambda msg, exc=None: calls.log.append((msg, exc)))
    return calls


def _argv(monkeypatch, *args):
    import app.elevation as el
    monkeypatch.setattr(el.sys, "argv", list(args))


def test_a_token_from_argv_is_echoed_into_the_cookie(signal, monkeypatch):
    _argv(monkeypatch, "CleanerTool.exe", "--elevation-token=abc123")

    from app.elevation import signal_elevated_startup
    signal_elevated_startup()

    assert signal.writes == [(os.getpid(), "abc123")]


def test_the_pending_token_file_is_the_fallback(signal, monkeypatch):
    """Started elevated without the flag (an older relaunch, or a re-run): the
    token it is expected to echo is the one the other side wrote down."""
    _argv(monkeypatch, "CleanerTool.exe")
    signal.pending = "deadbeef"

    from app.elevation import signal_elevated_startup
    signal_elevated_startup()

    assert signal.writes == [(os.getpid(), "deadbeef")]


def test_argv_wins_over_the_pending_file(signal, monkeypatch):
    """The flag is what this launch was actually told to echo; the file may be
    a leftover from an earlier attempt."""
    _argv(monkeypatch, "CleanerTool.exe", "--elevation-token=from-argv")
    signal.pending = "from-file"

    from app.elevation import signal_elevated_startup
    signal_elevated_startup()

    assert signal.writes == [(os.getpid(), "from-argv")]


def test_an_empty_flag_falls_back_to_the_pending_token(signal, monkeypatch):
    """`--elevation-token=` with nothing after it is not a token."""
    _argv(monkeypatch, "CleanerTool.exe", "--elevation-token=")
    signal.pending = "from-file"

    from app.elevation import signal_elevated_startup
    signal_elevated_startup()

    assert signal.writes == [(os.getpid(), "from-file")]


def test_a_token_containing_equals_signs_survives(signal, monkeypatch):
    _argv(monkeypatch, "--elevation-token=a=b=c")

    from app.elevation import signal_elevated_startup
    signal_elevated_startup()

    assert signal.writes == [(os.getpid(), "a=b=c")]


def test_the_first_flag_wins(signal, monkeypatch):
    _argv(monkeypatch, "--elevation-token=first", "--elevation-token=second")

    from app.elevation import signal_elevated_startup
    signal_elevated_startup()

    assert signal.writes == [(os.getpid(), "first")]


def test_without_a_token_and_without_a_pending_file_nothing_is_written(
        signal, monkeypatch):
    """A plain, unrelated start of the app: no token anywhere means this is not
    the relaunch, so it must stay silent — and must not even ask whether it is
    elevated, because the answer cannot change the outcome."""
    _argv(monkeypatch, "CleanerTool.exe")
    signal.pending = None

    from app.elevation import signal_elevated_startup
    signal_elevated_startup()

    assert signal.writes == []
    assert signal.admin_checks == 0
    assert [m for m, _e in signal.log] == [
        "signal_elevated_startup: no token and no pending token"]


def test_a_non_elevated_process_never_signals(signal, monkeypatch):
    """The spoof guard: a token is not enough. A medium-integrity process
    holding the token must not answer the wait, or the user gets a window that
    silently has no admin rights."""
    _argv(monkeypatch, "CleanerTool.exe", "--elevation-token=abc123")
    signal.is_admin = False

    from app.elevation import signal_elevated_startup
    signal_elevated_startup()

    assert signal.writes == []
    assert [m for m, _e in signal.log] == [
        "signal_elevated_startup: not admin, cannot signal"]


def test_a_failing_admin_check_is_treated_as_not_elevated(signal, monkeypatch):
    """If we cannot prove we are elevated, we do not claim to be."""
    _argv(monkeypatch, "CleanerTool.exe", "--elevation-token=abc123")
    signal.admin_raises = True

    from app.elevation import signal_elevated_startup
    signal_elevated_startup()

    assert signal.writes == []
    assert [m for m, _e in signal.log] == [
        "signal_elevated_startup: admin check failed"]
    assert isinstance(signal.log[0][1], OSError), "the cause is kept for the log"


def test_an_empty_pending_file_is_still_a_pending_file(signal, monkeypatch):
    """The trigger is the file's EXISTENCE, not its contents, so a truncated
    token file produces a pid-only cookie. That is harmless rather than a hole:
    the waiter refuses every cookie while its own expected token is blank, which
    is covered in dev/tests/test_elevation_wait.py.
    """
    _argv(monkeypatch, "CleanerTool.exe")
    signal.pending = ""

    from app.elevation import signal_elevated_startup
    signal_elevated_startup()

    assert signal.writes == [(os.getpid(), "")]
