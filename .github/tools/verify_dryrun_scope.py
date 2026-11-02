#!/usr/bin/env python3
"""CI guard: a task run for a dry-run ESTIMATE must never delete or write.

Why this exists
---------------
Storage Insight, the PC Health Report's junk sensor and the Low-Space nudge all
estimate Clean-tab sizes by CALLING THE REAL TASK FUNCTIONS with
`dry_run=True` (see `app/storage_scan.make_measure_ctx`). That is a good
property - one folder list, no knowledge to drift - and a dangerous one: a task
that forgets to look at `ctx.dry_run` deletes files during what the UI calls a
read-only measurement. H-001 was exactly that bug: a scan reached a service
stop and a Recycle Bin purge.

`SCAN_ALLOWLIST` is the declaration this guard keeps honest. It names the
Clean-tab tasks a measuring pass may invoke, each audited by hand to touch
nothing but dry-aware primitives. A hand audit does not survive a later edit:
add one `os.remove` to an allowlisted task, or add a task with `taskkill` in it
to the allowlist, and nothing in CI notices - until a user runs a scan.

What it checks (both directions)
--------------------------------
1. For every task on `SCAN_ALLOWLIST`, walk the code it can actually reach
   (module-level helpers by name, and helpers reached through an imported
   module alias) and require every MUTATION to sit behind a dry-run check:

       if _dry(ctx): return ...                    # early return
       if <dry>: <dry branch> else: <mutation>     # either branch
       if not <dry>: <mutation>                    # negated test
       if not dry and cond: <mutation>             # a conjunction that needs "wet"
       dry = bool(getattr(ctx, "dry_run", False))
       if dry: ...; continue                       # guard clause + terminate

   A mutation that no check dominates is a finding. The rule is deliberately
   PER FUNCTION: "my caller checks dry_run" is not accepted, because that only
   holds until a second caller appears.

2. Every declared dry-aware primitive (`clean_folder_contents`, `_clean_many`,
   `_clean_files`) is re-analysed with the same rule, so the declaration cannot
   outlive the behaviour it describes. If `clean_folder_contents` ever loses
   its `if not dry:` guard, every allowlisted task deletes during a scan - this
   is the check that says so.

3. Fail closed. An allowlisted key with no Clean-tab task, a task whose `run`
   is not a plain function this tool can read, or a declared primitive that no
   longer exists is a FAILURE, not a skip: an unverifiable promise is not a
   promise.

The one write a measuring pass MAY do is the app's own bookkeeping - the log
line the scan itself shows. That lives in `SELF_WRITE_OWNERS`, one file per
reason, and calls into those files are not followed. Anything else needs an
in-place, printed exception:

    os.remove(stale_lock)      # dry-ok: removes our own <100 byte lock file

Exceptions are printed by every run, so they are visible in CI rather than
silently accumulated, and one without a reason fails.

Scope limit (honest): the walk resolves plain function calls. A mutation hidden
behind `getattr(ctx, "x")()`, a string evaluated at run time, or a callback
handed to another module cannot be seen here; neither can one inside a
`lambda`. Where a task needs one of those, write the dry check in that function
and declare it - the escape hatches are all visible in the report.

Usage:
    python dev/tools/verify_dryrun_scope.py        # exit 1 on drift
    python dev/tools/verify_dryrun_scope.py -v     # per-task site counts
"""
from __future__ import annotations

import ast
import inspect
import io
import sys
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
APP = ROOT / "app"

#: In-place exception marker, e.g. `os.remove(p)   # dry-ok: <why>`.
MARKER = "dry-ok:"

#: The dry-run check spellings this guard understands. A new spelling is taught
#: here (and covered by a test row) - never by weakening the rule.
DRY_HELPERS = ("_dry",)

