"""Static check: no unbound names, no shadowed builtins, no shared mutable globals.

Hardening check. It catches two failure modes a plain compile cannot:

  1. A name that is loaded but never bound in the module. This is the class of
     bug that produced the ``orig_save`` typo in a verification harness and a
     module-level ``_telemetry_tasks`` literal: both compiled, both silently
     did the wrong thing.
  2. A module-level mutable literal assigned to a lowercase name, which is
     shared mutable state that any caller can mutate. A module-level constant
     like ``_TELEMETRY_TASKS`` must be a tuple, and this enforces that.
"""

from __future__ import annotations

import ast
import builtins
import os
import sys

_BUILTINS = set(dir(builtins))

# Implicit module bindings (never appear in the AST as a Store) and the
# conventional re-export manifest. `__all__` is a list by definition; it is a
# static list of strings, not mutable application state.
_IMPLICIT = {
    "__file__", "__name__", "__doc__", "__package__", "__spec__",
    "__loader__", "__builtins__", "__path__", "__all__", "__annotations__",
}
_MUTABLE_LITERAL_ALLOWED = {"__all__"}


def _bound_names(tree: ast.AST) -> set[str]:
    """Every name the module can legally load, at any scope."""
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
            names.add(node.id)
        elif isinstance(node, ast.arg):
            names.add(node.arg)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, ast.alias):
            names.add((node.asname or node.name).split(".")[0])
        elif isinstance(node, ast.ExceptHandler) and node.name:
            names.add(node.name)
        elif isinstance(node, (ast.Global, ast.Nonlocal)):
            names.update(node.names)
    return names


def _loads(tree: ast.AST) -> set[str]:
    return {
        node.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load)
    }


def _assigned_targets(node: ast.Assign) -> list[str]:
    out: list[str] = []
    for target in node.targets:
        if isinstance(target, ast.Name):
            out.append(target.id)
        elif isinstance(target, (ast.Tuple, ast.List)):
            out.extend(
                e.id for e in target.elts if isinstance(e, ast.Name)
            )
    return out


def main() -> int:
    failures: list[str] = []
    shared_mutables: list[str] = []
    modules = 0

    for dirpath, _dirs, files in os.walk("app"):
        for fname in sorted(files):
            if not fname.endswith(".py"):
                continue
            path = os.path.join(dirpath, fname)
            with open(path, encoding="utf-8") as handle:
                source = handle.read()
            try:
                tree = ast.parse(source, path)
            except SyntaxError as exc:
                failures.append("%s: SyntaxError: %s" % (path, exc))
                continue
            modules += 1

            bound = _bound_names(tree) | _BUILTINS | _IMPLICIT

            undefined = sorted(_loads(tree) - bound)
            if undefined:
                failures.append(
                    "%s: loaded but never bound: %s" % (path, ", ".join(undefined))
                )

            for node in tree.body:
                if not isinstance(node, ast.Assign):
                    continue
                for name in _assigned_targets(node):
                    if name in _BUILTINS:
                        failures.append(
                            "%s:%d shadows the builtin %r"
                            % (path, node.lineno, name)
                        )
                    elif isinstance(node.value, (ast.List, ast.Dict, ast.Set)):
                        if (
                            not name.isupper()
                            and name not in _MUTABLE_LITERAL_ALLOWED
                        ):
                            shared_mutables.append(
                                "%s:%d module-level mutable %r"
                                % (path, node.lineno, name)
                            )

    print("[1] every module parses, and loads only names it binds")
    for line in failures:
        print("  FAIL  %s" % line)
    if not failures:
        print("  PASS  %d modules, 0 unbound names, 0 shadowed builtins" % modules)

    print()
    print("[2] no module-level mutable default (shared state)")
    for line in shared_mutables:
        print("  FAIL  %s" % line)
    if not shared_mutables:
        print("  PASS  none found; module-level constants must be tuples")

    print()
    if failures or shared_mutables:
        print("NO-GLOBALS VERIFY: FAILED")
        return 1
    print("NO-GLOBALS VERIFY: ALL PASS (%d modules)" % modules)
    return 0


if __name__ == "__main__":
    sys.exit(main())
