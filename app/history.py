"""Run history & trends (N-006).

The Scorecard reports one run at a time; this is the layer that lets the app
look back. It reads the rolling ``run_history`` the run pipeline already
writes (``config_persist.record_run``), so nothing new is stored and there is
no second copy of the numbers to drift out of step.

Read-only and Tk-free, like app/tweak_center.py: the dialog decides how to
draw it, this decides what is true. An empty history yields empty results
(never a fabricated zero), and a malformed entry is skipped rather than
guessed at.
"""
from __future__ import annotations

import datetime as _dt

from typing import List, Tuple

#: Sparkline ramp, lowest first. Drawn from the Unicode block set so they
#: render in the app's normal UI font (no emoji fallback, no missing glyphs).
_RAMP = "\u2581\u2582\u2583\u2584\u2585\u2586\u2587\u2588"


def runs() -> List[dict]:
    """Every stored run as ``{"t": unix_seconds, "b": bytes}``, oldest first.

    Validated and sorted; a malformed entry is dropped, never repaired into
    a number it did not have."""
    try:
        from app.config_persist import load_config
        raw = load_config().get("run_history") or []
    except Exception:
        return []
    out = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        try:
            t = float(entry.get("t", 0))
            b = int(entry.get("b", 0))
        except (TypeError, ValueError):
            continue
        if t > 0 and b > 0:
            out.append({"t": t, "b": b})
    out.sort(key=lambda e: e["t"])
    return out


def total() -> Tuple[int, int]:
    """(total_bytes_reclaimed, run_count) across the whole stored history."""
    rs = runs()
    return sum(e["b"] for e in rs), len(rs)


def reclaimed_since(days: int = 30) -> Tuple[int, int]:
    """(bytes, count) in the last ``days`` days.

    Delegates to the one existing implementation so the Scorecard's 30-day
    sentence and this dialog can never disagree about the same window."""
    try:
        from app.config_persist import freed_since
        return freed_since(days)
    except Exception:
        return 0, 0


def daily(days: int = 30) -> List[int]:
    """Bytes reclaimed per local calendar day for the last ``days`` days,
    oldest first (index -1 is today).

    A day with no run is a real 0 — on a time axis a gap is information, not
    an unknown. Buckets by ``date`` (not by dividing by 86400) so daylight
    saving can't smear a run into the wrong day."""
    days = max(1, int(days))
    buckets = [0] * days
    today = _dt.date.today()
    start = today.toordinal() - (days - 1)
    for e in runs():
        try:
            day = _dt.date.fromtimestamp(e["t"])
        except (OverflowError, OSError, ValueError):
            continue
        pos = day.toordinal() - start
        if 0 <= pos < days:
            buckets[pos] += e["b"]
    return buckets


def sparkline(values: List[int]) -> str:
    """A one-line unicode sparkline of ``values``.

    All-zero input is a flat low line — a real "nothing happened" shape —
    rather than an empty string that would read as "no data"."""
    vals = [max(0, int(v)) for v in (values or [])]
    if not vals:
        return ""
    hi = max(vals)
    if hi <= 0:
        return _RAMP[0] * len(vals)
    top = len(_RAMP) - 1
    return "".join(
        _RAMP[max(0, min(top, int((v / hi) * top + 0.5)))] for v in vals)
