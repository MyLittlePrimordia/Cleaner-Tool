"""The third CI guard: a dry-run scan must not delete or write.

`SCAN_ALLOWLIST` says which Clean-tab tasks a measuring pass may invoke, and
Storage Insight / the Health sensor invoke them for real (`dry_run=True`).
That promise is only as good as the `if _dry(ctx)` checks in the code, so
`dev/tools/verify_dryrun_scope.py` proves - on every run - that every mutation an
allowlisted task can reach is behind such a check, and re-proves the dry-aware
primitives the allowlist leans on.

Like the other two guards, the point here is that it FIRES. Each row below is a
body shape, and the expectation is the number of mutations the guard should
call unguarded. The guarded shapes are rows too: a guard that quietly stopped
recognising `if not dry and cond:` would pass every failure test and let a
mutation through, so "should stay quiet" is asserted with the same weight as
"should fire".

Everything is AST-only: the snippets are never imported and the planted
regressions are copies in tmp_path, so no file here is read, deleted or
written by the code under test.
"""
from __future__ import annotations

import ast
import textwrap
from pathlib import Path

import pytest

import dev.tools.verify_dryrun_scope as guard

REPO_ROOT = Path(__file__).resolve().parent.parent.parent


def _sites(body: str, filename: str = "planted.py") -> list:
    """(site, guarded) pairs for a snippet wrapped in `def task(ctx):`."""
    source = "def task(ctx):\n" + textwrap.indent(
        textwrap.dedent(body).strip("\n"), "    ") + "\n"
    return guard.analyse_source(source, filename)


def _unguarded(body: str) -> list:
    return [site for site, guarded in _sites(body) if not guarded]


# --------------------------------------------------------------------------- #
# The table: one row per body shape the guard must read correctly.
# --------------------------------------------------------------------------- #

_ROWS = (
    # id, body, expected unguarded count, expected total sites
    ("bare delete", "os.remove(here())", 1, 1),
    ("delete after an early dry return",
     "if _dry(ctx):\n    return 0\nos.remove(here())", 0, 1),
    ("early return on ctx.dry_run",
     "if ctx.dry_run:\n    return 0\nos.remove(here())", 0, 1),
    ("early return on getattr",
     'if getattr(ctx, "dry_run", False):\n    return 0\nos.remove(here())', 0, 1),
    ("delete in the else of a dry test",
     'if _dry(ctx):\n    ctx.log("measuring")\nelse:\n    os.remove(here())', 0, 1),
    ("delete in the DRY branch",
     'if _dry(ctx):\n    os.remove(here())\nelse:\n    ctx.log("cleaning")', 1, 1),
    ("local alias, negated",
     'dry = bool(getattr(ctx, "dry_run", False))\nif not dry:\n    os.remove(here())',
     0, 1),
    ("local alias, no guard",
     'dry = bool(getattr(ctx, "dry_run", False))\nif dry:\n    ctx.log("x")\nos.remove(here())',
     1, 1),
    ("local alias re-bound once is not trusted",
     'dry = bool(getattr(ctx, "dry_run", False))\n'
     "if flag():\n    dry = False\n"
     'if dry:\n    ctx.log("x")\nos.remove(here())', 1, 1),
    ("guard clause with continue",
     "for p in paths():\n    if _dry(ctx):\n        continue\n    os.remove(p)",
     0, 1),
    ("guard clause with break",
     "for p in paths():\n    if ctx.dry_run:\n        break\n    os.remove(p)",
     0, 1),
    ("conjunction that requires wet",
     "if not extensions and not ctx.dry_run:\n    os.remove(here())", 0, 1),
    ("conjunction that requires dry",
     "if ctx.dry_run and flag():\n    os.remove(here())", 1, 1),
    ("or gives no information",
     "if ctx.dry_run or flag():\n    os.remove(here())", 1, 1),
    ("try inside the wet branch",
     'if not ctx.dry_run:\n    try:\n        os.remove(here())\n'
     '    finally:\n        ctx.log("done")', 0, 1),
    ("except handler of a wet try",
     "if not ctx.dry_run:\n    try:\n        risky()\n"
     "    except OSError:\n        shutil.rmtree(here())", 0, 1),
    ("except handler of an unknown try",
     "try:\n    risky()\nexcept OSError:\n    shutil.rmtree(here())", 1, 1),
    ("read open", "with open(here()) as fh:\n    fh.read()", 0, 0),
    ("binary read open", 'open(here(), "rb")', 0, 0),
    ("write open", 'with open(here(), "w") as fh:\n    fh.write("x")', 1, 1),
    ("open with a runtime mode", "open(here(), mode)", 1, 1),
    ("path write_text", 'Path(here()).write_text("x")', 1, 1),
    ("path read_text", "Path(here()).read_text()", 0, 0),
    ("runner call", 'run_cmd(ctx, "net stop wuauserv")', 1, 1),
    ("PowerShell runner", 'run_ps(ctx, "Clear-RecycleBin")', 1, 1),
    ("registry delete", 'winreg.DeleteValue(key, "x")', 1, 1),
    ("bare remove import", "remove(here())", 1, 1),
    ("quarantine stash", "quarantine.stash(here())", 1, 1),
    ("external process", 'subprocess.check_output(["tasklist"])', 1, 1),
    ("restore point", 'create_restore_point("x")', 1, 1),
    # An import under another name is the same call. The guard used to read the
    # local spelling and walk straight past `_sp.check_output(...)`, which is a
    # live probe in the Clean tab (three of them) - so the alias is a row, not
    # a detail.
    ("aliased module", 'import subprocess as _sp\n_sp.check_output(["tasklist"])',
     1, 1),
    ("aliased function", 'from os import remove as _rm\n_rm(here())', 1, 1),
    ("aliased runner",
     'from app.tasks.ps import run_ps as _ps\n_ps(ctx, "Clear-RecycleBin")', 1, 1),
    ("aliased probe inside the dry branch",
     'import subprocess as _sp\nif ctx.dry_run:\n    _sp.check_output(["tasklist"])',
     1, 1),
)


