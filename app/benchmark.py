"""A lightweight before/after reading for a run (N-002).

The Scorecard already brackets disk space. Disk is not the whole story of a
tweak run, so a run also brackets two cheap, honest readings:

  * available physical memory -- GlobalMemoryStatusEx, the exact call
    app/pc_specs.py already makes, so there is no new dependency and no
    second copy of the struct;
  * the pending-restart flag (the CBS RebootPending key), which a tweak can
    set.

Both are single, sub-millisecond reads, so taking them on the worker thread
before and after a run costs nothing and keeps the Tk thread free.

The honest bit
--------------
Memory available moves on its own -- browsers, the OS, this app -- so a
few-hundred-MB wiggle across a two-second run is background noise, not a
result. compare() therefore only reports a memory move past a real floor
(16 MiB, or 0.5% of RAM) and, when nothing crossed it, returns an empty list
so the caller can say "no measurable change" instead of dressing a zero up
as a win. An unreadable reading is None and never invents a change.
"""
from __future__ import annotations

from typing import Optional

from app.utils import format_bytes

_CBS_REBOOT = (r"SOFTWARE\Microsoft\Windows\CurrentVersion\Component Based "
               r"Servicing\RebootPending")

#: Below this, a memory move is background noise rather than a result.
MEM_FLOOR = 16 << 20          # 16 MiB


def restart_pending() -> Optional[bool]:
    """True/False for the pending-restart flag, or None if it can't be read.

    A missing key means "not pending" (the flag exists only while pending),
    which is a real False, not an unknown."""
    try:
        import winreg
    except Exception:                       # not Windows -- no probe available
        return None
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, _CBS_REBOOT):
            pass
        return True
    except FileNotFoundError:
        return False
    except OSError:
        return False
    except Exception:
        return None


def memory():
    """(total_bytes, available_bytes), or (None, None) if unreadable."""
    try:
        from app.pc_specs import mem_totals
        total, avail = mem_totals()
        if total:
            return int(total), int(avail)
    except Exception:
        pass
    return None, None


def snapshot() -> dict:
    """A cheap reading taken before and after a run. Never raises."""
    total, avail = memory()
    return {"mem_total": total, "mem_avail": avail,
            "restart_pending": restart_pending()}


def _int_or_none(value):
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def compare(before, after):
    """Rows describing what a run measurably changed.

    Returns
    -------
    list of {"label", "text", "tone"}
        One row per real movement; ``[]`` when readings were taken but none
        crossed the noise floor ("no measurable change").
    None
        When neither snapshot carried a usable reading -- the caller should
        stay silent rather than claim a change it could not see.

    ``tone`` is one of "good" / "bad" / "warn"; the UI maps it to a color.
    """
    if not isinstance(before, dict) or not isinstance(after, dict):
        return None

    rows = []
    b_avail = _int_or_none(before.get("mem_avail"))
    a_avail = _int_or_none(after.get("mem_avail"))
    total = (_int_or_none(before.get("mem_total"))
             or _int_or_none(after.get("mem_total")) or 0)
    mem_measured = b_avail is not None and a_avail is not None
    if mem_measured:
        delta = a_avail - b_avail
        floor = max(MEM_FLOOR, int(total * 0.005)) if total else MEM_FLOOR
        if abs(delta) >= floor:
            if delta > 0:
                rows.append({"label": "Available memory",
                             "text": "+%s" % format_bytes(delta),
                             "tone": "good"})
            else:
                rows.append({"label": "Available memory",
                             "text": "\u2212%s" % format_bytes(-delta),
                             "tone": "bad"})

    b_restart = before.get("restart_pending")
    a_restart = after.get("restart_pending")
    restart_measured = b_restart is not None and a_restart is not None
    if restart_measured and bool(b_restart) != bool(a_restart):
        rows.append({
            "label": "Restart",
            "text": "now pending" if a_restart else "no longer pending",
            "tone": "warn" if a_restart else "good"})

    if not rows and not (mem_measured or restart_measured):
        return None
    return rows
