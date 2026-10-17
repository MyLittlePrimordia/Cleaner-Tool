"""U-001: the diagnostics trail for intentional swallows.

The helper (app.utils.log_or_swallow) and the append-only events log already
existed; what was missing is that nothing ever read the trail back and the
most consequential silent sites never wrote to it. These tests pin both: the
reader, the writer, and two real instrumented sites.
"""
from __future__ import annotations

import pytest

from app import config_events


@pytest.fixture
def events_log(monkeypatch, tmp_path):
    path = tmp_path / "events.log"
    monkeypatch.setattr(config_events, "EVENTS_LOG_FILE", path)
    return path


def test_read_events_missing_file_is_an_empty_trail(monkeypatch, tmp_path):
    monkeypatch.setattr(config_events, "EVENTS_LOG_FILE", tmp_path / "nope.log")
    assert config_events.read_events() == []


def test_read_events_returns_the_tail(events_log):
    events_log.write_text(
        "\n".join("line %d" % i for i in range(10)) + "\n", encoding="utf-8")
    assert config_events.read_events(3) == ["line 7", "line 8", "line 9"]
    assert len(config_events.read_events()) == 10


def test_log_or_swallow_writes_one_line_and_never_raises(events_log):
    from app.utils import log_or_swallow
    log_or_swallow(None, "unit.test", ValueError("boom"))
    text = events_log.read_text(encoding="utf-8")
    assert "[ops]" in text
    assert "unit.test" in text
    assert "ValueError: boom" in text
    # an exception-free swallow still records the site name
    log_or_swallow(None, "unit.test.plain")
    assert "unit.test.plain" in events_log.read_text(encoding="utf-8")


def test_instrumented_scheduler_probe_records_a_swallow(events_log, monkeypatch):
    from app import scheduler

    def boom():
        raise RuntimeError("no schtasks here")

    monkeypatch.setattr(scheduler, "get_startup_status", boom)
    # never touch the real config from a test
    monkeypatch.setattr(scheduler, "_set_startup_flag", lambda on: None)

    assert scheduler.resync_startup_flag() is False
    text = events_log.read_text(encoding="utf-8")
    assert "scheduler.resync_startup_probe" in text
    assert "no schtasks here" in text


def test_instrumented_update_hash_failure_records(events_log, monkeypatch):
    from app import update_checker
    monkeypatch.setattr(update_checker, "get_current_exe_path",
                        lambda: r"C:\definitely\not\here\ct.exe")
    assert update_checker.get_current_exe_hash() == ""
    assert "update.exe_hash" in events_log.read_text(encoding="utf-8")
