#!/usr/bin/env python3
"""Drift guard for the SEC-001 per-task registry allowlist.

Why this exists
---------------
`_TASK_REG_ALLOWLIST` (+ `_TASK_REG_ALLOWLIST_MANUAL`, + the prefix table) is
the primary control that stops a tampered `config.json` from redirecting a
tweak's snapshot restore into an arbitrary registry key. It is only as good as
its agreement with the code: if an apply function starts writing a new
(hive, path, name) that the allowlist does not name, the allowlist silently
stops covering that task.

This script exits non-zero when that agreement breaks:

  * DRIFT — a LITERAL spec passed to `_snap_reg_values` / `_regn_apply` for a
    task that IS allowlisted is missing from that task's allowlist entry;
  * PREFIX DRIFT — a prefix rule's task writes a value name the rule does not
    cover, names a path/hive its apply function never mentions, or declares a
    name the function no longer writes;
  * MANUAL DRIFT — a hand-transcribed entry names a location the owning apply
    function never mentions (it would authorise more than the task writes);
  * a table entry is malformed (unknown hive, empty path, wrong arity);
  * `_TASK_REG_ALLOWED_NORM` does not match a fresh derivation from the tables
    (a build-order bug in the normalisation step).

It also prints a coverage report (allowlisted vs total tweak tasks) so the
remaining gap stays measured instead of drifting quietly.

Prefix rules are verified by SOURCE, not by hand. A task whose registry path is
built at run time (one subkey per network adapter, say) cannot be re-derived as
a literal triple, so it is declared in `_TASK_REG_ALLOW_PREFIX` instead. That
used to be the end of the guard's reach - the entry was "verified by hand" and
anything the apply function later started writing under that prefix went
unchecked. `_verify_prefix_rules` closes it: it locates the apply function that
feeds the task id to `_regn_apply`, requires the rule's path and hive to appear
as literals there, and requires the set of value names the function iterates to
match the set of names the rule declares - EXACTLY. A new name added to the
write loop without a matching rule entry is a hard failure, because that is the
name a tampered snapshot could otherwise redirect.

Scope limit (honest): a spec list is statically checkable only when it is built
from literals and module-level constants (the shared evaluator follows `+`, `%`
formatting, `NAME[i]` subscripts and comprehensions over constant lists). A list
assembled from function-local or OS-dependent values (`taskbar_cleanup`) cannot
be re-derived here; that one lives in `_TASK_REG_ALLOWLIST_MANUAL`. If a prefix
rule's function stops iterating a literal tuple of names, this guard FAILS
rather than skipping - fail-closed, so a refactor cannot silently reopen the
hole.
"""
from __future__ import annotations

import ast
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.tasks import tweak_tasks as T  # noqa: E402
from app.tasks import advanced_tasks as A  # noqa: E402
from app.task_keys import CUT_TASK_KEYS  # noqa: E402
# Shared AST helpers — the SAME code the Tweak "Preview changes" feature uses
# (app/tweak_introspect.py), so the guard and the preview cannot disagree about
# what a task writes. Kept under their original private names for the tests.
from app.tweak_introspect import (  # noqa: E402
    const_index as _const_index,
    literal_specs as _literal_specs,
    resolve_specs_node as _resolve_specs_node,
    resolve_str as _resolve_str,
    scan_reg_specs as _scan_reg_specs,
)
from app.utils import _HIVES  # noqa: E402

#: snapshot/write ids the code uses that are deliberately NOT Tweak task keys
#: (cut tasks kept defensively, and per-feature sub-snapshots). Used only to
#: keep the report readable; the scan itself decides what "expected" means.
_INTERNAL_ID_SUFFIXES = ("_regs", "_pmz", "_specs")


def _task_keys():
    try:
        from app.tab_presets import TABS
        return {t.key for t in TABS.get("Tweak", [])}
    except Exception:
        return set()


