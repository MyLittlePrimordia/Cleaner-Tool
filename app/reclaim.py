"""N-002: one ordered plan across the tools, instead of five separate windows.

The app already knows where space and sign-in time go, but each answer lives
behind its own card: Storage Insight measures junk by category, Duplicates
finds copies, Uninstall Programs owns leftovers, the Drivers tab owns the
driver store (U-005), Startup Manager owns what launches at boot. Someone whose only goal is
"get my PC back" has to guess the order nobody states.

`build_plan` is that statement. It takes the numbers a caller already measured,
adds the steps that have no byte count because they are decisions rather than
measurements, and returns one list, biggest measured win first. Nothing here
scans, shells out or reads a config: it is a pure function with a stable order,
so the dialog that draws it stays a drawing problem.

Honesty rules, carried from the rest of the app:

* a measured step's number is what this walk found, never an estimate of what
  a scan *might* find;
* an unmeasured step says so in its own words and is never rendered as 0;
* a step with no action is dropped rather than shown as a dead row.

The module is Tk-free (the dialog decides how to draw a step) and depends on
`app.utils.format_bytes` only, so core code can compose a plan too.
"""
from __future__ import annotations

from app.utils import format_bytes

#: Steps that are not part of the disk walk, in the order a user should think
#: about them. `target` is a route target (app.routes), and `why` is the one
#: sentence the row shows instead of a byte count.
_EXTRAS = (
    ("duplicates", "Duplicate & Large Files",
     "Copies are found by content, not by name. Open the tool to scan.",
     "duplicates"),
    ("uninstall", "Programs you don't use",
     "Installed apps plus their leftovers. Open the tool to review.",
     "uninstall"),
    # U-005: the driver store got its own window, so this row points at the
    # screen that can actually remove what it names. Before, it sent the user
    # to Drive Toolkit, which has no driver-store view at all.
    ("drivestore", "Driver store leftovers",
     "Superseded driver packages Windows keeps after an update. Open the "
     "driver store to see them.", "driver_store"),
)


def _size(value):
    """A byte count, or None. Anything else is 'not measured', never 0."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value > 0 else 0
    return None


def build_plan(categories, extras=True) -> dict:
    """The ordered reclaim plan.

    `categories` is what the caller measured, one dict each:

        {"title": "Game files & caches", "bytes": 2_100_000_000,
         "keys": ["shader_cache", ...], "unknown": 2}

    `bytes=None` means "this walk could not measure it" and `unknown` counts
    the member cleaners that declined to look, which is why a measured total
    can still be partial.

    Returns {"steps": [...], "measured_total": int, "partial": bool}:

    * steps with bytes and keys sort first, largest first (ties keep the
      caller's order, so two scans of the same machine never reshuffle);
    * then measured-but-unclearable steps (bytes, no keys): shown, not runnable;
    * then the unmeasured extras (the cross-tool decisions).
    """
    measured = []
    unfinished = []
    for index, raw in enumerate(categories or ()):
        if not isinstance(raw, dict):
            continue
        title = str(raw.get("title") or "").strip()
        if not title:
            continue
        size = _size(raw.get("bytes"))
        keys = [str(k) for k in (raw.get("keys") or ()) if str(k)]
        try:
            unknown = max(0, int(raw.get("unknown") or 0))
        except (TypeError, ValueError):
            unknown = 0
        if size is None:
            unfinished.append({
                "id": "cat:%s" % title,
                "title": title,
                "bytes": None,
                "keys": keys,
                "detail": "Not measured in this scan.",
                "severity": "info",
                "action": "",
                "target": "",
            })
            continue
        detail = "%s, %d cleaner%s" % (format_bytes(size), len(keys),
                                       "" if len(keys) == 1 else "s")
        if unknown:
            detail += " \\u00b7 %d did not look" % unknown
        measured.append({
            "id": "cat:%s" % title,
            "title": title,
            "bytes": size,
            "keys": keys,
            "detail": detail,
            "severity": "warn" if size else "ok",
            "action": "Clean" if keys else "",
            "target": "",
            "_order": index,
        })

    # Largest first; equal sizes keep the caller's order.
    measured.sort(key=lambda s: (-int(s["bytes"]), s["_order"]))
    for step in measured:
        step.pop("_order", None)

    total = sum(int(s["bytes"]) for s in measured)
    steps = list(measured)
    # A category the walk could not measure is still a step (the user may want
    # to open it), it is just never drawn as a byte count.
    steps.extend(unfinished)
    if extras:
        for eid, title, why, target in _EXTRAS:
            steps.append({
                "id": "extra:%s" % eid,
                "title": title,
                "bytes": None,
                "keys": [],
                "detail": why,
                "severity": "info",
                "action": "Open",
                "target": target,
            })

    # `partial` is about the MEASURED categories only: the extras never had a
    # size to lose, so their absence of one is not a partial total.
    return {
        "steps": steps,
        "measured_total": total,
        "partial": bool(unfinished),
    }


def headline(plan) -> str:
    """"X can be freed" — with the '?' the rest of the app uses when a walk
    declined to look, instead of a total that pretends to be complete."""
    try:
        total = int((plan or {}).get("measured_total") or 0)
    except (TypeError, ValueError):
        total = 0
    if not total:
        return "Nothing measured"
    text = format_bytes(total)
    return text + " + ?" if (plan or {}).get("partial") else text


def runnable_keys(step) -> list:
    """The Clean-tab task keys a step can run, [] when it only opens a tool."""
    if not isinstance(step, dict):
        return []
    if step.get("target"):
        return []
    return [str(k) for k in (step.get("keys") or ()) if str(k)]


__all__ = ["build_plan", "headline", "runnable_keys"]
