"""Typed records of what a task actually changed (audit 2026-10, R-005).

The scorecard used to learn "what happened" from two places: a memory /
restart-flag reading taken around the run (app/benchmark.py) and the free-text
log lines a task wrote while it worked. The first only sees side effects broad
enough to move a number; the second is prose, so the card cannot group it,
count it, or copy it into a support request.

This module is the third thing: a small, Tk-free record that the shared write
layer fills in at the moment of the write, where the prior and new value are
both in hand. It deliberately depends on NOTHING in the app, so `app.utils`
can import it without pulling in the runner package (whose `__init__` imports
`task_runner`, which imports `app.utils` again).

What gets recorded, and why only these
--------------------------------------
Registry writes/deletes have an exact, cheap before/after, so they are always
recorded. Commands are recorded only when their argv says what kind of state
they change -- `sc config`, `schtasks /change`, `powercfg` and `bcdedit` -- and
only when the command succeeded. `net stop`/`net start` are deliberately NOT
recorded: they are volatile (a reboot undoes them) and every task that uses one
also makes the persistent change through `sc config`, so recording both would
make the card's last-writer-wins line say "stopped" when the durable answer is
"disabled". Unrecognised commands are not recorded at all rather than guessed
at.
"""
from __future__ import annotations

from dataclasses import dataclass

KIND_REGISTRY = "registry"
KIND_SERVICE = "service"
KIND_SCHEDULED_TASK = "scheduled_task"
KIND_POWER = "power"
KIND_BOOT = "boot"

#: Display order for grouped counts; also the order rows are listed in.
KIND_ORDER = (KIND_REGISTRY, KIND_SERVICE, KIND_SCHEDULED_TASK,
              KIND_POWER, KIND_BOOT)

_NOUNS = {
    KIND_REGISTRY: ("registry value", "registry values"),
    KIND_SERVICE: ("service", "services"),
    KIND_SCHEDULED_TASK: ("scheduled task", "scheduled tasks"),
    KIND_POWER: ("power setting", "power settings"),
    KIND_BOOT: ("boot setting", "boot settings"),
}


def kind_noun(kind: str, count: int = 2) -> str:
    """Singular/plural noun for a kind; unknown kinds fall back to the raw id."""
    singular, plural = _NOUNS.get(kind, (kind, kind))
    return singular if count == 1 else plural


@dataclass(frozen=True)
class Change:
    """One state change: what, from what, to what.

    ``before is None`` means "was not set" for a registry value and "no prior
    value was read" for a command; ``before_known`` tells the two apart so the
    card never renders a made-up "None -> 1" arrow.
    """
    kind: str
    target: str
    before: object = None
    after: object = None
    before_known: bool = True
    type_name: str = ""

    @property
    def label(self) -> str:
        """A short, human-facing name: the leaf of a registry path or task."""
        text = str(self.target)
        if "\\" in text:
            return text.rsplit("\\", 1)[-1] or text
        return text


def format_value(value, *, quote: bool = False) -> str:
    """Render a stored value for a card/copy line. Never raises."""
    if value is None:
        return "not set"
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, (bytes, bytearray)):
        raw = bytes(value)
        return "0x" + raw.hex() if raw else "0x"
    if isinstance(value, str):
        return '"%s"' % value if quote else value
    try:
        return str(value)
    except Exception:                                        # pragma: no cover
        return "?"


def change_detail(change: Change) -> str:
    """The right-hand side of a change row: "1 -> 0", "-> disabled"."""
    if change.after is None:
        return "removed"
    after = format_value(change.after, quote=change.kind == KIND_REGISTRY)
    if not change.before_known:
        return "\u2192 " + after
    before = format_value(change.before, quote=change.kind == KIND_REGISTRY)
    return "%s \u2192 %s" % (before, after)


def _same(before, after) -> bool:
    """Value equality that does not let 1 == True or "1" == 1 hide a change."""
    if isinstance(before, bool) or isinstance(after, bool):
        return type(before) is type(after) and before == after
    if isinstance(before, (bytes, bytearray)) and isinstance(after, (bytes, bytearray)):
        return bytes(before) == bytes(after)
    return type(before) is type(after) and before == after


def add_change(ctx, change: Change):
    """Append to `ctx.changes` when the context carries a ledger.

    Contexts built by tests or by the install path may lack the list; recording
    is best-effort and must never turn a successful write into a failure.
    """
    try:
        bucket = getattr(ctx, "changes", None)
    except Exception:                                        # pragma: no cover
        return None
    if not isinstance(bucket, list):
        return None
    try:
        bucket.append(change)
    except Exception:                                        # pragma: no cover
        return None
    return change


def record_registry(ctx, hive: str, path: str, name: str, before, after,
                    *, before_known: bool = True, type_name: str = ""):
    """Record one registry write/delete. No-op writes are not recorded."""
    if before_known and _same(before, after):
        return None
    target = "\\".join(part for part in (hive, path, name) if part)
    return add_change(ctx, Change(KIND_REGISTRY, target, before, after,
                                  before_known=before_known, type_name=type_name))


