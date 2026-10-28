"""The snapshot/validation layer every reversible tweak shares.

Extracted from app/tasks/tweak_tasks.py (audit 2026-10, R-002, stage 1). The
domain split that report asks for is a two-stage job: the apply/revert pairs in
every domain call this layer, so it has to stand alone before any of them can
move. This module is that first stage.

What it holds:

  * snapshot-before-apply for powercfg pairs, and the restore path that puts
    back exactly what was read (the original bug it fixes: reverts wrote a
    hardcoded "default" that was never the user's value);
  * validation of every snapshot value on the way IN. Snapshots persist in the
    user-writable config.json, so a hand-edited or corrupted entry must be
    rejected, not passed to `powercfg`/`reg`;
  * scheduled-task name validation and the per-task prior-state bookkeeping,
    including the exact enable/disable restore;
  * the Ultimate Performance scheme lookup used by the row that toggles it.

No task definitions live here, and nothing here touches Tk. `tweak_tasks`
re-exports every name, so the apply/revert functions and `advanced_tasks`
kept their imports unchanged.
"""
from __future__ import annotations

from app.config_persist import (
    clear_tweak_snapshot, get_tweak_snapshot, merge_tweak_snapshot,
    save_tweak_snapshot,
)
from app.utils import IS_WINDOWS, TaskContext, powercfg_query_indexes, run_cmd
from app.tasks.ps import ps_argv, run_ps

#: The hidden Ultimate Performance power scheme. Defined here because the
#: scheme lookup below is the only code that needs it by name; tweak_tasks
#: imports it so the row that enables the plan and the verification both use
#: one literal.
ULTIMATE_PERF_GUID = "e9a42b02-d5df-448d-aa00-03f14749eb61"

# --------------------------------------------------------------------------- #
# Snapshot-before-apply helpers for powercfg-based tweaks (fix for M1:
# reverts used to write hardcoded "default" values instead of restoring
# what was actually on the machine before the tweak ran).
# --------------------------------------------------------------------------- #

def _snapshot_powercfg_pairs(ctx: TaskContext, task_id: str, pairs: list[tuple[str, str]]):
    """Read the CURRENT value of each (subgroup, setting) pair and save it
    under task_id, but only if no snapshot is already on file for this task
    — so re-applying a tweak twice in a row doesn't overwrite the true
    original value with the tweak's own output.

    audit fix (M7): the DC (battery) index is snapshotted too, stored under
    '<key>:dc', so a laptop's battery-side value survives a revert instead
    of being reset to whatever the fallback says.

    B6 audit fix: reads both sides in ONE locale-independent query (GUID-
    based, structural parse in utils.powercfg_query_indexes) instead of
    two separate English-label regex parsers (utils.powercfg_query_index +
    a local DC twin) that silently parsed empty on localized Windows."""
    values = {}
    for subgroup, setting in pairs:
        ac_val, dc_val = powercfg_query_indexes(ctx, subgroup, setting)
        if ac_val is not None:
            values[f"{subgroup}:{setting}"] = ac_val
        if dc_val is not None:
            values[f"{subgroup}:{setting}:dc"] = dc_val
    save_tweak_snapshot(task_id, values)


# --------------------------------------------------------------------------- #
# Snapshot-value validation (F-1 audit fix)
# --------------------------------------------------------------------------- #
# tweak snapshots persist in the USER-WRITABLE config.json. Everything the
# legit apply paths ever save is produced by this module's own parsers —
# enum strings, parsed ints, extracted GUIDs — so anything outside these
# shapes cannot be prior machine state. Five revert flows interpolate
# snapshot values straight into shell=True run_cmd command strings; a
# poisoned snapshot (hand-edited file or a same-user process) would
# otherwise execute with the app's privilege (admin, typically) the next
# time Undo runs. Validate at revert time and fail closed: legit values
# are never rejected, and the snapshot is kept (C6) for manual recovery.

_SC_START_TYPES = ("boot", "system", "auto", "demand", "disabled")


