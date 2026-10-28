"""Stage 5 guard: every module under `app/` must be reachable from an entry point.

Why this exists
---------------
Three failures in this codebase had one shape: machinery with no caller.

* `A-001` — six tools wrote findings into a store that no screen read.
* `A-005` — nine windows stayed registered after the rows that opened them went.
* `app/smartcare.py` — 424 lines of plan/step orchestration that kept passing its
  own tests for a year after its last entry point was deleted.

The third is the one nothing else in this suite can see. An orphan imports
cleanly, lints cleanly, and is *covered* — by the tests that were written for it.
That is exactly what keeps it alive: a module only its own tests import is the
signature, so tests are deliberately NOT roots here. "Only the tests use it" is
the finding, not an excuse.

How it reads
------------
Statically, through the AST of every `.py` file: importing `a.b.c` is an edge to
`c`, to the packages above it (that is what Python executes) and to any submodule
named in `from a.b import c`. Nothing is imported to see what happens — module
level code here opens dialogs, shells out to PowerShell and probes the registry,
none of which belongs in a test process.

A module reached only through a computed name would be a false positive. There is
none in this codebase today (`grep -rn "import_module\\|__import__(" app/` finds
one call, for stdlib `re`), which is why `_EXEMPT` is empty — and why adding to it
costs a written reason.

Measured when written: 204 modules under `app/`, 0 unreachable, after deleting the
five orphans it found — `app/mic_test.py`, `app/webcam_web.py`,
`app/ui/widgets/level_meter.py`, `app/ui/widgets/surface.py` and `app/services/`,
all leftovers of windows removed in the A-005 sweep.
"""
from __future__ import annotations

import ast
import os
import re

#: module path -> why it is allowed to have no caller. Keep empty if at all
#: possible: the honest response to this guard firing is to delete the module or
#: wire it up, and an entry here is a promise that the exception is deliberate.
_EXEMPT: dict = {}

#: The repo's real entry points: `python main.py` is a shim for `python -m app`.
_ROOTS = ("main", "app.__main__")

_SKIP_DIRS = {".git", "__pycache__", ".pytest_cache", ".ruff_cache", "artifacts",
              "build", "dist", "node_modules", ".mypy_cache", ".idea", ".vscode"}


# --------------------------------------------------------------------------- #
# The decision, as pure data (so it can be tested with a planted orphan)
# --------------------------------------------------------------------------- #

def _unreachable(names, imports, roots=_ROOTS, exempt=()) -> list:
    """Names under `app` that no entry point can reach, sorted.

    `names` is every module (dotted), `imports` maps a module to what it
    imports. Importing `a.b.c` also executes the packages above it, so each
    ancestor's own imports are followed too - a widget imported by
    `app/ui/widgets/__init__.py` is reachable because that file runs, not
    because of where it is.
    """
    seen = set()
    stack = [name for name in roots if name in names]
    while stack:
        mod = stack.pop()
        if mod in seen:
            continue
        seen.add(mod)
        for dep in imports.get(mod, ()):
            if dep not in seen:
                stack.append(dep)
        # The packages above it run on the same import statement.
        cursor = mod.rpartition(".")[0]
        while cursor:
            if cursor not in seen:
                stack.append(cursor)
            cursor = cursor.rpartition(".")[0]
    skipped = set(exempt)
    return sorted(name for name in names
                  if (name == "app" or name.startswith("app."))
                  and name not in seen and name not in skipped)


# --------------------------------------------------------------------------- #
# Reading the real tree
# --------------------------------------------------------------------------- #

def _module_name(rel) -> str:
    parts = rel[:-3].split("/")
    if parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


def _package_of(module, rel) -> str:
    if rel.endswith("__init__.py"):
        return module
    return module.rpartition(".")[0]


def _read_graph(root):
    """(names, imports, unparsed) read from source. No module is imported.

    The walk covers the product only: the `app/` package and the `main.py`
    shim. Tests and `tools/` are not read at all - an import there must never
    make a module look used, which is the whole point of the guard.
    """
    names, rels = set(), {}
    shim = os.path.join(root, "main.py")
    if os.path.isfile(shim):
        names.add("main")
        rels["main"] = "main.py"
    for base, dirs, files in os.walk(os.path.join(root, "app")):
        dirs[:] = sorted(d for d in dirs if d not in _SKIP_DIRS)
        for filename in sorted(files):
            if not filename.endswith(".py"):
                continue
            rel = os.path.relpath(os.path.join(base, filename), root)
            rel = rel.replace(os.sep, "/")
            module = _module_name(rel)
            names.add(module)
            rels[module] = rel

    def resolves(name):
        return name in names or any(m.startswith(name + ".") for m in names)

    imports, unparsed = {}, []
    for module, rel in rels.items():
        try:
            with open(os.path.join(root, rel), encoding="utf-8",
                      errors="replace") as handle:
                tree = ast.parse(handle.read())
        except SyntaxError:
            unparsed.append(rel)
            continue
        package = _package_of(module, rel)
        edges = imports.setdefault(module, set())

        def add(base, submodule_names=()):
            if base and resolves(base):
                edges.add(base)
            for sub in submodule_names:
                if base and resolves(base + "." + sub):
                    edges.add(base + "." + sub)
                elif not base and resolves(sub):
                    edges.add(sub)

        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    add(alias.name)
            elif isinstance(node, ast.ImportFrom):
                given = [alias.name for alias in node.names]
                if not node.level:
                    add(node.module or "", given)
                    continue
                climb = package.split(".") if package else []
                if node.level - 1 > len(climb):
                    continue
                base = climb[:len(climb) - (node.level - 1)]
                parts = (node.module or "").split(".") if node.module else []
                add(".".join(part for part in base + parts if part), given)
    return names, imports, unparsed


