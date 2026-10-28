"""Read-only inspection of what tweak tasks will change (roadmap 2.3 / 4.1).

The Tweak tab answers "what will this actually change on my PC?" *before* the
user presses Run. It cannot answer that by running the tasks -- running them is
the thing being previewed -- and tweak tasks write the registry, change services,
edit power plans and schedule tasks.

So this module parses the task source with `ast` and re-derives, per task, the
registry values it snapshots or writes, then reads each value's CURRENT setting
(read-only) so the preview can say `1 -> 0`. The AST half is shared with
`dev/tools/verify_sec001_task_allowlist.py` (the SEC-001 drift guard) so the guard
and the preview can never disagree about what a task touches.

Nothing here ever writes: every registry call is a read. Honest limits, which the
preview reports rather than hides:

  * a target is shown only where the source hard-codes it; a value computed at
    runtime (e.g. `0 if enabled else 1`) is reported as "will be changed";
  * a task that changes no registry value (a service, a power setting, a
    scheduled task) is reported as "changes Windows settings";
  * a task whose KEY PATH is built at run time (roadmap 4.1: one subkey per
    network adapter) cannot have a current value read for it, because the
    subkey may not exist yet. Those tasks are declared in the SEC-001 PREFIX
    table; the preview reports the path and the value names it will write
    under "<each subkey>" instead of inventing a path or staying silent.
"""
from __future__ import annotations

import ast
from typing import Optional

from app.utils import _REG_DENIED, reg_get_value_typed

# --------------------------------------------------------------------------- #
# AST helpers — shared with the SEC-001 allowlist guard (it imports these, so
# the two cannot drift apart on how a spec list is read).
# --------------------------------------------------------------------------- #

def const_index(tree):
    """Map module-level simple assignments: NAME -> string value, and NAME ->
    value AST node (for list/tuple constants referenced by name)."""
    consts, nodes = {}, {}
    for node in tree.body:
        if isinstance(node, ast.Assign):
            value = node.value
            targets = node.targets
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            value = node.value
            targets = [node.target]
        else:
            continue
        for tgt in targets:
            if isinstance(tgt, ast.Name):
                nodes[tgt.id] = value
                if isinstance(value, ast.Constant) and isinstance(value.value, str):
                    consts[tgt.id] = value.value
    return consts, nodes


#: sentinel for "not statically known" — distinct from a legitimate None value.
_UNKNOWN = object()


def eval_value(node, consts=None, nodes=None, local=None, depth=0):
    """Evaluate an expression to a plain Python value, following module-level
    constants and the same operations the interpreter would apply. Returns
    `_UNKNOWN` when the value is not statically known.

    This is the general engine behind `resolve_str` / `literal_specs`. It exists
    so a spec list written as `[w + ("REG_SZ",) for w in _FLAGS]`,
    `_SENSE[0]` or `"...\\%s\\InprocServer32" % CLSID` can be re-derived exactly
    as apply would build it — WITHOUT running the task (roadmap 4.1)."""
    consts = consts or {}
    nodes = nodes or {}
    local = local or {}
    if depth > 12:
        return _UNKNOWN
    if isinstance(node, ast.Constant):
        return node.value
    if isinstance(node, ast.Name):
        if node.id in local:
            return local[node.id]
        if node.id in consts:
            return consts[node.id]
        inner = nodes.get(node.id)
        if inner is not None:
            return eval_value(inner, consts, nodes, local, depth + 1)
        return _UNKNOWN
    if isinstance(node, ast.BinOp):
        left = eval_value(node.left, consts, nodes, local, depth + 1)
        right = eval_value(node.right, consts, nodes, local, depth + 1)
        if left is _UNKNOWN or right is _UNKNOWN:
            return _UNKNOWN
        try:
            if isinstance(node.op, ast.Add):
                return left + right
            if isinstance(node.op, ast.Mod):
                return left % right
        except Exception:
            return _UNKNOWN
        return _UNKNOWN
    if isinstance(node, ast.Subscript):
        base = eval_value(node.value, consts, nodes, local, depth + 1)
        key = eval_value(node.slice, consts, nodes, local, depth + 1)
        if base is _UNKNOWN or key is _UNKNOWN:
            return _UNKNOWN
        try:
            return base[key]
        except Exception:
            return _UNKNOWN
    if isinstance(node, (ast.Tuple, ast.List)):
        vals = []
        for item in node.elts:
            v = eval_value(item, consts, nodes, local, depth + 1)
            if v is _UNKNOWN:
                return _UNKNOWN
            vals.append(v)
        return tuple(vals) if isinstance(node, ast.Tuple) else vals
    # literals the evaluator does not special-case (incl. negative numbers,
    # which parse as UnaryOp(USub, Constant)).
    try:
        return ast.literal_eval(node)
    except Exception:
        return _UNKNOWN


