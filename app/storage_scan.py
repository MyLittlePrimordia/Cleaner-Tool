"""
Storage Insight scan engine (user-approved feature 3, 2026-09).

Two read-only scanners, no UI here (the dialog lives in gui.py):

1. measure_clean_tasks() — invokes real Clean-tab task functions with a
   dry_run TaskContext, so estimates come from the SAME folder lists a
   real run would clean (no duplicated path knowledge to drift).
   ONLY tasks on SCAN_ALLOWLIST are invocable, and every one of them is
   PROVED to mutate nothing under dry_run by dev/tools/verify_dryrun_scope.py
   (a CI guard that walks the code each task can reach and fails on a
   mutation no dry check dominates). A task outside the allowlist is
   declared in SCAN_EXCLUDED with the reason it cannot be measured: it
   deletes through something other than a folder walk (a service, an Appx
   package, an evtx channel, cleanmgr), it only reports, or it would
   double-count a folder another task already measures. An excluded task is
   never estimated; the dialog says so honestly instead of inventing
   numbers. A MEASURED task may still decline to look - Office holding
   unsynced edits, Steam mid-download - and records why with
   `utils.mark_unmeasured`; its size is then reported unknown, never 0.
   validate_allowlist() keeps the two tables in agreement, so a Clean task
   is never silently dropped from both.

2. find_game_libraries() — sizes installed games per launcher for the
   "Biggest games" column: Steam libraryfolders.vdf, Epic manifest
   .item files, plus default install roots for GOG/Ubisoft/Riot/Xbox.
   READ-ONLY directory walks (no deletion path exists in this module).

Both scanners honor ctx.cancelled()/cancelled() so closing the dialog
stops the walk promptly, and both skip reparse points (same guard as
the cleaner — a junction must never inflate another drive's total).

3. write_benchmark() — one HONEST number pair for the C: drive: writes
a temp file of N MB with fsync, reads it back, reports MB/s each way.
Temp file is always removed (finally + cancelled paths). stdlib only.
"""

from __future__ import annotations

import glob
import json
import os
import re
import time

from app.utils import TaskContext

# --------------------------------------------------------------------------- #
# 1. Junk measurement — audited allowlist + display categories
# --------------------------------------------------------------------------- #

# Every key here is a folder walk: the task function touches NOTHING but
# clean_folder_contents / _clean_many / _clean_files (all dry_run-aware)
# plus read-only probes, and dev/tools/verify_dryrun_scope.py proves that on
# every CI run. Add a key only once the guard passes for it - a task whose
# delete path is a service stop, a package removal or an external command
# does not belong here, however safe it feels.
SCAN_ALLOWLIST = frozenset({
    # Game files & caches
    "shader_cache", "launcher_cache", "engine_cache", "driver_junk",
    "game_files", "vrchat_cache", "fivem_cache", "starcitizen_cache",
    "hoyoverse_logs", "steam_depot",
    # Windows temp & logs
    "user_temp_files", "system_temp_files", "old_logs", "error_reports",
    "gpu_watchdog_dumps", "prefetch", "thumbnail_cache", "font_cache",
    "chk_fragments", "setup_logs", "onedrive_logs",
    # Browser & app caches
    "browser_cache", "inet_cache", "office_cache", "uwp_cache",
    "webview_cache", "stream_logs",
    # Update leftovers
    "update_leftovers", "win_update_cache", "delivery_optimization",
    "edge_update_cache", "squirrel_staging",
    # Dev & package caches
    "winget_cache", "dev_caches", "pkg_caches", "defender_history",
})

# Every Clean task NOT on the allowlist says why here, in one line. This is
# the other half of the declaration: without it, a task could drop out of
# the estimate because someone edited one set and forgot the tables, and the
# only symptom would be a smaller number in the dialog. validate_allowlist()
# fails when a task is in neither table, or in both.
# F17: game_captures is excluded by an explicit user ruling - Xbox Game Bar
# clips are user-created content, not junk. The Clean-tab task stays as an
# opt-in (default=False); Storage/Health estimates must never count them.
SCAN_EXCLUDED = {
    # Deletes through something that is not a folder walk.
    "dns_flush": "runs `ipconfig /flushdns` - a command, not files",
    "activity_traces": "clears registry traces, and that write has no dry-run check",
    "store_cache": "runs `wsreset`, which resets the Store rather than emptying a folder",
    "remove_bloat": "removes Appx packages through PowerShell",
    "restore_point": "creates a restore point - it writes, never frees",
    "steam_stuck": "deletes Steam staging after a process probe with no dry check",
    "backup_saves": "copies saves to a backup and prunes old ones - it adds bytes",
    "event_logs": "clears ~200 evtx channels; its dry pass returns 0, which reads as 'already clean'",
    "disk_cleanup_deep": "delegates to cleanmgr, whose result its dry pass cannot measure",
    "recycle_bin": "its dry pass sizes only the crash dumps it also cleans; the bin is not walked",
    "ram_purge": "trims RAM working sets - nothing on disk",
    # Reports, never deletes.
    "downloads_audit": "audits the user's Downloads and deletes nothing - those are user files",
    "ai_model_cache": "reports model weights, which are explicitly not junk",
    "docker_wsl_usage": "reports WSL/Docker usage from an external command and deletes nothing",
    # Dry pass would report 0 for a reason other than "empty".
    "orphan_leftovers": "walks every install root for low-confidence leftovers - a full installed-list walk per scan",
    # The same folders another measured task already walks.
    "riot_logs": "the Riot/VALORANT logs launcher_cache already measures",
    "rockstar_logs": "the Rockstar launcher logs launcher_cache already measures",
    "roblox_logs": "the Roblox logs folder game_files already measures",
    "temp_deep_clean": "the two temp folders user_temp_files and system_temp_files already measure",
    # Deliberate product decisions.
    "terminal_history": "a privacy action on a few KB, not a space estimate",
    "game_captures": "user-created clips (F17: default-OFF, never counted as junk)",
}