#: Functions the measuring pass may call and trust to mutate nothing, keyed by
#: (repo-relative path, function name). Each one is re-verified by the same
#: analysis below, so "declared dry-aware" is a claim the guard re-checks on
#: every run rather than a note in a comment.
DRY_AWARE = {
    ("app/delete_safety.py", "clean_folder_contents"):
        "the single destructive funnel - the one place a file is removed",
    ("app/tasks/clean_tasks.py", "_clean_many"):
        "loops folders through clean_folder_contents",
    ("app/tasks/game_tasks.py", "_clean_files"):
        "removes individual files behind its own `if not dry:` guard",
}

#: Modules whose whole job is writing the app's OWN bookkeeping (log and event
#: files under the config directory). A measuring pass may do that - the scan's
#: log is the app's surface, not the user's data - so a call into one of these
#: is not followed. Adding a file here claims everything it writes is the app's
#: own state; the claim is printed by every run.
SELF_WRITE_OWNERS = {
    "app/logsafe.py": "appends the app's own log files, never user data",
    "app/config_events.py": "appends the app's own security/event log",
}

#: The module that owns the allowlist, so a report can point at the line to
#: edit. Read through the module, never re-declared here.
ALLOWLIST_OWNER = ("app/storage_scan.py", "SCAN_ALLOWLIST")


# --------------------------------------------------------------------------- #
# What counts as a mutation
# --------------------------------------------------------------------------- #

#: `module.function` calls that delete, write, or otherwise change the machine.
_ATTR_MUTATIONS = {
    "os": {
        "remove": "deletes a file", "unlink": "deletes a file",
        "rmdir": "removes a directory", "removedirs": "removes a directory",
        "rename": "moves a file", "replace": "moves a file",
        "mkdir": "creates a directory", "makedirs": "creates a directory",
        "truncate": "truncates a file", "chmod": "changes file permissions",
        "symlink": "creates a link", "link": "creates a link",
        "utime": "writes file times",
    },
    "shutil": {
        "rmtree": "removes a directory tree", "move": "moves a file",
        "copy": "writes a file", "copy2": "writes a file",
        "copyfile": "writes a file", "copytree": "writes a directory tree",
        "make_archive": "writes an archive",
    },
    "subprocess": {
        "run": "spawns an external process", "call": "spawns an external process",
        "check_call": "spawns an external process",
        "check_output": "spawns an external process",
        "Popen": "spawns an external process",
    },
}

#: Task-side runners: each one can do anything the command says, so a measuring
#: pass must not reach one without a dry check.
_BARE_MUTATIONS = {
    "run_cmd": "runs an external command",
    "run_cmd_checked": "runs an external command",
    "run_ps": "runs a PowerShell script",
    "run_ps_checked": "runs a PowerShell script",
    "run_capture": "spawns an external process",
    "create_restore_point": "creates a system restore point",
}

#: Bare names that mutate when imported directly (`from os import remove`).
_BARE_FILE_MUTATIONS = {
    "remove": "deletes a file", "unlink": "deletes a file",
    "rmdir": "removes a directory", "removedirs": "removes a directory",
    "rename": "moves a file", "replace": "moves a file",
    "mkdir": "creates a directory", "makedirs": "creates a directory",
    "rmtree": "removes a directory tree", "move": "moves a file",
}

#: `pathlib.Path` methods, matched on the attribute name whatever the receiver.
_PATH_METHODS = {
    "write_text": "writes a file", "write_bytes": "writes a file",
    "unlink": "deletes a file", "rmdir": "removes a directory",
    "mkdir": "creates a directory", "touch": "writes a file",
    "rename": "moves a file", "replace": "moves a file",
    "symlink_to": "creates a link", "hardlink_to": "creates a link",
}


@dataclass(frozen=True)
class Site:
    """One mutation found in the code an allowlisted task can reach."""
    path: str
    line: int
    function: str
    what: str
    tasks: str = ""          # the allowlisted task(s) that reach it
    excepted: str = ""       # the reason from a `# dry-ok:` marker, or ""