def resolve_str(node, consts, nodes, depth=0):
    """Resolve an AST node to a string constant, following module-level names,
    `a + b` concatenation, `%` formatting and `NAME[i]` subscripts. Returns None
    when not statically known."""
    value = eval_value(node, consts, nodes, depth=depth)
    return value if isinstance(value, str) else None


def resolve_specs_node(node, consts, nodes, depth=0):
    """Follow a Name to its list/tuple literal and return the element NODES.

    List concatenation with `+` is followed too, so a constant built as
    `[...] + [...]` still yields every element (used by `_regn_targets`, which
    needs the value tail of each element, not just its identity)."""
    if depth > 10:
        return None
    if isinstance(node, (ast.List, ast.Tuple)):
        return list(node.elts)
    if isinstance(node, ast.Name):
        inner = nodes.get(node.id)
        if inner is not None:
            return resolve_specs_node(inner, consts, nodes, depth + 1)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left = resolve_specs_node(node.left, consts, nodes, depth + 1)
        right = resolve_specs_node(node.right, consts, nodes, depth + 1)
        if left is not None and right is not None:
            return left + right
    return None


def _eval_listcomp(node, consts, nodes):
    """Evaluate a single-`for`, `if`-free list comprehension over a statically
    known iterable whose element is built from the loop variable, e.g.
    `[w + ("REG_SZ",) for w in _ACCESSIBILITY_FLAGS]`."""
    if len(node.generators) != 1:
        return None
    gen = node.generators[0]
    if gen.ifs or not isinstance(gen.target, ast.Name):
        return None
    iterable = eval_value(gen.iter, consts, nodes)
    if iterable is _UNKNOWN or not isinstance(iterable, (list, tuple)):
        return None
    out = []
    for item in iterable:
        value = eval_value(node.elt, consts, nodes, {gen.target.id: item})
        if value is _UNKNOWN:
            return None
        out.append(value)
    return out


def _eval_specs(node, consts, nodes, depth=0):
    """Evaluate a spec-list node to a list of fully-evaluated elements (each a
    list/tuple), following Names, `+` concatenation and list comprehensions over
    constant lists. Returns None when the list is not statically known."""
    if depth > 10:
        return None
    if isinstance(node, (ast.List, ast.Tuple)):
        out = []
        for e in node.elts:
            value = eval_value(e, consts, nodes)
            if value is _UNKNOWN:
                return None
            out.append(value)
        return out
    if isinstance(node, ast.Name):
        inner = nodes.get(node.id)
        if inner is not None:
            return _eval_specs(inner, consts, nodes, depth + 1)
    if isinstance(node, ast.ListComp):
        return _eval_listcomp(node, consts, nodes)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left = _eval_specs(node.left, consts, nodes, depth + 1)
        right = _eval_specs(node.right, consts, nodes, depth + 1)
        if left is not None and right is not None:
            return left + right
    return None


