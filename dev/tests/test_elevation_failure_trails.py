"""The failure trails in app/elevation.py.

Every helper here is best-effort by design: a cookie that cannot be cleared, a
record that cannot be written or an admin check that throws must never break a
launch. The code used to express that with `pass`, which made a failure look
exactly like success in every log and support session — the exclusion record
could be lost and nothing anywhere said so.

So the contract these tests pin is two-sided:

  * the failure never escapes (every one of these returns its value or None),
    and
  * it names itself, with the exception, through `_log_or_swallow_fallback`.

The logger's own sinks are the exception to the second half. It IS the last
resort, so a broken sink is reported once on stderr instead of recursively
through itself, and its final `except: pass` is asserted never to raise: it is
called from `except` blocks whose original exception must not be replaced by a
logging one.

Nothing here touches %TEMP%, the real config, Defender or PowerShell.
"""
from __future__ import annotations

import os
import re
from types import SimpleNamespace

import pytest

#: The real logger, bound before conftest's autouse fixture replaces the module
#: attribute: these tests assert what it *writes*, not that it was called.
from app.elevation import _log_or_swallow_fallback as _real_log


@pytest.fixture
def tmpenv(tmp_path, monkeypatch):
    """The module's temp dir, so the cookie, token and debug log stay local."""
    import app.elevation as el

    d = tmp_path / "tmp"
    d.mkdir()
    monkeypatch.setattr(el, "tempfile", SimpleNamespace(gettempdir=lambda: str(d)))
    return d


@pytest.fixture
def trail(monkeypatch):
    """Every best-effort log call, in order: (message, exception)."""
    import app.elevation as el

    calls = []
    monkeypatch.setattr(el, "_log_or_swallow_fallback",
                        lambda msg, exc=None: calls.append((msg, exc)))
    return calls


@pytest.fixture
def events(monkeypatch):
    """The security event log, so a real one is never appended to."""
    import app.config_persist as cp

    seen = []
    monkeypatch.setattr(cp, "log_security_event",
                        lambda kind, text: seen.append((kind, text)))
    return seen


def _os(**overrides):
    """A mirror of `os` with `overrides` replaced (nested under `path=`).

    Patching `el.os` rather than the global `os` keeps the failure inside the
    code under test: breaking `os.path.abspath` process-wide would also break
    pytest's own bookkeeping half way through the run.
    """
    fake = SimpleNamespace(
        **{k: getattr(os, k) for k in dir(os) if not k.startswith("_")})
    path = {k: getattr(os.path, k)
            for k in dir(os.path) if not k.startswith("_")}
    path.update(overrides.pop("path", {}))
    fake.path = SimpleNamespace(**path)
    for key, value in overrides.items():
        setattr(fake, key, value)
    return fake


def _raiser(exc):
    def refuse(*args, **kwargs):
        raise exc
    return refuse


# --------------------------------------------------------------------------- #
# _log_or_swallow_fallback: the trail, and its own two sinks.
# --------------------------------------------------------------------------- #

def test_a_working_logger_reaches_both_sinks(tmpenv, events, capsys):
    _real_log("something went wrong")

    assert "! something went wrong" in capsys.readouterr().err
    log = (tmpenv / "cleaner_tool_elevation.log").read_text(encoding="utf-8")
    assert "! something went wrong" in log
    assert str(os.getpid()) in log, "the pid identifies the writing process"
    assert events == [("ops", "! something went wrong")]


def test_the_exception_is_named_on_every_sink(tmpenv, events, capsys):
    """The type and text are the whole point: "could not write" alone leaves a
    support session guessing between a lock, a permission problem and a bug."""
    _real_log("could not write", ValueError("disk full"))

    assert "(ValueError: disk full)" in capsys.readouterr().err
    log = (tmpenv / "cleaner_tool_elevation.log").read_text(encoding="utf-8")
    assert "(ValueError: disk full)" in log
    assert "(ValueError: disk full)" in events[-1][1]