def _scan_source():
    """task_id -> set of (hive, path, name) specs it snapshots/writes, resolved
    from literal spec lists (and module-level constants) in the source.

    Scans BOTH task modules: `tweak_tasks.py` (via `_snap_reg_values` /
    `_regn_apply`) and `advanced_tasks.py` (which snapshots through its
    `_apply_registry` helper — the shared scanner reads that as id-bearing too).
    Uses the same scanner the Tweak preview does (app/tweak_introspect.py) so the
    guard and the preview cannot disagree about what a task touches."""
    merged = {}
    for path in (T.__file__, A.__file__):
        for tid, specs in _scan_reg_specs(path).items():
            merged.setdefault(tid, set()).update(specs)
    return merged


def _call_name(node):
    """The bare (last) name of a call target, or None."""
    fn = getattr(node, "func", None)
    return getattr(fn, "id", None) or getattr(fn, "attr", None)


#: calls whose 2nd argument is a task/snapshot id (kept in step with
#: app.tweak_introspect._ID_CALLS, the shared scanner's list).
_ID_CALLS = ("_snap_reg_values", "_regn_apply", "_apply_registry")


def _load_trees(paths, failures):
    """Parse each source path once; a parse failure is itself a failure."""
    trees = []
    for path in paths:
        try:
            with open(path, "r", encoding="utf-8") as fh:
                trees.append(ast.parse(fh.read(), filename=path))
        except Exception as exc:                                  # pragma: no cover
            failures.append(f"cannot parse {path!r}: {exc}")
    return trees


def _apply_fn_for_id(tree, tid, consts, nodes):
    """The function that snapshots or writes `tid`, or None."""
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for sub in ast.walk(node):
            if not isinstance(sub, ast.Call) or _call_name(sub) not in _ID_CALLS:
                continue
            if len(sub.args) < 2:
                continue
            if _resolve_str(sub.args[1], consts, nodes) == tid:
                return node
    return None


def _find_fn(trees, tid):
    """(function, its module consts) for the task, or (None, None)."""
    for tree in trees:
        consts, nodes = _const_index(tree)
        fn = _apply_fn_for_id(tree, tid, consts, nodes)
        if fn is not None:
            return fn, consts
    return None, None


def _prefix_facts(fn):
    """(string literals in `fn`, literal string tuples `fn` iterates over).

    A prefix-ruled write is built as
        for name in ("TcpAckFrequency", "TCPNoDelay"):
            writes.append(("HKLM", "%s\\%s" % (base, sub), name, 1))
    so the value names are exactly the literal tuple that loop iterates, and the
    path base is a string literal somewhere in the function."""
    lits = set()
    loops = []
    for sub in ast.walk(fn):
        if isinstance(sub, ast.Constant) and isinstance(sub.value, str):
            lits.add(sub.value)
        if isinstance(sub, ast.For) and isinstance(sub.iter, (ast.Tuple, ast.List)):
            vals = [e.value for e in sub.iter.elts
                    if isinstance(e, ast.Constant) and isinstance(e.value, str)]
            if vals and len(vals) == len(sub.iter.elts):
                loops.append(set(vals))
    return lits, loops