# Display groups: (title, member keys in measure order, tooltip text).
# 5 rows fit the dialog with no scrolling; member task labels ride each
# row's tooltip so users can trace an estimate back to real tasks. Every
# allowlisted key is in exactly one group - validate_allowlist() enforces it,
# so the banner total is always the sum of the rows the user can see.
SCAN_CATEGORIES = (
    ("Game files & caches",
     ("shader_cache", "launcher_cache", "engine_cache", "driver_junk",
      "game_files", "vrchat_cache", "fivem_cache", "starcitizen_cache",
      "hoyoverse_logs", "steam_depot"),
     "Shader caches, launcher web caches, engine caches, driver leftovers, "
     "per-game logs/dumps and the VRChat / FiveM / Star Citizen / HoYoverse "
     "caches, plus Steam's depot cache. (Game Bar captures are opt-in on the "
     "Clean tab.)"),
    ("Windows temp & logs",
     ("user_temp_files", "system_temp_files", "old_logs", "error_reports",
      "gpu_watchdog_dumps", "prefetch", "thumbnail_cache", "font_cache",
      "chk_fragments", "setup_logs", "onedrive_logs"),
     "Temp folders, setup/event logs, error reports, GPU watchdog dumps, "
     "Prefetch, the Explorer thumbnail and font caches, chkdsk fragments "
     "and OneDrive logs."),
    ("Browser & app caches",
     ("browser_cache", "inet_cache", "office_cache", "uwp_cache",
      "webview_cache", "stream_logs"),
     "Chrome/Edge/Firefox caches, Windows web cache, Office and Store-app "
     "caches, embedded WebView2 caches, and Medal/Overwolf/SteelSeries logs."),
    ("Update leftovers",
     ("update_leftovers", "win_update_cache", "delivery_optimization",
      "edge_update_cache", "squirrel_staging"),
     "Windows.old and $Windows.~BT upgrade debris, the Windows Update download "
     "and Delivery Optimization caches, Edge update downloads and Squirrel "
     "update staging."),
    ("Dev & package caches",
     ("winget_cache", "dev_caches", "pkg_caches", "defender_history"),
     "winget/npm/pip/NuGet/Cargo/Gradle caches and Defender history."),
)

# What the scan deliberately does NOT estimate (the dialog's honest footnote).
# The counts are derived, not typed: they cannot drift from the tables.
SCAN_EXCLUDED_NOTE = (
    "Estimates cover file-based cleaners only (%d of the %d Clean tasks). "
    "Not estimated, deliberately: anything that deletes through a service, "
    "an Appx package, cleanmgr or an evtx channel; the Recycle Bin; the "
    "audits that only report; and game saves and captures. They still run "
    "normally - they just have no size estimate. A measured cleaner that "
    "declines to look (Office, Steam or FiveM still running) reports its "
    "size as unknown, never as 0."
    % (len(SCAN_ALLOWLIST), len(SCAN_ALLOWLIST) + len(SCAN_EXCLUDED))
)


def _clean_task_by_key() -> dict:
    """Live task objects for the merged Clean tab, keyed by task key.
    Resolved at call time (not import) so the scan always tracks the
    current task table — a renamed key degrades to 'skipped', never a
    crash (same tolerant-resolver philosophy as the scheduler)."""
    from app.tab_presets import TABS
    return {t.key: t for t in TABS.get("Clean", [])}


def make_measure_ctx(set_status=None, cancelled=None) -> TaskContext:
    """A TaskContext for scanning: dry_run on, with the status line wired to
    the dialog (both optional).

    MED-002: `log` used to be `lambda _m: None`, which silently discarded
    every message. That is fine while nothing logs - but it meant the failure
    logging added to measure_clean_tasks would have been thrown away on exactly
    the path that needed it. Messages now go to the status line, so a category
    that could not be measured actually tells the user instead of vanishing.
    """
    _status = (set_status or (lambda _m: None))
    return TaskContext(
        log=lambda m: _status(m),
        set_status=_status,
        cancelled=(cancelled or (lambda: False)),
        dry_run=True,
    )


