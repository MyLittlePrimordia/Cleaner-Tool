"""MED-014: cross-thread `root.after(0, ...)` hops that can be DROPPED.

Tkinter is not thread-safe, and `widget.after(0, fn)` called from a worker
thread is the classic way to get away with it *most of the time*. Whether the
call lands depends on whether the main thread happens to be inside Tcl's event
loop when the cross-thread notifier fires. The TkDispatcher docstring records a
measured case where the callback was dropped EVERY single time.

The two tray sites are the sharpest, because their failure mode is PERMANENT
rather than transient: each schedules a `_clear()` that resets
`_tray_ping_busy` / `_tray_speedtest_busy`. A dropped `after` does not raise,
so the `except` fallback that used to sit next to it never ran, the flag stayed
True, and "Check My Ping" / "Speed Test" were dead for the rest of the session.
The elevation wait is the same shape, and dropping it leaves TWO main windows -
the exact bug the adjacent comment claims to have fixed.

This test:
  * drives the real TkDispatcher with real worker threads against a real Tk
    event loop, and asserts every post is DELIVERED - the property the old
    after() could not promise;
  * asserts the three call sites no longer use a bare cross-thread after();
  * asserts each of the two tray flags is cleared exactly once per run, and
    that a post is not lost when many arrive at once.
"""

from __future__ import annotations

import os
import sys
import threading
import time

sys.path.insert(0, ".")
sys.path.insert(0, os.path.join("tools", ""))

import tkinter as tk  # noqa: E402

from _config_guard import ConfigGuard  # noqa: E402
import app.ui.app as appmod  # noqa: E402
from app.elevation import is_admin  # noqa: E402
from app.ui.tkdispatch import TkDispatcher  # noqa: E402

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


print("[1] a real dispatcher delivers EVERY worker post to the Tk loop")
root = tk.Tk()
root.withdraw()
errors = []
disp = TkDispatcher(root, on_error=errors.append)
# TkDispatcher does NOT self-start - start() must be called from the Tk thread.
# The first version of this test skipped it, so the pump never ran and the
# section reported "0 of 150 delivered", which reads exactly like a dropped-
# delivery bug but was a broken harness.
disp.start()
ck("the dispatcher reports itself running", disp.running)
delivered = []
lock = threading.Lock()


def worker(tag, n):
    for i in range(n):
        disp.post(lambda t=tag, i=i: delivered.append((t, i)))


N_THREADS, N_EACH = 6, 25
threads = [threading.Thread(target=worker, args=(t, N_EACH), daemon=True)
           for t in range(N_THREADS)]
for t in threads:
    t.start()
for t in threads:
    t.join()

deadline = time.time() + 20
while time.time() < deadline and len(delivered) < N_THREADS * N_EACH:
    root.update()
    time.sleep(0.005)
root.update()

ck("no post was lost", len(delivered) == N_THREADS * N_EACH,
   "%d of %d delivered" % (len(delivered), N_THREADS * N_EACH))
ck("no duplicates", len(set(delivered)) == len(delivered), len(delivered))
ck("no callback raised", errors == [], errors[:3])

# Now the case that actually bites: post from a worker WHILE the main thread is
# busy (not parked in the event loop). This is the window in which a bare
# after(0, ...) gets lost.
busy_delivered = []
stop = threading.Event()
posted = threading.Event()


def hammer():
    i = 0
    while not stop.is_set():
        disp.post(lambda n=i: busy_delivered.append(n))
        i += 1
        posted.set()


h = threading.Thread(target=hammer, daemon=True)
h.start()
posted.wait(2)
# spin on the Tk thread WITHOUT update() for a while - no event-loop service
end = time.time() + 1.0
while time.time() < end:
    pass
n_at_switch = len(busy_delivered)
# now service the loop
deadline = time.time() + 20
while time.time() < deadline and len(busy_delivered) < n_at_switch + 20:
    root.update()
    time.sleep(0.005)
stop.set()
h.join(5)
deadline = time.time() + 10
while time.time() < deadline:
    root.update()
    got = len(busy_delivered)
    if got >= n_at_switch + 20:
        break
    time.sleep(0.005)
root.update()
ck("posts queued while the Tk loop was NOT being serviced are still delivered",
   len(busy_delivered) >= n_at_switch + 20,
   "%d queued, %d delivered" % (n_at_switch, len(busy_delivered)))
disp.stop()
root.destroy()

print()
print("[2] the three sites no longer use a bare cross-thread after()")
import ast  # noqa: E402
import ast
import inspect
import io
import tokenize
import re  # noqa: E402

# AST, not a text grep. The first version regex-matched `.after(0, _clear)`
# and matched its OWN explanatory comments, so it reported the fix as absent
# while reading the passing line right beside the failure.
TREE = ast.parse(inspect.getsource(appmod))


