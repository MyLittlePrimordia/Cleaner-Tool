"""The PC Health scoring and priority model, with no UI and no I/O.

`app.health_scan` answers "what is this machine's state?" one sensor at a
time. This module answers the two questions the Health tab has to put above
those cards:

    how bad is it, overall?        a weighted score, capped by the worst
                                   measured finding
    what should I do first?        the findings ordered by severity and by
                                   how much they matter to a working PC

The old report used a plain mean of the grade letters. That is why a machine
with one failing-SMART drive and a dozen A checks still read "in good shape":
the healthy cards outvoted the only one that mattered. The mean is still
computed here, then clamped:

    any C/D/F            -> the overall can be no better than C
    any B                -> no better than B
    fewer than 60% known -> no better than C (we did not see enough to say A)

A is therefore only possible when every measured check is A. An unknown card
is NOT a fault and is never counted as one, but a report that mostly could
not be measured is not allowed to reassure either - the honesty rule the
sensors already follow, carried up to the headline.

Weights encode consequence, not drama: a failing drive or a crashing program
hurts more than background junk or an old GPU driver. They affect the score
and the order of the priority list; they never change a card's own grade.
"""
from __future__ import annotations

#: Grade -> points on the 0-100 scale. These are the same boundaries the
#: letter bands below use, so a score and its grade cannot disagree.
GRADE_POINTS = {"A": 100, "B": 84, "C": 64, "D": 38, "F": 12}

#: Score -> letter. One table, used by both the ring and the threshold caps.
GRADE_BANDS = ((90, "A"), (75, "B"), (60, "C"), (40, "D"), (0, "F"))

#: How much each measured card matters to the overall number. A drive that is
#: failing or a program that keeps crashing changes what a PC can do; an old
#: GPU driver or a full cache does not, at least not in the same way. Keys not
#: listed use DEFAULT_WEIGHT, so adding a sensor cannot make this table stale.
WEIGHTS = {
    "smart": 18,
    "crashes": 16,
    "windows": 13,
    "disk": 12,
    "security": 11,
    "memory": 7,
    "startup": 5,
    "junk": 3,
    "battery": 8,
    # TRIM/Storage Sense/Fast Startup decide whether the space a clean frees
    # comes back; a clock that cannot sync breaks installs outright.
    "maintenance": 6,
    "time": 4,
    # A device with no driver is hardware the PC cannot use, which is worse
    # than a startup entry and better than system files or a crashing program.
    # The same weight also covers "several drivers are over 3 years old", which
    # is the milder half of the card; the grade is what tells the two apart.
    "drivers": 10,
}
DEFAULT_WEIGHT = 4

#: Worst-first order for the priority list. A is deliberately absent: a
#: passing check is never an action item.
_SEVERITY = {"F": 0, "D": 1, "C": 2, "B": 3}

#: The one action each finding gets. A "care" target names a tab that owns
#: the same fix (the Health tab maps it to that screen); the rest open the
#: screen where the user makes the decision. Keeping this table here (rather
#: than in the widget) means a test can assert that every sensor has either an
#: action or an explicit "this needs you".
CARD_ACTION = {
    "disk": {"kind": "care", "target": "clean", "label": "Open Clean"},
    "junk": {"kind": "care", "target": "clean", "label": "Open Clean"},
    "windows": {"kind": "care", "target": "repair", "label": "Open Repair"},
    "security": {"kind": "care", "target": "windows_updates",
                 "label": "Open Updates"},
    "crashes": {"kind": "route", "target": "reliability",
                "label": "See crashes"},
    "startup": {"kind": "route", "target": "startup",
                "label": "Open Startup"},
    "memory": {"kind": "route", "target": "procmgr",
               "label": "Open Processes"},
    "smart": {"kind": "route", "target": "drivetool",
              "label": "Open Drive Toolkit"},
    # N-003: battery wear is hardware, so there is no in-app fix to offer.
    # The honest action is Windows' own full report, which has the per-cycle
    # history this card summarises.
    "battery": {"kind": "route", "target": "battery_report",
                "label": "See battery report"},
    # Repair owns both fixes: `ssd_maintenance` retrims and checks drive
    # health, `time_sync` corrects a drifted clock.
    "maintenance": {"kind": "care", "target": "maintenance",
                    "label": "Open Repair"},
    "time": {"kind": "care", "target": "time",
             "label": "Open Repair"},
    # The Drivers tab owns the fix: it looks the package up and installs it
    # (`app/drivers/`), which is why the card routes there rather than to a
    # Windows screen. The sensor's own note says the same thing.
    "drivers": {"kind": "route", "target": "drivers",
                "label": "Open Drivers"},
}