def _valid_snapshot_start_type(value) -> bool:
    """True if value is a start type this app could have snapshotted (the
    exact keyword set `sc config start=` accepts)."""
    return isinstance(value, str) and value in _SC_START_TYPES


#: `sc config start=` accepts these spellings for the same state. A restore
#: verified against one of them must not be called a failure just because the
#: canonical form differs.
_SC_START_ALIASES = {
    "auto": "auto", "automatic": "auto",
    "demand": "demand", "manual": "demand",
    "disabled": "disabled", "disable": "disabled",
    "boot": "boot", "system": "system",
}


def _start_types_equal(a, b) -> bool:
    """Compare two service start types, tolerating case, whitespace and
    sc's accepted synonyms. Used by MED-009's verified restore.

    Returns False for anything unrecognisable rather than assuming equality —
    a restore that cannot be confirmed must not be reported as confirmed.
    """
    if not isinstance(a, str) or not isinstance(b, str):
        return False
    na = _SC_START_ALIASES.get(a.strip().lower())
    nb = _SC_START_ALIASES.get(b.strip().lower())
    if na is None or nb is None:
        return False
    return na == nb


# --------------------------------------------------------------------------- #
# SEC-001: scheduled-task NAMES are config-file data.
#
# The per-task prior states recorded by the telemetry tweaks are keyed by
# task name, and those names come back out of config.json — which lives in
# the user's profile and is hand-editable. Two call sites used to hand that
# string straight to a shell:
#
#   revert_stop_telemetry   ->  run_cmd(ctx, f'schtasks /change /tn "{_task}" ...')
#   revert_nvidia_telemetry ->  _set_task_enabled() -> a hand-built PowerShell
#                               string with nested quotes and NO escaping
#
# Either one turns a tampered config.json into arbitrary command execution,
# ELEVATED, at Undo time. The sibling `sc` call two lines away was already
# fixed to argv + shell=False (see revert_nvidia_telemetry_optout) with the
# reasoning written down; these two were missed.
#
# Both are now argv + shell=False, so cmd.exe never parses the value at all.
# _valid_scheduled_task_name is defence in depth on top of that: it is the
# same belt-and-braces posture the module's threat model (see the comment at
# the top of this file) takes for every other config-derived value.
# --------------------------------------------------------------------------- #

#: Every character a genuine Windows scheduled-task name is built from.
#: Real examples this must keep accepting:
#:   \Microsoft\Windows\Application Experience\Microsoft Compatibility Appraiser
#:   NvTmRep_C_wxyz1234
#: Notably absent: quotes, %, &, |, ;, $, backtick, CR/LF, and every other
#: character cmd.exe or PowerShell would treat as syntax.
_TASK_NAME_CHARS = frozenset(
    "abcdefghijklmnopqrstuvwxyz"
    "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    "0123456789"
    " _-.\\"
)

#: Longest real task name seen in the Microsoft\ tree, with headroom. A
#: scheduled task path cannot be longer than MAX_PATH anyway.
_MAX_TASK_NAME_LEN = 260


def _valid_scheduled_task_name(name) -> bool:
    """True if `name` is shaped like a real Windows scheduled-task name.

    This is a shape check, not an allowlist of known tasks: the NVIDIA
    telemetry tasks carry random GUID suffixes, so the exact set cannot be
    enumerated. What can be rejected is anything containing a character that
    a command interpreter would treat as syntax.
    """
    if not isinstance(name, str):
        return False
    if not name or len(name) > _MAX_TASK_NAME_LEN:
        return False
    if name.strip() != name:          # leading/trailing whitespace is a sign
        return False                   # of a crafted value, not a real task
    return all(ch in _TASK_NAME_CHARS for ch in name)


def _ps_quote(value: str) -> str:
    """Single-quote a value for embedding inside a PowerShell -Command string.

    Only needed for the PowerShell cmdlets, which have no argv form for
    -TaskName. Within single quotes PowerShell treats every character
    literally, so doubling an embedded quote is the complete escape — and
    because the caller passes the whole thing as ONE argv element with
    shell=False, cmd.exe is not involved at all.
    """
    return "'%s'" % str(value).replace("'", "''")


