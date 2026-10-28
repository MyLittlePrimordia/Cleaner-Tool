"""
PC Health Report Card scan engine (user-approved feature 7, 2026-09).

LOW-002 disposition: KEEP, and note why, because the finding's own
recommendation no longer applies.

The audit called this "DEAD CODE - nothing under app/ imports it" and observed
that the Tools tab implemented the same five sensors INLINE with a different
design, recommending "wire it up or delete it". Two things have changed since:

  * the inline duplicate is GONE. After the H-series split there is no second
    implementation of these five sensors anywhere in app/ - the Tools tab
    (`app/ui/tabs/toolstab.py`) has no health panel at all. So this module is
    no longer one of two divergent copies; it is the only one.
  * deleting it would therefore destroy the ONLY copy of the five sensors and
    of the honesty rules below, which are the part worth keeping.

So it is kept as a first-class library, and the drift risk the finding names
is closed by making its coverage permanent rather than incidental: the grading
and the honesty contract are exercised directly by the test suite, instead of
leaving the only tests for it as a side-effect inside a GUI smoke run.

Grades the machine across eight read-only sensors plus an overall score — no
UI here (app/ui/tabs/healthtab.py owns the surface):

     disk       C: free-space ratio (instant ctypes read)
     memory     installed RAM and pagefile pressure
     crashes    Windows reliability records for the last seven days
     security   Defender real-time protection and update state
     startup    how many programs launch at sign-in
     smart      SMART verdict via the existing repair task (report-only)
     windows    DISM CheckHealth via the existing repair task (admin only)
     junk       reclaimable-bytes estimate via the Storage Insight engine

Honesty rules (the app's standing contract):
  * Sensors never write anything; repair tasks are invoked for their
    READ paths only (smart/gpu are report-only by design; DISM runs
    CheckHealth, never RestoreHealth, here).
  * Anything unmeasurable grades "?" (unknown) — never a fake F.
    A tweak the app can't verify shows as applied elsewhere; same
    spirit: no alarm without evidence.
  * fix_plan() maps findings to the single most useful next step;
    unknown grades never trigger actions.
"""

from __future__ import annotations

import ctypes
import sys

from app.utils import TaskCancelled, TaskSkipped
from app.health_sensors import (
    sense_battery, sense_crashes, sense_drivers, sense_maintenance,
    sense_memory, sense_security, sense_startup, sense_time,
)

# --------------------------------------------------------------------------- #
# Pure grading (unit-tested, no I/O)
# --------------------------------------------------------------------------- #

GRADE_SCORES = {"A": 4, "B": 3, "C": 2, "D": 1, "F": 0}

#: Longest the report will wait for the slow sensors before finishing without
#: them (each one normally lands in ~1-3s). Was an unbounded join, so a wedged
#: WMI/reliability probe kept the whole report - and the Run slot - pending
#: forever.
SLOW_SENSOR_JOIN_S = 45.0


def grade_disk_space(free_ratio: float) -> str:
    """Mirrors the drive-chip color bands (red at <=5% free, amber <=15%)."""
    try:
        r = float(free_ratio)
    except Exception:
        return "?"
    if r >= 0.15:
        return "A"
    if r >= 0.05:
        return "B"
    return "F"


def grade_junk(total_bytes) -> str:
    """Junk is never failure (no F): even 20 GB of temp files just
    means a very satisfying clean. None (unmeasured) grades '?', never
    a fake-healthy A — only a measured zero earns that."""
    if total_bytes is None:
        return "?"
    try:
        n = float(total_bytes)
    except Exception:
        return "?"
    if n < 1024 ** 3:
        return "A"
    if n < 5 * 1024 ** 3:
        return "B"
    if n < 15 * 1024 ** 3:
        return "C"
    return "D"


