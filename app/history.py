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
import time as _time

from typing import List, Tuple

#: Sparkline ramp, lowest first. Drawn from the Unicode block set so they
#: render in the app's normal UI font (no emoji fallback, no missing glyphs).
_RAMP = "\u2581\u2582\u2583\u2584\u2585\u2586\u2587\u2588"


#: Display names for the surface that produced a run. An entry written
#: before R-005 has no kind; those were all Clean runs, and that is the label
#: it gets. An unknown future kind is title-cased rather than hidden.
_KIND_LABELS = {"clean": "Clean", "tweak": "Tweak", "repair": "Repair",
                "undo": "Undo", "run": "Run"}


def kind_label(kind: str) -> str:
    kind = str(kind or "clean").strip().lower()
    return _KIND_LABELS.get(kind, kind.replace("_", " ").title() or "Run")


def _stored_changes(entry: dict) -> List[dict]:
    """Validated ``{k, l, d, p}`` change records from one stored run.

    The schema already sanitizes these on load; this repeats the check
    because `history` promises to validate whatever it is handed (a test can
    patch `load_config`) and a malformed row must be dropped, not rendered.
    """
    raw = entry.get("c")
    if not isinstance(raw, list):
        return []
    out = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        kind = item.get("k")
        label = item.get("l")
        detail = item.get("d")
        if not all(isinstance(v, str) and v.strip()
                   for v in (kind, label, detail)):
            continue
        target = item.get("p")
        out.append({"k": kind.strip(), "l": label.strip(),
                    "d": detail.strip(),
                    "p": target.strip() if isinstance(target, str) else ""})
    return out


def runs() -> List[dict]:
    """Every stored run, oldest first.

    Each run is ``{"t": unix_seconds, "b": bytes, "k": kind, "c": changes}``;
    ``k`` defaults to "clean" and ``c`` is empty for an entry written before
    R-005. A run with no bytes is kept when it has typed changes: a Tweak run
    frees nothing but is exactly what the ledger exists to remember.

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
        except (TypeError, ValueError, OverflowError):
            continue
        if t <= 0 or b < 0:
            continue
        changes = _stored_changes(entry)
        if b <= 0 and not changes:
            continue
        kind = entry.get("k")
        out.append({"t": t, "b": b,
                    "k": str(kind).strip().lower()
                    if isinstance(kind, str) and kind.strip() else "clean",
                    "c": changes})
    out.sort(key=lambda e: e["t"])
    return out


def clear() -> bool:
    """Forget every recorded run; False means the file could not be written.

    The write side lives with the other history writers (`record_run`) so both
    ends share one notion of what `run_history` is; this is the Tk-free door
    the viewer calls, because nothing above this module touches config.json.
    """
    try:
        from app.config_persist import clear_run_history
        return bool(clear_run_history())
    except Exception:
        return False


def total() -> Tuple[int, int]:
    """(total_bytes_reclaimed, run_count) across the whole stored history."""
    rs = runs()
    return sum(e["b"] for e in rs), len(rs)


def reclaimed_in(entries, days: int = 30) -> Tuple[int, int]:
    """(bytes, count) among ``entries`` inside the last ``days`` days.

    THE window rule (H-011). The scorecard works from runs it already loaded,
    `reclaimed_since()` from the validated run list, and
    `config_persist.freed_since()` from the raw config entries - all three go
    through this function, so they cannot drift apart about what "the last 30
    days" means. A malformed entry contributes nothing rather than raising."""
    try:
        cutoff = _time.time() - (max(1, int(days)) * 86400)
    except (TypeError, ValueError):
        return 0, 0
    total, count = 0, 0
    for entry in entries or ():
        if not isinstance(entry, dict):
            continue
        try:
            if float(entry.get("t", 0)) >= cutoff:
                total += int(entry.get("b", 0) or 0)
                count += 1
        except (TypeError, ValueError, OverflowError):
            continue
    return total, count


def reclaimed_since(days: int = 30) -> Tuple[int, int]:
    """(bytes, count) in the last ``days`` days, from the validated runs."""
    try:
        return reclaimed_in(runs(), days)
    except Exception:
        return 0, 0


def deltas(entries) -> list:
    """Each entry's byte change against the previous run of the SAME kind.

    N-007. A single run's number answers "did this help" only if the user
    remembers the last one, which nobody does. The comparison that is
    actually meaningful is like-for-like: a Tweak run freeing 0 bytes beside
    a 4 GB Clean is not a regression, and calling it one would make the
    ledger's one honest signal dishonest.

    Returns a list aligned with `entries` (the caller's order is preserved):
    each item is None when there is nothing to compare - the first run of that
    kind, or one of the two that freed no space - or
    {"bytes": current - previous, "previous": <the earlier entry>}.

    Order-independent and pure: it sorts a copy by time, so a caller may hand
    it newest-first (the viewer) or oldest-first (history.runs()) and get the
    same answers.
    """
    def _when(item) -> float:
        try:
            return float(item.get("t", 0) or 0)
        except (TypeError, ValueError, AttributeError):
            return 0.0

    seq = list(entries or ())
    out = [None] * len(seq)
    order = sorted(range(len(seq)), key=lambda i: _when(seq[i]))
    previous_by_kind = {}
    for index in order:
        entry = seq[index]
        kind = str(entry.get("k") or "clean")
        earlier = previous_by_kind.get(kind)
        if earlier is not None:
            try:
                current_bytes = int(entry.get("b", 0) or 0)
                earlier_bytes = int(earlier.get("b", 0) or 0)
            except (TypeError, ValueError, OverflowError):
                current_bytes = earlier_bytes = 0
            if current_bytes > 0 and earlier_bytes > 0:
                out[index] = {"bytes": current_bytes - earlier_bytes,
                              "previous": earlier}
        previous_by_kind[kind] = entry
    return out


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