# --------------------------------------------------------------------------- #
# BUG-016: per-task prior state for scheduled tasks.
#
# The telemetry tweaks disable/enable task GROUPS by wildcard, because the
# real names carry random GUID suffixes (NvTmRep_C<guid>). But "enable every
# task matching NvTmRep_C*" on revert also re-enables any task the user (or a
# corporate policy) had ALREADY disabled before the app touched it — the
# inverse of the promised behaviour, and re-arming exactly the telemetry
# collection the user asked to suppress.
#
# So resolve the real names and record each one's state BEFORE changing it.
# Revert then restores each recorded task to its recorded state and leaves
# everything else alone. The old snapshots have no task_states key and fall
# back to today's over-broad behaviour, which the finding accepts as better
# than nothing.
# --------------------------------------------------------------------------- #

_TASK_DISABLED = "Disabled"

#: The NVIDIA telemetry task groups, by prefix — the real names carry random
#: GUID suffixes, which is why wildcard matching is unavoidable. Kept in one
#: place so apply and revert cannot drift apart.
_NVIDIA_TASK_PATTERNS = ("NvTmRep_C", "NvTmRepOnLogon_C", "NvTmMon_C")

#: The ten CEIP / App-Experience diagnostic tasks the telemetry tweak
#: touches. Hoisted to module scope: it was duplicated verbatim in apply and
#: revert, so the two could drift, and a test could not import it.
_TELEMETRY_TASKS = (
    "\\Microsoft\\Windows\\Application Experience\\Microsoft Compatibility Appraiser",
    "\\Microsoft\\Windows\\Application Experience\\ProgramDataUpdater",
    "\\Microsoft\\Windows\\Application Experience\\StartupAppTask",
    "\\Microsoft\\Windows\\Application Experience\\PcaPatchDbTask",
    "\\Microsoft\\Windows\\Application Experience\\MareBackup",
    "\\Microsoft\\Windows\\Autochk\\Proxy",
    "\\Microsoft\\Windows\\Customer Experience Improvement Program\\Consolidator",
    "\\Microsoft\\Windows\\Customer Experience Improvement Program\\UsbCeip",
    "\\Microsoft\\Windows\\DiskDiagnostic\\Microsoft-Windows-DiskDiagnosticDataCollector",
    "\\Microsoft\\Windows\\Windows Error Reporting\\QueueReporting",
)
# historic alias, kept because the local name read better at the call sites
_telemetry_tasks = _TELEMETRY_TASKS


def _merge_tweak_snapshot(task_id, values):
    """Add keys to an existing snapshot without discarding it.

    `save_tweak_snapshot` deliberately refuses to overwrite a non-empty
    snapshot, which is right for the usual "keep the ORIGINAL pre-tweak
    value" rule but wrong here: the NVIDIA and telemetry applies record the
    service start type first and the per-task states a moment later, and the
    second write must not erase the first.

    ARCH-002: this used to do that merge by hand --

        existing = get_tweak_snapshot(task_id)
        existing.update(values)
        clear_tweak_snapshot(task_id)     # <-- snapshot now GONE
        save_tweak_snapshot(task_id, existing)

    -- which is three separate lock acquisitions. update_config holds
    _config_lock across load-mutate-save specifically to prevent lost
    updates, and this sequence defeated it: between the clear and the save
    the snapshot did not exist, so a crash, a concurrent --auto-clean, or a
    second writer in that window destroyed the task's entire undo history.
    merge_tweak_snapshot does the same merge in one atomic call.

    The module-level name is the seam every other tweak in this file uses,
    so a test can still intercept it.
    """
    if not values:
        return
    try:
        merge_tweak_snapshot(task_id, values)
    except Exception:
        # a bookkeeping failure must not abort the apply; the primary change
        # has already been made and revert still works from whatever is saved
        pass


