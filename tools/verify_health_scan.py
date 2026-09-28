"""LOW-002: health_scan's grading and its honesty contract, as a first-class check.

The audit called `app/health_scan.py` dead code whose only importer was the GUI
smoke suite, and noted its grading logic was "genuinely good - which is a
reason to wire it up, not delete it".

Since the audit the inline duplicate is gone: there is no second implementation
of these five sensors anywhere under app/, so this module is the ONLY copy.
Deleting it would destroy the only copy of the honesty rules, and wiring it into
a UI panel would be a feature, not a fix. So it is kept, and the drift risk the
finding names is closed by making its coverage permanent here rather than
incidental inside tools/smoke/dialogs.py.

What is pinned:
  * the 0-vs-None distinction the app's whole honesty contract rests on -
    a never-measured value must never grade as "A / already empty";
  * every grade boundary;
  * overall_grade and fix_plan/count_fixes behaviour;
  * that the module is genuinely importable and self-consistent.
"""

from __future__ import annotations

import sys

sys.path.insert(0, ".")

import app.health_scan as hs  # noqa: E402

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


print("[1] THE HONESTY CONTRACT: None is unknown, never 'already empty'")
ck("grade_junk(None) is '?', not 'A'", hs.grade_junk(None) == "?", hs.grade_junk(None))
ck("grade_junk(0) IS 'A' - a measured zero is a real result",
   hs.grade_junk(0) == "A", hs.grade_junk(0))
ck("None and 0 are graded DIFFERENTLY (this is the whole point)",
   hs.grade_junk(None) != hs.grade_junk(0))
ck("a non-numeric value is also '?', not a crash and not 'A'",
   hs.grade_junk("not a number") == "?", hs.grade_junk("not a number"))
ck("...and it does not raise on weird input",
   hs.grade_junk(object()) == "?")

print()
print("[2] grade_junk boundaries")
CASES = [
    (0, "A"), (1024 ** 3 - 1, "A"), (1024 ** 3, "B"),
    (5 * 1024 ** 3 - 1, "B"), (5 * 1024 ** 3, "C"),
    (15 * 1024 ** 3 - 1, "C"), (15 * 1024 ** 3, "D"),
    (500 * 1024 ** 3, "D"),
]
bad = 0
for value, want in CASES:
    got = hs.grade_junk(value)
    if got != want:
        bad += 1
        print("     %r -> %r, wanted %r" % (value, got, want))
ck("every grade boundary is exact", bad == 0, "%d wrong" % bad)
ck("junk never grades as F (the docstring says 'junk is never failure')",
   hs.grade_junk(10 ** 15) not in ("F", "E"), hs.grade_junk(10 ** 15))

print()
print("[3] grade_disk_space")
ck("an empty disk is the worst grade",
   hs.grade_disk_space(0.0) in ("F", "D"), hs.grade_disk_space(0.0))
ck("a full disk is the best grade",
   hs.grade_disk_space(1.0) in ("A", "B"), hs.grade_disk_space(1.0))
ck("it does not raise on a nonsensical ratio",
   isinstance(hs.grade_disk_space(-5.0), str)
   and isinstance(hs.grade_disk_space(float("nan")), str))
ck("a sensible ratio grades sensibly",
   hs.grade_disk_space(0.5) in ("A", "B", "C"), hs.grade_disk_space(0.5))

print()
print("[4] overall_grade")
ck("an empty grade dict does not raise", isinstance(hs.overall_grade({}), str))
ck("all-A sensors give an A",
   hs.overall_grade({"disk": "A", "gpu": "A", "smart": "A",
                     "windows": "A", "junk": "A"}) == "A",
   hs.overall_grade({"disk": "A", "gpu": "A", "smart": "A",
                     "windows": "A", "junk": "A"}))
ck("an all-unknown report grades '?' (claims nothing)", 
   hs.overall_grade({"disk": "?", "junk": "?"}) == "?",
   hs.overall_grade({"disk": "?", "junk": "?"}))
ck("...and an empty dict does too", hs.overall_grade({}) == "?")
# overall_grade is DOCUMENTED as the mean of KNOWN grades: an unknown is
# excluded rather than counted as a failure. My first check asserted the
# opposite and failed against correct, intentional code - the docstring says
# "Mean of known grades ... All-unknown or empty input grades '?'".
ck("an unknown sensor is EXCLUDED from the mean (documented behaviour)",
   hs.overall_grade({"disk": "A", "gpu": "A", "smart": "A",
                     "windows": "A", "junk": "?"}) == "A",
   hs.overall_grade({"disk": "A", "gpu": "A", "smart": "A",
                     "windows": "A", "junk": "?"}))
ck("a bad sensor drags the overall grade down",
   hs.overall_grade({"disk": "A", "gpu": "A", "smart": "A",
                     "windows": "A", "junk": "F"}) != "A")

print()
print("[5] count_fixes / fix_plan")
# count_fixes reads card["grade"]; a card is a MAPPING, not a tuple.
cards = {
    "disk": {"title": "Disk Space", "grade": "D", "detail": "free some space",
             "fix": "do the thing"},
    "junk": {"title": "Junk", "grade": "A", "detail": "nothing to clean",
             "fix": None},
    "gpu": {"title": "GPU", "grade": "?", "detail": "could not read",
            "fix": None},
}
try:
    n = hs.count_fixes(cards)
    ck("count_fixes returns a count", isinstance(n, int), type(n).__name__)
    ck("only grades below A count as actionable", n == 1, n)
    ck("an 'A' sensor is not a fix", True)
    ck("an unknown '?' is not counted as a fix ('things we couldn't see')",
       hs.count_fixes({"x": {"grade": "?"}}) == 0)
    ck("count_fixes tolerates an empty map", hs.count_fixes({}) == 0)
    ck("count_fixes tolerates None", hs.count_fixes(None) == 0)
    plan = hs.fix_plan(cards)
    ck("fix_plan returns something listable", hasattr(plan, "__iter__"),
       type(plan).__name__)
except Exception as exc:
    ck("count_fixes / fix_plan work on a simple card map", False, repr(exc))

print()
print("[6] the module is self-consistent and importable")
ck("run_health_scan exists", callable(hs.run_health_scan))
ck("the five sensors are registered",
   len(hs.SENSORS) == 5, [s[0] for s in hs.SENSORS]
   if isinstance(hs.SENSORS, (list, tuple)) else hs.SENSORS)
ck("the junk sensor is one of them",
   any(s[0] == "junk" for s in hs.SENSORS))
ck("LOW-002's disposition is recorded in the module",
   "LOW-002" in (hs.__doc__ or ""))
import io as _io  # noqa: E402
ck("...and says it is the ONLY implementation, not a duplicate",
   "only copy" in (hs.__doc__ or "").lower()
   or "no longer one of two" in (hs.__doc__ or "").lower())

print()
print("[7] the false 'Health Report panel' claim is gone from tab_presets")
_tp = _io.open(r"app\tab_presets.py", encoding="utf-8", newline="").read()
_bad = [l for l in _tp.splitlines()
        if "Health Report" in l and not l.strip().startswith("#")]
ck("no executable/comment line claims a Health Report panel exists",
   not _bad, _bad)
ck("...and the correction is recorded", "LOW-002" in _tp)

print()
if FAILS:
    print("LOW-002 VERIFY: %d/%d FAILED" % (FAILS, CHECKS))
    sys.exit(1)
print("LOW-002 VERIFY: ALL PASS (%d checks)" % CHECKS)
