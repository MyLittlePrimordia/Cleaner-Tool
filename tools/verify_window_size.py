"""Pin the work-area window sizing rules (app/config.py).

Run:  python -u tools/verify_window_size.py
Exit 0 = clean, 1 = at least one failure.

Why this exists: the main window used to open at a hardcoded "1040x800"
in PHYSICAL pixels. On the 2560x1440 development display that is 41% of
the screen width; on a 1366x768 laptop it is 76%. Two users on the same
build saw visibly different apps, and neither matched what the smoke suite
rendered, because the suite never applied the DPI opt-in that both real
launch paths apply (tk scaling 1.33 in tests vs 2.0 in the product).

compute_window_size() is pure precisely so this can be checked without a
display: the sizing rules are the thing most likely to silently regress,
and they are impossible to eyeball in CI.
"""

from __future__ import annotations

import io
import os
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


print("WINDOW SIZE VERIFY")

# --------------------------------------------------------------- pure logic --
print("\n[1] compute_window_size scales with the work area")
from app.config import (  # noqa: E402
    WINDOW_DEFAULT_SIZE, WINDOW_MAX_SIZE, WINDOW_MIN_SIZE, WINDOW_SIZE,
    compute_window_size,
)

small = compute_window_size(1366, 728)
large = compute_window_size(2560, 1400)
ck(large[0] > small[0] and large[1] > small[1],
   "a bigger screen yields a bigger window",
   "1366x728 -> %s ; 2560x1400 -> %s" % (small, large))
ck(small != (1040, 800),
   "the old hardcoded 1040x800 is no longer returned verbatim on a small "
   "screen", "got %s" % (small,))

print("\n[2] clamping to [minimum, maximum]")
cases = [
    # (work area, why)
    ((3840, 2160), "4K must not open a 3000px window"),
    ((800, 600), "tiny screen must still open usable"),
    ((0, 0), "zero work area"),
    ((-100, -100), "negative work area"),
]
for work, why in cases:
    got = compute_window_size(*work)
    ok = (WINDOW_MIN_SIZE[0] <= got[0] <= WINDOW_MAX_SIZE[0]
          and WINDOW_MIN_SIZE[1] <= got[1] <= WINDOW_MAX_SIZE[1])
    ck(ok, "clamped for %sx%s (%s)" % (work[0], work[1], why),
       "got %s, outside [%s, %s]" % (got, WINDOW_MIN_SIZE, WINDOW_MAX_SIZE))

ck(compute_window_size(3840, 2160) == WINDOW_MAX_SIZE,
   "4K clamps exactly to WINDOW_MAX_SIZE %s" % (WINDOW_MAX_SIZE,),
   "got %s" % (compute_window_size(3840, 2160),))
ck(compute_window_size(800, 600) == WINDOW_MIN_SIZE,
   "a sub-minimum screen clamps exactly to WINDOW_MIN_SIZE %s"
   % (WINDOW_MIN_SIZE,), "got %s" % (compute_window_size(800, 600),))

print("\n[3] garbage input never raises (a geometry call must not crash)")
for bad in (None, "", "abc", 3.5, object()):
    try:
        got = compute_window_size(bad, 1000)
        ok = (isinstance(got, tuple) and len(got) == 2
              and all(isinstance(v, int) for v in got))
    except Exception as exc:                       # noqa: BLE001
        ok, got = False, repr(exc)
    ck(ok, "work_w=%r returns a usable size" % (bad,), got)

print("\n[4] the pure function has no side effects")
ck(WINDOW_SIZE == "%dx%d" % WINDOW_DEFAULT_SIZE,
   "WINDOW_SIZE still equals the default size as a string",
   "%r vs %r" % (WINDOW_SIZE, WINDOW_DEFAULT_SIZE))
ck(WINDOW_MIN_SIZE == (980, 700),
   "WINDOW_MIN_SIZE unchanged (the chrome.py resize-grip clamp)", WINDOW_MIN_SIZE)

# ------------------------------------------------------- wiring / contract --
print("\n[5] the legacy app.gui contract still resolves")
try:
    import app.gui as gui
    missing = [n for n in ("WINDOW_SIZE", "WINDOW_MIN_SIZE", "COLORS", "F",
                           "Application", "launch")
               if not hasattr(gui, n)]
    ck(not missing, "app.gui re-exports the sizing + core names", missing)
except Exception as exc:                           # noqa: BLE001
    ck(False, "app.gui imports", repr(exc))

print("\n[6] app.py sizes from the work area, not a literal")
try:
    src = io.open(ROOT / "app" / "ui" / "app.py", encoding="utf-8").read()
    ck("resolve_window_size" in src,
       "app.py calls resolve_window_size()")
    ck('self.root.geometry("' not in src.replace("'", '"'),
       "app.py no longer hardcodes a geometry literal")
except Exception as exc:                           # noqa: BLE001
    ck(False, "app.py readable", repr(exc))

