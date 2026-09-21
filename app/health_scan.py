"""
PC Health Report Card scan engine (user-approved feature 7, 2026-09).

Grades the machine across five read-only sensors plus an overall
score — no UI here (the dialog lives in gui.py):

  disk     C: free-space ratio (instant ctypes read)
  gpu      GPU driver age via the existing repair task (report-only)
  smart    SMART verdict via the existing repair task (report-only)
  windows  DISM CheckHealth via the existing repair task (admin only)
  junk     reclaimable-bytes estimate via the Storage Insight engine

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

from app.utils import TaskContext, TaskCancelled, TaskSkipped

# --------------------------------------------------------------------------- #
# Pure grading (unit-tested, no I/O)
# --------------------------------------------------------------------------- #

GRADE_SCORES = {"A": 4, "B": 3, "C": 2, "D": 1, "F": 0}


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


def overall_grade(grades: dict) -> str:
    """Mean of known grades, rounded to nearest letter. All-unknown or
    empty input grades '?' — an empty report card claims nothing."""
    scores = [GRADE_SCORES[g] for g in (grades or {}).values()
              if g in GRADE_SCORES]
    if not scores:
        return "?"
    # L01: half-up (round() is banker's: round(2.5)==2) — grades must round
    # the way users expect.
    import math as _math
    avg = int(_math.floor(sum(scores) / len(scores) + 0.5))
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


# --------------------------------------------------------------------------- #
# Sensors — each fn(ctx) returns (grade, headline, detail, note).
# ctx is a collecting context (log list, marshalled set_status,
# dialog-wired cancelled). TaskSkipped / errors become "?" inside
# run_health_scan, never exceptions out of it.
# --------------------------------------------------------------------------- #

def _collecting_ctx(log_list, set_status=None, cancelled=None) -> TaskContext:
    return TaskContext(
        log=log_list.append,
        set_status=(set_status or (lambda _m: None)),
        cancelled=(cancelled or (lambda: False)),
    )


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
        return (grade_disk_space(ratio),
                f"{_fb(free.value)} free of {_fb(total.value)}",
                f"{letter} is {ratio * 100:.0f}% free.", "")
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


def sense_gpu(ctx) -> tuple:
    from app.tasks.repair_tasks import repair_gpu_driver_age, get_gpu_manufacturer_download_url
    import subprocess as _sp
    
    # First, get GPU name for manufacturer URL detection
    gpu_name = ""
    try:
        out = _sp.run(
            ["powershell", "-NoProfile", "-Command",
             "Get-CimInstance Win32_VideoController | "
             "Select-Object -ExpandProperty Name | Select-Object -First 1"],
            capture_output=True, text=True, timeout=30,
            creationflags=getattr(_sp, "CREATE_NO_WINDOW", 0),
        )
        gpu_name = (out.stdout or "").strip()
    except Exception:
        pass
    
    outcome, msg = _run_report_task(ctx, repair_gpu_driver_age)
    if outcome == "pass":
        return ("A", "GPU drivers current", "No update nudge needed.", "")
    if outcome == "fail":
        # Get manufacturer download URL
        download_url = get_gpu_manufacturer_download_url(gpu_name) if gpu_name else ""
        note = download_url if download_url else ""
        return ("C", "Driver update suggested",
                msg or "Your GPU driver is over a year old.", note)
    return ("?", "Couldn't check drivers",
            msg or "Driver info unavailable on this PC.", "")


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


def sense_junk(ctx) -> tuple:
    """Reclaimable-bytes estimate via the Storage Insight engine
    (dry-run task invocation — measures without deleting). Phase-6:
    consults the shared estimate cache first (a Storage dialog or nudge
    walk often just measured the same folders); a fresh walk runs under
    the scan coordinator so two dialogs never double-walk the disk."""
    try:
        from app.storage_scan import cached_estimate, measure_all_cached
        from app.utils import format_bytes as _fb
        hit = cached_estimate()
        if hit is not None:
            total = hit[0]
        else:
            total = measure_all_cached(
                set_status=lambda m: ctx.set_status(m),
                cancelled=ctx.cancelled)
        # F11: None = unknown (busy/cancel) — never "A / already empty".
        if total is None:
            return ("?", "Junk scan busy",
                    "Another scan is running — open Storage Insight for the full estimate.", "")
        return (grade_junk(total),
                f"~{_fb(total)} reclaimable" if total > 0 else "Nothing to clean",
                "Safe to clean." if total > 0
                else "Your junk folders are already empty.", "")
    except Exception as exc:
        return ("?", "Couldn't measure junk", f"{exc}"[:120], "")


SENSORS = (
    ("disk", "Disk Space", sense_disk),
    ("gpu", "GPU Driver", sense_gpu),
    ("smart", "SSD & Drives", sense_smart),
    ("windows", "Windows Files", sense_windows),
    ("junk", "Reclaimable Junk", sense_junk),
)


def run_health_scan(ctx, on_card=None, sensors=None):
    """Run sensors in order; call on_card(key, card) per result (may run
    on a worker — on_card must marshal to Tk itself). Honors
    ctx.cancelled() between sensors; a mid-sensor cancel surfaces as
    TaskCancelled from the task machinery and ends the loop quietly,
    leaving already-painted cards in place. Returns {key: card}."""
    cards = {}
    for key, title, fn in (sensors or SENSORS):
        try:
            if ctx.cancelled():
                break
        except Exception:
            pass
        try:
            try:
                ctx.set_status(f"Checking {title.lower()}…")
            except Exception:
                pass
            grade, headline, detail, note = fn(ctx)
        except TaskCancelled:
            break
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
    return cards