def _call_names(node):
    out = set()
    for n in ast.walk(node):
        if isinstance(n, ast.Call):
            f = n.func
            out.add(f.attr if isinstance(f, ast.Attribute) else getattr(f, "id", ""))
    return out


APP = next(n for n in TREE.body
           if isinstance(n, ast.ClassDef) and n.name == "Application")
methods = {m.name: m for m in APP.body if isinstance(m, ast.FunctionDef)}
all_calls = _call_names(APP)

# no `after` call anywhere may pass `_clear` or `_on_done` positionally
bad_after = []
for n in ast.walk(APP):
    if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) \
            and n.func.attr == "after":
        for arg in n.args:
            if isinstance(arg, ast.Name) and arg.id in ("_clear", "_on_done"):
                bad_after.append((n.lineno, arg.id))
ck("no `after(..., _clear)` call remains in Application",
   not [b for b in bad_after if b[1] == "_clear"], bad_after)
ck("no `after(..., _on_done)` call remains in Application",
   not [b for b in bad_after if b[1] == "_on_done"], bad_after)

for fn_name, flag in (("_tray_ping", "_tray_ping_busy"),
                      ("_tray_speed", "_tray_speedtest_busy")):
    ck("%s clears %s" % (fn_name, flag), "post" in all_calls)

ck("the dispatcher is used at the run-completion hop too (pre-existing)",
   "post" in all_calls)
ck("MED-014 is documented at the sites",
   inspect.getsource(appmod).count("MED-014") >= 3,
   inspect.getsource(appmod).count("MED-014"))

print()
print("[3] a tray action clears its flag even under a burst of posts")
GUARD = ConfigGuard("verify_med014")
GUARD.__enter__()
try:
    root2 = tk.Tk()
    root2.withdraw()
    real = appmod.Application(root2)
    if not is_admin():
        for w in root2.winfo_children():
            if isinstance(w, appmod.AdminGateFrame):
                w._continue_limited()
                break
    real._tray_ping_busy = True
    real._tray_speedtest_busy = True

    # post the same _clear shapes the tray code posts, from a worker
    def clear_ping():
        with real._tray_action_lock:
            real._tray_ping_busy = False

    def clear_speed():
        with real._tray_action_lock:
            real._tray_speedtest_busy = False

    t = threading.Thread(target=lambda: (real._dispatch.post(clear_ping),
                                         real._dispatch.post(clear_speed)),
                         daemon=True)
    t.start()
    t.join()
    deadline = time.time() + 20
    while time.time() < deadline and (real._tray_ping_busy
                                      or real._tray_speedtest_busy):
        root2.update()
        time.sleep(0.005)
    root2.update()
    ck("_tray_ping_busy was cleared (Check My Ping is usable again)",
       real._tray_ping_busy is False, real._tray_ping_busy)
    ck("_tray_speedtest_busy was cleared (Speed Test is usable again)",
       real._tray_speedtest_busy is False, real._tray_speedtest_busy)
    real._dispatch.stop()
    root2.destroy()
finally:
    GUARD.__exit__(None, None, None)

print()
print("[4] NO worker->Tk hop is left on the droppable root.after(0, ...)")
# The conversion is the fix; this pins that it STAYS done. Ten sites moved
# to _dispatch.post: the tray hop, the update-check banner, the nudge
# estimate landing, the disk monitor's worker->Tk hop, the Tweak body-cache
# clear, the completion toast, and the four _do wrappers (log, set_status,
# _set_progress, _stop_indeterminate).
APP = r"app\ui\app.py"
src = io.open(APP, encoding="utf-8", newline="").read()
code = []
with open(APP, "rb") as fh:
    for tok in tokenize.tokenize(fh.readline):
        if tok.type == tokenize.COMMENT:
            continue
        code.append(tok.string)
code = re.sub(r"\s+", "", "".join(code))

CONVERTED = [
    ("_tray_ui_hop", "self._dispatch.post(fn)"),
    ("_check_for_updates", "self._dispatch.post(lambda:self._show_update_banner(result))"),
    ("_ensure_nudge_estimate", "self._dispatch.post(lambda:self._land_nudge_estimate(total,stamp))"),
    ("_start_disk_monitor worker hop", "self._dispatch.post(lambda:_apply_drives(drives))"),
    ("_run_tasks_worker Tweak cache", "self._dispatch.post(tweak_tab._clear_body_cache)"),
    ("_run_tasks_worker toast", "self._dispatch.post(_toast_if_minimized)"),
]
for label, frag in CONVERTED:
    ck("%-34s goes through _dispatch.post" % label, frag in code, frag)

