"""Prior BUG-010: the scheduler drift check only compared /TR.

`enable_schedule` skips the rewrite when the live task "already matches", to
avoid churning Task Scheduler on every dialog Apply. But it compared ONLY the
`/TR` (Task To Run) string. If the task's TIME or FREQUENCY had changed - in
Task Scheduler directly, or by an older build - the check concluded "already
matches" and skipped the rewrite. The dialog then said 09:00 while the machine
ran the job at 03:00, and Apply silently did nothing.

The fix also had to dodge a trap: `schtasks /Query /FO LIST` field labels
("Task To Run:", "Start Time:") are TRANSLATED on a localized Windows, so
parsing them returns nothing there - the same trap B6 documented for `sc qc`'s
START_TYPE label. The check therefore reads `/XML`, whose element names are
schema-defined and locale-independent.

The property that matters most: the check is THREE-valued. It only skips the
rewrite when it can POSITIVELY confirm a match; an unreadable or unparseable
XML yields None, which falls through to the rewrite. Skipping on a failed check
is what produced the bug, so a failed check must never be treated as a match.
"""

from __future__ import annotations

import ast
import inspect
import io
import sys

sys.path.insert(0, ".")

from app import scheduler as sch  # noqa: E402

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


WEEKLY = ("<Weekly><WeeksInterval>1</WeeksInterval>"
          "<DaysOfWeek><Monday/></DaysOfWeek></Weekly>")
DAILY = "<Daily><DaysInterval>1</DaysInterval></Daily>"
MONTHLY = "<Monthly><DaysOfMonth><Day /></DaysOfMonth></Monthly>"


def task_xml(start="09:00:00", cal=WEEKLY):
    """A realistic `schtasks /Query /XML` payload.

    <StartTime> lives in <Settings>; the calendar type lives in the trigger's
    <Schedule><Calendar>. An earlier version of this fixture put the time only
    in StartBoundary, so every case returned None and the check appeared to
    "pass" for the wrong reason.
    """
    return ('<?xml version="1.0" encoding="UTF-16"?>\n'
            '<Task version="1.2" '
            'xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">\n'
            ' <Settings><MultipleInstancesPolicy>IgnoreNew'
            '</MultipleInstancesPolicy>\n'
            '  <StartTime>%s</StartTime><Enabled>true</Enabled></Settings>\n'
            ' <Triggers><CalendarTrigger>'
            '<StartBoundary>2026-01-01T%s</StartBoundary>\n'
            '  <Enabled>true</Enabled><Schedule><Calendar>'
            '<StartBoundary>2026-01-01T%s</StartBoundary>%s'
            '</Calendar></Schedule></CalendarTrigger></Triggers>\n</Task>'
            % (start, start, start, cal))


M = sch._xml_schedule_matches

print("[1] a genuinely matching schedule is recognised")
ck("weekly at 09:00 matches weekly at 09:00",
   M(task_xml(cal=WEEKLY), "weekly", "09:00") is True,
   M(task_xml(cal=WEEKLY), "weekly", "09:00"))
ck("daily at 06:30 matches daily at 06:30",
   M(task_xml(start="06:30:00", cal=DAILY), "daily", "06:30") is True)

print()
print("[2] THE BUG: a changed TIME is detected (previously invisible)")
ck("live 09:00 vs wanted 03:00 -> NOT a match",
   M(task_xml(start="09:00:00"), "weekly", "03:00") is False,
   M(task_xml(start="09:00:00"), "weekly", "03:00"))
ck("live 03:00 vs wanted 09:00 -> NOT a match",
   M(task_xml(start="03:00:00"), "weekly", "09:00") is False)

print()
print("[3] a changed FREQUENCY is detected (previously invisible)")
ck("live daily vs wanted weekly -> NOT a match",
   M(task_xml(cal=DAILY), "weekly", "09:00") is False,
   M(task_xml(cal=DAILY), "weekly", "09:00"))
ck("live weekly vs wanted monthly -> NOT a match",
   M(task_xml(cal=WEEKLY), "monthly", "09:00") is False)
ck("live monthly vs wanted monthly at the same time -> match",
   M(task_xml(cal=MONTHLY), "monthly", "09:00") is True)

print()
print("[4] UNREADABLE means 'rewrite', never 'skip' - the property that matters")
for label, xml in (("empty string", ""), ("None", None),
                   ("not XML at all", "nope"),
                   ("XML with no <StartTime>",
                    task_xml().replace("<StartTime>09:00:00</StartTime>", "")),
                   ("truncated XML", "<Task><Settings>")):
    ck("%-24s -> None (unknown)" % label, M(xml, "weekly", "09:00") is None,
       M(xml, "weekly", "09:00"))
ck("the three answers are genuinely distinct",
   len({repr(M(task_xml(), "weekly", "09:00")),
        repr(M(task_xml(), "weekly", "03:00")),
        repr(M("", "weekly", "09:00"))}) == 3)

print()
print("[5] the check is wired into enable_schedule, and /XML not /FO LIST")
src = io.open(r"app\scheduler.py", encoding="utf-8", newline="").read()
ck("_live_schedule_xml exists", hasattr(sch, "_live_schedule_xml"))
ck("...and it queries /XML", '"/XML"' in inspect.getsource(sch._live_schedule_xml))
ck("...not the localized /FO LIST form",
   '"/FO"' not in inspect.getsource(sch._live_schedule_xml))
ck("_xml_schedule_matches exists", hasattr(sch, "_xml_schedule_matches"))
en = inspect.getsource(sch.enable_schedule)
ck("enable_schedule consults the schedule check", "_xml_schedule_matches(" in en)
ck("...and only skips when it is NOT False (so None falls through to a rewrite)",
   "_sched_ok is not False" in en)
ck("the localized-label trap is documented", "TRANSLATED" in src)
ck("prior BUG-010 is annotated", "BUG-010" in src)

print()
print("[6] structural: the skip is genuinely guarded, not a bare /TR compare")
node = next(n for n in ast.walk(ast.parse(src))
            if isinstance(n, ast.FunctionDef) and n.name == "enable_schedule")
# every `return ... "Schedule already up to date"` must be dominated by the
# _sched_ok check
guarded = src.count("if _sched_ok is not False")
ck("there is exactly one skip path and it is guarded", guarded == 1, guarded)
ck("...and the 'already up to date' return lives inside it",
   src.index("if _sched_ok is not False")
   < src.index('"Schedule already up to date."'))

print()
if FAILS:
    print("PRIOR BUG-010 VERIFY: %d/%d FAILED" % (FAILS, CHECKS))
    sys.exit(1)
print("PRIOR BUG-010 VERIFY: ALL PASS (%d checks)" % CHECKS)