def test_a_dead_tempfile_sink_is_reported_once_and_keeps_the_event(
        tmp_path, monkeypatch, events, capsys):
    """A read-only %TEMP% makes this sink fail on EVERY log call. Repeating the
    warning would bury the message the logger exists to carry, so it is once
    per process — and the other sink must still be fed."""
    import app.elevation as el

    blocker = tmp_path / "not-a-directory"
    blocker.write_text("", encoding="utf-8")
    monkeypatch.setattr(el, "tempfile",
                        SimpleNamespace(gettempdir=lambda: str(blocker)))
    monkeypatch.setattr(el, "_reported_sinks", set())

    _real_log("first")
    _real_log("second")

    err = capsys.readouterr().err
    assert err.count("sink 'tempfile' is failing") == 1
    # Windows maps ENOTDIR onto FileNotFoundError, so the type is asserted as
    # one of the two rather than pinned to the POSIX spelling.
    assert re.search(r"failing \((FileNotFoundError|NotADirectoryError)", err), err
    assert [text for _kind, text in events] == ["! first", "! second"], (
        "one dead sink must not take the other down with it")


def test_a_dead_event_sink_is_reported_once_and_the_file_still_gets_the_line(
        tmpenv, monkeypatch, capsys):
    import app.config_persist as cp
    import app.elevation as el

    monkeypatch.setattr(cp, "log_security_event",
                        _raiser(OSError("events log is read-only")))
    monkeypatch.setattr(el, "_reported_sinks", set())

    _real_log("first")
    _real_log("second")

    err = capsys.readouterr().err
    assert err.count("sink 'security-event' is failing") == 1
    assert "OSError: events log is read-only" in err
    log = (tmpenv / "cleaner_tool_elevation.log").read_text(encoding="utf-8")
    assert log.count("! first") == 1 and log.count("! second") == 1


def test_the_logger_never_raises_when_stderr_is_gone(tmpenv, monkeypatch):
    """It is called from inside `except` blocks: raising here would replace the
    original exception with a logging error, which is worse than silence."""
    import builtins

    monkeypatch.setattr(builtins, "print", _raiser(OSError("stderr is closed")))

    assert _real_log("cannot be printed") is None


def test_the_sink_reporter_swallows_a_broken_stderr(tmp_path, monkeypatch, events):
    """The once-only report is itself inside an `except`; if stderr dies between
    the message and the report, the report is what gives way — and the logger
    still goes on to feed its remaining sink rather than unwinding."""
    import builtins

    import app.elevation as el

    blocker = tmp_path / "not-a-directory"
    blocker.write_text("", encoding="utf-8")
    monkeypatch.setattr(el, "tempfile",
                        SimpleNamespace(gettempdir=lambda: str(blocker)))
    monkeypatch.setattr(el, "_reported_sinks", set())

    real_print = builtins.print
    calls = []

    def flaky(*args, **kwargs):
        calls.append(args)
        if len(calls) > 1:          # the message printed, the report did not
            raise OSError("stderr gone")
        return real_print(*args, **kwargs)

    monkeypatch.setattr(builtins, "print", flaky)

    assert _real_log("first line") is None
    assert len(calls) == 2, "the report was attempted"
    assert events == [("ops", "! first line")], (
        "a failed report must not abort the sinks after it")


# --------------------------------------------------------------------------- #
# is_admin: the cause behind every "could not check elevation".
# --------------------------------------------------------------------------- #

def test_a_failing_admin_query_answers_false_and_says_why(trail, monkeypatch):
    import app.elevation as el

    err = OSError("shell32 unavailable")
    monkeypatch.setattr(el, "ctypes", SimpleNamespace(windll=SimpleNamespace(
        shell32=SimpleNamespace(IsUserAnAdmin=_raiser(err)))))

    assert el.is_admin() is False
    assert trail == [("could not query admin status", err)]


def test_a_plain_non_admin_answer_is_not_a_failure(trail, monkeypatch):
    import app.elevation as el

    monkeypatch.setattr(el, "ctypes", SimpleNamespace(windll=SimpleNamespace(
        shell32=SimpleNamespace(IsUserAnAdmin=lambda: 0))))

    assert el.is_admin() is False
    assert trail == [], "a real 'no' is the normal limited-mode answer"


