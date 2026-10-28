"""The two sensors that were written, never registered, and never run.

`sense_maintenance` (TRIM / Storage Sense / Fast Startup) and `sense_time` (the
Windows Time service + uptime) sat in `app/health_sensors.py` reading keys the
batched probe already prints, with no `SENSORS` entry - so nothing exercised
them, and running them for the first time found two ways they would have lied:

* `sense_maintenance` graded A when Windows reported nothing at all (a missing
  Storage Sense key, a failed `fsutil`): the same all-clear the 0-vs-None
  contract exists to prevent, one level up;
* `sense_time` graded C for a STOPPED Windows Time service - which is that
  service's normal resting state, since Windows ships it as a manual
  (trigger-start) service. On a stock install the row would have accused a
  machine that is working exactly as designed.

These tests pin the four-state reading (on / off / not supported / not
reported) and the state+start-mode truth table, plus the registration that
makes them run at all.
"""
from __future__ import annotations

from app import health_model as hm
from app import health_sensors as hs
from app.ui.tabs.healthtab import HealthTab


def _ctx():
    from app.utils import TaskContext
    return TaskContext(log=lambda _m: None, set_status=lambda _m: None)


def _fast(monkeypatch, data):
    """Plant a batched-probe reading.

    The probe's dict maps a key to the LIST of values it printed under that
    key (`_one()` takes the first), so a plain scalar here would silently
    read as its first character - which is exactly the bug this caught while
    it was being written.
    """
    planted = {key: (value if isinstance(value, list) else [value])
               for key, value in data.items()}
    monkeypatch.setattr(hs, "_fast", lambda ctx: planted)


# --------------------------------------------------------------------------- #
# Drive maintenance: TRIM, Storage Sense, Fast Startup
# --------------------------------------------------------------------------- #

def test_all_three_on_is_a_pass(monkeypatch):
    _fast(monkeypatch, {"TRIM": "on", "SENSE": "on", "FASTBOOT": "on"})
    grade, headline, detail, note = hs.sense_maintenance(_ctx())
    assert grade == "A"
    assert "set up properly" in headline
    assert "TRIM is on" in detail and "Storage Sense is on" in detail
    assert "Fast Startup is on" in detail
    assert note == ""


def test_a_setting_that_is_off_is_named_as_the_problem(monkeypatch):
    _fast(monkeypatch, {"TRIM": "off", "SENSE": "on", "FASTBOOT": "on"})
    grade, headline, detail, _note = hs.sense_maintenance(_ctx())
    assert grade == "C"
    assert headline == "1 setting could be improved"
    assert "TRIM is off" in detail and "slow down over time" in detail
    # The settings that ARE fine are not re-litigated as problems.
    assert "Storage Sense is off" not in detail


def test_unread_settings_can_never_add_up_to_a_pass(monkeypatch):
    """The bug this sensor had: no reading at all graded A. A missing Storage
    Sense key is a fact about the machine, not evidence that it is set up."""
    _fast(monkeypatch, {"TRIM": "on", "FASTBOOT": "on"})   # SENSE absent
    grade, headline, detail, _note = hs.sense_maintenance(_ctx())
    assert grade == "?"
    assert "could not be read" in headline
    assert "Storage Sense" in detail
    assert "TRIM is on" in detail, "what WAS read is still worth saying"


def test_nothing_readable_is_the_blunt_version_not_a_pass(monkeypatch):
    _fast(monkeypatch, {})
    grade, headline, detail, _note = hs.sense_maintenance(_ctx())
    assert grade == "?"
    assert headline == "Couldn't read maintenance settings"
    assert "did not report" in detail


def test_a_real_fault_still_grades_c_when_another_setting_is_unreadable(
        monkeypatch):
    _fast(monkeypatch, {"TRIM": "off"})                    # + nothing else
    grade, headline, detail, _note = hs.sense_maintenance(_ctx())
    assert grade == "C"
    assert "TRIM is off" in detail
    assert "did not report" in detail, (
        "the row must not hide the reading it could not take")


def test_not_supported_is_neither_a_fault_nor_a_claim(monkeypatch):
    """`DisableDeleteNotify = 2` means this volume's stack has no TRIM (an HDD,
    a VM). There is nothing to fix and nothing to report."""
    _fast(monkeypatch, {"TRIM": "na", "SENSE": "on", "FASTBOOT": "on"})
    grade, headline, detail, _note = hs.sense_maintenance(_ctx())
    assert grade == "A"
    assert "TRIM" not in detail
    assert "set up properly" in headline


