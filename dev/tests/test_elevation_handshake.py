"""The elevation handshake files: cookie and pending token, in a temp dir.

These two files are how the unelevated app recognises the elevated copy it just
asked for: the cookie carries `pid[:token]` written BY the elevated side, the
token file carries the random token written by the unelevated side BEFORE the
UAC prompt. Get either wrong and the app either accepts a stranger's process as
"our elevated copy" or hangs waiting for one that already arrived, so the
round-trip and the failure modes are pinned here.

The interesting case is SEC-002. Both names are predictable, so a same-user
attacker can pre-create the path as a symlink to a file they want written to
(an autorun script, say) and a plain `open(path, "w")` would follow it. The
helper unlinks first and then creates with O_EXCL, and these tests hold it to
that: a planted link is removed, its target is untouched, and a pre-existing
entry that could NOT be removed makes the write fail loudly instead of being
truncated.

Every path is redirected into pytest's tmp_path, and the security event log is
stubbed, so nothing here touches the real %TEMP% or the user's config.
"""
from __future__ import annotations

import os
import stat

import pytest

#: Bound before conftest's autouse guard replaces the module attribute. One test
#: below asserts what the REAL logger writes to disk, so it needs the original.
from app.elevation import _log_or_swallow_fallback as _REAL_LOG


@pytest.fixture
def tempdir(tmp_path, monkeypatch):
    """Redirect the module's temp dir (cookie, token AND its debug log)."""
    from types import SimpleNamespace

    import app.elevation as el

    d = tmp_path / "tmp"
    d.mkdir()
    monkeypatch.setattr(el, "tempfile", SimpleNamespace(gettempdir=lambda: str(d)))
    return d


@pytest.fixture
def logs(monkeypatch):
    """Capture calls to the module's best-effort logger."""
    import app.elevation as el

    calls = []
    monkeypatch.setattr(el, "_log_or_swallow_fallback",
                        lambda msg, exc=None: calls.append((msg, exc)))
    return calls


@pytest.fixture
def events(monkeypatch):
    """The security event log, so a logged failure does not hit the real one."""
    import app.config_persist as cp

    seen = []
    monkeypatch.setattr(cp, "log_security_event", lambda kind, text: seen.append((kind, text)))
    return seen


def test_both_handshake_files_live_in_the_temp_dir(tempdir):
    """Predictable names are a known, accepted cost of the handshake (the
    mitigation is the exclusive write), so pin the names: a rename here would
    silently stop the two sides finding each other."""
    from app.elevation import _get_elevation_cookie_path, _get_elevation_token_path

    assert _get_elevation_cookie_path() == os.path.join(str(tempdir),
                                                        "CleanerTool_elevation_cookie")
    assert _get_elevation_token_path() == os.path.join(str(tempdir),
                                                       "CleanerTool_elevation_token")


# --------------------------------------------------------------------------- #
# _exclusive_write: the SEC-002 primitive both writers sit on.
# --------------------------------------------------------------------------- #

def test_exclusive_write_creates_the_file_with_the_data(tempdir):
    from app.elevation import _exclusive_write

    path = tempdir / "file"
    _exclusive_write(str(path), "1234:tok")

    assert path.read_text(encoding="utf-8") == "1234:tok"


def test_exclusive_write_replaces_an_existing_entry(tempdir):
    """A stale cookie from a previous launch must not block the next one."""
    from app.elevation import _exclusive_write

    path = tempdir / "file"
    path.write_text("stale:old", encoding="utf-8")

    _exclusive_write(str(path), "new")

    assert path.read_text(encoding="utf-8") == "new"


def test_exclusive_write_does_not_write_through_a_planted_symlink(tempdir):
    """SEC-002: the attack this exists for. Pre-create the predictable path as
    a link to a file the attacker wants written; the helper must unlink the
    link and create its own file, leaving the target byte-for-byte alone."""
    from app.elevation import _exclusive_write

    victim = tempdir / "victim.cmd"
    victim.write_text("del /q C:\\*", encoding="utf-8")
    planted = tempdir / "cookie"
    try:
        os.symlink(str(victim), str(planted))
    except (OSError, NotImplementedError):
        pytest.skip("symlinks need developer mode / privileges here")

    _exclusive_write(str(planted), "1234")

    assert victim.read_text(encoding="utf-8") == "del /q C:\\*", "the target is untouched"
    assert not planted.is_symlink(), "the planted link was removed"
    assert planted.read_text(encoding="utf-8") == "1234"