def _scheduled_task_states(ctx, pattern):
    """{full task name: state} for every scheduled task matching `pattern`.

    Empty dict on any failure — the caller then falls back to the legacy
    wildcard behaviour rather than guessing.
    """
    try:
        import subprocess as _sp
        out = _sp.run(
            ps_argv("Get-ScheduledTask -TaskName '%s*' -ErrorAction SilentlyContinue | "
                    "ForEach-Object { $_.TaskName + '|' + $_.State }" % pattern),
            capture_output=True, text=True, timeout=30,
            creationflags=getattr(_sp, "CREATE_NO_WINDOW", 0),
        )
        if out.returncode != 0:
            return {}
    except Exception:
        return {}
    states = {}
    for line in (out.stdout or "").splitlines():
        line = line.strip()
        if not line or "|" not in line:
            continue
        name, _sep, state = line.rpartition("|")
        name = name.strip()
        if name:
            states[name] = state.strip()
    return states


def _collect_task_priors(ctx, patterns):
    """{task name: was-it-disabled} across every pattern, first match wins."""
    priors = {}
    for pattern in patterns:
        for name, state in _scheduled_task_states(ctx, pattern).items():
            priors.setdefault(name, state == _TASK_DISABLED)
    return priors


def _merge_task_priors(task_id, task_priors):
    """Record per-task priors, but only if none are recorded yet.

    B-0xx: merge_tweak_snapshot replaces the whole task_priors key. On a
    SECOND apply — reachable when a preset re-selects the tweak while it is
    already applied — the tasks are already disabled, so the freshly collected
    priors record was_disabled=True for every one, and revert then leaves them
    disabled forever. Keep the ORIGINAL collection, mirroring
    save_tweak_snapshot's refuse-to-overwrite rule.
    """
    if not task_priors:
        return
    existing = get_tweak_snapshot(task_id) or {}
    if isinstance(existing.get("task_priors"), dict) and existing["task_priors"]:
        return
    _merge_tweak_snapshot(task_id, {"task_priors": task_priors})


def _set_task_enabled(ctx, name, enabled):
    """Enable or disable one named scheduled task. True on success.

    SEC-001: this used to hand a config-file-derived name to a shell:

        'powershell -NoProfile -Command "%s-ScheduledTask -TaskName \'%s\' ..."'
                     % (verb, name)

    with no escaping whatsoever, under an implicit shell=True. A task name of
    x' ; calc.exe ; ' reached cmd.exe ELEVATED. Now: the name is shape-checked,
    quoted for PowerShell, and passed as a single argv element with
    shell=False, so no command interpreter other than PowerShell itself ever
    sees it -- and PowerShell treats single-quoted content literally.
    """
    if not _valid_scheduled_task_name(name):
        ctx.log("  ! refusing to touch scheduled task with an implausible "
                "name (not in config.json verbatim): %r" % (name,))
        return False
    verb = "Enable" if enabled else "Disable"
    return run_ps(
        ctx,
        "%s-ScheduledTask -TaskName %s -ErrorAction SilentlyContinue"
        % (verb, _ps_quote(name)),
        timeout=60) == 0


_SCHTASKS_ENABLED = "Enabled"
_SCHTASKS_DISABLED = "Disabled"


def _schtasks_state(ctx, task_name):
    """'Enabled' | 'Disabled' | None for one schtasks task, or None if the
    task does not exist or the query failed.

    Used by BUG-016: without knowing a task's prior state, Undo cannot tell
    "I disabled this" from "it was already off", and re-enables the latter.
    """
    try:
        import subprocess as _sp
        out = _sp.run(
            ["schtasks", "/Query", "/TN", task_name, "/FO", "LIST", "/V"],
            capture_output=True, text=True, timeout=30,
            creationflags=getattr(_sp, "CREATE_NO_WINDOW", 0),
        )
        if out.returncode != 0:
            return None
        for line in (out.stdout or "").splitlines():
            if ":" not in line:
                continue
            key, _sep, val = line.partition(":")
            if key.strip().lower() in ("scheduled task state", "status"):
                v = val.strip().lower()
                if v.startswith("disable"):
                    return _SCHTASKS_DISABLED
                if v:
                    return _SCHTASKS_ENABLED
        return None
    except Exception:
        return None