def _argv(command):
    if isinstance(command, (list, tuple)):
        return [str(part) for part in command if str(part) != ""]
    text = str(command).strip()
    return text.split() if text else []


def _exe(argv) -> str:
    head = argv[0].lower().replace("/", "\\")
    if "\\" in head:
        head = head.rsplit("\\", 1)[-1]
    return head[:-4] if head.endswith(".exe") else head


def _classify_sc(argv):
    verb = argv[1].lower() if len(argv) > 1 else ""
    if verb != "config" or len(argv) < 3:
        return None
    target = argv[2]
    for i, part in enumerate(argv):
        if part.lower() == "start=" and i + 1 < len(argv):
            return Change(KIND_SERVICE, target, after=argv[i + 1],
                          before_known=False)
    return None


def _classify_schtasks(argv):
    flags = {part.lower() for part in argv[1:]}
    target = None
    for i, part in enumerate(argv):
        if part.lower() in ("/tn", "-tn") and i + 1 < len(argv):
            target = argv[i + 1]
            break
    if target is None:
        return None
    if "/disable" in flags or "-disable" in flags:
        after = "disabled"
    elif "/enable" in flags or "-enable" in flags:
        after = "enabled"
    elif "/delete" in flags or "-delete" in flags:
        after = None
    elif "/create" in flags or "-create" in flags:
        after = "created"
    else:
        return None
    return Change(KIND_SCHEDULED_TASK, target, after=after, before_known=False)


def _classify_powercfg(argv):
    if len(argv) < 2:
        return None
    flag = argv[1].lower().replace("-", "/")
    if flag in ("/setacvalueindex", "/setdcvalueindex") and len(argv) >= 5:
        target = "%s\\%s" % (argv[3], argv[4])
        return Change(KIND_POWER, target, after=argv[-1], before_known=False)
    if flag == "/change" and len(argv) >= 4:
        return Change(KIND_POWER, argv[2], after=argv[3], before_known=False)
    if flag == "/setactive" and len(argv) >= 3:
        return Change(KIND_POWER, "active plan", after=argv[2],
                      before_known=False)
    if flag == "/duplicatescheme" and len(argv) >= 3:
        return Change(KIND_POWER, "duplicate plan", after=argv[2],
                      before_known=False)
    return None


def _classify_bcdedit(argv):
    if len(argv) < 3:
        return None
    flag = argv[1].lower().replace("-", "/")
    if flag == "/deletevalue":
        return Change(KIND_BOOT, argv[2], after=None, before_known=False)
    if flag != "/set":
        return None
    if argv[2].startswith("{") and len(argv) >= 4:
        target, value = argv[3], (argv[4] if len(argv) >= 5 else "on")
    else:
        target, value = argv[2], (argv[3] if len(argv) >= 4 else "on")
    return Change(KIND_BOOT, target, after=value, before_known=False)


def classify_command(command, *, ok: bool = True):
    """The Change a successful command represents, or None.

    None for a failure, an unrecognised tool, and the volatile `net start` /
    `net stop` pair (see the module docstring).
    """
    if not ok:
        return None
    argv = _argv(command)
    if not argv:
        return None
    exe = _exe(argv)
    try:
        if exe == "sc":
            return _classify_sc(argv)
        if exe == "schtasks":
            return _classify_schtasks(argv)
        if exe == "powercfg":
            return _classify_powercfg(argv)
        if exe == "bcdedit":
            return _classify_bcdedit(argv)
    except Exception:                                        # pragma: no cover
        return None
    return None


def record_command(ctx, command, *, ok: bool = True):
    change = classify_command(command, ok=ok)
    return add_change(ctx, change) if change is not None else None


def collapse(changes):
    """One Change per (kind, target), in first-seen order, without net no-ops.

    A run can touch the same value from two tasks (a baseline pass and a
    narrower tweak). The card should say what the run did, not replay every
    write: keep the FIRST prior (the run's real starting point) and the LAST
    target (where it ended up), and drop the row when those are equal.
    """
    merged = {}
    order = []
    for change in changes or ():
        if not isinstance(change, Change):
            continue
        key = (change.kind, change.target)
        if key in merged:
            first = merged[key]
            merged[key] = Change(change.kind, change.target, first.before,
                                 change.after, first.before_known,
                                 first.type_name)
        else:
            merged[key] = change
            order.append(key)
    out = []
    for key in order:
        change = merged[key]
        if change.before_known and _same(change.before, change.after):
            continue
        out.append(change)
    return out


def group_counts(changes) -> "list[tuple[str, int]]":
    """(kind, count) in display order, skipping kinds with no changes."""
    counts = {}
    for change in changes or ():
        if isinstance(change, Change):
            counts[change.kind] = counts.get(change.kind, 0) + 1
    return [(kind, counts[kind]) for kind in KIND_ORDER if counts.get(kind)]


def counts_text(changes) -> str:
    """"12 registry values \u00b7 2 services" for the card's subtitle line."""
    return " \u00b7 ".join("%d %s" % (n, kind_noun(kind, n))
                           for kind, n in group_counts(changes))
