"""The wait loop that decides an elevated copy of the app actually came back.

`_wait_for_elevated_process` is the piece that turns "we asked for admin" into
"elevation succeeded": it polls the cookie for 60 s and only accepts a pid when
the cookie's token matches the token we wrote before the UAC prompt, the pid is
still alive, and that pid's image is this app. Everything else — a cookie from
an unrelated running copy, a pid that already exited, a spoofed image name — has
to read as "keep waiting", never as success, and a give-up has to clean up so a
later attempt cannot mistake a late cookie for its own.

The clock is fake (`time()` moves only when `sleep()` is called), so every
outcome is deterministic: a 1 s timeout is exactly five 200 ms polls, with no
wall-clock waiting. The cookie, token, process handles and logger are all
in-memory; nothing here touches %TEMP%, Windows APIs, or the security event log.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest


@pytest.fixture
def clock(monkeypatch):
    """Fake clock: time only advances when the loop sleeps."""
    import app.elevation as el

    now = [1000.0]

    def sleep(seconds):
        now[0] += seconds

    # B-014: the loop's deadline is monotonic now, so the fake clock has to
    # answer for it too (one shared counter - the test still owns time).
    monkeypatch.setattr(el, "time", SimpleNamespace(
        time=lambda: now[0], monotonic=lambda: now[0], sleep=sleep))
    return now


class _DWORD:
    def __init__(self, value=0):
        self.value = value


class _Kernel32:
    """The three kernel32 calls the loop makes, plus what to return."""

    def __init__(self):
        self.open_calls = []
        self.closed = []
        self.handle = True          # OpenProcess succeeds
        self.exit_code = 259        # STILL_ACTIVE
        self.raise_on_open = False

    def OpenProcess(self, access, inherit, pid):
        self.open_calls.append(pid)
        if self.raise_on_open:
            raise OSError("access denied")
        return 1 if self.handle else 0

    def GetExitCodeProcess(self, handle, target):
        target.value = self.exit_code
        return True

    def CloseHandle(self, handle):
        self.closed.append(handle)


@pytest.fixture
def system(monkeypatch):
    """Cookie, token, process image and kernel32, all in memory."""
    import app.config_persist as cp
    import app.elevation as el

    kernel = _Kernel32()
    monkeypatch.setattr(el, "ctypes", SimpleNamespace(
        windll=SimpleNamespace(kernel32=kernel),
        wintypes=SimpleNamespace(DWORD=_DWORD),
        byref=lambda obj: obj,
    ))

    state = SimpleNamespace(
        token="a1b2c3d4",
        own_exe="cleanertool.exe",
        image="cleanertool.exe",
        cookie=(None, ""),
        reads=0,
        cleared=[],
        logged=[],
        events=[],
    )
    #: callable(cookie) -> (pid, token) for the next read
    state.on_read = lambda: state.cookie

    def read_cookie():
        state.reads += 1
        return state.on_read()

    monkeypatch.setattr(el, "_read_pending_token", lambda: state.token)
    monkeypatch.setattr(el, "_read_elevation_cookie", read_cookie)
    monkeypatch.setattr(el, "_clear_pending_token", lambda: state.cleared.append("token"))
    monkeypatch.setattr(el, "_clear_elevation_cookie", lambda: state.cleared.append("cookie"))
    monkeypatch.setattr(el, "_get_own_exe_name", lambda: state.own_exe)
    monkeypatch.setattr(el, "_process_image_name", lambda pid: state.image)
    monkeypatch.setattr(el, "_log_or_swallow_fallback",
                        lambda msg, exc=None: state.logged.append(msg))
    monkeypatch.setattr(cp, "log_security_event",
                        lambda kind, text: state.events.append((kind, text)))
    return SimpleNamespace(state=state, kernel=kernel)


def test_a_good_hand_back_is_accepted_on_the_first_poll(clock, system):
    """The cookie names a live process with our token and our image: that is
    the elevated copy, and the wait ends and cleans up immediately."""
    s = system.state
    s.cookie = (4321, "a1b2c3d4")

    from app.elevation import _wait_for_elevated_process
    assert _wait_for_elevated_process(timeout=60.0) is True

    assert s.reads == 1, "no need to poll again"
    assert system.kernel.open_calls == [4321], "the cookie's pid is the one checked"
    assert system.kernel.closed == [1], "the handle is closed on the way out"
    assert s.cleared == ["cookie", "token"], "a finished handshake leaves no files"
    assert any("elevation succeeded" in msg for msg in s.logged)
    assert [kind for kind, _t in s.events] == ["elevation"]


def test_the_wait_accepts_a_cookie_that_arrives_late(clock, system):
    """Every normal elevation has a UAC pause in it: several empty polls, then
    the cookie. Empty polls must not be treated as failure."""
    s = system.state
    calls = []

    def on_read():
        calls.append(len(calls))
        return (9999, "a1b2c3d4") if len(calls) >= 3 else (None, "")

    s.on_read = on_read

    from app.elevation import _wait_for_elevated_process
    assert _wait_for_elevated_process(timeout=60.0) is True
    assert s.reads == 3
    assert s.cleared == ["cookie", "token"]


def test_a_cookie_with_the_wrong_token_is_not_ours(clock, system):
    """A same-user process can write the predictable cookie path. Without a
    matching token it is ignored — however plausible the pid looks."""
    s = system.state
    seen = []

    def on_read():
        seen.append(1)
        return (1234, "someone-else") if len(seen) <= 2 else (4321, "a1b2c3d4")

    s.on_read = on_read

    from app.elevation import _wait_for_elevated_process
    assert _wait_for_elevated_process(timeout=60.0) is True

    assert s.reads == 3
    assert sum("token mismatch" in msg for msg in s.logged) == 2
    assert system.kernel.open_calls == [4321], (
        "the rejected pid was never even opened")


@pytest.mark.parametrize("expected,cookie_token", [
    (None, ""),        # unreadable token file
    ("", ""),          # token file present but empty — the dangerous pair
])
def test_without_a_readable_pending_token_every_cookie_is_refused(
        clock, system, expected, cookie_token):
    """H10: a transient token-read failure used to skip the match check and
    accept any cookie. The empty/empty pair is why the guard is separate from
    the comparison — with both sides blank, `token != expected_token` is False,
    so only the explicit `not expected_token` check keeps the cookie out.

    Give up when the timeout expires, cleaning both files so a late cookie
    cannot be adopted by a later wait."""
    s = system.state
    s.token = expected
    s.cookie = (4321, cookie_token)

    from app.elevation import _wait_for_elevated_process
    assert _wait_for_elevated_process(timeout=1.0) is False

    assert system.kernel.open_calls == [], "no cookie is trusted without a token"
    assert s.cleared == ["token", "cookie"]
    assert any("timed out" in msg for msg in s.logged)


def test_a_live_process_with_the_wrong_image_is_not_ours(clock, system):
    """Token plus a live pid is not enough on its own: the pid has to be an
    instance of this app, not some other process the cookie happens to name."""
    s = system.state
    s.cookie = (4321, "a1b2c3d4")
    s.image = "notepad.exe"

    from app.elevation import _wait_for_elevated_process
    assert _wait_for_elevated_process(timeout=1.0) is False

    assert sum("image name mismatch" in msg for msg in s.logged) == 5
    assert s.cleared == ["token", "cookie"]


def test_a_process_that_is_no_longer_alive_keeps_waiting(clock, system):
    """The elevated copy can exit immediately (a crash, or it handed over). A
    cookie naming a dead pid is not a successful hand-back."""
    s = system.state
    s.cookie = (4321, "a1b2c3d4")
    system.kernel.exit_code = 0

    from app.elevation import _wait_for_elevated_process
    assert _wait_for_elevated_process(timeout=1.0) is False

    assert s.reads == 5, "a 1 s timeout is five 200 ms polls"
    assert s.cleared == ["token", "cookie"]


def test_a_pid_that_cannot_be_opened_keeps_waiting(clock, system):
    s = system.state
    s.cookie = (4321, "a1b2c3d4")
    system.kernel.handle = False

    from app.elevation import _wait_for_elevated_process
    assert _wait_for_elevated_process(timeout=1.0) is False

    assert system.kernel.closed == [], "nothing to close"
    assert s.cleared == ["token", "cookie"]


def test_a_raising_process_check_is_logged_and_does_not_escape(clock, system):
    """ctypes failing is a reason to keep waiting, not to crash the caller's
    worker thread — the gate is blocked on this answer."""
    s = system.state
    s.cookie = (4321, "a1b2c3d4")
    system.kernel.raise_on_open = True

    from app.elevation import _wait_for_elevated_process
    assert _wait_for_elevated_process(timeout=1.0) is False      # must not raise

    assert sum("exception checking process" in msg for msg in s.logged) == 5
    assert s.cleared == ["token", "cookie"]


def test_the_public_wrapper_passes_its_timeout_through(monkeypatch):
    """Callers use the public name; the default has to stay the 60 s that
    covers UAC click time plus a slow --onefile unpack."""
    import app.elevation as el

    seen = []
    monkeypatch.setattr(el, "_wait_for_elevated_process",
                        lambda timeout=60.0: seen.append(timeout) or "answer")

    assert el.wait_for_elevated_process() == "answer"
    assert el.wait_for_elevated_process(5.0) == "answer"
    assert seen == [60.0, 5.0]


def test_a_failed_success_event_does_not_turn_success_into_a_timeout(
        clock, system, monkeypatch):
    """The event log is observability. If it is unavailable the handshake has
    still succeeded: report the failed write, keep the verdict."""
    import app.config_persist as cp

    s = system.state
    s.cookie = (4321, "a1b2c3d4")

    def refuse(kind, text):
        raise OSError("events log is read-only")

    monkeypatch.setattr(cp, "log_security_event", refuse)

    from app.elevation import _wait_for_elevated_process
    assert _wait_for_elevated_process(timeout=60.0) is True

    assert s.cleared == ["cookie", "token"]
    assert any("could not record the elevation success event" in msg
               for msg in s.logged)


def test_a_failed_timeout_event_does_not_change_the_verdict(
        clock, system, monkeypatch):
    """Same for the give-up: a missing event log must not turn a clean
    `False` into an exception in the caller's worker thread."""
    import app.config_persist as cp

    s = system.state
    s.cookie = None

    def refuse(kind, text):
        raise OSError("events log is read-only")

    monkeypatch.setattr(cp, "log_security_event", refuse)

    from app.elevation import _wait_for_elevated_process
    assert _wait_for_elevated_process(timeout=0.0) is False

    assert s.cleared == ["token", "cookie"]
    assert any("could not record the elevation timeout event" in msg
               for msg in s.logged)


def test_a_zero_timeout_polls_nothing_and_still_cleans_up(clock, system):
    s = system.state
    s.cookie = (4321, "a1b2c3d4")

    from app.elevation import _wait_for_elevated_process
    assert _wait_for_elevated_process(timeout=0.0) is False

    assert s.reads == 0
    assert s.cleared == ["token", "cookie"]
    assert [kind for kind, _t in s.events] == ["elevation"], (
        "the give-up is recorded once")