@dataclass
class Report:
    tasks: int = 0                      # allowlisted tasks analysed
    functions: int = 0                  # functions analysed (deduplicated)
    sites: int = 0                      # mutations seen, guarded or not
    findings: "list[Site]" = field(default_factory=list)
    exceptions: "list[Site]" = field(default_factory=list)
    problems: "list[str]" = field(default_factory=list)
    per_task: "list[tuple[str, int, int]]" = field(default_factory=list)


# --------------------------------------------------------------------------- #
# Recognising the dry-run check
# --------------------------------------------------------------------------- #

def dry_value(expr: ast.AST, dry_names=frozenset(), wet_names=frozenset()):
    """True / False when this expression IS the dry-run flag, else None.

    The spellings in the codebase, and nothing else:

        _dry(ctx)                          getattr(ctx, "dry_run", False)
        ctx.dry_run                        <a local bound to either of those>

    A `bool(...)` wrapper and `not ...` are folded, so
    `if not getattr(ctx, "dry_run", False):` reads as "mutate only when wet".
    """
    if isinstance(expr, ast.UnaryOp) and isinstance(expr.op, ast.Not):
        inner = dry_value(expr.operand, dry_names, wet_names)
        return None if inner is None else (not inner)
    if isinstance(expr, ast.Name):
        if expr.id in dry_names:
            return True
        if expr.id in wet_names:
            return False
        return None
    if isinstance(expr, ast.Attribute) and expr.attr == "dry_run":
        return True
    if isinstance(expr, ast.Call):
        func = expr.func
        if isinstance(func, ast.Name):
            if func.id in DRY_HELPERS:
                return True
            if func.id == "bool" and len(expr.args) == 1:
                return dry_value(expr.args[0], dry_names, wet_names)
            if func.id == "getattr" and len(expr.args) >= 2:
                flag = expr.args[1]
                if isinstance(flag, ast.Constant) and flag.value == "dry_run":
                    return True
    return None


def branch_certainties(test, dry_names=frozenset(), wet_names=frozenset()):
    """(body, else) certainty for an `if` test: True dry, False wet, None unknown.

    For a bare dry test both branches are known (`if dry:` / `if not dry:`).
    For `A and B and not dry` the BODY needs "wet", but the else branch says
    nothing - it can be reached while dry - so only the body is known. `or`
    gives no information in either direction and is left unknown on purpose:
    guessing there is how a mutation slips through.
    """
    exact = dry_value(test, dry_names, wet_names)
    if exact is not None:
        return exact, (not exact)
    if isinstance(test, ast.BoolOp) and isinstance(test.op, ast.And):
        values = [dry_value(v, dry_names, wet_names) for v in test.values]
        if any(v is False for v in values):
            return False, None
        if values and all(v is True for v in values):
            return True, None
    return None, None


def _named_calls(node) -> tuple:
    """Local names assigned from a dry-run expression, as (dry, wet) sets.

    Flow-insensitive and deliberately narrow: only names bound exactly ONCE in
    the function count, so a variable re-bound later cannot make an unguarded
    mutation look guarded.
    """
    counts = {}
    polarity = {}
    for child in _own_nodes(node):
        value = None
        target = None
        if isinstance(child, ast.Assign) and len(child.targets) == 1:
            value, target = child.value, child.targets[0]
        elif isinstance(child, ast.AnnAssign) and child.value is not None:
            value, target = child.value, child.target
        if value is None or not isinstance(target, ast.Name):
            continue
        counts[target.id] = counts.get(target.id, 0) + 1
        polarity[target.id] = dry_value(value)
    dry = {n for n, c in counts.items() if c == 1 and polarity.get(n) is True}
    wet = {n for n, c in counts.items() if c == 1 and polarity.get(n) is False}
    return dry, wet


def _own_nodes(func):
    """Every node inside this function's own body, nested defs excluded.

    A nested function is analysed on its own (see `analyse_function`), so its
    locals must not leak into the enclosing body and vice versa.
    """
    stack = list(getattr(func, "body", []) or [])
    while stack:
        node = stack.pop()
        yield node
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef,
                                  ast.ClassDef, ast.Lambda)):
                continue
            stack.append(child)


