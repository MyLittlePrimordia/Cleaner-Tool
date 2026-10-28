"""Reading a pid's executable: `_process_image_name` and `process_exe_path`.

These two answer the same Windows question at different lengths, and both feed
decisions where a wrong answer matters:

* the elevation wait compares `_process_image_name(pid)` against this app's own
  basename — a cookie naming some other live process must not count as "our
  elevated copy came back", and both sides lowercase so `CLEANERTOOL.EXE` still
  matches;
* the Auto-Pilot matches a process's FULL path against a watched game folder, so
  `process_exe_path` must return the path intact (case and all, no basename) or
  a same-named exe elsewhere would trigger a session.

Both are best-effort by design: a pid that cannot be opened, a failed query and
any exception all come back as "unknown" (`None` / `""`), never as an error, and
the handle is closed whenever there was one to close. ctypes is stubbed here, so
no Windows process is touched.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest


class _DWORD:
    def __init__(self, value=0):
        self.value = value


class _Buffer:
    def __init__(self, capacity):
        self.capacity = capacity
        self.value = ""


class _Kernel32:
    """The three calls these functions make, and what to answer with."""

    def __init__(self):
        self.image = ""
        self.handle = True
        self.query_ok = True
        self.raise_on_open = False
        self.raise_on_query = False
        self.opened = []
        self.closed = []
        self.capacities = []

    def OpenProcess(self, access, inherit, pid):
        self.opened.append((access, inherit, pid))
        if self.raise_on_open:
            raise OSError("access denied")
        return 7 if self.handle else 0

    def QueryFullProcessImageNameW(self, handle, flags, buf, size):
        self.capacities.append(buf.capacity)
        if self.raise_on_query:
            raise OSError("query exploded")
        if not self.query_ok:
            return 0
        buf.value = self.image
        size.value = len(self.image)
        return 1

    def CloseHandle(self, handle):
        self.closed.append(handle)


@pytest.fixture
def procs(monkeypatch):
    import app.elevation as el

    kernel = _Kernel32()
    monkeypatch.setattr(el, "ctypes", SimpleNamespace(
        windll=SimpleNamespace(kernel32=kernel),
        wintypes=SimpleNamespace(DWORD=_DWORD),
        create_unicode_buffer=lambda n: _Buffer(n),
        byref=lambda obj: obj,
    ))
    return kernel


@pytest.fixture
def own(monkeypatch, tmp_path):
    """This app is `C:\\Apps\\CleanerTool.exe`, as far as the comparison goes."""
    import app.elevation as el

    exe = str(tmp_path / "Apps" / "CleanerTool.exe")
    monkeypatch.setattr(el.sys, "executable", exe)
    return exe


# --------------------------------------------------------------------------- #
# _process_image_name: the basename the handshake compares.
# --------------------------------------------------------------------------- #

def test_a_valid_pid_yields_the_basename(procs, own):
    procs.image = own

    from app.elevation import _process_image_name
    assert _process_image_name(4242) == "cleanertool.exe"

    assert procs.opened == [(0x1000, False, 4242)], (
        "PROCESS_QUERY_LIMITED_INFORMATION is the least privilege that answers this")
    assert procs.closed == [7], "the handle is always closed"


def test_the_name_matches_ours_whatever_the_case(procs, own):
    """Both ends lowercase: a portable copy's `CLEANERTOOL.EXE` is the same
    program as our `CleanerTool.exe`, and the wait must not time out on case."""
    procs.image = "D:\\Portable\\CLEANERTOOL.EXE"

    from app.elevation import _get_own_exe_name, _process_image_name
    assert _process_image_name(1) == _get_own_exe_name()


def test_a_different_image_is_not_our_name(procs, own):
    """The mismatch the handshake rejects: a live pid whose image is some other
    program cannot be the elevated copy, however right its token looks."""
    procs.image = r"C:\Windows\System32\notepad.exe"

    from app.elevation import _get_own_exe_name, _process_image_name
    name = _process_image_name(1)
    assert name == "notepad.exe"
    assert name != _get_own_exe_name()


def test_a_pid_that_cannot_be_opened_is_unknown(procs, own):
    procs.handle = False

    from app.elevation import _process_image_name
    assert _process_image_name(4242) is None
    assert procs.closed == [], "nothing was opened, so nothing is closed"


def test_a_failed_query_is_unknown_but_still_closes_the_handle(procs, own):
    procs.query_ok = False

    from app.elevation import _process_image_name
    assert _process_image_name(4242) is None
    assert procs.closed == [7]


def test_a_raising_openprocess_is_unknown(procs, own):
    procs.raise_on_open = True

    from app.elevation import _process_image_name
    assert _process_image_name(4242) is None       # must not raise


def test_a_raising_query_is_unknown_and_the_handle_is_closed(procs, own):
    procs.raise_on_query = True

    from app.elevation import _process_image_name
    assert _process_image_name(4242) is None
    assert procs.closed == [7], "the finally block still runs"


def test_the_name_buffer_is_the_260_character_max_path(procs, own):
    """The API truncates at the buffer size. 260 is the classic MAX_PATH; a
    longer path comes back cut, which can only ever cause a mismatch — it can
    never produce a false match — so the limit is a documented trade."""
    procs.image = own

    from app.elevation import _process_image_name
    _process_image_name(1)

    assert procs.capacities == [260]


def test_a_truncated_path_cannot_match_our_name_by_accident(procs, own):
    procs.image = "C:\\" + ("x" * 400)

    from app.elevation import _get_own_exe_name, _process_image_name
    name = _process_image_name(1)
    assert name is not None
    assert name != _get_own_exe_name()


# --------------------------------------------------------------------------- #
# process_exe_path: the whole path the Auto-Pilot matches folders against.
# --------------------------------------------------------------------------- #

def test_the_full_path_comes_back_intact(procs):
    """Case and separators preserved: this is compared as a path prefix, so
    lowercasing or truncating to a basename would break the match."""
    procs.image = r"D:\Games\Baldur's Gate 3\bin\bg3.exe"

    from app.elevation import process_exe_path
    assert process_exe_path(4242) == r"D:\Games\Baldur's Gate 3\bin\bg3.exe"
    assert procs.closed == [7]


def test_the_two_lookups_agree_on_the_same_pid(procs):
    procs.image = r"D:\Games\Baldur's Gate 3\bin\bg3.exe"

    from app.elevation import _process_image_name, process_exe_path
    assert process_exe_path(1).lower().endswith(_process_image_name(1)), (
        "full path and basename are views of the same process")


def test_a_pid_that_cannot_be_opened_has_no_path(procs):
    procs.handle = False

    from app.elevation import process_exe_path
    assert process_exe_path(4242) == ""
    assert procs.closed == []


def test_a_failed_query_has_no_path(procs):
    procs.query_ok = False

    from app.elevation import process_exe_path
    assert process_exe_path(4242) == ""
    assert procs.closed == [7]


def test_an_empty_answer_is_an_empty_path(procs):
    procs.image = ""

    from app.elevation import process_exe_path
    assert process_exe_path(4242) == ""


def test_an_exception_has_no_path(procs):
    procs.raise_on_open = True

    from app.elevation import process_exe_path
    assert process_exe_path(4242) == ""       # must not raise


def test_the_path_buffer_holds_a_long_install_path(procs):
    """1024 rather than 260: a game under a deep `SteamLibrary` path must not
    come back cut off, because a cut path fails the folder match and the session
    would silently never start."""
    procs.image = "C:\\" + "\\".join(["a" * 20] * 40) + "\\game.exe"

    from app.elevation import process_exe_path
    assert process_exe_path(1) == procs.image
    assert procs.capacities == [1024]


# --------------------------------------------------------------------------- #
# _get_own_exe_name: the other side of the comparison.
# --------------------------------------------------------------------------- #

def test_our_own_name_is_the_lowercased_basename(own):
    from app.elevation import _get_own_exe_name
    assert _get_own_exe_name() == "cleanertool.exe"


def test_our_own_name_is_the_interpreter_for_a_script_run(monkeypatch):
    """`python main.py`: the elevated copy is another python.exe, so the
    interpreter is the right thing to compare — not the script."""
    import app.elevation as el

    monkeypatch.setattr(el.sys, "executable", r"C:\Python315\PYTHON.EXE")

    from app.elevation import _get_own_exe_name
    assert _get_own_exe_name() == "python.exe"
