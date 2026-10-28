"""B-005: the schedule drift check must read the XML schtasks really emits.

`schtasks /Query /TN ... /XML` returns a namespaced Task definition whose
trigger carries an ISO ``StartBoundary`` and one of ``ScheduleByDay`` /
``ScheduleByWeek`` / ``ScheduleByMonth``. The previous check looked for
``StartTime`` and ``Weekly``/``Daily``/``Monthly``, neither of which exists in
that document, so it could never confirm a match - and it never looked at the
day at all, so a task moved to another weekday read as "already correct".
"""
from __future__ import annotations

import datetime

import pytest

from app import scheduler as s

_WEEKDAY_NAMES = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday",
                  "Saturday", "Sunday"]

_HEAD = ('<?xml version="1.0" encoding="UTF-16"?>\n'
         '<Task version="1.2" '
         'xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">\n'
         '  <RegistrationInfo><Description>x</Description></RegistrationInfo>\n'
         '  <Triggers>\n    <CalendarTrigger>\n')
_TAIL = ('    </CalendarTrigger>\n  </Triggers>\n'
         '  <Settings><Enabled>true</Enabled>'
         '<DisallowStartIfOnBatteries>true</DisallowStartIfOnBatteries>'
         '</Settings>\n</Task>\n')


def _xml(body: str) -> str:
    return _HEAD + body + _TAIL


def _weekly_xml(time="03:00:00", day="Saturday", extra=""):
    return _xml(
        "      <StartBoundary>2026-10-24T%s</StartBoundary>\n"
        "      <Enabled>true</Enabled>\n"
        "      <ScheduleByWeek><WeeksInterval>1</WeeksInterval>\n"
        "        <DaysOfWeek><%s/>%s</DaysOfWeek>\n"
        "      </ScheduleByWeek>\n" % (time, day, extra))


def _daily_xml(time="03:00:00"):
    return _xml(
        "      <StartBoundary>2026-10-24T%s</StartBoundary>\n"
        "      <Enabled>true</Enabled>\n"
        "      <ScheduleByDay><DaysInterval>1</DaysInterval></ScheduleByDay>\n"
        % time)


def _monthly_xml(day=24, time="03:00:00"):
    return _xml(
        "      <StartBoundary>2026-10-24T%s</StartBoundary>\n"
        "      <Enabled>true</Enabled>\n"
        "      <ScheduleByMonth><DaysOfMonth><Day>%d</Day></DaysOfMonth>\n"
        "      </ScheduleByMonth>\n" % (time, day))


SATURDAY = datetime.date(2026, 10, 24)


def test_today_is_the_day_the_fixture_assumes():
    assert _WEEKDAY_NAMES[SATURDAY.weekday()] == "Saturday"


def test_a_matching_weekly_task_is_a_positive_match():
    assert s._xml_schedule_matches(_weekly_xml(), "weekly", "03:00",
                                   today=SATURDAY) is True


def test_a_changed_time_is_drift():
    assert s._xml_schedule_matches(_weekly_xml(time="09:00:00"), "weekly",
                                   "03:00", today=SATURDAY) is False


def test_a_changed_weekday_is_drift():
    """The old check ignored the day entirely."""
    assert s._xml_schedule_matches(_weekly_xml(day="Monday"), "weekly",
                                   "03:00", today=SATURDAY) is False


def test_an_extra_weekday_is_drift():
    assert s._xml_schedule_matches(
        _weekly_xml(extra="<Monday/>"), "weekly", "03:00",
        today=SATURDAY) is False


def test_a_different_frequency_is_drift():
    assert s._xml_schedule_matches(_daily_xml(), "weekly", "03:00",
                                   today=SATURDAY) is False
    assert s._xml_schedule_matches(_weekly_xml(), "daily", "03:00",
                                   today=SATURDAY) is False


def test_daily_ignores_the_day_but_not_the_time():
    assert s._xml_schedule_matches(_daily_xml(), "daily", "03:00",
                                   today=SATURDAY) is True
    assert s._xml_schedule_matches(_daily_xml("04:00:00"), "daily", "03:00",
                                   today=SATURDAY) is False


def test_monthly_compares_the_day_of_month():
    assert s._xml_schedule_matches(_monthly_xml(24), "monthly", "03:00",
                                   today=SATURDAY) is True
    assert s._xml_schedule_matches(_monthly_xml(1), "monthly", "03:00",
                                   today=SATURDAY) is False


def test_monthly_clamps_like_the_writer_does():
    assert s._schedule_day_args("MONTHLY", datetime.date(2026, 10, 31)) == \
        ["/D", "28"]
    assert s._xml_schedule_matches(_monthly_xml(28), "monthly", "03:00",
                                   today=datetime.date(2026, 10, 31)) is True


def test_a_zone_offset_and_fraction_still_read():
    xml = _weekly_xml(time="03:00:00.1234567-07:00")
    assert s._xml_schedule_matches(xml, "weekly", "03:00", today=SATURDAY) is True


def test_a_disabled_trigger_is_drift():
    xml = _weekly_xml().replace("<Enabled>true</Enabled>",
                                "<Enabled>false</Enabled>", 1)
    assert s._xml_schedule_matches(xml, "weekly", "03:00", today=SATURDAY) is False


def test_settings_enabled_does_not_masquerade_as_the_trigger():
    """The trigger is enabled; only <Settings><Enabled> is untouched."""
    xml = _xml(
        "      <StartBoundary>2026-10-24T03:00:00</StartBoundary>\n"
        "      <Enabled>true</Enabled>\n"
        "      <ScheduleByDay><DaysInterval>1</DaysInterval></ScheduleByDay>\n")
    assert s._xml_schedule_matches(xml, "daily", "03:00", today=SATURDAY) is True


@pytest.mark.parametrize("xml", [
    "",
    "not xml at all",
    "<Task/>",
    # right frequency, no StartBoundary at all
    _xml("      <Enabled>true</Enabled>\n"
         "      <ScheduleByWeek><WeeksInterval>1</WeeksInterval>"
         "<DaysOfWeek><Saturday/></DaysOfWeek></ScheduleByWeek>\n"),
    _xml("      <StartBoundary>2026-10-24T03:00:00</StartBoundary>\n"
         "      <ScheduleByWeek><WeeksInterval>1</WeeksInterval>"
         "</ScheduleByWeek>\n"),
])
def test_unreadable_is_none_not_a_match(xml):
    """None must never be read as "already correct" by the caller."""
    assert s._xml_schedule_matches(xml, "weekly", "03:00", today=SATURDAY) is None


def test_the_legacy_start_time_form_is_still_understood():
    xml = _xml("      <StartTime>03:00:00</StartTime>\n"
               "      <Weekly><DaysOfWeek><Saturday/></DaysOfWeek></Weekly>\n")
    assert s._xml_schedule_matches(xml, "weekly", "03:00", today=SATURDAY) is True