# ----------------------------------------------------------------- DPI wiring --
print("\n[7] every launch path and harness applies the DPI opt-in")
from app.dpi import enable_dpi_awareness, is_dpi_aware  # noqa: E402

ck(is_dpi_aware(),
   "this process reports DPI-aware (the harness opted in before Tk)")
ck(enable_dpi_awareness() in (True, False),
   "enable_dpi_awareness() returns a bool and does not raise")

for rel in ("app/__main__.py", "tools/smoke_gui.py", "tools/smoke_async.py"):
    try:
        text = io.open(ROOT / rel, encoding="utf-8").read()
        ck("_enable_dpi_awareness()" in text,
           "%s applies the DPI opt-in" % rel)
    except Exception as exc:                       # noqa: BLE001
        ck(False, "%s readable" % rel, repr(exc))

# The call must precede Tk creation in each file, or it is a no-op.
print("\n[8] the opt-in precedes the first Tk root")
for rel in ("tools/smoke_gui.py", "tools/smoke_async.py"):
    text = io.open(ROOT / rel, encoding="utf-8").read()
    i_dpi = text.find("_enable_dpi_awareness()")
    i_tk = text.find("tk.Tk()")
    ck(i_dpi != -1 and (i_tk == -1 or i_dpi < i_tk),
       "%s: DPI opt-in at offset %d precedes tk.Tk() at %d"
       % (rel, i_dpi, i_tk))

# ------------------------------------------------- proportional dialogs ------
# Feature dialogs used to be a hardcoded 800x600, which meant that after the
# main window grew they read as small letterboxes floating inside it (user
# report). They now take a clamped fraction of the window they open over.
print("\n[9] feature dialogs are sized proportionally to their parent")
from app.ui.base import ThemedModal  # noqa: E402


class _FakeWin:
    def __init__(self, w, h):
        self._w, self._h = w, h

    def winfo_width(self):
        return self._w

    def winfo_height(self):
        return self._h


for pw, ph in [(1280, 900), (1040, 800), (1920, 1080), (980, 700), (1600, 1000)]:
    w, h = ThemedModal._proportional_size(_FakeWin(pw, ph))
    inside = w < pw and h < ph
    within = (ThemedModal.SIZE_MIN[0] <= w <= ThemedModal.SIZE_MAX[0]
              and ThemedModal.SIZE_MIN[1] <= h <= ThemedModal.SIZE_MAX[1])
    ck(inside and within,
       "parent %4dx%-4d -> dialog %4dx%-4d (smaller, and within limits)"
       % (pw, ph, w, h))

ck(ThemedModal._proportional_size(_FakeWin(1, 1))
   == (ThemedModal.WIDTH, ThemedModal.HEIGHT),
   "an unmapped parent (1x1) falls back to %dx%d instead of collapsing"
   % (ThemedModal.WIDTH, ThemedModal.HEIGHT))

frac_w, frac_h = ThemedModal.SIZE_FRACTION
ck(0 < frac_w < 1 and 0 < frac_h < 1,
   "both fractions keep the parent visible around the dialog",
   ThemedModal.SIZE_FRACTION)

print("\n[10] no dialog pins its own WIDTH/HEIGHT any more")
bad = []
for p in sorted((ROOT / "app" / "ui").rglob("*.py")):
    for i, line in enumerate(io.open(p, encoding="utf-8").read().splitlines(), 1):
        stripped = line.strip()
        if stripped.startswith(("WIDTH =", "HEIGHT =")):
            # base.py's own pair is the documented fallback, and only it.
            if p.name != "base.py":
                bad.append("%s:%d %s" % (p.relative_to(ROOT), i, stripped))
ck(not bad, "only ThemedModal defines the fallback footprint", bad)

# The six smoke assertions that used to hardcode (800, 600). Matched on the
# AST, not the text: the replacement helper's own docstring explains what it
# replaced and necessarily quotes the old numbers.
import ast  # noqa: E402

stale = []
for p in sorted((ROOT / "tools").rglob("*.py")):
    tree = ast.parse(io.open(p, encoding="utf-8").read())
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assert):
            continue
        for sub in ast.walk(node.test):
            if (isinstance(sub, ast.Tuple) and len(sub.elts) == 2
                    and all(isinstance(e, ast.Constant) and e.value in (800, 600)
                            for e in sub.elts)):
                stale.append("%s:%d" % (p.relative_to(ROOT), node.lineno))
ck(not stale, "no check still asserts a dialog is the old 800x600", stale)

from tools.smoke import assert_dialog_proportional  # noqa: E402
ck(callable(assert_dialog_proportional),
   "the shared proportionality assertion is available to the suite")

print()
if FAILS:
    print("WINDOW SIZE VERIFY: %d FAILURE(S)" % len(FAILS))
    sys.exit(1)
print("WINDOW SIZE VERIFY: ALL PASS (%d checks)" % 8)
sys.exit(0)