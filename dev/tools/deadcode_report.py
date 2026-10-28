#!/usr/bin/env python3
"""Report defs under `app/` that nothing in the repo refers to.

The next class of dead code after an orphaned module (`AUDIT-2026-10-HONESTY`,
Stage 5): a function or method whose last caller was deleted with a screen, a
button or a feature, left behind because nothing else noticed. `app/smartcare.py`
was the module-sized version of this; `level_meter.py` was the widget-sized one.

Run it by hand for the whole listing:

    python dev/tools/deadcode_report.py

It is also a GATE: `dev/tests/test_deadcode_report.py` runs the same reading in the
suite and fails when a bucket that means "dead" stops being empty, so a def or a
constant orphaned by a later change is caught by the suite rather than by a
reader. That test also checks the exemption table in both directions - every key
must name a real, still-unread constant and carry its reason - because an
unchecked hatch is how a reading that says zero stops meaning zero.

How it decides
--------------
It reads source (never imports: importing a dialog here opens windows and
shells out) and collects, for every file, the identifiers that are actually
USED -    `ast.Name`, attribute access, function arguments, import aliases, keyword
arguments, and the text of every string literal, because this app names methods
in strings (the routes table, dialog registries, sensor tables). Definitions do
not count as uses. Only Python files count as callers: a name that survives in a
README, an audit or a JSON config is not used by anything.

A def is then:

    NOWHERE      its name appears in no file at all
    TEST-ONLY    no file under app/ uses it; tests do
    PROTOCOL     the same name is defined in two or more places (an override or
                 a widget API), so a missing call site means nothing

The same reading runs one level up, over module-level assignments, because
deleting a def routinely orphans the constant it was the last reader of. Only
LOADS count as uses, so `X = ...` never counts as a reader of `X`; a name with a
written reason to exist without one lives in `_EXEMPT_CONSTANTS`.

It cannot see a name that is assembled at runtime. This codebase composes
attribute names in exactly four places - `single_instance`, `admingateframe`,
`scroll` and `tasktab` - and all four address widget attributes, never methods.
Re-check those four (and any new `getattr(x, some_name)`) before deleting.

Delete in dependency order: removing a dead wrapper usually exposes the helper
it was the only caller of, so run this again afterwards.
"""
from __future__ import annotations

import ast
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_SKIP_DIRS = {".git", "__pycache__", ".pytest_cache", ".ruff_cache", "artifacts",
              "build", "dist", "node_modules"}

_TOKEN = re.compile(r"[A-Za-z_][A-Za-z_0-9]*")

#: Names the stdlib calls for us, so a missing call site is expected: a report
#: walker's HTMLParser hooks and the IO protocol on a file-like object.
_PROTOCOL = {
    "handle_starttag", "handle_endtag", "handle_data", "handle_comment",
    "handle_decl", "handle_pi", "unknown_decl", "error",
    "read", "readline", "readlines", "seek", "tell", "seekable", "readable",
    "writable", "fileno", "flush", "close", "write",
}

#: Only Python files carry call sites. A name quoted in a README, an audit or a
#: config file is NOT a caller: prose remembering a helper is exactly the excuse
#: this report exists to surface, so prose never suppresses a finding.
_USE_SUFFIXES = (".py",)