def _grade_of(value) -> str:
    """Accept either a bare grade letter ("A") or a full sensor card dict.

    BUG-018: overall_grade took ONLY the bare-letter shape while its two
    siblings, count_fixes() and fix_plan(), take the {key: card} mapping
    that run_health_scan() actually returns. Nothing caught it because the
    module had no caller - the only exercise passed flat letters, so the two
    shapes were never used side by side. The first real
    consumer (the PC Health Report dialog) passed `cards` and got
    TypeError: cannot use 'dict' as a dict key.

    Same one-entry duck type as count_fixes uses, so all three helpers now
    read the same mapping interchangeably.
    """
    if isinstance(value, str):
        return value
    try:
        return (value or {}).get("grade", "?")
    except Exception:
        return "?"


def overall_grade(grades: dict) -> str:
    """Mean of known grades, rounded to nearest letter, then CAPPED by the
    worst finding. All-unknown or empty input grades '?' - an empty report
    card claims nothing.

    `grades` may be either {key: "A"} or the {key: card} mapping
    run_health_scan() returns; see _grade_of.

    WHY THE CAP (BUGFIX 2026-10). A bare mean lets a serious finding be
    averaged away, and this dialog's own copy turned that into a false
    reassurance. Measured on a real machine: "Crashes & Reliability" graded
    F, and the report still opened on "Your PC is in good shape - One small
    thing is worth a look, nothing is broken." Thirteen mostly-A cards had
    outvoted one card that said something was actively wrong.

    That contradicts this module's own rule ("never a fake A - only a measured
    zero earns that") in the worst possible direction: the number was true and
    the meaning was false. So the mean is still computed the same way, and
    then clamped - a D or F anywhere means the overall can be no better than
    C, whatever the average says. A/B remain reachable only when every known
    card is A or B.
    """
    known = [g for g in (_grade_of(v) for v in (grades or {}).values())
             if g in GRADE_SCORES]
    if not known:
        return "?"
    # L01: half-up (round() is banker's: round(2.5)==2) — grades must round
    # the way users expect.
    import math as _math
    avg = int(_math.floor(sum(GRADE_SCORES[g] for g in known) / len(known)
                          + 0.5))
    # The cap. GRADE_SCORES is A=4..F=0, so C=2 is the ceiling here.
    if "D" in known or "F" in known:
        avg = min(avg, GRADE_SCORES["C"])
    for letter, score in GRADE_SCORES.items():
        if score == avg:
            return letter
    return "?"  # unreachable, kept for totality


def count_fixes(cards: dict) -> int:
    """Actionable findings: known grades below A. Unknowns are not
    'things to fix' — they're things we couldn't see."""
    n = 0
    for card in (cards or {}).values():
        try:
            g = card.get("grade", "?")
        except Exception:
            continue
        if g in ("B", "C", "D", "F"):
            n += 1
    return n


def fix_plan(cards: dict):
    """(action, payload) for the 'Fix what's found' button — the single
    most useful next step in priority order. Pure function (unit-tested).
    Actions the dialog executes: 'storage' (open Storage Insight),
    'repair' (pre-check task keys on the Repair tab), 'clean' (select a
    Clean preset), 'install' (show the Install tab), 'none'."""
    def _grade(key):
        try:
            return (cards.get(key) or {}).get("grade", "?")
        except Exception:
            return "?"

    if _grade("disk") == "F":
        return ("storage", None)
    if _grade("windows") in ("C", "D", "F"):
        return ("repair", ["dism_restorehealth", "sfc_scan"])
    if _grade("junk") in ("B", "C", "D", "F"):
        return ("clean", "Quick Clean")
    if _grade("gpu") in ("B", "C", "D", "F"):
        return ("install", None)
    return ("none", None)


#: Plain-language verdicts. The whole point of surfacing this engine is that a
#: casual user should never be handed a bare letter grade, so the letter is
#: only ever shown next to a sentence that says what it MEANS.
VERDICTS = {
    "A": ("Your PC looks healthy",
          "We found nothing that needs fixing."),
    "B": ("Your PC is in good shape",
          "One small thing is worth a look - nothing is broken."),
    # BUGFIX 2026-10: these two promised things the dialog does not do.
    # "each step below is reversible" / "work down the list" describe an
    # ORDERED LIST OF STEPS. The report is a set of cards, each with its own
    # button that hands off to a different screen - a Storage dialog, the
    # Install tab, a browser tab - and those are not steps and not one list.
    # "Reversible" is also wrong on its own terms: the routes include running
    # DISM/SFC, and a user cannot undo a repair the OS decided was needed.
    # What is true, and what the user actually needs to know, is that the
    # buttons only OPEN other screens - this dialog changes nothing.
    "C": ("Your PC needs a little attention",
          "Something is off, but nothing is broken. Each button below opens "
          "the tool that can look into it."),
    "D": ("Your PC needs some work",
          "A couple of things are unhappy. Open the tools below to look at "
          "them, starting with the red ones."),
    "F": ("Your PC needs attention now",
          "Something needs fixing. Start with the red items below."),
}