def _terminates(body) -> bool:
    """True when this block cannot fall through (return/raise/continue/break)."""
    if not body:
        return False
    last = body[-1]
    if isinstance(last, (ast.Return, ast.Raise, ast.Continue, ast.Break)):
        return True
    if isinstance(last, ast.If) and last.orelse:
        return _terminates(last.body) and _terminates(last.orelse)
    return False


def _writes_file(call: ast.Call) -> "bool | None":
    """True when `open(...)`'s mode is a writing one, False for a plain read.

    An unknown mode (`open(p, mode)`) counts as a write: the guard cannot prove
    it is read-only, and guessing "read" is how a mutation slips through.
    """
    mode = call.args[1] if len(call.args) > 1 else None
    if mode is None:
        for kw in call.keywords:
            if kw.arg == "mode":
                mode = kw.value
                break
    if mode is None:
        return False                      # `open(path)` is read-only
    if isinstance(mode, ast.Constant) and isinstance(mode.value, str):
        return any(flag in mode.value for flag in ("w", "a", "x", "+"))
    return None


def name_aliases(tree: ast.Module) -> dict:
    """{local name: the name it was imported as} for this module.

    `import subprocess as _sp` and `from os import remove as _rm` both hide the
    table's names behind a local one. Reading the local spelling is how the
    guard saw the plain `subprocess.check_output` probe and walked straight
    past the aliased `_sp.check_output` one - a mutation is not less real for
    being imported under another name.
    """
    out = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.asname:
                    out[alias.asname] = alias.name
        elif isinstance(node, ast.ImportFrom) and not node.level:
            for alias in node.names:
                if alias.asname:
                    out[alias.asname] = alias.name
    return out


def mutation(call: ast.Call, aliases=None) -> str:
    """Why this call mutates the machine, or "" when it does not."""
    aliases = aliases or {}
    func = call.func
    if isinstance(func, ast.Name):
        name = aliases.get(func.id, func.id)
        if name == "open":
            written = _writes_file(call)
            if written is False:
                return ""
            return ("opens a file for writing" if written
                    else "opens a file with a mode built at run time")
        if name in _BARE_MUTATIONS:
            return _BARE_MUTATIONS[name]
        if name in _BARE_FILE_MUTATIONS:
            return _BARE_FILE_MUTATIONS[name]
        return ""
    if not isinstance(func, ast.Attribute):
        return ""
    owner = func.value
    if isinstance(owner, ast.Name):
        owner_name = aliases.get(owner.id, owner.id)
        table = _ATTR_MUTATIONS.get(owner_name)
        if table and func.attr in table:
            return table[func.attr]
        if owner_name == "winreg" and _winreg_mutates(func.attr):
            return "writes the registry"
        if owner_name == "quarantine" and func.attr == "stash":
            return "moves a file into the Quarantine bin"
    if func.attr == "open":
        written = _writes_file(call)
        if written is False:
            return ""
        return ("opens a file for writing" if written
                else "opens a file with a mode built at run time")
    if func.attr in _PATH_METHODS:
        return _PATH_METHODS[func.attr]
    return ""


def _winreg_mutates(attr: str) -> bool:
    return (attr.startswith("Set") or attr.startswith("Delete")
            or attr.startswith("CreateKey") or attr in {"SaveKey", "LoadKey"})


# --------------------------------------------------------------------------- #
# Walking a function body with a three-valued "is the context dry?"
# --------------------------------------------------------------------------- #

