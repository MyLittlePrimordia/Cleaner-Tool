"""Tweak Center — the Tk-free capability layer behind Phase 2's tweak features.

Everything the Undo / backup UI needs, with no Tk and no direct registry
writes. It answers three questions and nothing more:

    * WHICH tweaks are applied?     -> applied_tweak_keys() / applied_tweak_tasks()
    * WHAT will Undo put back?      -> describe_applied() / change_log_text()
    * HOW do I keep or move them?   -> backup_to() / restore_from()

Read-only by design. It never writes the registry itself: an actual Undo is
still run through the existing task runner (`app.run_tasks(..., mode="revert")`),
so this layer only decides WHICH tasks and explains WHAT they will change.

Why a separate module: the same three questions are asked by the Tweak tab, the
Settings tab and (a future) CLI. Keeping the answers here — and off the Tk
thread — means one tested implementation instead of three copies.
"""

from __future__ import annotations

import time

from app.config_persist import (
    export_tweak_snapshots,
    get_tweak_snapshot,
    get_tweak_state,
    import_tweak_snapshots,
)
from app.tab_presets import TABS
from app.utils import _REG_DENIED, TaskContext, reg_get_value_typed

#: A ctx whose callbacks are no-ops, for the read-only registry lookups here.
#: reg_get_value_typed takes a ctx for interface symmetry and does not use it,
#: but passing a real one keeps this honest if that ever changes.
_QUIET_CTX = TaskContext(log=lambda *_a: None, set_status=lambda *_a: None,
                         cancelled=lambda: False)


def _tweak_tasks_by_key() -> dict:
    return {t.key: t for t in TABS.get("Tweak", [])}


def applied_tweak_keys() -> list:
    """Task ids the registry says are currently applied. Never raises."""
    try:
        state = get_tweak_state() or {}
    except Exception:
        return []
    return list(state.keys())


def applied_tweak_tasks() -> list:
    """Applied tweaks that map to a real, revertable Task — exactly the set
    Undo and the health view can act on. An id from an older build (no matching
    task, or a task with no revert) is skipped rather than offered and failing."""
    by_key = _tweak_tasks_by_key()
    out = []
    for key in applied_tweak_keys():
        t = by_key.get(key)
        if t is not None and getattr(t, "revert", None) is not None:
            out.append(t)
    return out


def undo_all_tasks() -> list:
    """Every applied, revertable tweak — the list 'Undo all' runs in revert
    mode. Named for the call site; identical to applied_tweak_tasks()."""
    return applied_tweak_tasks()


def task_for_key(key):
    """The Task for a tweak key, or None. Read-only."""
    return _tweak_tasks_by_key().get(key)


def applied_groups() -> list:
    """The applied, revertable tweaks as ordered groups — the data behind the
    per-row snapshot browser (roadmap 2.1's second half).

    One group per applied AND revertable tweak (exactly the set Undo acts on),
    in the order they were applied:
        {"task": key, "label": str, "rows": [describe_applied rows]}

    `rows` is the describe_applied() detail for that tweak — prior -> current
    per registry value, `changed` flagging a value Windows has since reset — and
    is EMPTY for a tweak whose snapshot carries no registry "specs" (powercfg
    pairs, service start types). Such a tweak still gets a group, so it can
    still be undone; it simply has no value diff to show, which the browser
    states honestly rather than hiding the tweak. Never raises."""
    rows_by_task = {}
    for r in describe_applied():
        rows_by_task.setdefault(r["task"], []).append(r)
    return [
        {"task": t.key,
         "label": getattr(t, "label", t.key),
         "rows": rows_by_task.get(t.key, [])}
        for t in applied_tweak_tasks()
    ]


def _fmt(v) -> str:
    if isinstance(v, dict) and "__bytes_hex__" in v:
        return "0x" + str(v["__bytes_hex__"])
    if v is None:
        return "(not set)"
    return str(v)


def describe_applied() -> list:
    """One row per snapshotted registry value across all applied tweaks.

    Each row: {task, label, hive, path, name, prior, current, changed}.

    `prior` is what was there before the tweak (from the snapshot); `current`
    is the live value read now. A tweak a Windows Update quietly reset shows
    `changed == True` — which is exactly what "Reinforce" fixes and what an
    honest change log should tell the user.

    Snapshots with no registry "specs" (powercfg pairs, NVIDIA start type, GPU
    power) have a different shape and are skipped here — they still revert, they
    just have nothing to diff as a registry value. Never raises."""
    by_key = _tweak_tasks_by_key()
    rows = []
    for key in applied_tweak_keys():
        snap = get_tweak_snapshot(key) or {}
        specs = snap.get("specs")
        if not isinstance(specs, (list, tuple)):
            continue
        task = by_key.get(key)
        label = getattr(task, "label", key)
        for i, spec in enumerate(specs):
            try:
                hive, path, name = spec[0], spec[1], spec[2]
            except Exception:
                continue
            prior_present = bool(snap.get(f"{i}:present"))
            prior_denied = bool(snap.get(f"{i}:denied"))
            prior = snap.get(f"{i}:value")
            try:
                current, _current_type = reg_get_value_typed(
                    _QUIET_CTX, hive, path, name)
            except Exception:
                current = None
            cur_denied = current is _REG_DENIED
            cur_present = cur_denied or current is not None

            if prior_denied:
                prior_disp = "(denied)"
            elif prior_present:
                prior_disp = _fmt(prior)
            else:
                prior_disp = "(not set)"
            cur_disp = "(denied)" if cur_denied else _fmt(current)

            rows.append({
                "task": key,
                "label": label,
                "hive": hive,
                "path": path,
                "name": name,
                "prior": prior_disp,
                "current": cur_disp,
                "changed": bool(prior_disp != cur_disp
                                or prior_present != cur_present),
            })
    return rows


def change_log_text() -> str:
    """A plain-text report of every applied tweak's recorded change, grouped by
    tweak, for the user to save or paste. Never raises."""
    rows = describe_applied()
    lines = [
        "Cleaner Tool — applied tweak change log",
        time.strftime("%Y-%m-%d %H:%M:%S"),
        "",
    ]
    if not rows:
        lines.append("No applied tweaks with a recorded prior value.")
        return "\n".join(lines) + "\n"
    cur_task = None
    for r in rows:
        if r["task"] != cur_task:
            cur_task = r["task"]
            lines.append(f"[{cur_task}] {r['label']}")
        mark = "   <-- currently differs from the snapshot" if r["changed"] else ""
        lines.append(f"    {r['hive']}\\{r['path']}\\{r['name']}: "
                     f"{r['prior']} -> {r['current']}{mark}")
    lines.append("")
    return "\n".join(lines)


def backup_to(path: str) -> int:
    """Save the undo history to `path`. Thin wrapper over config_persist."""
    return export_tweak_snapshots(path)


def restore_from(path: str, merge: bool = True) -> int:
    """Load an undo history written by backup_to(). Thin wrapper."""
    return import_tweak_snapshots(path, merge=merge)
