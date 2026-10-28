"""`_process_is_elevated(pid)`: does that process hold an admin token?

This is the strongest of the handshake's identity checks, and it is the one the
wait loop deliberately does NOT require (a medium-integrity caller often gets
ACCESS_DENIED reading a high-integrity child's token, which used to turn every
successful elevation into a false timeout). It is still the answer to "is this
pid really running with admin rights", so it has to be right in both directions
and has to give every handle back:

    OpenProcess -> OpenProcessToken -> GetTokenInformation(TokenElevation)

Every early return must close what was already opened, and every failure —
including an exception from ctypes — must read as "not elevated", never escape.
ctypes is stubbed, so no process is opened and no token is queried.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest


class _DWORD:
    def __init__(self, value=0):
        self.value = value


class _Handle:
    def __init__(self, value=0):
        self.value = value


class _Kernel32:
    def __init__(self):
        self.process_handle = True
        self.token_handle = True
        self.raise_on_open = False
        self.raise_on_token = False
        self.opened = []
        self.token_opens = []
        self.closed = []

    def OpenProcess(self, access, inherit, pid):
        self.opened.append((access, inherit, pid))
        if self.raise_on_open:
            raise OSError("access denied")
        return 101 if self.process_handle else 0

    def OpenProcessToken(self, handle, access, token_ref):
        self.token_opens.append((handle, access))
        if self.raise_on_token:
            raise OSError("access denied")
        if self.token_handle:
            token_ref.value = 202
            return 1
        return 0

    def CloseHandle(self, handle):
        # the token is passed as the HANDLE object ctypes filled in; record the
        # value so assertions read as handles, not object identities
        self.closed.append(getattr(handle, "value", handle))


class _Advapi32:
    def __init__(self):
        self.answer = 1
        self.query_ok = True
        self.raise_on_query = False
        self.calls = []

    def GetTokenInformation(self, token, info_class, elevation_ref, size, ret_ref):
        self.calls.append((getattr(token, "value", token), info_class, size))
        if self.raise_on_query:
            raise OSError("query exploded")
        if not self.query_ok:
            return 0
        elevation_ref.value = self.answer
        ret_ref.value = size
        return 1


@pytest.fixture
def probe(monkeypatch):
    import app.elevation as el

    kernel = _Kernel32()
    advapi = _Advapi32()
    monkeypatch.setattr(el, "ctypes", SimpleNamespace(
        windll=SimpleNamespace(kernel32=kernel, advapi32=advapi),
        wintypes=SimpleNamespace(HANDLE=_Handle, DWORD=_DWORD),
        byref=lambda obj: obj,
        sizeof=lambda obj: 4,
    ))
    return SimpleNamespace(kernel=kernel, advapi=advapi)


def test_an_elevated_process_is_reported_elevated(probe):
    probe.advapi.answer = 1

    from app.elevation import _process_is_elevated
    assert _process_is_elevated(4242) is True

    assert probe.kernel.opened == [(0x1000, False, 4242)], (
        "PROCESS_QUERY_LIMITED_INFORMATION — enough to read the token, nothing more")
    assert probe.kernel.token_opens == [(101, 0x0008)], "TOKEN_QUERY"
    assert probe.advapi.calls == [(202, 1, 4)], (
        "TokenElevation, with the DWORD's real size")
    assert probe.kernel.closed == [202, 101], (
        "the token first, then the process handle")


def test_a_non_elevated_process_is_reported_as_such(probe):
    probe.advapi.answer = 0

    from app.elevation import _process_is_elevated
    assert _process_is_elevated(4242) is False
    assert probe.kernel.closed == [202, 101]


@pytest.mark.parametrize("value,want", [(1, True), (2, True), (0, False)])
def test_the_answer_is_the_elevation_dword_not_its_exact_value(probe, value, want):
    """TOKEN_ELEVATION is a DWORD flag; any non-zero value means elevated."""
    probe.advapi.answer = value

    from app.elevation import _process_is_elevated
    assert _process_is_elevated(1) is want


def test_a_pid_that_cannot_be_opened_is_not_elevated(probe):
    probe.kernel.process_handle = False

    from app.elevation import _process_is_elevated
    assert _process_is_elevated(4242) is False
    assert probe.kernel.token_opens == [], "no token is asked for"
    assert probe.kernel.closed == [], "nothing was opened, so nothing to close"


def test_a_token_that_cannot_be_opened_is_not_elevated(probe):
    """A protected process refuses the token open; the process handle is still
    ours to close."""
    probe.kernel.token_handle = False

    from app.elevation import _process_is_elevated
    assert _process_is_elevated(4242) is False
    assert probe.advapi.calls == [], "no query without a token"
    assert probe.kernel.closed == [101]


def test_a_failed_token_query_is_not_elevated(probe):
    probe.advapi.query_ok = False

    from app.elevation import _process_is_elevated
    assert _process_is_elevated(4242) is False
    assert probe.kernel.closed == [202, 101], "both handles come back"


def test_a_raising_openprocess_is_not_elevated(probe):
    probe.kernel.raise_on_open = True

    from app.elevation import _process_is_elevated
    assert _process_is_elevated(4242) is False       # must not raise
    assert probe.kernel.closed == []


def test_a_raising_token_open_is_not_elevated_and_closes_the_process(probe):
    probe.kernel.raise_on_token = True

    from app.elevation import _process_is_elevated
    assert _process_is_elevated(4242) is False
    assert probe.kernel.closed == [101]


def test_a_raising_token_query_is_not_elevated_and_closes_both(probe):
    probe.advapi.raise_on_query = True

    from app.elevation import _process_is_elevated
    assert _process_is_elevated(4242) is False
    assert probe.kernel.closed == [202, 101]


def test_the_probe_is_never_asked_about_its_own_pid_by_accident(probe):
    """The pid is passed straight through — a caller checking pid 0 (the idle
    process) must not be silently redirected to this process."""
    probe.kernel.process_handle = False

    from app.elevation import _process_is_elevated
    _process_is_elevated(0)

    assert probe.kernel.opened == [(0x1000, False, 0)]