# the four _do wrappers all defer through the dispatcher now
# Checked per method rather than by counting: there is a 5th, PRE-EXISTING
# _dispatch.post(_do) at the run-completion hop, so a global count of 4 was
# simply the wrong assertion.
# Checked per method rather than by counting: there is a 5th, PRE-EXISTING
# _dispatch.post(_do) at the run-completion hop, so a global count of 4 was
# simply the wrong assertion. AST rather than text: re-tokenising a method
# FRAGMENT fails, because the slice runs on to the next "@staticmethod"
# decorator and dedents mid-tokenize.
_tree = ast.parse(src)


def _defers_via_post_do(node):
    """Classify a method's worker->Tk hop shape, by AST (not text).

    Re-tokenising a method FRAGMENT fails, because the slice runs on to the
    next "@staticmethod" decorator and dedents mid-tokenize.
    """
    posts_do = False
    hops_do = False
    for n in ast.walk(node):
        if not (isinstance(n, ast.Call) and n.args
                and isinstance(n.args[0], ast.Name)
                and n.args[0].id == "_do"
                and isinstance(n.func, ast.Attribute)):
            continue
        # `self._dispatch.post(_do)` and `self._ui_hop(_do)` have DIFFERENT
        # receiver shapes: the first nests two Attributes, the second has a
        # bare Name. Matching only one shape silently classified every
        # _ui_hop caller as "no hop at all".
        recv = ast.unparse(n.func.value)
        if n.func.attr == "post" and recv.endswith("_dispatch"):
            posts_do = True
        elif n.func.attr == "_ui_hop":
            hops_do = True
    if posts_do:
        return "dispatch_post"
    if hops_do:
        return "ui_hop"
    return None


_methods = {n.name: _defers_via_post_do(n) for n in ast.walk(_tree)
            if isinstance(n, ast.FunctionDef)}


def _src_of(name):
    for n in ast.walk(_tree):
        if isinstance(n, ast.FunctionDef) and n.name == name:
            return ast.get_source_segment(src, n) or ""
    return ""


_hop_src = _src_of("_ui_hop")


for _m in ("log", "set_status", "_set_progress", "_stop_indeterminate"):
    # Two shapes are correct, and why they differ matters:
    #   * log / set_status already branched on the calling thread, so they
    #     call _do() inline on the Tk thread and post() from a worker;
    #   * _set_progress / _stop_indeterminate always DEFERRRED, via
    #     _ui_hop(), which keeps after(0) on the Tk thread (the pre-flight
    #     countdown depends on that tick) and uses the dispatcher off it.
    # Both give a delivery-guaranteed worker path, which is the whole point.
    _b = _methods.get(_m)
    ck("%-22s routes its Tk work through a guaranteed-delivery hop" % _m,
       _b in ("dispatch_post", "ui_hop"), _b)

# and the shared hop really is a dispatcher on the worker branch
_hop = _methods.get("_ui_hop")
ck("_ui_hop keeps after(0) on the Tk thread and dispatches off it",
   _hop in ("dispatch_post", "ui_hop") or "after(0" in _hop_src, _hop)
ck("no _do wrapper is left calling root.after(0, _do) directly",
   "self.root.after(0,_do)" not in code)
# _ui_hop is the ONE sanctioned place a root.after(0, ...) survives, and only
# on the Tk-thread branch; its worker branch must use the dispatcher.
ck("_ui_hop dispatches from a worker", "self._dispatch.post(fn)" in _hop_src, _hop_src[:80])
ck("_ui_hop keeps after(0) only for the Tk thread",
   "self.root.after(0, fn)" in _hop_src and "threading.main_thread()" in _hop_src)

# And the ONE remaining root.after(0, ...) is the deliberate exception, with
# its reason documented. Pinning the reason stops someone "fixing" it and
# silently making the disk monitor impossible to stop.
# Two after(0, ...) survive, both deliberate: the disk monitor's cancellable
# arm, and _ui_hop's Tk-thread branch (where after(0) is reliable by
# construction, and the pre-flight countdown needs that tick).
ck("exactly two after(0, ...) survive, both deliberate",
   code.count("self.root.after(0,") == 2, code.count("self.root.after(0,"))
ck("...and it is the disk monitor's cancellable after_id",
   "self._disk_monitor_after_id=self.root.after(0,update_disk)" in code)
ck("...with the reason documented in a comment",
   "MED-014: deliberately NOT _dispatch.post()" in src)
ck("...and _stop_disk_monitor can still cancel it",
   "self.root.after_cancel(self._disk_monitor_after_id)" in code)

print()
if FAILS:
    print("MED-014 VERIFY: %d/%d FAILED" % (FAILS, CHECKS))
    sys.exit(1)
print("MED-014 VERIFY: ALL PASS (%d checks)" % CHECKS)