@pytest.mark.parametrize("name,body,expected_unguarded,expected_sites", _ROWS,
                         ids=[row[0] for row in _ROWS])
def test_the_guard_reads_each_body_shape(name, body, expected_unguarded,
                                         expected_sites):
    sites = _sites(body)
    assert len(sites) == expected_sites, (
        "%s: expected %d mutation(s), saw %r"
        % (name, expected_sites, [s.what for s, _g in sites]))
    assert len([s for s, guarded in sites if not guarded]) == expected_unguarded, (
        "%s: guarded classification is wrong (%r)"
        % (name, [(s.what, g) for s, g in sites]))


def test_a_guarded_mutation_is_still_counted_as_seen():
    """Coverage honesty: the report says how many sites it proved, so a row
    that is correctly guarded must not vanish from the count."""
    sites = _sites("if not ctx.dry_run:\n    os.remove(here())")
    assert len(sites) == 1 and sites[0][1] is True


def test_the_rule_is_per_function_not_per_caller():
    """"My caller checks dry_run" is not accepted - that only holds until a
    second caller appears."""
    planted = textwrap.dedent('''
        def helper(ctx):
            os.remove(here())

        def task(ctx):
            if _dry(ctx):
                return 0
            helper(ctx)
    ''')
    unguarded = [s for s, g in guard.analyse_source(planted) if not g]
    assert [s.function for s in unguarded] == ["helper"]


# --------------------------------------------------------------------------- #
# In-place exceptions
# --------------------------------------------------------------------------- #

def test_a_marker_with_a_reason_makes_the_call_an_exception():
    sites = _sites("os.remove(here())  # dry-ok: our own 100-byte lock file")
    assert len(sites) == 1
    site, guarded = sites[0]
    assert guarded is False
    assert site.excepted == "our own 100-byte lock file"


def test_a_marker_on_a_multi_line_call_is_found():
    body = "os.remove(\n    here(),\n)  # dry-ok: our own lock file"
    site, _guarded = _sites(body)[0]
    assert site.excepted == "our own lock file"


def test_a_marker_without_a_reason_is_not_an_exception():
    """An exception with no reason is an undocumented one, so it fails."""
    site, guarded = _sites("os.remove(here())  # dry-ok:")[0]
    assert guarded is False
    assert site.excepted == ""


def test_a_marker_does_not_silence_a_guarded_call_that_needs_no_marker():
    sites = _sites('if not ctx.dry_run:\n    os.remove(here())  # dry-ok: x')
    assert sites[0][1] is True          # guarded anyway; the marker is harmless


# --------------------------------------------------------------------------- #
# Reachability: what the walk follows, and what a declaration stops it from
# following.
# --------------------------------------------------------------------------- #

