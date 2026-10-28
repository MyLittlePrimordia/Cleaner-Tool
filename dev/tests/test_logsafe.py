"""B-003 / H-001 / H-003 / I-003: secrets never reach a shareable artifact.

The elevation handshake's token used to be written verbatim by
`relaunch_as_admin` (its argv, command line and PowerShell string all carry
`--elevation-token=<secret>`), by `is_elevation_child`, and by the wait loop's
diagnostics. Those sinks feed `%TEMP%` trails and the event log, and
`export_logs()` copies the event tail into whatever file the user saves for a
bug report. These tests pin the two halves of the fix: `app.logsafe.redact()`
removes the value, and every sink in that chain goes through it.
"""
from __future__ import annotations

import os
import re
from types import SimpleNamespace

import pytest

from app import logsafe
from app.logsafe import redact

#: Bound before conftest's autouse fixture replaces the module attribute, so
#: these tests assert what the REAL logger writes into its sinks.
from app.elevation import _log_or_swallow_fallback as _real_elevation_log

_TOKEN = "0123456789abcdef0123456789abcdef"


# --------------------------------------------------------------------------- #
# redact(): narrow on purpose.
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("text,expected", [
    (f"--elevation-token={_TOKEN}", "--elevation-token=<redacted>"),
    (f"--elevation-token {_TOKEN}", "--elevation-token <redacted>"),
    (f"'--elevation-token={_TOKEN}'", "'--elevation-token=<redacted>'"),
    (f"token={_TOKEN}", "token=<redacted>"),
    (f"token: {_TOKEN}", "token: <redacted>"),
    (r"C:\Users\Alice\AppData\Local\Temp\x.log",
     r"C:\Users\<user>\AppData\Local\Temp\x.log"),
    ("C:/Users/Bob/x", "C:/Users/<user>/x"),
    # nothing to redact stays exactly as written
    ("Downloading https://example.test/x.exe", "Downloading https://example.test/x.exe"),
    ("plain text", "plain text"),
    ("", ""),
])
def test_redact_removes_only_the_secret_and_the_user(text, expected):
    assert redact(text) == expected


def test_a_digest_stays_readable_for_a_support_bundle():
    """A release digest is verification evidence, not a credential."""
    digest = "a" * 64
    assert redact("SHA-256 verified (" + digest + ")") == (
        "SHA-256 verified (" + digest + ")")


def test_redact_never_raises_on_junk():
    assert redact(None) == ""
    assert redact(123) == "123"


def test_redaction_covers_an_exported_tail_line_for_line():
    """The export hands the whole payload to `redact` in one call
    (app/ui/logsurface.py), so a tail must come back line-for-line."""
    payload = "\n".join([
        "Cleaner Tool log export",
        f"[ops] ! relaunch argv=['--elevation-token={_TOKEN}']",
        "[ops] ! plain failure",
    ]) + "\n"
    out = redact(payload).splitlines()
    assert out[0] == "Cleaner Tool log export"
    assert _TOKEN not in "\n".join(out)
    assert "<redacted>" in out[1]
    assert out[2] == "[ops] ! plain failure"   # untouched line, not dropped


# --------------------------------------------------------------------------- #
# H-001: link-safe, bounded trail files.
# --------------------------------------------------------------------------- #

@pytest.fixture
def trail_file(tmp_path):
    return str(tmp_path / "trail.log")


def test_append_is_link_safe_and_capped(trail_file, monkeypatch):
    monkeypatch.setattr(logsafe, "_TRAIL_KEEP_LINES", 3)
    for i in range(10):
        logsafe.append_text_no_follow(trail_file, f"line {i}\n", max_bytes=40)
    text = open(trail_file, encoding="utf-8").read()
    assert "line 9" in text
    assert "line 0" not in text
    assert len(text) < 400


def test_append_refuses_a_link(tmp_path, trail_file):
    target = tmp_path / "target.txt"
    target.write_text("keep me", encoding="utf-8")
    link = str(tmp_path / "link.log")
    try:
        os.symlink(target, link)
    except (OSError, NotImplementedError):
        pytest.skip("this account cannot create symlinks")
    with pytest.raises(OSError):
        logsafe.append_text_no_follow(link, "evil\n")
    assert target.read_text(encoding="utf-8") == "keep me"


def test_read_refuses_a_link(tmp_path):
    target = tmp_path / "secret.txt"
    target.write_text(_TOKEN, encoding="utf-8")
    link = str(tmp_path / "link.txt")
    try:
        os.symlink(target, link)
    except (OSError, NotImplementedError):
        pytest.skip("this account cannot create symlinks")
    with pytest.raises(OSError):
        logsafe.read_text_no_follow(link)


def test_read_returns_a_normal_file(tmp_path):
    path = tmp_path / "plain.txt"
    path.write_text("hello", encoding="utf-8")
    assert logsafe.read_text_no_follow(str(path)) == "hello"


# --------------------------------------------------------------------------- #
# H-002: one writer across processes.
# --------------------------------------------------------------------------- #