def _refusal_mark(ctx) -> int:
    """How many refusal notes `ctx` carries right now (0 for a stub context).

    A task that declines to look records why in `ctx.unmeasured`
    (`app.utils.mark_unmeasured`); the count taken before a task runs is the
    watermark its new notes are read from, so one task's refusal can never be
    mistaken for the next task's.
    """
    try:
        return len(getattr(ctx, "unmeasured", None) or ())
    except Exception:
        return 0


def _refusal_notes(ctx, since: int) -> "list[str]":
    try:
        return [str(n) for n in (getattr(ctx, "unmeasured", None) or ())[since:]]
    except Exception:
        return []


def measure_clean_tasks(ctx: TaskContext, keys, reasons=None) -> dict:
    """Run the given allowlisted task functions with the dry-run ctx and
    return {key: estimated bytes}.

    A key whose task was never measured is ``None``, NOT 0. MED-002: the old
    version recorded 0 for every failure, so a category that blew up mid-scan
    was painted as "0 bytes" and graded "A - already empty" by
    health_scan.grade_junk. The app asserted a clean bill of health for
    something it never looked at, inverting its own honesty contract. 0 is a
    real measurement; None is the absence of one, and grade_junk already
    grades None as "?" - this function was simply never handing it one.

    A refusal is the same absence, and used to slip through as 0: several
    cleaners skip while the thing they clean is live (Office with unsynced
    edits, Steam mid-download, FiveM in a session) or when the list they judge
    against is untrustworthy, and a skip returns 0 - indistinguishable from an
    already-empty folder. Such a task calls `mark_unmeasured(ctx, reason)` next
    to that 0; any note recorded during its run turns its result into None here,
    so "Office is running" can no longer read as "Office cache is clean".

    `reasons`, when given, is filled with {key: the task's own reason} for every
    refusal, so a headless caller (`--dry-run`) can say WHY a size is unknown
    instead of only that it is.

    Honors ctx.cancelled() between tasks."""
    by_key = _clean_task_by_key()
    out = {}
    for key in keys:
        if ctx.cancelled():
            break
        task = by_key.get(key)
        if key not in SCAN_ALLOWLIST:
            # A key outside the allowlist is deliberately NOT measured; callers
            # report those to the user as "skipped", and 0 is the documented
            # contract for them (see tasktab's preview).
            out[key] = 0
            continue
        if task is None:
            # Allowlisted but no task resolved for it: we genuinely could not
            # measure it. That is None (unknown), never 0 — the old code lumped
            # this with the skipped case and painted a real category as empty.
            out[key] = None
            continue
        mark = _refusal_mark(ctx)
        try:
            value = task.run(ctx) or 0
        except Exception as exc:
            out[key] = None          # unmeasured, NOT zero
            msg = f"  ! could not measure {key}: {exc}"
            try:
                ctx.log(msg)
            except Exception:
                pass
            try:
                ctx.set_status(f"Could not measure {key} — its size is unknown.")
            except Exception:
                pass
            continue
        notes = _refusal_notes(ctx, mark)
        if notes:
            out[key] = None          # declined to look, NOT empty
            if reasons is not None:
                reasons[key] = notes[0]
            # One line, not three: the task already logged its own refusal, and
            # make_measure_ctx wires log to the same status line set_status
            # paints, so a third restatement would only push the reason up the
            # dialog's status strip. This line names WHICH task is unknown.
            try:
                ctx.log(f"  ! {key} did not look: {notes[0]}")
            except Exception:
                pass
            continue
        out[key] = value
    return out


def category_members(keys, sizes, refused=None) -> "list[dict]":
    """The member cleaners behind one SCAN_CATEGORIES row, with their numbers.

    U-003: a category row answers "how much is in here", never "what is in
    here" - so a 16 GB row could not be audited without opening the Clean tab
    and guessing which of its groups fed the number. This hands the dialog one
    row per member: the task's own label and description (the same words the
    Clean tab shows) plus the size it measured in THIS walk, or the reason it
    declined to look. Unknown stays unknown (`size=None` + reason), exactly
    like the parent row's "+ ?".
    """
    refused = refused or {}
    try:
        tasks = _clean_task_by_key()
    except Exception:
        tasks = {}
    out = []
    for key in keys or ():
        task = tasks.get(key)
        size = sizes.get(key)
        out.append({
            "key": str(key),
            "label": str(getattr(task, "label", "") or key),
            "description": str(getattr(task, "description", "") or ""),
            "size": size if isinstance(size, int) else None,
            "reason": str(refused.get(key) or ""),
        })
    return out


