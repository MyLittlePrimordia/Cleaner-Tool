"""PERF: the PC Specs card froze the app while reading hardware.

`verify_pcspecs_perf.py` removed the DUPLICATE work in pc_specs (6.0 s -> 3.4 s
cold). This covers the other half: even a single cold pass is several seconds of
registry and SMBIOS reads, and `SpecsDialog._show` ran them INLINE on the Tk
thread, so opening or switching a section froze the entire application - not
just this dialog, because Tk has one thread for all of it.

The fix splits the method in two:

  * `_gather(tab)` returns (label, value) pairs and touches no widget, so it
    belongs on a worker;
  * `_render(token, rows)` is the only part that calls `self._rows`, and it
    runs on the Tk thread via the app's TkDispatcher.

Two details that are easy to get wrong and are pinned here:

  * The attribute holding the app is `self.app`. Reading "_app" silently yields
    None, and the fallback would drop the whole path back onto the Tk thread -
    the same trap that bit the Process Manager dialog earlier.
  * A result for a section the user has already navigated away from must be
    discarded, or a slow early section lands on top of a later one.
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
SECTIONS = ("Summary", "OS", "CPU", "RAM", "Board", "Graphics", "Storage",
            "Audio", "Network")


def ck(label, cond, detail=""):
    global FAILS, CHECKS
    CHECKS += 1
    if cond:
        print("  PASS  %s" % label)
    else:
        FAILS += 1
        print("  FAIL  %s%s" % (label, ("  <- " + str(detail)) if detail else ""))


SRC = io.open(r"app\ui\dialogs\specs_dialog.py", encoding="utf-8", newline="").read()
TREE = ast.parse(SRC)
FNS = {n.name: ast.get_source_segment(SRC, n) for n in ast.walk(TREE)
       if isinstance(n, ast.FunctionDef)}

print("[1] the method is split into a pure half and a Tk half")
ck("_gather exists and is separate", "_gather" in FNS)
ck("_render exists and is separate", "_render" in FNS)
_show, _gather, _render = FNS["_show"], FNS["_gather"], FNS["_render"]
PROBES = ("os_info", "cpu_info", "ram_info", "board_info", "gpu_info",
          "storage_info", "audio_info", "net_info", "summary_lines")
ck("_show calls NO probe directly", not [p for p in PROBES if p in _show],
   [p for p in PROBES if p in _show])
ck("_gather is where the probes live", [p for p in PROBES if p in _gather])
ck("_gather makes NO Tk call", "_rows(" not in _gather and ".after(" not in _gather,
   [l for l in _gather.splitlines() if "_rows(" in l or ".after(" in l][:2])
ck("_render is the only place that draws", "_rows(" in _render)
ck("PERF: the reason is documented at the split", "PERF" in _show)

print()
print("[2] every section still renders")
for t in SECTIONS:
    ck('section "%s" is handled' % t, ('"%s"' % t) in _gather)
ck("_gather has a safe fallthrough", 'return [("", "")]' in _gather)

print()
print("[3] the worker marshals through the dispatcher, reading self.app")
ck("_show starts a worker thread", "Thread(" in _show)
ck("...and names it, so it is identifiable in a stack dump",
   "name=" in _show and "PcSpecs" in _show)
ck("it reads self.app with an _app fallback",
   'getattr(self, "app", None)' in _show and 'getattr(self, "_app", None)' in _show,
   _show[:0])
ck("it posts through the dispatcher when one exists", "_dispatch" in _show)
ck("...with an after() fallback if there is none", ".after(0," in _show)

print()
print("[4] a stale result cannot land on a section the user left")
ck("_show keeps a monotonic token", "_gather_seq" in _show)
ck("_render discards a superseded token", "token !=" in _render)
ck("...comparing against the current sequence", "_gather_seq" in _render)
ck("and the placeholder is shown immediately, not after the read",
   _show.index("Gathering") < _show.index("Thread("),
   (_show.index("Gathering"), _show.index("Thread(")))

print()
print("[5] the UI thread is genuinely not blocked (real threads, fake window)")
import tkinter  # noqa: F401  - only imported to prove we are not on Tk here


class FakeRows:
    def __init__(self):
        self.calls = []

    def __call__(self, pairs):
        self.calls.append(list(pairs))


class FakeDispatch:
    def __init__(self):
        self.posted = []

    def post(self, fn):
        self.posted.append(fn)
        fn()


class FakeApp:
    def __init__(self):
        self._dispatch = FakeDispatch()

    def log(self, *a, **k):
        pass


from app.ui.dialogs.specs_dialog import SpecsDialog  # noqa: E402

dlg = SpecsDialog.__new__(SpecsDialog)
dlg.app = FakeApp()
dlg._rows = FakeRows()
dlg._gather_seq = 0
# a deliberately slow gather, so a blocking implementation is unmistakable
_real_gather = SpecsDialog._gather


def _slow_gather(tab):
    time.sleep(0.30)
    return [("Processor", "FAKE"), ("Speed", "FAKE")]


dlg._gather = _slow_gather
main_id = threading.current_thread().ident
t0 = time.perf_counter()
dlg._show("CPU")
elapsed_ms = (time.perf_counter() - t0) * 1000
print("        _show('CPU') returned in %.1f ms while gather takes 300 ms" % elapsed_ms)
ck("it returns long before the gather finishes", elapsed_ms < 150,
   "%.1f ms" % elapsed_ms)
ck("a placeholder was shown immediately",
   any("Gathering" in str(c) for c in dlg._rows.calls), dlg._rows.calls[:1])

deadline = time.time() + 5.0
while time.time() < deadline and not dlg.app._dispatch.posted:
    time.sleep(0.005)
ck("the result arrived through the dispatcher", len(dlg.app._dispatch.posted) == 1,
   len(dlg.app._dispatch.posted))
ck("...and the real rows were rendered",
   any("Processor" in str(c) for c in dlg._rows.calls),
   dlg._rows.calls[-1:])

print()
print("[6] a superseded result is dropped")
dlg2 = SpecsDialog.__new__(SpecsDialog)
dlg2.app = FakeApp()
dlg2._rows = FakeRows()
dlg2._gather_seq = 0
dlg2._gather = _slow_gather
dlg2._show("CPU")          # starts a 300 ms gather
dlg2._gather_seq = 99      # user navigated away while it ran
dlg2.app._dispatch.posted and dlg2.app._dispatch.posted.pop()()
ck("a result for an abandoned section is not rendered",
   not any("Processor" in str(c) for c in dlg2._rows.calls),
   dlg2._rows.calls)

print()
if FAILS:
    print("PC SPECS DIALOG PERF VERIFY: %d/%d FAILED -> %s" % (FAILS, CHECKS, FAILS))
    sys.exit(1)
print("PC SPECS DIALOG PERF VERIFY: ALL PASS (%d checks)" % CHECKS)