def test_exclusive_write_does_not_write_through_a_planted_hardlink(tempdir):
    """The same SEC-002 property as the symlink case, and the one that runs on a
    stock Windows box: a hardlink needs no privileges. Writing through it would
    modify the victim's contents (same file), so the unlink-then-create order
    is what protects it."""
    from app.elevation import _exclusive_write

    victim = tempdir / "victim.cmd"
    victim.write_text("original", encoding="utf-8")
    planted = tempdir / "cookie"
    try:
        os.link(str(victim), str(planted))
    except (OSError, NotImplementedError):
        pytest.skip("hardlinks unavailable on this filesystem")

    _exclusive_write(str(planted), "1234")

    assert victim.read_text(encoding="utf-8") == "original", "the target is untouched"
    assert planted.read_text(encoding="utf-8") == "1234"


def test_exclusive_write_refuses_an_entry_it_cannot_replace(tempdir, monkeypatch):
    """O_EXCL is the second half of the guard: if the unlink did not remove the
    entry (the attacker won the race, or the file is locked), the write must
    fail rather than truncate whatever is there."""
    import app.elevation as el

    path = tempdir / "file"
    path.write_text("ATTACKER", encoding="utf-8")
    monkeypatch.setattr(el.os, "unlink", lambda p: None)   # the removal "fails"

    with pytest.raises(FileExistsError):
        el._exclusive_write(str(path), "ours")

    assert path.read_text(encoding="utf-8") == "ATTACKER", "never written through"


def test_exclusive_write_raises_when_it_cannot_create_the_file(tempdir):
    """The caller catches this and aborts the handshake — it must not be
    swallowed here, or a failed handshake looks like a successful one."""
    from app.elevation import _exclusive_write

    with pytest.raises(FileNotFoundError):
        _exclusive_write(str(tempdir / "no-such-dir" / "file"), "x")


@pytest.mark.skipif(os.name != "posix", reason="POSIX file modes only")
def test_exclusive_write_is_owner_only_on_posix(tempdir):
    from app.elevation import _exclusive_write

    path = tempdir / "file"
    _exclusive_write(str(path), "x")

    assert stat.S_IMODE(path.stat().st_mode) == 0o600


# --------------------------------------------------------------------------- #
# The cookie: written by the elevated side, read by the waiting one.
# --------------------------------------------------------------------------- #

def test_cookie_round_trips_pid_and_token(tempdir):
    from app.elevation import _read_elevation_cookie, _write_elevation_cookie

    _write_elevation_cookie(4321, "a1b2c3")

    assert _read_elevation_cookie() == (4321, "a1b2c3")


def test_cookie_without_a_token_is_just_the_pid(tempdir):
    from app.elevation import _read_elevation_cookie, _write_elevation_cookie

    _write_elevation_cookie(4321)

    assert _read_elevation_cookie() == (4321, "")


def test_cookie_token_may_contain_a_colon(tempdir):
    """Split on the FIRST colon, or a token that ever gains a separator would
    silently parse as a different pid."""
    from app.elevation import _read_elevation_cookie, _write_elevation_cookie

    _write_elevation_cookie(7, "a:b:c")

    assert _read_elevation_cookie() == (7, "a:b:c")


def test_a_cookie_with_padding_reads_the_same(tempdir):
    from app.elevation import _get_elevation_cookie_path, _read_elevation_cookie

    with open(_get_elevation_cookie_path(), "w", encoding="utf-8") as f:
        f.write("  42 : tok \n")

    assert _read_elevation_cookie() == (42, "tok")


def test_a_missing_cookie_is_no_pending_elevation_and_is_not_logged(tempdir, logs):
    """The normal case on a plain launch: not an error, so no log entry."""
    from app.elevation import _read_elevation_cookie

    assert _read_elevation_cookie() == (None, "")
    assert logs == []


