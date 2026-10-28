"""What the Tools hub knows about YOU: what you opened, what needs attention.

AUDIT TV-001 (2026-10). Twenty-six equal cards in historical order is a
catalogue, not a hub: there is no memory of what the user actually uses, and
nothing on the page connects the PC's own state to a tool that can act on it.

This module is the Tk-free half of the hub:

  * `remember()` / `recent()` keep a tiny rolling list of opened tools in the
    config, so the page can put them first. It stores card KEYS, never titles
    or paths, and is capped by config_schema.TOOLS_RECENT_MAX.

  * `recommendations()` names at most three tools and says WHY, using only
    facts the app has already measured:
        - findings other tools published to the bus (app.insights),
        - the latest PC Health checkup's finding grades (if it is recent), and
        - the system drive's free space, read live with ctypes (instant).
    It never runs a scan to fill the hub, never guesses a reason, and returns
    no row when it has no evidence. A healthy PC gets no "recommended" band
    rather than three arbitrary cards.

    A tool's own finding is more specific than a grade ("Corsair MP600 reports
    a warning" vs "Disk scored C"), so it wins a tie; a worse grade from the
    checkup still wins, because the hub must not downgrade a real problem.

  * `badges()` turns the same live findings into the count and severity shown
    on the Storage, Duplicates, Game Readiness, Startup and Services cards,
    so the catalogue says what needs a look before the user opens a tool.

The reasons are phrased as facts ("Last checkup: Disk Space scored C", "C: is
8% free"), never as commands. The hub offers the tool; the user decides.

Tk-free on purpose: the wording and the grades are testable without a display.
"""
from __future__ import annotations

import os
import time

from app.config_schema import TOOLS_RECENT_MAX

#: A health checkup older than this is history, not a recommendation. The
#: grade may still be true, but the hub must not present a stale measurement
#: as current advice.
FRESH_DAYS = 14

#: How many recommendations the page shows. More than three turns a helpful
#: band into a second catalogue.
RECOMMEND_MAX = 3

#: A finding that is worth acting on. A/B are fine; "?" measured nothing.
_ACTION_GRADES = ("C", "D", "F")
_RANK = {"F": 0, "D": 1, "C": 2}

#: Sensor key -> the card that can act on it. Windows/security/time findings
#: have no single Tools card (their fixes live in Repair or Health), so they
#: are deliberately absent rather than pointed at a tool that cannot help.
#: A `drivers` finding is the same case: its fix is the Drivers TAB, which is
#: not a card in this catalogue, and pointing it at Game Readiness would send
#: someone with an undriven fingerprint reader to a gaming screen.
_FINDING_TOOL = {
    "disk": "storage",
    "junk": "storage",
    "memory": "procman",
    "startup": "startup",
    "maintenance": "drivetoolkit",
    "smart": "drivetoolkit",
    "gpu": "game_readiness",
}

#: A bus finding's action target -> the Tools card that owns that screen.
#: Targets with no card (Drivers is a main tab, not a tool) are absent rather
#: than pointed at a card that cannot help.
_TARGET_TOOL = {
    "storage": "storage",
    "drivetoolkit": "drivetoolkit",
    "drivetool": "drivetoolkit",
    "duplicates": "duplicates",
    "readiness": "game_readiness",
    "startup": "startup",
    "services": "services",
    "procmgr": "procman",
}

#: Cards that carry a live finding count. These are the tools whose own scan
#: produces a persistent, countable condition (junk, duplicate groups, startup
#: load, background services, readiness rows). The badge belongs to the scan's
#: owner, not to every tool in the same domain: a RAM warning is Health's to
#: surface, not the Process Manager card's to invent.
_BADGED_TOOLS = ("storage", "duplicates", "game_readiness", "startup",
                 "services")


# --------------------------------------------------------------------------- #
# Recency
# --------------------------------------------------------------------------- #
def remember(key, limit=TOOLS_RECENT_MAX):
    """Move `key` to the front of the recent-tools list. Best-effort."""
    key = str(key or "").strip()
    if not key:
        return []
    try:
        from app.config_persist import update_config

        def _mutate(cfg):
            old = cfg.get("tools_recent")
            items = [str(k) for k in old if isinstance(k, str)] \
                if isinstance(old, list) else []
            items = [k for k in items if k != key]
            items.insert(0, key)
            cfg["tools_recent"] = items[:max(1, int(limit))]
            return cfg

        cfg = update_config(_mutate)
        return list(cfg.get("tools_recent") or [])
    except Exception:
        return []


def recent(limit=TOOLS_RECENT_MAX):
    """The stored keys, newest first. Never raises."""
    try:
        from app.config_persist import load_config
        items = load_config().get("tools_recent")
        if not isinstance(items, list):
            return []
        return [k for k in items[:max(1, int(limit))]
                if isinstance(k, str) and k.strip()]
    except Exception:
        return []


# --------------------------------------------------------------------------- #
# Recommendations
# --------------------------------------------------------------------------- #
def _latest_checkup(now=None):
    """The newest usable health point, or None."""
    try:
        from app.health_history import entries
        points = entries()
    except Exception:
        return None
    if not points:
        return None
    latest = max(points, key=lambda p: p.get("t", 0))
    try:
        age = (time.time() if now is None else float(now)) - float(
            latest.get("t", 0))
    except (TypeError, ValueError):
        return None
    if age < 0 or age > FRESH_DAYS * 86400:
        return None
    return latest


