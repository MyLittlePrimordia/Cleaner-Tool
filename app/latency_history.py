"""Latency history (N-004): one-shot pings become a visible trend.

Every timing this app already takes is a claim about the present. "78 ms" is
useful; "78 ms, and it has been climbing for a week" answers the question
people actually ask. This keeps a small bounded series per target and renders
it with the same block ramp as app.history.

Nothing is estimated: a probe that got no reply records nothing, and a target
with one sample draws no line. Tk-free, like every other sampler here.
"""
from __future__ import annotations

import time

from app.config_schema import LATENCY_SAMPLES_MAX, LATENCY_TARGETS_MAX

_RAMP = "\u2581\u2582\u2583\u2584\u2585\u2586\u2587\u2588"


def _clean_target(target) -> str:
    return str(target or "").strip()[:80]


def record(target, ms) -> bool:
    """Append one measured latency. False when there is nothing to record."""
    name = _clean_target(target)
    try:
        value = float(ms)
    except (TypeError, ValueError, OverflowError):
        return False
    if not name or value <= 0 or value != value:
        return False
    try:
        from app.config_persist import update_config

        def _mut(cfg):
            table = cfg.get("latency_history")
            if not isinstance(table, dict):
                table = {}
            series = table.get(name)
            if not isinstance(series, list):
                series = []
            series.append({"t": time.time(), "ms": value})
            table[name] = series[-LATENCY_SAMPLES_MAX:]
            # A hand-edited file must not let one target crowd out the rest.
            if len(table) > LATENCY_TARGETS_MAX:
                for stale in list(table)[:-LATENCY_TARGETS_MAX]:
                    table.pop(stale, None)
            cfg["latency_history"] = table

        update_config(_mut)
        return True
    except Exception:
        return False


def series(target) -> list:
    """Measured values for `target`, oldest first. [] when none."""
    name = _clean_target(target)
    try:
        from app.config_persist import load_config
        table = load_config().get("latency_history")
        rows = table.get(name) if isinstance(table, dict) else None
    except Exception:
        return []
    out = []
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        try:
            value = float(row.get("ms"))
        except (TypeError, ValueError, OverflowError):
            continue
        if value > 0 and value == value:
            out.append(value)
    return out[-LATENCY_SAMPLES_MAX:]


def spark(target) -> str:
    """A block sparkline over the stored samples, or "" when there is no line."""
    values = series(target)
    if len(values) < 2:
        return ""
    lo, hi = min(values), max(values)
    span = hi - lo
    if span <= 0:
        return _RAMP[0] * len(values)
    chars = []
    for value in values:
        index = int(round((value - lo) / span * (len(_RAMP) - 1)))
        chars.append(_RAMP[max(0, min(len(_RAMP) - 1, index))])
    return "".join(chars)


def summary(target):
    """(last, best, count) or None. Best is the lowest measured value."""
    values = series(target)
    if not values:
        return None
    return values[-1], min(values), len(values)


__all__ = ["record", "series", "spark", "summary"]