def verdict_for(grade: str, n_fixes: int = 0):
    """(headline, detail) a non-technical user can act on.

    Pure, so the wording is unit-tested rather than eyeballed in a dialog.

    An all-unknown report ("?") deliberately does NOT reassure. The engine's
    standing rule is that a sensor which could not be measured grades "?"
    rather than a fake pass, so the verdict must not imply a clean bill of
    health we never actually established. It says what happened and names the
    next action instead.
    """
    g = (grade or "?").upper()
    if g not in VERDICTS:
        return ("We couldn't check this PC",
                "Some checks couldn't run, so there's no score to show. "
                "Try again, or restart as Administrator for the full report.")
    headline, detail = VERDICTS[g]
    if g in ("C", "D", "F") and n_fixes:
        detail = "%s %d thing%s to look at." % (
            detail, n_fixes, "" if n_fixes == 1 else "s")
    return (headline, detail)


# --------------------------------------------------------------------------- #
# Sensors — each fn(ctx) returns (grade, headline, detail, note).
# ctx is a collecting context (log list, marshalled set_status,
# dialog-wired cancelled). TaskSkipped / errors become "?" inside
# run_health_scan, never exceptions out of it.
# --------------------------------------------------------------------------- #


def sense_disk(ctx) -> tuple:
    """System-drive free-space ratio + human numbers. Instant, no subprocess."""
    try:
        import os as _os
        import ctypes as _ct
        sysdrive = _os.environ.get("SYSTEMDRIVE", "C:")
        root = sysdrive if sysdrive.endswith("\\") else sysdrive + "\\"
        free = _ct.c_ulonglong(0)
        total = _ct.c_ulonglong(0)
        avail = _ct.c_ulonglong(0)
        ok = _ct.windll.kernel32.GetDiskFreeSpaceExW(
            _ct.c_wchar_p(root), _ct.byref(avail),
            _ct.byref(total), _ct.byref(free))
        if not ok or not total.value:
            return ("?", "Couldn't read disk", "Free-space query failed.", "")
        from app.utils import format_bytes as _fb
        ratio = free.value / total.value
        letter = root[:2]
        grade = grade_disk_space(ratio)
        # BUG-021: this used to put the NUMBERS in the headline
        # ("599.31 GB free of 930.56 GB") and the verdict in the detail.
        # Every other sensor returns a plain-language verdict as the headline
        # and the supporting figures as the detail, and the PC Health Report
        # card relies on that contract: the collapsed card shows the headline
        # only, so a number there reads as noise on a scorecard. Normalised
        # here rather than special-cased in the dialog.
        if grade == "A":
            verdict = "Plenty of free space"
        elif grade == "B":
            verdict = "Free space is a bit tight"
        elif grade == "C":
            verdict = "Free space getting low"
        elif grade == "D":
            verdict = "Your drive is nearly full"
        elif grade == "F":
            verdict = "Your drive is full"
        else:
            verdict = "Couldn't read disk"
        return (grade, verdict,
                f"{_fb(free.value)} free of {_fb(total.value)}"
                f"  -  {letter} is {ratio * 100:.0f}% free.", "")
    except Exception as exc:
        return ("?", "Couldn't read disk", f"{exc}"[:120], "")