@pytest.mark.parametrize("content", ["", "not-a-pid", "abc:tok", ":tok", "42x"])
def test_a_garbage_cookie_reads_as_nothing_and_is_logged(tempdir, logs, content):
    """A corrupt cookie must not be mistaken for a pending elevation (which
    would make the app wait for a process that does not exist), and it must
    leave a trail instead of vanishing."""
    from app.elevation import _get_elevation_cookie_path, _read_elevation_cookie

    with open(_get_elevation_cookie_path(), "w", encoding="utf-8") as f:
        f.write(content)

    assert _read_elevation_cookie() == (None, "")
    assert [m for m, _e in logs] == ["could not read elevation cookie"]
    assert logs[0][1] is not None, "the exception is passed on for the log"


def test_a_successful_cookie_write_leaves_the_file(tempdir):
    from app.elevation import _get_elevation_cookie_path, _write_elevation_cookie

    _write_elevation_cookie(99, "t")

    assert os.path.exists(_get_elevation_cookie_path())


def test_clearing_the_cookie_removes_it(tempdir):
    from app.elevation import (
        _clear_elevation_cookie,
        _get_elevation_cookie_path,
        _write_elevation_cookie,
    )

    _write_elevation_cookie(99)
    _clear_elevation_cookie()

    assert not os.path.exists(_get_elevation_cookie_path())


def test_clearing_a_cookie_that_is_not_there_is_silent(tempdir):
    """Cleanup runs on paths where nothing was ever written; it must not
    raise into a launch or a timeout handler."""
    from app.elevation import _clear_elevation_cookie

    _clear_elevation_cookie()          # must not raise


def test_a_failed_cookie_write_is_logged_and_does_not_raise(tempdir, monkeypatch,
                                                           events):
    """REL-001: a write failure strands the user mid-handshake (the waiting side
    never learns the elevated copy arrived), so it is logged — but it can never
    take down the launch. The real logger runs here, redirected to tempdir."""
    import app.elevation as el

    monkeypatch.setattr(el, "_log_or_swallow_fallback", _REAL_LOG)
    monkeypatch.setattr(el, "_get_elevation_cookie_path",
                        lambda: str(tempdir / "no-such-dir" / "cookie"))

    el._write_elevation_cookie(4321, "tok")          # must not raise

    log = tempdir / "cleaner_tool_elevation.log"
    assert log.exists(), "the failure left a trail on disk"
    assert "could not write elevation cookie" in log.read_text(encoding="utf-8")
    assert any("could not write elevation cookie" in text for _kind, text in events)


# --------------------------------------------------------------------------- #
# The pending token: written before the UAC prompt, read by the elevated side.
# --------------------------------------------------------------------------- #

def test_token_round_trips(tempdir):
    from app.elevation import _read_pending_token, _write_pending_token

    _write_pending_token("deadbeef")

    assert _read_pending_token() == "deadbeef"


def test_a_missing_token_reads_as_none(tempdir, logs):
    from app.elevation import _read_pending_token

    assert _read_pending_token() is None
    assert logs == []


def test_an_unreadable_token_reads_as_none_and_is_logged(tempdir, logs):
    """Something exists at the path but is not a readable file."""
    from app.elevation import _get_elevation_token_path, _read_pending_token

    os.makedirs(_get_elevation_token_path())

    assert _read_pending_token() is None
    assert [m for m, _e in logs] == ["could not read pending elevation token"]


def test_a_failed_token_write_is_logged_and_does_not_raise(tempdir, monkeypatch,
                                                           logs):
    import app.elevation as el

    monkeypatch.setattr(el, "_get_elevation_token_path",
                        lambda: str(tempdir / "no-such-dir" / "token"))

    el._write_pending_token("deadbeef")              # must not raise

    assert [m for m, _e in logs] == ["could not write pending elevation token"]


def test_clearing_the_token_removes_it_and_is_silent_when_absent(tempdir):
    from app.elevation import (
        _clear_pending_token,
        _get_elevation_token_path,
        _write_pending_token,
    )

    _write_pending_token("deadbeef")
    _clear_pending_token()
    assert not os.path.exists(_get_elevation_token_path())

    _clear_pending_token()                           # must not raise


# --------------------------------------------------------------------------- #
# _current_exe_path: what the app believes it is. The Defender exclusion is
# taken from this, so "frozen exe, never the folder" starts here.
# --------------------------------------------------------------------------- #