def sum_measured(sizes: dict, refused=()):
    """Total a {key: bytes|None} mapping, or None if anything is unknown.

    Callers used to write `sum(sizes.values())`, which both invented a total
    and would now raise TypeError on a None. This keeps the 0-vs-None
    distinction all the way to the UI: one unmeasured category makes the whole
    total unknown rather than quietly too small.

    `refused` (a key -> reason mapping or any container of keys, as filled by
    `measure_clean_tasks`) separates the two ways a size can be None:

    * a FAILURE - something raised, and nobody knows what it would have found.
      That poisons the total, always.
    * a REFUSAL - the cleaner said, in its own words, that it did not look
      because Office/Steam/FiveM is live. A known skip. Treating it as a
      failure would blank a total the user can see most of, and only because
      Steam happens to be open - the surfaces that have a row to show it in
      (Storage Insight, Preview, --dry-run) print "X + ?" instead.

    So: pass `refused` where the caller can name the skips, and leave it empty
    where an unknown byte count must never be presented as a total.
    """
    if not sizes:
        return 0
    for key, value in sizes.items():
        if value is None and key not in refused:
            return None
    return sum_known(sizes)


def count_unmeasured(sizes: dict) -> int:
    return sum(1 for v in sizes.values() if v is None)


def sum_known(sizes: dict) -> int:
    """Total of the values that WERE measured, ignoring the unknown ones.

    For a row, not for a headline on its own. A category whose Office cache was
    skipped because Office is open still has four other tasks' sizes worth
    showing - "12.4 GB + ?" says more than "? unknown" and, unlike 0, never
    claims the category is empty. `sum_measured` is what decides whether that
    number may be called a total (see its `refused` argument).
    """
    return sum(v for v in sizes.values() if v is not None)


def validate_allowlist() -> "tuple[bool, list[str]]":
    """(ok, problems): the declarations must agree with the live Clean table.

    Run by dev/tools/verify_dryrun_scope.py on every CI run. It used to check one
    direction only - that an allowlisted key exists - which is the direction
    that fails loudly. The direction that rots silently is the other one: a
    task dropped from the allowlist without a line in SCAN_EXCLUDED, a
    category naming a key nobody measures, or the same key in two categories
    (counted twice). Each of those shows up as a smaller or larger number in
    the dialog with no visible cause, so they are failures here.
    """
    by_key = _clean_task_by_key()
    problems = []
    for key in sorted(SCAN_ALLOWLIST):
        if key not in by_key:
            problems.append("allowlisted key %r is not a Clean task" % key)
    for key, why in sorted(SCAN_EXCLUDED.items()):
        if key not in by_key:
            problems.append("excluded key %r is not a Clean task" % key)
        if key in SCAN_ALLOWLIST:
            problems.append("%r is both measured and excluded" % key)
        if not why.strip():
            problems.append("%r is excluded with no reason" % key)
    for key in sorted(by_key):
        if key not in SCAN_ALLOWLIST and key not in SCAN_EXCLUDED:
            problems.append(
                "%r is neither measured nor excluded - declare which, and why"
                % key)
    seen = {}
    for title, keys, _tip in SCAN_CATEGORIES:
        for key in keys:
            if key not in SCAN_ALLOWLIST:
                problems.append(
                    "category %r measures %r, which is not allowlisted"
                    % (title, key))
            seen[key] = seen.get(key, 0) + 1
    for key in sorted(SCAN_ALLOWLIST):
        if key not in seen:
            problems.append(
                "%r is measured but in no category, so the dialog cannot show it"
                % key)
        elif seen[key] > 1:
            problems.append(
                "%r appears in %d categories - it would be counted twice"
                % (key, seen[key]))
    return (not problems, problems)


# --------------------------------------------------------------------------- #
# 1b. Scan coordinator (Phase-6 orchestration) — one walk serves all three
#     consumers: Storage dialog, Health's junk sensor, the Low-Space nudge.
#     A process-wide mutex prevents concurrent disk walks; a 5-minute TTL
#     cache lets Health/Nudge quote a fresh walk instead of re-walking.
# --------------------------------------------------------------------------- #

import threading as _threading

_SCAN_MUTEX = _threading.Lock()  # L08: eager — no lazy double-init race
# (total_bytes, timestamp, declined reasons) or None. The reasons ride the
# total on purpose: a walk that skipped a live Office/Steam/FiveM cache is a
# LOWER BOUND, and a consumer quoting the number out of this cache (Health)
# has to be able to say so. Keeping them apart meant the same walk was honest
# in Storage Insight and a complete-looking figure in the Health report.
_ESTIMATE_CACHE = None
_ESTIMATE_TTL_S = 300.0


def _scan_mutex():
    return _SCAN_MUTEX


