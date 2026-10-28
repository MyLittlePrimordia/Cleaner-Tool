"""N-003: battery wear from powercfg's own report.

Two rules matter more than the thresholds: a desktop is "not applicable"
rather than a silent pass, and a capacity Windows could not report is unknown
rather than 0% wear.
"""
from __future__ import annotations

import pytest

from app import health_sensors as hs

_NS = "http://schemas.microsoft.com/win/2004/08/events/event"


def _report(batteries):
    rows = "".join(
        "<Battery><Id>%d</Id><DesignCapacity>%d</DesignCapacity>"
        "<FullChargeCapacity>%d</FullChargeCapacity>%s</Battery>"
        % (i, design, full,
           ("<CycleCount>%d</CycleCount>" % cycles)
           if cycles is not None else "")
        for i, (design, full, cycles) in enumerate(batteries, start=1)
    )
    return ('<BatteryReport xmlns="http://schemas.microsoft.com/'
            'windows/2004/08/batteryreport"><Batteries>%s</Batteries>'
            "</BatteryReport>" % rows)


@pytest.mark.parametrize("design,full,grade", [
    (60000, 60000, "A"),
    (60000, 50000, "B"),
    (60000, 45000, "C"),
    (60000, 38000, "D"),
    (60000, 20000, "F"),
])
def test_wear_maps_to_a_grade(design, full, grade):
    graded = hs.grade_battery(design, full)
    assert graded is not None
    assert graded[0] == grade


def test_wear_is_a_percentage_of_design_capacity():
    grade, wear, _verdict = hs.grade_battery(60000, 45000)
    assert round(wear) == 25
    assert grade == "C"


def test_a_report_with_no_battery_is_empty_not_zero():
    assert hs.parse_battery_report(_report([])) == []
    assert hs.parse_battery_report("<BatteryReport/>") == []
    assert hs.parse_battery_report("not xml") == []


def test_the_report_parses_even_though_the_namespace_changes():
    rows = hs.parse_battery_report(_report([(57000, 51000, 123)]))
    assert rows == [(57000, 51000, 123)]


def test_missing_cycle_count_is_none_not_zero():
    rows = hs.parse_battery_report(_report([(57000, 51000, None)]))
    assert rows[0][2] is None


def test_a_nonsense_capacity_is_no_measurement():
    assert hs.grade_battery(0, 50000) is None
    assert hs.grade_battery(50000, 0) is None
    assert hs.grade_battery("x", 1) is None


def test_a_desktop_gets_an_explicit_not_applicable_card(monkeypatch,
                                                        tmp_path):
    from app.utils import TaskSkipped

    monkeypatch.setattr("app.utils.run_capture",
                        lambda *_a, **_k: (0, "report written"))
    monkeypatch.setattr(hs, "parse_battery_report", lambda _text: [])
    import tempfile
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(tmp_path))
    (tmp_path / ("cleaner_tool_battery_%d.xml" % __import__("os").getpid())
     ).write_text(_report([]), encoding="utf-8")
    ctx = type("Ctx", (), {"log": lambda *a: None,
                           "set_status": lambda *a: None})()
    with pytest.raises(TaskSkipped) as info:
        hs.sense_battery(ctx)
    assert "desktop" in str(info.value)


def test_a_laptop_card_states_design_and_current_capacity(monkeypatch, tmp_path):
    path = tmp_path / "battery.xml"
    path.write_text(_report([(60000, 45000, 200)]), encoding="utf-8")
    monkeypatch.setattr("app.utils.run_capture",
                        lambda *_a, **_k: (0, "ok"))

    ctx = type("Ctx", (), {"log": lambda *a: None,
                           "set_status": lambda *a: None})()
    # The sensor writes to %TEMP%; point it at the fixture instead.
    import tempfile
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(tmp_path))
    (tmp_path / ("cleaner_tool_battery_%d.xml" % __import__("os").getpid())
     ).write_text(_report([(60000, 45000, 200)]), encoding="utf-8")
    grade, headline, detail, note = hs.sense_battery(ctx)
    assert grade == "C"
    assert "losing capacity" in headline
    assert "60 Wh" in detail and "45 Wh" in detail
    assert "200 charge cycles" in note