# --------------------------------------------------------------------------- #
# The ctypes probes: one trail per site, however often they are polled.
# --------------------------------------------------------------------------- #

@pytest.fixture
def fresh_failure_sites(monkeypatch):
    """No site has reported yet — the real set lives for the whole process."""
    import app.elevation as el

    monkeypatch.setattr(el, "_reported_failures", set())


def _raising_ctypes(exc):
    def boom(*args, **kwargs):
        raise exc
    return SimpleNamespace(windll=SimpleNamespace(
        kernel32=SimpleNamespace(OpenProcess=boom)))


def test_a_broken_image_name_query_is_reported_once(trail, fresh_failure_sites,
                                                    monkeypatch):
    """This runs at 5 Hz while a UAC prompt is open, so an uncapped trail would
    write 300 identical lines and bury the wait's own messages."""
    import app.elevation as el

    err = OSError("ctypes blew up")
    monkeypatch.setattr(el, "ctypes", _raising_ctypes(err))

    assert el._process_image_name(4242) is None
    assert el._process_image_name(4242) is None

    assert trail == [("could not query the image name for pid 4242", err)]


def test_a_broken_exe_path_query_is_reported_once(trail, fresh_failure_sites,
                                                 monkeypatch):
    """Auto-Pilot reads "" as "the game is not under a watched folder", so a
    silent failure here is a feature that never fires."""
    import app.elevation as el

    err = OSError("ctypes blew up")
    monkeypatch.setattr(el, "ctypes", _raising_ctypes(err))

    assert el.process_exe_path(4242) == ""
    assert el.process_exe_path(4242) == ""

    assert trail == [("could not query the full path for pid 4242", err)]


def test_a_broken_elevation_probe_is_reported_once(trail, fresh_failure_sites,
                                                  monkeypatch):
    """False is a plausible answer, so a broken query and a genuinely limited
    token are indistinguishable to the caller without the trail."""
    import app.elevation as el

    err = OSError("ctypes blew up")
    monkeypatch.setattr(el, "ctypes", _raising_ctypes(err))

    assert el._process_is_elevated(4242) is False
    assert el._process_is_elevated(4242) is False

    assert trail == [("could not check whether pid 4242 is elevated", err)]


def test_each_ctypes_site_gets_its_own_first_report(trail, fresh_failure_sites,
                                                    monkeypatch):
    """The cap is per site, not global: one broken query must not silence the
    other two."""
    import app.elevation as el

    monkeypatch.setattr(el, "ctypes", _raising_ctypes(OSError("ctypes blew up")))

    el._process_image_name(1)
    el.process_exe_path(1)
    el._process_is_elevated(1)

    assert [msg for msg, _exc in trail] == [
        "could not query the image name for pid 1",
        "could not query the full path for pid 1",
        "could not check whether pid 1 is elevated",
    ]


# --------------------------------------------------------------------------- #
# Clearing the handshake files: absent is normal, unremovable is not.
# --------------------------------------------------------------------------- #

def test_clearing_handshake_files_that_are_absent_is_normal(tmpenv, trail):
    """Every elevation clears a cookie before one exists — this path must stay
    quiet or a normal UAC prompt writes two errors to the log."""
    import app.elevation as el

    el._clear_elevation_cookie()
    el._clear_pending_token()

    assert trail == []


@pytest.mark.parametrize("name,message", [
    ("_clear_elevation_cookie", "could not clear elevation cookie"),
    ("_clear_pending_token", "could not clear pending elevation token"),
])
def test_a_handshake_file_that_cannot_be_removed_is_recorded(
        tmpenv, trail, monkeypatch, name, message):
    """A stale cookie left behind is what lets a later wait adopt somebody
    else's process, so the reason it survived has to be on the record."""
    import app.elevation as el

    err = PermissionError("in use by another process")
    monkeypatch.setattr(el, "os", _os(remove=_raiser(err)))

    getattr(el, name)()

    assert trail == [(message, err)]


