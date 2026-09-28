"""H11 targeted verification: tweak_state is a read-only LIVE view.

The bug: `Application.tweak_state` was a snapshot dict refreshed by hand at
four sites. A run finishing through any other path left the applied badges
showing pre-run state, and TaskTab._context_applied_count carried a comment
admitting the cache "can lag a run-completion hop".

Checks:
  1  the attribute is a property, not a stored dict
  2  it cannot be written
  3  it re-reads on every access, so it cannot go stale
  4  a registry change is visible immediately, with no invalidation call
  5  it returns a fresh dict, so a caller cannot corrupt the registry
  6  it never raises
  7  the module-global patch point still works (smoke_async depends on it)
  8  TaskTab no longer keeps its own copy
"""
from __future__ import annotations

import ast
import io
import sys

sys.path.insert(0, r"C:\Users\User\Desktop\Cleaner Tool")

import tkinter as tk

import app.ui.app as appmod
import app.ui.tabs.tasktab as tabmod
from app.elevation import is_admin

FAILS = []
N = [0]


def ck(label, cond, detail=""):
    N[0] += 1
    if cond:
        print("  PASS  %s" % label)
    else:
        FAILS.append(label)
        print("  FAIL  %s%s" % (label, ("  <- " + str(detail)) if detail else ""))


# ------------------------------------------------------------------ static --
print("\n[1] declared as a property, in the source")
src = io.open(r"C:\Users\User\Desktop\Cleaner Tool\app\ui\app.py",
              encoding="utf-8").read()
tree = ast.parse(src)
appcls = next(n for n in tree.body
              if isinstance(n, ast.ClassDef) and n.name == "Application")
prop = None
for m in appcls.body:
    if isinstance(m, ast.FunctionDef) and m.name == "tweak_state":
        prop = m
ck("Application.tweak_state is defined", prop is not None)
if prop is not None:
    decos = []
    for d in prop.decorator_list:
        decos.append(d.id if isinstance(d, ast.Name) else getattr(d, "attr", ""))
    ck("it carries @property", "property" in decos, decos)
    ck("it has no setter (read-only)", "@tweak_state.setter" not in src
       and "tweak_state.setter" not in src)
# and nothing assigns to it anywhere
writers = []
for p in ("app/ui/app.py", "app/ui/tabs/tasktab.py", "app/ui/tabs/installtab.py",
          "app/ui/tabs/toolstab.py"):
    t = ast.parse(io.open(r"C:\Users\User\Desktop\Cleaner Tool" + "\\" +
                          p.replace("/", "\\"), encoding="utf-8").read())
    for n in ast.walk(t):
        if isinstance(n, ast.Assign):
            for tg in n.targets:
                if isinstance(tg, ast.Attribute) and tg.attr == "tweak_state":
                    writers.append("%s:%d" % (p, n.lineno))
ck("no module assigns to .tweak_state", not writers, writers)

# ------------------------------------------------------------------- live ---
print("\n[2-7] behaviour on a real Application")
root = tk.Tk()
root.withdraw()
real = appmod.Application(root)
if not is_admin():
    for w in root.winfo_children():
        if isinstance(w, appmod.AdminGateFrame):
            w._continue_limited()
            break

try:
    ck("reading it returns a dict", isinstance(real.tweak_state, dict),
       type(real.tweak_state))
    ck("a fresh object each access (not a stored dict)",
       real.tweak_state is not real.tweak_state)

    print("\n[2] it cannot be written")
    try:
        real.tweak_state = {"forged": True}
        wrote = True
    except AttributeError:
        wrote = False
    ck("assignment raises AttributeError", wrote is False)
    ck("...and the forged value did not take",
       "forged" not in (real.tweak_state or {}))

    print("\n[3-4] it cannot go stale")
    # drive the real registry through its public writer
    from app.config_persist import mark_tweak_applied, get_tweak_state
    KEY = "h11_readonly_probe"
    mark_tweak_applied(KEY)
    try:
        ck("a registry write is visible with no invalidation call",
           KEY in real.tweak_state, sorted(real.tweak_state)[:6])
        ck("the view agrees with the live registry",
           set(real.tweak_state) == set(get_tweak_state() or {}))

        # this is the exact H11 bug: change the registry behind the app's back
        mark_tweak_applied("h11_second_probe")
        ck("a second write is visible too",
           "h11_second_probe" in real.tweak_state)
        from app.config_persist import mark_tweak_reverted
        mark_tweak_reverted("h11_second_probe")
        ck("a revert is visible with no invalidation call",
           "h11_second_probe" not in real.tweak_state)
    finally:
        for k in (KEY, "h11_second_probe"):
            try:
                mark_tweak_reverted(k)
            except Exception:
                pass
    ck("cleanup removed the probe keys", KEY not in real.tweak_state)

    print("\n[5] a caller cannot corrupt anything")
    snapshot = dict(real.tweak_state)
    stolen = real.tweak_state
    stolen["injected"] = True
    stolen.clear()
    ck("mutating the returned dict does not affect the next read",
       dict(real.tweak_state) == snapshot, (snapshot, real.tweak_state))
    ck("and the live registry is untouched",
       set(get_tweak_state() or {}) == set(snapshot))

    print("\n[6] it never raises")
    orig = appmod.get_tweak_state
    appmod.get_tweak_state = lambda: (_ for _ in ()).throw(OSError("boom"))
    try:
        ck("a failing registry read yields {}", real.tweak_state == {},
           real.tweak_state)
    finally:
        appmod.get_tweak_state = orig
    ck("recovers once the registry works again",
       isinstance(real.tweak_state, dict))

    print("\n[7] the module-global patch point (smoke_async relies on it)")
    appmod.get_tweak_state = lambda: {"patched_key": True}
    try:
        ck("patching app.ui.app.get_tweak_state changes the view",
           real.tweak_state == {"patched_key": True}, real.tweak_state)
    finally:
        appmod.get_tweak_state = orig

    print("\n[8] TaskTab keeps no copy of its own")
    tsrc = io.open(r"C:\Users\User\Desktop\Cleaner Tool\app\ui\tabs\\tasktab.py",
                   encoding="utf-8").read()
    ck("tasktab does not import get_tweak_state",
       "get_tweak_state" not in tsrc)
    ck("tasktab does not assign app.tweak_state",
       ".tweak_state =" not in tsrc)
    ck("tasktab reads app.tweak_state instead",
       "self.app.tweak_state" in tsrc)

finally:
    try:
        root.destroy()
    except Exception:
        pass

print()
if FAILS:
    print("H11 VERIFY: %d/%d FAILED -> %s" % (len(FAILS), N[0], FAILS))
    sys.exit(1)
print("H11 VERIFY: ALL PASS (%d checks)" % N[0])