def acquire_scan(cancelled=None, timeout=30.0):
    """Cooperative scan slot: blocks up to `timeout` for a prior walk to
    finish (Storage closing often cancels its walk mid-flight, so the
    wait is short), then gives up and returns False — callers degrade
    to 'no estimate', never queue up three walks behind each other."""
    import time as _time
    m = _scan_mutex()
    deadline = _time.time() + max(0.0, timeout)
    while True:
        got = m.acquire(timeout=0.25)
        if got:
            return True
        try:
            if cancelled is not None and cancelled():
                return False
        except Exception:
            return False
        try:
            if _time.time() >= deadline:
                return False
        except Exception:
            return False


def release_scan():
    try:
        _scan_mutex().release()
    except Exception:
        pass


def cached_estimate(max_age_s=None):
    """(total_bytes, timestamp) of the last full-allowlist walk when
    fresh enough, else None. Pure read; never walks."""
    import time as _time
    try:
        total, stamp = _ESTIMATE_CACHE[0], _ESTIMATE_CACHE[1]
        ttl = _ESTIMATE_TTL_S if max_age_s is None else max_age_s
        if total is not None and (_time.time() - stamp) <= ttl:
            return (total, stamp)
    except Exception:
        pass
    return None


def cached_declined():
    """The reasons the cleaners that skipped the CACHED walk gave, if any.

    Read next to a fresh `cached_estimate()` hit: the cache is one record, so
    the reasons belong to the total in it. Empty means the walk looked at
    everything it could - the number is a complete estimate.
    """
    try:
        return tuple(_ESTIMATE_CACHE[2] or ())
    except Exception:
        return ()


def store_estimate(total_bytes, declined=()):
    """Publish a finished walk's total and why it skipped what it skipped.

    Called by the walker itself (Storage Insight or `measure_all_cached`).
    `declined` is the reasons the skipped cleaners gave, in their own words.
    """
    global _ESTIMATE_CACHE
    import time as _time
    try:
        _ESTIMATE_CACHE = (total_bytes, _time.time(), tuple(declined or ()))
    except Exception:
        pass


#: Below this the finding is not worth a row on the Health tab or a card in
#: the hub. The app still shows the exact number inside Storage Insight; the
#: bus is for conclusions, not every measurement.
JUNK_FINDING_BYTES = 5 * (1024 ** 3)


def publish_junk_finding(total_bytes, categories, declined=0) -> list:
    """Publish a complete junk walk's conclusion (AUDIT TX-002).

    Only a complete, uncancelled walk may call this. A total of None means
    at least one category could not be measured; that is not evidence of a
    clean machine, so the previous finding is left alone rather than cleared.
    Below the threshold the namespace is cleared, so cleaning up really does
    remove the warning from Health/hub.
    """
    if total_bytes is None:
        return []
    try:
        from app import insights
        from app.insights import Finding
        from app.utils import format_bytes
        import time as _time
    except Exception:
        return []
    if total_bytes < JUNK_FINDING_BYTES:
        try:
            return insights.replace("storage.junk")
        except Exception:
            return []
    declined = max(0, int(declined or 0))
    detail = ("Measured across %d categories. Open Storage to see exactly "
              "what would be removed." % max(1, int(categories or 1)))
    if declined:
        detail += (" %d cache%s could not be fully read while an app was "
                   "using %s." % (declined, "" if declined == 1 else "s",
                                  "it" if declined == 1 else "them"))
    finding = Finding(
        id="storage.junk",
        domain="storage",
        severity="warn",
        title="%s of junk can be reclaimed" % format_bytes(total_bytes),
        detail=detail,
        evidence={"bytes": int(total_bytes), "categories": int(categories or 0),
                  "declined": declined},
        action={"kind": "route", "target": "storage",
                "label": "Open Storage"},
        observed_at=_time.time(),
        confidence="partial" if declined else "measured",
    )
    try:
        return insights.replace("storage.junk", finding)
    except Exception:
        return []


def invalidate_junk_reading(task_keys, removed_bytes) -> bool:
    """Drop the junk reading when a finished run spent part of its estimate.

    Both the published finding and the cached walk quote what the measured
    cleaners WOULD free. A run that removed bytes from any of them has already
    spent part of that estimate, so the number still on the board is a PRE-RUN
    reading: the Health row and the hub card would be telling the user to fix
    what this run just fixed, and the sensor card would quote the same stale
    total from the 5-minute cache. The finding is retracted and the cache is
    dropped; the next walk (Storage Insight or the next checkup) republishes a
    number that is true again.

    A run that removed nothing (everything locked, or nothing selected) leaves
    the estimate as true as it was, so the reading stands - clearing it there
    would be throwing away a measurement nobody has contradicted.

    `task_keys` is the run's selection and `removed_bytes` what its tasks
    reported removing (`freed.Account.counted`, so a run whose deletions the
    disk has not visibly gained back still counts: those files are gone).
    Returns True when the finding was retracted. Never raises: retracting is
    best-effort, like publishing.
    """
    global _ESTIMATE_CACHE
    try:
        if int(removed_bytes or 0) <= 0:
            return False
        keys = {str(k or "") for k in (task_keys or ())}
        if not keys & set(SCAN_ALLOWLIST):
            return False
        _ESTIMATE_CACHE = None
        from app import insights
        return bool(insights.retract("storage.junk"))
    except Exception:
        return False