def sense_reboot_note() -> str:
    """Pending-reboot one-liner (CBS RebootPending key) or ''. Instant
    registry read — folded into the Windows Files card as a sub-note so
    the 2x3 card grid stays symmetric (no 7th card)."""
    try:
        import winreg as _wr
        with _wr.OpenKey(_wr.HKEY_LOCAL_MACHINE,
                         r"SOFTWARE\Microsoft\Windows\CurrentVersion\Component Based Servicing\RebootPending"):
            pass
        return "Restart pending — reboot first for best repair results."
    except OSError:
        return ""
    except Exception:
        return ""


def _run_report_task(ctx, fn):
    """("pass"/"fail"/"skip", message): run a report-only repair task
    with a collecting ctx and classify the outcome. The task's own
    honesty contract does the hard work (raises with a plain-language
    verdict instead of returning fake success)."""
    try:
        fn(ctx)
        return ("pass", "")
    except TaskSkipped as exc:
        return ("skip", str(exc))
    except TaskCancelled:
        raise
    except Exception as exc:
        return ("fail", str(exc).splitlines()[0][:160] if str(exc) else "check failed")


def sense_smart(ctx) -> tuple:
    from app.tasks.repair_tasks import repair_smart_verdict
    outcome, msg = _run_report_task(ctx, repair_smart_verdict)
    if outcome == "pass":
        return ("A", "Drives healthy", "SMART reports no problems.", "")
    if outcome == "fail":
        return ("F", "Drive warning — back up now",
                msg or "A drive reports poor health.", "")
    return ("?", "Couldn't check drives",
            msg or "SMART data unavailable on this PC.", "")


def sense_windows(ctx) -> tuple:
    """DISM CheckHealth (read-only image check). Admin-gated up front:
    DISM needs elevation, so limited mode reports unknown instead of
    spending a minute failing."""
    try:
        from app.elevation import is_admin as _ia
        admin = _ia()
    except Exception:
        admin = False
    if not admin:
        return ("?", "Needs Administrator",
                "Restart as admin for the file check.", "")
    from app.tasks.repair_tasks import repair_dism_checkhealth
    outcome, msg = _run_report_task(ctx, repair_dism_checkhealth)
    note = sense_reboot_note()
    if outcome == "pass":
        return ("A", "Windows image healthy",
                "No corruption detected.", note)
    if outcome == "fail":
        return ("C", "Possible corruption found",
                (msg or "The image check flagged a problem.")
                + " Deep Repair can usually fix this.", note)
    return ("?", "Couldn't check files",
            msg or "The image check did not complete.", note)


def lower_bound_note(reasons) -> str:
    """One clause naming why a junk total is a lower bound, or ''.

    The reasons are the cleaners' own words (`app.utils.mark_unmeasured`), so
    the card repeats the sentence the run log already showed - naming which
    app was open without the card having to guess or keep a second list of
    apps in step with the tasks."""
    seen = []
    for reason in reasons or ():
        text = str(reason or "").strip().rstrip(".")
        if text and text not in seen:
            seen.append(text)
    return "; ".join(seen)