#: "file:NAME" -> why it is allowed to have no reader. Keep it short and keep it
#: rare: the honest response to a CONSTANTS finding is to delete the name or
#: wire it, and an entry here is a written promise that the exception is
#: deliberate (the same discipline as the module guard's _EXEMPT). The gate
#: empties this table and demands every name reappear as a finding, so an entry
#: that stops being unread - or that drifts to another file - fails.
_EXEMPT_CONSTANTS = {
    # Vocabulary a designer writes a screen against, not a value a widget reads:
    # the shape scale and the M3 state layers are declared so the next surface
    # picks 4px or 0.10 from the ladder instead of inventing its own. The two
    # SURFACE roles name the two tones the palette keys already answer (a well
    # is the page tone plus a hairline; a selected row is the tonal container).
    "app/ui/theme.py:SHAPE_XS":
        "smallest rung of the documented shape scale",
    "app/ui/theme.py:SURFACE_WELL":
        "names the recessed surface role (page tone + hairline), not a value",
    "app/ui/theme.py:SURFACE_SELECTED":
        "names the selected-row tonal container role",
    "app/ui/theme.py:STATE_FOCUS":
        "M3 state-layer opacity; STATE_HOVER is the one in use today",
    "app/ui/theme.py:STATE_DRAG":
        "M3 state-layer opacity; STATE_HOVER is the one in use today",
    # Not vocabulary: a name with callers the reader cannot see.
    "app/config_persist.py:_get_config_dir":
        "documented legacy alias, kept importable for out-of-tree callers",
}


def _files(suffixes=_USE_SUFFIXES):
    for base, dirs, names in os.walk(ROOT):
        dirs[:] = [d for d in dirs if d not in _SKIP_DIRS]
        for name in names:
            if name.endswith(suffixes):
                path = os.path.join(base, name)
                yield os.path.relpath(path, ROOT).replace(os.sep, "/")


def _read(rel):
    with open(os.path.join(ROOT, rel), encoding="utf-8", errors="replace") as fh:
        return fh.read()


def _defs(rel):
    """[(qualified, plain, lineno, kind)] for the top level of one module."""
    try:
        tree = ast.parse(_read(rel))
    except SyntaxError:
        return []
    out = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            out.append((node.name, node.name, node.lineno, "def"))
        elif isinstance(node, ast.ClassDef):
            for sub in node.body:
                if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    out.append(("%s.%s" % (node.name, sub.name), sub.name,
                                sub.lineno, "method"))
    return out


def _consts(rel):
    """[(name, lineno)] for module-level assignment targets in one module.

    The def-level pass cannot see these, and deleting a def routinely orphans
    the constant it was the last reader of (`NIM_MODIFY` outlived the tooltip
    setter that used it; `CLIPBOARD_COLUMNS` outlived its exporter).
    """
    try:
        tree = ast.parse(_read(rel))
    except SyntaxError:
        return []
    out = []
    for node in tree.body:
        targets = []
        if isinstance(node, ast.Assign):
            targets = node.targets
        elif isinstance(node, ast.AnnAssign):
            targets = [node.target]
        elif isinstance(node, (ast.For, ast.AsyncFor)):
            targets = [node.target]
        for t in targets:
            names = [t] if isinstance(t, ast.Name) else \
                [n for n in getattr(t, "elts", []) if isinstance(n, ast.Name)]
            for n in names:
                out.append((n.id, node.lineno))
    return out


def _used_tokens(rel):
    """Identifiers USED in one file. Definitions are not uses; aliases are."""
    text = _read(rel)
    found = set()
    if not rel.endswith(".py"):
        return set(_TOKEN.findall(text))
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return found
    for node in ast.walk(tree):
        # Only LOADS are uses. `X = ...` and `obj.attr = ...` are definitions
        # (store context), which is what keeps a module-level constant from
        # counting as its own reader.
        if isinstance(node, ast.Name):
            if isinstance(node.ctx, ast.Load):
                found.add(node.id)
        elif isinstance(node, ast.Attribute):
            if isinstance(node.ctx, ast.Load):
                found.add(node.attr)
        elif isinstance(node, ast.arg):
            found.add(node.arg)
        elif isinstance(node, ast.alias):
            found.add(node.name.rpartition(".")[2])
            if node.asname:
                found.add(node.asname)
        elif isinstance(node, ast.keyword) and node.arg:
            found.add(node.arg)
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            found.update(_TOKEN.findall(node.value))
    return found