class _Analysis:
    """Findings for one source file."""

    def __init__(self, path: str, source: str, task: str = "", aliases=None):
        self.path = path
        self.lines = source.splitlines()
        self.task = task
        self.aliases = (name_aliases(ast.parse(source))
                        if aliases is None else aliases)
        self.sites = []                  # [(Site, guarded: bool)]

    def marker(self, node) -> str:
        """The reason from a `# dry-ok: ...` comment on this call, or "".

        The whole call is searched, not just its first line: a marker on the
        closing paren of a multi-line call is as clear as one on the first.
        """
        first = getattr(node, "lineno", 0)
        last = getattr(node, "end_lineno", 0) or first
        for lineno in range(first, last + 1):
            if not (1 <= lineno <= len(self.lines)):
                continue
            text = self.lines[lineno - 1]
            at = text.find("#")
            if at >= 0 and MARKER in text[at:]:
                return text[text.index(MARKER) + len(MARKER):].strip()
        return ""

    def record(self, node: ast.Call, certainty, function: str):
        what = mutation(node, self.aliases)
        if not what:
            return
        lineno = getattr(node, "lineno", 0)
        self.sites.append((
            Site(self.path, lineno, function, what, self.task,
                 excepted=self.marker(node)),
            certainty is False,
        ))


def analyse_function(node, analysis: _Analysis) -> list:
    """(site, guarded) pairs for one function body, in source order."""
    dry, wet = _named_calls(node)
    start = len(analysis.sites)
    _walk_body(node.body, None, node.name, analysis, dry, wet)
    return analysis.sites[start:]


def analyse_source(source: str, filename: str = "<test>", task: str = "") -> list:
    """[(Site, guarded)] for every top-level function in this source.

    The same analysis the scan runs, without needing a task table - which is
    what the tests plant their regressions in, and what keeps the rule and its
    test able to disagree about nothing.
    """
    tree = ast.parse(source, filename=filename)
    analysis = _Analysis(filename, source, task)
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            analyse_function(node, analysis)
    return list(analysis.sites)


def _walk_body(body, certainty, function, analysis, dry, wet):
    for stmt in body:
        if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue                    # analysed as its own function
        _walk_stmt(stmt, certainty, function, analysis, dry, wet)
        certainty = _certainty_after(stmt, certainty, dry, wet)


def _walk_stmt(stmt, certainty, function, analysis, dry, wet):
    if isinstance(stmt, ast.If):
        _sites_in(stmt.test, certainty, function, analysis)
        body_c, else_c = branch_certainties(stmt.test, dry, wet)
        _walk_body(stmt.body, certainty if body_c is None else body_c,
                   function, analysis, dry, wet)
        _walk_body(stmt.orelse, certainty if else_c is None else else_c,
                   function, analysis, dry, wet)
        return
    if isinstance(stmt, (ast.For, ast.AsyncFor)):
        _sites_in(stmt.target, certainty, function, analysis)
        _sites_in(stmt.iter, certainty, function, analysis)
        _walk_body(stmt.body, certainty, function, analysis, dry, wet)
        _walk_body(stmt.orelse, certainty, function, analysis, dry, wet)
        return
    if isinstance(stmt, ast.While):
        _sites_in(stmt.test, certainty, function, analysis)
        _walk_body(stmt.body, certainty, function, analysis, dry, wet)
        _walk_body(stmt.orelse, certainty, function, analysis, dry, wet)
        return
    if isinstance(stmt, (ast.With, ast.AsyncWith)):
        for item in stmt.items:
            _sites_in(item.context_expr, certainty, function, analysis)
        _walk_body(stmt.body, certainty, function, analysis, dry, wet)
        return
    if isinstance(stmt, ast.Try) or stmt.__class__.__name__ == "TryStar":
        # An exception can fire anywhere in the try body, so a handler is only
        # "definitely not dry" when the whole try is. Everything else keeps the
        # incoming certainty.
        handler_c = False if certainty is False else None
        _walk_body(stmt.body, certainty, function, analysis, dry, wet)
        for handler in stmt.handlers:
            _sites_in(handler.type, certainty, function, analysis)
            _walk_body(handler.body, handler_c, function, analysis, dry, wet)
        _walk_body(stmt.orelse, certainty, function, analysis, dry, wet)
        _walk_body(stmt.finalbody, certainty, function, analysis, dry, wet)
        return
    if isinstance(stmt, ast.Match):
        _sites_in(stmt.subject, certainty, function, analysis)
        for case in stmt.cases:
            _walk_body(case.body, certainty, function, analysis, dry, wet)
        return
    _sites_in(stmt, certainty, function, analysis)