def test_a_frozen_build_is_its_own_exe_and_ignores_argv(monkeypatch, tmp_path):
    """PyInstaller sets sys.frozen and points sys.executable at the launched
    .exe; argv[0] in that build is not a path worth trusting (it can be
    relative, or a script name from the build machine)."""
    import app.elevation as el

    exe = str(tmp_path / "Apps" / "CleanerTool.exe")
    monkeypatch.setattr(el.sys, "frozen", True, raising=False)
    monkeypatch.setattr(el.sys, "executable", exe)
    monkeypatch.setattr(el.sys, "argv", [str(tmp_path / "ignored.py"), "--flag"])

    assert el._current_exe_path() == os.path.abspath(exe)


def test_a_script_run_is_argv_zero(monkeypatch, tmp_path):
    """Unfrozen, `python main.py`: the script is the thing being run."""
    import app.elevation as el

    script = tmp_path / "main.py"
    monkeypatch.setattr(el.sys, "frozen", False, raising=False)
    monkeypatch.setattr(el.sys, "argv", [str(script)])
    monkeypatch.setattr(el.sys, "executable", "python.exe")

    assert el._current_exe_path() == os.path.abspath(str(script))


def test_an_empty_argv_zero_falls_back_to_the_interpreter(monkeypatch):
    """`argv[0]` is empty when the app is started in ways that leave it blank;
    the interpreter is a better answer than the empty string, which would make
    abspath() hand back the cwd."""
    import app.elevation as el

    monkeypatch.setattr(el.sys, "frozen", False, raising=False)
    monkeypatch.setattr(el.sys, "argv", [""])
    monkeypatch.setattr(el.sys, "executable", "python.exe")

    assert el._current_exe_path() == os.path.abspath("python.exe")
    assert el._current_exe_path() != os.path.abspath("")


def test_no_argv_at_all_falls_back_to_the_interpreter(monkeypatch):
    """An empty argv list raises IndexError on argv[0]; that must not escape
    and must not cost the caller its answer."""
    import app.elevation as el

    monkeypatch.setattr(el.sys, "frozen", False, raising=False)
    monkeypatch.setattr(el.sys, "argv", [])
    monkeypatch.setattr(el.sys, "executable", "python.exe")

    assert el._current_exe_path() == os.path.abspath("python.exe")


def test_an_unresolvable_path_falls_back_to_the_interpreter(monkeypatch):
    from types import SimpleNamespace

    import app.elevation as el

    calls = []

    def abspath(p):
        calls.append(p)
        if len(calls) == 1:
            raise ValueError("bad path")
        return "resolved:" + str(p)

    monkeypatch.setattr(el, "os", SimpleNamespace(path=SimpleNamespace(abspath=abspath)))
    monkeypatch.setattr(el.sys, "frozen", False, raising=False)
    monkeypatch.setattr(el.sys, "argv", ["weird"])
    monkeypatch.setattr(el.sys, "executable", "python.exe")

    assert el._current_exe_path() == "resolved:python.exe"
    assert calls == ["weird", "python.exe"], "tries argv[0] first, then the interpreter"


def test_nothing_resolvable_returns_empty(monkeypatch):
    """The caller treats "" as "no path to exclude" and skips the PowerShell
    call, so this must be an empty string rather than an exception or a cwd."""
    from types import SimpleNamespace

    import app.elevation as el

    def abspath(_p):
        raise OSError("no idea where I am")

    monkeypatch.setattr(el, "os", SimpleNamespace(path=SimpleNamespace(abspath=abspath)))
    monkeypatch.setattr(el.sys, "frozen", False, raising=False)
    monkeypatch.setattr(el.sys, "argv", ["weird"])

    assert el._current_exe_path() == ""


def test_the_token_survives_the_cookie_being_cleared(tempdir):
    """Two files, two lifetimes: the token is cleared when the handshake ends,
    the cookie when the elevated pid is consumed. Clearing one must not touch
    the other, or a retry would wait for a token that no longer exists."""
    from app.elevation import (
        _clear_elevation_cookie,
        _read_pending_token,
        _write_elevation_cookie,
        _write_pending_token,
    )

    _write_pending_token("deadbeef")
    _write_elevation_cookie(4321, "deadbeef")

    _clear_elevation_cookie()

    assert _read_pending_token() == "deadbeef"
