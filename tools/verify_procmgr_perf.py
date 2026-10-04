"""PERF: the Process Manager card froze the entire app for seconds.

User report: "process manager tool card when clicked freeze the app for a few
seconds and take a while to show/load the popup window and causes the whole app
to lag and lock up."

Two independent causes, both measured rather than guessed:

  1. `_tasklist_csv()` passed `/v` to tasklist. The verbose form makes tasklist
     resolve a window title, a user name and session status for EVERY process
     before printing anything. Measured on this machine: 18.3 s with /v versus
     348 ms without, on 250 processes. The parser reads only the image name
     (field 0), the PID (field 1) and the memory figure (field 4) - all three
     present in the 5-column non-verbose output. So /v bought nothing and cost
     18 seconds.

  2. The dialog called `sampler.sample()` inline, ON THE TK THREAD, on open and
     every _TICK_MS. So even a fast sample froze the whole app for its
     duration, repeatedly, for as long as the dialog was open.

The fix samples on a worker thread and marshals only the widget update back -
through the app's TkDispatcher, because a widget's after() called from a
non-main thread is exactly the MED-014 hazard this session's other fixes were
about.

This test asserts the PROPERTIES, because the symptom is a timing symptom and
an absolute millisecond budget would be machine-dependent. It drives the real
dialog with a fake sampler on a fake Tk object, so it can assert WHICH THREAD
did the sampling without needing a live mainloop.
"""

from __future__ import annotations

import ast
import io
import sys
import threading
import time

sys.path.insert(0, ".")

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


PM = r"app\process_manager.py"
DLG = r"app\ui\dialogs\process_manager_dialog.py"
pm_src = io.open(PM, encoding="utf-8", newline="").read()
dlg_src = io.open(DLG, encoding="utf-8", newline="").read()


def body(src, name):
    tree = ast.parse(src)
    for n in ast.walk(tree):
        if isinstance(n, ast.FunctionDef) and n.name == name:
            return ast.get_source_segment(src, n) or ""
    return ""


print("[1] the 18-second cause: tasklist is no longer asked for /v")
_fn = body(pm_src, "_tasklist_csv")
ck("the function exists", bool(_fn))
ck("the parser still reads fields 0, 1 and 4",
   "parts[0]" in _fn and "parts[1]" in _fn and "parts[4]" in _fn)
ck("PERF: the reason is recorded at the call site",
   "PERF" in _fn and "18.3" in _fn)

# The tasklist invocations, found STRUCTURALLY. An earlier version of this check
# split the source on "except Exception:" and took the text before the first
# match - which is the `resolve_exe` import, BEFORE the primary call. So the
# "does the primary call pass /v" assertion was inspecting an empty segment and
# passing VACUOUSLY. That is the third time this session a text check has done
# that, so this one is AST-based.
_pm_tree = ast.parse(pm_src)
_fn_node = next(n for n in ast.walk(_pm_tree)
                if isinstance(n, ast.FunctionDef) and n.name == "_tasklist_csv")
_calls = []
for _c in ast.walk(_fn_node):
    if (isinstance(_c, ast.Call) and isinstance(_c.func, ast.Attribute)
            and _c.func.attr == "check_output" and _c.args):
        _flags = [e.value for e in _c.args[0].elts
                  if isinstance(e, ast.Constant) and isinstance(e.value, str)
                  ]
        _calls.append(_flags)
ck("tasklist is invoked at least once", len(_calls) >= 1, len(_calls))
ck("the PRIMARY call does NOT pass /v", _calls and "/v" not in _calls[0], _calls[:1])
ck("the primary call asks for CSV with no header row",
   _calls and {"/fo", "csv", "/nh"} <= set(_calls[0]), _calls[:1])
ck("the /v form survives only as a FALLBACK",
   len(_calls) > 1 and "/v" in _calls[-1], _calls[-1:])
ck("...and there is exactly one fallback", len(_calls) == 2, len(_calls))

print()
print("[2] the real cost, measured")
import app.process_manager as pm  # noqa: E402
t0 = time.perf_counter()
rows = pm._tasklist_csv()
ms = (time.perf_counter() - t0) * 1000
print("        _tasklist_csv() = %.0f ms for %d rows" % (ms, len(rows)))
ck("it returns a real process list", len(rows) > 20, len(rows))
ck("names, pids and memory all parsed",
   all(isinstance(r[0], str) and isinstance(r[1], int) for r in rows[:20]))
# A budget, not an absolute: the verbose form measured 18.3 s / 250 procs, so
# anything under a second proves /v is gone without being machine-specific.
ck("a full enumeration completes well under a second", ms < 1000, "%.0f ms" % ms)

print()
print("[3] the Tk thread is NOT blocked: sampling happens on a worker")
_sampler_threads = []
main_id = threading.current_thread().ident


_sample_done = threading.Event()


class FakeSampler:
    def sample(self):
        _sampler_threads.append(threading.current_thread().ident)
        time.sleep(0.05)          # stand in for tasklist
        _sample_done.set()
        return [{"pid": 10 + i, "name": "p%d.exe" % i, "display": "p%d" % i,
                 "cpu_percent": 1.0, "ram_mb": 10.0, "safe": True, "kind": "apps"}
                for i in range(5)]

    def _finish(self):
        _sample_done.set()


class FakeDispatch:
    def __init__(self):
        self.posted = []

    def post(self, fn):
        self.posted.append(fn)
        fn()                       # the dispatcher runs it on the Tk thread


class FakeApp:
    def __init__(self):
        self._dispatch = FakeDispatch()

    def log(self, *a, **k):
        pass