def _collect_schtasks_priors(ctx, task_names):
    """{task name: was-it-disabled} for each task that actually exists.

    A task that is absent yields None and is left out entirely, so revert
    never touches it.
    """
    priors = {}
    for name in task_names:
        state = _schtasks_state(ctx, name)
        if state is None:
            continue
        priors[name] = (state == _SCHTASKS_DISABLED)
    return priors


def _valid_snapshot_power_index(value) -> bool:
    """True if value is a powercfg setting index this app could have
    snapshotted (powercfg_query_indexes parses hex into a non-negative int)."""
    return (isinstance(value, int) and not isinstance(value, bool)
            and 0 <= value <= 0xFFFFFFFF)


def _valid_snapshot_guid(value) -> bool:
    """True if value is a canonical GUID string (active power schemes are
    captured via a 36-char hex-and-dash regex from powercfg output)."""
    import re as _re
    return (isinstance(value, str)
            and _re.fullmatch(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}"
                              r"-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}", value) is not None)


def _snapshot_validation_error(task_id: str, field: str, value) -> RuntimeError:
    return RuntimeError(
        f"Saved '{task_id}' snapshot has an invalid {field} "
        f"({value!r}) — it does not look like a value this app saved, so it "
        "was NOT executed. Nothing was changed; the snapshot was kept. "
        "If you hand-edited config.json, restore the real prior value there "
        "or clear the snapshot and re-apply the tweak."
    )


def _restore_powercfg_pairs(ctx: TaskContext, task_id: str, pairs: list[tuple[str, str]], fallback: dict[str, int]):
    """Restore each (subgroup, setting) pair from the saved snapshot. Falls
    back to a documented-correct default only if no snapshot exists (e.g.
    tweak was applied by an older app version, or config was cleared).

    audit fix (M7): the snapshot helper only read the AC index and restore
    only wrote setacvalueindex — on a laptop running on battery, a revert
    restored the wrong side (or nothing). Both sides are now snapshotted
    and restored; powercfg_query_index gains a DC variant.

    MED-011: this is now TWO PASSES - resolve and validate every value first,
    write second. It used to validate inside the write loop, so a poisoned (or
    simply corrupt) snapshot with a bad value at position 1 left position 0
    ALREADY restored and 1..n untouched. A half-reverted machine is worse than
    an un-reverted one: the user cannot tell which half moved, and the retry
    that follows starts from a state neither apply nor Undo ever described.
    _restore_reg_values already validates everything up front; this makes the
    powercfg twin match it.
    """
    snapshot = get_tweak_snapshot(task_id)
    had_snapshot = bool(snapshot)
    used_fallback = False

    # ---- pass 1: resolve + validate. Nothing is written in this pass. -------
    plan: list = []
    for subgroup, setting in pairs:
        key = f"{subgroup}:{setting}"
        value = snapshot.get(key)
        if value is None:
            value = fallback.get(key)
            used_fallback = True
        if value is None:
            continue
        # F-1: snapshot values flow into a shell command string — only a
        # parsed powercfg index (int) may ever run.
        if not _valid_snapshot_power_index(value):
            raise _snapshot_validation_error(task_id, f"power index for {key}", value)
        dc_value = snapshot.get(key + ":dc")
        if dc_value is not None and not _valid_snapshot_power_index(dc_value):
            raise _snapshot_validation_error(task_id, f"DC power index for {key}", dc_value)
        plan.append((key, subgroup, setting, value, dc_value))

    # ---- pass 2: write. Every value above is already known good. ----------
    failures: list = []
    for key, subgroup, setting, value, dc_value in plan:
        rc = run_cmd(ctx, ["powercfg", "/setacvalueindex", "scheme_current",
                           subgroup, setting, str(value)], shell=False)
        if rc != 0:
            failures.append(f"{key} AC")
        # snapshot may also carry the DC ("key:dc") value if the machine
        # was on battery at apply time
        if dc_value is not None:
            rc_dc = run_cmd(ctx, ["powercfg", "/setdcvalueindex", "scheme_current",
                                 subgroup, setting, str(dc_value)], shell=False)
            if rc_dc != 0:
                failures.append(f"{key} DC")
    rc_active = run_cmd(ctx, "powercfg /setactive scheme_current")
    if rc_active != 0:
        failures.append("setactive")
    if used_fallback and not snapshot:
        ctx.log("  (no saved prior value found — restored to documented Windows default instead)")
    # C6: keep the snapshot when the restore failed so the true original
    # survives for a retry; only a verified restore drops it.
    if failures:
        raise RuntimeError(
            f"Could not restore {task_id} ({', '.join(failures)}) — snapshot kept "
            "so Undo can be retried."
        )
    if had_snapshot:
        clear_tweak_snapshot(task_id)