def literal_specs(node, consts=None, nodes=None, depth=0):
    """Return the (hive, path, name) triples from a spec list, resolving
    module-level constants, `+` concatenation, `%` formatting, `NAME[i]`
    subscripts and comprehensions over constant lists. Returns None if the node
    is not statically resolvable."""
    consts = consts or {}
    nodes = nodes or {}
    elts = _eval_specs(node, consts, nodes, depth)
    if elts is None:
        return None
    out = []
    for e in elts:
        # _regn_apply specs are (hive, path, name, value[, type]) — only the
        # first three are the registry identity; the value/type tail may be any
        # literal. _snap_reg_values specs are the bare 3-tuple.
        if not isinstance(e, (list, tuple)) or len(e) < 3:
            return None
        head = []
        for v in e[:3]:
            if not isinstance(v, str):
                return None
            head.append(v)
        out.append(tuple(head))
    return out or None


def literal_value(node, consts=None, nodes=None, depth=0):
    """Resolve an AST node to a plain Python value (str/int/bool/list) for use
    as a previewed registry target. Returns None when not statically known."""
    if depth > 10:
        return None
    consts = consts or {}
    nodes = nodes or {}
    target = node
    if isinstance(node, ast.Name) and node.id in nodes:
        target = nodes[node.id]
    try:
        return ast.literal_eval(target)
    except (ValueError, SyntaxError, TypeError):
        return None


def _regn_targets(node, consts, nodes):
    """{triple: target-or-None} for the `_regn_apply(ctx, id, writes)` writes
    whose (hive, path, name) are statically known. Per-element: a single dynamic
    entry (e.g. a path built with `%`) skips just that entry, not the call."""
    elts = resolve_specs_node(node.args[2], consts, nodes)
    if not elts:
        return {}
    out = {}
    for e in elts:
        if not isinstance(e, (ast.Tuple, ast.List)) or len(e.elts) < 4:
            continue
        vals, ok = [], True
        for v in e.elts[:3]:
            s = resolve_str(v, consts, nodes)
            if s is None:
                ok = False
                break
            vals.append(s)
        if ok:
            out[tuple(vals)] = literal_value(e.elts[3], consts, nodes)
    return out


# --------------------------------------------------------------------------- #
# Scanning
# --------------------------------------------------------------------------- #

#: calls that carry the task/snapshot id as their 2nd argument. `_apply_registry`
#: (app/tasks/advanced_tasks.py) is the advanced-tab equivalent of `_regn_apply`:
#: `_apply_registry(ctx, id, specs, writes)` snapshots `specs` under `id`.
_ID_CALLS = ("_snap_reg_values", "_regn_apply", "_apply_registry")
#: writer calls (no id argument — attributed via the enclosing apply function)
_WRITE_CALLS = ("reg_set_value", "reg_set_value_checked")
#: value-delete / key-delete calls
_DELETE_CALLS = ("reg_delete_value", "reg_delete_key")
#: calls that change something other than the registry (services, power, tasks)
_SYSTEM_CALLS = ("run_cmd", "run_cmd_checked", "restart_explorer")


def _call_name(node) -> Optional[str]:
    fn = node.func
    return getattr(fn, "id", None) or getattr(fn, "attr", None)


def _apply_fn_keys(tree, consts, nodes):
    """apply-function name -> task key, from `Task("key", label, desc, run, …)`.

    Task's 4th positional field is `run` (see app/tasks/__init__.py), so this
    attributes the id-less `reg_set_value` / `reg_delete_*` calls to a task."""
    out = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and _call_name(node) == "Task":
            args = node.args
            if len(args) < 4:
                continue
            key = resolve_str(args[0], consts, nodes)
            fn = args[3]
            if key and isinstance(fn, ast.Name):
                out.setdefault(fn.id, key)
    return out