def _verify_prefix_rules(paths, failures) -> int:
    """Check every PREFIX rule against the source; return the entries verified.

    Prefix-ruled tasks used to be skipped (`continue`), so the rule was
    "verified by hand" and a new value name added to the write loop went
    unnoticed — that name is exactly what a tampered snapshot could redirect.
    This fails CLOSED: if the apply function cannot be found, or no longer
    iterates a literal tuple of value names, that is a failure, not a skip."""
    trees = _load_trees(paths, failures)

    verified = 0
    for tid, specs in sorted(T._TASK_REG_ALLOW_PREFIX.items()):
        fn, _consts = _find_fn(trees, tid)
        if fn is None:
            failures.append(
                f"PREFIX DRIFT: task {tid!r} is prefix-ruled but no apply "
                f"function passes that id to _regn_apply")
            continue

        lits, loops = _prefix_facts(fn)
        if not loops:
            failures.append(
                f"PREFIX DRIFT: task {tid!r}: its apply function no longer "
                f"iterates a literal tuple of value names, so the prefix rule "
                f"cannot be verified — update this guard and the rule together")
            continue

        written = set()
        for names in loops:
            written |= names
        declared = set()
        for spec in specs:
            hive, path, name = spec[0], spec[1], spec[2]
            if path not in lits:
                failures.append(
                    f"PREFIX DRIFT: {tid!r} rule names path {path!r} but "
                    f"{fn.name}() never mentions it")
            if hive not in lits:
                failures.append(
                    f"PREFIX DRIFT: {tid!r} rule names hive {hive!r} but "
                    f"{fn.name}() never mentions it")
            declared.add(name)
            verified += 1
        for name in sorted(written - declared):
            failures.append(
                f"PREFIX DRIFT: {fn.name}() writes value {name!r} under a "
                f"run-time path but the {tid!r} prefix rule does not cover it "
                f"— a tampered snapshot could redirect that name")
        for name in sorted(declared - written):
            failures.append(
                f"PREFIX DRIFT: the {tid!r} prefix rule declares value {name!r} "
                f"that {fn.name}() no longer iterates (stale rule)")
    return verified


def _manual_literals(fn, consts):
    """Every string the function could build a spec from: its own literals, its
    local `name = "..."` assignments, and the module's string constants."""
    lits = {v for v in consts.values() if isinstance(v, str)}
    for node in ast.walk(fn):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            lits.add(node.value)
        elif isinstance(node, ast.Assign):
            v = node.value
            if isinstance(v, ast.Constant) and isinstance(v.value, str):
                lits.add(v.value)
    return lits


def _verify_manual_rules(paths, derived, failures) -> int:
    """Check the hand-written entries the scanner cannot derive.

    `taskbar_cleanup` is version-FILTERED: it builds its write list in the
    function and snapshots a subset of it, so the shared scanner sees no
    literal spec list for it and never reports it (the guard's normal drift
    check therefore cannot fire). The entry is transcribed by hand — which is
    the whole reason this repository keeps a guard — so it is checked here:
    every declared (hive, path, name) must still appear as a literal in the
    owning apply function.

    That is the security-relevant direction. An entry granting a location the
    task does not write is what a tampered snapshot could exploit, so an entry
    that has drifted away from the code must fail, not be trusted.

    Entries the scanner CAN derive (`shader_cache_10gb`, `ssd_last_access`,
    `nvidia_telemetry_regs` go through `_regn_apply` with literal lists) are
    skipped here: the normal drift check already compares them exactly."""
    trees = _load_trees(paths, failures)
    verified = 0
    for tid, specs in sorted(T._TASK_REG_ALLOWLIST_MANUAL.items()):
        if tid in derived:
            continue
        fn, consts = _find_fn(trees, tid)
        if fn is None:
            failures.append(
                f"MANUAL DRIFT: task {tid!r} is hand-allowlisted but no apply "
                f"function snapshots or writes that id")
            continue
        lits = _manual_literals(fn, consts)
        for spec in specs:
            hive, path, name = spec[0], spec[1], spec[2]
            missing = [label for label, value in
                       (("hive", hive), ("path", path), ("name", name))
                       if value not in lits]
            if missing:
                failures.append(
                    f"MANUAL DRIFT: the {tid!r} entry names {'/'.join(missing)} "
                    f"of ({hive!r}, {path!r}, {name!r}) that {fn.name}() never "
                    f"mentions — the entry authorises more than the task writes")
            else:
                verified += 1
    return verified


