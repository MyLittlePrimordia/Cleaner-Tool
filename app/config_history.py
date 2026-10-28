"""The rolling "how is my PC doing?" run ledger (R-006/R-005).

Split out of `app.config_persist`: these three writers only ever touch one
config key (`run_history`), while the rest of that module owns loading,
saving, recovery and locking. Keeping them here means the ledger's rules -
what counts as a rememberable run, how many runs keep their details - sit
in one place instead of in the middle of a 1300-line file.

`app.config_persist` re-exports all four names, so existing importers are
unchanged. The load/save helpers are imported *inside* each function: this
module is imported from config_persist, so a top-level import would cycle.
"""
from __future__ import annotations

import time

from app.config_schema import (
    RUN_CHANGE_DETAIL_MAX,
    RUN_CHANGE_MAX,
    RUN_CHANGE_TARGET_MAX,
    RUN_HISTORY_CHANGE_RUNS,
    RUN_HISTORY_MAX,
    RUN_KIND_MAX,
)


def _history_changes(changes) -> "list[dict]":
    """Display-ready change records for the rolling history (R-005).

    Collapsed first, so a run that wrote the same value twice shows one row,
    and capped so one privacy-baseline run cannot turn the history into a
    registry dump. Raw values are rendered here, not stored: history is
    read-only and must stay plain JSON.
    """
    try:
        from app.changes import change_detail, collapse
        merged = collapse(list(changes or ()))
    except Exception:
        return []
    out = []
    for change in merged[:RUN_CHANGE_MAX]:
        try:
            record = {"k": str(change.kind)[:RUN_KIND_MAX],
                      "l": str(change.label)[:RUN_CHANGE_TARGET_MAX],
                      "d": change_detail(change)[:RUN_CHANGE_DETAIL_MAX]}
            target = str(change.target)
            if target and target != record["l"]:
                record["p"] = target[:RUN_CHANGE_TARGET_MAX]
            out.append(record)
        except Exception:
            continue
    return out


def record_run(bytes_freed: int, *, kind: str = "clean", changes=()) -> None:
    """Append one finished run to the rolling history.

    R-005: a run is worth remembering when it freed space OR changed
    something, so a Tweak/Repair/Undo run with no byte count is still recorded
    (its typed changes are the point). `kind` labels which surface ran it.

    N-010: `bytes_freed` is the run's CREDITED regain (see `app/freed.py`) -
    the caller passes the verified number, not the raw sum of file sizes. This
    layer stores what it is given and never re-derives it, so the ledger and
    the scorecard cannot disagree about one run.

    Called on the Tk thread right after a run lands. Best-effort: a
    failure here must never disturb the run's own outcome, so every
    error is swallowed (same contract as mark_tweak_applied)."""
    try:
        n = int(bytes_freed or 0)
    except (TypeError, ValueError):
        return
    compact = _history_changes(changes)
    if n <= 0 and not compact:
        return          # nothing freed and nothing changed = nothing to remember
    try:
        from app.config_persist import update_config
        stamp = time.time()
        label = str(kind or "clean")[:RUN_KIND_MAX]

        def _mut(cfg):
            hist = cfg.get("run_history")
            if not isinstance(hist, list):
                hist = []
            entry = {"t": stamp, "b": n, "k": label}
            if compact:
                entry["c"] = compact
            hist.append(entry)
            hist = hist[-RUN_HISTORY_MAX:]
            # Match the schema's rule: only the newest runs keep detail.
            for old in hist[:-RUN_HISTORY_CHANGE_RUNS]:
                if isinstance(old, dict):
                    old.pop("c", None)
            cfg["run_history"] = hist

        update_config(_mut)
    except Exception:
        pass


def clear_run_history() -> bool:
    """Forget every recorded run. True when the write went through.

    Only the ledger is emptied: nothing a run actually changed is touched, so
    clearing history can never undo a registry value or re-enable a service.
    Best-effort like record_run, but it DOES report success — the viewer has
    to be able to say "still here" instead of silently showing an empty list."""
    try:
        from app.config_persist import update_config

        def _mut(cfg):
            cfg["run_history"] = []

        update_config(_mut)
        return True
    except Exception:
        return False


def freed_since(days: int = 30) -> "tuple[int, int]":
    """(total_bytes_freed, run_count) over the last `days` days.

    Returns (0, 0) when there is no history yet — the caller then shows
    no sentence at all rather than a zero.

    H-011: the window rule lives in `history.reclaimed_in()`, shared with the
    scorecard and the history dialog. This function is the "read config, then
    summarise" door; it must not grow a second copy of the cutoff maths."""
    try:
        from app.config_persist import load_config
        from app.history import reclaimed_in
        return reclaimed_in(load_config().get("run_history", []) or [], days)
    except Exception:
        return 0, 0
