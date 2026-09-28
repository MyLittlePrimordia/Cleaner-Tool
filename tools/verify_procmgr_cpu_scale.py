"""BUG: Process Manager reported per-process CPU on a different scale to Windows
Task Manager, so the app looked broken next to the thing users compare it to.

User report, with a screenshot: the top row read "162% CPU" for a game while
Task Manager read 10.8% for the same process at the same moment.

The cause was not a calculation error but a SCALE error. The sampler worked out
CPU time consumed as a percentage of ONE core, then capped the display at
100 * n_cores - 1600% on a 16-core machine. Task Manager normalises across every
core, so 100% there means "the whole machine". The two numbers described the
same load on different scales: 162 / 16 = 10.1, which is the reading the user
saw in Task Manager.

This test pins the SCALE, because that is the part that was wrong and the part a
plausible-looking formula would get wrong again. It uses the pure helper rather
than live process timing, so it is deterministic and machine-independent.
"""

from __future__ import annotations

import ast
import io
import re
import tokenize
import sys

sys.path.insert(0, ".")

from app.process_manager import _process_cpu_percent as pct  # noqa: E402

FAILS = 0
CHECKS = 0
CORES = 16                       # the user's machine, and the reported case
TICKS_PER_SEC = 10_000_000.0     # 100ns units


def ck(label, cond, detail=""):
    global FAILS, CHECKS
    CHECKS += 1
    if cond:
        print("  PASS  %s" % label)
    else:
        FAILS += 1
        print("  FAIL  %s%s" % (label, ("  <- " + str(detail)) if detail else ""))


print("[1] THE REPORTED CASE: 162%% of one core is ~10%% of a 16-core machine")
got = pct(1.62 * TICKS_PER_SEC, 1.0, CORES)
print("        1.62 cores over 1s on %d cores -> %.1f%%" % (CORES, got))
ck("lands near Task Manager's 10.8%, not 162%", 9.0 <= got <= 11.5, got)
ck("is nowhere near the old 162% reading", got < 20, got)

print()
print("[2] it is on the same 0-100 scale Task Manager uses")
ck("one full core on an otherwise idle machine reads 100/CORES",
   abs(pct(1 * TICKS_PER_SEC, 1.0, CORES) - 100.0 / CORES) < 1e-6,
   pct(1 * TICKS_PER_SEC, 1.0, CORES))
ck("saturating every core reads 100", pct(CORES * TICKS_PER_SEC, 1.0, CORES) == 100.0,
   pct(CORES * TICKS_PER_SEC, 1.0, CORES))
ck("half a core reads 50/CORES", abs(pct(0.5 * TICKS_PER_SEC, 1.0, CORES) - 3.125) < 1e-6,
   pct(0.5 * TICKS_PER_SEC, 1.0, CORES))

print()
print("[3] the old 'percent of one core' formula is gone from the sampler")
src = io.open(r"app\process_manager.py", encoding="utf-8", newline="").read()
tree = ast.parse(src)
fn = next(n for n in ast.walk(tree)
          if isinstance(n, ast.FunctionDef) and n.name == "_sample_unlocked")
body = ast.get_source_segment(src, fn) or ""
ck("the sampler calls the normalising helper", "_process_cpu_percent(" in body)
# Strip comments before looking for the old cap. The comment explaining the
# removal QUOTES `min(cpu_pct, 100.0 * n_cores)`, so a plain substring search
# finds it and the check passes/fails on documentation rather than code. That is
# the fifth time this session a text check has been fooled by the very thing it
# was documenting.
def _code_only(text):
    out = []
    for tok in tokenize.tokenize(io.BytesIO(text.encode()).readline):
        if tok.type in (tokenize.COMMENT, tokenize.STRING) or tok.type == tokenize.NL:
            continue
        out.append(tok.string)
    return re.sub(r"\s+", "", "".join(out))


_body_code = _code_only(body)
ck("the 100*n_cores cap is not EXECUTED (comments stripped)",
   "min(cpu_pct,100.0*n_cores)" not in _body_code,
   _body_code[-120:])
ck("...and the old raw formula is gone from code too",
   "(delta/10_000_000.0)/wall_delta*100.0" not in _body_code)

ck("BUG is documented at the helper",
   "BUG" in ast.get_source_segment(src, next(
       n for n in ast.walk(tree)
       if isinstance(n, ast.FunctionDef) and n.name == "_process_cpu_percent")))

print()
print("[4] the helper is total - no input may produce a nonsense reading")
for label, args, want in (
    ("no time elapsed", (1 * TICKS_PER_SEC, 0, CORES), 0.0),
    ("negative time delta", (-5 * TICKS_PER_SEC, 1.0, CORES), 0.0),
    ("zero cores reported", (1 * TICKS_PER_SEC, 1.0, 0), 0.0),
    ("negative cores", (1 * TICKS_PER_SEC, 1.0, -4), 0.0),
    ("absurd input clamps at 100", (999 * TICKS_PER_SEC, 0.001, CORES), 100.0),
):
    ck("%-26s -> %.1f" % (label, want), pct(*args) == want, pct(*args))

print()
print("[5] scale is independent of core count in the right way")
# Same absolute load, more cores -> a SMALLER share of the machine. That is
# exactly what Task Manager does, and what the old per-core reading got wrong.
one_core_4 = pct(1 * TICKS_PER_SEC, 1.0, 4)
one_core_16 = pct(1 * TICKS_PER_SEC, 1.0, 16)
ck("one core of 4 reads 25%", one_core_4 == 25.0, one_core_4)
ck("the same core of 16 reads 6.25%", one_core_16 == 6.25, one_core_16)
ck("...so a 4-core user is not shown a number 4x too small", one_core_4 > one_core_16)

print()
print("[6] the first sample is honest rather than invented")
ck("no previous reading yields 0.0, not a guess",
   "cpu_pct = 0.0" in body and "else:" in body)
ck("...and the reason is documented",
   "no previous reading" in body or "First sighting" in body)

print()
print("[7] the sibling CPU readout was already correct and is unchanged")
# hw_monitor uses GetSystemTimes kernel+user against idle, which is already a
# whole-machine percentage. It must NOT be "fixed" the same way - dividing an
# already-normalised figure by the core count would under-report by 16x.
hm = io.open(r"app\hw_monitor.py", encoding="utf-8", newline="").read()
ck("hw_monitor's system CPU divides by idle time, not cores",
   "busy / total" in hm or "(busy / total)" in hm)
ck("...and it is not divided by a core count",
   "cpu_percent_from_deltas" in hm and "n_cores" not in hm.split("def cpu_percent_from_deltas")[1][:600])
ck("its own docstring states the 0-100 contract", "min(100.0" in hm)

print()
if FAILS:
    print("PROCESS MANAGER CPU SCALE VERIFY: %d/%d FAILED -> %s" % (FAILS, CHECKS, FAILS))
    sys.exit(1)
print("PROCESS MANAGER CPU SCALE VERIFY: ALL PASS (%d checks)" % CHECKS)
