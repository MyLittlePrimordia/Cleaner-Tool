"""The Drivers card: grading, the probe's parsing, and the wiring.

The sensor was written and then left standing for a year: `grade_drivers` is
pure and its docstring said "so it is unit-tested", but nothing tested it, and
a `SLOW_SENSORS` tuple named it while `health_scan.SENSORS` - the list the scan
actually reads - did not. A device with no driver is hardware the PC cannot
use, and the fix has a screen of its own (the Drivers tab looks the package up
and installs it), so the card is registered now.

These tests pin the three halves that make it real: the grade the user reads,
the probe rows it is computed from, and the action that takes them to Drivers.
"""
from __future__ import annotations

import datetime

import app.health_scan as hs
import app.health_sensors as hsn
from app import health_model as hm
from app.ui.tabs.healthtab import HealthTab

_TODAY = datetime.date(2026, 10, 27)


def _grade(old=(), missing=(), today=_TODAY):
    return hsn.grade_drivers(list(old), list(missing), today=today)


# --------------------------------------------------------------------------- #
# Pure grading
# --------------------------------------------------------------------------- #

def test_a_clean_machine_grades_a_and_says_what_was_measured():
    grade, headline, detail, note = _grade()
    assert grade == "A"
    assert "current" in headline.lower()
    # "A" means nothing was found wrong - not that nothing was measured.
    assert "no missing drivers" in detail.lower()


def test_missing_drivers_outrank_old_ones():
    """A device Windows cannot drive is broken; an old driver usually works."""
    old = [("2020-01-01", "Old Wifi"), ("2019-01-01", "Old GPU"),
           ("2018-01-01", "Old Audio")]
    assert _grade(old=old)[0] == "C"
    grade, headline, detail, note = _grade(
        old=old, missing=["Fingerprint Sensor"])
    assert grade == "C"
    assert "1 device has no driver" in headline
    assert "Fingerprint Sensor" in detail
    assert "Drivers tab" in note


def test_three_or_more_devices_without_a_driver_grades_d():
    grade, headline, _detail, _note = _grade(
        missing=["A", "B", "C"])
    assert grade == "D"
    assert "3 devices have no driver" in headline


def test_several_old_drivers_grade_c_and_one_grades_b():
    """One 3-year-old driver is not worth a warning; a pattern is."""
    assert _grade(old=[("2020-01-01", "Old GPU"), ("2021-01-01", "Old Net"),
                       ("2022-01-01", "Old Audio")])[0] == "C"
    grade, headline, detail, _note = _grade(old=[("2020-01-01", "Old GPU")])
    assert grade == "B"
    assert "Old GPU" in detail and "2020" in detail


def test_the_cutoff_is_three_years_and_is_not_off_by_a_day():
    within = ("2023-10-27", "Just under three")
    older = ("2023-10-26", "Just over three")
    assert _grade(old=[within])[0] == "A"
    assert _grade(old=[older])[0] == "B"


def test_a_leap_day_today_does_not_break_the_cutoff():
    """`today.replace(year=... - 3)` raises on Feb 29 in an ordinary year."""
    assert _grade(today=datetime.date(2028, 2, 29))[0] == "A"


# --------------------------------------------------------------------------- #
# The probe's rows
# --------------------------------------------------------------------------- #

def test_the_sensor_reads_the_old_and_missing_rows_of_the_slow_probe(monkeypatch):
    monkeypatch.setattr(hsn, "_slow", lambda _ctx: {
        "OK": "1",
        "OLD": ["2020-01-01|Old GPU", "2021-05-05|Old Network"],
        "MISS": ["Fingerprint Sensor"],
    })
    grade, headline, _detail, _note = hsn.sense_drivers(None)
    assert grade == "C"
    assert "1 device has no driver" in headline


def test_a_malformed_probe_row_is_ignored_rather_than_graded(monkeypatch):
    """The row is `date|name`; a truncated one must not become a finding."""
    monkeypatch.setattr(hsn, "_slow", lambda _ctx: {
        "OK": "1", "OLD": ["not-a-date|Something", "2020-01-01"],
        "MISS": [],
    })
    assert hsn.sense_drivers(None)[0] == "A"


def test_an_unreadable_probe_is_an_honest_unknown(monkeypatch):
    monkeypatch.setattr(hsn, "_slow", lambda _ctx: {})
    grade, headline, _detail, _note = hsn.sense_drivers(None)
    assert grade == "?"
    assert "Couldn't check" in headline


# --------------------------------------------------------------------------- #
# Registration and the way to the fix
# --------------------------------------------------------------------------- #

def test_the_card_is_registered_as_a_slow_sensor():
    by_key = {spec[0]: spec for spec in hs.SENSORS}
    assert by_key["drivers"][1:] == ("Drivers", hsn.sense_drivers, True), (
        "the ~3s probe must not block the first paint")


def test_the_finding_routes_to_the_tab_that_owns_the_fix():
    """Every sensor needs an action or an explicit dead end; this one's own
    note tells the user where to go, so the button has to match it."""
    assert hm.CARD_ACTION["drivers"]["target"] == "drivers"
    assert hm.action_for("drivers", "C") is not None
    assert hm.action_for("drivers", "A") is None
    target, label = HealthTab._action_for(hm.CARD_ACTION["drivers"])
    assert (target, label) == ("drivers", "Open Drivers")