def _tree(tmp_path, files: dict):
    """Write {relative path: source} under tmp_path/app and index it."""
    root = tmp_path / "app"
    root.mkdir()
    for rel, source in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(textwrap.dedent(source), encoding="utf-8")
    return guard._Index(root)


def test_a_helper_is_followed_through_a_plain_call(tmp_path):
    index = _tree(tmp_path, {
        "__init__.py": "",
        "taskmod.py": '''
            def task(ctx):
                _wipe("x")

            def _wipe(p):
                os.remove(p)
        ''',
    })
    sites, visited = guard._reachable(
        index, [("app/taskmod.py", index.defs[("app/taskmod.py", "task")])])
    assert [s.function for s, g in sites if not g] == ["_wipe"]
    assert len(visited) == 2


def test_a_helper_is_followed_through_an_imported_module_alias(tmp_path):
    """`_lp.refresh_dynamic_paths()` must be followed: an attribute call is
    still a call, and the helper could be the thing that writes."""
    index = _tree(tmp_path, {
        "__init__.py": "",
        "launcher_paths.py": '''
            def refresh():
                os.makedirs("x")
        ''',
        "taskmod.py": '''
            import app.launcher_paths as _lp

            def task(ctx):
                _lp.refresh()
        ''',
    })
    sites, _visited = guard._reachable(
        index, [("app/taskmod.py", index.defs[("app/taskmod.py", "task")])])
    assert [s.path for s, g in sites if not g] == ["app/launcher_paths.py"]


def test_a_declared_dry_aware_primitive_is_not_followed(tmp_path, monkeypatch):
    """The declaration is what lets an allowlisted task call a helper that
    would otherwise be a violation; the primitive is verified separately."""
    index = _tree(tmp_path, {
        "__init__.py": "",
        "wipe.py": '''
            def wipe(p):
                os.remove(p)
        ''',
        "tasks.py": '''
            def helper(ctx, p):
                wipe(p)

            def task(ctx):
                helper(ctx, "x")
        ''',
    })
    root = index.defs[("app/tasks.py", "task")]

    monkeypatch.setattr(guard, "DRY_AWARE", {})
    sites, _visited = guard._reachable(index, [("app/tasks.py", root)])
    assert [s.path for s, g in sites if not g] == ["app/wipe.py"]

    monkeypatch.setattr(guard, "DRY_AWARE",
                        {("app/tasks.py", "helper"): "declared for this test"})
    sites, _visited = guard._reachable(index, [("app/tasks.py", root)])
    assert sites == [], "a declared primitive must be verified on its own"


def test_a_self_write_owner_is_not_followed(tmp_path):
    """The app's own log is the one write a scan may do; that is exactly the
    path `log_security_event` takes to the file it appends."""
    index = _tree(tmp_path, {
        "__init__.py": "",
        "config_events.py": '''
            def log_security_event(category, message):
                os.makedirs("logs")
                os.remove("x")
        ''',
        "taskmod.py": '''
            def task(ctx):
                log_security_event("c", "m")
        ''',
    })
    sites, _visited = guard._reachable(
        index, [("app/taskmod.py", index.defs[("app/taskmod.py", "task")])])
    assert sites == []


def test_a_nested_closure_is_analysed_on_its_own(tmp_path):
    index = _tree(tmp_path, {
        "__init__.py": "",
        "taskmod.py": '''
            def task(ctx):
                def inner():
                    os.remove("x")
                if ctx.dry_run:
                    return 0
                inner()
        ''',
    })
    sites, _visited = guard._reachable(
        index, [("app/taskmod.py", index.defs[("app/taskmod.py", "task")])])
    assert [s.function for s, g in sites if not g] == ["inner"], (
        "a closure with no dry check of its own must not hide behind its caller")


# --------------------------------------------------------------------------- #
# The real repository
# --------------------------------------------------------------------------- #

def test_the_repository_passes_the_guard():
    report = guard.scan()
    assert report.problems == []
    assert report.findings == []
    keys, _owner = guard._allowlist()
    assert report.tasks == len(keys), (
        "every allowlisted key must resolve to a task this guard can read")
    assert report.sites >= 1, "the scan should have seen the deletion funnels"


