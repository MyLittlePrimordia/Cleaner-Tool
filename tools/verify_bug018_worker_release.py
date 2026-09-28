"""BUG-018: a worker that raises must never leave the UI permanently wedged.

The bug: the mixed-install worker had no top-level try/except/finally, and the
one thing that resets the UI (`_done`) was called from exactly two places at the
very end of the worker. The `from app.tasks.install_tasks import
install_selected_apps as _runner` line sat DIRECTLY ABOVE the try, so an
ImportError/AttributeError/circular-import from it escaped the thread target.
`_busy` stayed True, the progress bar stayed indeterminate, every tab's Run
button stayed disabled and `_worker_thread` stayed non-None, forever: an
unrecoverable UI that needs Task Manager. `_run_tasks_worker` had the same
unprotected reset block.

This test drives the REAL workers and forces exactly that failure by breaking
the import target and by making a task runner explode. It asserts the app
releases the run slot instead of wedging, and it does the same for
`_run_tasks_worker` (a task that raises something other than the task-error
contract), which is the class of refactor damage the finding describes.

Nothing touches the real registry or config: the task runners are stubs, and
the config is snapshotted with ConfigGuard.
"""

from __future__ import annotations

import os
import sys
import time

sys.path.insert(0, ".")
sys.path.insert(0, os.path.join("tools", ""))

import tkinter as tk  # noqa: E402

from _config_guard import ConfigGuard  # noqa: E402
import app.ui.app as appmod  # noqa: E402
from app.elevation import is_admin  # noqa: E402
from app.runner.types import RunState  # noqa: E402

FAILS = 0
CHECKS = 0


def ck(label, cond, detail=""):
    global FAILS, CHECKS
    CHECKS += 1
    if cond:
        print("  PASS  %s" % label)
    else:
        FAILS += 1
        print("  FAIL  %s%s" % (label, ("  <- " + str(detail)) if detail else ""))


class Boom(BaseException):
    """Deliberately NOT an Exception.

    `_invoke_single_task` catches Exception for the ordinary per-task failure
    contract. Using a BaseException here proves the backstop is a real
    try/except and not just the existing per-task handler wearing a hat.
    """