# --------------------------------------------------------------------------- #
# "could not check elevation": all three entry points report the same cause.
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("call", [
    pytest.param(lambda el: el.remove_defender_exclusions(),
                 id="remove_defender_exclusions"),
    pytest.param(lambda el: el.remove_path_exclusion(r"C:\Apps\CleanerTool.exe"),
                 id="remove_path_exclusion"),
    pytest.param(lambda el: el.ensure_defender_exclusion(),
                 id="ensure_defender_exclusion"),
])
def test_an_admin_check_that_throws_is_visible_at_every_entry_point(
        trail, monkeypatch, call):
    """The message already told the user "could not check elevation" — but not
    what threw, and it is the only thing standing between them and a removal
    that quietly does not happen."""
    import app.elevation as el

    err = OSError("kernel32 unavailable")
    monkeypatch.setattr(el, "is_admin", _raiser(err))

    assert call(el) == (False, "could not check elevation")
    assert trail == [("could not check elevation", err)], (
        "the message and the trail must name the same cause")


# --------------------------------------------------------------------------- #
# The exclusion record: read and write are what removal depends on.
# --------------------------------------------------------------------------- #

def test_a_record_of_the_wrong_shape_reads_as_no_record(trail, monkeypatch):
    import app.config_persist as cp
    import app.elevation as el

    monkeypatch.setattr(cp, "load_config",
                        lambda: {"defender_exclusions_applied": r"C:\Apps"})

    assert el._applied_exclusions() == []
    assert trail == [], "an odd config value is not an error we can act on"


def test_an_unreadable_config_reads_as_no_record_and_says_why(trail, monkeypatch):
    """[] is what the removal path acts on by removing nothing, so a read
    failure must not be indistinguishable from an empty record."""
    import app.config_persist as cp
    import app.elevation as el

    err = OSError("config is locked")
    monkeypatch.setattr(cp, "load_config", _raiser(err))

    assert el._applied_exclusions() == []
    assert trail == [("could not read the applied-exclusion record", err)]


def test_a_failed_record_write_never_raises_and_leaves_a_trail(trail, monkeypatch):
    """This is the swallow that shipped broken (`_mut()` instead of `_mut`):
    the write failed, nothing was recorded, and the removal path spent months
    with nothing to remove and no evidence of why."""
    import app.config_persist as cp
    import app.elevation as el

    err = OSError("config is read-only")
    monkeypatch.setattr(cp, "update_config", _raiser(err))

    assert el._record_applied_exclusions([r"C:\Apps\CleanerTool.exe"]) is None
    assert trail == [("could not record the applied exclusions", err)]


def test_an_empty_record_means_there_is_nothing_to_forget(trail, monkeypatch):
    import app.elevation as el

    written = []
    monkeypatch.setattr(el, "_applied_exclusions", lambda: [])
    monkeypatch.setattr(el, "_record_applied_exclusions",
                        lambda *a, **k: written.append(a))

    el._forget_exclusion(r"C:\Apps\CleanerTool.exe")

    assert written == [], "no record, no rewrite"
    assert trail == []


def test_a_path_that_cannot_be_normalised_is_reported(trail, monkeypatch):
    """Failing to normalise silently skips the subtraction, which leaves the
    record claiming an exclusion Defender no longer has."""
    import app.elevation as el

    path = r"C:\Apps\CleanerTool.exe"
    err = ValueError("embedded null byte")
    monkeypatch.setattr(el, "_applied_exclusions", lambda: [path])
    monkeypatch.setattr(el, "os", _os(path={"normpath": _raiser(err)}))

    el._forget_exclusion(path)

    assert trail == [(f"could not normalise exclusion path {path!r}", err)]


def test_a_failure_while_forgetting_is_reported_and_never_raised(trail, monkeypatch):
    import app.elevation as el

    err = RuntimeError("record exploded")
    monkeypatch.setattr(el, "_applied_exclusions", _raiser(err))

    assert el._forget_exclusion(r"C:\Apps\CleanerTool.exe") is None
    assert trail == [("could not forget a removed exclusion", err)]


def test_powershell_that_cannot_run_reads_as_no_exclusions_and_says_why(
        trail, monkeypatch):
    """[] is also the answer for "Defender is not installed", which is what the
    migration notice reasons about — so the two cases must not look alike."""
    import app.elevation as el

    err = FileNotFoundError("powershell vanished")
    monkeypatch.setattr(el, "_run_mp_script", _raiser(err))

    assert el.list_defender_exclusions() == []
    assert trail == [("could not list Defender exclusions", err)]