def test_every_printed_exception_states_a_reason():
    report = guard.scan()
    for site in report.exceptions:
        assert site.excepted.strip(), (
            "%s:%d is excepted with no reason" % (site.path, site.line))
    print_reason = guard.main([]) == 0
    assert print_reason, "the tool's own entry point must agree with scan()"


def test_a_delete_planted_in_a_real_allowlisted_task_is_caught():
    """The regression that matters: one `os.remove` added to a task that the
    allowlist already trusts. The scan must call it unguarded."""
    source = (REPO_ROOT / "app" / "tasks" / "clean_tasks.py").read_text(
        encoding="utf-8")
    marker = "def clean_shader_cache(ctx: TaskContext):\n"
    assert source.count(marker) == 1
    drifted = source.replace(
        marker, marker + "    os.remove(os.environ.get('TEMP', 'x'))\n", 1)
    unguarded = [site for site, guarded in guard.analyse_source(
        drifted, "app/tasks/clean_tasks.py")
        if not guarded and site.function == "clean_shader_cache"]
    assert len(unguarded) == 1
    assert unguarded[0].what == "deletes a file"


def test_a_planted_guard_clause_removal_is_caught():
    """`_clean_files` is declared dry-aware because of one `if not dry:`. Remove
    that line and the declaration must stop being true."""
    source = (REPO_ROOT / "app" / "tasks" / "game_tasks.py").read_text(
        encoding="utf-8")
    tree = ast.parse(source)
    func = next(node for node in tree.body
                if isinstance(node, ast.FunctionDef) and node.name == "_clean_files")
    starts = [node.lineno for node in ast.walk(func)
              if isinstance(node, ast.If)
              and any(isinstance(inner, ast.Call)
                      and getattr(inner.func, "attr", "") == "remove"
                      for inner in ast.walk(node))]
    assert starts, "_clean_files no longer has an `if ...: os.remove(...)`"
    lines = source.splitlines(keepends=True)
    guard_line = lines[starts[0] - 1]
    assert "not dry" in guard_line
    lines[starts[0] - 1] = guard_line.replace("not dry", "True")
    drifted = "".join(lines)
    assert drifted != source
    unguarded = [site for site, guarded in guard.analyse_source(
        drifted, "app/tasks/game_tasks.py")
        if not guarded and site.function == "_clean_files"]
    assert [site.what for site in unguarded] == ["deletes a file"]


# --------------------------------------------------------------------------- #
# Fail closed
# --------------------------------------------------------------------------- #

def test_an_allowlisted_key_with_no_task_is_a_failure(monkeypatch):
    monkeypatch.setattr(guard, "_allowlist",
                        lambda: (frozenset({"ghost_task"}),
                                 "app/storage_scan.py"))
    report = guard.scan()
    assert any("ghost_task" in problem for problem in report.problems)


def test_an_unreadable_task_function_is_a_failure(monkeypatch):
    """Fail closed: a `run` this guard cannot read is not a pass."""
    monkeypatch.setattr(guard, "_allowlist",
                        lambda: (frozenset({"shader_cache"}),
                                 "app/storage_scan.py"))
    tasks = dict(guard._clean_tasks())
    tasks["shader_cache"] = ("app/tasks/clean_tasks.py", "not_a_function")
    monkeypatch.setattr(guard, "_clean_tasks", lambda: tasks)
    report = guard.scan()
    assert any("fail closed" in problem for problem in report.problems)


def test_a_declared_primitive_that_no_longer_exists_is_a_failure(monkeypatch):
    declared = dict(guard.DRY_AWARE)
    declared[("app/gone.py", "vanished")] = "declared for this test"
    monkeypatch.setattr(guard, "DRY_AWARE", declared)
    report = guard.scan()
    assert any("vanished" in problem for problem in report.problems)


def test_the_entry_point_fails_on_a_finding(monkeypatch):
    report = guard.Report(tasks=1, findings=[
        guard.Site("app/x.py", 3, "task", "deletes a file", "shader_cache")])
    monkeypatch.setattr(guard, "scan", lambda *a, **k: report)
    assert guard.main([]) == 1


def test_the_entry_point_passes_on_a_clean_report(monkeypatch):
    report = guard.Report(tasks=1, sites=1, exceptions=[
        guard.Site("app/x.py", 3, "task", "spawns an external process",
                   "browser_cache", "read-only probe")])
    monkeypatch.setattr(guard, "scan", lambda *a, **k: report)
    assert guard.main([]) == 0