def sense_junk(ctx) -> tuple:
    """Reclaimable-bytes estimate via the Storage Insight engine
    (dry-run task invocation — measures without deleting). Phase-6:
    consults the shared estimate cache first (a Storage dialog or nudge
    walk often just measured the same folders); a fresh walk runs under
    the scan coordinator so two dialogs never double-walk the disk.

    A cleaner that skipped (Office/Steam/FiveM live) is a known skip, so the
    walk still returns a number - but a LOWER BOUND, and this card has to say
    so. Without it, "About 4 GB can be cleaned" quietly presented a total that
    omitted a skipped Office cache: the same all-clear the 0-vs-None contract
    exists to prevent, one level up. The headline carries the marker because
    it is the only field the report row renders."""
    try:
        from app.storage_scan import (
            cached_declined, cached_estimate, measure_all_cached)
        from app.utils import format_bytes as _fb
        declined = []
        hit = cached_estimate()
        if hit is not None:
            total = hit[0]
            declined.extend(cached_declined())
        else:
            total = measure_all_cached(
                set_status=lambda m: ctx.set_status(m),
                cancelled=ctx.cancelled,
                declined=declined)
        # F11: None = unknown (busy/cancel) — never "A / already empty".
        if total is None:
            return ("?", "Junk scan busy",
                    "Another scan is running — open Storage Insight for the full estimate.", "")
        grade = grade_junk(total)
        skipped = lower_bound_note(declined)
        # BUG-021: verdict in the headline, figures in the detail - see the
        # matching note in sense_disk. This used to return the amount as the
        # headline and "Safe to clean." as the detail.
        if total <= 0:
            if skipped:
                # Nothing that could be measured is junk, but a skipped cache
                # can still hold gigabytes: "already empty" would be a claim
                # about a folder nobody looked at. Unknown, like the dialog's
                # "? unknown" pill for the same case.
                return ("?", "Nothing found so far",
                        "The folders that could be measured are empty; %s."
                        % skipped, "")
            return (grade, "Nothing to clean",
                    "Your junk folders are already empty.", "")
        if skipped:
            return (grade, "At least %s can be cleaned" % _fb(total),
                    "%s of temporary files and leftovers, and possibly more: "
                    "%s. Safe to remove - nothing personal is deleted."
                    % (_fb(total), skipped), "")
        return (grade, "About %s can be cleaned" % _fb(total),
                "%s of temporary files and leftovers. Safe to remove - "
                "nothing personal is deleted." % _fb(total), "")
    except Exception as exc:
        return ("?", "Couldn't measure junk", f"{exc}"[:120], "")


#: (key, title, function, slow). `slow=True` marks a sensor that must NOT
#: block the first paint, so it runs after the fast ones on a background
#: thread and reports its card as it lands.
#:
#: BUG-044 measured the split rather than guessing it. Sequential, the report
#: showed NOTHING for several seconds. With the split, the seven cheap cards
#: are up quickly and the slow ones fill in underneath.
#:
#: The fast set is everything that answers from one batched PowerShell launch
#: plus an already-cached value. `smart`, `windows` and `junk` are read-only
#: too, but each costs its own probe, so putting them in the fast set meant
#: the first paint waited for cards the user reads seconds later anyway.
#: `drivers` reads the batched slow probe beside them; its query is the ~3s
#: half of that probe, which is exactly why it does not belong in the first
#: paint.
#:
#: Order is deliberate and fixed: it is the order a stable priority list falls
#: back on (health_model sorts by severity, then by this dict order), so a card
#: filling in always means the same thing and the list never jumps around.
SENSORS = (
    ("disk", "Disk Space", sense_disk, False),
    ("memory", "Memory & Pagefile", sense_memory, False),
    ("crashes", "Recent Crashes", sense_crashes, False),
    ("security", "Security", sense_security, False),
    ("startup", "Startup Programs", sense_startup, False),
    # Both read the SAME batched probe the five cards above use, so they add
    # a graded row without adding a probe (and stay in the fast set).
    ("maintenance", "Drive Maintenance", sense_maintenance, False),
    ("time", "Clock & Uptime", sense_time, False),
    ("smart", "SSD & Drives", sense_smart, True),
    ("windows", "Windows Files", sense_windows, True),
    ("junk", "Reclaimable Junk", sense_junk, True),
    # N-003: laptops only. A desktop raises TaskSkipped, which becomes an
    # explicit "Not applicable" card - not a pass and not a failure.
    ("battery", "Battery Health", sense_battery, True),
    # A device with no driver is broken hardware, and a missing driver is the
    # one finding here whose fix has its own screen: the Drivers tab looks the
    # package up and installs it. Last in the list because it depends on the
    # slowest probe in the app.
    ("drivers", "Drivers", sense_drivers, True),
)


def open_reliability_monitor() -> bool:
    """Open Windows' Reliability Monitor on the Apps section.

    The destination for the "Recent Crashes" card. There is no single fix
    for "this program keeps crashing" - that judgement has been right since
    the card was written, and it is why the card originally had NO action at
    all. But there IS a screen that answers the question the user actually
    has, which is *which* programs crashed and when: Reliability Monitor is
    exactly that, Windows already has it, and the app does not duplicate it.

    perfmon.exe is a real executable, so ShellExecuteW launches it cleanly
    (measured rc=42 here). No elevation, no console, no output to capture.
    Never raises.
    """
    if not sys.platform.startswith("win"):
        return False
    try:
        rc = ctypes.windll.shell32.ShellExecuteW(None, "open", "perfmon.exe",
                                                 "/rel", None, 1)
        return rc is not None and int(rc) > 32
    except Exception:
        return False


