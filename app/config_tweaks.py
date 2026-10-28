"""Tweak undo history: snapshots, the applied registry, and their portable bundle.

Split out of `app.config_persist` (R-006, continued): these writers all own one
region of the config — `tweak_snapshots`, `applied_tweaks` — and nothing else
in that module, which is why they can move without dragging the loader, the
atomic writer, the recovery path or the file lock along with them.

`app.config_persist` re-exports every public name here, so all existing
importers (`from app.config_persist import get_tweak_snapshot`) are unchanged.

Two rules this module keeps visible, because they are the reason the code is
shaped the way it is:

  * every write goes through `update_config`, which holds the config lock
    across load-mutate-save — a read-then-save here would leave a window where
    the undo history does not exist;
  * a snapshot, once taken, is never overwritten by a later apply, because the
    value that matters is the one from BEFORE the tweak ever ran.

The config doors are fetched inside each function, not imported at module
level: `config_persist` imports this module to re-export it, so a top-level
import would cycle. Binding at call time also means swapping `load_config` /
`update_config` (recovery mode, tests) still takes effect here.
"""
from __future__ import annotations

import copy
import json
import time

from app.config_schema import apply_schema


def _cp():
    """`app.config_persist`, resolved at call time (see the module docstring)."""
    from app import config_persist
    return config_persist


def get_tweak_snapshot(task_id: str) -> dict:
    """Return the saved pre-apply values for a tweak task, or {} if none."""
    cfg = _cp().load_config()
    return cfg.get("tweak_snapshots", {}).get(task_id, {})


def save_tweak_snapshot(task_id: str, values: dict) -> None:
    """Save pre-apply values for a tweak task, but only if none exist yet —
    if the tweak is applied again while an unreverted snapshot is already
    on file, keep the ORIGINAL pre-tweak value, not the value from the last
    apply (which would just re-snapshot the tweak's own output)."""
    if not values:
        return

    def _mutate(cfg):
        snapshots = cfg.setdefault("tweak_snapshots", {})
        if task_id in snapshots and snapshots[task_id]:
            return None
        snapshots[task_id] = values

    _cp().update_config(_mutate)


def merge_tweak_snapshot(task_id: str, values: dict) -> None:
    """Merge `values` into an existing snapshot under a SINGLE lock hold.

    Some applies record their snapshot in two stages: the service start type
    first, then the per-scheduled-task priors a moment later. The second write
    must not erase the first, which is why `save_tweak_snapshot` refuses to
    overwrite — but a read-then-clear-then-save sequence does that by hand,
    and it is NOT atomic:

        existing = get_tweak_snapshot(task_id)   # lock acquired + released
        clear_tweak_snapshot(task_id)            # lock acquired + released
        save_tweak_snapshot(task_id, existing)    # lock acquired + released

    Between the clear and the save the snapshot does not exist. A crash, a
    concurrent --auto-clean process, or a second writer in that window
    destroys the task's ENTIRE undo history — the one thing the user cannot
    get back. update_config holds _config_lock across load-mutate-save
    precisely to close that class of hole, so this is one call.

    Fails closed: if the mutation cannot be applied the existing snapshot is
    left exactly as it was.
    """
    if not values:
        return

    def _mutate(cfg):
        snapshots = cfg.setdefault("tweak_snapshots", {})
        existing = snapshots.get(task_id)
        merged = dict(existing) if isinstance(existing, dict) else {}
        merged.update(values)
        snapshots[task_id] = merged

    _cp().update_config(_mutate)


def clear_tweak_snapshot(task_id: str) -> None:
    """Drop a tweak's saved snapshot after a successful revert, so the next
    apply starts capturing fresh again."""
    def _mutate(cfg):
        snapshots = cfg.get("tweak_snapshots", {})
        if task_id in snapshots:
            del snapshots[task_id]

    _cp().update_config(_mutate)


# --------------------------------------------------------------------------- #
# Applied-tweak registry — Phase 2 (#14)
# --------------------------------------------------------------------------- #
# The "Applied" badge must show what is ACTUALLY ACTIVE on this PC, not the
# toggle's on/off state. Two sources imply "applied":
#   * tweak_snapshots — powercfg tweaks snapshot their prior value on apply
#     (only max_performance_gpu / max_cpu_power use this today), and the
#     snapshot is cleared on successful revert;
#   * applied_tweaks — the explicit registry the GUI marks after a tweak
#     run succeeds and clears after its revert succeeds.
# get_tweak_state() merges both in a single config load.

def mark_tweak_applied(task_id: str) -> None:
    def _mutate(cfg):
        applied = cfg.setdefault("applied_tweaks", [])
        if task_id not in applied:
            applied.append(task_id)

    _cp().update_config(_mutate)


def mark_tweak_reverted(task_id: str) -> None:
    def _mutate(cfg):
        applied = cfg.get("applied_tweaks", [])
        if task_id in applied:
            applied.remove(task_id)

    _cp().update_config(_mutate)