def grade_of(card) -> str:
    """The letter from a card dict, tolerating a bare letter or bad input."""
    if isinstance(card, str):
        return card.upper()
    try:
        return str((card or {}).get("grade", "?")).upper()
    except Exception:
        return "?"


def known_cards(cards) -> int:
    return sum(1 for c in (cards or {}).values()
               if grade_of(c) in GRADE_POINTS)


def grade_for_score(score) -> str:
    """Score -> letter, or '?' when there is no score at all."""
    try:
        s = float(score)
    except (TypeError, ValueError):
        return "?"
    for floor, letter in GRADE_BANDS:
        if s >= floor:
            return letter
    return "F"


def score_cards(cards) -> int:
    """Weighted score 0-100, clamped by the worst measured finding.

    None when nothing could be measured. Unknown cards are excluded from the
    mean; the confidence cap above is what stops a mostly-unmeasured report
    from turning into an A.
    """
    entries = []
    for key, card in (cards or {}).items():
        g = grade_of(card)
        if g in GRADE_POINTS:
            entries.append((GRADE_POINTS[g], WEIGHTS.get(key, DEFAULT_WEIGHT),
                            g))
    if not entries:
        return None
    total_w = float(sum(w for _p, w, _g in entries)) or 1.0
    score = sum(p * w for p, w, _g in entries) / total_w
    grades = [g for _p, _w, g in entries]
    if any(g in ("C", "D", "F") for g in grades):
        score = min(score, 74.0)      # C ceiling
    elif any(g == "B" for g in grades):
        score = min(score, 89.0)      # B ceiling
    total = len(cards or {})
    if total and (len(entries) / float(total)) < 0.60:
        score = min(score, 74.0)      # not enough evidence for A/B
    return int(round(max(0.0, min(100.0, score))))


def verdict(grade, fixes: int = 0, known: int = 0, total: int = 0):
    """(headline, detail) in plain language. Pure and unit-tested."""
    g = str(grade or "?").upper()
    if g not in GRADE_POINTS:
        return ("We couldn't check this PC",
                "No check finished, so there is no score to show. Try again, "
                "or restart as Administrator for the full report.")
    if g == "A":
        return ("Your PC looks healthy",
                "All %d measured checks passed." % known if known
                else "Nothing measured needed attention.")
    if g == "B":
        return ("Your PC is in good shape",
                "One small thing is worth a look - nothing is broken.")
    if g == "C":
        return ("Your PC needs a little attention",
                "%d thing%s to look at, worst first."
                % (fixes, "" if fixes == 1 else "s"))
    if g == "D":
        return ("Your PC needs some work",
                "%d thing%s need attention. Start with the red one%s above."
                % (fixes, "" if fixes == 1 else "s",
                   "" if fixes == 1 else "s"))
    return ("Your PC needs attention now",
            "%d thing%s need fixing. Start at the top of the list."
            % (fixes, "" if fixes == 1 else "s"))


def priority_cards(cards):
    """Actionable findings, worst and heaviest first.

    Each entry is {key, card, grade, weight}. The order is stable for equal
    severity/weight: the sensor's own dict order, which matches health_scan's
    fixed SENSORS order, so a refresh never makes the list jump.
    """
    out = []
    for key, card in (cards or {}).items():
        g = grade_of(card)
        if g not in _SEVERITY:
            continue
        out.append({"key": key, "card": card, "grade": g,
                    "weight": WEIGHTS.get(key, DEFAULT_WEIGHT)})
    out.sort(key=lambda e: (_SEVERITY[e["grade"]], -e["weight"]))
    return out


def unknown_cards(cards):
    """Keys whose sensor could not be measured (honest '?' rows)."""
    return [k for k, c in (cards or {}).items() if grade_of(c) not in GRADE_POINTS]


def action_for(key, grade=None):
    """The action for a finding, or None. Passing a grade of A/'?' gives None."""
    g = str(grade or "").upper()
    if g and g not in ("B", "C", "D", "F"):
        return None
    return dict(CARD_ACTION.get(str(key), {})) or None


def summarize(cards) -> dict:
    """Everything the Health tab's header needs, in one pure call."""
    cards = cards or {}
    known = known_cards(cards)
    total = len(cards)
    score = score_cards(cards)
    grade = grade_for_score(score) if score is not None else "?"
    priorities = priority_cards(cards)
    headline, detail = verdict(grade, len(priorities), known, total)
    return {
        "score": score,
        "grade": grade,
        "known": known,
        "total": total,
        "confidence": (known / float(total)) if total else 0.0,
        "fixes": len(priorities),
        "critical": sum(1 for p in priorities if p["grade"] in ("D", "F")),
        "headline": headline,
        "detail": detail,
        "priorities": priorities,
        "unknown": unknown_cards(cards),
    }
