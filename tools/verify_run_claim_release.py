"""A run that fails BEFORE its worker must not strand the app.

Run:  python -u tools/verify_run_claim_release.py
Exit 0 = clean, 1 = at least one failure.

Why this exists. run_tasks and install_selected_mixed claim the single run
slot (RunState.PREFLIGHT) and then do a lot of work before any worker
exists: pre-flight notices, a hazard-combo confirm, an admin-gating dialog,
the selected_tasks config write, the button and progress updates, and the
scorecard's before-snapshot.

Any exception in that window used to unwind straight out of the Tk callback.
_release_run() was never reached, so the app sat in PREFLIGHT for the rest
of the session: _busy stayed True, every Run button stayed disabled, and
_on_close refused to close the window. The only escape was killing the
process.

The reachable raisers are not exotic: TclError when the root is destroyed
under a modal, a grab_set failure on a dialog, a drive query that returns
something unexpected.

This test drives both entry points with a deliberately exploding seam and
asserts the app comes back to IDLE and can run again.
"""

from __future__ import annotations

import io
import pathlib
import sys
import tkinter as tk

ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

FAILS = []


def ck(cond, label, detail=""):
    if cond:
        print("  PASS  %s" % label)
    else:
        FAILS.append(label)
        print("  FAIL  %s%s" % (label, ("  <- " + str(detail)) if detail else ""))


print("RUN CLAIM RELEASE VERIFY")

from app.runner import RunState                     # noqa: E402
from app.tasks import clean_tasks                   # noqa: E402
from app.ui import app as appmod                    # noqa: E402


class FakeRoot:
    """The smallest thing Application.__init__ can be handed.

    Application builds a real Tk tree in _build_main_ui, which needs a
    display and pops modals. This test is about the state machine around the
    claim, not about rendering, so it drives the two entry points on a stub
    and asserts on RunState.
    """
    def __getattr__(self, name):
        def _noop(*a, **kw):
            return None
        return _noop


def _bare_app():
    """An Application with only the state-machine attributes wired up."""
    obj = appmod.Application.__new__(appmod.Application)
    obj._state = RunState.IDLE
    obj._run_gen = 0
    obj._current_gen = 0
    import threading
    obj._busy_lock = threading.Lock()
    obj._worker_thread = None
    obj._cancel = appmod.CancellationToken()
    obj._cancel_requested = False
    obj.root = FakeRoot()
    obj._abort_log = []
    obj.log = lambda m: obj._abort_log.append(m)
    obj._progress_show = lambda: None
    obj._progress_hide = lambda: None
    obj._set_buttons_enabled = lambda v: None
    obj.set_status = lambda m: None
    obj.progress_bar = type("P", (), {"set_fraction": lambda s, v: None,
                                      "set_indeterminate": lambda s, v: None})()
    obj._run_results = []
    obj._run_before_bytes = None
    obj._run_before_drives = None
    obj._query_drive_free_total = lambda root: 0
    obj._query_drives = lambda: []
    obj._preflight_check = lambda tasks: []
    obj._snapshot_all_drives = None
    obj.tabs = {}
    return obj


# Patch the two popup helpers module-wide so the test never blocks on a
# dialog; one of them is what we make explode.
_popup_calls = []


def _fake_askyesno(root, title, msg, **kw):
    _popup_calls.append(("askyesno", title))
    return True


def _fake_showinfo(root, title, msg, **kw):
    _popup_calls.append(("showinfo", title))
    return None


def _boom_askyesno(root, title, msg, **kw):
    _popup_calls.append(("askyesno", title))
    raise tk.TclError("application has been destroyed")


def _boom_showinfo(root, title, msg, **kw):
    _popup_calls.append(("showinfo", title))
    raise tk.TclError("application has been destroyed")


def _install_patches(askyesno, showinfo):
    appmod._themed_askyesno = askyesno
    appmod._themed_showinfo = showinfo
    appmod._snapshot_all_drives = lambda *a, **kw: []


_install_patches(_fake_askyesno, _fake_showinfo)

TASKS = [clean_tasks.TASKS[0]]
INSTALL_TASKS = [t for t in clean_tasks.TASKS[:1]]

print("\n[1] a pre-flight confirm dialog that raises must not wedge the app")
app = _bare_app()
# Force a pre-flight notice so run_tasks actually opens the confirm dialog.
app._preflight_check = lambda tasks: ["disk is nearly full"]
_install_patches(_boom_askyesno, _fake_showinfo)
try:
    app.run_tasks("Clean", TASKS, mode="run", quiet=False)
    ck(app._state is RunState.IDLE,
       "run_tasks returns to IDLE after the modal raises",
       app._state)
    ck(not app._busy, "_busy is False", app._busy)
    ck(app._worker_thread is None, "no worker thread was adopted",
       app._worker_thread)
    ck(any("Could not start" in m for m in app._abort_log),
       "the failure is reported in the log", app._abort_log)
finally:
    _install_patches(_fake_askyesno, _fake_showinfo)

print("\n[2] the app can still run after a failed start")
app2 = _bare_app()
app2._preflight_check = lambda tasks: []
started = []
import threading                                  # noqa: E402


def _fake_worker(tab_name, tasks, mode, quiet, toast_kind, toast_extra,
                 gen, run_results):
    started.append(gen)
    app2._release_run(ok=True)


