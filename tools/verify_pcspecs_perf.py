"""PERF: the PC Specs card froze the app for ~6 seconds.

User report: the PC Specs and Monitor Test tool cards "have a delay/lag to
them", and the app "lag and lock up".

Measured on the user's machine, `full_report_lines()` took 6.0 s - and the
cause was duplicate work. It calls `summary_lines()` first, which runs the same
probes, then runs them AGAIN for its own sections:

    os_info    756 ms     cpu_info  1342 ms     board_info  597 ms
    net_info   427 ms     ram_info    1.2 ms   gpu_info     0.1 ms

So roughly 2.7 s of the 6 s was the same registry and SMBIOS reads performed
twice per report, all of it on the Tk thread. There was no caching at all.

These are static facts about the machine - OS build, CPU model, motherboard -
so re-reading them hundreds of times a session cannot reveal anything new. The
six probes `full_report_lines` and `summary_lines` share are now memoised for
the process lifetime, taking a cold report from ~6.0 s to ~3.5 s and a repeat
to ~0.5 s.

This test asserts the CACHE STATE, not wall-clock. A timing comparison is the
wrong instrument: under the full suite the machine is busy and a "warm is half
of cold" bound flakes, while "the cache is populated" and "clear_cache empties
it" are exact and load-independent. Timing is printed, never asserted.
"""

from __future__ import annotations

import ast
import copy
import io
import sys
import time

sys.path.insert(0, ".")

import app.pc_specs as ps  # noqa: E402

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


SHARED = ("os_info", "cpu_info", "ram_info", "board_info", "gpu_info",
          "storage_info")


def _facts(lines):
    """The report minus the generation timestamp and the live RAM figure.

    The timestamp differs by design, and "35.7 GB free" is genuinely live, so
    comparing those byte-for-byte asserts something untrue.
    """
    out = []
    for l in lines:
        if l.startswith("Generated:"):
            continue
        if l.startswith("Available:"):
            out.append("Available: <live>")
            continue
        out.append(l)
    return out


print("[1] the shared probes are memoised")
for name in SHARED:
    ck("%-14s is wrapped" % name,
       getattr(getattr(ps, name, None), "__wrapped__", None) is not None, name)
src = io.open(r"app\pc_specs.py", encoding="utf-8", newline="").read()
ck("exactly the six shared probes are decorated", src.count("@_memo") == len(SHARED),
   src.count("@_memo"))
ck("net_info is NOT memoised (it changes minute to minute)",
   getattr(ps.net_info, "__wrapped__", None) is None)
ast.parse(src)          # the module must still be valid Python

print()
print("[2] the cache is populated, and clear_cache() empties it")
ck("a cache dict exists", isinstance(getattr(ps, "_SPEC_CACHE", None), dict))
ps.clear_cache()
ck("clear_cache leaves it empty", len(ps._SPEC_CACHE) == 0, len(ps._SPEC_CACHE))
ps.os_info()
ck("a probe populates it", len(ps._SPEC_CACHE) == 1, len(ps._SPEC_CACHE))
ps.cpu_info()
ck("a second probe adds to it", len(ps._SPEC_CACHE) == 2, len(ps._SPEC_CACHE))
ps.clear_cache()
ck("clear_cache() genuinely empties it", len(ps._SPEC_CACHE) == 0, len(ps._SPEC_CACHE))
_cc = next(n for n in ast.walk(ast.parse(src))
           if isinstance(n, ast.FunctionDef) and n.name == "clear_cache")
ck("clear_cache is callable and has a docstring",
   callable(ps.clear_cache) and ast.get_docstring(_cc) is not None,
   ast.get_docstring(_cc))

print()
print("[3] the report is still complete and honest")
ps.clear_cache()
lines = ps.full_report_lines()
text = ps.full_report_text()
ck("the report has real content", len(lines) > 20, len(lines))
ck("every section is present",
   all(any(sec in l for l in lines) for sec in
       ("[OS]", "[CPU]", "[RAM]", "[Board]", "[Graphics]")),
   [l for l in lines if l.startswith("[")])
ck("text and lines agree in substance", "Cleaner Tool" in text and "Generated:" in text)
ck("no fact changes between two builds of the same report",
   _facts(lines) == _facts(ps.full_report_lines()),
   [(a, b) for a, b in zip(_facts(lines), _facts(ps.full_report_lines()))
    if a != b][:3])

print()
print("[4] cached results cannot be corrupted by a caller")
ps.clear_cache()
gpus = ps.gpu_info()
ck("gpu_info returns a list", isinstance(gpus, list), type(gpus).__name__)
if gpus:
    gpus.append("SENTINEL-SHOULD-NOT-PERSIST")
    ck("a caller's edit does NOT leak into the next caller",
       "SENTINEL-SHOULD-NOT-PERSIST" not in ps.gpu_info())
disks = ps.storage_info()
ck("storage_info returns a list", isinstance(disks, list), type(disks).__name__)
if disks:
    disks.append("SENTINEL-2")
    ck("...same for storage_info", "SENTINEL-2" not in ps.storage_info())

print()
print("[5] the cost, printed but NOT asserted (load-dependent)")
ps.clear_cache()
t0 = time.perf_counter()
ps.full_report_lines()
cold_ms = (time.perf_counter() - t0) * 1000
t0 = time.perf_counter()
ps.full_report_lines()
warm_ms = (time.perf_counter() - t0) * 1000
print("        cold %.0f ms -> warm %.0f ms   (was ~6000 ms before)" % (cold_ms, warm_ms))
print("        informational only: a timing bound here flakes under the suite")

print()
print("[6] the finding is recorded where a reader will find it")
ck("PERF and the duplicate-probe cause are documented in the module",
   "PERF" in src and "duplicate" in src.lower())
ck("the deep-copy rationale is documented",
   "DEEP-COPIED" in src or "deep-copied" in src.lower())
ck("the stale-cache caveat points at clear_cache",
   "clear_cache" in src and ("adding or removing RAM" in src
                             or "RAM added or removed" in src))

print()
if FAILS:
    print("PC SPECS PERF VERIFY: %d/%d FAILED -> %s" % (FAILS, CHECKS, FAILS))
    sys.exit(1)
print("PC SPECS PERF VERIFY: ALL PASS (%d checks)" % CHECKS)