def get_tweak_state() -> dict:
    """One-shot read of which tweak ids are currently applied. Returns
    {task_id: True}. Kept as a plain dict (not a set) so callers can cheaply
    re-read after a run; loads config once, not per tweak."""
    cfg = _cp().load_config()
    state = {tid: True for tid in cfg.get("tweak_snapshots", {})}
    state.update({tid: True for tid in cfg.get("applied_tweaks", [])})
    return state


# --------------------------------------------------------------------------- #
# Phase 2 — portable tweak snapshot export/import (backup & move to a new PC)
# --------------------------------------------------------------------------- #
# The undo history — tweak_snapshots + applied_tweaks — is the one thing a user
# cannot get back once it is gone. These two calls make it a file the user can
# keep: on a USB stick, in cloud backup, or carried to a new machine before an
# in-place Windows upgrade wipes the profile.
#
# Import is additive and fail-closed by design:
#   * it NEVER replaces a snapshot already on file for a task id (merge=True,
#     the default) — the value that was true before the tweak is the one Undo
#     needs, and a stale backup must not overwrite a fresher record;
#   * it validates the JSON shape before touching the config, so a truncated or
#     hand-edited file raises ValueError instead of corrupting the registry.

_SNAPSHOT_BUNDLE_VERSION = 1


def export_tweak_snapshots(path: str) -> int:
    """Write {applied_tweaks, tweak_snapshots} to a portable JSON file.

    Reads the config under the lock so a concurrent apply cannot be captured
    half-written. Returns the number of tweaks (snapshot entries) written.
    Propagates OSError (unwritable path) and TypeError (non-serialisable data)
    to the caller — a backup that silently failed is worse than none.
    """
    cp = _cp()
    with cp._config_lock:
        cfg = cp.load_config()  # private copy, read under the lock
        bundle = {
            "version": _SNAPSHOT_BUNDLE_VERSION,
            "exported_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "applied_tweaks": list(cfg.get("applied_tweaks", []) or []),
            "tweak_snapshots": copy.deepcopy(cfg.get("tweak_snapshots", {}) or {}),
        }
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(bundle, fh, indent=2, ensure_ascii=False)
    return len(bundle["tweak_snapshots"])


def import_tweak_snapshots(path: str, merge: bool = True) -> int:
    """Merge a bundle written by export_tweak_snapshots() into this config.

    Returns the number of NEW task ids added. Raises ValueError on a malformed
    or oversized file (nothing is written in that case).

    H-008: a well-formed JSON bundle can still carry snapshots the registry
    would not accept. Those are filtered through the schema's own coercer and
    reported in the security event log, so an import can never put a shape on
    disk that `_tweak_snapshots` would silently drop on the next load - and
    never badges a task whose snapshot was not understood.
    """
    cp = _cp()
    with open(path, "r", encoding="utf-8") as fh:
        raw = fh.read(cp._MAX_CONFIG_BYTES + 1)
    if len(raw) > cp._MAX_CONFIG_BYTES:
        raise ValueError("Snapshot file is too large to import.")
    try:
        data = json.loads(raw)
    except Exception as exc:
        raise ValueError(f"Not a valid JSON snapshot file: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError("Snapshot file must contain a JSON object.")
    snaps = data.get("tweak_snapshots", {})
    applied = data.get("applied_tweaks", [])
    if not isinstance(snaps, dict) or not isinstance(applied, list):
        raise ValueError(
            "Snapshot file is missing 'tweak_snapshots' or 'applied_tweaks'.")

    # H-008: the JSON being well-formed is not the same as the snapshots being
    # usable. This docstring used to claim shape validation that did not happen:
    # any dict was deep-copied straight into the live registry, bypassing every
    # rule the schema enforces on load (spec shape, hive/path/NUL checks, value
    # types, 256-task / 64-spec caps). The restore path defends itself, but a
    # junk import still badges ids as "applied" and can bloat the file, so the
    # bundle goes through the schema's own coercer here as well - one rulebook.
    validated = apply_schema({"tweak_snapshots": snaps},
                             {"tweak_snapshots": {}})["tweak_snapshots"]
    rejected = sorted(str(tid) for tid in snaps if tid not in validated)

    added = 0

    def _mutate(cfg):
        nonlocal added
        cur = cfg.setdefault("tweak_snapshots", {})
        for tid, val in validated.items():
            if not isinstance(tid, str) or not isinstance(val, dict):
                continue
            if merge and cur.get(tid):
                continue  # keep the fresher local record
            cur[tid] = copy.deepcopy(val)
            added += 1
        app = cfg.setdefault("applied_tweaks", [])
        for tid in applied[:512]:
            if isinstance(tid, str) and tid.strip() and len(tid) <= 120 \
                    and tid not in app:
                app.append(tid)

    cp.update_config(_mutate)
    if rejected:
        # The caller shows a count; this keeps the ids themselves somewhere a
        # support bundle can carry, without a second return value. Logged
        # through config_persist's binding on purpose: that is the seam the
        # recovery path and the tests already swap.
        cp.log_security_event(
            "snapshot_import_rejected",
            "Ignored %d snapshot(s) whose shape the registry would not accept: %s"
            % (len(rejected), ", ".join(rejected[:8])
               + (" ..." if len(rejected) > 8 else "")))
    return added