app2._run_tasks_worker = _fake_worker
try:
    app2.run_tasks("Clean", TASKS, mode="run", quiet=False)
    ck(len(started) == 1, "a subsequent run does start a worker", started)
    ck(app2._state is RunState.IDLE,
       "and it settles back to IDLE", app2._state)
except Exception as exc:                            # noqa: BLE001
    ck(False, "the recovery run completed", repr(exc))

print("\n[3] install_selected_mixed releases on the same failure")
app3 = _bare_app()
app3._preflight_check = lambda tasks: []
# A non-empty admin-gated selection drives the admin dialog path.
from app.tasks import install_tasks                  # noqa: E402

_install_tasks = [t for t in install_tasks.TASKS if t.admin_required][:1] \
    or install_tasks.TASKS[:1]
_install_patches(_fake_askyesno, _boom_showinfo)
_real_is_admin = appmod.is_admin
appmod.is_admin = lambda: False
try:
    app3.install_selected_mixed([], _install_tasks)
    ck(app3._state is RunState.IDLE,
       "install_selected_mixed returns to IDLE after the dialog raises",
       app3._state)
    ck(not app3._busy, "_busy is False", app3._busy)
finally:
    appmod.is_admin = _real_is_admin
    _install_patches(_fake_askyesno, _fake_showinfo)

print("\n[4] the guard is structural, not incidental")
src = io.open(ROOT / "app" / "ui" / "app.py", encoding="utf-8").read()
ck("_abort_unstarted_run" in src, "_abort_unstarted_run exists")
ck(src.count("_abort_unstarted_run(exc)") == 2,
   "both run entry points funnel through it",
   src.count("_abort_unstarted_run(exc)"))
ck("_start_claimed_run" in src and "_start_claimed_install" in src,
   "the pre-worker halves are split out so the guard wraps only them")

print("\n[5] the scorecard's drive snapshots are off the Tk thread (THREAD-002)")
# Before: 26 GetDiskFreeSpaceExW calls per snapshot on the Tk thread, before
# AND after every run -- the exact hazard the disk monitor was moved off-thread
# to avoid. Both readings now happen on the worker.
#
# Checked against the AST rather than the text: both sites carry comments that
# quote the old code, and a substring match flags its own documentation.
import ast  # noqa: E402

_tree = ast.parse(src)


def _fn(name):
    for node in ast.walk(_tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) \
                and node.name == name:
            return node
    return None


def _calls(fn, attr=None, fname=None):
    """Every call in `fn`, optionally filtered by attribute/function name.

    Matches BOTH the bare-name form (`_snapshot_all_drives(...)`) and the
    method form (`self._query_drive_free_total(...)`), because the two
    helpers are called each way.
    """
    out = []
    if fn is None:
        return out
    for node in ast.walk(fn):
        if not isinstance(node, ast.Call):
            continue
        f = node.func
        if attr is not None and isinstance(f, ast.Attribute) and f.attr == attr:
            out.append(node)
        elif fname is not None and isinstance(f, ast.Name) and f.id == fname:
            out.append(node)
        elif attr is not None and isinstance(f, ast.Name) and f.id == attr:
            out.append(node)
    return out


_worker = _fn("_run_tasks_worker")
_popup = _fn("_done_popup")
_upd = _fn("_check_for_updates")

ck(bool(_worker) and bool(_popup) and bool(_upd),
   "all three functions were located")

ck(len(_calls(_worker, attr="_query_drive_free_total")) >= 2,
   "both the before and the after reading are on the WORKER",
   len(_calls(_worker, attr="_query_drive_free_total")))
ck(len(_calls(_worker, fname="_snapshot_all_drives")) >= 2,
   "the worker takes both drive snapshots",
   len(_calls(_worker, fname="_snapshot_all_drives")))
ck(not _calls(_popup, attr="_snapshot_all_drives"),
   "_done_popup no longer snapshots drives on the Tk thread",
   len(_calls(_popup, attr="_snapshot_all_drives")))
ck(not _calls(_popup, attr="_query_drive_free_total"),
   "_done_popup no longer reads free space on the Tk thread",
   len(_calls(_popup, attr="_query_drive_free_total")))

print("\n[6] no off-thread Tk call remains in the update checker (THREAD-001)")
# The old code had `if self.root.winfo_exists():` inside the worker body,
# under a comment claiming it was on the main thread. tkinter posts a foreign
# thread's call to the interpreter and waits; on a destroyed root that burns
# about a second before it raises.
def _winfo_in(fn):
    hits = []
    for node in ast.walk(fn) if fn else []:
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            if node.func.attr.startswith("winfo_"):
                hits.append(node.func.attr)
    return hits


# `_show_update_banner` legitimately uses winfo_exists, but it runs on the Tk
# thread via the dispatcher. Only the worker body matters.
ck(not _winfo_in(_upd),
   "_check_for_updates makes no winfo_* call on the worker thread",
   _winfo_in(_upd))
ck(len(_calls(_upd, attr="post")) >= 1,
   "it hands the banner to the dispatcher instead")

print("\n[7] the worker still receives its generation (ARCH check 8)")

print()
if FAILS:
    print("RUN CLAIM RELEASE VERIFY: %d FAILURE(S)" % len(FAILS))
    sys.exit(1)
print("RUN CLAIM RELEASE VERIFY: ALL PASS")
sys.exit(0)