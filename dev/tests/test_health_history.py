"""The PC Health score trend: what is stored, and what a stored point means.

A checkup that could not measure anything must not become a 0 (that would
read as "your PC got worse"), a malformed point must be dropped rather than
drawn as a dip, and the ledger must stay bounded. The trend maths is pinned
here; the tab's wiring is pinned in test_health_tab.py.
"""
from __future__ import annotations

import pytest

from app import health_history
from app.config_schema import (
    HEALTH_FINDINGS_MAX, HEALTH_HISTORY_MAX, _health_history,
)


@pytest.fixture
def ledger(monkeypatch):
    """An in-memory stand-in for config.json's health_history."""
    import app.config_persist as cp

    state = {"health_history": []}

    def _update(fn):
        cfg = {"health_history": list(state["health_history"])}
        fn(cfg)
        state["health_history"] = list(cfg["health_history"])

    def _load():
        return {"health_history": list(state["health_history"])}

    monkeypatch.setattr(cp, "update_config", _update)
    monkeypatch.setattr(cp, "load_config", _load)
    return state


# --------------------------------------------------------------------------- #
# writing
# --------------------------------------------------------------------------- #

def test_a_scored_checkup_is_appended_with_time_score_and_grade():
    cfg = {"health_history": []}
    assert health_history.add_to_config(cfg, 84, "b", when=1000.0) is True
    assert cfg["health_history"] == [{"t": 1000.0, "s": 84, "g": "B"}]


def test_a_checkup_with_no_score_is_not_stored_as_zero():
    cfg = {"health_history": []}
    assert health_history.add_to_config(cfg, None, "C", when=1000.0) is False
    assert cfg["health_history"] == []


def test_an_unknown_grade_or_out_of_range_score_is_not_a_point():
    cfg = {"health_history": []}
    assert health_history.add_to_config(cfg, 50, "?", when=1000.0) is False
    assert health_history.add_to_config(cfg, 101, "A", when=1000.0) is False
    assert health_history.add_to_config(cfg, -1, "A", when=1000.0) is False
    assert cfg["health_history"] == []


def test_malformed_entries_already_in_the_file_are_dropped():
    cfg = {"health_history": [
        {"t": 0, "s": 50, "g": "C"},          # no timestamp
        {"t": "x", "s": 50, "g": "C"},        # not a time
        {"t": 1000.0, "s": 50, "g": "Z"},     # not a grade
        {"t": 1001.0, "s": 90, "g": "A"},
    ]}
    health_history.add_to_config(cfg, 70, "C", when=1002.0)
    assert [p["s"] for p in cfg["health_history"]] == [90, 70]


def test_the_ledger_is_capped_keeping_the_newest_points():
    cfg = {"health_history": [{"t": float(i + 1), "s": 50, "g": "C"}
                              for i in range(HEALTH_HISTORY_MAX + 20)]}
    health_history.add_to_config(cfg, 99, "A", when=9999.0)
    stored = cfg["health_history"]
    assert len(stored) == HEALTH_HISTORY_MAX
    assert stored[-1] == {"t": 9999.0, "s": 99, "g": "A"}
    assert stored[0]["t"] == 22.0          # the 21 oldest fell off


def test_record_writes_through_the_config_mutation_once(ledger):
    point = health_history.record(72, "C", when=2000.0)
    assert point == {"t": 2000.0, "s": 72, "g": "C"}
    assert ledger["health_history"] == [point]


def test_record_returns_none_and_writes_nothing_for_an_unmeasurable_check(ledger):
    assert health_history.record(None, "?") is None
    assert ledger["health_history"] == []


def test_a_checkup_records_what_it_measured(ledger):
    point = health_history.record(
        80, "B", when=3000.0, findings={"smart": "B", "disk": "A"})
    assert point["f"] == {"smart": "B", "disk": "A"}
    assert ledger["health_history"][0]["f"] == {"smart": "B", "disk": "A"}


def test_a_malformed_findings_map_is_dropped_without_losing_the_score():
    cfg = {"health_history": []}
    health_history.add_to_config(
        cfg, 80, "B", when=1000.0,
        findings={"smart": "b", "disk": "?", "": "A", "boot": 5})
    assert cfg["health_history"] == [
        {"t": 1000.0, "s": 80, "g": "B", "f": {"smart": "B"}}]


def test_findings_are_bounded():
    raw = {"k%02d" % i: "C" for i in range(HEALTH_FINDINGS_MAX + 5)}
    cfg = {"health_history": []}
    health_history.add_to_config(cfg, 50, "C", when=1000.0, findings=raw)
    assert len(cfg["health_history"][0]["f"]) == HEALTH_FINDINGS_MAX