def _sites_in(node, certainty, function, analysis):
    for child in ast.walk(node):
        if isinstance(child, ast.Call):
            analysis.record(child, certainty, function)


def _certainty_after(stmt, certainty, dry, wet):
    """What a dry check earlier in this block says about the statements after
    it: `if dry: ...; continue` makes the rest of the block definitely wet."""
    if not isinstance(stmt, ast.If):
        return certainty
    body_c, else_c = branch_certainties(stmt.test, dry, wet)
    if body_c is None and else_c is None:
        return certainty
    body_term = _terminates(stmt.body)
    else_term = _terminates(stmt.orelse) if stmt.orelse else False
    if body_term and else_term:
        return None                     # nothing after this can run
    if body_term:
        return else_c                   # the dry path left the block
    if else_term:
        return body_c                   # the wet path left the block
    if body_c is not None and body_c == else_c:
        return body_c
    return None


# --------------------------------------------------------------------------- #
# Reachability: which functions can an allowlisted task actually run?
# --------------------------------------------------------------------------- #

class _Index:
    """Function definitions per source file, plus import aliases per file."""

    def __init__(self, app_dir: Path):
        self.app_dir = app_dir
        self.sources = {}               # relpath -> source
        self.defs = {}                  # (relpath, name) -> node
        self.by_name = {}               # name -> [(relpath, node)]
        self.aliases = {}               # relpath -> {alias: relpath}
        self._load()

    def _load(self):
        for path in sorted(self.app_dir.rglob("*.py")):
            rel = path.relative_to(self.app_dir.parent).as_posix()
            try:
                source = io.open(path, encoding="utf-8").read()
                tree = ast.parse(source, filename=str(path))
            except (OSError, SyntaxError) as exc:   # a broken file is ruff's job
                print("  ! could not parse %s: %s" % (rel, exc))
                continue
            self.sources[rel] = source
            for node in tree.body:
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    self.defs[(rel, node.name)] = node
                    self.by_name.setdefault(node.name, []).append((rel, node))
            self.aliases[rel] = _import_aliases(tree)

    def resolve(self, rel: str, call: ast.Call):
        """The (relpath, node) pairs a call runs, or [] when unresolvable."""
        func = call.func
        if isinstance(func, ast.Name):
            return list(self.by_name.get(func.id, ()))
        if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
            target = self.aliases.get(rel, {}).get(func.value.id)
            if target and (target, func.attr) in self.defs:
                return [(target, self.defs[(target, func.attr)])]
        return []


def _import_aliases(tree: ast.Module) -> dict:
    """{local alias: repo-relative module} for `import app.x as y` forms.

    Only module aliases: `_lp.refresh_dynamic_paths()` in a task module has to
    be followed to know whether the helper it names mutates.
    """
    out = {}
    for node in tree.body:
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.asname and alias.name.startswith("app."):
                    out[alias.asname] = alias.name.replace(".", "/") + ".py"
        elif isinstance(node, ast.ImportFrom) and node.module \
                and node.module.startswith("app."):
            base = node.module.replace(".", "/") + ".py"
            for alias in node.names:
                if alias.asname:
                    continue        # `from app.x import y as z` names a function
                out[alias.name] = base
    return out


def _trusted(name: str) -> bool:
    return any(declared == name for (_path, declared) in DRY_AWARE)