def test_the_lock_is_held_against_a_second_handle(tmp_path):
    """A threading.Lock serializes one process; the event log has two."""
    path = str(tmp_path / "trail.log")
    with logsafe.file_lock(path) as outer:
        assert outer is True
        with logsafe.file_lock(path, timeout=0.05) as inner:
            assert inner is False
    with logsafe.file_lock(path, timeout=0.05) as again:
        assert again is True


def test_the_lock_degrades_instead_of_blocking_forever(tmp_path):
    path = str(tmp_path / "nope" / "trail.log")      # no such directory
    with logsafe.file_lock(path, timeout=0.01) as got:
        assert got is False


def test_an_append_still_lands_when_the_lock_is_unavailable(tmp_path,
                                                           monkeypatch):
    path = str(tmp_path / "trail.log")
    monkeypatch.setattr(logsafe, "_try_lock", lambda fd: False)
    logsafe.append_text_no_follow(path, "line\n")
    assert open(path, encoding="utf-8").read() == "line\n"


# --------------------------------------------------------------------------- #
# B-003: the elevation sinks.
# --------------------------------------------------------------------------- #

@pytest.fixture
def tmp_trail(tmp_path, monkeypatch):
    """Point elevation's %TEMP% at a local dir and return it."""
    import app.elevation as el

    d = tmp_path / "tmp"
    d.mkdir()
    monkeypatch.setattr(el, "tempfile",
                        SimpleNamespace(gettempdir=lambda: str(d)))
    return d


def test_the_elevation_trail_also_reaches_the_crash_log(tmp_trail, monkeypatch):
    """I-003: a %TEMP% trail nobody opens is not a report."""
    import app.diagnostics as diag

    seen = []
    monkeypatch.setattr(diag, "log", lambda message, level=None: seen.append(
        str(message)), raising=True)
    _real_elevation_log("cookie read failed")
    assert any("cookie read failed" in s for s in seen), seen


def test_the_single_instance_trail_also_reaches_the_crash_log(
        tmp_trail, monkeypatch):
    import app.diagnostics as diag
    import app.single_instance as si

    seen = []
    monkeypatch.setattr(diag, "log", lambda message, level=None: seen.append(
        str(message)), raising=True)
    monkeypatch.setattr(si, "tempfile",
                        SimpleNamespace(gettempdir=lambda: str(tmp_trail)))
    si._si_log("claim attempt failed")
    assert any("claim attempt failed" in s for s in seen), seen


def test_the_elevation_log_redacts_a_token(tmp_trail):
    _real_elevation_log("relaunch argv=['--elevation-token=%s']" % _TOKEN)
    text = (tmp_trail / "cleaner_tool_elevation.log").read_text(encoding="utf-8")
    assert _TOKEN not in text
    assert "<redacted>" in text


def test_the_wait_loop_never_states_the_expected_token(tmp_trail, monkeypatch):
    import app.elevation as el

    calls = []
    monkeypatch.setattr(el, "_log_or_swallow_fallback",
                        lambda msg, exc=None: calls.append(msg))
    # A pending token file exists, so the "present" branch is taken.
    (tmp_trail / "CleanerTool_elevation_token").write_text(_TOKEN,
                                                           encoding="utf-8")
    # B-014: the wait's deadline is monotonic, so the fake clock answers to
    # both names (the loop must exit immediately with timeout=0 either way).
    monkeypatch.setattr(el, "time", SimpleNamespace(
        time=lambda: 0.0, monotonic=lambda: 0.0,
        sleep=lambda _s: (_ for _ in ()).throw(
            AssertionError("the wait should have exited by timeout"))))
    el._wait_for_elevated_process(timeout=0)

    joined = "\n".join(calls)
    assert _TOKEN not in joined
    assert "present" in joined.lower() or "token" in joined.lower()


def test_relaunch_as_admin_never_logs_the_token(tmp_trail, monkeypatch):
    import app.elevation as el

    calls = []
    monkeypatch.setattr(el, "_log_or_swallow_fallback",
                        lambda msg, exc=None: calls.append(str(msg)))
    monkeypatch.setattr(el, "_write_pending_token", lambda token: None)
    monkeypatch.setattr(el, "_clear_elevation_cookie", lambda: None)
    monkeypatch.setattr(el, "_clear_pending_token", lambda: None)
    monkeypatch.setattr(el.subprocess, "run",
                        lambda *a, **k: SimpleNamespace(
                            returncode=0, stdout="", stderr=""))

    assert el.relaunch_as_admin() is True
    joined = "\n".join(calls)
    assert not re.search(r"\b[0-9a-f]{32}\b", joined), joined
    assert "--elevation-token=<redacted>" in joined, (
        "the command shape stays legible, the value does not")


def test_the_single_instance_detection_redacts_the_argument(
        tmp_trail, capsys, monkeypatch):
    import app.single_instance as si

    monkeypatch.setattr(si, "tempfile",
                        SimpleNamespace(gettempdir=lambda: str(tmp_trail)))

    assert si.is_elevation_child([f"--elevation-token={_TOKEN}"]) is True
    assert _TOKEN not in capsys.readouterr().err
    log = (tmp_trail / "cleaner_tool_single_instance.log").read_text(
        encoding="utf-8")
    assert _TOKEN not in log
