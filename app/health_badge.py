"""The PC Health badge: a one-word answer on the main window, kept fresh.

The health report used to be something you had to remember to open. This is
the always-visible half of it: a small badge in the status row that says
Great / Good / Fair / Bad (or "Check PC" before the first check), and opens
the full check on click. It re-checks by itself on the schedule the user
chose in Settings (at startup, daily, weekly, or never).

Everything here is READ-ONLY. The automatic check runs the same thirteen
read-only sensors the report uses and records only the overall grade and the
time. It never cleans, repairs or installs anything on its own - fixing is
always a button the user presses (Fix everything in the report).

Tk-free, so the check can run on a worker and the logic can be unit-tested.
"""
from __future__ import annotations

import time
from typing import Optional, Tuple

#: Grade -> (word, COLORS key). Four words, because a letter grade alone
#: tells a casual user nothing; "Bad" is honest for D and F.
_WORDS = {
    "A": ("Great", "accent_green"),
    "B": ("Good", "accent_green"),
    "C": ("Fair", "accent_yellow"),
    "D": ("Bad", "accent_red"),
    "F": ("Bad", "accent_red"),
}

UNCHECKED = ("Check PC", "subtext")

MODES = ("off", "startup", "daily", "weekly")

#: How stale a result may be before a schedule re-checks. A little under the
#: nominal period so a daily check still fires if the PC is only on at the
#: same time each day and the previous check landed a few minutes late.
_PERIOD = {"daily": 22 * 3600, "weekly": 6.5 * 86400}


def badge_for(grade: Optional[str]) -> Tuple[str, str]:
    """(label, COLORS key) for a grade. Unknown or empty -> 'Check PC'."""
    return _WORDS.get(str(grade or "").upper(), UNCHECKED)


def is_due(mode: str, last_ts: float, now: Optional[float] = None,
           at_startup: bool = False) -> bool:
    """Should an automatic check run now?

    off      never.
    startup  once per launch (the caller passes at_startup=True exactly once).
    daily / weekly   when the last check is older than the period - and also
                     at startup, so opening the app after a long gap shows a
                     fresh badge instead of a stale one.
    """
    mode = str(mode or "off").lower()
    if mode == "off" or mode not in MODES:
        return False
    if mode == "startup":
        return bool(at_startup)
    now = time.time() if now is None else float(now)
    last = float(last_ts or 0.0)
    if last <= 0 or last > now + 60:       # never checked, or clock went back
        return True
    return (now - last) >= _PERIOD[mode]


def age_text(last_ts: float, now: Optional[float] = None) -> str:
    """'just now', '3 hours ago', '2 days ago' - for the badge tooltip."""
    now = time.time() if now is None else float(now)
    last = float(last_ts or 0.0)
    if last <= 0:
        return "never checked"
    secs = max(0, now - last)
    if secs < 90:
        return "just now"
    if secs < 3600:
        return "%d minutes ago" % int(secs // 60)
    if secs < 86400:
        h = int(secs // 3600)
        return "%d hour%s ago" % (h, "" if h == 1 else "s")
    d = int(secs // 86400)
    return "%d day%s ago" % (d, "" if d == 1 else "s")


def record(grade: str, when: Optional[float] = None) -> None:
    """Persist the result. Single-lock read-modify-write via update_config,
    never a stale whole-dict save. Never raises (a badge must not be able to
    break the app)."""
    try:
        from app.config_persist import update_config
        g = str(grade or "").upper()
        ts = time.time() if when is None else float(when)

        def _mut(cfg, _g=g, _t=ts):
            cfg["health_last_grade"] = _g if _g in _WORDS else ""
            cfg["health_last_check"] = _t
        update_config(_mut)
    except Exception:
        pass


def load_state() -> Tuple[str, str, float]:
    """(mode, last_grade, last_ts) from the saved config; safe defaults."""
    try:
        from app.config_persist import load_config
        cfg = load_config()
        return (str(cfg.get("health_check_mode", "startup")),
                str(cfg.get("health_last_grade", "")),
                float(cfg.get("health_last_check", 0.0) or 0.0))
    except Exception:
        return ("startup", "", 0.0)


def quick_check(cancelled=lambda: False) -> str:
    """Run the read-only health scan and return the overall grade ('' if it
    could not be measured). Blocking: call from a worker. Records the result.
    """
    from app.health_scan import overall_grade, run_health_scan
    from app.utils import TaskContext
    ctx = TaskContext(log=lambda _m: None, set_status=lambda _m: None,
                      cancelled=cancelled)
    cards = run_health_scan(ctx)
    if not cards or cancelled():
        return ""
    grade = overall_grade(cards)
    if grade not in _WORDS:
        return ""
    record(grade)
    return grade
