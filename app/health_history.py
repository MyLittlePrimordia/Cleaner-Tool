"""The PC Health score trend: one point per completed checkup.

`app.health_badge` remembers only the LAST grade, which answers "is my PC OK
right now?". This module answers the other question the Health tab has to be
able to answer: "is it getting better or worse?". It appends a compact
``{t, s, g, f?}`` point (time, score, grade, measured findings) to the config
key ``health_history`` every time a checkup produces a real score.

The findings map is what lets a falling trend name a CAUSE ("Drive health
dropped to C") instead of only the drop. It is optional because points
written before it existed are still valid, and because a scan can measure a
score without exposing per-sensor grades.

Rules, matching the honesty rule the sensors already follow:

  * A checkup that measured NOTHING is not a point. It is not a 0 either -
    a failed scan must never read as "your PC got worse".
  * A malformed stored entry is dropped, never repaired into a number it did
    not have (same contract as the run ledger in `app.config_history`).
  * A malformed FINDINGS map loses only the map: the score it was attached
    to was measured, so the point stays.
  * A cause is only named when BOTH ends of the fall measured the finding.
    An absent key is not a regression - it may be a sensor that refused -
    so the trend never reads silence as a drop.
  * The ledger is bounded by `config_schema.HEALTH_HISTORY_MAX`, so a
    hand-edited or long-lived config cannot grow without limit.

Tk-free: the tab decides how to draw a trend, this decides what is true.
"""
from __future__ import annotations

import time

from app.config_schema import (
    HEALTH_FINDINGS_MAX, HEALTH_FINDING_KEY_MAX, HEALTH_HISTORY_MAX,
)

#: Grades a stored point may carry. Kept in step with health_model by using
#: that module's table at validation time, never a second hand-written set.
_GRADES = ("A", "B", "C", "D", "F")


def _valid(entry) -> "dict | None":
    """One validated point, or None. Never raises."""
    if not isinstance(entry, dict):
        return None
    try:
        t = float(entry.get("t", 0))
        s = int(entry.get("s", -1))
    except (TypeError, ValueError, OverflowError):
        return None
    grade = entry.get("g")
    grade = grade.strip().upper() if isinstance(grade, str) else ""
    if t <= 0 or not 0 <= s <= 100 or grade not in _GRADES:
        return None
    point = {"t": t, "s": s, "g": grade}
    findings = _clean_findings(entry.get("f"))
    if findings:
        point["f"] = findings
    return point


def _clean_findings(raw) -> dict:
    """{sensor key: grade} for one checkup, bounded. Never raises.

    Only KNOWN grades (A-F) are kept: a sensor that answered "?" did not
    measure anything, and storing that would let a later diff call the
    missing measurement an improvement or a regression.
    """
    if not isinstance(raw, dict):
        return {}
    out = {}
    for key, grade in list(raw.items())[:HEALTH_FINDINGS_MAX]:
        if not isinstance(key, str) or not key.strip():
            continue
        g = grade.strip().upper() if isinstance(grade, str) else ""
        if g not in _GRADES:
            continue
        out[key.strip()[:HEALTH_FINDING_KEY_MAX]] = g
    return out


def findings_from_cards(cards) -> dict:
    """{sensor key: grade} for the checks a scan actually graded.

    One rule for both recording paths (the tab's scan and the automatic badge
    check): a card with a real letter grade is a finding; "?" is not, because
    it did not measure anything.
    """
    if not isinstance(cards, dict):
        return {}
    out = {}
    for key, card in list(cards.items())[:HEALTH_FINDINGS_MAX]:
        if not isinstance(key, str) or not key.strip():
            continue
        grade = card.get("grade") if isinstance(card, dict) else card
        g = grade.strip().upper() if isinstance(grade, str) else ""
        if g in _GRADES:
            out[key.strip()[:HEALTH_FINDING_KEY_MAX]] = g
    return out


def add_to_config(cfg, score, grade, when=None, findings=None) -> bool:
    """Append one point to ``cfg["health_history"]``; False when not a point.

    Used inside a caller's ``update_config`` mutation so a checkup's badge
    fields and its trend point are written once, under one lock. Best-effort
    validation: a missing/None score (a scan that could not measure) is not
    stored at all.
    """
    try:
        if score is None:
            return False
        s = int(round(float(score)))
        g = str(grade or "").strip().upper()
        if not 0 <= s <= 100 or g not in _GRADES:
            return False
        t = time.time() if when is None else float(when)
        if t <= 0:
            return False
        hist = cfg.get("health_history")
        if not isinstance(hist, list):
            hist = []
        clean = [e for e in (_valid(x) for x in hist) if e is not None]
        point = {"t": t, "s": s, "g": g}
        measured = _clean_findings(findings)
        if measured:
            point["f"] = measured
        clean.append(point)
        cfg["health_history"] = clean[-HEALTH_HISTORY_MAX:]
        return True
    except Exception:
        return False


def record(score, grade, when=None, findings=None) -> "dict | None":
    """Store one checkup's score (and what it measured). None if not stored.

    `findings` is the optional key -> grade map from `findings_from_cards`;
    it is what a later decline uses to name a cause. Best-effort: history is
    observability, never allowed to break a checkup.
    """
    try:
        from app.config_persist import update_config
        point = {}

        def _mut(cfg):
            if add_to_config(cfg, score, grade, when, findings):
                point.update(cfg["health_history"][-1])

        update_config(_mut)
        return dict(point) or None
    except Exception:
        return None


