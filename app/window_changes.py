"""N-005: what this app changed around a point in time.

The Safety Center lists restore points, and every run already stores the exact
values it changed. Together those answer a question neither can answer alone:
"what did this app do just before this restore point?" - read-only, using the
ledger that exists, with no new storage and no restoring.
"""
from __future__ import annotations


def changes_between(start, end):
    """Change records from runs inside ``[start, end]`` (epoch seconds).

    Returns [] for a malformed window rather than guessing at one. Each row is
    ``{t, run, kind, label, detail}`` - the run's kind and its display-ready
    change records, exactly as the ledger stores them.
    """
    try:
        start = float(start)
        end = float(end)
    except (TypeError, ValueError, OverflowError):
        return []
    if end < start:
        start, end = end, start
    try:
        from app.history import runs
        rows = runs()
    except Exception:
        return []
    out = []
    for run in rows:
        when = float(run.get("t") or 0)
        if not (start <= when <= end):
            continue
        for change in run.get("c") or ():
            if not isinstance(change, dict):
                continue
            out.append({
                "t": when,
                "run": run.get("k") or "clean",
                "kind": change.get("k", ""),
                "label": change.get("l", ""),
                "detail": change.get("d", ""),
            })
    return out


__all__ = ["changes_between"]
