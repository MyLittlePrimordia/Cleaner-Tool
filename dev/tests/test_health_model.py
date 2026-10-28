"""The Health tab's scoring model, without Tk or a real machine.

These tests pin the three things the old dialog got wrong: a mean that let
healthy cards outvote one serious finding, a priority list that was not
actually ordered, and an action table that could silently miss a sensor.
"""
from __future__ import annotations

from app import health_model as hm


def _card(grade, headline="x"):
    return {"grade": grade, "headline": headline, "detail": ""}


def _cards(**grades):
    return {k: _card(v) for k, v in grades.items()}


def test_all_a_scores_a():
    cards = _cards(disk="A", smart="A", crashes="A", windows="A")
    summary = hm.summarize(cards)
    assert summary["grade"] == "A"
    assert summary["score"] == 100
    assert summary["fixes"] == 0
    assert "healthy" in summary["headline"].lower()


def test_one_failing_drive_cannot_be_outvoted_by_green_cards():
    cards = _cards(**{k: "A" for k in hm.WEIGHTS})
    cards["smart"] = _card("F")
    summary = hm.summarize(cards)
    assert summary["grade"] == "C", summary
    assert summary["score"] <= 74
    assert summary["critical"] == 1


def test_one_b_caps_the_overall_at_b():
    cards = _cards(**{k: "A" for k in hm.WEIGHTS})
    cards["junk"] = _card("B")
    summary = hm.summarize(cards)
    assert summary["grade"] == "B"
    assert summary["score"] <= 89


def test_a_mostly_unmeasured_report_cannot_claim_an_a():
    # Four A checks out of eleven sensors is not enough evidence for "healthy".
    cards = _cards(disk="A", memory="A", junk="A", security="A")
    for k in ("smart", "crashes", "windows", "gpu", "drivers", "startup",
              "maintenance"):
        cards[k] = _card("?")
    summary = hm.summarize(cards)
    assert summary["grade"] == "C"
    assert summary["confidence"] < 0.6
    assert summary["score"] <= 74


def test_no_measurements_has_no_score_and_does_not_reassure():
    cards = _cards(disk="?", smart="?")
    summary = hm.summarize(cards)
    assert summary["score"] is None
    assert summary["grade"] == "?"
    assert "couldn't check" in summary["headline"].lower()


def test_priorities_are_worst_first_then_heaviest():
    cards = _cards(disk="C", smart="F", crashes="D", junk="C", gpu="B")
    order = [p["key"] for p in hm.priority_cards(cards)]
    assert order[0] == "smart"          # F beats everything
    assert order[1] == "crashes"        # then the only D
    assert order[-1] == "gpu"           # B last
    # Two C findings are ordered by consequence, not by dict insertion.
    assert order.index("disk") < order.index("junk")


def test_unknown_cards_are_listed_but_never_treated_as_failures():
    cards = _cards(disk="A", smart="?")
    summary = hm.summarize(cards)
    assert summary["fixes"] == 0
    assert summary["unknown"] == ["smart"]
    assert summary["known"] == 1


def test_every_sensor_has_an_explicit_action_or_is_unknown_only():
    from app.health_scan import SENSORS
    keys = {s[0] for s in SENSORS}
    assert keys == set(hm.CARD_ACTION), (
        "a sensor without an action silently becomes a dead row: %r"
        % sorted(keys - set(hm.CARD_ACTION)))


def test_action_for_never_offers_a_fix_for_a_passing_or_unknown_card():
    assert hm.action_for("disk", "A") is None
    assert hm.action_for("disk", "?") is None
    assert hm.action_for("not_a_sensor", "F") is None
    act = hm.action_for("smart", "F")
    assert act and act["kind"] == "route" and act["target"] == "drivetool"