def clear() -> bool:
    """Forget every stored checkup point. True when the write went through.

    Only the trend ledger is emptied. The badge's last grade and last check
    time live in their own keys and stay, so the status row does not claim
    the machine has never been checked; nothing a run did is undone.

    Best-effort like `record`, but it REPORTS the outcome: the viewer has to
    be able to say "still here" instead of silently showing an empty list.
    """
    try:
        from app.config_persist import update_config

        def _mut(cfg):
            cfg["health_history"] = []

        update_config(_mut)
        return True
    except Exception:
        return False


def entries(limit: int = 0) -> "list[dict]":
    """Every stored point, oldest first. A missing ledger is simply empty.

    Re-validates what the loader handed over: a test can patch `load_config`,
    and a malformed row must be dropped rather than drawn as a dip.
    """
    try:
        from app.config_persist import load_config
        raw = load_config().get("health_history") or []
    except Exception:
        return []
    out = [e for e in (_valid(x) for x in raw) if e is not None]
    out.sort(key=lambda e: e["t"])
    if limit and limit > 0:
        out = out[-int(limit):]
    return out


#: A decline is only worth a callout when it is a RUN, not a wobble: this
#: many consecutive drops (so one more checkup than this) AND this many
#: points lost across them. Two one-point dips are noise; the tab says
#: nothing about them rather than training the reader to ignore it.
DECLINE_MIN_DROPS = 2
DECLINE_MIN_POINTS = 5


def _decline(scores) -> "dict | None":
    """The unbroken fall at the END of `scores`, if it is worth naming.

    Scans backwards and stops at the first checkup that did not drop. An
    equal score ends the run too: "three checks in a row" has to mean three
    strictly lower scores, or the sentence would be true of a flat week.
    """
    streak = 0
    for i in range(len(scores) - 1, 0, -1):
        if scores[i] < scores[i - 1]:
            streak += 1
        else:
            break
    if streak < DECLINE_MIN_DROPS:
        return None
    top = scores[-(streak + 1)]
    drop = top - scores[-1]
    if drop < DECLINE_MIN_POINTS:
        return None
    return {"streak": streak, "drop": drop, "from": top, "to": scores[-1],
            "causes": []}


def _worse_causes(old, new) -> "list[dict]":
    """Findings both checkups graded that got a WORSE letter in `new`.

    Both ends must have measured the key: a key missing from either map is
    "not measured", not "fixed" or "broken", so it cannot be named as the
    cause of anything. Entries are returned most-severe-first (the biggest
    grade fall, then the worse final grade, then key), so the tab names the
    same cause for the same data every time instead of whatever dict order
    happened to be.
    """
    old_f = old.get("f") if isinstance(old, dict) else None
    new_f = new.get("f") if isinstance(new, dict) else None
    if not isinstance(old_f, dict) or not isinstance(new_f, dict):
        return []
    causes = []
    for key, before in old_f.items():
        after = new_f.get(key)
        if before in _GRADES and after in _GRADES:
            if _GRADES.index(after) > _GRADES.index(before):
                causes.append({"key": key, "from": before, "to": after})
    causes.sort(key=lambda c: (-(_GRADES.index(c["to"])
                                 - _GRADES.index(c["from"])),
                               -_GRADES.index(c["to"]), c["key"]))
    return causes


def causes_between(old, new) -> "list[dict]":
    """Findings that got worse between two stored points. Public wrapper.

    The history window names the cause for every row, so the comparison rule
    (both ends must have measured the key) stays in this module instead of
    being copied into the UI.
    """
    return _worse_causes(old, new)


def trend(points=None) -> dict:
    """Everything the trend strip needs, from the stored points (or given).

    ``delta`` is the change from the PREVIOUS checkup, not from the first
    one: the card says "since last check", and a long history would make that
    sentence mean something else. ``overall`` is last minus first, for the
    longer view. ``decline`` is the run of drops at the end, when there is
    one long enough to act on - None otherwise, never a zero-shaped stand-in.
    Empty history yields an empty dict of zeros - never a fabricated score.
    """
    if points is None:
        pts = entries()
    else:
        pts = [e for e in (_valid(x) for x in points) if e is not None]
        # A caller-supplied list is sorted too: a trend drawn from a shuffled
        # list would describe a shape that never happened, and compute its
        # delta against the wrong neighbour.
        pts.sort(key=lambda e: e["t"])
    scores = [e["s"] for e in pts]
    if not scores:
        return {"count": 0, "points": [], "scores": [], "first": None,
                "last": None, "best": None, "worst": None, "delta": None,
                "overall": None, "decline": None, "first_ts": None,
                "last_ts": None, "span_days": 0.0}
    first, last = scores[0], scores[-1]
    decline = _decline(scores)
    if decline:
        # Compare the two ENDS of the run: those are the checkups the
        # sentence is about. An intermediate wobble inside the run is not
        # what dragged the latest score down.
        decline["causes"] = _worse_causes(
            pts[-(decline["streak"] + 1)], pts[-1])
    return {
        "count": len(scores),
        "points": pts,
        "scores": scores,
        "first": first,
        "last": last,
        "best": max(scores),
        "worst": min(scores),
        "delta": (last - scores[-2]) if len(scores) >= 2 else None,
        "overall": (last - first) if len(scores) >= 2 else None,
        "decline": decline,
        "first_ts": pts[0]["t"],
        "last_ts": pts[-1]["t"],
        "span_days": max(0.0, (pts[-1]["t"] - pts[0]["t"]) / 86400.0),
    }