def _reachable(index: _Index, roots, task: str = ""):
    """Analyse every function reachable from `roots`, once each.

    Returns (sites, analysed) where sites is [(Site, guarded)] and analysed is
    the set of function identities visited. A declared dry-aware primitive or a
    self-write owner is neither analysed nor followed from here: each is
    verified on its own, which is what makes those declarations meaningful.
    """
    seen = set()
    queue = list(roots)
    sites = []
    while queue:
        rel, node = queue.pop()
        key = (rel, getattr(node, "name", ""), getattr(node, "lineno", 0))
        if key in seen:
            continue
        seen.add(key)
        analysis = _Analysis(rel, index.sources[rel], task)
        sites.extend(analyse_function(node, analysis))
        for child in ast.walk(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)) \
                    and child is not node:
                # A nested closure is not in the module index (its name is
                # local), but it still runs - so it gets analysed on its own,
                # with its own dry check, rather than vanishing from the walk.
                queue.append((rel, child))
                continue
            if not isinstance(child, ast.Call):
                continue
            if mutation(child, analysis.aliases):
                # A mutating call is judged where it is written. Expanding into
                # it would report a runner's own internals as extra findings
                # and bury the one line that needs the dry check.
                continue
            callee = child.func
            if isinstance(callee, ast.Name) and _trusted(callee.id):
                continue
            if isinstance(callee, ast.Attribute) and _trusted(callee.attr):
                continue
            for target_rel, target in index.resolve(rel, child):
                if target_rel in SELF_WRITE_OWNERS:
                    continue
                queue.append((target_rel, target))
    return sites, seen


# --------------------------------------------------------------------------- #
# The scan
# --------------------------------------------------------------------------- #

def _allowlist():
    """(keys, owner_path) read from the module that owns the declaration."""
    sys.path.insert(0, str(ROOT))
    from app.storage_scan import SCAN_ALLOWLIST
    return frozenset(SCAN_ALLOWLIST), ALLOWLIST_OWNER[0]


def _declaration_problems() -> list:
    """The allowlist's own consistency check (app.storage_scan.validate_allowlist).

    An unverifiable declaration is a failure here, so a task that is neither
    measured nor explained - the silent way the estimate shrinks - cannot
    pass CI just because every key that IS declared looks fine.
    """
    sys.path.insert(0, str(ROOT))
    try:
        from app.storage_scan import validate_allowlist
        ok, problems = validate_allowlist()
    except Exception as exc:                    # an import cycle is a failure
        return ["could not validate the allowlist tables: %s" % exc]
    return [] if ok else list(problems)


def _clean_tasks():
    """key -> (relpath, function name) for the live Clean tab."""
    sys.path.insert(0, str(ROOT))
    from app.tab_presets import TABS

    out = {}
    for task in TABS.get("Clean", []):
        run = task.run
        module = inspect.getmodule(run)
        path = inspect.getsourcefile(run)
        if module is None or path is None:
            out[task.key] = ("", "")
            continue
        try:
            rel = Path(path).resolve().relative_to(ROOT).as_posix()
        except ValueError:
            rel = Path(path).as_posix()
        out[task.key] = (rel, run.__name__)
    return out