def measure_all_cached(set_status=None, cancelled=None, declined=None):
    """Full-allowlist estimate with cache + coordination (Health/Nudge
    path). Fresh cache hit: instant, zero disk. Otherwise: takes the
    scan slot (brief wait, honest give-up), walks once, publishes the
    cache AND the walk's conclusion on the findings bus (a cache hit
    publishes nothing: the walk that filled the cache already said it),
    releases. Returns total bytes, or None when it couldn't run
    (F11: 0-vs-None — give-up/cancel must not grade as 'A / empty', and
    a cancelled walk must not leave a partial total behind as a reading).

    `declined`, when given, is filled with the reasons any cleaner gave for
    skipping (Office/Steam/FiveM live). A refusal is a known skip, so the
    total stays a number - but a LOWER BOUND, and the caller that shows it has
    to be able to say so. The reasons come from the cached walk too: quoting
    the cached number without its skips would turn a lower bound back into a
    complete-looking total."""
    hit = cached_estimate()
    if hit is not None:
        if declined is not None:
            declined.extend(cached_declined())
        return hit[0]
    if not acquire_scan(cancelled=cancelled):
        return None
    try:
        if cancelled is not None and cancelled():
            return None
        # BUG-004: everything below used to sit INSIDE that if-block, after its
        # `return None` -- an indentation slip that made the whole body
        # unreachable, so this function could only ever return None (or a stale
        # cache hit). Measured with the walker instrumented: it was never
        # invoked. The Health "Reclaimable Junk" card therefore always graded
        # "?" with "another scan is running", and the low-space chip tooltip
        # never gained a reclaimable number.
        ctx = make_measure_ctx(set_status=set_status, cancelled=cancelled)
        # MED-002: sum the measured values, but let a single unmeasured
        # category make the TOTAL unknown rather than silently too small.
        # `refused` distinguishes a cleaner that said "Steam is running, I did
        # not look" (a known skip, counted as 0 in this lower-bound total and
        # named in its own row elsewhere) from one that blew up (unknown).
        refused = {}
        total = sum_measured(
            measure_clean_tasks(ctx, sorted(SCAN_ALLOWLIST), refused), refused)
        # A walk cut short by a cancel is not a reading. `measure_clean_tasks`
        # breaks between tasks, so the keys it never reached are simply ABSENT
        # from the mapping and the sum would look complete while silently
        # missing whatever came after the click. Storing that would grade the
        # Health card from a partial total for the next five minutes, and
        # publishing it would put the same short number on the bus as a
        # measured one. Nothing was concluded, so the answer is None.
        if cancelled is not None and cancelled():
            return None
        if total is not None:
            store_estimate(total, tuple(refused.values()))
            if declined is not None:
                declined.extend(refused.values())
            # A finished walk also tells the rest of the app what it found,
            # exactly like Storage Insight's own walk does. Until this, the
            # checkup and the low-space nudge walked the same folders and told
            # nobody: a junk row could only ever appear - or come back after a
            # clean invalidated the reading - if the user happened to open
            # Storage. A cache HIT above publishes nothing, because the walk
            # that filled the cache already said it.
            try:
                publish_junk_finding(total, len(SCAN_CATEGORIES),
                                     len(refused))
            except Exception:
                pass
        return total
    finally:
        release_scan()


# --------------------------------------------------------------------------- #
# 2. Biggest-games measurement — read-only size walks
# --------------------------------------------------------------------------- #

def _is_link(path: str) -> bool:
    """Reparse-point/junction guard for the size walk.

    LOW-014: this was a hand-rolled duplicate of the cleaner's
    _is_reparse_point, and it got all three things wrong that the real one is
    careful about:

      1. It FAILED OPEN. `except Exception: return False` means "not a link",
         so any error told the walk to descend. The real helper fails CLOSED -
         utils.py:47-48 returns True and documents why: refuse to walk rather
         than risk following a planted junction.
      2. Its INVALID_FILE_ATTRIBUTES check was DEAD CODE. With no `restype`
         set, ctypes returns a c_int, so 0xFFFFFFFF arrives as -1 and the
         comparison never matched - meaning that path fell through to the
         "return bool(attrs & 0x400)" line with attrs == -1.
      3. `os.path.islink` alone is wrong on Windows: it is False for a real
         junction, which is exactly what this guard exists to catch.

    It now delegates to the single correct implementation rather than keeping a
    third copy of the rule. app.utils is already imported here (TaskContext),
    so this adds no dependency - it removes a divergent one.
    """
    from app.utils import _is_reparse_point
    return _is_reparse_point(path)


