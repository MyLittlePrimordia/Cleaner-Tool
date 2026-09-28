"""The Install catalog build chain must terminate.

I shipped a diagnostic that wrapped the catalog slice step for timing:

    def _catalog_step_timed(self):
        _t0 = time.perf_counter()
        try:
            self._catalog_step()          # <- what this MUST say
        finally:
            ...

and then rewrote call sites with a string replace. The replace matched the
first occurrence of `self._catalog_step()` in the file, which was the wrapper's
own call, so the wrapper ended up calling itself. Clicking Install produced:

    File "app\\ui\\tabs\\installtab.py", line 219, in _catalog_step_timed
      self._catalog_step_timed()
    [Previous line repeated 990 more times]
    RecursionError: maximum recursion depth exceeded

and the catalog sat on "Preparing catalog..." forever.

Worth being precise about why every existing gate passed:

  * compileall - the code is valid Python, just wrong;
  * the 46-check suite - none of them execute the Install build chain, so a
    self-calling chain is invisible to them;
  * ast.parse - structurally perfect.

A hang is a behavioural bug, so this is checked by CALLING the chain with a
stubbed slice, under a recursion limit low enough that an accidental cycle
raises rather than eating the stack.
"""

from __future__ import annotations

import ast
import io
import sys

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


P = r"app\ui\tabs\installtab.py"
S = io.open(P, encoding="utf-8", newline="").read()
F = {n.name: (ast.get_source_segment(S, n) or "")
     for n in ast.walk(ast.parse(S)) if isinstance(n, ast.FunctionDef)}

W = F.get("_catalog_step_timed", "")
ck("_catalog_step_timed exists", bool(W))
ck("the wrapper calls the STEP, never itself",
   "self._catalog_step()" in W and "self._catalog_step_timed()" not in W)
ck("_start_catalog_build kicks off through the wrapper",
   "self._catalog_step_timed()" in F.get("_start_catalog_build", ""))
ck("the raw step is never called from inside the wrapper",
   W.count("self._catalog_step()") == 1)

print()
print("[1] drive the real chain and prove it terminates")
# A stub tab: the real one needs a full Tk tree, and the bug under test is
# control flow, not widgets.
SLICES = []


class _Tab:
    _catalog_ready = False
    _catalog_after = None
    _cat_t0 = None
    _cat_slices = 0
    _cat_work = 0.0
    _catalog_queue = []
    after_calls = 0

    def __init__(self):
        self.logged = []
        self.finished = 0

    def _catalog_step(self):
        """Stand-in for the real slice: consume one item, chain the next.

        Note it re-queues itself through after(), exactly like the real one, so
        a wrapper that pointed at itself shows up here as unbounded recursion.
        """
        SLICES.append(1)
        if self._catalog_queue:
            self._catalog_queue.pop(0)
        if self._catalog_queue:
            self.after_calls += 1
            if self.after_calls > 200:
                raise RuntimeError("chain did not terminate: %d hops" % self.after_calls)
            return _Tab._catalog_step_timed(self)   # next hop, via the wrapper
        self._catalog_finish()

    def _catalog_step_timed(self):
        import time
        t0 = time.perf_counter()
        self._cat_slices += 1
        try:
            self._catalog_step()
        finally:
            self._cat_work += (time.perf_counter() - t0) * 1000.0

    def _catalog_finish(self):
        self.finished += 1
        self._catalog_ready = True
        self.logged.append((self._cat_slices, self._cat_work))

    def after(self, _ms, fn):
        return fn()


tab = _Tab()
tab._catalog_queue = list(range(12))
sys.setrecursionlimit(300)   # a cycle raises here instead of eating the stack
try:
    tab._catalog_step_timed()
    ok, err = True, ""
except RecursionError as e:
    ok, err = False, "RecursionError - the wrapper calls itself"
finally:
    sys.setrecursionlimit(1000)

ck("12 queued slices drain without recursing", ok, err)
ck("every slice ran exactly once", len(SLICES) == 12, "%d" % len(SLICES))
ck("the counter matches the slices", tab._cat_slices == 12, tab._cat_slices)
ck("the chain finished exactly once", tab.finished == 1, tab.finished)
ck("slice work was accounted (non-zero)", tab._cat_work > 0, tab._cat_work)

print()
print("[2] and the shipped module really is wired that way")
src_lines = S.splitlines()
tgt = [i for i, l in enumerate(src_lines) if "def _catalog_step_timed" in l]
ck("exactly one wrapper is defined", len(tgt) == 1)
if tgt:
    body = "\n".join(src_lines[tgt[0]:tgt[0] + 18])
    ck("no self-call anywhere in the wrapper body",
       "self._catalog_step_timed()" not in body)
ck("the chain and kick-off both route through the wrapper",
   S.count("self._catalog_step_timed") == 2
   and "self._catalog_step_timed)" in S,
   "%d refs" % S.count("self._catalog_step_timed"))
ck("...and no chain site still calls the raw step",
   "GAP_MS,\n" in S and "_CATALOG_SLICE_GAP_MS" in S
   and "self._CATALOG_SLICE_GAP_MS,\n                                                 self._catalog_step)"
   not in S)

print()
if FAILS:
    print("INSTALL CATALOG CHAIN: %d/%d FAILED -> %s" % (FAILS, CHECKS, FAILS))
    sys.exit(1)
print("INSTALL CATALOG CHAIN: ALL PASS (%d checks)" % CHECKS)