def report():
    """{"nowhere": [...], "test_only": [...], "protocol": [...]} of tuples."""
    product = [rel for rel in _files() if rel.startswith("app/")]
    tests = [rel for rel in _files() if rel.startswith("dev/tests/")]
    # This file is never a caller. Its exemption table and its _PROTOCOL set are
    # lists of names in a Python string, and the token reader counts a name in a
    # string as a use - so counting it here made every exemption justify itself
    # (`SHAPE_XS` looked read because it is written in the very table that says
    # nobody reads it) and made the protocol names look called. The dev scripts
    # beside it ARE callers: the SEC-001 guard really imports
    # `tweak_introspect.scan_all_reg_specs`.
    _self = os.path.relpath(os.path.abspath(__file__), ROOT).replace(os.sep, "/")
    rest = [rel for rel in _files()
            if not rel.startswith("app/") and not rel.startswith("dev/tests/")
            and rel != _self]

    app_used = {rel: _used_tokens(rel) for rel in product}
    test_used = {rel: _used_tokens(rel) for rel in tests}
    other_used = set()
    for rel in rest:
        other_used |= _used_tokens(rel)

    defs = []
    for rel in product:
        for qual, plain, line, kind in _defs(rel):
            defs.append((rel, qual, plain, line, kind))

    out = {"nowhere": [], "test_only": [], "protocol": [],
           "constants": [], "constants_test_only": []}
    for rel, qual, plain, line, kind in defs:
        if plain.startswith("__") and plain.endswith("__"):
            continue
        if plain in _PROTOCOL:
            continue
        defined_twice = any(p == plain and (r, q) != (rel, qual)
                            for r, q, p, _l, _k in defs)
        in_app = any(plain in toks for toks in app_used.values())
        in_other = plain in other_used
        in_tests = any(plain in toks for toks in test_used.values())
        if in_app or in_other:
            continue
        if defined_twice:
            out["protocol"].append((rel, qual, line))
        elif in_tests:
            out["test_only"].append((rel, qual, line, kind))
        else:
            out["nowhere"].append((rel, qual, line, kind))

    # The same reading one level up: a module-level name (usually a constant)
    # whose only occurrence in the repo is the assignment that defines it.
    exempt = []
    for rel in product:
        for name, line in _consts(rel):
            key = "%s:%s" % (rel, name)
            if name.startswith("__"):
                continue
            if key in _EXEMPT_CONSTANTS:
                exempt.append((rel, name, line, _EXEMPT_CONSTANTS[key]))
                continue
            in_app = any(name in toks for toks in app_used.values())
            in_other = name in other_used
            in_tests = any(name in toks for toks in test_used.values())
            if in_app or in_other:
                continue
            if in_tests:
                out["constants_test_only"].append((rel, name, line))
            else:
                out["constants"].append((rel, name, line))
    out["constants_exempt"] = exempt
    return out, len(defs)


def main(argv=None) -> int:
    found, total = report()
    print("defs under app/: %d" % total)
    for key, title in (("nowhere", "NOWHERE (no Python file mentions the name)"),
                       ("test_only", "TEST-ONLY (no file under app/ uses it; tests do)"),
                       ("protocol", "PROTOCOL (same name defined in 2+ places)"),
                       ("constants", "CONSTANTS (module-level names nothing reads)"),
                       ("constants_test_only",
                        "CONSTANTS read only by tests")):
        rows = sorted(found[key])
        print("\n%s: %d" % (title, len(rows)))
        for row in rows:
            rel, qual, line = row[0], row[1], row[2]
            print("   %s:%d  %s" % (rel, line, qual))
    exempt = sorted(found["constants_exempt"])
    print("\nCONSTANTS kept without a reader (written reason): %d" % len(exempt))
    for rel, name, line, why in exempt:
        print("   %s:%d  %s\n       %s" % (rel, line, name, why))
    return 0


if __name__ == "__main__":
    sys.exit(main())