def measure_folder_tree(path: str, cancelled=None) -> int:
    """Sum of file bytes under path (iterative, no recursion limit risk).
    Skips reparse points; checks cancelled() every 256 files AND every
    directory (L07: a single flat dir with 100k files used to stall Stop).
    Returns the partial total on cancel — the dialog labels partial scans
    honestly."""
    total = 0
    if not path or not os.path.isdir(path) or _is_link(path):
        return 0
    is_cancelled = cancelled or (lambda: False)
    stack = [path]
    checked = 0
    while stack:
        if is_cancelled():
            break
        cur = stack.pop()
        try:
            with os.scandir(cur) as it:
                for entry in it:
                    try:
                        checked += 1
                        if checked % 256 == 0 and is_cancelled():
                            break
                        if entry.is_symlink():
                            continue
                        if entry.is_dir(follow_symlinks=False):
                            p = entry.path
                            if not _is_link(p):
                                stack.append(p)
                        elif entry.is_file(follow_symlinks=False):
                            try:
                                total += entry.stat(follow_symlinks=False).st_size
                            except OSError:
                                continue
                    except OSError:
                        continue
        except OSError:
            continue
    return total


def _steam_library_roots() -> "list[str]":
    """All Steam library roots: install root + libraryfolders.vdf extras.
    VDF parsed with a narrow regex (only "path" lines) — no VDF parser
    dependency for one field."""
    roots = []
    steam_root = ""
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                            r"Software\Valve\Steam") as k:
            steam_root, _ = winreg.QueryValueEx(k, "SteamPath")
    except Exception:
        steam_root = ""
    # Steam writes SteamPath with forward slashes — normalize so every
    # downstream join produces clean Windows paths
    steam_root = (steam_root or "").replace("/", "\\")
    if not steam_root or not os.path.isdir(steam_root):
        pf86 = os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")
        steam_root = os.path.join(pf86, "Steam")
    if os.path.isdir(steam_root):
        roots.append(steam_root)
        vdf = os.path.join(steam_root, "steamapps", "libraryfolders.vdf")
        try:
            with open(vdf, "r", encoding="utf-8", errors="replace") as f:
                for m in re.finditer(r'"path"\s+"([^"]+)"', f.read()):
                    p = m.group(1).replace("\\\\", "\\")
                    # normalize any stray forward slashes so downstream
                    # joins produce clean Windows paths (tooltips read
                    # these strings directly)
                    p = p.replace("/", "\\")
                    if os.path.isdir(p) and p not in roots:
                        roots.append(p)
        except OSError:
            pass
    return roots


def _epic_installs() -> "list[tuple[str, str]]":
    """[(display name, install path)] from Epic manifest .item files
    (JSON, first-party format). Unparseable/missing dir -> skipped."""
    out = []
    progdata = os.environ.get("ProgramData", r"C:\ProgramData")
    man_dir = os.path.join(progdata, "Epic", "EpicGamesLauncher",
                           "Data", "Manifests")
    try:
        names = glob.glob(os.path.join(man_dir, "*.item"))
    except OSError:
        names = []
    for fn in names:
        try:
            with open(fn, "r", encoding="utf-8", errors="replace") as f:
                data = json.load(f)
            loc = data.get("InstallLocation", "")
            name = data.get("DisplayName", "") or os.path.basename(loc)
            if loc and os.path.isdir(loc):
                out.append((name, loc))
        except (OSError, ValueError, AttributeError):
            continue
    return out


def _default_root_games() -> "list[tuple[str, str, str]]":
    """[(launcher, game, path)] from well-known default install roots.
    Only top-level directories that exist; each entry guarded."""
    out = []
    pf = os.environ.get("ProgramFiles", r"C:\Program Files")
    pf86 = os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")
    sysdrive = os.environ.get("SYSTEMDRIVE", "C:") + "\\"
    roots = [
        ("GOG", os.path.join(sysdrive, "GOG Games")),
        ("Ubisoft", os.path.join(pf86, "Ubisoft", "Ubisoft Game Launcher", "games")),
        ("Riot", os.path.join(sysdrive, "Riot Games")),
        ("Xbox", os.path.join(sysdrive, "XboxGames")),
        ("EA", os.path.join(pf, "EA Games")),
    ]
    for launcher, root in roots:
        try:
            if not os.path.isdir(root):
                continue
            with os.scandir(root) as it:
                for entry in it:
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            out.append((launcher, entry.name, entry.path))
                    except OSError:
                        continue
        except OSError:
            continue
    return out