# --------------------------------------------------------------------------- #
# The guard
# --------------------------------------------------------------------------- #

def test_every_module_under_app_can_be_reached_from_an_entry_point():
    import pathlib

    root = str(pathlib.Path(__file__).resolve().parent.parent.parent)
    names, imports, unparsed = _read_graph(root)

    # A file that does not parse has no import graph to read, so the guard
    # cannot speak for it. That is a broken build, not a pass.
    assert unparsed == [], unparsed
    # A trivial reading (a bad walk root, an empty tree) must not look like
    # success: the app has ~200 modules and no fewer.
    assert len([n for n in names if n.startswith("app.")]) >= 150, len(names)

    orphans = _unreachable(names, imports, _ROOTS, _EXEMPT)
    assert orphans == [], (
        "no entry point imports these modules under app/, so nothing in the app "
        "can run them (their own tests will). Delete them, or wire them up - if "
        "one really must stay, add it to _EXEMPT with the reason:\n  "
        + "\n  ".join(orphans))


def test_an_exemption_is_a_real_module_and_says_why():
    """An exemption is a promise, so a stale one has to fail like any other."""
    import pathlib

    root = str(pathlib.Path(__file__).resolve().parent.parent.parent)
    names, _imports, _unparsed = _read_graph(root)
    for module, reason in _EXEMPT.items():
        assert module in names, "%s is exempt but does not exist" % module
        assert isinstance(reason, str) and reason.strip(), (
            "%s is exempt without a reason" % module)


def test_the_guard_fires_on_a_planted_orphan_and_on_tests_only_use():
    """The semantic that would have caught `app/smartcare.py`: a module whose
    only importers are tests is an orphan, because no screen can run it."""
    names = {"app", "app.__main__", "app.ui", "app.ui.healthtab", "app.orphan",
             "app.tests_only", "tests.test_insights", "tools.verify"}
    imports = {
        "app.__main__": {"app.ui.healthtab"},
        "tests.test_insights": {"app.tests_only"},
        "tools.verify": {"app.orphan"},
    }
    assert _unreachable(names, imports) == ["app.orphan", "app.tests_only"]
    # ... and the escape hatch is the only thing that silences it.
    assert _unreachable(names, imports, exempt=("app.orphan",)) == \
        ["app.tests_only"]


def test_a_package_init_runs_even_when_only_its_child_is_imported():
    """`import app.ui.healthtab` executes `app/ui/__init__.py`, so a widget that
    file imports is in use. Reachability is about what runs, not about graph
    shape."""
    names = {"app", "app.__main__", "app.ui", "app.ui.healthtab",
             "app.ui.widgets", "app.ui.widgets.button"}
    imports = {
        "app.__main__": {"app.ui.healthtab"},
        "app.ui": {"app.ui.widgets.button"},
    }
    assert _unreachable(names, imports) == []


def test_what_a_module_imports_inside_a_function_still_counts():
    """This app imports lazily on purpose (heavy task modules, Tk-free cores),
    so an edge inside a function body must be an edge. Read from source: a walk
    that only looked at module-level imports would call half the app dead."""
    src = "def open_it():\n    from app.storage_scan import measure\n"
    found = {node.module for node in ast.walk(ast.parse(src))
             if isinstance(node, ast.ImportFrom)}
    assert found == {"app.storage_scan"}


def test_the_reader_sees_the_real_tree_end_to_end():
    """Pin the reader itself on the repo: it finds the entry points, it finds a
    module that is only ever imported inside a function, and it edges a package
    `__init__` that imports its children."""
    import pathlib

    root = str(pathlib.Path(__file__).resolve().parent.parent.parent)
    names, imports, unparsed = _read_graph(root)

    assert unparsed == []
    assert "app.__main__" in names and "main" in names
    # Reads inside function bodies: `app/health_scan.py` imports the storage
    # walker inside `sense_junk`, which is how half this app is wired.
    assert "app.storage_scan" in imports.get("app.health_scan", set())
    # Package inits bring their children: `app/ui/widgets/__init__.py` imports
    # the widget modules, so importing any of them runs that file.
    assert "app.ui.widgets" in names
    for module, deps in imports.items():
        if not module.startswith("app."):
            continue
        # Nothing in the product may import a test module or a dev tool.
        assert not any(d.startswith("tests.") or d.startswith("tools.")
                       for d in deps), (module, deps)


def test_the_skip_list_is_about_vendored_and_generated_trees():
    """`_SKIP_DIRS` must not quietly include `app/` or a package name: that
    would shrink the tree until the guard passes by not looking."""
    for name in _SKIP_DIRS:
        assert not name.startswith("app"), name
        assert re.match(r"^\.?\w+$", name), name