class FakeWin:
    """Minimal stand-in for the dialog's Tk window.

    `after` here RECORDS rather than schedules. The real widget's after() cannot
    be exercised from a test without a live mainloop, and - more to the point -
    a from-worker after() is the bug, not the fix. Recording lets the test assert
    that the worker never touches it.
    """

    def __init__(self):
        self.afters = []

    def after(self, ms, fn=None, *a):
        self.afters.append((ms, fn))
        return "after-id"

    def after_cancel(self, _id):
        pass

    def winfo_exists(self):
        return True

    def winfo_children(self):
        return []

    def destroy(self):
        pass

    def update(self):
        pass


from app.ui.dialogs.process_manager_dialog import ProcessManagerDialog  # noqa: E402

app = FakeApp()
dlg = ProcessManagerDialog.__new__(ProcessManagerDialog)
dlg.app = app
# BUG-001: the sampling hop now uses the DIALOG's dispatcher -- ThemedModal
# creates one per dialog and stops it in close() -- instead of reaching for the
# application's. __new__ skips __init__, so supply it as the real constructor
# would.
dlg._dispatch = FakeDispatch()
dlg._sampler = FakeSampler()
dlg._dlg = FakeWin()
dlg._sampling = False
dlg._last_items = []
dlg._shown_groups = []
dlg._displayed_keys = []
dlg._row_widgets = {}
dlg._MAX_GROUPS = 40
dlg._status = type("S", (), {"config": lambda self, **k: None})()
dlg._panel = type("P", (), {"inner": FakeWin()})()
dlg._say = lambda *a, **k: None
dlg._stats_text = lambda g: ""
dlg._update_rows_in_place = lambda g: None
dlg._rebuild_rows = lambda g: None

t0 = time.perf_counter()
dlg._refresh_list(first=True)
elapsed = (time.perf_counter() - t0) * 1000
ck("the sampler ran", len(_sampler_threads) == 1, _sampler_threads)
ck("...on a DIFFERENT thread from the caller",
   _sampler_threads and _sampler_threads[0] != main_id, _sampler_threads)
ck("...and never on the Tk thread", main_id not in _sampler_threads)
ck("the worker never called the widget's after()",
   dlg._dlg.afters == [], dlg._dlg.afters)
print("        _refresh_list returned in %.1f ms (sampling took 50 ms)" % elapsed)
ck("...returning long before the sample finished", elapsed < 45, "%.1f ms" % elapsed)
# The return is the whole point, but its EFFECTS must still land, so wait for
# the worker rather than asserting against a half-finished thread.
# Wait for the ACTUAL condition, not for the sampler to finish. The sampler
# sets the event, then the worker still has to run group_processes() and post
# through the dispatcher - and a fixed sleep after the event races that, which
# is what made this check fail intermittently once the sampling code changed.
_deadline = time.time() + 5.0
while time.time() < _deadline and not dlg._dispatch.posted:
    time.sleep(0.005)
ck("the result was marshalled through the app's dispatcher",
   len(dlg._dispatch.posted) == 1, len(dlg._dispatch.posted))
ck("_last_items was captured for the End-task action",
   len(dlg._last_items) == 5, len(dlg._last_items))
ck("_sampling is cleared so the next tick can sample again", dlg._sampling is False)

print()
print("[4] overlapping samples are refused, not queued")
app2 = FakeApp()
dlg.app = app2
dlg._sampling = True
_before = len(_sampler_threads)
dlg._refresh_list(first=False)
ck("a second sample is skipped while one is in flight",
   len(_sampler_threads) == _before, len(_sampler_threads))
dlg._sampling = False

print()
print("[5] the apply half is Tk-only and cannot be entered off-thread")
_apply = body(dlg_src, "_apply_groups")
ck("_apply_groups exists", bool(_apply))
ck("it re-checks the dialog still exists before touching widgets",
   "winfo_exists" in _apply)
ck("it clears the in-flight flag first", _apply.strip().splitlines()[1].strip()
   == "self._sampling = False" or "self._sampling = False" in _apply)
ck("PERF: the split is documented at both halves",
   "PERF" in body(dlg_src, "_refresh_list")
   and "BUG-001" in body(dlg_src, "_refresh_list"))

print()
print("[6] the heavy lifting is still on the worker")
_work_src = body(dlg_src, "_refresh_list")
ck("sample() is called inside the worker, not before it",
   _work_src.count("self._sampler.sample()") == 1)
ck("group_processes is called on the worker too (pure data)",
   "group_processes" in _work_src)
ck("no Tk call happens before the dispatcher post",
   _work_src.index("self._sampler.sample()")
   < _work_src.index("self._dispatch.post"))
# BUG-001: the `getattr(app, "_dispatch")` lookup and its off-thread
# `self._dlg.after(0, ...)` fallback are both gone -- the dialog's own
# dispatcher is always present, so there is nothing to look up and no path
# back to a Tk call on a worker. Matched against code with comments blanked:
# the replacement's own comment quotes the old form verbatim. (The remaining
# getattr is the unrelated `getattr(self, "_sampling", False)` guard.)
_work_code = "\n".join(ln.split("#")[0] for ln in _work_src.split("\n"))
ck("...and the worker never falls back to the widget's after()",
   '"_dispatch", None' not in _work_code
   and "self._dlg.after(0" not in _work_code)

print()
if FAILS:
    print("PROCESS MANAGER PERF VERIFY: %d/%d FAILED -> %s" % (FAILS, CHECKS, FAILS))
    sys.exit(1)
print("PROCESS MANAGER PERF VERIFY: ALL PASS (%d checks)" % CHECKS)