def find_game_libraries(cancelled=None, on_game=None) -> list:
    """Ordered-biggest-first list of dicts {launcher, name, path, bytes,
    drive}. Steam common dirs + Epic manifests + default roots. Each
    game measured with measure_folder_tree (cancellable, partial-safe).
    on_game(name) is called per game for live dialog status (optional)."""
    is_cancelled = cancelled or (lambda: False)
    games = []
    seen_paths = set()

    def _add(launcher, name, path):
        if is_cancelled():
            return False
        norm = os.path.normcase(os.path.abspath(path))
        if norm in seen_paths or not os.path.isdir(path):
            return True
        seen_paths.add(norm)
        if on_game is not None:
            try:
                on_game(name)
            except Exception:
                pass
        size = measure_folder_tree(path, cancelled=is_cancelled)
        drive = os.path.splitdrive(path)[0].upper() or "C:"
        games.append({"launcher": launcher, "name": name, "path": path,
                      "bytes": size, "drive": drive})
        return True

    # Steam (per-library common dirs)
    for lib in _steam_library_roots():
        if is_cancelled():
            break
        common = os.path.join(lib, "steamapps", "common")
        try:
            if not os.path.isdir(common):
                continue
            with os.scandir(common) as it:
                entries = sorted(
                    (e.name, e.path) for e in it
                    if e.is_dir(follow_symlinks=False))
        except OSError:
            continue
        for name, path in entries:
            if not _add("Steam", name, path):
                break

    # Epic (manifest installs)
    if not is_cancelled():
        for name, path in _epic_installs():
            if not _add("Epic", name, path):
                break

    # Default roots (GOG / Ubisoft / Riot / Xbox / EA)
    if not is_cancelled():
        for launcher, name, path in _default_root_games():
            if not _add(launcher, name, path):
                break

    games.sort(key=lambda g: g["bytes"], reverse=True)
    return games


# --------------------------------------------------------------------------- #
# 3. Write benchmark — honest sequential MB/s, temp file always removed
# --------------------------------------------------------------------------- #

BENCH_MB = 32
_BENCH_CHUNK = 1 << 20  # 1 MB incompressible-ish chunks


def write_benchmark(drive_root: str = "C:\\", size_mb: int = BENCH_MB,
                    cancelled=None, progress_cb=None):
    """Sequential write+read benchmark on one drive.

    Writes `size_mb` MB to a uniquely-named temp file in the drive's
    temp dir with fsync, reads it back, returns (write_mbps, read_mbps).
    (None, None) when cancelled or on any failure — never a fake 0.
    The temp file is removed on every path (success, cancel, error).
    stdlib only; Tk-free (the dialog threads + hops this)."""
    is_cancelled = cancelled or (lambda: False)
    try:
        import tempfile as _tf
        tmpdir = _tf.gettempdir()
        if os.path.splitdrive(os.path.abspath(tmpdir))[0].upper() != \
                os.path.splitdrive(os.path.abspath(drive_root))[0].upper():
            # Benchmark ON the requested drive. The old fallback was
            # <drive>:\Windows\Temp, which almost never exists off the system
            # drive, so "Speed" on D:/E: always answered "could not measure".
            # A unique file at the drive root needs no new directory and is
            # writable for the drive's owner; if not, the honest answer is
            # still None (mkstemp raises below) rather than C: numbers.
            tmpdir = os.path.abspath(drive_root)
        fd, path = _tf.mkstemp(prefix="cleaner_bench_", suffix=".bin", dir=tmpdir)
        os.close(fd)
    except Exception:
        return None, None
    total = max(1, int(size_mb)) * (1 << 20)
    data = None
    try:
        data = os.urandom(_BENCH_CHUNK)
    except Exception:
        pass
    if data is None:
        try:
            os.remove(path)
        except Exception:
            pass
        return None, None
    write_mbps = read_mbps = None
    try:
        # --- sequential write (fsync so the number isn't page-cache fiction)
        t0 = time.perf_counter()
        written = 0
        with open(path, "wb") as fh:
            while written < total:
                if is_cancelled():
                    return None, None
                fh.write(data)
                written += len(data)
                try:
                    if progress_cb is not None:
                        progress_cb(0.5 * written / total)
                except Exception:
                    pass
            try:
                fh.flush()
                os.fsync(fh.fileno())
            except Exception:
                pass
        dt = time.perf_counter() - t0
        if dt > 0 and not is_cancelled():
            write_mbps = (written / dt) / 1_000_000.0
        # --- sequential read back
        t0 = time.perf_counter()
        got = 0
        with open(path, "rb") as fh:
            while True:
                if is_cancelled():
                    return None, None
                chunk = fh.read(_BENCH_CHUNK)
                if not chunk:
                    break
                got += len(chunk)
                try:
                    if progress_cb is not None:
                        progress_cb(0.5 + 0.5 * got / max(1, written))
                except Exception:
                    pass
        dt = time.perf_counter() - t0
        if dt > 0 and not is_cancelled() and got:
            read_mbps = (got / dt) / 1_000_000.0
        if is_cancelled():
            return None, None
        return write_mbps, read_mbps
    except Exception:
        return None, None
    finally:
        try:
            if os.path.exists(path):
                os.remove(path)
        except Exception:
            pass