def scan(app_dir: Path = APP) -> Report:
    """Analyse every allowlisted task plus every declared primitive."""
    report = Report()
    keys, owner = _allowlist()
    tasks = _clean_tasks()
    index = _Index(app_dir)
    report.problems.extend(_declaration_problems())

    # The same helper is usually reachable from several tasks; a mutation is a
    # property of the code, so collect every site once and note who reaches it.
    sites = {}                       # (path, line, function, what) -> dict
    analysed = set()
    for key in sorted(keys):
        where = tasks.get(key)
        if not where or not where[0]:
            report.problems.append(
                "%s is on SCAN_ALLOWLIST but no Clean task answers to it - an "
                "unverifiable declaration cannot be checked (%s)"
                % (key, owner))
            continue
        rel, name = where
        node = index.defs.get((rel, name))
        if node is None:
            report.problems.append(
                "%s -> %s:%s is not a plain function this guard can read, so "
                "its dry-run promise cannot be checked (fail closed)"
                % (key, rel, name))
            continue
        report.tasks += 1
        found, visited = _reachable(index, [(rel, node)], key)
        analysed |= visited
        unguarded = 0
        for site, guarded in found:
            entry = sites.setdefault(
                (site.path, site.line, site.function, site.what),
                {"site": site, "guarded": guarded, "tasks": set()})
            entry["tasks"].add(key)
            if not guarded:
                unguarded += 1
        report.per_task.append((key, len(visited), unguarded))

    # The declarations themselves: re-verified by the same rule, so "dry-aware"
    # cannot outlive the guard that makes it true.
    for (rel, name), why in sorted(DRY_AWARE.items()):
        node = index.defs.get((rel, name))
        if node is None:
            report.problems.append(
                "declared dry-aware primitive %s:%s (%s) is gone - the "
                "allowlist leans on it, so this guard can no longer prove "
                "anything" % (rel, name, why))
            continue
        found, visited = _reachable(index, [(rel, node)])
        analysed |= visited
        for site, guarded in found:
            key = (site.path, site.line, site.function, site.what)
            entry = sites.setdefault(
                key, {"site": site, "guarded": guarded, "tasks": set()})
            entry["guarded"] = entry["guarded"] and guarded
            if not guarded and not site.excepted:
                report.problems.append(
                    "declared dry-aware primitive %s mutates unguarded at "
                    "%s:%d (%s) - every allowlisted task that reaches it would "
                    "do this during a scan"
                    % (name, site.path, site.line, site.what))

    report.functions = len(analysed)
    report.sites = len(sites)
    for entry in sites.values():
        site = entry["site"]
        if entry["guarded"]:
            continue
        tagged = Site(site.path, site.line, site.function, site.what,
                      ",".join(sorted(entry["tasks"])), site.excepted)
        if site.excepted:
            report.exceptions.append(tagged)
        else:
            report.findings.append(tagged)
    report.findings.sort(key=lambda s: (s.path, s.line))
    report.exceptions.sort(key=lambda s: (s.path, s.line))
    return report


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    verbose = "-v" in argv or "--verbose" in argv

    report = scan(APP)
    keys, owner = _allowlist()
    print("Dry-run scope check")
    print("  allowlist                : %s (%d of %d Clean tasks)"
          % (owner, len(keys), len(_clean_tasks())))
    print("  tasks analysed           : %d" % report.tasks)
    print("  functions analysed       : %d" % report.functions)
    print("  mutations seen           : %d (guarded or not)" % report.sites)
    print("  dry-aware primitives     : %d" % len(DRY_AWARE))
    for (path, name), why in sorted(DRY_AWARE.items()):
        print("    - %s:%s  (%s)" % (path, name, why))
    print("  app-owned write modules  : %d" % len(SELF_WRITE_OWNERS))
    for path, why in sorted(SELF_WRITE_OWNERS.items()):
        print("    - %s  (%s)" % (path, why))
    print("  unguarded mutations      : %d" % len(report.findings))
    print("  documented exceptions    : %d" % len(report.exceptions))

    if verbose:
        for key, functions, found in report.per_task:
            print("    %-22s %3d function(s)  %d unguarded"
                  % (key, functions, found))
    for site in report.exceptions:
        print("    - %s:%d  %s  [%s]"
              % (site.path, site.line, site.what, site.excepted))
    for site in report.findings:
        print("  %s:%d  %s  in %s()  [reachable from %s]"
              % (site.path, site.line, site.what, site.function, site.tasks))
    for problem in report.problems:
        print("  ! %s" % problem)

    if report.findings or report.problems:
        print("\nFAIL: a task on SCAN_ALLOWLIST can run during a read-only "
              "scan and still delete or write. Guard the mutation with "
              "`if _dry(ctx)` / `if not ctx.dry_run`, call a declared dry-aware "
              "primitive, or - if it truly is safe in a measuring pass - mark "
              "the call `# dry-ok: <reason>`.")
        return 1
    print("\nOK: every mutation an allowlisted task can reach is behind a "
          "dry-run check.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