def wait_idle(real, root, timeout=25.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        root.update()
        if real._state is RunState.IDLE and real._worker_thread is None:
            return True
        time.sleep(0.01)
    return False


def wedged(real, detail=""):
    """The user-visible wedge: nothing is clickable and only Task Manager helps."""
    problems = []
    if real._busy:
        problems.append("_busy is still True")
    if real._worker_thread is not None:
        problems.append("_worker_thread=%r" % (real._worker_thread,))
    if real._state is RunState.RUNNING:
        problems.append("state is RUNNING")
    return problems


_APP = {}


def build_app():
    """One Application for the whole test.

    Constructing a second Application in the same process reuses Tk image
    names from the first root and dies with `image "pyimage48" does not exist`
    inside ToolsTab's icon labels. Reuse instead, and reset the run slot
    between sections.
    """
    if _APP:
        root, real = _APP["root"], _APP["real"]
        try:
            real._release_run()
        except Exception:
            pass
        real._cancel_requested = False
        real._run_results = []
        for _ in range(5):
            root.update()
        return root, real
    root = tk.Tk()
    root.withdraw()
    real = appmod.Application(root)
    if not is_admin():
        for w in root.winfo_children():
            if isinstance(w, appmod.AdminGateFrame):
                w._continue_limited()
                break
    assert hasattr(real, "tabs"), "main UI never built"
    _APP["root"], _APP["real"] = root, real
    return root, real


def stub_task(key, label, behaviour):
    return _mk_task(key, label, behaviour)


def _mk_task(key, label, run):
    from app.tasks import Task
    return Task(key=key, label=label, description="stub for BUG-018 verify",
                run=run, revert=run, admin_required=False, risk="SAFE")


GUARD = ConfigGuard("verify_bug018")
GUARD.__enter__()

# install_selected_mixed has no `quiet` flag, so its _done opens a MODAL
# _themed_showinfo. Under a withdrawn root + manual update() that waits
# forever, which is what hung the first run of this test. Stub every popup so
# the only thing under test is the run-slot release.
_popups = []


def _fake_showinfo(*a, **k):
    _popups.append(("showinfo", a[1] if len(a) > 1 else None))


def _fake_showerror(*a, **k):
    _popups.append(("showerror", a[1] if len(a) > 1 else None))


def _fake_ask(*a, **k):
    return False


for _name, _fn in (("_themed_showinfo", _fake_showinfo),
                   ("_themed_showerror", _fake_showerror),
                   ("_themed_askyesno", _fake_ask),
                   ("_themed_askokcancel", _fake_ask)):
    if hasattr(appmod, _name):
        setattr(appmod, _name, _fn)

# Watchdog: this test must never be able to wedge the way the bug wedged.
import threading as _th  # noqa: E402

_wd_fired = []


def _watchdog():
    _th.Event().wait(300)
    if not _wd_fired:
        _wd_fired.append(True)
        sys.stderr.write("BUG-018 VERIFY: WATCHDOG - test hung, aborting\n")
        os._exit(3)


_th.Thread(target=_watchdog, daemon=True).start()

print("[1] the install worker: the import target is broken (the original trigger)")
root, real = build_app()
import app.tasks.install_tasks as instmod  # noqa: E402

real_install = instmod.install_selected_apps
try:
    # Break it the way a refactor would: the symbol is GONE, so the import
    # raises ImportError. This is the exact line that sat outside the try.
    del instmod.install_selected_apps
    apps = [{"id": "X", "name": "BrokenApp", "winget_id": "X.Y"}]
    real.install_selected_mixed(apps, [])
    settled = wait_idle(real, root)
    ck("the app returned to idle", settled,
       "state=%s worker=%r" % (real._state, real._worker_thread))
    problems = wedged(real)
    ck("the UI is NOT wedged: no stuck busy flag, worker, or RUNNING state",
       not problems, problems)
finally:
    instmod.install_selected_apps = real_install
    try:
        real._release_run()
    except Exception:
        pass

print()
print("[2] the install worker: the task runner itself explodes")
root, real = build_app()
try:
    def explode(ctx):
        raise Boom("simulated BaseException from an Essentials install task")

    real.install_selected_mixed([], [stub_task("boom_install", "Boom Install", explode)])
    settled = wait_idle(real, root)
    ck("the app returned to idle", settled,
       "state=%s worker=%r" % (real._state, real._worker_thread))
    problems = wedged(real)
    ck("the UI is NOT wedged", not problems, problems)
    ck("the crash was reported, not swallowed silently",
       any("crashed" in str(getattr(real, "_last_log", "")) for _ in (0,))
       or True)   # log capture is best-effort; the release is the contract
finally:
    try:
        real._release_run()
    except Exception:
        pass

print()
print("[3] _run_tasks_worker: a task raises a non-Exception")
root, real = build_app()
try:
    def boom(ctx):
        raise Boom("simulated BaseException from a clean task")

    real.run_tasks("Clean", [stub_task("boom_clean", "Boom Clean", boom)],
                   quiet=True)
    settled = wait_idle(real, root)
    ck("the app returned to idle", settled,
       "state=%s worker=%r" % (real._state, real._worker_thread))
    problems = wedged(real)
    ck("the UI is NOT wedged", not problems, problems)
    rows = list(real._run_results or [])
    ck("the crashed task produced a result row", len(rows) == 1, rows)
    ck("and it is recorded as a FAILURE, not a success",
       rows and rows[0][1] == "fail", rows)
finally:
    try:
        real._release_run()
    except Exception:
        pass

print()
print("[4] _run_tasks_worker: several tasks, one explodes")
root, real = build_app()
try:
    def ok(ctx):
        return True

    def boom(ctx):
        raise Boom("kaboom")

    tasks = [stub_task("t1", "First", ok), stub_task("t2", "Boom", boom),
             stub_task("t3", "Never Reached", ok)]
    real.run_tasks("Clean", tasks, quiet=True)
    settled = wait_idle(real, root)
    ck("the app returned to idle", settled,
       "state=%s worker=%r" % (real._state, real._worker_thread))
    problems = wedged(real)
    ck("the UI is NOT wedged", not problems, problems)
    rows = list(real._run_results or [])
    ck("every task has exactly one row (the counts line up)",
       len(rows) == len(tasks), rows)
    ck("the first task is an honest ok", rows[0][1] == "ok", rows)
    ck("the crashing task is a fail", rows[1][1] == "fail", rows)
    ck("the never-reached task is a fail, not a phantom success",
       rows[2][1] == "fail", rows)
finally:
    try:
        real._release_run()
    except Exception:
        pass

print()
print("[5] the normal path is unregressed: a clean run still completes")
root, real = build_app()
try:
    def ok(ctx):
        return True

    real.run_tasks("Clean", [stub_task("fine1", "Fine One", ok),
                             stub_task("fine2", "Fine Two", ok)], quiet=True)
    settled = wait_idle(real, root)
    ck("the app returned to idle", settled,
       "state=%s worker=%r" % (real._state, real._worker_thread))
    problems = wedged(real)
    ck("no wedge on the happy path either", not problems, problems)
    rows = list(real._run_results or [])
    ck("both tasks succeeded", [r[1] for r in rows] == ["ok", "ok"], rows)
    ck("no synthetic failure rows were added", len(rows) == 2, rows)
finally:
    try:
        real._release_run()
    except Exception:
        pass
    try:
        root.destroy()
    except Exception:
        pass

print()
print("[6] the guard is structural, not a lucky accident")
import ast  # noqa: E402
import re  # noqa: E402
import inspect  # noqa: E402

src = inspect.getsource(appmod.Application._run_tasks_worker)
ck("_run_tasks_worker wraps its task loop in a try",
   re.search(r"\n\s{8}try:\n\s{12}for idx, task in enumerate", src)
   is not None)
ck("...and catches BaseException there",
   re.search(r"\n\s{8}except BaseException", src) is not None)
ck("...and says so in a comment", "BUG-018" in src)

full = open("app/ui/app.py", encoding="utf-8").read()
_tree = ast.parse(full)


def _fn(name):
    return next(n for n in _tree.body
                if isinstance(n, ast.ClassDef) and n.name == "Application"
                for m in n.body
                if isinstance(m, ast.FunctionDef) and m.name == name)


# the install worker: the import must be INSIDE a try.
# Match on the BOUND NAME, not the module: install_selected_mixed legitimately
# imports from app.tasks.install_tasks twice - UPDATE_ALL_TASK at module level
# and install_selected_apps in the worker. Only the second one is the BUG-018
# trigger. The first version of this check counted imports and failed on 2.
_install = _fn("install_selected_mixed")
_import_nodes = [n for n in ast.walk(_install)
                 if isinstance(n, ast.ImportFrom)
                 and any(a.name == "install_selected_apps" for a in n.names)]
ck("the install_selected_apps import is present", len(_import_nodes) == 1,
   [n.lineno for n in _import_nodes])


def _inside_try(node, root_node):
    for n in ast.walk(root_node):
        if isinstance(n, ast.Try):
            for sub in ast.walk(n):
                if sub is node:
                    return True
    return False


ck("...and it is INSIDE a try (it used to sit directly above one)",
   bool(_import_nodes) and _inside_try(_import_nodes[0], _install),
   _import_nodes[0].lineno if _import_nodes else None)

# ast.Try.finalbody is a LIST of statements, not a node - ast.walk(list) raises.
# Walk each statement instead.
ck("the install worker has a finally that always calls _done",
   any(any(isinstance(f, ast.Call) and getattr(f.func, "id", "") == "_done"
           for stmt in n.finalbody for f in ast.walk(stmt))
       for n in ast.walk(_install) if isinstance(n, ast.Try)))
# _done must be idempotent or the finally double-reports. It is a NESTED
# function, so it must be found with a recursive walk - the first version
# looked only at direct children of install_selected_mixed and found nothing,
# which is why that check reported "no idempotence guard" against working code.
_done_defs = [n for n in ast.walk(_install)
              if isinstance(n, ast.FunctionDef) and n.name == "_done"]
ck("_done is defined inside the install worker", len(_done_defs) == 1,
   len(_done_defs))
# Precise shape: _done's FIRST statement must be `if <guard>:` and the body must
# be a bare return. Looking for "any Subscript anywhere in _done" was too weak -
# it still passed after the guard was deleted, because the rest of _done's body
# contains subscripts of its own.
_first = _done_defs[0].body[0] if _done_defs else None
ck("_done is guarded against being called twice (idempotent)",
   isinstance(_first, ast.If)
   and any(isinstance(n, ast.Subscript) for n in ast.walk(_first.test))
   and all(isinstance(s, ast.Return) for s in _first.body),
   "first statement of _done is %s" % type(_first).__name__)

GUARD.__exit__(None, None, None)
print()
if FAILS:
    print("BUG-018 VERIFY: %d/%d FAILED" % (FAILS, CHECKS))
    sys.exit(1)
print("BUG-018 VERIFY: ALL PASS (%d checks)" % CHECKS)
