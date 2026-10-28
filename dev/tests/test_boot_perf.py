"""U-002 / N-004: boot timings from Windows' own event channel.

The value of this module is that it never estimates: an unreadable channel is
"not measured", and a reordered event payload cannot turn a duration into a
version string because fields are matched by NAME.
"""
from __future__ import annotations

import pytest

from app import boot_perf

_NS = "http://schemas.microsoft.com/win/2004/08/events/event"


def _event(event_id, when, **data):
    fields = "".join('<Data Name="%s">%s</Data>' % (k, v)
                     for k, v in data.items())
    return (
        '<Event xmlns="%s"><System><EventID>%d</EventID>'
        '<TimeCreated SystemTime="%s"/></System>'
        "<EventData>%s</EventData></Event>" % (_NS, event_id, when, fields)
    )


_BOOT = _event(100, "2026-10-25T03:00:00.0000000Z", BootTime=24320,
               MainPathBootTime=18000, BootPostBootTime=6320)
_BOOT_OLD = _event(100, "2026-10-24T03:00:00.0000000Z", BootTime=31000)
_SLOW_APP = _event(102, "2026-10-25T03:00:00.0000000Z", Name="Steam",
                   FileName=r"C:\Program Files (x86)\Steam\steam.exe",
                   TotalTime=4210)
_SLOW_SVC = _event(103, "2026-10-25T03:00:00.0000000Z",
                   Name="Windows Search", TotalTime=9000)


def test_events_are_parsed_by_field_name_not_position():
    rows = boot_perf.parse_events(_BOOT)
    assert rows[0]["id"] == 100
    assert rows[0]["data"]["BootTime"] == "24320"
    assert rows[0]["t"] is not None


def test_a_shuffled_payload_still_reads_the_duration():
    shuffled = _event(100, "2026-10-25T03:00:00.0000000Z",
                      MainPathBootTime=18000, BootTime=24320)
    boot = boot_perf.parse_boots(boot_perf.parse_events(shuffled))[0]
    assert boot["boot_ms"] == 24320
    assert boot["main_ms"] == 18000


def test_boots_are_newest_first():
    events = boot_perf.parse_events(_BOOT_OLD + _BOOT)
    boots = boot_perf.parse_boots(events)
    assert [b["boot_ms"] for b in boots] == [24320, 31000]


def test_offenders_keep_the_slowest_measurement_per_stem():
    later = _event(102, "2026-10-26T03:00:00.0000000Z", Name="Steam",
                   FileName=r"C:\Steam\steam.exe", TotalTime=8000)
    events = boot_perf.parse_events(_SLOW_APP + later)
    out = boot_perf.parse_offenders(events)
    assert out["steam"]["ms"] == 8000


def test_a_service_offender_has_no_file_to_match_but_is_still_recorded():
    out = boot_perf.parse_offenders(boot_perf.parse_events(_SLOW_SVC))
    assert out["windows search"]["ms"] == 9000


def test_impact_matches_a_run_value_by_its_exe_stem():
    impact = boot_perf.parse_offenders(boot_perf.parse_events(_SLOW_APP))
    item = {"name": "Steam Client Bootstrapper",
            "command": r'"C:\Program Files (x86)\Steam\steam.exe" -silent'}
    assert boot_perf.impact_for(item, impact)["ms"] == 4210


def test_an_unmeasured_entry_gets_none_not_a_zero():
    assert boot_perf.impact_for({"name": "Foo", "command": r"C:\Foo\foo.exe"},
                                {}) is None
    assert boot_perf.impact_for({"name": "Foo"}, {"steam": {"ms": 1}}) is None


@pytest.mark.parametrize("value,text", [
    (None, "not measured"),
    (420, "420 ms"),
    (24320, "24.3 s"),
    (1000, "1.0 s"),
])
def test_durations_are_stated_in_seconds(value, text):
    assert boot_perf.format_ms(value) == text


def test_an_unreadable_channel_is_not_an_empty_machine(monkeypatch):
    monkeypatch.setattr(boot_perf, "_query_xml",
                        lambda *_a, **_k: ("", "Access is denied."))
    out = boot_perf.summary()
    assert out["ok"] is False
    assert "Access is denied" in out["error"]
    assert out["last_ms"] is None


def test_summary_reports_last_and_average(monkeypatch):
    monkeypatch.setattr(
        boot_perf, "_query_xml",
        lambda *_a, **_k: (_BOOT_OLD + _BOOT + _SLOW_APP, ""))
    out = boot_perf.summary()
    assert out["ok"] is True
    assert out["last_ms"] == 24320
    assert out["average_ms"] == int(round((24320 + 31000) / 2.0))
    assert out["impact"]["steam"]["ms"] == 4210
