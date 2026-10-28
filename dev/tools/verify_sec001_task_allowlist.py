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
    (a build-order bug in the normalisation step);
  * DECLARATION DRIFT — an entry in `_TASK_REG_ALLOWLIST_NOT_A_TASK` names an
    id the allowlist no longer has, or states no reason. That table is where an
    allowlist id that is deliberately NOT a Tweak task key says WHY it exists;
    the report prints every one of them, every run.

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

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from app.tasks import tweak_tasks as T  # noqa: E402
import app.tweak_introspect as _ti  # noqa: E402
from app.task_keys import CUT_TASK_KEYS  # noqa: E402
# Shared AST helpers — the SAME code the Tweak "Preview changes" feature uses
# (app/tweak_introspect.py), so the guard and the preview cannot disagree about
# what a task writes. Kept under their original private names for the tests.
from app.tweak_introspect import (  # noqa: E402
    const_index as _const_index,            # noqa: F401  (re-exported: tests
    literal_specs as _literal_specs,        # noqa: F401   read the AST helpers
    merged_index as _merged_index,          #             off this module)
    resolve_specs_node as _resolve_specs_node,  # noqa: F401
    resolve_str as _resolve_str,
    scan_all_reg_specs as _scan_all_reg_specs,
)
from app.utils import _HIVES  # noqa: E402

#: Allowlist ids that are deliberately not Tweak task keys are DECLARED beside
#: the table they belong to (`tweak_common._TASK_REG_ALLOWLIST_NOT_A_TASK`),
#: each with the reason it exists. This module used to guess instead, from the
#: id's spelling: anything ending in "_regs"/"_pmz"/"_specs" was reported as
#: "expected, internal". That is a heuristic doing a security report's job - an
#: entry nobody meant to add, ending in one of those, read as fine. Now every
#: unexplained non-task id is a warning the reviewer has to look at, and a
#: declaration without a reason (or for an id that no longer exists) fails.
_DECLARED = "_TASK_REG_ALLOWLIST_NOT_A_TASK"


def _task_keys():
    try:
        from app.tab_presets import TABS
        return {t.key for t in TABS.get("Tweak", [])}
    except Exception:
        return set()


def _scan_source():
    """task_id -> set of (hive, path, name) specs it snapshots/writes, resolved
    from literal spec lists (and module-level constants) in the source.

    Scans EVERY tweak module (the per-domain split put the functions and the
    `Task(...)` registry in different files) with ONE merged constant index, so
    a spec table declared in `tweak_common.py` still resolves from the domain
    module that uses it. `advanced_tasks.py` snapshots through its
    `_apply_registry` helper, which the shared scanner reads as id-bearing too.
    Uses the same scanner the Tweak preview does (app/tweak_introspect.py) so the
    guard and the preview cannot disagree about what a task touches."""
    return _scan_all_reg_specs(_ti.tweak_source_paths())


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
    """(function, merged consts) for the task, or (None, None).

    The constants are merged across modules: the id and the spec table it names
    are routinely not in the same file since the per-domain split."""
    consts, nodes = _merged_index(trees)
    for tree in trees:
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
    prefix_verified = _verify_prefix_rules(_ti.tweak_source_paths(), failures)

    # 3c. so are the hand-transcribed entries the scanner cannot derive
    manual_verified = _verify_manual_rules(_ti.tweak_source_paths(), found,
                                           failures)

    # 4. coverage / staleness report
    #
    # 4a. the in-place declarations must be well-formed and still current. A
    #     reason is the whole point of declaring an exception, so an empty one
    #     is a failure rather than a cosmetic problem.
    declared = {k: v for k, v in getattr(T, _DECLARED, {}).items()}
    for k, reason in sorted(declared.items()):
        if k not in allow_norm:
            failures.append(
                f"{_DECLARED}[{k!r}] declares an id that is not in the allowlist "
                f"any more - drop the declaration with the entry")
        if not str(reason or "").strip():
            failures.append(f"{_DECLARED}[{k!r}] has no reason; say why this id is "
                            f"not a task key")

    if task_keys:
        for k in sorted(set(allow_norm) - task_keys):
            if k in declared:
                notes.append(f"{k!r} is not a task key: {declared[k]}")
            elif k in found or k in CUT_TASK_KEYS:
                notes.append(f"allowlist id {k!r} is an internal snapshot id / cut "
                             f"task (expected, not a task key)")
            else:
                warnings.append(
                    f"allowlist id {k!r} is neither a Tweak task key nor an id "
                    f"used in the source (possible stale or typo entry) - if it "
                    f"is deliberate, declare it in {_DECLARED}")

    print("SEC-001 allowlist check")
    print(f"  allowlisted tasks        : {len(allow_norm)}")
    print(f"  prefix-ruled tasks       : {len(allow_prefix)}")
    print(f"  prefix entries verified  : {prefix_verified}")
    print(f"  manual entries verified  : {manual_verified}")
    print(f"  tweak task keys          : {len(task_keys)}")
    print(f"  static-spec tasks found  : {len(found)}")
    print(f"  declared non-task ids    : {len(declared)}")
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