def _active_scheme_guid() -> "str | None":
    """Return the GUID of the currently active power scheme, or None."""
    import subprocess as _sp
    import re as _re
    try:
        out = _sp.check_output(["powercfg", "/getactivescheme"], text=True,
                               stderr=_sp.STDOUT, timeout=15)
        m = _re.search(r"GUID:\s*([0-9a-fA-F-]{36})", out)
        if m:
            return m.group(1)
    except Exception:
        pass
    return None


_POWER_SCHEMES_KEY = r"SYSTEM\CurrentControlSet\Control\Power\User\PowerSchemes"
# powrprof.dll resource index for the Ultimate Performance display name.
# Language-independent: on non-English Windows the FriendlyName resolves to
# localized text, but the indirect-string resource index stays -19.
_UP_RESOURCE_INDEX = "-19,"


def _guids_equal(a: str, b: str) -> bool:
    return a.lower().replace("{", "").replace("}", "") == b.lower().replace("{", "").replace("}", "")


def _find_ultimate_scheme_guid() -> "str | None":
    """Find an existing 'Ultimate Performance' scheme GUID — complete and
    language-safe.

    L7 fix (LTSC/user-reported + 24H2 overlay quirk): the old code parsed
    `powercfg /list` stdout and required the literal English name. Two
    failure modes: (a) non-English Windows localizes the scheme name, so
    the match always failed; (b) on 24H2 with the power-mode OVERLAY
    active, `powercfg /list` hides every scheme except the active one —
    verified on the user's own machine, which showed 1 of 12 schemes.
    Both cases silently fell into the dead template-GUID fallback and
    'succeeded' while doing nothing.

    Fix: enumerate schemes from the registry (always complete, unaffected
    by overlay filtering) and identify Ultimate Performance via the
    powrprof.dll resource index (-19) in the indirect FriendlyName string,
    which is language-independent.
    """
    if not IS_WINDOWS:
        return None
    import winreg as _wr
    matches = []
    try:
        root = _wr.OpenKey(_wr.HKEY_LOCAL_MACHINE, _POWER_SCHEMES_KEY)
    except OSError:
        return None
    i = 0
    while True:
        try:
            sub = _wr.EnumKey(root, i)
            i += 1
        except OSError:
            break
        try:
            # I-001: close the subkey in a finally; a QueryValueEx raise
            # otherwise leaked the handle on every scheme lacking that value.
            k = _wr.OpenKey(root, sub)
            try:
                name = _wr.QueryValueEx(k, "FriendlyName")[0]
            finally:
                _wr.CloseKey(k)
        except OSError:
            continue
        # indirect string form: @%SystemRoot%\system32\powrprof.dll,-19,Ultimate Performance
        if _UP_RESOURCE_INDEX in str(name):
            matches.append(sub)
    _wr.CloseKey(root)
    if not matches:
        return None
    # The registry may contain the CANONICAL TEMPLATE GUID itself (it is
    # registered as a scheme on some builds, observed on the user's LTSC)
    # — but `setactive` REJECTS it ('Attempted to write to unsupported
    # setting', verified rc=1). So prefer any real duplicate; fall back to
    # the template GUID only as a last resort (callers must verify after
    # activating anyway).
    real = [g for g in matches if not _guids_equal(g, ULTIMATE_PERF_GUID)]
    return real[0] if real else matches[0]