def test_findings_from_cards_keeps_only_measured_grades():
    cards = {"smart": {"grade": "C"}, "disk": {"grade": "?"},
             "boot": "A", "none": {}}
    assert health_history.findings_from_cards(cards) == {
        "smart": "C", "boot": "A"}


def test_the_automatic_badge_check_also_records_a_trend_point(monkeypatch):
    """A scheduled checkup is a checkup: it must feed the trend too."""
    from app import health_badge, health_model, health_scan

    cards = {"disk": {"grade": "C", "headline": "x", "detail": "y"},
             "smart": {"grade": "?", "headline": "x", "detail": "y"}}
    monkeypatch.setattr(health_scan, "run_health_scan", lambda _ctx: cards)
    calls = []
    monkeypatch.setattr(
        health_badge, "record",
        lambda grade, score=None, when=None, findings=None:
            calls.append((grade, score, findings)))

    grade = health_badge.quick_check()
    assert grade == health_scan.overall_grade(cards)
    assert calls == [(grade, health_model.summarize(cards)["score"],
                      {"disk": "C"})]
    # "?" is not a finding: that sensor measured nothing, and storing it
    # would let a later checkup read the silence as a change.


# --------------------------------------------------------------------------- #
# clearing
# --------------------------------------------------------------------------- #

def test_clear_empties_the_ledger_and_reports_success(ledger):
    ledger["health_history"] = [{"t": 1.0, "s": 80, "g": "B"},
                                {"t": 2.0, "s": 70, "g": "C"}]
    assert health_history.clear() is True
    assert ledger["health_history"] == []


def test_clear_reports_a_failed_write_instead_of_claiming_success(monkeypatch):
    import app.config_persist as cp

    def _read_only(_fn):
        raise OSError("config file locked")

    monkeypatch.setattr(cp, "update_config", _read_only)
    assert health_history.clear() is False


# --------------------------------------------------------------------------- #
# reading
# --------------------------------------------------------------------------- #

def test_entries_drops_a_malformed_row_from_whatever_the_loader_returned(
        ledger):
    ledger["health_history"] = [
        {"t": 2.0, "s": 88, "g": "B"},
        "not a point",
        {"t": 1.0, "s": 70, "g": "C"},
    ]
    assert [e["s"] for e in health_history.entries()] == [70, 88]


def test_an_empty_ledger_is_empty_not_a_zero(ledger):
    assert health_history.entries() == []


# --------------------------------------------------------------------------- #
# trend maths
# --------------------------------------------------------------------------- #

def _points(*scores, start=1000.0, step=86400.0):
    return [{"t": start + i * step, "s": s, "g": "C"}
            for i, s in enumerate(scores)]


def test_no_history_has_no_fabricated_score():
    trend = health_history.trend(points=[])
    assert trend["count"] == 0
    assert trend["last"] is None and trend["best"] is None
    assert trend["delta"] is None


def test_delta_is_against_the_previous_check_not_the_first_one():
    trend = health_history.trend(points=_points(60, 82, 88))
    assert trend["count"] == 3
    assert trend["last"] == 88
    assert trend["delta"] == 6          # 88 - 82, not 88 - 60
    assert trend["overall"] == 28
    assert trend["best"] == 88 and trend["worst"] == 60


def test_a_single_check_has_no_delta_yet():
    trend = health_history.trend(points=_points(74))
    assert trend["delta"] is None and trend["overall"] is None
    assert trend["scores"] == [74]


def test_a_negative_move_is_reported_as_negative():
    assert health_history.trend(points=_points(90, 81))["delta"] == -9


# --------------------------------------------------------------------------- #
# a sustained fall
# --------------------------------------------------------------------------- #

def test_a_run_of_falling_checks_is_a_decline_worth_naming():
    trend = health_history.trend(points=_points(50, 88, 84, 80, 79))
    assert trend["decline"] == {"streak": 3, "drop": 9,
                                "from": 88, "to": 79, "causes": []}


def test_two_drops_are_enough_when_the_fall_is_real():
    decline = health_history.trend(points=_points(80, 74, 70))["decline"]
    assert decline["streak"] == 2 and decline["drop"] == 10


def test_a_tiny_wobble_is_not_a_decline():
    # Two one-point dips are noise; calling them out would train the reader
    # to ignore the line on the day it matters.
    assert health_history.trend(points=_points(80, 79, 78))["decline"] is None


def test_one_drop_is_not_a_run():
    assert health_history.trend(points=_points(90, 70))["decline"] is None