def test_no_batched_data_at_all_is_unknown(monkeypatch):
    _fast(monkeypatch, {})
    assert hs.sense_maintenance(_ctx())[0] == "?"


# --------------------------------------------------------------------------- #
# The clock: state AND start mode
# --------------------------------------------------------------------------- #

def test_a_running_time_service_is_a_pass(monkeypatch):
    _fast(monkeypatch, {"W32STATUS": "Running", "W32STARTMODE": "Auto",
                        "UPTIME": "5"})
    grade, headline, detail, _note = hs.sense_time(_ctx())
    assert grade == "A"
    assert "kept in sync" in headline
    assert "Up 5 hours" in detail


def test_a_stopped_manual_service_is_how_windows_ships(monkeypatch):
    """The false alarm: W32Time is manual/trigger-start, so "Stopped" is its
    normal state. Grading it C would accuse almost every stock install."""
    _fast(monkeypatch, {"W32STATUS": "Stopped", "W32STARTMODE": "Manual",
                        "UPTIME": "73"})
    grade, headline, detail, _note = hs.sense_time(_ctx())
    assert grade == "A"
    assert "on demand" in headline
    assert "default" in detail
    assert "Up 3 days" in detail, "24h multiples read as days"


def test_a_stopped_automatic_service_is_a_fault(monkeypatch):
    _fast(monkeypatch, {"W32STATUS": "Stopped", "W32STARTMODE": "Auto",
                        "UPTIME": "4"})
    grade, headline, detail, note = hs.sense_time(_ctx())
    assert grade == "C"
    assert "not being corrected" in headline
    assert "set to start automatically" in detail
    assert "certificate" in note


def test_a_disabled_service_is_a_fault_even_while_it_runs(monkeypatch):
    """Running now, dead after the next reboot: the row has to say so."""
    _fast(monkeypatch, {"W32STATUS": "Running", "W32STARTMODE": "Disabled",
                        "UPTIME": "9"})
    grade, headline, detail, _note = hs.sense_time(_ctx())
    assert grade == "C"
    assert "disabled" in detail.lower()


def test_a_stopped_service_with_no_start_mode_is_unknown_not_a_fault(
        monkeypatch):
    _fast(monkeypatch, {"W32STATUS": "Stopped", "UPTIME": "9"})
    grade, _headline, detail, _note = hs.sense_time(_ctx())
    assert grade == "?"
    assert "how the Windows Time service starts" in detail


def test_an_unknown_state_is_unknown(monkeypatch):
    _fast(monkeypatch, {"UPTIME": "30"})
    grade, _headline, _detail, _note = hs.sense_time(_ctx())
    assert grade == "?"


def test_uptime_wording_scales(monkeypatch):
    _fast(monkeypatch, {"W32STATUS": "Running", "W32STARTMODE": "Auto",
                        "UPTIME": str(24 * 31)})
    _grade, _headline, detail, _note = hs.sense_time(_ctx())
    assert "running for 31 days without a restart" in detail


def test_no_batched_data_is_unknown_not_a_fault(monkeypatch):
    _fast(monkeypatch, {})
    grade, headline, detail, _note = hs.sense_time(_ctx())
    assert grade == "?"
    assert "Couldn't check the clock" in headline
    assert detail


# --------------------------------------------------------------------------- #
# Registration: the half that was missing
# --------------------------------------------------------------------------- #

def test_both_sensors_are_registered_and_come_from_the_batched_fast_probe():
    from app.health_scan import SENSORS

    by_key = {spec[0]: spec for spec in SENSORS}
    assert by_key["maintenance"][1:] == ("Drive Maintenance",
                                         hs.sense_maintenance, False)
    assert by_key["time"][1:] == ("Clock & Uptime", hs.sense_time, False)


def test_each_new_sensor_has_a_way_to_the_fix():
    """Every sensor needs either an action or an explicit dead end (checked
    two-way in test_health_model); for these two, the screen that owns the fix
    is the Repair tab, by the same care-target mapping Health already had."""
    assert hm.CARD_ACTION["maintenance"]["target"] == "maintenance"
    assert hm.CARD_ACTION["time"]["target"] == "time"
    assert hm.action_for("maintenance", "C") is not None
    assert hm.action_for("time", "C") is not None
    for key in ("maintenance", "time"):
        target, label = HealthTab._action_for(hm.CARD_ACTION[key])
        assert target == "repair", key
        assert label == "Open Repair", (key, label)


def test_a_passing_or_unknown_card_offers_no_action(monkeypatch):
    assert hm.action_for("maintenance", "A") is None
    assert hm.action_for("maintenance", "?") is None
    assert hm.action_for("time", "A") is None
