"""H9 end-to-end: drive a REAL run through Application and assert the
state machine held, observed from both the Tk thread and the worker.

Complements tools/verify_run_state.py, which checks the enum and the
plumbing in isolation. This one runs an actual task end to end and
watches the transitions:

  * the worker handoff happens from PREFLIGHT, not from IDLE
  * RUNNING never coexists with a missing worker, and IDLE never with
    a live one
  * the run returns to IDLE, drops the worker, and records an outcome

Needs a Tk display, like the smoke suites.
"""
import sys, threading, time
import pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from _config_guard import ConfigGuard  # noqa: E402
import tkinter as tk
from app.runner import RunState
import app.ui.app as appmod
from app import task_keys as _k

root = tk.Tk(); root.withdraw()
_GUARD = ConfigGuard('verify_run_state_e2e.py')
_GUARD.__enter__()
real = appmod.Application(root)

# Non-admin here, so __init__ parks the app behind AdminGateFrame and
# never runs _build_main_ui (no tabs / progress_bar). Pass the gate the
# same way tools/smoke_gui.py does.
from app.elevation import is_admin
if not is_admin():
    for w in root.winfo_children():
        if isinstance(w, appmod.AdminGateFrame):
            w._continue_limited(); break
assert hasattr(real, 'tabs'), 'main UI never built'
seen = []
viol = []
_orig_log = real.log
def spy(msg, *a, **k):
    seen.append(("log", real._state.name))
    return _orig_log(msg, *a, **k)
real.log = spy

# one harmless real task
def _noop(ctx):
    return True


task = type("T", (), {"key": "user_temp_files_probe", "label": "probe",
                      "admin_required": False, "run": staticmethod(_noop),
                      "revert": staticmethod(_noop),
                      "long_running": False, "risk": "safe", "category": "clean",
                      "requires_reboot": False})()

def observe():
    for _ in range(400):
        st = real._state
        seen.append(("poll", st.name))
        # INVARIANT: busy implies a run is claimed; a live worker implies busy
        if st is RunState.RUNNING and real._worker_thread is None:
            viol.append("RUNNING with no worker")
        if st is RunState.PREFLIGHT and real._worker_thread is not None:
            viol.append("PREFLIGHT with a worker already attached")
        if st is RunState.IDLE and real._worker_thread is not None:
            viol.append("IDLE with a live worker")
        if st is RunState.CANCELLING and real._busy is not True:
            viol.append("CANCELLING but _busy False")
        time.sleep(0.005)
        if st is RunState.IDLE and len(seen) > 5:
            return

# Polling cannot reliably see PREFLIGHT: with no warnings it is a
# microsecond-wide window between _claim_run() and _mark_running(). Spy on the
# handoff instead, so the transition is observed deterministically.
handoff = []
_orig_mark = real._mark_running
def _spy_mark(thread):
    handoff.append(real._state.name)
    return _orig_mark(thread)
real._mark_running = _spy_mark

t = threading.Thread(target=observe, daemon=True); t.start()
real.run_tasks("Clean", [task], quiet=True)

# run_tasks returns as soon as the worker is spawned; pump the Tk loop until
# the run actually reports back, otherwise we sample mid-flight.
deadline = time.time() + 60
while time.time() < deadline:
    root.update()
    if real._state is RunState.IDLE and len(seen) > 5:
        break
    time.sleep(0.01)
t.join(5)
root.update()

print("distinct states observed (sampled):", sorted({s for _, s in seen}))
print("state at worker handoff          :", handoff)
print("last outcome           :", real.last_run_outcome)
print("state after run        :", real._state.name)
print("worker after run       :", real._worker_thread)
print("invariant violations   :", viol or "NONE")
names = {n for _, n in seen}          # `seen` holds state NAMES, not enums
checks = [
    ("no invariant violations",        not viol),
    ("worker handoff happened from PREFLIGHT", handoff == ["PREFLIGHT"]),
    ("RUNNING was actually entered",   "RUNNING" in names),
    ("back to IDLE at rest",           real._state is RunState.IDLE),
    ("no worker left behind",          real._worker_thread is None),
    ("outcome recorded",               real.last_run_outcome is RunState.DONE),
    ("_busy view agrees at rest",      real._busy is False),
]
for label, good in checks:
    print("  %-34s %s" % (label, "PASS" if good else "FAIL"))
ok = all(g for _, g in checks)
_GUARD.__exit__(None, None, None)   # restore FIRST, then check it worked
if not _GUARD.verify():
    print('  FAIL  the real config.json was NOT restored')
print()
print("H9 E2E:", "ALL PASS" if ok else "FAIL")
try: root.destroy()
except Exception: pass
sys.exit(0 if ok else 1)
