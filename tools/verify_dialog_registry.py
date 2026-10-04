"""The dialog registry must stay complete and callable.

Run:  python -u tools/verify_dialog_registry.py
Exit 0 = clean, 1 = at least one failure.

Why this exists. Blueprint A replaced twenty near-identical
`Application._open_*` methods with one table plus a generic opener. The
smoke suite clicks most of those entry points, but it clicks them through the
UI, so a table entry that names a dialog class which no longer exists fails
as "the dialog did not appear" rather than "the registry is wrong".

These checks pin the contract directly:
  * every Application forwarder resolves to a registry key;
  * every registry key has a forwarder (no orphan table entry);
  * every registered class is a real dialog class;
  * the two special cases still behave (busy-guard, third-arg + abort).
"""

from __future__ import annotations

import ast
import inspect
import io
import pathlib
import sys

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


print("DIALOG REGISTRY VERIFY")

from app.ui.dialogs import registry  # noqa: E402

print("\n[1] the table is well formed")
ck(len(registry.DIALOGS) >= 19, "every feature dialog is registered",
   len(registry.DIALOGS))
for key, spec in sorted(registry.DIALOGS.items()):
    ck(isinstance(key, str) and key, "key %r is a non-empty string" % key)
    ck(inspect.isclass(spec.cls),
       "%-18s -> a class (%s)" % (key, spec.cls.__name__))
    ck(spec.guard_busy in (True, False),
       "%-18s guard_busy is a bool" % key)
    ck(spec.extra_arg is None or callable(spec.extra_arg),
       "%-18s extra_arg is None or a resolver" % key)

print("\n[2] Application's forwarders and the table agree, both ways")
src = io.open(ROOT / "app" / "ui" / "app.py", encoding="utf-8").read()
tree = ast.parse(src)
cls = next(n for n in tree.body
           if isinstance(n, ast.ClassDef) and n.name == "Application")

forwarders = {}
for m in cls.body:
    if isinstance(m, ast.FunctionDef) and m.name.startswith("_open_"):
        # open_dialog(self, "key") -- the key is whichever argument is a
        # string constant; the first argument is `self`.
        for c in ast.walk(m):
            if not (isinstance(c, ast.Call) and isinstance(c.func, ast.Name)
                    and c.func.id == "open_dialog"):
                continue
            for a in c.args:
                if isinstance(a, ast.Constant) and isinstance(a.value, str):
                    forwarders[m.name] = a.value
                    break

ck(len(forwarders) >= 19, "Application kept a forwarder per dialog",
   len(forwarders))

unknown = {m: k for m, k in forwarders.items() if k not in registry.DIALOGS}
ck(not unknown, "every forwarder names a registered dialog", unknown)
orphans = sorted(set(registry.DIALOGS) - set(forwarders.values()))
ck(not orphans, "every registered dialog has a forwarder", orphans)

print("\n[3] the two special cases still behave")


class _FakeApp:
    """Just enough Application for the opener: a root, a busy flag, a
    status sink and the tabs dict the Quick Tools resolver reads."""
    def __init__(self, busy=False, tools_target="SOMETHING"):
        import threading
        self.root = object()
        self._busy_lock = threading.Lock()
        self._busy = busy
        self.status = []
        self.set_status = self.status.append
        self.opened = []

        class _Tab:
            _open_target = tools_target
        self.tabs = {"Tools": _Tab()}


def _recorder(key):
    """A stand-in dialog class that records that it was opened.

    Needs an __init__ that swallows the (root, app) arguments the opener
    passes -- object.__init__ takes none, and open_dialog swallows the
    resulting TypeError, which looks exactly like "did not open".
    """
    def __init__(self, *a, **k):
        pass
    def wait(self):
        opened.append(key)
    return type("Recorder", (), {"__init__": __init__, "wait": wait})


print("\n[4] a busy run blocks the guarded dialog and no other")
# DialogSpec is a namedtuple, so swap whole specs in the table rather than
# trying to assign to a field.
_real_dialogs = dict(registry.DIALOGS)
opened = []
try:
    for key, spec in sorted(_real_dialogs.items()):
        recorder = _recorder(key)
        registry.DIALOGS[key] = spec._replace(cls=recorder)
        app = _FakeApp(busy=True)
        registry.open_dialog(app, key)
        if spec.guard_busy:
            ck(key not in opened,
               "%-18s refused while busy" % key)
            ck(any("Busy" in m for m in app.status),
               "%-18s said so in the status bar" % key, app.status)
        else:
            ck(key in opened, "%-18s opened anyway" % key)
finally:
    registry.DIALOGS.clear()
    registry.DIALOGS.update(_real_dialogs)

print("\n[5] Quick Tools opens nothing when the Tools tab has no target")
app = _FakeApp(tools_target=None)
ck(registry._quick_tools_target(app) is registry.ABORT,
   "the resolver returns ABORT when there is no target")
opened.clear()
spec = _real_dialogs["quick_tools"]
registry.DIALOGS["quick_tools"] = spec._replace(cls=_recorder("quick_tools"))
try:
    registry.open_dialog(_FakeApp(tools_target=None), "quick_tools")
    ck(not opened, "and no dialog was constructed")
finally:
    registry.DIALOGS["quick_tools"] = spec

print("\n[6] the registry does not reach upward")
bad = []
for node in ast.walk(ast.parse(io.open(registry.__file__, encoding="utf-8").read())):
    if isinstance(node, ast.ImportFrom) and node.module:
        if node.module.startswith("app.ui.tabs") or node.module == "app.ui.app":
            bad.append(node.module)
    if isinstance(node, ast.Import):
        for a in node.names:
            if a.name.startswith("app.ui.tabs") or a.name == "app.ui.app":
                bad.append(a.name)
ck(not bad, "registry imports no tabs/app module", bad)

print()
if FAILS:
    print("DIALOG REGISTRY VERIFY: %d FAILURE(S)" % len(FAILS))
    sys.exit(1)
print("DIALOG REGISTRY VERIFY: ALL PASS (%d dialogs)" % len(registry.DIALOGS))
sys.exit(0)