def _scan_tree(tree, consts=None, nodes=None, fn_to_key=None):
    """Return (by_id, by_id_targets, by_fn, fn_to_key).

    by_id        : task/snapshot id -> set of (hive, path, name) from
                   _snap_reg_values / _regn_apply (the exact set the SEC-001
                   guard checks).
    by_id_targets: task/snapshot id -> {triple: literal target} for _regn_apply
                   writes whose value is hard-coded.
    by_fn        : apply-function name -> {"regs": {triple: target},
                   "deletes": set(triple), "system": bool}
    fn_to_key    : apply-function name -> task key

    `consts`/`nodes` may be supplied from a cross-module index: since the
    per-domain split (R-002) a function often references a spec constant that
    lives in tweak_common, and a per-file index cannot resolve that name.
    """
    own_consts, own_nodes = const_index(tree)
    if consts is None:
        consts = own_consts
    if nodes is None:
        nodes = own_nodes
    if fn_to_key is None:
        fn_to_key = _apply_fn_keys(tree, consts, nodes)
    by_id = {}
    by_id_targets = {}
    by_fn = {}

    def _triple(call, base):
        vals = []
        for i in base:
            if i >= len(call.args):
                return None
            s = resolve_str(call.args[i], consts, nodes)
            if s is None:
                return None
            vals.append(s)
        return tuple(vals)

    # id-bearing calls (walked globally so nested helpers are still counted)
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = _call_name(node)
        if name not in _ID_CALLS or len(node.args) < 3:
            continue
        tid = resolve_str(node.args[1], consts, nodes)
        if not tid:
            continue
        specs = literal_specs(node.args[2], consts, nodes)
        if specs:
            by_id.setdefault(tid, set()).update(specs)
        if name == "_regn_apply":
            for trip, val in _regn_targets(node, consts, nodes).items():
                by_id_targets.setdefault(tid, {})[trip] = val

    # per-function, id-less calls
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        key = fn_to_key.get(node.name)
        if not key:
            continue
        info = by_fn.setdefault(
            key, {"regs": {}, "deletes": set(), "system": False})
        for sub in ast.walk(node):
            if not isinstance(sub, ast.Call):
                continue
            name = _call_name(sub)
            if name in _SYSTEM_CALLS:
                info["system"] = True
            elif name in _WRITE_CALLS:
                trip = _triple(sub, (1, 2, 3))
                if trip is not None:
                    info["regs"].setdefault(
                        trip, literal_value(sub.args[4], consts, nodes)
                        if len(sub.args) > 4 else None)
            elif name in _DELETE_CALLS:
                trip = _triple(sub, (1, 2, 3))
                if trip is not None:
                    info["deletes"].add(trip)

    return by_id, by_id_targets, by_fn, fn_to_key


def merged_index(trees) -> "tuple[dict, dict]":
    """Constants and assignment nodes from every tree, merged.

    A split module imports its spec tables from tweak_common; resolving a name
    against only the file that uses it would silently drop every such spec.
    Names are unique across the tweak modules, so first-one-wins is exact.
    """
    consts, nodes = {}, {}
    for tree in trees:
        c, n = const_index(tree)
        for name, value in c.items():
            consts.setdefault(name, value)
        for name, node in n.items():
            nodes.setdefault(name, node)
    return consts, nodes


def scan_reg_specs(path) -> dict:
    """id -> set of (hive, path, name); the exact shape the SEC-001 guard uses."""
    by_id, _targets, _by_fn, _fn_to_key = _scan_tree(_parse(path))
    return by_id


def scan_all_reg_specs(paths) -> dict:
    """`scan_reg_specs` merged across every module, with one const index.

    This is what the SEC-001 guard calls: after the R-002 split the literal
    specs, the constants they name, and the `Task(...)` entries that tie a
    function to a key all live in different files of the same package.
    """
    trees = [_parse(p) for p in paths]
    consts, nodes = merged_index(trees)
    merged = {}
    for tree in trees:
        by_id, _targets, _by_fn, _fn_to_key = _scan_tree(tree, consts, nodes)
        for tid, specs in by_id.items():
            merged.setdefault(tid, set()).update(specs)
    return merged


def _parse(path):
    with open(path, "r", encoding="utf-8") as fh:
        return ast.parse(fh.read(), filename=path)