def _norm_spec(spec):
    """Accept a 3-tuple (legacy, always fast) or the 4-tuple with `slow`."""
    if len(spec) == 4:
        return tuple(spec)
    key, title, fn = spec
    return (key, title, fn, False)


def run_health_scan(ctx, on_card=None, sensors=None):
    """Run sensors and call on_card(key, card) per result (may run on a
    worker - on_card must marshal to Tk itself). Honors ctx.cancelled()
    between sensors; a mid-sensor cancel surfaces as TaskCancelled from the
    task machinery and ends the loop quietly, leaving already-painted cards
    in place. Returns {key: card}.

    BUG-044: sensors are split into a FAST set and a SLOW set. The slow ones
    (physical-disk detail ~1.0s, reliability records ~2.1s, driver dates
    ~2.9s on the reference machine) used to run inline, so the report sat on
    "Checking..." for four seconds before showing anything at all.

    They now run after the fast ones, on a background thread, reporting each
    card as it lands - so the fast cards are on screen in well under a second
    and the slow detail fills in underneath. The thread is JOINED
    before returning, which keeps the contract intact: the returned mapping
    still contains every card, so the dialog's overall grade and verdict are
    computed from the complete set rather than a partial one.
    """
    import threading

    # BUG-044: drop any probe cache left on a REUSED ctx, so a rescan
    # re-reads the machine instead of replaying the previous answer.
    for _stale in ('_hs_fast', '_hs_slow'):
        try:
            delattr(ctx, _stale)
        except Exception:
            pass

    cards = {}
    specs = [_norm_spec(s) for s in (sensors or SENSORS)]

    def run_one(key, title, fn):
        try:
            try:
                ctx.set_status(f"Checking {title.lower()}…")
            except Exception:
                pass
            grade, headline, detail, note = fn(ctx)
        except TaskCancelled:
            return None
        except TaskSkipped as exc:
            grade, headline, detail, note = (
                "?", "Not applicable", str(exc)[:160], "")
        except Exception as exc:
            grade, headline, detail, note = (
                "?", "Couldn't check", str(exc)[:160], "")
        card = {"key": key, "title": title, "grade": grade,
                "headline": headline, "detail": detail, "note": note}
        cards[key] = card
        if on_card is not None:
            try:
                on_card(key, card)
            except Exception:
                pass
        return card

    def cancelled():
        try:
            return bool(ctx.cancelled())
        except Exception:
            return False

    for key, title, fn, slow in specs:
        if slow or cancelled():
            continue
        run_one(key, title, fn)

    slow_specs = [s for s in specs if s[3]]
    if slow_specs and not cancelled():
        def slow_worker():
            for key, title, fn, _slow in slow_specs:
                if cancelled():
                    return
                run_one(key, title, fn)

        th = threading.Thread(target=slow_worker, daemon=True,
                              name="HealthScanSlow")
        th.start()
        # JOINED, not detached: the caller computes the overall grade from the
        # return value, so returning early would silently drop the slow cards
        # out of the verdict. BOUNDED (audit 2026-10): an unbounded join on
        # this thread hung the report forever if one WMI/reliability probe
        # wedged. On timeout the unfinished sensors still get an honest
        # "couldn't check" card, so the grade stays derived from every key.
        th.join(timeout=SLOW_SENSOR_JOIN_S)
        if th.is_alive():
            for key, title, _fn, _slow in slow_specs:
                if key in cards or cancelled():
                    continue
                card = {"key": key, "title": title, "grade": "?",
                        "headline": "Still measuring",
                        "detail": ("This check was taking too long, so the "
                                   "report finished without it."),
                        "note": ""}
                cards[key] = card
                if on_card is not None:
                    try:
                        on_card(key, card)
                    except Exception:
                        pass

    # A cancelled run still reports whatever it managed to finish.
    return cards
