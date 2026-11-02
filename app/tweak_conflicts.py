"""N-001: two selected tweaks that write the same value.

The Tweak tab can run a dozen tasks at once. Each one records its own snapshot
and each one reports "applied", but when two of them set the same registry
value Windows keeps only the last write: one tweak silently wins, the other's
badge lies, and Undo replays two snapshots of the same value.

`tweak_introspect.scan_tasks()` already derives, per task, the registry values
it writes (from the same AST the preview and the SEC-001 guard use), so this is
a comparison over data the app already has. Nothing is claimed without both
values: an unknown target, or two deletes of the same value, is not a clash.
Tk-free and pure.
"""
from __future__ import annotations

_MAX_REPORTED = 8
_MAX_LISTED = 4


def _where(item) -> str:
    path = str(item.get("path") or "")
    name = str(item.get("name") or "")
    if len(path) > 48:
        path = "\u2026" + path[-47:]
    return "%s\\%s" % (path, name)


def conflicts(task_ids, info=None):
    """[{a, b, path, name, a_value, b_value, reason}] for the given task keys.

    `info` is `tweak_introspect.scan_tasks()`; passing it in keeps the tests
    offline. A pair is reported only when the app can prove both sides: two
    different literal values, or a write against a delete.
    """
    if info is None:
        try:
            from app.tweak_introspect import scan_tasks
            info = scan_tasks()
        except Exception:
            return []
    seen = {}
    out = []
    for key in task_ids or ():
        entry = info.get(key)
        if not isinstance(entry, dict):
            continue
        title = entry.get("title") or key
        for triple, value in (entry.get("regs") or {}).items():
            prior = seen.setdefault(triple, [])
            for earlier in prior:
                if earlier["key"] == key or earlier.get("deletes"):
                    continue
                if value is None or earlier.get("value") is None:
                    continue
                if str(value) == str(earlier.get("value")):
                    continue
                out.append({
                    "a": earlier["title"], "b": title,
                    "path": triple[1], "name": triple[2],
                    "a_value": earlier.get("value"), "b_value": value,
                    "reason": "both set it",
                })
            prior.append({"key": key, "title": title, "value": value,
                          "deletes": False})
        for triple in (entry.get("deletes") or ()):
            prior = seen.setdefault(triple, [])
            for earlier in prior:
                if earlier["key"] == key:
                    continue
                if earlier.get("deletes"):
                    continue
                out.append({
                    "a": earlier["title"], "b": title,
                    "path": triple[1], "name": triple[2],
                    "a_value": earlier.get("value"), "b_value": None,
                    "reason": "one removes it, the other sets it",
                })
            prior.append({"key": key, "title": title, "value": None,
                          "deletes": True})
    return out[:_MAX_REPORTED]


def describe(clashes, limit=_MAX_LISTED):
    """A confirmation body for `clashes`, honest about what it cannot see."""
    if not clashes:
        return ""
    lines = ["%s and %s both change %s (%s)."
             % (c["a"], c["b"], _where(c), c["reason"])
             for c in clashes[:limit]]
    if len(clashes) > limit:
        lines.append("...and %d more." % (len(clashes) - limit))
    lines.append("")
    lines.append("Windows keeps one value, so the tweak that runs last wins.")
    return "\n".join(lines)


__all__ = ["conflicts", "describe"]