# --------------------------------------------------------------------------- #
# remove_defender_exclusions: the path list it resolves.
# --------------------------------------------------------------------------- #

def test_an_unresolvable_path_is_reported_and_the_others_still_go(trail, monkeypatch):
    import app.elevation as el

    bad = "C:\\Apps\\bad\x00"
    good = r"C:\Apps\CleanerTool.exe"
    err = ValueError("embedded null byte")

    def abspath(p):
        if p == bad:
            raise err
        return os.path.abspath(p)

    scripts = []
    monkeypatch.setattr(el, "is_admin", lambda: True)
    monkeypatch.setattr(el, "_applied_exclusions", lambda: [])
    monkeypatch.setattr(el, "_run_mp_script",
                        lambda script: scripts.append(script) or (True, ""))
    monkeypatch.setattr(el, "_record_applied_exclusions", lambda *a, **k: None)
    monkeypatch.setattr(el, "os", _os(path={"abspath": abspath}))

    assert el.remove_defender_exclusions([bad, good]) == (
        True, "removed 1 exclusion(s)")
    assert len(scripts) == 1 and good in scripts[0]
    assert trail == [(f"could not resolve exclusion path {bad!r}", err)]


def test_empty_and_non_string_paths_are_ignored(trail, monkeypatch):
    import app.elevation as el

    scripts = []
    monkeypatch.setattr(el, "is_admin", lambda: True)
    monkeypatch.setattr(el, "_applied_exclusions", lambda: [])
    monkeypatch.setattr(el, "_run_mp_script",
                        lambda script: scripts.append(script) or (True, ""))

    assert el.remove_defender_exclusions(["", None, 42]) == (
        True, "no exclusions recorded by this app")
    assert scripts == [], "no command for an empty list"
    assert trail == []


# --------------------------------------------------------------------------- #
# remove_path_exclusion / ensure_defender_exclusion.
# --------------------------------------------------------------------------- #

def test_a_removal_without_admin_is_refused_with_a_reason(trail, monkeypatch):
    import app.elevation as el

    monkeypatch.setattr(el, "is_admin", lambda: False)

    assert el.remove_path_exclusion(r"C:\Apps") == (
        False, "not running as administrator")
    assert trail == []


@pytest.mark.parametrize("path", ["", None, 123])
def test_a_removal_needs_a_path(trail, monkeypatch, path):
    import app.elevation as el

    scripts = []
    monkeypatch.setattr(el, "is_admin", lambda: True)
    monkeypatch.setattr(el, "_run_mp_script",
                        lambda script: scripts.append(script) or (True, ""))

    assert el.remove_path_exclusion(path) == (False, "no path given")
    assert scripts == [], "an empty path must never reach Remove-MpPreference"
    assert trail == []


def test_a_powershell_that_raises_is_returned_as_the_message(trail, monkeypatch):
    """The message IS the trail for this one: the caller shows it to the user,
    so it must carry the failure rather than a bare "failed"."""
    import app.elevation as el

    monkeypatch.setattr(el, "is_admin", lambda: True)
    monkeypatch.setattr(el, "_run_mp_script", _raiser(RuntimeError("cannot spawn")))

    assert el.remove_path_exclusion(r"C:\Apps") == (
        False, "RuntimeError: cannot spawn")
    assert trail == []


def test_an_unresolvable_explicit_path_leaves_nothing_to_exclude(trail, monkeypatch):
    import app.elevation as el

    bad = "C:\\Apps\\bad\x00"
    err = ValueError("embedded null byte")
    monkeypatch.setattr(el, "is_admin", lambda: True)
    monkeypatch.setattr(el, "os", _os(path={"abspath": _raiser(err)}))

    assert el.ensure_defender_exclusion([bad]) == (False, "no path to exclude")
    assert trail == [(f"could not resolve exclusion path {bad!r}", err)]