def test_a_rise_in_the_middle_breaks_the_run():
    trend = health_history.trend(points=_points(90, 70, 75, 60))
    assert trend["decline"] is None


def test_a_flat_checkup_ends_the_run_too():
    assert health_history.trend(
        points=_points(90, 80, 80, 70))["decline"] is None


def test_the_decline_counts_only_the_checks_in_the_run():
    decline = health_history.trend(
        points=_points(50, 90, 84, 76))["decline"]
    assert decline["streak"] == 2
    assert decline["from"] == 90 and decline["to"] == 76


def test_no_history_has_no_decline():
    assert health_history.trend(points=[])["decline"] is None


def _pt(t, s, g, findings=None):
    point = {"t": float(t), "s": s, "g": g}
    if findings is not None:
        point["f"] = findings
    return point


def test_a_decline_names_the_finding_that_fell():
    pts = [_pt(1, 92, "A", {"smart": "A", "disk": "A"}),
           _pt(2, 86, "B", {"smart": "C", "disk": "A"}),
           _pt(3, 79, "C", {"smart": "D", "disk": "A"})]
    causes = health_history.trend(points=pts)["decline"]["causes"]
    assert causes == [{"key": "smart", "from": "A", "to": "D"}]


def test_a_finding_that_improved_is_not_a_cause():
    pts = [_pt(1, 90, "A", {"smart": "A"}),
           _pt(2, 80, "B", {"smart": "B"}),
           _pt(3, 70, "C", {"smart": "A"})]
    assert health_history.trend(points=pts)["decline"]["causes"] == []


def test_a_finding_missing_from_either_end_is_not_blamed():
    """An absent key is "not measured", not "broke" - so it names nothing."""
    pts = [_pt(1, 95, "A", {"disk": "A", "smart": "A"}),
           _pt(2, 85, "B", {"disk": "A", "smart": "A"}),
           _pt(3, 75, "C", {"smart": "A"})]
    assert health_history.trend(points=pts)["decline"]["causes"] == []


def test_points_from_before_findings_existed_still_decline_without_causes():
    trend = health_history.trend(points=_points(92, 86, 79))
    assert trend["decline"]["drop"] == 13
    assert trend["decline"]["causes"] == []


def test_the_biggest_fall_is_named_first():
    pts = [_pt(1, 92, "A", {"smart": "A", "disk": "B", "boot": "A"}),
           _pt(2, 86, "B", {"smart": "B", "disk": "D", "boot": "A"}),
           _pt(3, 80, "B", {"smart": "B", "disk": "F", "boot": "A"})]
    causes = health_history.trend(points=pts)["decline"]["causes"]
    assert [c["key"] for c in causes] == ["disk", "smart"]


def test_points_out_of_order_are_sorted_before_the_delta_is_computed():
    shuffled = [{"t": 3.0, "s": 90, "g": "A"},
                {"t": 1.0, "s": 70, "g": "C"},
                {"t": 2.0, "s": 80, "g": "B"}]
    trend = health_history.trend(points=shuffled)
    assert trend["scores"] == [70, 80, 90]
    assert trend["delta"] == 10


# --------------------------------------------------------------------------- #
# schema coercer
# --------------------------------------------------------------------------- #

def test_the_schema_coercer_keeps_only_well_formed_points():
    raw = [
        {"t": "100", "s": "85", "g": "b"},     # numeric strings still load
        {"t": 0, "s": 50, "g": "C"},
        {"t": 200, "s": 50, "g": "Z"},
        {"t": 300, "s": 50, "g": "C"},
        "junk",
    ]
    assert _health_history(raw, []) == [
        {"t": 100.0, "s": 85, "g": "B"},
        {"t": 300.0, "s": 50, "g": "C"},
    ]


def test_the_schema_coercer_rejects_a_non_list():
    assert _health_history({"t": 1}, []) == []
    assert _health_history(None, []) == []


def test_the_schema_coercer_sorts_and_caps():
    raw = [{"t": float(i), "s": 50, "g": "C"}
           for i in range(HEALTH_HISTORY_MAX + 5)]
    raw.reverse()
    out = _health_history(raw, [])
    assert len(out) == HEALTH_HISTORY_MAX
    assert out[-1]["t"] == float(HEALTH_HISTORY_MAX + 4)


def test_the_schema_and_defaults_agree_on_the_new_key():
    from app.config_persist import DEFAULT_CONFIG
    from app.config_schema import validate_schema

    assert "health_history" in DEFAULT_CONFIG
    assert validate_schema(DEFAULT_CONFIG) == []