def main() -> int:
    failures = []
    warnings = []
    notes = []

    task_keys = _task_keys()
    allow_norm = T._TASK_REG_ALLOWED_NORM
    allow_prefix = T._TASK_REG_ALLOW_PREFIX
    norm = T._norm_reg_spec

    # 1. structural validity of every table entry
    tables = {
        "_TASK_REG_ALLOWLIST": T._TASK_REG_ALLOWLIST,
        "_TASK_REG_ALLOWLIST_MANUAL": T._TASK_REG_ALLOWLIST_MANUAL,
        "_TASK_REG_ALLOW_PREFIX": T._TASK_REG_ALLOW_PREFIX,
    }
    for tname, table in tables.items():
        for tid, specs in table.items():
            for spec in specs:
                if len(spec) < 3:
                    failures.append(
                        f"{tname}[{tid!r}]: spec {spec!r} has fewer than 3 elements")
                    continue
                hive, path, name = spec[0], spec[1], spec[2]
                if hive not in _HIVES:
                    failures.append(f"{tname}[{tid!r}]: unknown hive {hive!r}")
                if not isinstance(path, str) or not path:
                    failures.append(f"{tname}[{tid!r}]: bad path {path!r}")
                if not isinstance(name, str):
                    failures.append(f"{tname}[{tid!r}]: bad name {name!r}")

    # 2. the pre-normalised cache agrees with the tables (build-order bug check)
    recomputed = {}
    for tid, specs in T._TASK_REG_ALLOWLIST.items():
        recomputed[tid] = frozenset(norm(*s) for s in specs)
    for tid, specs in T._TASK_REG_ALLOWLIST_MANUAL.items():
        recomputed[tid] = frozenset(norm(*s) for s in specs)
    if recomputed != allow_norm:
        failures.append("_TASK_REG_ALLOWED_NORM does not match a fresh derivation "
                        "from _TASK_REG_ALLOWLIST/_TASK_REG_ALLOWLIST_MANUAL")

    # 3. static drift: literal specs for an allowlisted task must be allowlisted
    found = _scan_source()
    for tid, specs in sorted(found.items()):
        if tid in allow_norm:
            allowed = allow_norm[tid]
            for spec in sorted(specs):
                if norm(*spec) not in allowed:
                    failures.append(
                        f"DRIFT: task {tid!r} writes {spec!r} but its allowlist "
                        f"entry does not cover it")
        elif tid in allow_prefix:
            # A prefix-ruled task's specs are resolved at run time, so the
            # scanner cannot derive them and never reaches this branch with a
            # non-empty spec set. Their rules are checked separately below.
            continue
        else:
            warnings.append(f"task {tid!r} has literal specs but no allowlist entry "
                            f"(denylist-only)")

    # 3b. prefix rules are checked against the source, not trusted by hand
    prefix_verified = _verify_prefix_rules((T.__file__, A.__file__), failures)

    # 3c. so are the hand-transcribed entries the scanner cannot derive
    manual_verified = _verify_manual_rules((T.__file__, A.__file__), found,
                                           failures)

    # 4. coverage / staleness report
    if task_keys:
        for k in sorted(set(allow_norm) - task_keys):
            if k in found or k in CUT_TASK_KEYS or k.endswith(_INTERNAL_ID_SUFFIXES):
                notes.append(f"allowlist id {k!r} is an internal snapshot id / cut "
                             f"task (expected, not a task key)")
            else:
                warnings.append(
                    f"allowlist id {k!r} is neither a Tweak task key nor an id "
                    f"used in the source (possible stale or typo entry)")

    print("SEC-001 allowlist check")
    print(f"  allowlisted tasks        : {len(allow_norm)}")
    print(f"  prefix-ruled tasks       : {len(allow_prefix)}")
    print(f"  prefix entries verified  : {prefix_verified}")
    print(f"  manual entries verified  : {manual_verified}")
    print(f"  tweak task keys          : {len(task_keys)}")
    print(f"  static-spec tasks found  : {len(found)}")
    if notes:
        print("\nNotes (expected, informational):")
        for n in notes:
            print(f"  - {n}")
    if warnings:
        print("\nWarnings (review):")
        for w in warnings:
            print(f"  - {w}")
    if failures:
        print("\nFAIL:")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("\nOK: allowlist is structurally valid and has no static drift.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