def _drive_recommendation():
    """(key, reason, grade) when the system drive is actually tight."""
    try:
        import ctypes
        root = os.environ.get("SYSTEMDRIVE", "C:")
        if not root.endswith("\\"):
            root += "\\"
        free = ctypes.c_ulonglong(0)
        total = ctypes.c_ulonglong(0)
        avail = ctypes.c_ulonglong(0)
        ok = ctypes.windll.kernel32.GetDiskFreeSpaceExW(
            ctypes.c_wchar_p(root), ctypes.byref(avail),
            ctypes.byref(total), ctypes.byref(free))
        if not ok or not total.value:
            return None
        from app.health_scan import grade_disk_space
        ratio = free.value / total.value
        grade = grade_disk_space(ratio)
        if grade not in _ACTION_GRADES:
            return None
        return ("storage",
                "%s is %d%% free." % (root[:2], round(ratio * 100)),
                grade)
    except Exception:
        return None


def _bus_recommendations(now=None):
    """[(tool key, reason, grade)] for findings tools actually published.

    Only warn/critical: an `info` row is context, not a reason to interrupt
    the catalogue. The bus already sorts worst-first, so the first finding
    for a tool is the strongest one; a second finding for the same tool must
    not replace it with a weaker claim.
    """
    try:
        from app import insights
        findings = insights.active(min_severity="warn", now=now)
    except Exception:
        return []
    out = []
    seen = set()
    for finding in findings:
        action = getattr(finding, "action", {}) or {}
        key = _TARGET_TOOL.get(str(action.get("target") or ""))
        if not key or key in seen:
            continue
        seen.add(key)
        reason = str(getattr(finding, "title", "") or "")
        detail = str(getattr(finding, "detail", "") or "").strip()
        if detail:
            reason = "%s - %s" % (reason, detail)
        if len(reason) > 160:
            reason = reason[:157].rstrip() + "..."
        grade = "F" if getattr(finding, "severity", "") == "critical" \
            else "C"
        out.append((key, reason, grade))
    return out


def badges(now=None):
    """{tool key: (finding count, worst severity)} for the badged cards.

    Live, not a stored counter: every call reads the findings bus, so a
    finding that expired or was retracted disappears here as soon as it
    disappears from the Health tab. An unreadable bus returns {} - a
    badge is a claim about the machine, and the hub must not invent one.
    """
    try:
        from app import insights
        findings = insights.active(min_severity="warn", now=now)
    except Exception:
        return {}
    out = {}
    for finding in findings:
        action = getattr(finding, "action", {}) or {}
        if not isinstance(action, dict):
            continue
        key = _TARGET_TOOL.get(str(action.get("target") or ""))
        if key not in _BADGED_TOOLS:
            continue
        severity = str(getattr(finding, "severity", "") or "")
        if severity not in ("warn", "critical"):
            continue
        count, previous = out.get(key, (0, ""))
        out[key] = (count + 1,
                    "critical" if "critical" in (previous, severity)
                    else "warn")
    return out


def card_notes(limit=3, now=None):
    """{tool key: [finding title, ...]} for the badged cards.

    A badge says how many; this says what. The card's tooltip reads it so
    "2 issues" is never an unexplained number, and the ORDER is the bus order
    (worst first), so the first line is the one to act on.
    """
    try:
        from app import insights
        findings = insights.active(min_severity="warn", now=now)
    except Exception:
        return {}
    out = {}
    for finding in findings:
        action = getattr(finding, "action", {}) or {}
        if not isinstance(action, dict):
            continue
        key = _TARGET_TOOL.get(str(action.get("target") or ""))
        if key not in _BADGED_TOOLS:
            continue
        title = str(getattr(finding, "title", "") or "").strip()
        if not title:
            continue
        bucket = out.setdefault(key, [])
        if len(bucket) < max(1, int(limit)):
            bucket.append(title)
    return out


def recommendations(now=None):
    """[(tool key, one-line reason, grade)] worst evidence first.

    Deduplicated by tool, worst grade wins, capped at RECOMMEND_MAX. An
    unreadable source contributes nothing - never a generic suggestion.
    """
    found = {}
    for key, reason, grade in _bus_recommendations(now):
        if key not in found:
            found[key] = (key, reason, grade)

    latest = _latest_checkup(now)
    if latest:
        findings = latest.get("f")
        if isinstance(findings, dict):
            try:
                from app.health_scan import SENSORS
                labels = dict((sp[0], sp[1]) for sp in SENSORS)
            except Exception:
                labels = {}
            for sensor, grade in findings.items():
                if grade not in _ACTION_GRADES:
                    continue
                key = _FINDING_TOOL.get(str(sensor))
                if not key:
                    continue
                label = labels.get(sensor, str(sensor).replace("_", " "))
                candidate = (key, "Last checkup: %s scored %s." % (label,
                                                                  grade),
                             grade)
                prev = found.get(key)
                if prev is None or _RANK.get(grade, 9) < _RANK.get(prev[2], 9):
                    found[key] = candidate

    drive = _drive_recommendation()
    if drive and (drive[0] not in found
                  or _RANK.get(drive[2], 9) < _RANK.get(found[drive[0]][2], 9)):
        found[drive[0]] = drive

    out = sorted(found.values(),
                 key=lambda row: (_RANK.get(row[2], 9), row[0]))
    return out[:RECOMMEND_MAX]