def test_a_startup_apply_that_throws_never_breaks_the_launch(trail, monkeypatch):
    """Runs on every elevated launch before the window exists; a config or
    Defender problem here must not become a failure to start."""
    import app.config_persist as cp
    import app.elevation as el

    err = OSError("config locked")
    monkeypatch.setattr(el, "is_admin", lambda: True)
    monkeypatch.setattr(cp, "load_config", _raiser(err))

    assert el.apply_defender_exclusion_if_wanted() is None
    assert trail == [("could not apply the Defender exclusion setting", err)]


def test_a_blocked_add_is_recorded(trail, monkeypatch, tmp_path):
    """Opting in is a promise that the binary is excluded. A policy block or a
    third-party AV answers with a non-zero exit and no exception, so this used
    to be the one place where nothing at all was written."""
    import app.config_persist as cp
    import app.elevation as el

    monkeypatch.setattr(el, "is_admin", lambda: True)
    monkeypatch.setattr(cp, "load_config",
                        lambda: {"add_defender_exclusion": True})
    monkeypatch.setattr(el, "_current_exe_path",
                        lambda: str(tmp_path / "CleanerTool.exe"))
    monkeypatch.setattr(el, "_record_applied_exclusions", lambda *a, **k: None)
    monkeypatch.setattr(el, "_run_mp_script",
                        lambda s: (False, "Add-MpPreference : Access is denied."))

    assert el.apply_defender_exclusion_if_wanted() is None
    assert trail == [("could not add the Defender exclusion: "
                      "Add-MpPreference : Access is denied.", None)]


def test_a_blocked_removal_after_opt_out_is_recorded(trail, monkeypatch):
    """The user asked for the exclusion to go and it did not go. The record
    still names it, so every later launch retries this — silently, until now."""
    import app.config_persist as cp
    import app.elevation as el

    recorded = [r"C:\Apps\CleanerTool.exe"]
    monkeypatch.setattr(el, "is_admin", lambda: True)
    monkeypatch.setattr(cp, "load_config",
                        lambda: {"add_defender_exclusion": False})
    monkeypatch.setattr(el, "_applied_exclusions", lambda: recorded)
    monkeypatch.setattr(el, "_run_mp_script",
                        lambda s: (False, "Remove-MpPreference : Access is denied."))

    assert el.apply_defender_exclusion_if_wanted() is None
    assert trail == [("could not remove the recorded exclusions: "
                      "Remove-MpPreference : Access is denied.", None)]


def test_a_successful_setting_change_stays_quiet(trail, monkeypatch, tmp_path):
    """The trail is for failures: a working add must not write one."""
    import app.config_persist as cp
    import app.elevation as el

    monkeypatch.setattr(el, "is_admin", lambda: True)
    monkeypatch.setattr(cp, "load_config",
                        lambda: {"add_defender_exclusion": True})
    monkeypatch.setattr(el, "_current_exe_path",
                        lambda: str(tmp_path / "CleanerTool.exe"))
    monkeypatch.setattr(el, "_record_applied_exclusions", lambda *a, **k: None)
    monkeypatch.setattr(el, "_run_mp_script", lambda s: (True, ""))

    assert el.apply_defender_exclusion_if_wanted() is None
    assert trail == []


# --------------------------------------------------------------------------- #
# relaunch_as_admin: the failure path that also has to clean up.
# --------------------------------------------------------------------------- #

def test_a_relaunch_that_fails_and_cannot_clean_up_leaves_both_trails(
        trail, monkeypatch):
    """Both lines matter and they are different failures: the launch failed,
    and the token that would have made the NEXT attempt unambiguous is still
    on disk. Neither may escape — the caller is a Tk button handler."""
    import app.elevation as el

    launch_err = OSError("cookie locked")
    token_err = OSError("token locked")
    monkeypatch.setattr(el, "_clear_elevation_cookie", _raiser(launch_err))
    monkeypatch.setattr(el, "_clear_pending_token", _raiser(token_err))

    assert el.relaunch_as_admin() is False
    assert trail == [
        ("relaunch_as_admin: exception", launch_err),
        ("relaunch_as_admin: could not clear the pending token", token_err),
    ]