def tweak_source_modules():
    """Every module whose source the scanner must read.

    The tweak implementations used to live in one file; they are now split by
    domain (audit 2026-10, R-002), so a literal registry spec can be in any of
    them. `tweak_tasks` itself holds only the registry, but it is included so a
    task declared inline there is still seen.
    """
    from app.tasks import (
        advanced_tasks, tweak_common, tweak_explorer, tweak_gaming,
        tweak_network, tweak_power, tweak_privacy, tweak_system, tweak_tasks,
    )
    return [tweak_tasks, tweak_common, tweak_power, tweak_gaming,
            tweak_network, tweak_privacy, tweak_explorer, tweak_system,
            advanced_tasks]


def tweak_source_paths() -> "list[str]":
    """`tweak_source_modules()` as file paths, for the SEC-001 guard."""
    return [m.__file__ for m in tweak_source_modules()]


def _task_modules():
    return tweak_source_modules()


def prefix_specs() -> dict:
    """id -> set of (hive, path, name) from the SEC-001 PREFIX table.

    A prefix rule covers a registry path whose leaf is resolved at RUN time
    (one subkey per network adapter), so — unlike `_TASK_REG_ALLOWLIST` — no
    exact triple can be derived from the source. The rule is the authoritative
    list of what the task may touch beneath that path, and
    `dev/tools/verify_sec001_task_allowlist.py` checks it against the source, so
    the preview and the security control read the same declaration.

    Imported lazily: this module must stay importable, and parseable, without
    pulling in the task modules (the guard imports both)."""
    try:
        from app.tasks.tweak_tasks import _TASK_REG_ALLOW_PREFIX
        return {k: set(v) for k, v in _TASK_REG_ALLOW_PREFIX.items()}
    except Exception:
        return {}


def scan_tasks() -> dict:
    """key -> {"regs": {triple: target}, "deletes": set, "system": bool, "title":
    str} merged across the tweak + advanced modules. Cached; parsed once."""
    global _CACHE
    if _CACHE is not None:
        return _CACHE

    titles = {}
    info = {}
    fn_to_key = {}
    by_id_all = {}
    by_id_targets_all = {}
    trees = []
    for mod in _task_modules():
        try:
            trees.append(_parse(mod.__file__))
        except Exception:
            continue
    consts, nodes = merged_index(trees)
    # The Task(...) entries live in ONE module (the registry); the functions
    # they name live in the domain modules. Build the function->key map from
    # every tree first, then scan each tree with it.
    for tree in trees:
        fn_to_key.update(_apply_fn_keys(tree, consts, nodes))
    for tree in trees:
        by_id, by_id_targets, by_fn, _f2k = _scan_tree(
            tree, consts, nodes, fn_to_key)
        by_id_all.update(by_id)
        for key, targets in by_id_targets.items():
            by_id_targets_all.setdefault(key, {}).update(targets)
        for key, fninfo in by_fn.items():
            merged = info.setdefault(
                key, {"regs": {}, "deletes": set(), "system": False})
            merged["regs"].update(fninfo["regs"])
            merged["deletes"].update(fninfo["deletes"])
            merged["system"] = merged["system"] or fninfo["system"]
    for mod in _task_modules():
        for t in getattr(mod, "TASKS", []) or []:
            titles[t.key] = getattr(t, "label", None) or t.key

    # fold in the id-bearing snapshot specs (a snapshot touches a value even if
    # the matching write lives in a helper we named differently). The literal
    # target comes from _regn_apply's 4th element when it is hard-coded.
    for key, specs in by_id_all.items():
        merged = info.setdefault(
            key, {"regs": {}, "deletes": set(), "system": False})
        targets = by_id_targets_all.get(key) or {}
        for trip in specs:
            merged["regs"].setdefault(trip, targets.get(trip))

    # Fold in the SEC-001 PREFIX rules. A task whose path is built at run time
    # has no derivable triple, so without this the preview told the user
    # "no registry detail to preview" about a tweak that rewrites an unknown-
    # number of adapter subkeys — the least honest answer available. The rule
    # names the paths and values involved; the exact subkey cannot be known
    # until apply runs, and the preview says so.
    for key, specs in prefix_specs().items():
        merged = info.setdefault(
            key, {"regs": {}, "deletes": set(), "system": False})
        merged.setdefault("prefix", set()).update(specs)

    for key, merged in info.items():
        merged["title"] = titles.get(key, key)
        # a value both written and deleted: the delete is the honest action
        for trip in merged["deletes"]:
            merged["regs"].pop(trip, None)

    _CACHE = info
    return info


_CACHE = None


# --------------------------------------------------------------------------- #
# Read-only current value + preview text
# --------------------------------------------------------------------------- #

class _NullCtx:
    """reg_get_value_typed takes a ctx but never uses it; this is a stand-in so
    the preview does not construct a real TaskContext."""

    def log(self, *_a, **_k):
        pass


_NULL_CTX = _NullCtx()


def read_current(hive, path, name):
    """(value, type_name) for a registry value, or (None, None) / (_REG_DENIED,
    None). Never raises. Read-only."""
    try:
        return reg_get_value_typed(_NULL_CTX, hive, path, name)
    except Exception:
        return None, None


def _fmt_value(v) -> str:
    if v is _REG_DENIED:
        return "unreadable (access denied)"
    if v is None:
        return "(not set)"
    if isinstance(v, bool):
        return "1" if v else "0"
    if isinstance(v, (bytes, bytearray)):
        raw = bytes(v)
        return "0x" + raw.hex() if raw else "0x"
    if isinstance(v, str):
        return '"%s"' % (v if len(v) <= 48 else v[:45] + "...")
    return str(v)


def _fmt_target(v) -> str:
    if v is None:
        return ""
    if isinstance(v, bool):
        return "1" if v else "0"
    if isinstance(v, str):
        return '"%s"' % (v if len(v) <= 48 else v[:45] + "...")
    return str(v)


def _value_line(hive, path, name, target) -> str:
    where = "%s\\%s\\%s" % (hive, path, name)
    cur, _t = read_current(hive, path, name)
    now = _fmt_value(cur)
    if target is None:
        return "  %s: now %s (will be changed)" % (where, now)
    return "  %s: %s -> %s" % (where, now, _fmt_target(target))


def preview_lines(keys) -> list[str]:
    """Human-readable 'what will change' for the given tweak keys, in order.

    Read-only: it reads current registry values but never writes anything. Keys
    with no statically-known detail still get an honest one-line entry, and a
    task whose path is built at run time (a SEC-001 prefix rule) reports the
    path and values it will touch without inventing a current value for a
    subkey that may not exist yet."""
    info = scan_tasks()
    lines = []
    for key in keys:
        entry = info.get(key)
        title = (entry or {}).get("title") or key
        lines.append("%s — %s" % (title, key))
        if not entry:
            lines.append("  changes Windows settings (no registry detail to "
                         "preview)")
            lines.append("")
            continue
        regs = entry.get("regs") or {}
        for trip in sorted(regs):
            lines.append(_value_line(trip[0], trip[1], trip[2], regs[trip]))
        for trip in sorted(entry.get("deletes") or ()):
            cur, _t = read_current(trip[0], trip[1], trip[2])
            lines.append("  %s\\%s\\%s: %s -> (removed)"
                         % (trip[0], trip[1], trip[2], _fmt_value(cur)))
        for hive, ppath, pname in sorted(entry.get("prefix") or ()):
            lines.append("  %s\\%s\\<each subkey>\\%s: will be set (one "
                         "subkey per device)" % (hive, ppath, pname))
        if entry.get("system"):
            lines.append("  also changes Windows settings (services / power / "
                         "scheduled tasks)")
        if (not regs and not entry.get("deletes") and not entry.get("prefix")
                and not entry.get("system")):
            lines.append("  changes Windows settings (no detail to preview)")
        lines.append("")
    if lines and lines[-1] == "":
        lines.pop()
    return lines
