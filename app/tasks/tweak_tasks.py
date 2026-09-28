"""
Tweak tab — all reversible. Short gamer-friendly labels + tooltip details.
Fixed: Fast Startup uses HiberbootEnabled, not hibernate off; added safe new tweaks.
"""

import subprocess
import time

from app.utils import (
    TaskContext, TaskSkipped, TaskCancelled, reg_set_value, reg_set_value_checked, reg_delete_value, reg_delete_key, reg_get_value, run_cmd, run_cmd_checked, create_restore_point, IS_WINDOWS,
    resolve_asset_path, powercfg_query_indexes, sc_query_start_type, restart_explorer,
    atomic_write_text, exclusive_create_text,
)
from app.config_persist import save_tweak_snapshot, get_tweak_snapshot, clear_tweak_snapshot

if IS_WINDOWS:
    import winreg

ULTIMATE_PERF_GUID = "e9a42b02-d5df-448d-aa00-03f14749eb61"
BALANCED_GUID = "381b4222-f694-41f0-9685-ff5bb260df2e"
CONTEXT_MENU_CLSID = "{86ca1aa0-34aa-4e8b-a509-50c905bae2a2}"

# --------------------------------------------------------------------------- #
# Ad Blocker (hosts file) constants
# --------------------------------------------------------------------------- #
import os

_HOSTS_PATH = os.path.join(
    os.environ.get("WINDIR", "C:\\Windows"), "System32", "drivers", "etc", "hosts"
)
_HOSTS_BACKUP_PATH = _HOSTS_PATH + ".cleanertool_bak"
_ADBLOCK_MARKER_START = "# >>> Cleaner Tool AdBlock Start >>>"
_ADBLOCK_MARKER_END = "# <<< Cleaner Tool AdBlock End <<<"


import functools

@functools.lru_cache(maxsize=1)
def _has_ssd() -> bool:
    """Check if system has at least one SSD drive (cached — PowerShell is slow).

    C1 fix: the old command wrapped the PowerShell -eq comparand in escaped
    double quotes (\"SSD\"). Under shell=True, cmd.exe strips those before
    PowerShell ever sees them, so PowerShell received a bare `-eq SSD` with
    no value — a parse error — and this function always returned False,
    silently disabling all 4 SSD tweaks (and their reverts).
    Fix: build the argv as a list and call PowerShell with shell=False, so
    cmd.exe never gets a chance to mangle the quoting. -eq 'SSD' (single
    quotes, PowerShell's own string syntax) is used inside the script text
    since there's no outer shell to strip it now."""
    if not IS_WINDOWS:
        return False
    try:
        ps_script = "(Get-PhysicalDisk | Where-Object {$_.MediaType -eq 'SSD'} | Select-Object -First 1) -ne $null"
        result = subprocess.run(
            ["powershell", "-NoProfile", "-Command", ps_script],
            shell=False, capture_output=True, text=True, timeout=10,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        return result.returncode == 0 and result.stdout.strip().lower() == "true"
    except Exception:
        return False


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
    value" rule but wrong here: the NVIDIA apply records the service start
    type first and the per-task states a moment later, and the second write
    must not erase the first. Only ADDS or overwrites the keys it is given,
    so the original service value is preserved.
    """
    if not values:
        return
    try:
        # Deliberately the MODULE-level names, not a fresh import from
        # config_persist: that is the seam every other tweak in this file uses,
        # and re-importing bypassed it — a test could not intercept the merge,
        # so the merge silently wrote to the real user config instead.
        existing = dict(get_tweak_snapshot(task_id) or {})
        existing.update(values)
        # clear first so save_tweak_snapshot's "already present" guard does
        # not reject the merge
        clear_tweak_snapshot(task_id)
        save_tweak_snapshot(task_id, existing)
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
            ["powershell", "-NoProfile", "-Command",
             "Get-ScheduledTask -TaskName '%s*' -ErrorAction SilentlyContinue | "
             "ForEach-Object { $_.TaskName + '|' + $_.State }" % pattern],
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


def _set_task_enabled(ctx, name, enabled):
    """Enable or disable one named scheduled task. True on success."""
    verb = "Enable" if enabled else "Disable"
    return run_cmd(
        ctx,
        'powershell -NoProfile -Command "%s-ScheduledTask -TaskName \'%s\' '
        '-ErrorAction SilentlyContinue"' % (verb, name)) == 0


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
        rc = run_cmd(ctx, f"powercfg /setacvalueindex scheme_current {subgroup} {setting} {value}")
        if rc != 0:
            failures.append(f"{key} AC")
        # snapshot may also carry the DC ("key:dc") value if the machine
        # was on battery at apply time
        if dc_value is not None:
            rc_dc = run_cmd(ctx, f"powercfg /setdcvalueindex scheme_current {subgroup} {setting} {dc_value}")
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
        out = _sp.check_output("powercfg /getactivescheme", shell=True, text=True,
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
            k = _wr.OpenKey(root, sub)
            name = _wr.QueryValueEx(k, "FriendlyName")[0]
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


def apply_ultimate_performance(ctx: TaskContext):
    """Enable the hidden Ultimate Performance plan — idempotently and
    HONESTLY.

    H5: only duplicates the scheme if none exists yet (no junk schemes).
    L7 (LTSC/user-reported): the old code had a dead fallback — if the
    duplicate/re-scan failed it ran `setactive <TEMPLATE GUID>`, which is
    not an activatable scheme on ANY edition (verified: rc=1 'Attempted to
    write to unsupported setting'), then logged 'not supported on this
    edition' and returned None — which the runner counted as SUCCESS. The
    user (on Win11 IoT LTSC) watched this tweak 'succeed' while doing
    nothing. Fix: no dead fallbacks. Duplicate, find the resulting GUID,
    activate, then VERIFY with `getactivescheme` and raise on failure so
    the runner reports the tweak as failed instead of lying.
    """
    ctx.set_status("Enabling fastest power plan...")

    # Snapshot the user's current scheme so revert restores the REAL prior
    # plan (M1 pattern), not a hardcoded guess like 'Balanced'.
    # Snapshot-failure hygiene (C5): the snapshot is taken before the
    # mutating commands, so a later failure must NOT leave it behind —
    # get_tweak_state() treats any snapshot as "✓ Active". Only clear what
    # WE created (a pre-existing snapshot from an earlier success stays).
    prior_guid = _active_scheme_guid()
    had_snapshot = bool(get_tweak_snapshot("ultimate_performance"))
    if prior_guid:
        save_tweak_snapshot("ultimate_performance", {"active_scheme": prior_guid})

    try:
        guid = _find_ultimate_scheme_guid()
        if guid is None:
            # No scheme yet -> duplicate the hidden template, then re-detect
            # via the registry (language- and overlay-safe).
            rc = run_cmd(ctx, f"powercfg -duplicatescheme {ULTIMATE_PERF_GUID}")
            guid = _find_ultimate_scheme_guid()
            if rc != 0 or guid is None:
                raise RuntimeError(
                    "Could not create the Ultimate Performance power plan on this "
                    f"PC (powercfg returned {rc}). The plan may not be supported "
                    "by this Windows edition."
                )
        else:
            ctx.log("Ultimate Performance plan already present — reusing it (no duplicate created).")

        rc2 = run_cmd(ctx, f"powercfg -setactive {guid}")
        if rc2 != 0:
            raise RuntimeError(f"powercfg -setactive failed (code {rc2}).")

        # Verify it ACTUALLY took effect — the exact check the user ran by
        # hand when the old silent failure burned them.
        active = _active_scheme_guid()
        if active is None or not _guids_equal(active, guid):
            raise RuntimeError(
                "Power plan did not activate (active scheme is "
                f"{active or 'unknown'}, expected {guid})."
            )
    except Exception:
        if not had_snapshot:
            clear_tweak_snapshot("ultimate_performance")
        raise
    ctx.log(f"Verified: active power plan is now Ultimate Performance ({guid}).")


def _guids_equal(a: str, b: str) -> bool:
    return a.lower().replace("{", "").replace("}", "") == b.lower().replace("{", "").replace("}", "")


def revert_ultimate_performance(ctx: TaskContext):
    """Restore the power plan that was active BEFORE this tweak ran (from
    the M1 snapshot), falling back to Balanced only if no snapshot exists."""
    snapshot = get_tweak_snapshot("ultimate_performance")
    target = snapshot.get("active_scheme") if snapshot else None
    # F-1: the snapshot value flows into a shell command string — only a
    # canonical GUID this app could have parsed from powercfg may run.
    if target is not None and not _valid_snapshot_guid(target):
        raise _snapshot_validation_error("ultimate_performance", "active_scheme", target)
    if target and _guids_equal(target, ULTIMATE_PERF_GUID):
        # snapshot somehow holds the tweak's own output — don't restore
        # Ultimate as if it were the prior state
        target = None
    if target:
        # The snapshot may hold a DUPLICATE of Ultimate (random GUID) that
        # was active before apply — e.g. the user created it manually.
        # Restoring that would silently re-activate the tweak. Only treat
        # the saved scheme as 'prior state' if it is NOT an Ultimate plan.
        up = _find_ultimate_scheme_guid()
        if up and _guids_equal(target, up):
            ctx.log("  (saved prior plan is itself an Ultimate Performance plan — restoring Balanced instead)")
            target = None
    if not target:
        target = BALANCED_GUID
        ctx.log("  (no saved prior plan — restoring Balanced, the Windows default)")
    # Revert-failure hygiene (C6): only drop the snapshot after a VERIFIED
    # restore. Clearing unconditionally would destroy the true original on
    # a failed undo, and the next apply would snapshot the tweak's own
    # output as "prior state".
    rc = run_cmd(ctx, f"powercfg -setactive {target}")
    active = _active_scheme_guid()
    if rc != 0 or active is None or not _guids_equal(active, target):
        raise RuntimeError(
            f"Could not restore power plan (active={active}, wanted {target}, "
            f"exit {rc}) — snapshot kept so Undo can be retried. If this "
            "persists, switch plans manually in Settings > Power."
        )
    ctx.log(f"Restored power plan {target}.")
    clear_tweak_snapshot("ultimate_performance")


def _restart_explorer(ctx: TaskContext) -> bool:
    """Thin alias — the real implementation now lives in app.utils.restart_explorer
    so clean_tasks.py can reuse it too (see C3 fix in clean_tasks.py)."""
    return restart_explorer(ctx)


def apply_classic_context_menu(ctx: TaskContext):
    # MED-007: the old revert called reg_delete_key on the WHOLE
    # Software\Classes\CLSID\{86ca1aa0-...} key. That destroys every other
    # value and subkey under it, including a pre-existing InprocServer32 the
    # user (or another tool) had put there. Undo is now a VALUE-level restore,
    # and the key itself is only removed if apply is what created it.
    _regn_apply(ctx, "classic_context_menu",
                [("HKCU",
                  "Software\\Classes\\CLSID\\%s\\InprocServer32" % CONTEXT_MENU_CLSID,
                  "", "", "REG_SZ")],
                error="Could not write the classic-menu registry value.")
    # audit fix: restart_explorer's False return was ignored — the tweak
    # reported success while the taskbar might never have come back.
    if not _restart_explorer(ctx):
        raise RuntimeError(
            "Registry value set, but Explorer did not restart cleanly — "
            "your taskbar may be missing. Press Ctrl+Shift+Esc > File > Run "
            "new task > explorer.exe."
        )


def revert_classic_context_menu(ctx: TaskContext):
    # Restore just the value apply wrote, then drop the key ONLY if it is now
    # empty - i.e. only if this tweak is what created it. Never delete a key
    # that holds anything else.
    _regn_revert(ctx, "classic_context_menu", "Classic context menu")
    _prune_key_if_empty(ctx, "HKCU",
                        "Software\\Classes\\CLSID\\%s" % CONTEXT_MENU_CLSID)
    if not _restart_explorer(ctx):
        ctx.log("  ! Explorer did not restart cleanly — your taskbar may be missing. "
                "Press Ctrl+Shift+Esc > File > Run new task > explorer.exe.")


def _prune_key_if_empty(ctx: TaskContext, hive: str, path: str) -> bool:
    """Delete `path` only if it has no values and no subkeys left.

    Used after a value-level revert, where the tweak may have created the key
    in the first place. Refuses to touch anything non-empty, which is the whole
    point: the old reg_delete_key revert destroyed a pre-existing
    InprocServer32 and anything else living under the CLSID.
    """
    if not IS_WINDOWS:
        return False
    root = {"HKCU": winreg.HKEY_CURRENT_USER,
            "HKLM": winreg.HKEY_LOCAL_MACHINE}.get(hive)
    if root is None:
        return False
    try:
        with winreg.OpenKey(root, path, 0, winreg.KEY_READ) as key:
            try:
                winreg.QueryValueEx(key, "")           # any value left?
            except FileNotFoundError:
                pass
            else:
                ctx.log("  (left %s\\%s in place — it still holds data)"
                        % (hive, path))
                return False
            if winreg.QueryInfoKey(key)[0]:            # any subkey left?
                ctx.log("  (left %s\\%s in place — it still has subkeys)"
                        % (hive, path))
                return False
    except FileNotFoundError:
        return False                                # already gone
    except Exception as exc:
        ctx.log("  (could not inspect %s\\%s: %s)" % (hive, path, exc))
        return False
    try:
        with winreg.OpenKey(root, path, 0, winreg.KEY_WRITE | winreg.KEY_READ):
            winreg.DeleteKey(root, path)
        return True
    except Exception as exc:
        ctx.log("  (could not remove the now-empty %s\\%s: %s)" % (hive, path, exc))
        return False


_GAME_DVR_SPECS = [
    ("HKCU", "System\\GameConfigStore", "GameDVR_Enabled"),
    ("HKCU", "Software\\Microsoft\\Windows\\CurrentVersion\\GameDVR", "AppCaptureEnabled"),
    ("HKLM", "SOFTWARE\\Policies\\Microsoft\\Windows\\GameDVR", "AllowGameDVR"),
]


def apply_disable_game_dvr(ctx: TaskContext):
    # M3: snapshot priors — the old revert hardcoded 1/1 + delete.
    # F09: mixed HKCU/HKLM but admin_required=False — best-effort per value
    # (taskbar_cleanup pattern) so limited-mode still gets the HKCU half
    # instead of a guaranteed whole-task failure.
    from app.utils import reg_set_value as _set
    had_snapshot = bool(get_tweak_snapshot("disable_game_dvr"))
    _snap_reg_values(ctx, "disable_game_dvr", _GAME_DVR_SPECS)
    writes = [
        ("HKCU", "System\\GameConfigStore", "GameDVR_Enabled", 0),
        ("HKCU", "Software\\Microsoft\\Windows\\CurrentVersion\\GameDVR", "AppCaptureEnabled", 0),
        ("HKLM", "SOFTWARE\\Policies\\Microsoft\\Windows\\GameDVR", "AllowGameDVR", 0),
    ]
    ok, skipped = 0, []
    for hive, path, name, val in writes:
        if _set(ctx, hive, path, name, val):
            ok += 1
        else:
            skipped.append(f"{hive}\\{name}")
    if ok == 0:
        if not had_snapshot:
            clear_tweak_snapshot("disable_game_dvr")
        raise RuntimeError("Could not apply Game DVR settings (all writes blocked). Nothing was marked as applied.")
    if skipped:
        ctx.log(f"  (applied {ok} of {len(writes)}; skipped: {'; '.join(skipped)} — admin rights needed for HKLM)")


def revert_disable_game_dvr(ctx: TaskContext):
    snap = get_tweak_snapshot("disable_game_dvr")
    if snap and "specs" in snap:
        _restore_reg_values(ctx, "disable_game_dvr")
    else:
        reg_set_value_checked(ctx, "HKCU", "System\\GameConfigStore", "GameDVR_Enabled", 1)
        reg_set_value_checked(ctx, "HKCU", "Software\\Microsoft\\Windows\\CurrentVersion\\GameDVR", "AppCaptureEnabled", 1)
        reg_delete_value(ctx, "HKLM", "SOFTWARE\\Policies\\Microsoft\\Windows\\GameDVR", "AllowGameDVR")
        ctx.log("  (no snapshot found — restored to documented Windows defaults instead)")


_MOUSE_SPECS = [
    ("HKCU", "Control Panel\\Mouse", "MouseSpeed", "REG_SZ"),
    ("HKCU", "Control Panel\\Mouse", "MouseThreshold1", "REG_SZ"),
    ("HKCU", "Control Panel\\Mouse", "MouseThreshold2", "REG_SZ"),
]


def apply_disable_mouse_accel(ctx: TaskContext):
    # M3: snapshot priors — the old revert hardcoded 1/6/10.
    had_snapshot = bool(get_tweak_snapshot("mouse_accel"))
    _snap_reg_values(ctx, "mouse_accel", _MOUSE_SPECS)
    try:
        reg_set_value_checked(ctx, "HKCU", "Control Panel\\Mouse", "MouseSpeed", "0", value_type="REG_SZ")
        reg_set_value_checked(ctx, "HKCU", "Control Panel\\Mouse", "MouseThreshold1", "0", value_type="REG_SZ")
        reg_set_value_checked(ctx, "HKCU", "Control Panel\\Mouse", "MouseThreshold2", "0", value_type="REG_SZ")
    except Exception:
        if not had_snapshot:
            clear_tweak_snapshot("mouse_accel")
        raise


def revert_disable_mouse_accel(ctx: TaskContext):
    snap = get_tweak_snapshot("mouse_accel")
    if snap and "specs" in snap:
        _restore_reg_values(ctx, "mouse_accel", value_type="REG_SZ")
    else:
        reg_set_value_checked(ctx, "HKCU", "Control Panel\\Mouse", "MouseSpeed", "1", value_type="REG_SZ")
        reg_set_value_checked(ctx, "HKCU", "Control Panel\\Mouse", "MouseThreshold1", "6", value_type="REG_SZ")
        reg_set_value_checked(ctx, "HKCU", "Control Panel\\Mouse", "MouseThreshold2", "10", value_type="REG_SZ")
        ctx.log("  (no snapshot found — restored to documented Windows defaults instead)")


def apply_visual_effects_perf(ctx: TaskContext):
    # M1 audit fix (double-owned MenuShowDelay): the menu_delay tweak also
    # writes HKCU\...\Desktop\MenuShowDelay (snapshot + 100ms). This apply
    # used to overwrite it with 0 and its revert hardcode 400 back —
    # reverting one tweak clobbered the other's bookkeeping. Snapshot the
    # prior value under OUR task id before writing (same helpers menu_delay
    # uses); the revert restores exactly what this apply overwrote.
    # Residual order-dependence (documented, not fixable without merging
    # the tweaks): each undo restores what its own apply overwrote, so if
    # both tweaks are applied, undoing them in apply order lands on the
    # true original value — undoing in reverse order lands on the other
    # tweak's output.
    had_snapshot = bool(get_tweak_snapshot("visual_effects"))
    _snap_reg_values(ctx, "visual_effects",
                     [("HKCU", "Control Panel\\Desktop", "MenuShowDelay")])
    try:
        reg_set_value_checked(ctx, "HKCU", "Software\\Microsoft\\Windows\\CurrentVersion\\Themes\\Personalize",
                      "EnableTransparency", 0)
        reg_set_value_checked(ctx, "HKCU", "Control Panel\\Desktop\\WindowMetrics", "MinAnimate", "0", value_type="REG_SZ")
        reg_set_value_checked(ctx, "HKCU", "Software\\Microsoft\\Windows\\CurrentVersion\\Explorer\\Advanced",
                      "TaskbarAnimations", 0)
        reg_set_value_checked(ctx, "HKCU", "Control Panel\\Desktop", "MenuShowDelay", "0", value_type="REG_SZ")
    except Exception:
        if not had_snapshot:
            clear_tweak_snapshot("visual_effects")
        raise


def revert_visual_effects_perf(ctx: TaskContext):
    reg_set_value_checked(ctx, "HKCU", "Software\\Microsoft\\Windows\\CurrentVersion\\Themes\\Personalize",
                  "EnableTransparency", 1)
    reg_set_value_checked(ctx, "HKCU", "Control Panel\\Desktop\\WindowMetrics", "MinAnimate", "1", value_type="REG_SZ")
    reg_set_value_checked(ctx, "HKCU", "Software\\Microsoft\\Windows\\CurrentVersion\\Explorer\\Advanced",
                  "TaskbarAnimations", 1)
    # M1 audit fix: restore the snapshotted prior MenuShowDelay (an absent
    # prior value is deleted back to absence) instead of hardcoding 400 —
    # the hardcode clobbered the menu_delay tweak's value and any custom
    # delay the user had set.
    if get_tweak_snapshot("visual_effects").get("specs"):
        _restore_reg_values(ctx, "visual_effects", value_type="REG_SZ")
    else:
        # No snapshot (tweak applied by an older app version, or config
        # cleared): Windows' own default is an ABSENT value — remove ours.
        ctx.log("  (no MenuShowDelay snapshot on file — removing the value; Windows then uses its default)")
        reg_delete_value(ctx, "HKCU", "Control Panel\\Desktop", "MenuShowDelay")


_NET_THROTTLE_SPECS = [
    ("HKLM", "SOFTWARE\\Microsoft\\Windows NT\\CurrentVersion\\Multimedia\\SystemProfile",
     "NetworkThrottlingIndex"),
    ("HKLM", "SOFTWARE\\Microsoft\\Windows NT\\CurrentVersion\\Multimedia\\SystemProfile",
     "SystemResponsiveness"),
]


def apply_network_throttling(ctx: TaskContext):
    # M3: snapshot priors — the old revert hardcoded 0xA/20.
    had_snapshot = bool(get_tweak_snapshot("network_throttling"))
    _snap_reg_values(ctx, "network_throttling", _NET_THROTTLE_SPECS)
    try:
        reg_set_value_checked(ctx, "HKLM",
                      "SOFTWARE\\Microsoft\\Windows NT\\CurrentVersion\\Multimedia\\SystemProfile",
                      "NetworkThrottlingIndex", 0xFFFFFFFF)
        # SystemResponsiveness: 10 = reserve 10% CPU for background (values <10 treated as 20%)
        reg_set_value_checked(ctx, "HKLM",
                      "SOFTWARE\\Microsoft\\Windows NT\\CurrentVersion\\Multimedia\\SystemProfile",
                      "SystemResponsiveness", 10)
    except Exception:
        if not had_snapshot:
            clear_tweak_snapshot("network_throttling")
        raise


def revert_network_throttling(ctx: TaskContext):
    snap = get_tweak_snapshot("network_throttling")
    if snap and "specs" in snap:
        _restore_reg_values(ctx, "network_throttling")
    else:
        reg_set_value_checked(ctx, "HKLM",
                      "SOFTWARE\\Microsoft\\Windows NT\\CurrentVersion\\Multimedia\\SystemProfile",
                      "NetworkThrottlingIndex", 0xA)
        reg_set_value_checked(ctx, "HKLM",
                      "SOFTWARE\\Microsoft\\Windows NT\\CurrentVersion\\Multimedia\\SystemProfile",
                      "SystemResponsiveness", 20)
        ctx.log("  (no snapshot found — restored to documented Windows defaults instead)")


_GAMES_PRIORITY_SPECS = [
    ("HKLM", "SOFTWARE\\Microsoft\\Windows NT\\CurrentVersion\\Multimedia\\SystemProfile\\Tasks\\Games",
     "Scheduling Category", "REG_SZ"),
    ("HKLM", "SOFTWARE\\Microsoft\\Windows NT\\CurrentVersion\\Multimedia\\SystemProfile\\Tasks\\Games",
     "GPU Priority"),
    ("HKLM", "SOFTWARE\\Microsoft\\Windows NT\\CurrentVersion\\Multimedia\\SystemProfile\\Tasks\\Games",
     "Priority"),
    ("HKLM", "SOFTWARE\\Microsoft\\Windows NT\\CurrentVersion\\Multimedia\\SystemProfile\\Tasks\\Games",
     "SFIO Priority"),
]


def apply_games_priority(ctx: TaskContext):
    """Boost CPU/GPU/IO priority for games via Multimedia System Profile."""
    # M3: snapshot priors — the old revert hardcoded Medium/2/2/2.
    base = "SOFTWARE\\Microsoft\\Windows NT\\CurrentVersion\\Multimedia\\SystemProfile\\Tasks\\Games"
    had_snapshot = bool(get_tweak_snapshot("games_priority"))
    _snap_reg_values(ctx, "games_priority", _GAMES_PRIORITY_SPECS)
    try:
        reg_set_value_checked(ctx, "HKLM", base, "Scheduling Category", "High", value_type="REG_SZ")
        reg_set_value_checked(ctx, "HKLM", base, "GPU Priority", 8)
        reg_set_value_checked(ctx, "HKLM", base, "Priority", 6)
        reg_set_value_checked(ctx, "HKLM", base, "SFIO Priority", 8)
    except Exception:
        if not had_snapshot:
            clear_tweak_snapshot("games_priority")
        raise


def revert_games_priority(ctx: TaskContext):
    base = "SOFTWARE\\Microsoft\\Windows NT\\CurrentVersion\\Multimedia\\SystemProfile\\Tasks\\Games"
    snap = get_tweak_snapshot("games_priority")
    if snap and "specs" in snap:
        _restore_reg_values(ctx, "games_priority")
    else:
        reg_set_value_checked(ctx, "HKLM", base, "Scheduling Category", "Medium", value_type="REG_SZ")
        reg_set_value_checked(ctx, "HKLM", base, "GPU Priority", 2)
        reg_set_value_checked(ctx, "HKLM", base, "Priority", 2)
        reg_set_value_checked(ctx, "HKLM", base, "SFIO Priority", 2)
        ctx.log("  (no snapshot found — restored to documented Windows defaults instead)")


def _open_interfaces_key():
    # Use 64-bit view to match reg_set_value's WOW64 writes
    access = winreg.KEY_READ
    try:
        access |= winreg.KEY_WOW64_64KEY  # type: ignore[attr-defined]
    except AttributeError:
        pass
    return winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, "SYSTEM\\CurrentControlSet\\Services\\Tcpip\\Parameters\\Interfaces", 0, access)


def _network_interface_subs() -> list:
    """Every subkey of the Tcpip Interfaces key, i.e. every network adapter.

    Shared by apply and revert so the two always agree on WHICH adapters they
    are talking about. Returns names, not full paths.
    """
    key = _open_interfaces_key()
    try:
        names, i = [], 0
        while True:
            try:
                names.append(winreg.EnumKey(key, i))
            except OSError:
                break
            i += 1
        return names
    finally:
        winreg.CloseKey(key)


def apply_disable_nagle(ctx: TaskContext):
    base = "SYSTEM\\CurrentControlSet\\Services\\Tcpip\\Parameters\\Interfaces"
    if not IS_WINDOWS:
        return
    try:
        subs = _network_interface_subs()
    except FileNotFoundError:
        # B5 audit fix: this used to log and fall off the end of the
        # function — the runner counted it applied although nothing was
        # written. A machine with no Interfaces key has nothing to change.
        raise TaskSkipped("No network interfaces found — nothing to tweak.")
    if not subs:
        raise TaskSkipped("No network interfaces found — nothing to tweak.")
    # MED-007: this is the worst case in the finding. It writes TcpAckFrequency
    # and TCPNoDelay on EVERY adapter, and the old revert blind-deleted both
    # from EVERY adapter — so a user who had deliberately set TcpAckFrequency=2
    # (the Windows delayed-ACK default) on their VPN interface lost that
    # setting, and one who had set TCPNoDelay=0 to re-enable Nagle for a
    # specific link lost that too. Snapshot each (adapter, value) pair.
    writes = []
    for sub in subs:
        for name in ("TcpAckFrequency", "TCPNoDelay"):
            writes.append(("HKLM", "%s\\%s" % (base, sub), name, 1))
    _regn_apply(ctx, "disable_nagle", writes)


def revert_disable_nagle(ctx: TaskContext):
    if not IS_WINDOWS:
        return
    # MED-007: was a blind reg_delete_value of both values on every adapter.
    # Now restores each value to what that adapter actually had, and leaves
    # adapters that appeared since apply completely alone (they were never
    # in the snapshot, so they are not in the spec list).
    _regn_revert(ctx, "disable_nagle", "Nagle optimisation")


_HAGS_SPECS = [
    ("HKLM", "SYSTEM\\CurrentControlSet\\Control\\GraphicsDrivers", "HwSchMode"),
]


def apply_hags(ctx: TaskContext):
    # M3: snapshot priors — the old revert hardcoded 1.
    had_snapshot = bool(get_tweak_snapshot("hags"))
    _snap_reg_values(ctx, "hags", _HAGS_SPECS)
    try:
        reg_set_value_checked(ctx, "HKLM", "SYSTEM\\CurrentControlSet\\Control\\GraphicsDrivers", "HwSchMode", 2)
    except Exception:
        if not had_snapshot:
            clear_tweak_snapshot("hags")
        raise
    ctx.log("Reboot required for graphics scheduling to take effect.")


def revert_hags(ctx: TaskContext):
    snap = get_tweak_snapshot("hags")
    if snap and "specs" in snap:
        _restore_reg_values(ctx, "hags")
    else:
        reg_set_value_checked(ctx, "HKLM", "SYSTEM\\CurrentControlSet\\Control\\GraphicsDrivers", "HwSchMode", 1)
        ctx.log("  (no snapshot found — restored to documented Windows default instead)")
    ctx.log("Reboot required for graphics scheduling to take effect.")


_GAME_MODE_SPECS = [
    ("HKCU", "Software\\Microsoft\\GameBar", "AutoGameModeEnabled"),
    ("HKCU", "Software\\Microsoft\\GameBar", "AllowAutoGameMode"),
]


def apply_game_mode(ctx: TaskContext):
    # M3: snapshot priors — the old revert hardcoded 0/0.
    had_snapshot = bool(get_tweak_snapshot("game_mode"))
    _snap_reg_values(ctx, "game_mode", _GAME_MODE_SPECS)
    try:
        reg_set_value_checked(ctx, "HKCU", "Software\\Microsoft\\GameBar", "AutoGameModeEnabled", 1)
        reg_set_value_checked(ctx, "HKCU", "Software\\Microsoft\\GameBar", "AllowAutoGameMode", 1)
    except Exception:
        if not had_snapshot:
            clear_tweak_snapshot("game_mode")
        raise


def revert_game_mode(ctx: TaskContext):
    snap = get_tweak_snapshot("game_mode")
    if snap and "specs" in snap:
        _restore_reg_values(ctx, "game_mode")
    else:
        reg_set_value_checked(ctx, "HKCU", "Software\\Microsoft\\GameBar", "AutoGameModeEnabled", 0)
        reg_set_value_checked(ctx, "HKCU", "Software\\Microsoft\\GameBar", "AllowAutoGameMode", 0)
        ctx.log("  (no snapshot found — restored to documented Windows defaults instead)")


_FSE_SPECS = [
    ("HKCU", "System\\GameConfigStore", "GameDVR_FSEBehaviorMode"),
    ("HKCU", "System\\GameConfigStore", "GameDVR_DXGIHonorFSEWindowsCompatible"),
    ("HKCU", "System\\GameConfigStore", "GameDVR_HonorUserFSEBehaviorMode"),
]


def apply_windowed_optimize(ctx: TaskContext):
    # Optimizations for windowed games — supported Windows 11 setting
    # M3: snapshot priors — the old revert hardcoded 0 + deletes.
    had_snapshot = bool(get_tweak_snapshot("windowed_optimize"))
    _snap_reg_values(ctx, "windowed_optimize", _FSE_SPECS)
    try:
        reg_set_value_checked(ctx, "HKCU", "System\\GameConfigStore", "GameDVR_FSEBehaviorMode", 2)
        reg_set_value_checked(ctx, "HKCU", "System\\GameConfigStore", "GameDVR_DXGIHonorFSEWindowsCompatible", 1)
        reg_set_value_checked(ctx, "HKCU", "System\\GameConfigStore", "GameDVR_HonorUserFSEBehaviorMode", 1)
    except Exception:
        if not had_snapshot:
            clear_tweak_snapshot("windowed_optimize")
        raise


def revert_windowed_optimize(ctx: TaskContext):
    snap = get_tweak_snapshot("windowed_optimize")
    if snap and "specs" in snap:
        _restore_reg_values(ctx, "windowed_optimize")
    else:
        reg_set_value_checked(ctx, "HKCU", "System\\GameConfigStore", "GameDVR_FSEBehaviorMode", 0)
        reg_delete_value(ctx, "HKCU", "System\\GameConfigStore", "GameDVR_DXGIHonorFSEWindowsCompatible")
        reg_delete_value(ctx, "HKCU", "System\\GameConfigStore", "GameDVR_HonorUserFSEBehaviorMode")
        ctx.log("  (no snapshot found — restored to documented Windows defaults instead)")


_HIBERBOOT_SPECS = [
    ("HKLM", "SYSTEM\\CurrentControlSet\\Control\\Session Manager\\Power", "HiberbootEnabled"),
]


def apply_fast_startup_fix(ctx: TaskContext):
    # Correct: disable Fast Startup via HiberbootEnabled, not hibernate off
    # M3: snapshot priors — the old revert hardcoded 1.
    had_snapshot = bool(get_tweak_snapshot("disable_fast_startup"))
    _snap_reg_values(ctx, "disable_fast_startup", _HIBERBOOT_SPECS)
    try:
        reg_set_value_checked(ctx, "HKLM", "SYSTEM\\CurrentControlSet\\Control\\Session Manager\\Power", "HiberbootEnabled", 0)
    except Exception:
        if not had_snapshot:
            clear_tweak_snapshot("disable_fast_startup")
        raise


def revert_fast_startup_fix(ctx: TaskContext):
    snap = get_tweak_snapshot("disable_fast_startup")
    if snap and "specs" in snap:
        _restore_reg_values(ctx, "disable_fast_startup")
    else:
        reg_set_value_checked(ctx, "HKLM", "SYSTEM\\CurrentControlSet\\Control\\Session Manager\\Power", "HiberbootEnabled", 1)
        ctx.log("  (no snapshot found — restored to documented Windows default instead)")


def apply_limit_telemetry(ctx: TaskContext):
    ctx.set_status("Telemetry limit...")
    # MED-007: Undo used to blind-delete this value, destroying a
    # pre-existing different setting. The real prior state is
    # snapshotted now, so Undo restores what was actually there,
    # "absent" included.
    _regn_apply(ctx, 'limit_telemetry', [("HKLM", "SOFTWARE\\Policies\\Microsoft\\Windows\\DataCollection", "AllowTelemetry", 1)])
    ctx.log("Telemetry limit applied.")



def revert_limit_telemetry(ctx: TaskContext):
    # MED-007: was a blind `reg_delete_value(...)`, which destroyed
    # a pre-existing different value. Now restores the snapshot.
    _regn_revert(ctx, 'limit_telemetry', "Telemetry limit")



_PRIORITY_SEP_SPECS = [
    ("HKLM", "SYSTEM\\CurrentControlSet\\Control\\PriorityControl", "Win32PrioritySeparation"),
]


def apply_priority_separation(ctx: TaskContext):
    # Foreground app gets more CPU — 0x26 = gaming bias
    # M3: snapshot priors — the old revert hardcoded 2.
    had_snapshot = bool(get_tweak_snapshot("priority_separation"))
    _snap_reg_values(ctx, "priority_separation", _PRIORITY_SEP_SPECS)
    try:
        reg_set_value_checked(ctx, "HKLM", "SYSTEM\\CurrentControlSet\\Control\\PriorityControl", "Win32PrioritySeparation", 38)
    except Exception:
        if not had_snapshot:
            clear_tweak_snapshot("priority_separation")
        raise


def revert_priority_separation(ctx: TaskContext):
    snap = get_tweak_snapshot("priority_separation")
    if snap and "specs" in snap:
        _restore_reg_values(ctx, "priority_separation")
    else:
        reg_set_value_checked(ctx, "HKLM", "SYSTEM\\CurrentControlSet\\Control\\PriorityControl", "Win32PrioritySeparation", 2)
        ctx.log("  (no snapshot found — restored to documented Windows default instead)")


_POWER_THROTTLE_SPECS = [
    ("HKLM", "SYSTEM\\CurrentControlSet\\Control\\Power\\PowerThrottling", "PowerThrottlingOff"),
]


def apply_power_throttling_off(ctx: TaskContext):
    """Disable CPU power throttling for consistent performance."""
    # M3: snapshot priors — the old revert deleted unconditionally.
    had_snapshot = bool(get_tweak_snapshot("power_throttling_off"))
    _snap_reg_values(ctx, "power_throttling_off", _POWER_THROTTLE_SPECS)
    try:
        reg_set_value_checked(ctx, "HKLM", "SYSTEM\\CurrentControlSet\\Control\\Power\\PowerThrottling", "PowerThrottlingOff", 1)
    except Exception:
        if not had_snapshot:
            clear_tweak_snapshot("power_throttling_off")
        raise


def revert_power_throttling_off(ctx: TaskContext):
    snap = get_tweak_snapshot("power_throttling_off")
    if snap and "specs" in snap:
        _restore_reg_values(ctx, "power_throttling_off")
    else:
        reg_delete_value(ctx, "HKLM", "SYSTEM\\CurrentControlSet\\Control\\Power\\PowerThrottling", "PowerThrottlingOff")
        ctx.log("  (no snapshot found — removed the value; Windows then uses its default)")


def apply_startup_delay(ctx: TaskContext):
    ctx.set_status("Startup delay...")
    # MED-007: Undo used to blind-delete this value, destroying a
    # pre-existing different setting. The real prior state is
    # snapshotted now, so Undo restores what was actually there,
    # "absent" included.
    _regn_apply(ctx, 'startup_delay', [("HKCU", "Software\\Microsoft\\Windows\\CurrentVersion\\Explorer\\Serialize", "StartupDelayInMSec", 0)])
    ctx.log("Startup delay applied.")



def revert_startup_delay(ctx: TaskContext):
    # MED-007: was a blind `reg_delete_value(...)`, which destroyed
    # a pre-existing different value. Now restores the snapshot.
    _regn_revert(ctx, 'startup_delay', "Startup delay")



#: USB selective suspend: subgroup/setting GUID pair, plus the value this app
#: writes (0 = disabled) and the Windows default (1 = enabled) used only as a
#: last-resort fallback when no snapshot exists.
_USB_SUSPEND_SUBGROUP = "2a737441-1930-4402-8d77-b2bbe5a308a3"
_USB_SUSPEND_SETTING = "48e6b7a6-50f5-4782-a5d4-53bb8fcc84df"
_USB_SUSPEND_PAIRS = [(_USB_SUSPEND_SUBGROUP, _USB_SUSPEND_SETTING)]
_USB_SUSPEND_FALLBACK = {
    "%s:%s" % (_USB_SUSPEND_SUBGROUP, _USB_SUSPEND_SETTING): 1,
}


def apply_usb_suspend(ctx: TaskContext):
    """Disable USB selective suspend (fixes audio dropouts on USB mics/headsets).

    H4 fix: 'powercfg /change usb-selective-suspend-setting' is NOT a valid
    /change alias (verified: 'Invalid Parameters'). Only the GUID form works,
    and it must be applied to both AC and DC values.

    LTSC/user-reported fix: on some PCs (Win10 IoT LTSC, VMs, stripped
    power plans) this subgroup/setting does not exist — powercfg prints
    'The power scheme, subgroup or setting specified does not exist' and
    exits non-zero. The old code ignored the return code, so the log showed
    the error while the tweak still counted as success. Now: both sides
    failing honestly SKIPS (nothing to change on this PC); one side failing
    logs a warning but still applies the side that worked.
    """
    # MED-007: the old revert hardcoded index 1, which happens to be the
    # Windows default - but "the default" is not "what this user had". A
    # machine that had already disabled selective suspend (or set some other
    # index) had that value silently overwritten by Undo. Snapshot the real
    # AC and DC indexes first, like every other powercfg tweak here.
    _snapshot_powercfg_pairs(ctx, "usb_suspend", _USB_SUSPEND_PAIRS)
    rc_ac = run_cmd(ctx, f"powercfg /setacvalueindex scheme_current {_USB_SUSPEND_SUBGROUP} {_USB_SUSPEND_SETTING} 0")
    rc_dc = run_cmd(ctx, f"powercfg /setdcvalueindex scheme_current {_USB_SUSPEND_SUBGROUP} {_USB_SUSPEND_SETTING} 0")
    if rc_ac != 0 and rc_dc != 0:
        # Nothing was written, so there is nothing to undo - drop the snapshot
        # rather than leave an Undo record for a change that did not happen.
        if not get_tweak_snapshot("usb_suspend"):
            clear_tweak_snapshot("usb_suspend")
        raise TaskSkipped(
            "USB selective suspend setting not present in this power plan — "
            "skipping (nothing to change on this PC)."
        )
    if rc_ac != 0 or rc_dc != 0:
        ctx.log("  (one power side accepted the change, the other is not present — applied what exists)")
    run_cmd(ctx, "powercfg /setactive scheme_current")


def revert_usb_suspend(ctx: TaskContext):
    # MED-007: was a hardcoded `... 1` on both sides. Now restores the
    # snapshotted AC and DC indexes, falling back to the documented default
    # only when no snapshot exists (and saying so).
    _restore_powercfg_pairs(ctx, "usb_suspend", _USB_SUSPEND_PAIRS,
                            _USB_SUSPEND_FALLBACK)
    run_cmd(ctx, "powercfg /setactive scheme_current")


def apply_disk_timeout(ctx: TaskContext):
    # NOTE: powercfg /change aliases have no per-machine snapshot (the
    # underlying GUID pair differs by scheme); apply/reset use documented
    # Windows defaults (0 = never spin down on AC/DC; revert = 20 min).
    # Return codes are honored so LTSC/stripped schemes fail honestly.
    rc_ac = run_cmd(ctx, "powercfg /change disk-timeout-ac 0")
    rc_dc = run_cmd(ctx, "powercfg /change disk-timeout-dc 0")
    if rc_ac != 0 and rc_dc != 0:
        raise TaskSkipped(
            "Disk-timeout setting not present in this power plan — "
            "skipping (nothing to change on this PC)."
        )
    if rc_ac != 0 or rc_dc != 0:
        ctx.log("  (one power side accepted the change, the other is not present — applied what exists)")


def revert_disk_timeout(ctx: TaskContext):
    # Restores the documented Windows default (20 min), not necessarily the
    # exact prior value — see apply_disk_timeout note.
    rc_ac = run_cmd(ctx, "powercfg /change disk-timeout-ac 20")
    rc_dc = run_cmd(ctx, "powercfg /change disk-timeout-dc 20")
    if rc_ac != 0 and rc_dc != 0:
        raise RuntimeError("Could not restore disk timeout (powercfg rejected both sides).")


_KEYBOARD_SPECS = [
    ("HKCU", "Control Panel\\Keyboard", "KeyboardDelay"),
    ("HKCU", "Control Panel\\Keyboard", "KeyboardSpeed"),
]


def apply_keyboard_tuning(ctx: TaskContext):
    # Snapshot first: the old revert wrote Speed=31 (identical to apply),
    # so a non-default prior Speed was never restored (M4).
    had_snapshot = bool(get_tweak_snapshot("keyboard_tuning"))
    _snap_reg_values(ctx, "keyboard_tuning", _KEYBOARD_SPECS)
    try:
        reg_set_value_checked(ctx, "HKCU", "Control Panel\\Keyboard", "KeyboardDelay", "0", value_type="REG_SZ")
        reg_set_value_checked(ctx, "HKCU", "Control Panel\\Keyboard", "KeyboardSpeed", "31", value_type="REG_SZ")
    except Exception:
        if not had_snapshot:
            clear_tweak_snapshot("keyboard_tuning")
        raise


def revert_keyboard_tuning(ctx: TaskContext):
    # Exact prior values when snapshotted; documented Windows defaults
    # (Delay 1 = 250ms, Speed 31) only when applied by an older version
    # without a snapshot.
    snap = get_tweak_snapshot("keyboard_tuning")
    if snap and "specs" in snap:
        _restore_reg_values(ctx, "keyboard_tuning", value_type="REG_SZ")
    else:
        reg_set_value_checked(ctx, "HKCU", "Control Panel\\Keyboard", "KeyboardDelay", "1", value_type="REG_SZ")
        reg_set_value_checked(ctx, "HKCU", "Control Panel\\Keyboard", "KeyboardSpeed", "31", value_type="REG_SZ")
        ctx.log("  (no snapshot found — restored to documented Windows defaults instead)")


def apply_ssd_trim(ctx: TaskContext):
    """Enable TRIM/DisableDeleteNotify for SSD."""
    # B5 audit fix: these skip paths used to log + `return` — the runner
    # counted them as applied and the GUI badged '✓ Active' although
    # nothing changed. TaskSkipped = completed-with-skip (no badge).
    if not _has_ssd():
        raise TaskSkipped("No SSD detected — skipping TRIM tweak (only applies to SSDs).")
    rc = run_cmd(ctx, "fsutil behavior set disabledeletenotify 0")
    if rc != 0:
        raise RuntimeError(f"Could not enable TRIM (fsutil exited {rc}).")


def revert_ssd_trim(ctx: TaskContext):
    # M5: restore regardless of current _has_ssd() (cached, and the disk
    # may have changed since apply) — `disabledeletenotify 0` (TRIM
    # enabled) is the safe Windows default on any disk. Revert restores
    # the default (TRIM enabled = 0), never disables TRIM (1).
    # Disabling TRIM harms SSD performance/lifespan.
    rc = run_cmd(ctx, "fsutil behavior set disabledeletenotify 0")
    if rc != 0:
        raise RuntimeError(f"Could not restore TRIM default (fsutil exited {rc}).")


def apply_ssd_superfetch(ctx: TaskContext):
    """Disable SysMain (Superfetch) — unnecessary on SSD/NVMe."""
    if not _has_ssd():
        raise TaskSkipped("No SSD detected — skipping SysMain tweak (only applies to SSDs).")
    # M5: snapshot the real prior start type (auto/demand/disabled/...) so
    # revert restores it instead of assuming `auto`.
    had_snapshot = bool(get_tweak_snapshot("ssd_superfetch"))
    prior = sc_query_start_type(ctx, "SysMain")
    if prior is not None:
        save_tweak_snapshot("ssd_superfetch", {"start_type": prior})
    rc = run_cmd(ctx, "sc config SysMain start= disabled")
    if rc != 0:
        if not had_snapshot:
            clear_tweak_snapshot("ssd_superfetch")
        raise RuntimeError(f"Could not disable SysMain (sc exited {rc}).")
    run_cmd(ctx, "net stop SysMain")


def revert_ssd_superfetch(ctx: TaskContext):
    # M5: restore regardless of current _has_ssd() — the disk may have
    # changed since apply, and the service exists on HDD machines too.
    snap = get_tweak_snapshot("ssd_superfetch")
    target = snap.get("start_type") if snap else None
    if not target:
        target = "auto"
        ctx.log("  (no saved prior SysMain start type — restoring `auto`, the Windows default)")
    elif not _valid_snapshot_start_type(target):
        # F-1: the snapshot value flows into a shell command string — only
        # a start-type keyword this app could have snapshotted may run.
        raise _snapshot_validation_error("ssd_superfetch", "start_type", target)
    rc = run_cmd(ctx, f"sc config SysMain start= {target}")
    if rc != 0:
        raise RuntimeError(f"Could not restore SysMain start type (sc exited {rc}) — snapshot kept.")
    run_cmd(ctx, "net start SysMain")
    clear_tweak_snapshot("ssd_superfetch")


def apply_ssd_last_access(ctx: TaskContext):
    """Disable last access timestamp updates (NtfsDisableLastAccessUpdate)."""
    if not _has_ssd():
        raise TaskSkipped("No SSD detected — skipping last access tweak (only applies to SSDs).")
    reg_set_value_checked(ctx, "HKLM", "SYSTEM\\CurrentControlSet\\Control\\FileSystem",
                  "NtfsDisableLastAccessUpdate", 1)


def revert_ssd_last_access(ctx: TaskContext):
    # P7-01: no _has_ssd() gate — same contract as revert_ssd_prefetch /
    # revert_ssd_superfetch / revert_ssd_trim: restore regardless of the
    # current disk (the probe is cached, and the disk may have changed
    # since apply). 0 = last-access updates ON — the safe Windows default.
    reg_set_value_checked(ctx, "HKLM", "SYSTEM\\CurrentControlSet\\Control\\FileSystem",
                  "NtfsDisableLastAccessUpdate", 0)


_SSD_PREFETCH_PATH = "SYSTEM\\CurrentControlSet\\Control\\Session Manager\\Memory Management\\PrefetchParameters"
_SSD_PREFETCH_SPECS = [
    ("HKLM", _SSD_PREFETCH_PATH, "EnablePrefetcher"),
    ("HKLM", _SSD_PREFETCH_PATH, "EnableSuperfetch"),
]


def apply_ssd_prefetch(ctx: TaskContext):
    """Disable Prefetcher and Superfetch for SSD."""
    if not _has_ssd():
        raise TaskSkipped("No SSD detected — skipping prefetch tweak (only applies to SSDs).")
    # M5: snapshot exact priors — the old revert hardcoded 3/3, losing
    # custom values like 1/2.
    had_snapshot = bool(get_tweak_snapshot("ssd_prefetch"))
    _snap_reg_values(ctx, "ssd_prefetch", _SSD_PREFETCH_SPECS)
    try:
        reg_set_value_checked(ctx, "HKLM", _SSD_PREFETCH_PATH,
                      "EnablePrefetcher", 0)
        reg_set_value_checked(ctx, "HKLM", _SSD_PREFETCH_PATH,
                      "EnableSuperfetch", 0)
    except Exception:
        if not had_snapshot:
            clear_tweak_snapshot("ssd_prefetch")
        raise


def revert_ssd_prefetch(ctx: TaskContext):
    # Exact priors when snapshotted (e.g. by this version); documented
    # Windows default 3/3 only for pre-snapshot applies. No _has_ssd gate:
    # restore must work even if the disk changed since apply.
    snap = get_tweak_snapshot("ssd_prefetch")
    if snap and "specs" in snap:
        _restore_reg_values(ctx, "ssd_prefetch")
    else:
        reg_set_value_checked(ctx, "HKLM", _SSD_PREFETCH_PATH,
                      "EnablePrefetcher", 3)
        reg_set_value_checked(ctx, "HKLM", _SSD_PREFETCH_PATH,
                      "EnableSuperfetch", 3)
        ctx.log("  (no snapshot found — restored to documented Windows defaults instead)")


# Disables activity history #
_ACTIVITY_HISTORY_SPECS = [
    ("HKLM", "SOFTWARE\\Policies\\Microsoft\\Windows\\System", "EnableActivityFeed"),
    ("HKLM", "SOFTWARE\\Policies\\Microsoft\\Windows\\System", "PublishUserActivities"),
    ("HKLM", "SOFTWARE\\Policies\\Microsoft\\Windows\\System", "UploadUserActivities"),
]

def apply_activity_history_disable(ctx: TaskContext):
    ctx.log("[Tweak] Activity History - Disable")
    # CT-007 audit fix (double-owned EnableActivityFeed): privacy_baseline
    # also writes EnableActivityFeed under this exact HKLM path, via proper
    # snapshot/restore (_snap_reg_values/_restore_reg_values). This task
    # used to unconditionally SET 0 on apply and DELETE on revert with no
    # snapshot at all — undoing whichever of the two ran second could
    # silently flip or delete a value the other tweak still considers
    # "applied" (and the blind delete also lost any real prior GPO value on
    # PublishUserActivities/UploadUserActivities in isolation). Snapshot the
    # same way privacy_baseline and the MenuShowDelay fix above do. Same
    # documented residual order-dependence as that fix: each undo restores
    # what was there immediately before its OWN apply, not necessarily the
    # machine's original state, if the two tweaks are applied in
    # overlapping order — not fixable without merging the tweaks.
    had_snapshot = bool(get_tweak_snapshot("activity_history_disable"))
    _snap_reg_values(ctx, "activity_history_disable", _ACTIVITY_HISTORY_SPECS)
    try:
        for _, base, name in _ACTIVITY_HISTORY_SPECS:
            reg_set_value_checked(ctx, "HKLM", base, name, 0)
            ctx.log(f"  Set {base}\\{name}=0")
    except Exception:
        if not had_snapshot:
            clear_tweak_snapshot("activity_history_disable")
        raise
    ctx.log("Activity History disabled.")

def revert_activity_history_disable(ctx: TaskContext):
    _restore_reg_values(ctx, "activity_history_disable")
    ctx.log("Activity History reverted.")

# Disables consumer features #
def apply_consumer_features_disable(ctx: TaskContext):
    ctx.set_status("ConsumerFeatures...")
    # MED-007: Undo used to blind-delete this value, destroying a
    # pre-existing different setting. The real prior state is
    # snapshotted now, so Undo restores what was actually there,
    # "absent" included.
    _regn_apply(ctx, 'consumer_features', [("HKLM", "SOFTWARE\\Policies\\Microsoft\\Windows\\CloudContent", "DisableWindowsConsumerFeatures", 1)])
    ctx.log("ConsumerFeatures applied.")


def revert_consumer_features_disable(ctx: TaskContext):
    # MED-007: was a blind `reg_delete_value(...)`, which destroyed
    # a pre-existing different value. Now restores the snapshot.
    _regn_revert(ctx, 'consumer_features', "ConsumerFeatures")


# Disables delivery optimization #
def apply_delivery_optimization_disable(ctx: TaskContext):
    ctx.set_status("Delivery Optimization...")
    # MED-007: Undo used to blind-delete this value, destroying a
    # pre-existing different setting. The real prior state is
    # snapshotted now, so Undo restores what was actually there,
    # "absent" included.
    _regn_apply(ctx, 'tweak_delivery_optimization', [("HKLM", "SOFTWARE\\Policies\\Microsoft\\Windows\\DeliveryOptimization", "DODownloadMode", 0)])
    ctx.log("Delivery Optimization applied.")


def revert_delivery_optimization_disable(ctx: TaskContext):
    # MED-007: was a blind `reg_delete_value(...)`, which destroyed
    # a pre-existing different value. Now restores the snapshot.
    _regn_revert(ctx, 'tweak_delivery_optimization', "Delivery Optimization")


# Disables explorer auto discovery #
def apply_explorer_auto_discovery_disable(ctx: TaskContext):
    ctx.log("[Tweak] File Explorer Automatic Folder Discovery - Disable")
    for sub in (r"Software\Classes\Local Settings\Software\Microsoft\Windows\Shell\Bags",
                r"Software\Classes\Local Settings\Software\Microsoft\Windows\Shell\BagMRU"):
        reg_delete_key(ctx, "HKCU", sub)
        ctx.log(f"  Removed HKCU\\{sub}")
    all_folders = r"Software\Classes\Local Settings\Software\Microsoft\Windows\Shell\Bags\AllFolders\Shell"
    reg_set_value_checked(ctx, "HKCU", all_folders, "FolderType", "NotSpecified", value_type="REG_SZ")
    ctx.log("  Set FolderType=NotSpecified.")
    ctx.log("Please sign out/in or restart to apply.")

def revert_explorer_auto_discovery_disable(ctx: TaskContext):
    # C7: the old revert deleted Bags/BagMRU AGAIN (wiping views created
    # after apply) while leaving FolderType=NotSpecified in place — Undo
    # made things worse. Per-folder views deleted by apply cannot be
    # restored (no backup was taken), so revert only removes the
    # FolderType value this tweak set and says so honestly.
    reg_delete_value(ctx, "HKCU",
                     r"Software\Classes\Local Settings\Software\Microsoft\Windows\Shell\Bags\AllFolders\Shell",
                     "FolderType")
    ctx.log("Explorer AutoDiscovery undone (FolderType reset). Note: per-folder "
            "views cleared by apply cannot be restored — they rebuild as you browse.")

# Disables background apps #
def apply_background_apps_disable(ctx: TaskContext):
    ctx.set_status("Background apps...")
    # MED-007: Undo used to blind-delete this value, destroying a
    # pre-existing different setting. The real prior state is
    # snapshotted now, so Undo restores what was actually there,
    # "absent" included.
    _regn_apply(ctx, 'background_apps', [("HKCU", "Software\\Microsoft\\Windows\\CurrentVersion\\BackgroundAccessApplications", "GlobalUserDisabled", 1)])
    ctx.log("Background apps applied.")


def revert_background_apps_disable(ctx: TaskContext):
    # MED-007: was a blind `reg_delete_value(...)`, which destroyed
    # a pre-existing different value. Now restores the snapshot.
    _regn_revert(ctx, 'background_apps', "Background apps")


# Sets NVIDIA shader cache to 10GB #
def apply_shader_cache_10gb(ctx: TaskContext):
    ctx.log("[Tweak] Shader Cache Size 10GB")
    # NVIDIA: HKCU\Software\NVIDIA Corporation\Global\FTS
    # F-005: the old try/except-swallow around this write meant a real
    # failure still reported success ('badge lied'). But CreateKeyEx would
    # also CREATE the FTS key (junk) on non-NVIDIA machines. Honest shape:
    # skip with a log line when NVIDIA's key isn't there; raise when a real
    # write to the real key fails.
    from app.utils import reg_get_value
    if reg_get_value(ctx, "HKCU", "Software\\NVIDIA Corporation\\Global\\FTS",
                     "RMShaderCacheSize") is None and \
       reg_get_value(ctx, "HKCU", "Software\\NVIDIA Corporation\\Global\\NvControlPanel2\\Client",
                     "OptInOrOutPreference") is None:
        # B5 audit fix: this skip used to log + `return`, badging the tweak
        # '✓ Active' although nothing changed — TaskSkipped keeps the
        # guard honest (completed-with-skip, no badge).
        raise TaskSkipped("NVIDIA settings key not found — skipping (nothing to change on this GPU).")
    reg_set_value_checked(ctx, "HKCU", "Software\\NVIDIA Corporation\\Global\\FTS", "EnableGR535", 1)
    # B1 fix: RMShaderCacheSize is a REG_DWORD in MB units (the NVCP
    # "Shader Cache Size" selector is MB; driver defaults are 128/1024 MB),
    # NOT bytes. The old value 10737418240 (= 0x280000000) overflows the
    # DWORD type the driver created, so the write silently failed under
    # -ErrorAction SilentlyContinue and the tweak was badged "✓ Active"
    # while doing nothing. 10 GB = 10240 MB, a valid DWORD — and it is
    # written through the checked helper so a failed write RAISES instead
    # of a fake success.
    reg_set_value_checked(ctx, "HKCU", "Software\\NVIDIA Corporation\\Global\\FTS",
                          "RMShaderCacheSize", 10240)  # 10 GB in MB (REG_DWORD)
    ctx.log("Shader cache size set to 10 GB (10240 MB) for NVIDIA.")

def revert_shader_cache_10gb(ctx: TaskContext):
    # M3 fix: revert BOTH values set by apply (previously left EnableGR535=1)
    reg_delete_value(ctx, "HKCU", "Software\\NVIDIA Corporation\\Global\\FTS", "RMShaderCacheSize")
    reg_delete_value(ctx, "HKCU", "Software\\NVIDIA Corporation\\Global\\FTS", "EnableGR535")
    ctx.log("Shader cache size reverted.")

# Sets GPU to prefer max performance #
_NVIDIA_MAX_PERF_PAIRS = [("sub_processor", "PROCTHROTTLEMAX")]
_NVIDIA_MAX_PERF_FALLBACK = {"sub_processor:PROCTHROTTLEMAX": 100}  # Windows' documented default (0x64)

def apply_nvidia_max_performance(ctx: TaskContext):
    ctx.log("[Tweak] Prefer Max Performance")
    # M1 fix: snapshot the real current value before we overwrite it, so
    # Undo restores what was actually there instead of a hardcoded guess.
    # C5: drop our snapshot if the sets below fail (else a false badge).
    had_snapshot = bool(get_tweak_snapshot("max_performance_gpu"))
    _snapshot_powercfg_pairs(ctx, "max_performance_gpu", _NVIDIA_MAX_PERF_PAIRS)
    # CT-002 audit fix: this tweak also writes NVIDIA's own
    # PowerMizerEnable=0 below, but only the powercfg pairs were ever
    # snapshotted — Undo left PowerMizerEnable stuck at 0 permanently.
    # Snapshot it under its own task id (distinct from the powercfg
    # snapshot's shape/key) so revert can restore or remove it too.
    had_pmz_snapshot = bool(get_tweak_snapshot("max_performance_gpu_pmz"))
    _snap_reg_values(ctx, "max_performance_gpu_pmz",
                     [("HKCU", "Software\\NVIDIA Corporation\\Global\\FTS", "PowerMizerEnable")])
    try:
        # Generic via powercfg + NVIDIA PowerMizer
        run_cmd_checked(ctx, "powercfg /setacvalueindex scheme_current sub_processor PROCTHROTTLEMAX 100", timeout=30)
        run_cmd_checked(ctx, "powercfg /setactive scheme_current", timeout=30)
        # H1-class quoting fix: the old shell=True string embedded literal
        # \" sequences (PowerShell ParserError on every run, rc ignored).
        # shell=False argv + PS single quotes; a missing FTS key is fine
        # (non-NVIDIA or driver without it) — only a real write failure raises.
        run_cmd(ctx, ["powershell", "-NoProfile", "-Command",
                      "if (Test-Path 'HKCU:\\Software\\NVIDIA Corporation\\Global\\FTS\\') { "
                      "Set-ItemProperty -Path 'HKCU:\\Software\\NVIDIA Corporation\\Global\\FTS\\' "
                      "-Name 'PowerMizerEnable' -Value 0 -ErrorAction Stop }"],
                shell=False, timeout=60)
    except Exception:
        if not had_snapshot:
            clear_tweak_snapshot("max_performance_gpu")
        if not had_pmz_snapshot:
            clear_tweak_snapshot("max_performance_gpu_pmz")
        raise
    ctx.log("GPU prefer max performance set.")

def revert_nvidia_max_performance(ctx: TaskContext):
    # MED-010: this used to run _restore_powercfg_pairs FIRST and
    # _restore_reg_values second, with nothing between them. powercfg shells
    # out several times and can fail (a stripped power plan, a subgroup the
    # plan does not have); when it raised, PowerMizerEnable was left at 0
    # PERMANENTLY with no revert path back. An ordering dependency with no
    # rollback.
    #
    # Both halves are now attempted, registry first (a single cheap local
    # write, versus several subprocesses), and their failures are reported
    # together. Neither can now silently strand the other.
    problems = []
    # CT-002 audit fix: restore (or remove, if it was absent before) the
    # PowerMizerEnable value apply_nvidia_max_performance wrote — previously
    # left permanently at 0 with no revert path at all.
    try:
        if _restore_reg_values(ctx, "max_performance_gpu_pmz") <= 0:
            problems.append("PowerMizerEnable (no snapshot recorded)")
    except Exception as exc:
        problems.append(f"PowerMizerEnable: {exc}")
    # M1 fix: restore the snapshotted real prior value (was hardcoded to 90,
    # which is BELOW Windows' real default of 100 — Undo used to leave the
    # CPU throttled more than it was before the tweak ever ran).
    try:
        _restore_powercfg_pairs(ctx, "max_performance_gpu",
                                _NVIDIA_MAX_PERF_PAIRS,
                                _NVIDIA_MAX_PERF_FALLBACK)
    except Exception as exc:
        problems.append(f"power plan: {exc}")
    if problems:
        # Raise, but only AFTER both halves were attempted, so the caller is
        # told everything that is still outstanding rather than fixing one
        # problem and discovering the other on the next Undo.
        raise RuntimeError(
            "GPU performance Undo was only partially applied ("
            + "; ".join(problems) + "). Anything already restored stays "
            "restored — run Undo again to retry the rest."
        )
    ctx.log("GPU performance reverted.")

# Disables fullscreen optimizations #
def apply_fullscreen_optimizations_disable(ctx: TaskContext):
    ctx.set_status("Fullscreen optimizations...")
    # MED-007: Undo used to blind-delete this value, destroying a
    # pre-existing different setting. The real prior state is
    # snapshotted now, so Undo restores what was actually there,
    # "absent" included.
    _regn_apply(ctx, 'fullscreen_opt', [("HKCU", "Software\\Microsoft\\Windows NT\\CurrentVersion\\AppCompatFlags\\Layers", "DISABLEDXMAXIMIZEDWINDOWEDMODE", 1)])
    ctx.log("Fullscreen optimizations applied.")


def revert_fullscreen_optimizations_disable(ctx: TaskContext):
    # MED-007: was a blind `reg_delete_value(...)`, which destroyed
    # a pre-existing different value. Now restores the snapshot.
    _regn_revert(ctx, 'fullscreen_opt', "Fullscreen optimizations")



# --------------------------------------------------------------------------- #
# New merged tweaks (researched from Sophia Script / privacy.sexy — reversible)
# --------------------------------------------------------------------------- #

_MAX_CPU_POWER_PAIRS = [
    ("sub_processor", "PERFBOOSTMODE"),
    ("sub_processor", "CPMINCORES"),
    ("sub_processor", "CPMAXCORES"),
    ("sub_processor", "PROCTHROTTLEMIN"),
]
# Windows' documented stock AC defaults — only used as a last-resort fallback
# when no snapshot exists (e.g. tweak applied by an older app version).
_MAX_CPU_POWER_FALLBACK = {
    "sub_processor:PERFBOOSTMODE": 1,     # Enabled (stock default on most editions)
    "sub_processor:CPMINCORES": 0,        # 0% min cores — Windows manages core parking
    "sub_processor:CPMAXCORES": 100,
    "sub_processor:PROCTHROTTLEMIN": 5,   # Windows' typical stock min processor state
}

def apply_max_cpu_power(ctx: TaskContext):
    """Aggressive CPU boost + no core parking + 100% min processor state (AC).
    Biggest single CPU-latency win for gaming laptops and many desktops."""
    # M1 fix: snapshot real current values before overwriting them.
    # H5/C5: the old unchecked run_cmd calls logged success on LTSC/VMs
    # where powercfg exits non-zero — and left a snapshot behind (false
    # badge). Checked writes + snapshot cleanup on failure.
    had_snapshot = bool(get_tweak_snapshot("max_cpu_power"))
    _snapshot_powercfg_pairs(ctx, "max_cpu_power", _MAX_CPU_POWER_PAIRS)
    try:
        run_cmd_checked(ctx, "powercfg /setacvalueindex scheme_current sub_processor PERFBOOSTMODE 2", timeout=30)   # Aggressive
        run_cmd_checked(ctx, "powercfg /setacvalueindex scheme_current sub_processor CPMINCORES 100", timeout=30)    # no core parking
        run_cmd_checked(ctx, "powercfg /setacvalueindex scheme_current sub_processor CPMAXCORES 100", timeout=30)
        run_cmd_checked(ctx, "powercfg /setacvalueindex scheme_current sub_processor PROCTHROTTLEMIN 100", timeout=30)  # min 100%
        run_cmd_checked(ctx, "powercfg /setactive scheme_current", timeout=30)
    except Exception:
        if not had_snapshot:
            clear_tweak_snapshot("max_cpu_power")
        raise
    ctx.log("CPU boost set to Aggressive; core parking off; min processor state 100% (AC).")

def revert_max_cpu_power(ctx: TaskContext):
    # M1 fix: restore the machine's real prior values instead of hardcoded
    # ones (PERFBOOSTMODE 0=Disabled and PROCTHROTTLEMIN 5 were not this
    # machine's actual defaults in the audited case — stock was 80).
    _restore_powercfg_pairs(ctx, "max_cpu_power", _MAX_CPU_POWER_PAIRS, _MAX_CPU_POWER_FALLBACK)
    ctx.log("CPU power settings restored to their prior values.")


def _is_win11_or_newer() -> bool:
    """True on Windows 11+ (build 22000+). Win10 (incl. IoT LTSC 2021) has
    no Widgets/Chat/search-highlights keys — attempting them there creates
    junk values at best, Access-denied noise at worst."""
    try:
        import sys as _sys
        if not _sys.platform.startswith("win"):
            return False
        import platform as _pf
        ver = _pf.version()  # e.g. '10.0.22631'
        parts = ver.split(".")
        if len(parts) >= 3 and parts[0] == "10" and parts[1] == "0":
            return int(parts[2]) >= 22000
    except Exception:
        pass
    return False


def apply_taskbar_cleanup(ctx: TaskContext):
    """Win11: remove Widgets, Chat/Teams icon, Meet Now, search highlights,
    and OneDrive ads in File Explorer — all background CPU/RAM consumers.

    LTSC/user-reported fix: the old code used reg_set_value_checked for all
    5 values, so ONE denied/missing value (observed: TaskbarDa -> WinError 5
    on Win10 IoT LTSC 2021, where the Win11-only Widgets key doesn't exist)
    failed the WHOLE tweak. Now each value is best-effort: Win11-only keys
    are skipped with a log line on Win10, other failures are logged, and the
    tweak only fails when NOTHING could be applied.

    BUG-017: the prior states are now SNAPSHOTTED. Revert used to hardcode a
    restore value of 1 for every key, but the Windows default for
    ShowSyncProviderNotifications and IsDynamicSearchBoxEnabled is 0/absent.
    So Undo turned Explorer sync-provider ads ON for a user who never had them
    on - the opposite of what they clicked. Every key this apply writes is
    snapshotted so Undo restores what was actually there.
    """
    from app.utils import reg_set_value as _set
    adv = "Software\\Microsoft\\Windows\\CurrentVersion\\Explorer\\Advanced"
    is_win11 = _is_win11_or_newer()
    if not is_win11:
        ctx.log("  (Win10 detected — Widgets/Chat/search-highlights are Win11-only; applying what exists here)")
    # (hive, path, name, value, win11_only)
    writes = [
        ("HKCU", adv, "TaskbarDa", 0, True),        # Widgets
        ("HKCU", adv, "TaskbarMn", 0, True),        # Chat / Teams icon
        ("HKCU", adv, "ShowSyncProviderNotifications", 0, False),  # Explorer ads
        ("HKCU", "Software\\Microsoft\\Windows\\CurrentVersion\\SearchSettings", "IsDynamicSearchBoxEnabled", 0, True),
        ("HKLM", "Software\\Microsoft\\Windows\\CurrentVersion\\Policies\\Explorer", "HideSCAMeetNow", 1, False),
    ]
    # Snapshot ONLY the keys we are actually going to write. On Win10 the
    # Win11-only keys are never touched, so recording them would widen the
    # revert's blast radius for no reason.
    active = [w for w in writes if not (w[4] and not is_win11)]
    had_snapshot = bool(get_tweak_snapshot("taskbar_cleanup"))
    _snap_reg_values(ctx, "taskbar_cleanup",
                     [(h, p, n) for h, p, n, _v, _w in active])
    ok, skipped = 0, []
    for hive, path, name, value, win11_only in writes:
        if win11_only and not is_win11:
            skipped.append(f"{name} (Win11-only — not present on Win10)")
            ctx.log(f"  (skipped) {hive}\\{path}\\{name}: Win11-only key on Win10 — nothing to change.")
            continue
        if _set(ctx, hive, path, name, value):
            ok += 1
        else:
            skipped.append(f"{name} (blocked — policy or permissions)")
    if ok == 0:
        # Nothing was applied, so there is nothing to undo - drop the snapshot
        # we just took rather than leaving a badge the user cannot clear.
        if not had_snapshot:
            clear_tweak_snapshot("taskbar_cleanup")
        raise RuntimeError(
            "Could not apply any taskbar setting "
            f"({'; '.join(skipped) or 'all writes blocked'}). "
            "Nothing was marked as applied."
        )
    if skipped:
        ctx.log(f"  (applied {ok} of {len(writes)}; skipped: {'; '.join(skipped)})")
    ctx.log("Widgets, Chat icon, search highlights and Explorer ads disabled. Restart Explorer to see changes.")

def revert_taskbar_cleanup(ctx: TaskContext):
    """Restore every value this tweak wrote to the state it was ACTUALLY in
    before, from the snapshot apply took.

    BUG-017, two halves:
      1. It used to write a hardcoded 1 for all four HKCU values and blind-delete
         HideSCAMeetNow. The Windows default for ShowSyncProviderNotifications
         and IsDynamicSearchBoxEnabled is 0 (absent), so Undo switched Explorer
         sync-provider ads ON for a user who never had them on.
      2. It never raised. `if ok == 0: ctx.log(...)` was immediately followed by
         an unconditional "Taskbar items restored.", so a total write failure
         still told the user their taskbar had been restored. Restoring through
         _restore_reg_values makes each failed write raise; the count it returns
         lets the "nothing happened" case be reported as the failure it is.
    """
    had_snapshot = bool(get_tweak_snapshot("taskbar_cleanup"))
    restored = _restore_reg_values(ctx, "taskbar_cleanup")
    if restored <= 0:
        raise RuntimeError(
            "Taskbar Undo could not restore anything"
            + ("" if had_snapshot else
               " — no snapshot was recorded, so the prior values are unknown "
               "and guessing would re-enable settings you never had.")
            + " Nothing was changed."
        )
    ctx.log("Taskbar items restored to their prior settings. Restart Explorer to see changes.")


_LOCAL_SEARCH_SPECS = [
    ("HKCU", "Software\\Policies\\Microsoft\\Windows\\Explorer", "DisableSearchBoxSuggestions"),
    ("HKLM", "SOFTWARE\\Policies\\Microsoft\\Windows\\Explorer", "DisableSearchBoxSuggestions"),
    ("HKCU", "Software\\Microsoft\\Windows\\CurrentVersion\\Search", "BingSearchEnabled"),
    ("HKCU", "Software\\Microsoft\\Windows\\CurrentVersion\\Search", "CortanaConsent"),
    ("HKCU", "Software\\Microsoft\\Windows\\CurrentVersion\\SearchSettings", "IsMSACloudSearchEnabled"),
    ("HKCU", "Software\\Microsoft\\Windows\\CurrentVersion\\SearchSettings", "IsAADCloudSearchEnabled"),
]


def apply_local_search(ctx: TaskContext):
    """Make Start-menu search local-only and instant: no Bing, no web results,
    no cloud content (Sophia Script + privacy.sexy verified values)."""
    # M3: snapshot priors — the old revert deleted unconditionally.
    # F09: best-effort (HKLM half needs admin; limited-mode keeps HKCU half).
    from app.utils import reg_set_value as _set
    had_snapshot = bool(get_tweak_snapshot("local_search"))
    _snap_reg_values(ctx, "local_search", _LOCAL_SEARCH_SPECS)
    writes = [
        ("HKCU", "Software\\Policies\\Microsoft\\Windows\\Explorer", "DisableSearchBoxSuggestions", 1),
        ("HKLM", "SOFTWARE\\Policies\\Microsoft\\Windows\\Explorer", "DisableSearchBoxSuggestions", 1),
        ("HKCU", "Software\\Microsoft\\Windows\\CurrentVersion\\Search", "BingSearchEnabled", 0),
        ("HKCU", "Software\\Microsoft\\Windows\\CurrentVersion\\Search", "CortanaConsent", 0),
        ("HKCU", "Software\\Microsoft\\Windows\\CurrentVersion\\SearchSettings", "IsMSACloudSearchEnabled", 0),
        ("HKCU", "Software\\Microsoft\\Windows\\CurrentVersion\\SearchSettings", "IsAADCloudSearchEnabled", 0),
    ]
    ok, skipped = 0, []
    for hive, path, name, val in writes:
        if _set(ctx, hive, path, name, val):
            ok += 1
        else:
            skipped.append(f"{hive}\\{name}")
    if ok == 0:
        if not had_snapshot:
            clear_tweak_snapshot("local_search")
        raise RuntimeError("Could not apply local-search settings (all writes blocked). Nothing was marked as applied.")
    if skipped:
        ctx.log(f"  (applied {ok} of {len(writes)}; skipped: {'; '.join(skipped)})")
    ctx.log("Search is now local-only — results appear instantly with no web/Bing content.")

def revert_local_search(ctx: TaskContext):
    snap = get_tweak_snapshot("local_search")
    if snap and "specs" in snap:
        _restore_reg_values(ctx, "local_search")
    else:
        reg_delete_value(ctx, "HKCU", "Software\\Policies\\Microsoft\\Windows\\Explorer", "DisableSearchBoxSuggestions")
        reg_delete_value(ctx, "HKLM", "SOFTWARE\\Policies\\Microsoft\\Windows\\Explorer", "DisableSearchBoxSuggestions")
        reg_delete_value(ctx, "HKCU", "Software\\Microsoft\\Windows\\CurrentVersion\\Search", "BingSearchEnabled")
        reg_delete_value(ctx, "HKCU", "Software\\Microsoft\\Windows\\CurrentVersion\\Search", "CortanaConsent")
        reg_delete_value(ctx, "HKCU", "Software\\Microsoft\\Windows\\CurrentVersion\\SearchSettings", "IsMSACloudSearchEnabled")
        reg_delete_value(ctx, "HKCU", "Software\\Microsoft\\Windows\\CurrentVersion\\SearchSettings", "IsAADCloudSearchEnabled")
        ctx.log("  (no snapshot found — removed the values; Windows then uses its defaults)")
    ctx.log("Search restored to defaults (Bing + web suggestions back).")


_ADS_CDM_NAMES = (
    "SubscribedContent-338387Enabled",
    "SubscribedContent-338388Enabled",
    "SubscribedContent-338389Enabled",
    "SubscribedContent-338393Enabled",
    "SubscribedContent-353694Enabled",
    "SubscribedContent-353696Enabled",
    "SilentInstalledAppsEnabled",
    "PreInstalledAppsEnabled",
    "OemPreInstalledAppsEnabled",
    "SystemPaneSuggestionsEnabled",
    "RotatingLockScreenOverlayEnabled",
    "SoftLandingEnabled",
    # Winhance-audit additions: timeline suggestions + welcome
    # experience slots, same safety shape as the rest (HKCU CDM DWORDs).
    "SubscribedContent-353698Enabled",
    "SubscribedContent-310093Enabled",
)
_ADS_CDM = "Software\\Microsoft\\Windows\\CurrentVersion\\ContentDeliveryManager"
_STOP_ADS_SPECS = (
    [("HKCU", _ADS_CDM, n) for n in _ADS_CDM_NAMES]
    + [("HKCU", "Software\\Microsoft\\Windows\\CurrentVersion\\UserProfileEngagement",
        "ScoobeSystemSettingEnabled")]
    + [("HKLM", "SOFTWARE\\Policies\\Microsoft\\Windows\\CloudContent", "DisableSoftLanding")]
    + [("HKLM", "SOFTWARE\\Policies\\Microsoft\\Windows\\CloudContent", "DisableCloudOptimizedContent")]
)


def apply_stop_windows_ads(ctx: TaskContext):
    """The full ContentDeliveryManager sweep — every 'suggested content',
    auto-installed app, lock-screen ad and tip switch in one go."""
    # M3: snapshot priors — the old revert hardcoded 1s + deletes.
    # F09: best-effort (HKLM CloudContent needs admin).
    from app.utils import reg_set_value as _set
    cdm = _ADS_CDM
    had_snapshot = bool(get_tweak_snapshot("stop_windows_ads"))
    _snap_reg_values(ctx, "stop_windows_ads", _STOP_ADS_SPECS)
    writes = [(("HKCU", cdm, n, 0)) for n in _ADS_CDM_NAMES]
    writes += [("HKCU", "Software\\Microsoft\\Windows\\CurrentVersion\\UserProfileEngagement", "ScoobeSystemSettingEnabled", 0),
               ("HKLM", "SOFTWARE\\Policies\\Microsoft\\Windows\\CloudContent", "DisableSoftLanding", 1),
               ("HKLM", "SOFTWARE\\Policies\\Microsoft\\Windows\\CloudContent", "DisableCloudOptimizedContent", 1)]
    ok, skipped = 0, []
    for hive, path, name, val in writes:
        if _set(ctx, hive, path, name, val):
            ok += 1
        else:
            skipped.append(f"{hive}\\{name}")
    if ok == 0:
        if not had_snapshot:
            clear_tweak_snapshot("stop_windows_ads")
        raise RuntimeError("Could not disable Windows ads (all writes blocked). Nothing was marked as applied.")
    if skipped:
        ctx.log(f"  (applied {ok} of {len(writes)}; skipped: {'; '.join(skipped)})")
    ctx.log("Windows ads, suggestions, auto-installs and lock-screen tips disabled.")

def revert_stop_windows_ads(ctx: TaskContext):
    snap = get_tweak_snapshot("stop_windows_ads")
    if snap and "specs" in snap:
        _restore_reg_values(ctx, "stop_windows_ads")
    else:
        cdm = _ADS_CDM
        for name in _ADS_CDM_NAMES:
            reg_set_value_checked(ctx, "HKCU", cdm, name, 1)
        reg_set_value_checked(ctx, "HKCU", "Software\\Microsoft\\Windows\\CurrentVersion\\UserProfileEngagement", "ScoobeSystemSettingEnabled", 1)
        reg_delete_value(ctx, "HKLM", "SOFTWARE\\Policies\\Microsoft\\Windows\\CloudContent", "DisableSoftLanding")
        reg_delete_value(ctx, "HKLM", "SOFTWARE\\Policies\\Microsoft\\Windows\\CloudContent", "DisableCloudOptimizedContent")
        ctx.log("  (no snapshot found — restored to documented Windows defaults instead)")
    ctx.log("Windows suggestions and tips restored to defaults.")


_PRIVACY_BASELINE_SPECS = [
    ("HKCU", "Software\\Microsoft\\Windows\\CurrentVersion\\AdvertisingInfo", "Enabled"),
    ("HKCU", "Software\\Microsoft\\Windows\\CurrentVersion\\Privacy", "TailoredExperiencesWithDiagnosticDataEnabled"),
    ("HKCU", "Software\\Microsoft\\Windows\\CurrentVersion\\Explorer\\Advanced", "Start_TrackProgs"),
    ("HKLM", "SOFTWARE\\Policies\\Microsoft\\Windows\\System", "EnableActivityFeed"),
    ("HKCU", "Software\\Microsoft\\Windows\\CurrentVersion\\CapabilityAccessManager\\ConsentStore\\humaninterfaceenterprise",
     "Value", "REG_SZ"),
    ("HKCU", "Control Panel\\International\\User Profile", "HttpAcceptLanguageOptOut"),
    ("HKCU", "Software\\Microsoft\\Windows\\CurrentVersion\\OnlineSpeechPrivacy", "HasAccepted"),
    ("HKCU", "Software\\Microsoft\\Siuf\\Rules", "NumberOfSIUFInPeriod"),
    ("HKCU", "Software\\Microsoft\\Input\\TIPC", "Enabled"),
    ("HKLM", "SOFTWARE\\Policies\\Microsoft\\Input\\TIPC", "Enabled"),
    ("HKCU", "Software\\Microsoft\\Personalization\\Settings", "AcceptedPrivacyPolicy"),
    ("HKCU", "Software\\Microsoft\\InputPersonalization\\TrainedDataStore", "HarvestContacts"),
]


def apply_privacy_baseline(ctx: TaskContext):
    """One-click privacy baseline: advertising ID, activity feed, app-launch
    tracking, input personalization, online speech, tailored experiences,
    language-list access, feedback prompts. All standard HKCU values that
    Windows itself exposes in Settings — fully reversible."""
    # M3: snapshot priors — the old revert deleted unconditionally, losing
    # any non-default priors (e.g. a user who had tailored experiences ON).
    # F09: best-effort (two HKLM values need admin).
    from app.utils import reg_set_value as _set
    had_snapshot = bool(get_tweak_snapshot("privacy_baseline"))
    _snap_reg_values(ctx, "privacy_baseline", _PRIVACY_BASELINE_SPECS)
    writes = [
        ("HKCU", "Software\\Microsoft\\Windows\\CurrentVersion\\AdvertisingInfo", "Enabled", 0, "REG_DWORD"),
        ("HKCU", "Software\\Microsoft\\Windows\\CurrentVersion\\Privacy", "TailoredExperiencesWithDiagnosticDataEnabled", 0, "REG_DWORD"),
        ("HKCU", "Software\\Microsoft\\Windows\\CurrentVersion\\Explorer\\Advanced", "Start_TrackProgs", 0, "REG_DWORD"),
        ("HKLM", "SOFTWARE\\Policies\\Microsoft\\Windows\\System", "EnableActivityFeed", 0, "REG_DWORD"),
        ("HKCU", "Software\\Microsoft\\Windows\\CurrentVersion\\CapabilityAccessManager\\ConsentStore\\humaninterfaceenterprise", "Value", "Deny", "REG_SZ"),
        ("HKCU", "Control Panel\\International\\User Profile", "HttpAcceptLanguageOptOut", 1, "REG_DWORD"),
        ("HKCU", "Software\\Microsoft\\Windows\\CurrentVersion\\OnlineSpeechPrivacy", "HasAccepted", 0, "REG_DWORD"),
        ("HKCU", "Software\\Microsoft\\Siuf\\Rules", "NumberOfSIUFInPeriod", 0, "REG_DWORD"),
        ("HKCU", "Software\\Microsoft\\Input\\TIPC", "Enabled", 0, "REG_DWORD"),
        ("HKLM", "SOFTWARE\\Policies\\Microsoft\\Input\\TIPC", "Enabled", 0, "REG_DWORD"),
        ("HKCU", "Software\\Microsoft\\Personalization\\Settings", "AcceptedPrivacyPolicy", 0, "REG_DWORD"),
        ("HKCU", "Software\\Microsoft\\InputPersonalization\\TrainedDataStore", "HarvestContacts", 0, "REG_DWORD"),
    ]
    ok, skipped = 0, []
    for hive, path, name, val, vtype in writes:
        if _set(ctx, hive, path, name, val, value_type=vtype):
            ok += 1
        else:
            skipped.append(f"{hive}\\{name}")
    if ok == 0:
        if not had_snapshot:
            clear_tweak_snapshot("privacy_baseline")
        raise RuntimeError("Could not apply privacy baseline (all writes blocked). Nothing was marked as applied.")
    if skipped:
        ctx.log(f"  (applied {ok} of {len(writes)}; skipped: {'; '.join(skipped)})")
    # audit fix (AllowTelemetry coupling): this task used to ALSO write
    # AllowTelemetry=1 here and DELETE it in revert — the exact same value
    # limit_telemetry owns. Reverting Privacy Baseline silently undid
    # Limit Tracking while its badge still read 'applied'. limit_telemetry
    # is now the single owner of that value; this task no longer touches it.
    ctx.log("Privacy baseline applied: ads ID, activity feed, tracking, typing data and speech uploads off.")

def revert_privacy_baseline(ctx: TaskContext):
    snap = get_tweak_snapshot("privacy_baseline")
    if snap and "specs" in snap:
        _restore_reg_values(ctx, "privacy_baseline")
    else:
        reg_delete_value(ctx, "HKCU", "Software\\Microsoft\\Windows\\CurrentVersion\\AdvertisingInfo", "Enabled")
        reg_delete_value(ctx, "HKCU", "Software\\Microsoft\\Windows\\CurrentVersion\\Privacy", "TailoredExperiencesWithDiagnosticDataEnabled")
        reg_delete_value(ctx, "HKCU", "Software\\Microsoft\\Windows\\CurrentVersion\\Explorer\\Advanced", "Start_TrackProgs")
        reg_delete_value(ctx, "HKLM", "SOFTWARE\\Policies\\Microsoft\\Windows\\System", "EnableActivityFeed")
        reg_set_value_checked(ctx, "HKCU", "Software\\Microsoft\\Windows\\CurrentVersion\\CapabilityAccessManager\\ConsentStore\\humaninterfaceenterprise", "Value", "Allow", value_type="REG_SZ")
        reg_delete_value(ctx, "HKCU", "Control Panel\\International\\User Profile", "HttpAcceptLanguageOptOut")
        reg_delete_value(ctx, "HKCU", "Software\\Microsoft\\Windows\\CurrentVersion\\OnlineSpeechPrivacy", "HasAccepted")
        reg_delete_value(ctx, "HKCU", "Software\\Microsoft\\Siuf\\Rules", "NumberOfSIUFInPeriod")
        reg_delete_value(ctx, "HKCU", "Software\\Microsoft\\Input\\TIPC", "Enabled")
        reg_delete_value(ctx, "HKLM", "SOFTWARE\\Policies\\Microsoft\\Input\\TIPC", "Enabled")
        reg_delete_value(ctx, "HKCU", "Software\\Microsoft\\Personalization\\Settings", "AcceptedPrivacyPolicy")
        reg_delete_value(ctx, "HKCU", "Software\\Microsoft\\InputPersonalization\\TrainedDataStore", "HarvestContacts")
        ctx.log("  (no snapshot found — removed the values; Windows then uses its defaults)")
    # audit fix: AllowTelemetry delete removed — limit_telemetry owns that
    # value (see the note in apply_privacy_baseline). Deleting it here broke
    # Limit Tracking for anyone who had both applied.
    ctx.log("Privacy baseline reverted to Windows defaults.")


def apply_stop_telemetry(ctx: TaskContext):
    """Disable telemetry services + all CEIP/App-Experience scheduled tasks.
    Does NOT block network hosts or delete system files (safety: keeps
    Windows Update fully functional). Services are set to 'disabled' and
    can be restored by the revert."""
    # H5: the old unchecked run_cmd calls logged success even when every
    # service/task was absent (LTSC) or access-denied. Best-effort with
    # honest counting: individual misses warn, but zero successes skips.
    # M5/M6 parity: snapshot the real prior service start types so revert
    # restores them instead of guessing auto/demand.
    had_snapshot = bool(get_tweak_snapshot("stop_telemetry"))
    _priors: dict = {}
    for _svc in ("DiagTrack", "dmwappushservice"):
        _prior = sc_query_start_type(ctx, _svc)
        if _prior is not None:
            _priors[_svc] = _prior
    if _priors:
        save_tweak_snapshot("stop_telemetry", _priors)
    _ok = 0
    for _svc in ("DiagTrack", "dmwappushservice"):
        if ctx.cancelled():
            raise TaskCancelled("Stop Telemetry stopped by user.")
        if run_cmd(ctx, f"sc config {_svc} start= disabled", timeout=30) == 0:
            _ok += 1
        else:
            ctx.log(f"  ! skipped (service absent or access denied): sc config {_svc} start= disabled")
    # Volatile stop only — unchecked and uncounted (a reboot undoes it; the
    # persistent change is the sc config above). Same shape as M5 SysMain.
    run_cmd(ctx, "net stop DiagTrack", timeout=30)
    # BUG-016: record each diagnostic task's REAL prior state before
    # disabling it. These ten used to be disabled with nothing recorded, and
    # revert then re-enabled ALL TEN unconditionally — so on any machine where
    # a task (or a corporate policy) had already disabled some of them, Undo
    # re-armed exactly the telemetry collection the user asked to suppress.
    # Only tasks that were actually enabled get disabled, and only those get
    # restored.
    task_priors = _collect_schtasks_priors(ctx, _telemetry_tasks)
    for _task, _was_disabled in task_priors.items():
        if ctx.cancelled():
            raise TaskCancelled("Stop Telemetry stopped by user.")
        if _was_disabled:
            continue          # already off — leave it, and it stays recorded
        if run_cmd(ctx, f'schtasks /change /tn "{_task}" /disable', timeout=30) == 0:
            _ok += 1
        else:
            ctx.log(f"  ! skipped (task absent): {_task}")
    if task_priors:
        _merge_tweak_snapshot("stop_telemetry", {"task_priors": task_priors})
    if _ok == 0:
        if not had_snapshot:
            clear_tweak_snapshot("stop_telemetry")
        # Nothing to do (LTSC/VM with no DiagTrack+tasks) is a skip, not a
        # failure — same contract as NVIDIA opt-out / SSD tweaks.
        raise TaskSkipped("No telemetry service or task found — nothing to disable (nothing to do).")
    ctx.log(f"Telemetry services and diagnostic scheduled tasks disabled ({_ok} change(s) applied).")

def revert_stop_telemetry(ctx: TaskContext):
        # M5/M6 parity: restore the snapshotted prior start types. Fall back to
    # the documented Windows defaults only when no snapshot exists (tweak
    # applied by an older app version, or config cleared).
    snap = get_tweak_snapshot("stop_telemetry") or {}
    _defaults = {"DiagTrack": "auto", "dmwappushservice": "demand"}
    _targets: dict = {}
    for _svc, _dflt in _defaults.items():
        _t = snap.get(_svc)
        if _t is None:
            ctx.log(f"  (no saved prior {_svc} start type — restoring `{_dflt}`, the Windows default)")
            _t = _dflt
        elif not _valid_snapshot_start_type(_t):
            raise _snapshot_validation_error("stop_telemetry", f"{_svc} start_type", _t)
        _targets[_svc] = _t
    _ok = 0
    for _svc, _t in _targets.items():
        if ctx.cancelled():
            raise TaskCancelled("Stop Telemetry restore stopped by user — snapshot kept for Undo.")
        if run_cmd(ctx, f"sc config {_svc} start= {_t}", timeout=30) == 0:
            _ok += 1
        else:
            ctx.log(f"  ! skipped (service absent or access denied): sc config {_svc} start= {_t}")
    # Volatile start only — unchecked and uncounted (mirror of apply).
    run_cmd(ctx, "net start DiagTrack", timeout=30)
    # BUG-016 mirror: restore each task to its RECORDED state, not to
    # "enabled". Tasks with no recorded prior state are left alone entirely.
    _priors = snap.get("task_priors")
    if isinstance(_priors, dict) and _priors:
        for _task, _was_disabled in _priors.items():
            if not isinstance(_task, str) or not _task.strip():
                continue
            if ctx.cancelled():
                raise TaskCancelled("Stop Telemetry restore stopped by user "
                                   "— snapshot kept for Undo.")
            # A task recorded as ALREADY-DISABLED must stay disabled; only the
            # ones this apply actually turned off get turned back on. (Getting
            # this backwards is the whole bug: the first version of this line
            # read `enable if _was_disabled else disable`, which re-enabled
            # precisely the tasks the user had switched off.)
            _want = "disable" if _was_disabled else "enable"
            if run_cmd(ctx, f'schtasks /change /tn "{_task}" /{_want}',
                       timeout=30) == 0:
                _ok += 1
            else:
                ctx.log(f"  ! skipped (task absent): {_task}")
    else:
        ctx.log("  (no recorded per-task states — diagnostic tasks left "
                "untouched rather than re-enabled blindly.)")
    if _ok == 0:
        raise TaskSkipped("No telemetry service or task found — nothing to restore.")
    clear_tweak_snapshot("stop_telemetry")
    ctx.log(f"Telemetry services and tasks re-enabled ({_ok} change(s) applied).")


def apply_nvidia_telemetry_optout(ctx: TaskContext):
    """NVIDIA telemetry opt-out (unique gamer feature from privacy.sexy):
    control-panel preference flags + NvTelemetry service + tasks. Driver
    itself is untouched; no files deleted.

    F-005: the old try/except-swallow meant a genuinely failed write still
    reported success. Honest shape now: skip when no NVIDIA software is
    present (nothing to opt out of — verified via the NvControlPanel key
    OR the NvTelemetryContainer service), raise when a real write to the
    real key fails."""
    from app.utils import reg_get_value
    has_nv_key = reg_get_value(ctx, "HKCU",
                               "Software\\NVIDIA Corporation\\Global\\NvControlPanel2\\Client",
                               "OptInOrOutPreference") is not None \
                 or reg_get_value(ctx, "HKCU",
                                  "Software\\NVIDIA Corporation\\Global\\FTS",
                                  "EnableRID44231") is not None
    has_nv_service = sc_query_start_type(ctx, "NvTelemetryContainer") is not None
    if not has_nv_key and not has_nv_service:
        # B5 audit fix: log + `return` used to badge this '✓ Active' with
        # nothing changed — TaskSkipped = completed-with-skip (no badge).
        raise TaskSkipped("No NVIDIA software detected — skipping telemetry opt-out (nothing to do).")
    if has_nv_key:
        reg_set_value_checked(ctx, "HKCU", "Software\\NVIDIA Corporation\\Global\\NvControlPanel2\\Client", "OptInOrOutPreference", 0)
        reg_set_value_checked(ctx, "HKCU", "Software\\NVIDIA Corporation\\Global\\FTS", "EnableRID44231", 0)
        reg_set_value_checked(ctx, "HKCU", "Software\\NVIDIA Corporation\\Global\\FTS", "EnableRID64640", 0)
        reg_set_value_checked(ctx, "HKCU", "Software\\NVIDIA Corporation\\Global\\FTS", "EnableRID66610", 0)
    else:
        ctx.log("  (preference keys absent — service-side opt-out only)")
    # M6 fix: snapshot the service's real current start type before changing
    # it — previously this set "demand" unconditionally and revert set the
    # exact same value, so the service state was never actually changed by
    # either direction, while claiming to opt the service out.
    #
    # MED-001: neither the `sc config` nor any `Disable-ScheduledTask`
    # result was checked, so the task reported a clean opt-out on a machine
    # where it had changed nothing. A snapshot is also now cleared if the
    # service change fails, so a failed apply cannot leave a "✓ Active"
    # badge and an Undo record for a change that never happened.
    changed = []
    if has_nv_service:
        prior_start_type = sc_query_start_type(ctx, "NvTelemetryContainer")
        save_tweak_snapshot("nvidia_telemetry", {"NvTelemetryContainer_start_type": prior_start_type or "demand"})
        rc = run_cmd(ctx, "sc config NvTelemetryContainer start= disabled")
        if rc == -1 and ctx.cancelled():
            clear_tweak_snapshot("nvidia_telemetry")
            raise TaskCancelled("NVIDIA telemetry opt-out cancelled by user")
        if rc == 0:
            changed.append("service")
        else:
            # Nothing else has run yet, so drop the snapshot rather than
            # leave an Undo entry describing a change that did not occur.
            clear_tweak_snapshot("nvidia_telemetry")
            ctx.log(f"  ! could not disable NvTelemetryContainer (code {rc})")
    # BUG-016: record each task's REAL prior state before touching it. The
    # old code disabled every NvTm* task by wildcard and recorded nothing, so
    # revert re-enabled tasks the user (or a corporate policy) had already
    # switched off — the exact opposite of opting out of telemetry, and the
    # inverse of what Undo promises. Only tasks that were actually enabled
    # are disabled, and only those are restored.
    task_priors = _collect_task_priors(ctx, _NVIDIA_TASK_PATTERNS)
    task_hits = 0
    for name, was_disabled in task_priors.items():
        if was_disabled:
            continue          # already off before we got here — leave it off
        if _set_task_enabled(ctx, name, False):
            task_hits += 1
    if task_priors:
        _merge_tweak_snapshot("nvidia_telemetry", {"task_priors": task_priors})
    if task_hits:
        changed.append(f"{task_hits} scheduled task"
                       f"{'s' if task_hits > 1 else ''}")
    if not changed:
        # Preserve the registry half of the work, if any happened above.
        clear_tweak_snapshot("nvidia_telemetry")
        raise RuntimeError(
            "NVIDIA telemetry was not opted out: the telemetry service could "
            "not be disabled and no report task could be disabled. This "
            "usually needs Administrator rights.")
    ctx.log("NVIDIA telemetry opted out ("
            + ", ".join(changed) + ").")

def revert_nvidia_telemetry_optout(ctx: TaskContext):
    """F-005 mirror: restore only what exists. reg_delete_value already
    treats FileNotFound as success, so absent keys are fine; but a genuine
    failure (ACL/policy) must not be swallowed — the deletes raise through
    reg_delete_value's False return only via log today, so check the
    NvControlPanel key first and let real deletes fail loudly upstream."""
    from app.utils import reg_get_value
    has_nv_key = reg_get_value(ctx, "HKCU",
                               "Software\\NVIDIA Corporation\\Global\\NvControlPanel2\\Client",
                               "OptInOrOutPreference") is not None \
                 or reg_get_value(ctx, "HKCU",
                                  "Software\\NVIDIA Corporation\\Global\\FTS",
                                  "EnableRID44231") is not None
    if has_nv_key:
        reg_delete_value(ctx, "HKCU", "Software\\NVIDIA Corporation\\Global\\NvControlPanel2\\Client", "OptInOrOutPreference")
        reg_delete_value(ctx, "HKCU", "Software\\NVIDIA Corporation\\Global\\FTS", "EnableRID44231")
        reg_delete_value(ctx, "HKCU", "Software\\NVIDIA Corporation\\Global\\FTS", "EnableRID64640")
        reg_delete_value(ctx, "HKCU", "Software\\NVIDIA Corporation\\Global\\FTS", "EnableRID66610")
    # M6 fix: restore the service's real prior start type instead of writing
    # the same "demand" value apply() already wrote.
    snap = get_tweak_snapshot("nvidia_telemetry")
    start_type = snap.get("NvTelemetryContainer_start_type", "demand")
    # F-1: the snapshot value flows into a shell command string — only a
    # start-type keyword this app could have snapshotted may run.
    if not _valid_snapshot_start_type(start_type):
        raise _snapshot_validation_error("nvidia_telemetry", "start_type", start_type)
    # MED-009: the rc used to be discarded and clear_tweak_snapshot ran
    # UNCONDITIONALLY below. So a failed restore reported "NVIDIA telemetry
    # settings restored.", destroyed the ONLY undo record, and left the
    # service disabled forever — with no way to retry and no badge to say so.
    #
    # The rc is now checked, AND the restore is verified by re-reading the
    # live start type rather than trusting the exit code alone (the same
    # shape revert_ultimate_performance uses). The snapshot is cleared only
    # after a verified restore, so a failed Undo stays retryable and the
    # next apply cannot snapshot the tweak's own output as "prior state".
    #
    # argv + shell=False here too: `sc` is a real executable, so there is no
    # reason to hand a config-file-derived string to a shell parser at all.
    # _valid_snapshot_start_type stays as defence in depth.
    rc = run_cmd(ctx, ["sc", "config", "NvTelemetryContainer",
                       "start=", start_type], shell=False, timeout=60)
    if rc == -1 and ctx.cancelled():
        # Nothing was written, and the snapshot must survive so the user can
        # retry Undo once the cancel clears.
        raise TaskCancelled("NVIDIA telemetry restore cancelled by user")
    if rc != 0:
        raise RuntimeError(
            f"Could not restore NvTelemetryContainer's start type (exit {rc}) "
            "— snapshot kept so Undo can be retried. To finish by hand, run "
            "'sc config NvTelemetryContainer start= demand' from an "
            "Administrator prompt."
        )
    live = sc_query_start_type(ctx, "NvTelemetryContainer")
    if live is not None and not _start_types_equal(live, start_type):
        raise RuntimeError(
            f"NvTelemetryContainer reports start type {live!r} but "
            f"{start_type!r} was requested (exit {rc}) — the restore did not "
            "take. Snapshot kept so Undo can be retried. To finish by hand, "
            "run 'sc config NvTelemetryContainer start= demand' from an "
            "Administrator prompt."
        )
    # BUG-016 mirror: restore each task to the state it was RECORDED in, not
    # to "enabled". Enabling everything matching NvTm* used to re-activate
    # tasks the user had deliberately switched off before this app ran.
    # Tasks with no recorded prior state are left exactly as they are.
    priors = snap.get("task_priors")
    restored = 0
    if isinstance(priors, dict) and priors:
        for name, was_disabled in priors.items():
            if not isinstance(name, str) or not name.strip():
                continue
            if _set_task_enabled(ctx, name, not was_disabled):
                restored += 1
        if restored:
            ctx.log(f"  restored {restored} scheduled task"
                    f"{'s' if restored > 1 else ''} to their recorded state.")
    else:
        ctx.log("  (no recorded per-task states — scheduled tasks left "
                "untouched rather than re-enabled blindly.)")
    # MED-009: the snapshot is cleared only now — after the service restore was
    # VERIFIED. It used to be cleared unconditionally, one line below the
    # unchecked `sc config`, which meant a failed Undo destroyed the only undo
    # record and left the service disabled permanently with nothing to retry.
    clear_tweak_snapshot("nvidia_telemetry")
    ctx.log("NVIDIA telemetry settings restored.")


def apply_ad_blocker(ctx: TaskContext):
    """System-wide ad blocker via the hosts file.

    Writes a de-duplicated, merged block of known ad/tracker domains
    (StevenBlack + AdAway lists, cross-referenced) between marker comments,
    redirecting each to 0.0.0.0 so requests fail instead of loading ads.

    Fully reversible: the original hosts file is backed up once (on first
    apply) and the revert function simply strips the marked block back out.
    Takes effect after a DNS flush; a reboot is not required but is a safe
    way to make sure all apps pick it up.

    F05: the block is ~2MB. Windows resolves hosts linearly, so first-visit
    lookups can cost extra milliseconds on slow disks — if browsing feels
    slower after applying, Undo Tweaks restores the original file.
    """
    ctx.set_status("Applying system-wide ad blocker (hosts file)...")

    asset_path = resolve_asset_path("adblock_hosts.txt")
    if not asset_path:
        # audit fix: these were bare `return None` — the runner counted
        # None as SUCCESS and marked the tweak applied, so the badge lied
        # while nothing was blocked. Raise so the runner reports failure.
        raise RuntimeError("Could not find bundled ad-block list (adblock_hosts.txt missing).")

    if not os.path.isfile(_HOSTS_PATH):
        raise RuntimeError(f"Hosts file not found at {_HOSTS_PATH}.")

    try:
        with open(_HOSTS_PATH, "r", encoding="utf-8", errors="ignore") as f:
            current = f.read()
    except Exception as exc:
        raise RuntimeError(f"Could not read hosts file: {exc}")

    # Back up the ORIGINAL hosts file exactly once. If we ever re-apply after
    # already having applied (e.g. to refresh the list), we must not overwrite
    # an existing backup with our own already-modified file.
    # SEC-001 audit fix: exclusive-create instead of isfile()-check + plain
    # open("w") — that was a racy check-then-write that also followed a
    # symlink if one was already planted at the backup path.
    try:
        created = exclusive_create_text(_HOSTS_BACKUP_PATH, current)
        if created:
            ctx.log(f"Backed up original hosts file to {_HOSTS_BACKUP_PATH}")
    except Exception as exc:
        raise RuntimeError(f"Could not back up hosts file, aborting for safety: {exc}")

    # Strip any previous block first (idempotent re-apply / list refresh)
    base_content = _strip_adblock_block(current)

    try:
        with open(asset_path, "r", encoding="utf-8", errors="ignore") as f:
            block_lines = f.read()
    except Exception as exc:
        raise RuntimeError(f"Could not read bundled ad-block list: {exc}")

    # M4: count real domain mappings, not every non-blank line (future
    # header/comment lines in the bundled list must not inflate the count).
    # Hosts mapping lines start with 0.0.0.0 (the list's redirect target).
    domain_count = sum(1 for line in block_lines.splitlines()
                       if line.strip() and not line.strip().startswith("#")
                       and line.split()[0] == "0.0.0.0")

    if not base_content.endswith("\n"):
        base_content += "\n"
    new_content = (
        base_content
        + "\n" + _ADBLOCK_MARKER_START + "\n"
        + f"# {domain_count} ad/tracker domains — do not edit this block by hand,\n"
        + "# use the Cleaner Tool app to update or remove it.\n"
        + block_lines
        + _ADBLOCK_MARKER_END + "\n"
    )

    try:
        _write_hosts_file(new_content)
    except Exception as exc:
        raise RuntimeError(f"Could not write hosts file (need admin rights?): {exc}")

    run_cmd(ctx, "ipconfig /flushdns", timeout=30)
    try:
        _block_bytes = len(block_lines.encode("utf-8", "ignore"))
        from app.utils import format_bytes as _fmt2
        _size_note = f" (~{_fmt2(_block_bytes)} added to hosts)"
    except Exception:
        _size_note = ""
    ctx.log(f"Ad blocker applied: {domain_count} domains now blocked system-wide{_size_note}.")
    ctx.log("Note: a multi-MB hosts file can slow first-visit lookups on slow disks — "
            "Undo Tweaks restores the original file if browsing feels slower.")


def revert_ad_blocker(ctx: TaskContext):
    """Remove the ad-block block from the hosts file, restoring normal DNS
    resolution for those domains. Does not touch anything else a user may
    have manually added to their hosts file."""
    ctx.set_status("Removing system-wide ad blocker (hosts file)...")

    if not os.path.isfile(_HOSTS_PATH):
        raise RuntimeError(f"Hosts file not found at {_HOSTS_PATH}.")

    try:
        with open(_HOSTS_PATH, "r", encoding="utf-8", errors="ignore") as f:
            current = f.read()
    except Exception as exc:
        raise RuntimeError(f"Could not read hosts file: {exc}")

    if _ADBLOCK_MARKER_START not in current:
        ctx.log("Ad blocker block not found in hosts file (already removed?).")
        return

    new_content = _strip_adblock_block(current)

    try:
        _write_hosts_file(new_content)
    except Exception as exc:
        raise RuntimeError(f"Could not write hosts file (need admin rights?): {exc}")

    run_cmd(ctx, "ipconfig /flushdns", timeout=30)
    ctx.log("Ad blocker removed. Hosts file restored to its previous state.")


def _strip_adblock_block(content: str) -> str:
    """Remove our marker block (and one blank line we add before it) from
    hosts-file content, if present. Leaves everything else untouched."""
    start = content.find(_ADBLOCK_MARKER_START)
    if start == -1:
        return content
    end = content.find(_ADBLOCK_MARKER_END, start)
    if end == -1:
        # Marker start with no matching end (corrupted) — cut from start to EOF
        cleaned = content[:start]
    else:
        end += len(_ADBLOCK_MARKER_END)
        cleaned = content[:start] + content[end:]
    # Trim the blank line we insert right before the start marker
    cleaned = cleaned.rstrip("\n") + "\n"
    return cleaned


def _write_hosts_file(content: str):
    """Write hosts file content atomically — unique temp file in the SAME
    directory, flush + fsync, then os.replace (the config_persist.py
    pattern). The old truncate-and-write (open(path, "w")) left a corrupt,
    truncated hosts file — breaking ALL DNS resolution — if the machine
    crashed, lost power, or an AV tool locked the file mid-write, with no
    auto-recovery. restore_readonly clears the read-only attribute that some
    AV/security tools flip (os.replace also needs the destination writable)
    and re-applies it to the replaced file — the exact dance this function
    used to hand-roll.
    F3-2: the shared helper's unique temp name means a concurrent repair-tab
    hosts restore can no longer clobber this write mid-flight (both used to
    blindly reuse the fixed name hosts.cleanertool.tmp)."""
    atomic_write_text(_HOSTS_PATH, content, newline="\n", restore_readonly=True)


# --------------------------------------------------------------------------- #
# Focus / stability tweaks (tasks.txt additions)
# --------------------------------------------------------------------------- #

_ACCESSIBILITY_FLAGS = (
    ("HKCU", "Control Panel\\Accessibility\\StickyKeys", "Flags", "506"),
    ("HKCU", "Control Panel\\Accessibility\\Keyboard Response", "Flags", "122"),
    ("HKCU", "Control Panel\\Accessibility\\ToggleKeys", "Flags", "58"),
)


def apply_disable_sticky_keys(ctx: TaskContext):
    """Stop the 'Do you want to turn on Sticky Keys?' popup and its beep
    from interrupting games when Shift is tapped repeatedly (sprint,
    crouch). Sets the accessibility hotkey Flags to 'hotkey off' states —
    the same values Windows' own Settings panel writes when you untick
    the shortcut checkboxes. Fully reversible."""
    ctx.set_status("Disabling Sticky Keys popups...")
    # MED-007: the old revert wrote hardcoded "Windows defaults" ("510",
    # "126", "62"), which are NOT what a user who had customised their
    # accessibility hotkeys had. Undo silently overwrote their settings.
    # The real prior values are snapshotted now.
    _regn_apply(ctx, "disable_sticky_keys",
                [w + ("REG_SZ",) for w in _ACCESSIBILITY_FLAGS],
                error="Could not write accessibility Flags values.")
    ctx.log("Sticky Keys / Filter Keys / Toggle Keys popups disabled.")


def revert_disable_sticky_keys(ctx: TaskContext):
    """Restore the accessibility hotkey flags to their previous values."""
    ctx.set_status("Restoring accessibility popups...")
    # MED-007: was three hardcoded writes of the "Windows default", not a
    # restore. Now puts back what the user actually had.
    _regn_revert(ctx, "disable_sticky_keys", "Accessibility popups")


def apply_suppress_crash_popups(ctx: TaskContext):
    ctx.set_status("Crash popups...")
    # MED-007: Undo used to blind-delete this value, destroying a
    # pre-existing different setting. The real prior state is
    # snapshotted now, so Undo restores what was actually there,
    # "absent" included.
    _regn_apply(ctx, 'suppress_crash_popups', [("HKCU", "Software\\Microsoft\\Windows\\Windows Error Reporting", "DontShowUI", 1)])
    ctx.log("Crash popups applied.")



def revert_suppress_crash_popups(ctx: TaskContext):
    # MED-007: was a blind `reg_delete_value(...)`, which destroyed
    # a pre-existing different value. Now restores the snapshot.
    _regn_revert(ctx, 'suppress_crash_popups', "Crash popups")



_HIDE_RECENT_SPECS = [
    ("HKCU", "Software\\Microsoft\\Windows\\CurrentVersion\\Explorer", "ShowRecent"),
    ("HKCU", "Software\\Microsoft\\Windows\\CurrentVersion\\Explorer", "ShowFrequent"),
]


def apply_hide_recent(ctx: TaskContext):
    """Stop Explorer Home from showing recent files and frequent folders —
    the switch that stops re-creating what the 'Clear Activity Traces'
    clean removes. Same values Settings writes when you untick both boxes
    (Folder Options > Privacy). Fully reversible via snapshot."""
    had_snapshot = bool(get_tweak_snapshot("hide_recent"))
    _snap_reg_values(ctx, "hide_recent", _HIDE_RECENT_SPECS)
    try:
        reg_set_value_checked(ctx, "HKCU", "Software\\Microsoft\\Windows\\CurrentVersion\\Explorer",
                              "ShowRecent", 0)
        reg_set_value_checked(ctx, "HKCU", "Software\\Microsoft\\Windows\\CurrentVersion\\Explorer",
                              "ShowFrequent", 0)
    except Exception:
        if not had_snapshot:
            clear_tweak_snapshot("hide_recent")
        raise
    if not _restart_explorer(ctx):
        raise RuntimeError(
            "Registry values set, but Explorer did not restart cleanly — "
            "your taskbar may be missing. Press Ctrl+Shift+Esc > File > Run "
            "new task > explorer.exe.")


def revert_hide_recent(ctx: TaskContext):
    snap = get_tweak_snapshot("hide_recent")
    if snap and "specs" in snap:
        _restore_reg_values(ctx, "hide_recent")
    else:
        # MED-007: the legacy fallback wrote 1, claiming that was "the
        # documented Windows default". It is not: the default is 0/absent, so
        # a user who had never enabled recent files got them ENABLED by
        # clicking Undo - the opposite of the change they were undoing. With
        # no snapshot the honest move is to write the value apply wrote's
        # opposite-of-default, i.e. 0, and to SAY the prior value is unknown
        # rather than pretending a guess is a restore.
        reg_set_value_checked(ctx, "HKCU", "Software\\Microsoft\\Windows\\CurrentVersion\\Explorer",
                              "ShowRecent", 0)
        reg_set_value_checked(ctx, "HKCU", "Software\\Microsoft\\Windows\\CurrentVersion\\Explorer",
                              "ShowFrequent", 0)
        ctx.log("  (no snapshot found — this was applied by an older version, so "
                "your previous setting is unknown. Set both to 0, the Windows "
                "default, rather than the 1 the old Undo used to write.)")
    if not _restart_explorer(ctx):
        ctx.log("  ! Explorer did not restart cleanly — your taskbar may be missing. "
                "Press Ctrl+Shift+Esc > File > Run new task > explorer.exe.")


_TDR_SPECS = [
    ("HKLM", "SYSTEM\\CurrentControlSet\\Control\\GraphicsDrivers", "TdrDelay"),
]


def apply_tdr_delay(ctx: TaskContext):
    """Raise the GPU hang timeout from 2s to 8s — the documented fix for
    DXGI_ERROR_DEVICE_HUNG / driver-timeout crashes on overclocked or
    heavy-RT GPUs. Windows waits longer before resetting the driver
    instead of crashing the game. Reversible (delete = 2s default)."""
    had_snapshot = bool(get_tweak_snapshot("tdr_delay"))
    _snap_reg_values(ctx, "tdr_delay", _TDR_SPECS)
    try:
        reg_set_value_checked(ctx, "HKLM", "SYSTEM\\CurrentControlSet\\Control\\GraphicsDrivers",
                              "TdrDelay", 8)
    except Exception:
        if not had_snapshot:
            clear_tweak_snapshot("tdr_delay")
        raise
    ctx.log("GPU timeout raised to 8s. Reboot to take effect.")


def revert_tdr_delay(ctx: TaskContext):
    snap = get_tweak_snapshot("tdr_delay")
    if snap and "specs" in snap:
        _restore_reg_values(ctx, "tdr_delay")
    else:
        # Windows default is an ABSENT value (2s) — remove ours.
        reg_delete_value(ctx, "HKLM", "SYSTEM\\CurrentControlSet\\Control\\GraphicsDrivers", "TdrDelay")
        ctx.log("  (no snapshot found — removed the value; Windows then uses its 2s default)")
    ctx.log("Reboot to take effect.")
    ctx.log("Crash popups restored.")


def apply_no_update_reboot(ctx: TaskContext):
    ctx.set_status("Windows Update auto-reboot...")
    # MED-007: Undo used to blind-delete this value, destroying a
    # pre-existing different setting. The real prior state is
    # snapshotted now, so Undo restores what was actually there,
    # "absent" included.
    _regn_apply(ctx, 'no_update_reboot', [("HKLM", "SOFTWARE\\Policies\\Microsoft\\Windows\\WindowsUpdate\\AU", "NoAutoRebootWithLoggedOnUsers", 1)])
    ctx.log("Windows Update auto-reboot applied.")



def revert_no_update_reboot(ctx: TaskContext):
    # MED-007: was a blind `reg_delete_value(...)`, which destroyed
    # a pre-existing different value. Now restores the snapshot.
    _regn_revert(ctx, 'no_update_reboot', "Windows Update auto-reboot")



def apply_mpo_fix(ctx: TaskContext):
    ctx.set_status("MPO...")
    # MED-007: Undo used to blind-delete this value, destroying a
    # pre-existing different setting. The real prior state is
    # snapshotted now, so Undo restores what was actually there,
    # "absent" included.
    _regn_apply(ctx, 'mpo_fix', [("HKLM", "SOFTWARE\\Microsoft\\Windows\\Dwm", "OverlayTestMode", 5)])
    ctx.log("MPO applied.")



def revert_mpo_fix(ctx: TaskContext):
    # MED-007: was a blind `reg_delete_value(...)`, which destroyed
    # a pre-existing different value. Now restores the snapshot.
    _regn_revert(ctx, 'mpo_fix', "MPO")



# --------------------------------------------------------------------------- #
# Round 3 tasks (user request): NIC Energy-Efficient Ethernet, NTFS 8.3
# names, dynamic tick. All reversible — EEE snapshots each adapter's exact
# prior display values (gaming_dns pattern), 8.3 snapshots the queried
# prior fsutil state, dynamic tick reverts via bcdedit /deletevalue.
# --------------------------------------------------------------------------- #

_EEE_DISPLAY_NAMES = ("Energy Efficient Ethernet", "Energy-Efficient Ethernet (EEE)",
                      "Green Ethernet", "Power Saving Mode", "Gigabit Lite")


def _query_eee_props() -> "list[tuple[str, str, str]]":
    """(adapter name, property display name, current display value) for every
    active adapter that exposes an EEE-family property.

    C1-fix discipline: PowerShell argv list + shell=False so cmd.exe never
    mangles the quoting; single quotes inside the script text (PS's own
    string syntax) since there's no outer shell to strip them."""
    import subprocess as _sp
    names = "@('" + "','".join(_EEE_DISPLAY_NAMES) + "')"
    ps = ("Get-NetAdapter | Where-Object Status -eq 'Up' | "
          "Get-NetAdapterAdvancedProperty -ErrorAction SilentlyContinue | "
          f"Where-Object DisplayName -in {names} | "
          "ForEach-Object { 'EEE|' + $_.Name + '|' + $_.DisplayName + '|' + $_.DisplayValue }")
    try:
        out = _sp.run(["powershell", "-NoProfile", "-Command", ps], shell=False,
                      capture_output=True, text=True, timeout=30,
                      creationflags=getattr(_sp, "CREATE_NO_WINDOW", 0))
    except Exception:
        return []
    rows = []
    for line in (out.stdout or "").splitlines():
        parts = line.split("|", 3)
        if len(parts) == 4 and parts[0] == "EEE":
            rows.append((parts[1], parts[2], parts[3]))
    return rows


def _ps_sq(s: str) -> str:
    """Escape a string for embedding in a PowerShell single-quoted literal
    (L3: adapter/property names are system-controlled but can contain `'`,
    e.g. "John's Ethernet" — an unescaped quote breaks out of the PS
    string; doubling is PS's escape)."""
    return str(s).replace("'", "''")


def apply_eee_disable(ctx: TaskContext):
    """Disable Energy Efficient Ethernet / Green Ethernet on active NICs —
    the documented cause of micro-disconnects and speed drops on Realtek
    NICs. Snapshots each adapter's exact prior display value first (valid
    values are driver-defined: Realtek says Disabled/Enabled, others say
    Off/On — a hardcoded 'Enabled' revert would be wrong on some NICs)."""
    ctx.set_status("Disabling Energy Efficient Ethernet...")
    props = _query_eee_props()
    if not props:
        raise RuntimeError("No Energy Efficient Ethernet-capable adapter found.")
    snapshot = {"adapters": {}}                    # {adapter: {display: prior_value}}
    for adapter, display, value in props:
        snapshot["adapters"].setdefault(adapter, {})[display] = value
    # C5: a failed apply must not leave this snapshot behind (false badge).
    had_snapshot = bool(get_tweak_snapshot("eee_disable"))
    save_tweak_snapshot("eee_disable", snapshot)
    changed = 0
    try:
        for adapter, display, _value in props:
            # P7-02: honor Stop between adapters — a cancel must surface as
            # TaskCancelled, never as a half-applied 'success' or a bogus
            # 'no adapter accepted the change' error.
            if ctx.cancelled():
                raise TaskCancelled("EEE disable stopped by user (Undo can restore the partial state).")
            # shell=False argv (no cmd.exe mangling) + ''-escaped names.
            rc = run_cmd(
                ctx,
                ["powershell", "-NoProfile", "-Command",
                 f"Set-NetAdapterAdvancedProperty -Name '{_ps_sq(adapter)}' "
                 f"-DisplayName '{_ps_sq(display)}' -DisplayValue 'Disabled'"],
                shell=False,
                timeout=60)
            if rc == 0:
                changed += 1
            else:
                ctx.log(f"  ! {adapter}/{display}: could not set (code {rc}) — this NIC may use different values.")
        if not changed:
            # honest failure: nothing accepted the change — never claim success
            raise RuntimeError("No adapter accepted the change (NICs vary — check your adapter's own advanced tab).")
    except TaskCancelled:
        # keep the snapshot: a partial apply must stay undoable; the C5
        # 'no false badge' cleanup only applies to real failures.
        raise
    except Exception:
        if not had_snapshot:
            clear_tweak_snapshot("eee_disable")
        raise
    ctx.log(f"EEE disabled on {changed} propert(ies). Brief network blip is normal; reconnect if you were in a game.")


def revert_eee_disable(ctx: TaskContext):
    """Restore each adapter's exact prior display values from the snapshot
    (driver-defined strings, per the M1/M6 lesson). Adapters that vanished
    between apply and revert (USB NICs) just fail their Set- call and log —
    best-effort, mirror of revert_gaming_dns."""
    ctx.set_status("Re-enabling Energy Efficient Ethernet...")
    snap = get_tweak_snapshot("eee_disable")
    adapters = snap.get("adapters", {}) if snap else {}
    if not adapters:
        raise RuntimeError("No snapshot on file — cannot know the prior values. Set them manually in Device Manager.")
    restored = 0
    for adapter, props in adapters.items():
        for display, value in props.items():
            # P7-02: honor Stop mid-revert — snapshot stays on file (only
            # the 'nothing restored' path below decides the failure text).
            if ctx.cancelled():
                raise TaskCancelled("EEE restore stopped by user — snapshot kept for Undo.")
            rc = run_cmd(ctx,
                         ["powershell", "-NoProfile", "-Command",
                          f"Set-NetAdapterAdvancedProperty -Name '{_ps_sq(adapter)}' "
                          f"-DisplayName '{_ps_sq(display)}' -DisplayValue '{_ps_sq(value)}'"],
                         shell=False, timeout=60)
            if rc == 0:
                restored += 1
            else:
                ctx.log(f"  ! {adapter}/{display}: could not restore (code {rc}) — adapter may be gone.")
    # C6: keep the snapshot when NOTHING restored so Undo can be retried.
    if restored == 0:
        raise RuntimeError("No EEE setting could be restored — snapshot kept so Undo can be retried.")
    clear_tweak_snapshot("eee_disable")
    ctx.log("Energy Efficient Ethernet restored to previous values.")


def apply_ntfs_8dot3_disable(ctx: TaskContext):
    """Disable NTFS 8.3 short-name creation — speeds up folders with many
    files. Snapshot-first: the stock value is 2 (per-volume, system volume
    enabled) on modern Windows, so the honest revert is 'restore what
    fsutil reported', never a hardcoded guess (the source list's
    'revert to 0' was backwards: 1 disables, 0 enables)."""
    import subprocess as _sp
    ctx.set_status("Disabling 8.3 short-name creation...")
    try:
        out = _sp.run(["fsutil", "behavior", "query", "disable8dot3"], shell=False,
                      capture_output=True, text=True, timeout=15,
                      creationflags=getattr(_sp, "CREATE_NO_WINDOW", 0))
    except Exception as exc:
        raise RuntimeError(f"Could not query the current 8.3 setting: {exc}")
    # B6 audit fix: the numeric state (0/1/2) is locale-free, but this
    # used to run on unchecked output — a localized error/odd text meant
    # NO snapshot was saved and the revert later restored a hardcoded 2
    # guess. Parse the number explicitly and fail loudly (before any
    # change) when the output has no number to snapshot.
    prior_value = None
    if out.returncode == 0:
        for token in (out.stdout or "").split():
            if token.isdigit():
                prior_value = int(token)
                break
    if prior_value is None:
        ctx.log(f"  ! could not read the current 8.3 name setting from fsutil "
                f"(rc={out.returncode}: {(out.stdout or out.stderr or '').strip()[:120]}) — "
                "aborting so a revert never has to guess.")
        raise RuntimeError("Could not read the current 8.3 name setting — nothing was changed.")
    # C5: a failed set must not leave this snapshot behind (false badge).
    had_snapshot = bool(get_tweak_snapshot("ntfs_8dot3"))
    save_tweak_snapshot("ntfs_8dot3", {"value": prior_value})
    try:
        run_cmd_checked(ctx, "fsutil behavior set disable8dot3 1")
    except Exception:
        if not had_snapshot:
            clear_tweak_snapshot("ntfs_8dot3")
        raise
    ctx.log("8.3 short-name creation disabled (speeds up folders with many files).")


def revert_ntfs_8dot3_disable(ctx: TaskContext):
    snap = get_tweak_snapshot("ntfs_8dot3")
    prior = snap.get("value", 2) if snap else 2
    # F-1: the snapshot value flows into a shell command string — only a
    # numeric fsutil state (0/1/2/3) this app could have parsed may run.
    if not (isinstance(prior, int) and not isinstance(prior, bool) and 0 <= prior <= 3):
        raise _snapshot_validation_error("ntfs_8dot3", "value", prior)
    run_cmd_checked(ctx, f"fsutil behavior set disable8dot3 {prior}")
    clear_tweak_snapshot("ntfs_8dot3")
    ctx.log("8.3 name creation restored to previous setting.")


def apply_disable_dynamic_tick(ctx: TaskContext):
    """Disable the dynamic timer tick (bcdedit) — a legacy latency tweak.
    Gains are debatable on modern hardware; kept opt-in for users who
    benchmark. Reboot required."""
    run_cmd_checked(ctx, "bcdedit /set disabledynamictick yes")
    ctx.log("Dynamic tick disabled. Reboot required.")


def revert_disable_dynamic_tick(ctx: TaskContext):
    # /deletevalue exits 1 when the value doesn't exist (never applied) —
    # that's a no-op, not a failure. Same honesty pattern as
    # revert_limit_telemetry's delete-when-absent cases.
    rc = run_cmd(ctx, "bcdedit /deletevalue disabledynamictick")
    if rc != 0:
        ctx.log("  (value not present — nothing to remove)")
    ctx.log("Dynamic tick restored to Windows default. Reboot required.")

# Each active network adapter's DNS is snapshotted (by adapter GUID+index)
# before the switch, so revert puts back exactly what was there — including
# "no static DNS" (DHCP), which we record as None and restore by clearing.
_DNS_CLOUDFLARE = ["1.1.1.1", "1.0.0.1"]


def _dns_snapshot_key():
    return {"schema": 1, "adapters": {}}


def _dns_set_command(idx: str, servers) -> list:
    """PowerShell argv that sets an adapter's DNS servers (shell=False,
    no quoting surface). Extracted pure so the harness can assert the
    exact command for default AND custom servers without executing it."""
    quoted = ",".join(f"'{s}'" for s in servers)
    return ["powershell", "-NoProfile", "-Command",
            f"Set-DnsClientServerAddress -InterfaceIndex {idx} "
            f"-ServerAddresses ({quoted})"]


def apply_gaming_dns(ctx: TaskContext, servers=None):
    """Switch every active adapter's DNS (default Cloudflare 1.1.1.1 /
    1.0.0.1), snapshotting the prior static servers first so revert is
    exact. 1.1.1.1 is consistently among the fastest public resolvers
    for game server lookups; fully reversible, no reset needed.

    servers: optional (primary, secondary) pair — the DNS tester dialog
    passes the measured winner here. Default None keeps the classic
    Cloudflare behavior byte-identical (same command, same snapshot,
    same logs for the default pair)."""
    if servers is None:
        servers = tuple(_DNS_CLOUDFLARE)
    else:
        servers = (str(servers[0]), str(servers[1]))
    _label = "Cloudflare" if list(servers) == _DNS_CLOUDFLARE else " / ".join(servers)
    ctx.set_status(f"Switching DNS to {_label}...")
    if not IS_WINDOWS:
        raise RuntimeError("Windows only.")
    import subprocess as _sp
    try:
        out = _sp.run(
            ["powershell", "-NoProfile", "-Command",
             "Get-NetAdapter | Where-Object Status -eq 'Up' | "
             "ForEach-Object { $d = Get-DnsClientServerAddress -InterfaceIndex $_.ifIndex -AddressFamily IPv4 -ErrorAction SilentlyContinue; "
             "'ADAPT|' + $_.ifIndex + '|' + ($d.ServerAddresses -join ',') }"],
            capture_output=True, text=True, timeout=30,
            creationflags=getattr(_sp, "CREATE_NO_WINDOW", 0),
        )
    except Exception as exc:
        raise RuntimeError(f"Could not list network adapters: {exc}")
    snapshot = _dns_snapshot_key()
    switched = 0
    for line in (out.stdout or "").splitlines():
        if not line.startswith("ADAPT|"):
            continue
        # B-1 audit fix: the unpack target MUST NOT be named `servers` — it
        # shadowed the function parameter holding the NEW pair to apply, so
        # _dns_set_command received each adapter's OLD value (a comma-joined
        # string, iterated char-by-char into garbage single-char 'servers')
        # and the chosen pair was never set. `prior` is the adapter's current
        # DNS (snapshot use only); the set below uses the parameter.
        _, idx, prior = line.split("|", 2)
        idx = idx.strip()
        if not idx.isdigit():
            continue
        snapshot["adapters"][idx] = prior.strip() or None   # None = DHCP
        # H4: netsh `name=` wants the adapter NAME, not the numeric ifIndex
        # (passing the index fails or hits the wrong NIC). The PowerShell
        # cmdlet takes -InterfaceIndex directly — unambiguous, shell=False
        # argv so no quoting/injection surface. NOTE: ifIndex can change
        # across reboots/NIC swaps; revert only touches these snapshot
        # indexes and reports misses honestly.
        rc = run_cmd(ctx, _dns_set_command(idx, servers),
                     shell=False, timeout=30)
        if rc == 0:
            switched += 1
        else:
            ctx.log(f"  ! could not set DNS on adapter index {idx} (exit {rc}) — skipping it.")
    if not snapshot["adapters"]:
        raise RuntimeError("No active network adapters found — nothing to switch.")
    # H4/C5: saving before verifying left a snapshot + success log on 0
    # switched adapters (false badge). Only save when something changed.
    if switched == 0:
        raise RuntimeError(
            "Could not switch DNS on any adapter — nothing was changed "
            "(snapshot NOT saved, tweak not marked applied)."
        )
    save_tweak_snapshot("gaming_dns", snapshot)
    ctx.log(f"DNS switched to {_label} on {switched} adapter(s). (Snapshot saved for undo.)")


def revert_gaming_dns(ctx: TaskContext):
    """Restore each adapter's exact prior DNS — including DHCP (no static
    servers), which is recorded as None in the snapshot. Only ever touches
    adapters present in the snapshot: with no snapshot there is nothing
    this app configured, so it refuses to rewrite the network config
    (a blanket DHCP reset would destroy static DNS the app never set)."""
    ctx.set_status("Restoring previous DNS settings...")
    if not IS_WINDOWS:
        raise RuntimeError("Windows only.")
    snap = get_tweak_snapshot("gaming_dns")
    adapters = snap.get("adapters", {}) if snap else {}
    if not adapters:
        # B3 fix (eee_disable contract): this used to fall back to a
        # "best-effort" Set-DnsClientServerAddress -ResetServerAddresses
        # over EVERY up adapter, wiping pre-existing static DNS this app
        # never configured. No snapshot -> raise before running anything.
        raise RuntimeError(
            "No DNS snapshot on file — cannot know the prior DNS servers "
            "to restore. Restore your DNS manually: Settings > Network & "
            "Internet > your adapter > DNS (or ask your network "
            "administrator / ISP for the correct servers)."
        )
    import re as _re
    failures: list = []
    for idx, servers in adapters.items():
        if not str(idx).strip().isdigit():
            failures.append(str(idx))
            continue
        if servers:  # had static servers before — validate (snapshot lives
            # in user-writable config.json; never interpolate raw into PS)
            toks = [s.strip() for s in str(servers).split(",") if s.strip()]
            if not toks or not all(_re.fullmatch(r"[0-9.]+", t) for t in toks):
                ctx.log(f"  ! snapshot for adapter {idx} has unexpected servers {servers!r} — skipping it.")
                failures.append(str(idx))
                continue
            quoted = ",".join(f"'{t}'" for t in toks)
            rc = run_cmd(ctx, ["powershell", "-NoProfile", "-Command",
                               f"Set-DnsClientServerAddress -InterfaceIndex {idx} "
                               f"-ServerAddresses ({quoted})"],
                         shell=False, timeout=30)
            if rc != 0:
                failures.append(str(idx))
        else:        # was DHCP — clear static servers back to automatic
            rc = run_cmd(ctx, ["powershell", "-NoProfile", "-Command",
                               f"Set-DnsClientServerAddress -InterfaceIndex {idx} "
                               "-ResetServerAddresses"],
                         shell=False, timeout=30)
            if rc != 0:
                failures.append(str(idx))
    # C6: keep the snapshot when any adapter failed so Undo can be retried.
    if failures:
        raise RuntimeError(
            f"Could not restore DNS on adapter(s) {', '.join(failures)} — "
            "snapshot kept so Undo can be retried. Restore manually: "
            "Settings > Network & Internet > your adapter > DNS."
        )
    clear_tweak_snapshot("gaming_dns")
    ctx.log("DNS restored to previous settings.")


_DEVMODE_CLS = None


def _devmode_class():
    """Build (and cache) the one correct, complete DEVMODEW ctypes layout
    (wingdi.h) for EnumDisplaySettingsW / ChangeDisplaySettingsW.

    Grok-assisted audit fix: this file previously had THREE separate inline
    DEVMODE definitions (in _query_video_mode_list, apply_refresh_rate_fix,
    revert_refresh_rate_fix), and all three were missing ~84 bytes in the
    middle of the real struct — the dmColor/dmDuplex/dmYResolution/
    dmTTOption/dmCollate SHORTs, the 32-WCHAR dmFormName, and dmLogPixels —
    and had dmBitsPerPel positioned before dmPelsWidth/dmPelsHeight instead
    of after. dmSize told Windows how big THIS (too-short) buffer was, but
    EnumDisplaySettingsW/ChangeDisplaySettingsW write to the real struct's
    fixed offsets regardless of what dmSize claims — so this code's
    dmPelsWidth/dmPelsHeight fields were reading (and writing, in the
    apply/revert direction) bytes from the wrong real field entirely,
    producing the "0x0" resolution shown everywhere it's surfaced (Monitor
    Test, PC Specs) while dmDisplayFrequency, positioned differently,
    happened to still land close enough to read correctly. This is also
    the exact class of bug the file's own comment above already worried
    about ("a short struct lets a sloppy display driver write past the
    buffer — heap corruption... observed on virtual GPUs") — the fix for
    that is a COMPLETE struct (this one totals the documented 220 bytes),
    not a slightly-larger-but-still-wrong one.
    One shared, correct, size-verified definition now backs every caller
    instead of three separately-maintained (and equally wrong) copies.
    """
    global _DEVMODE_CLS
    if _DEVMODE_CLS is not None:
        return _DEVMODE_CLS
    import ctypes
    from ctypes import wintypes

    class DEVMODE(ctypes.Structure):
        _fields_ = [
            ("dmDeviceName", wintypes.WCHAR * 32),
            ("dmSpecVersion", wintypes.WORD),
            ("dmDriverVersion", wintypes.WORD),
            ("dmSize", wintypes.WORD),
            ("dmDriverExtra", wintypes.WORD),
            ("dmFields", wintypes.DWORD),
            # 16-byte union (display case: position + orientation/output).
            ("dmPositionX", ctypes.c_long),
            ("dmPositionY", ctypes.c_long),
            ("dmDisplayOrientation", wintypes.DWORD),
            ("dmDisplayFixedOutput", wintypes.DWORD),
            ("dmColor", ctypes.c_short),
            ("dmDuplex", ctypes.c_short),
            ("dmYResolution", ctypes.c_short),
            ("dmTTOption", ctypes.c_short),
            ("dmCollate", ctypes.c_short),
            ("dmFormName", wintypes.WCHAR * 32),
            ("dmLogPixels", wintypes.WORD),
            ("dmBitsPerPel", wintypes.DWORD),
            ("dmPelsWidth", wintypes.DWORD),
            ("dmPelsHeight", wintypes.DWORD),
            ("dmDisplayFlags", wintypes.DWORD),
            ("dmDisplayFrequency", wintypes.DWORD),
            ("dmICMMethod", wintypes.DWORD),
            ("dmICMIntent", wintypes.DWORD),
            ("dmMediaType", wintypes.DWORD),
            ("dmDitherType", wintypes.DWORD),
            ("dmReserved1", wintypes.DWORD),
            ("dmReserved2", wintypes.DWORD),
            ("dmPanningWidth", wintypes.DWORD),
            ("dmPanningHeight", wintypes.DWORD),
        ]
    assert ctypes.sizeof(DEVMODE) == 220, \
        f"DEVMODE layout drifted from the documented 220 bytes: {ctypes.sizeof(DEVMODE)}"
    _DEVMODE_CLS = DEVMODE
    return DEVMODE


def _query_video_mode_list():
    """Return (current, maximum) (width, height, hz, bits) tuples via
    EnumDisplaySettings, or (None, None) if anything fails."""
    if not IS_WINDOWS:
        return None, None
    import ctypes
    try:
        DEVMODE = _devmode_class()
        user32 = ctypes.windll.user32
        dm = DEVMODE()
        dm.dmSize = ctypes.sizeof(DEVMODE)
        if not user32.EnumDisplaySettingsW(None, ctypes.c_ulong(0xFFFFFFFF), ctypes.byref(dm)):  # ENUM_CURRENT_SETTINGS
            return None, None
        w, h = int(dm.dmPelsWidth), int(dm.dmPelsHeight)
        # Grok-suggested defensive fallback: on the rare driver that still
        # reports 0 even off the now-correct struct, GetSystemMetrics is a
        # second, independent way to get the real desktop resolution rather
        # than surfacing "0x0" to the user.
        if w <= 0 or h <= 0:
            w = int(user32.GetSystemMetrics(0))   # SM_CXSCREEN
            h = int(user32.GetSystemMetrics(1))   # SM_CYSCREEN
        current = (w, h, dm.dmDisplayFrequency, dm.dmBitsPerPel)
        # walk mode i=0.. until 0 to find the max frequency at current resolution+depth
        best = None
        i = 0
        while True:
            m = DEVMODE()
            m.dmSize = ctypes.sizeof(DEVMODE)
            if not user32.EnumDisplaySettingsW(None, i, ctypes.byref(m)):
                break
            if (m.dmPelsWidth, m.dmPelsHeight, m.dmBitsPerPel) == current[:1] + current[1:2] + current[3:4]:
                if best is None or m.dmDisplayFrequency > best:
                    best = m.dmDisplayFrequency
            i += 1
        maximum = (current[0], current[1], best, current[3]) if best else current
        return current, maximum
    except Exception:
        return None, None


def _query_precise_refresh_rate():
    """Exact rational refresh rate (e.g. 59.94Hz) via QueryDisplayConfig.
    Read-only, active paths only. Returns float or None — never guesses.
    Fail-closed: struct-size approximations that don't match hardware just
    return None and callers fall back to the integer Hz."""
    if not IS_WINDOWS:
        return None
    import ctypes
    from ctypes import wintypes
    try:
        class LUID(ctypes.Structure):
            _fields_ = [("LowPart", wintypes.DWORD), ("HighPart", wintypes.LONG)]

        class _RATIONAL(ctypes.Structure):
            _fields_ = [("Numerator", wintypes.UINT), ("Denominator", wintypes.UINT)]

        class _SRC(ctypes.Structure):
            _fields_ = [("adapterId", LUID), ("id", wintypes.UINT),
                        ("modeInfoIdx", wintypes.UINT), ("statusFlags", wintypes.UINT)]

        class _TGT(ctypes.Structure):
            _fields_ = [("adapterId", LUID), ("id", wintypes.UINT),
                        ("modeInfoIdx", wintypes.UINT), ("outputTechnology", wintypes.UINT),
                        ("rotation", wintypes.UINT), ("scaling", wintypes.UINT),
                        ("refreshRate", _RATIONAL), ("scanLineOrdering", wintypes.UINT),
                        ("targetAvailable", wintypes.BOOL), ("statusFlags", wintypes.UINT)]

        class _PATH(ctypes.Structure):
            _fields_ = [("sourceInfo", _SRC), ("targetInfo", _TGT), ("flags", wintypes.UINT)]

        user32 = ctypes.windll.user32
        pc, mc = wintypes.UINT(), wintypes.UINT()
        if user32.GetDisplayConfigBufferSizes(2, ctypes.byref(pc), ctypes.byref(mc)) != 0:
            return None
        if not pc.value:
            return None
        paths = (_PATH * pc.value)()
        modes = (ctypes.c_byte * (mc.value * 64))()
        if user32.QueryDisplayConfig(2, ctypes.byref(pc), ctypes.byref(paths),
                                     ctypes.byref(mc), ctypes.byref(modes), None) != 0:
            return None
        rr = paths[0].targetInfo.refreshRate
        if not rr.Denominator:
            return None
        return rr.Numerator / rr.Denominator
    except Exception:
        return None


def _query_monitor_model():
    """Best-effort monitor name from EDID via WmiMonitorID (root\\wmi
    namespace) — the same data Device Manager reads. Returns a plain
    string like 'AG276QZD2', or None if it can't be read: some
    monitors/cables/KVMs don't carry usable EDID data, and the class can
    be blocked on some locked-down images. Never guesses."""
    if not IS_WINDOWS:
        return None
    import subprocess as _sp
    try:
        cmd = ("Get-CimInstance -Namespace root\\wmi -ClassName WmiMonitorID "
               "-ErrorAction Stop | Select-Object UserFriendlyName,"
               "UserFriendlyNameLength | ConvertTo-Json -Compress")
        creationflags = getattr(_sp, "CREATE_NO_WINDOW", 0)
        proc = _sp.run(
            ["powershell", "-NoProfile", "-NonInteractive",
             "-Command", cmd],
            capture_output=True, text=True, timeout=4.0,
            creationflags=creationflags)
        raw = (proc.stdout or "").strip()
        if not raw:
            return None
        import json as _json
        data = _json.loads(raw)
        if isinstance(data, dict):
            data = [data]
        for row in data or []:
            codes = row.get("UserFriendlyName") or []
            length = row.get("UserFriendlyNameLength")
            if not codes:
                continue
            if not isinstance(length, int) or length <= 0:
                length = len(codes)
            try:
                name = "".join(chr(c) for c in codes[:length] if c).strip()
            except Exception:
                continue
            if name:
                return name
        return None
    except Exception:
        return None


def apply_refresh_rate_fix(ctx: TaskContext):
    """Detect the monitor's maximum refresh rate at the CURRENT resolution
    and depth; if Windows is running it lower (the classic 144Hz panel stuck
    at 60Hz), set the mode to the max via ChangeDisplaySettings. Snapshots
    the prior mode for exact revert. Skips honestly when already maxed."""
    ctx.set_status("Checking monitor refresh rate...")
    current, maximum = _query_video_mode_list()
    if not current or not maximum:
        raise RuntimeError("Could not query display settings.")
    cur_hz, max_hz = current[2], maximum[2]
    ctx.log(f"Current: {current[0]}x{current[1]} @ {cur_hz} Hz — monitor supports {max_hz} Hz at this resolution.")
    if cur_hz >= max_hz:
        # B5 audit fix: this used to save a snapshot, log and `return` —
        # the runner then badged the tweak '✓ Active' although nothing
        # changed (and the saved snapshot made the badge source say so
        # too). Nothing was changed, so nothing is snapshotted; the skip
        # is raised BEFORE the snapshot so state stays untouched.
        raise TaskSkipped(f"Already running at the maximum ({max_hz} Hz) — nothing to change.")
    import ctypes
    try:
        DEVMODE = _devmode_class()
        user32 = ctypes.windll.user32
        dm = DEVMODE()
        dm.dmSize = ctypes.sizeof(DEVMODE)
        dm.dmPelsWidth, dm.dmPelsHeight = maximum[0], maximum[1]
        dm.dmBitsPerPel = maximum[3]
        dm.dmDisplayFrequency = max_hz
        # B4 fix: 0x02000000 is DM_MEDIATYPE, not BPP — dmMediaType was
        # never populated, so the flag made no change while DM_BITSPERPEL
        # (0x00040000) was missing and the populated dmBitsPerPel below was
        # ignored. Bit depth IS intended (dmBitsPerPel = maximum[3], the
        # mode's real depth), so flag it properly.
        dm.dmFields = 0x00040000 | 0x00080000 | 0x00100000 | 0x00400000  # DM_BITSPERPEL | DM_PELSWIDTH | DM_PELSHEIGHT | DM_DISPLAYFREQUENCY
        # 0 = apply dynamically (no reboot), CDS_UPDATEREGISTRY makes it persist
        rc = user32.ChangeDisplaySettingsW(ctypes.byref(dm), 0x00000001)
        if rc != 0:
            raise RuntimeError(f"Display mode change failed (code {rc}).")
    except RuntimeError:
        raise
    except Exception as exc:
        raise RuntimeError(f"Could not apply refresh rate: {exc}")
    save_tweak_snapshot("refresh_rate_fix", {"mode": current})
    ctx.log(f"Refresh rate set to {max_hz} Hz (was {cur_hz} Hz). "
            "If the screen stays black for >10s, Windows reverts it automatically.")


def revert_refresh_rate_fix(ctx: TaskContext):
    """Restore the snapshotted display mode (resolution, Hz, depth)."""
    ctx.set_status("Restoring previous display mode...")
    snap = get_tweak_snapshot("refresh_rate_fix")
    mode = snap.get("mode") if snap else None
    if not mode:
        ctx.log("No snapshot on file — nothing to revert (the tweak only records when it changes something).")
        clear_tweak_snapshot("refresh_rate_fix")
        return
    import ctypes
    try:
        DEVMODE = _devmode_class()
        user32 = ctypes.windll.user32
        dm = DEVMODE()
        dm.dmSize = ctypes.sizeof(DEVMODE)
        w, h, hz, bpp = mode
        dm.dmPelsWidth, dm.dmPelsHeight = w, h
        dm.dmBitsPerPel = bpp
        dm.dmDisplayFrequency = hz
        # B4 fix: same as apply — 0x00040000 (DM_BITSPERPEL) so the
        # snapshotted dmBitsPerPel depth is actually honored; 0x02000000
        # (DM_MEDIATYPE) was a bogus flag with no populated field behind it.
        dm.dmFields = 0x00040000 | 0x00080000 | 0x00100000 | 0x00400000  # DM_BITSPERPEL | DM_PELSWIDTH | DM_PELSHEIGHT | DM_DISPLAYFREQUENCY
        rc = user32.ChangeDisplaySettingsW(ctypes.byref(dm), 0x00000001)
        if rc != 0:
            raise RuntimeError(f"Revert display mode failed (code {rc}).")
    except RuntimeError:
        raise
    except Exception as exc:
        raise RuntimeError(f"Could not revert refresh rate: {exc}")
    clear_tweak_snapshot("refresh_rate_fix")
    ctx.log(f"Display mode restored to {mode[0]}x{mode[1]} @ {mode[2]} Hz.")


# GpuPreference: 0 = let Windows decide, 1 = power-saving (iGPU), 2 = high
# performance (dGPU). A global "prefer dGPU" directive keeps laptops from
# launching games on the iGPU. Written as a REG_SZ — that's what Windows'
# own Graphics Settings UI writes.
_GPU_PREF_PATH = "SOFTWARE\\Microsoft\\DirectX\\UserGpuPreferences"
_GPU_PREF_VALUE = "DirectXUserGlobalSettings"


def _read_gpu_pref() -> str:
    """Read the current global GpuPreference string (empty if unset)."""
    if not IS_WINDOWS:
        return ""
    try:
        root = winreg.OpenKey(winreg.HKEY_CURRENT_USER, _GPU_PREF_PATH)
        try:
            val, _ = winreg.QueryValueEx(root, _GPU_PREF_VALUE)
            return str(val)
        finally:
            winreg.CloseKey(root)
    except OSError:
        return ""


def apply_gpu_preference_high(ctx: TaskContext):
    """Tell Windows to run everything on the dedicated GPU (per-user, no
    admin): sets DirectXUserGlobalSettings' GPU preference to 2. Snapshots
    the exact prior string (usually 'GPUPreference=0;' or empty) for a
    precise revert."""
    ctx.set_status("Setting games to prefer the dedicated GPU...")
    prior = _read_gpu_pref()
    new = "GPUPreference=2;"
    ok = reg_set_value(ctx, "HKCU", _GPU_PREF_PATH, _GPU_PREF_VALUE, new, value_type="REG_SZ")
    if not ok:
        raise RuntimeError("Could not write GPU preference.")
    save_tweak_snapshot("gpu_preference_high", {"value": prior})
    ctx.log("Windows will now run games on the dedicated (high-performance) GPU.")


def revert_gpu_preference_high(ctx: TaskContext):
    """Restore the exact prior global GPU preference string (empty values
    are restored by deleting the registry value)."""
    ctx.set_status("Restoring GPU preference...")
    snap = get_tweak_snapshot("gpu_preference_high")
    prior = snap.get("value", "") if snap else ""
    if prior:
        reg_set_value_checked(ctx, "HKCU", _GPU_PREF_PATH, _GPU_PREF_VALUE, prior, value_type="REG_SZ")
    else:
        reg_delete_value(ctx, "HKCU", _GPU_PREF_PATH, _GPU_PREF_VALUE)
    clear_tweak_snapshot("gpu_preference_high")
    ctx.log("GPU preference restored to previous setting.")


# --- Round 4: snapshot helpers (one value or several per tweak) --- #
def _snap_reg_values(ctx: TaskContext, task_id: str, specs: "list[tuple]"):
    """Snapshot the real prior state (value or absent) of each registry
    value so revert restores exactly what was there — never a guess.

    Each spec is (hive, path, name) or (hive, path, name, value_type);
    the per-value type is stored so mixed-type tweaks (REG_SZ + REG_DWORD)
    restore with the right type (M7 audit gap: the old single value_type
    arg restored every value as one type).
    """
    from app.config_persist import save_tweak_snapshot
    from app.utils import _REG_DENIED, reg_get_value_typed
    data: dict = {"specs": [list(s[:3]) for s in specs]}
    for i, spec in enumerate(specs):
        hive, path, name = spec[0], spec[1], spec[2]
        # CT-001 audit fix: read the REAL prior type via QueryValueEx instead
        # of guessing REG_DWORD for any spec that didn't spell out a 4th
        # element. The old default silently mis-stamped REG_SZ priors (e.g.
        # KeyboardDelay/KeyboardSpeed, WaitToKillAppTimeout — all natively
        # REG_SZ on Windows) as REG_DWORD, so restoring the string prior
        # under that type raised inside winreg.SetValueEx and revert failed
        # outright for every tweak using a 3-element spec against a REG_SZ
        # value (fast_app_close, keyboard_tuning, numlock_boot,
        # visual_effects and any future 3-element spec against a string
        # value). Precedence: real queried type first (it's what's actually
        # there); an explicit spec[3] override only when nothing could be
        # queried (value absent/denied); "REG_DWORD" as the last-resort
        # fallback so legacy snapshots without recorded types keep working.
        prior, queried_type = reg_get_value_typed(ctx, hive, path, name)
        # F13: denied reads are PRESENT-but-unreadable — never "absent"
        # (absent made revert delete a value the user already had).
        data[f"{i}:present"] = prior is not None and prior is not _REG_DENIED
        data[f"{i}:denied"] = prior is _REG_DENIED
        # config.json can't hold bytes (REG_BINARY priors) — hex-encode
        # with a marker; restore decodes. Without this the save below
        # throws TypeError and the tweak wrongly reports failure.
        if isinstance(prior, bytes):
            data[f"{i}:value"] = {"__bytes_hex__": prior.hex()}
        else:
            data[f"{i}:value"] = None if prior is _REG_DENIED else prior
        data[f"{i}:type"] = queried_type or (spec[3] if len(spec) > 3 else "REG_DWORD")
    save_tweak_snapshot(task_id, data)


# ---- prior SEC-001: code-owned per-task registry allowlist ------------------------------
#
# The tweak snapshot lives in USER-WRITABLE config.json and is replayed
# into the registry while ELEVATED. Until now the only check on those
# persisted (hive, path, name) triples was a DENYLIST of persistence
# locations, so anything not on that list was permitted - a tampered
# record could name any non-persistence HKCU/HKLM/HKCR target.
#
# This table is CODE-OWNED and is the primary control: a snapshot spec is
# honoured only if the task itself is defined to touch that location.
# It is derived from the same spec lists the apply path passes, and
# tools/verify_sec001_task_allowlist.py recomputes it from this source
# and fails on drift, so a new tweak location cannot slip in undeclared.
#
# SCOPE, MEASURED - THIS FINDING IS NOT CLOSED. 40 of ~65 registry-backed
# tweaks are listed here. The rest still rely on the denylist alone:
#   * 6 build their locations dynamically and need PREFIX rules, not
#     exact triples: disable_nagle (per-interface GUID under
#     ...\Services\Tcpip\Parameters\Interfaces\), ssd_prefetch,
#     stop_windows_ads, disable_sticky_keys, prefer_ipv4, taskbar_cleanup
#   * the NVIDIA telemetry / shader-cache / last-access / Explorer tasks
#     write via reg_set_value_checked without a snapshot list
#   * ssd_trim, eee_disable, disk_timeout, max_cpu_power and friends use
#     other helpers entirely and were not enumerated here
# Those are tracked in references/FINDINGS_LEDGER.txt under SEC-001.
_TASK_REG_ALLOWLIST = {
    "activity_history_disable": (
        ("HKLM", "SOFTWARE\\Policies\\Microsoft\\Windows\\System", "EnableActivityFeed"),
        ("HKLM", "SOFTWARE\\Policies\\Microsoft\\Windows\\System", "PublishUserActivities"),
        ("HKLM", "SOFTWARE\\Policies\\Microsoft\\Windows\\System", "UploadUserActivities"),
    ),
    "ads_master_off": (
        ("HKCU", "Software\\Microsoft\\Windows\\CurrentVersion\\ContentDeliveryManager", "ContentDeliveryAllowed"),
        ("HKCU", "Software\\Microsoft\\Windows\\CurrentVersion\\ContentDeliveryManager", "FeatureManagementEnabled"),
        ("HKCU", "Software\\Microsoft\\Windows\\CurrentVersion\\ContentDeliveryManager", "SubscribedContentEnabled"),
    ),
    "aero_shake": (
        ("HKCU", "Software\\Microsoft\\Windows\\CurrentVersion\\Explorer\\Advanced", "DisallowShaking"),
    ),
    "alt_tab_windows_only": (
        ("HKCU", "Software\\Microsoft\\Windows\\CurrentVersion\\Explorer\\Advanced", "MultiTaskingAltTabFilter"),
    ),
    "autoplay_off": (
        ("HKCU", "Software\\Microsoft\\Windows\\CurrentVersion\\Explorer\\AutoplayHandlers", "DisableAutoplay"),
        ("HKCU", "Software\\Microsoft\\Windows\\CurrentVersion\\Policies\\Explorer", "NoDriveTypeAutoRun"),
    ),
    "background_apps": (
        ("HKCU", "Software\\Microsoft\\Windows\\CurrentVersion\\BackgroundAccessApplications", "GlobalUserDisabled"),
    ),
    "classic_context_menu": (
        ("HKCU", "Software\\Classes\\CLSID\\{86ca1aa0-34aa-4e8b-a509-50c905bae2a2}\\InprocServer32", ""),
    ),
    "classic_shortcut_icons": (
        ("HKCU", "Software\\Microsoft\\Windows\\CurrentVersion\\Explorer", "link"),
        ("HKLM", "SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\Explorer\\Shell Icons", "29"),
    ),
    "clipboard_sync_off": (
        ("HKLM", "SOFTWARE\\Policies\\Microsoft\\Windows\\System", "AllowCrossDeviceClipboard"),
    ),
    "clock_seconds": (
        ("HKCU", "Software\\Microsoft\\Windows\\CurrentVersion\\Explorer\\Advanced", "ShowSecondsInSystemClock"),
    ),
    "consumer_features": (
        ("HKLM", "SOFTWARE\\Policies\\Microsoft\\Windows\\CloudContent", "DisableWindowsConsumerFeatures"),
    ),
    "dark_mode": (
        ("HKCU", "Software\\Microsoft\\Windows\\CurrentVersion\\Themes\\Personalize", "AppsUseLightTheme"),
    ),
    "device_search_history_off": (
        ("HKCU", "Software\\Microsoft\\Windows\\CurrentVersion\\SearchSettings", "IsDeviceSearchHistoryEnabled"),
    ),
    "disable_fast_startup": (
        ("HKLM", "SYSTEM\\CurrentControlSet\\Control\\Session Manager\\Power", "HiberbootEnabled"),
    ),
    "disable_game_dvr": (
        ("HKCU", "Software\\Microsoft\\Windows\\CurrentVersion\\GameDVR", "AppCaptureEnabled"),
        ("HKCU", "System\\GameConfigStore", "GameDVR_Enabled"),
        ("HKLM", "SOFTWARE\\Policies\\Microsoft\\Windows\\GameDVR", "AllowGameDVR"),
    ),
    "disable_sticky_keys": (
        ("HKCU", "Control Panel\\Accessibility\\Keyboard Response", "Flags"),
        ("HKCU", "Control Panel\\Accessibility\\StickyKeys", "Flags"),
        ("HKCU", "Control Panel\\Accessibility\\ToggleKeys", "Flags"),
    ),
    "discord_no_ducking": (
        ("HKCU", "Software\\Microsoft\\Multimedia\\Audio", "UserDuckingPreference"),
    ),
    "edge_preload": (
        ("HKLM", "SOFTWARE\\Policies\\Microsoft\\Edge", "BackgroundModeEnabled"),
        ("HKLM", "SOFTWARE\\Policies\\Microsoft\\Edge", "StartupBoostEnabled"),
    ),
    "fast_app_close": (
        ("HKCU", "Control Panel\\Desktop", "AutoEndTasks"),
        ("HKCU", "Control Panel\\Desktop", "HungAppTimeout"),
        ("HKCU", "Control Panel\\Desktop", "WaitToKillAppTimeout"),
    ),
    "file_extensions": (
        ("HKCU", "Software\\Microsoft\\Windows\\CurrentVersion\\Explorer\\Advanced", "Hidden"),
        ("HKCU", "Software\\Microsoft\\Windows\\CurrentVersion\\Explorer\\Advanced", "HideFileExt"),
    ),
    "fullscreen_opt": (
        ("HKCU", "Software\\Microsoft\\Windows NT\\CurrentVersion\\AppCompatFlags\\Layers", "DISABLEDXMAXIMIZEDWINDOWEDMODE"),
    ),
    "game_mode": (
        ("HKCU", "Software\\Microsoft\\GameBar", "AllowAutoGameMode"),
        ("HKCU", "Software\\Microsoft\\GameBar", "AutoGameModeEnabled"),
    ),
    "games_priority": (
        ("HKLM", "SOFTWARE\\Microsoft\\Windows NT\\CurrentVersion\\Multimedia\\SystemProfile\\Tasks\\Games", "GPU Priority"),
        ("HKLM", "SOFTWARE\\Microsoft\\Windows NT\\CurrentVersion\\Multimedia\\SystemProfile\\Tasks\\Games", "Priority"),
        ("HKLM", "SOFTWARE\\Microsoft\\Windows NT\\CurrentVersion\\Multimedia\\SystemProfile\\Tasks\\Games", "SFIO Priority"),
        ("HKLM", "SOFTWARE\\Microsoft\\Windows NT\\CurrentVersion\\Multimedia\\SystemProfile\\Tasks\\Games", "Scheduling Category"),
    ),
    "hags": (
        ("HKLM", "SYSTEM\\CurrentControlSet\\Control\\GraphicsDrivers", "HwSchMode"),
    ),
    "hide_recent": (
        ("HKCU", "Software\\Microsoft\\Windows\\CurrentVersion\\Explorer", "ShowFrequent"),
        ("HKCU", "Software\\Microsoft\\Windows\\CurrentVersion\\Explorer", "ShowRecent"),
    ),
    "instant_alt_tab": (
        ("HKCU", "Control Panel\\Desktop", "ForegroundFlashCount"),
        ("HKCU", "Control Panel\\Desktop", "ForegroundLockTimeout"),
    ),
    "keyboard_tuning": (
        ("HKCU", "Control Panel\\Keyboard", "KeyboardDelay"),
        ("HKCU", "Control Panel\\Keyboard", "KeyboardSpeed"),
    ),
    "limit_telemetry": (
        ("HKLM", "SOFTWARE\\Policies\\Microsoft\\Windows\\DataCollection", "AllowTelemetry"),
    ),
    "local_search": (
        ("HKCU", "Software\\Microsoft\\Windows\\CurrentVersion\\Search", "BingSearchEnabled"),
        ("HKCU", "Software\\Microsoft\\Windows\\CurrentVersion\\Search", "CortanaConsent"),
        ("HKCU", "Software\\Microsoft\\Windows\\CurrentVersion\\SearchSettings", "IsAADCloudSearchEnabled"),
        ("HKCU", "Software\\Microsoft\\Windows\\CurrentVersion\\SearchSettings", "IsMSACloudSearchEnabled"),
        ("HKCU", "Software\\Policies\\Microsoft\\Windows\\Explorer", "DisableSearchBoxSuggestions"),
        ("HKLM", "SOFTWARE\\Policies\\Microsoft\\Windows\\Explorer", "DisableSearchBoxSuggestions"),
    ),
    "location_tracking": (
        ("HKLM", "SOFTWARE\\Policies\\Microsoft\\Windows\\LocationAndSensors", "DisableLocation"),
    ),
    "lock_screen": (
        ("HKLM", "SOFTWARE\\Policies\\Microsoft\\Windows\\Personalization", "NoLockScreen"),
    ),
    "long_paths": (
        ("HKLM", "SYSTEM\\CurrentControlSet\\Control\\FileSystem", "LongPathsEnabled"),
    ),
    "max_performance_gpu_pmz": (
        ("HKCU", "Software\\NVIDIA Corporation\\Global\\FTS", "PowerMizerEnable"),
    ),
    "menu_delay": (
        ("HKCU", "Control Panel\\Desktop", "MenuShowDelay"),
    ),
    "mic_privacy_allow": (
        ("HKCU", "Software\\Microsoft\\Windows\\CurrentVersion\\CapabilityAccessManager\\ConsentStore\\microphone", "Value"),
    ),
    "mouse_accel": (
        ("HKCU", "Control Panel\\Mouse", "MouseSpeed"),
        ("HKCU", "Control Panel\\Mouse", "MouseThreshold1"),
        ("HKCU", "Control Panel\\Mouse", "MouseThreshold2"),
    ),
    "mpo_fix": (
        ("HKLM", "SOFTWARE\\Microsoft\\Windows\\Dwm", "OverlayTestMode"),
    ),
    "network_throttling": (
        ("HKLM", "SOFTWARE\\Microsoft\\Windows NT\\CurrentVersion\\Multimedia\\SystemProfile", "NetworkThrottlingIndex"),
        ("HKLM", "SOFTWARE\\Microsoft\\Windows NT\\CurrentVersion\\Multimedia\\SystemProfile", "SystemResponsiveness"),
    ),
    "no_update_reboot": (
        ("HKLM", "SOFTWARE\\Policies\\Microsoft\\Windows\\WindowsUpdate\\AU", "NoAutoRebootWithLoggedOnUsers"),
    ),
    "numlock_boot": (
        ("HKCU", "Control Panel\\Desktop", "InitialKeyboardIndicators"),
    ),
    "power_throttling_off": (
        ("HKLM", "SYSTEM\\CurrentControlSet\\Control\\Power\\PowerThrottling", "PowerThrottlingOff"),
    ),
    "prefer_ipv4": (
        ("HKLM", "SYSTEM\\CurrentControlSet\\Services\\Tcpip6\\Parameters", "DisabledComponents"),
    ),
    "priority_separation": (
        ("HKLM", "SYSTEM\\CurrentControlSet\\Control\\PriorityControl", "Win32PrioritySeparation"),
    ),
    "privacy_baseline": (
        ("HKCU", "Control Panel\\International\\User Profile", "HttpAcceptLanguageOptOut"),
        ("HKCU", "Software\\Microsoft\\InputPersonalization\\TrainedDataStore", "HarvestContacts"),
        ("HKCU", "Software\\Microsoft\\Input\\TIPC", "Enabled"),
        ("HKCU", "Software\\Microsoft\\Personalization\\Settings", "AcceptedPrivacyPolicy"),
        ("HKCU", "Software\\Microsoft\\Siuf\\Rules", "NumberOfSIUFInPeriod"),
        ("HKCU", "Software\\Microsoft\\Windows\\CurrentVersion\\AdvertisingInfo", "Enabled"),
        ("HKCU", "Software\\Microsoft\\Windows\\CurrentVersion\\CapabilityAccessManager\\ConsentStore\\humaninterfaceenterprise", "Value"),
        ("HKCU", "Software\\Microsoft\\Windows\\CurrentVersion\\Explorer\\Advanced", "Start_TrackProgs"),
        ("HKCU", "Software\\Microsoft\\Windows\\CurrentVersion\\OnlineSpeechPrivacy", "HasAccepted"),
        ("HKCU", "Software\\Microsoft\\Windows\\CurrentVersion\\Privacy", "TailoredExperiencesWithDiagnosticDataEnabled"),
        ("HKLM", "SOFTWARE\\Policies\\Microsoft\\Input\\TIPC", "Enabled"),
        ("HKLM", "SOFTWARE\\Policies\\Microsoft\\Windows\\System", "EnableActivityFeed"),
    ),
    "remote_assist": (
        ("HKLM", "SYSTEM\\CurrentControlSet\\Control\\Terminal Server", "fAllowToGetHelp"),
    ),
    "snap_flyout_off": (
        ("HKCU", "Software\\Microsoft\\Windows\\CurrentVersion\\Explorer\\Advanced", "EnableSnapAssistFlyout"),
    ),
    "ssd_prefetch": (
        ("HKLM", "SYSTEM\\CurrentControlSet\\Control\\Session Manager\\Memory Management\\PrefetchParameters", "EnablePrefetcher"),
        ("HKLM", "SYSTEM\\CurrentControlSet\\Control\\Session Manager\\Memory Management\\PrefetchParameters", "EnableSuperfetch"),
    ),
    "startup_delay": (
        ("HKCU", "Software\\Microsoft\\Windows\\CurrentVersion\\Explorer\\Serialize", "StartupDelayInMSec"),
    ),
    "stop_windows_ads": (
        ("HKCU", "Software\\Microsoft\\Windows\\CurrentVersion\\ContentDeliveryManager", "OemPreInstalledAppsEnabled"),
        ("HKCU", "Software\\Microsoft\\Windows\\CurrentVersion\\ContentDeliveryManager", "PreInstalledAppsEnabled"),
        ("HKCU", "Software\\Microsoft\\Windows\\CurrentVersion\\ContentDeliveryManager", "RotatingLockScreenOverlayEnabled"),
        ("HKCU", "Software\\Microsoft\\Windows\\CurrentVersion\\ContentDeliveryManager", "SilentInstalledAppsEnabled"),
        ("HKCU", "Software\\Microsoft\\Windows\\CurrentVersion\\ContentDeliveryManager", "SoftLandingEnabled"),
        ("HKCU", "Software\\Microsoft\\Windows\\CurrentVersion\\ContentDeliveryManager", "SubscribedContent-310093Enabled"),
        ("HKCU", "Software\\Microsoft\\Windows\\CurrentVersion\\ContentDeliveryManager", "SubscribedContent-338387Enabled"),
        ("HKCU", "Software\\Microsoft\\Windows\\CurrentVersion\\ContentDeliveryManager", "SubscribedContent-338388Enabled"),
        ("HKCU", "Software\\Microsoft\\Windows\\CurrentVersion\\ContentDeliveryManager", "SubscribedContent-338389Enabled"),
        ("HKCU", "Software\\Microsoft\\Windows\\CurrentVersion\\ContentDeliveryManager", "SubscribedContent-338393Enabled"),
        ("HKCU", "Software\\Microsoft\\Windows\\CurrentVersion\\ContentDeliveryManager", "SubscribedContent-353694Enabled"),
        ("HKCU", "Software\\Microsoft\\Windows\\CurrentVersion\\ContentDeliveryManager", "SubscribedContent-353696Enabled"),
        ("HKCU", "Software\\Microsoft\\Windows\\CurrentVersion\\ContentDeliveryManager", "SubscribedContent-353698Enabled"),
        ("HKCU", "Software\\Microsoft\\Windows\\CurrentVersion\\ContentDeliveryManager", "SystemPaneSuggestionsEnabled"),
        ("HKCU", "Software\\Microsoft\\Windows\\CurrentVersion\\UserProfileEngagement", "ScoobeSystemSettingEnabled"),
        ("HKLM", "SOFTWARE\\Policies\\Microsoft\\Windows\\CloudContent", "DisableCloudOptimizedContent"),
        ("HKLM", "SOFTWARE\\Policies\\Microsoft\\Windows\\CloudContent", "DisableSoftLanding"),
    ),
    "storage_sense": (
        ("HKCU", "Software\\Microsoft\\Windows\\CurrentVersion\\StorageSense\\Parameters\\StoragePolicy", "01"),
        ("HKCU", "Software\\Microsoft\\Windows\\CurrentVersion\\StorageSense\\Parameters\\StoragePolicy", "04"),
        ("HKCU", "Software\\Microsoft\\Windows\\CurrentVersion\\StorageSense\\Parameters\\StoragePolicy", "256"),
    ),
    "suppress_crash_popups": (
        ("HKCU", "Software\\Microsoft\\Windows\\Windows Error Reporting", "DontShowUI"),
    ),
    "taskbar_endtask": (
        ("HKCU", "Software\\Microsoft\\Windows\\CurrentVersion\\Explorer\\Advanced\\TaskbarDeveloperSettings", "TaskbarEndTask"),
    ),
    "taskbar_left": (
        ("HKCU", "Software\\Microsoft\\Windows\\CurrentVersion\\Explorer\\Advanced", "TaskbarAl"),
    ),
    "taskbar_never_combine": (
        ("HKCU", "Software\\Microsoft\\Windows\\CurrentVersion\\Explorer\\Advanced", "TaskbarGlomLevel"),
    ),
    "tdr_delay": (
        ("HKLM", "SYSTEM\\CurrentControlSet\\Control\\GraphicsDrivers", "TdrDelay"),
    ),
    "tweak_delivery_optimization": (
        ("HKLM", "SOFTWARE\\Policies\\Microsoft\\Windows\\DeliveryOptimization", "DODownloadMode"),
    ),
    "verbose_boot": (
        ("HKLM", "SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\Policies\\System", "VerboseStatus"),
    ),
    "visual_effects": (
        ("HKCU", "Control Panel\\Desktop", "MenuShowDelay"),
    ),
    "widgets_board_off": (
        ("HKCU", "Software\\Microsoft\\Windows\\CurrentVersion\\Feeds", "ShellFeedsTaskbarViewMode"),
        ("HKLM", "SOFTWARE\\Policies\\Microsoft\\Dsh", "AllowNewsAndInterests"),
    ),
    "windowed_optimize": (
        ("HKCU", "System\\GameConfigStore", "GameDVR_DXGIHonorFSEWindowsCompatible"),
        ("HKCU", "System\\GameConfigStore", "GameDVR_FSEBehaviorMode"),
        ("HKCU", "System\\GameConfigStore", "GameDVR_HonorUserFSEBehaviorMode"),
    ),
}


# ---- prior SEC-001: entries the source-derivation cannot reach

# taskbar_cleanup builds its spec list from FUNCTION-LOCAL variables
#
#   writes = [("HKCU", adv, "TaskbarDa", 0, True), ...]
#   active = [w for w in writes if not (w[4] and not is_win11)]
#   _snap_reg_values(ctx, "taskbar_cleanup", [(h, p, n) for h, p, n, _v, _w in active])
#
# so it is invisible to a resolver that walks module-level constants, and it is
# filtered by Windows version - honouring that filter would make a derived table
# depend on the machine it was generated on. These five are transcribed by hand,
# which is precisely what the drift guard exists to make safe, so they are
# declared SEPARATELY and the guard checks each appears literally in the owning
# apply function. Its automatic derivation is NOT weakened: it still proves
# completeness for every task it can derive.
#
# The list is the UNFILTERED superset. A snapshot can only ever hold a subset of
# what apply wrote, so allowlisting the superset grants nothing extra.
_TASK_REG_ALLOWLIST_MANUAL = {
    "taskbar_cleanup": (
        ("HKCU", r"Software\Microsoft\Windows\CurrentVersion\Explorer\Advanced", "TaskbarDa"),
        ("HKCU", r"Software\Microsoft\Windows\CurrentVersion\Explorer\Advanced", "TaskbarMn"),
        ("HKCU", r"Software\Microsoft\Windows\CurrentVersion\Explorer\Advanced", "ShowSyncProviderNotifications"),
        ("HKCU", r"Software\Microsoft\Windows\CurrentVersion\SearchSettings", "IsDynamicSearchBoxEnabled"),
        ("HKLM", r"Software\Microsoft\Windows\CurrentVersion\Policies\Explorer", "HideSCAMeetNow"),
    ),
}


# ---- prior SEC-001: PREFIX rules for tweaks whose key path is built at run time

# Most tweaks name a fixed registry path, so _TASK_REG_ALLOWLIST above can hold
# exact (hive, path, name) triples. A few cannot, and this table is how they are
# expressed.
#
# disable_nagle is the case that forced it: it enumerates network interfaces and
# writes a DIFFERENT subkey per interface GUID, under
#   SYSTEM\CurrentControlSet\Services\Tcpip\Parameters\Interfaces\{GUID}
# for the values TcpAckFrequency and TCPNoDelay. There is no fixed path to
# allowlist, and hardcoding today's GUIDs would be both wrong and useless.
#
# A prefix entry authorises (hive, path, name) when the normalised path starts
# with the normalised prefix AND the value name matches. Both halves matter:
# the prefix alone would also authorise a sibling value under the same key, and
# the name alone would authorise any path.
_TASK_REG_ALLOW_PREFIX = {
    "disable_nagle": (
        ("HKLM",
         r"SYSTEM\CurrentControlSet\Services\Tcpip\Parameters\Interfaces",
         "TcpAckFrequency"),
        ("HKLM",
         r"SYSTEM\CurrentControlSet\Services\Tcpip\Parameters\Interfaces",
         "TCPNoDelay"),
    ),
}

_TASK_REG_ALLOW_PREFIX_IDS = frozenset(_TASK_REG_ALLOW_PREFIX)


def _norm_reg_spec(hive, path, name):
    """Canonical form of a registry spec, for allowlist matching only.

    prior SEC-001's verification plan explicitly calls out ALTERNATE PATH
    SPELLINGS, so matching has to be case- and separator-insensitive:
    "software\\microsoft", "SOFTWARE\\Microsoft\\", "SOFTWARE\\\\Microsoft" and
    a trailing backslash must all collapse to the same key, or the allowlist
    could be walked straight past by a tampered record.

    Only ever used to COMPARE. The value handed to the registry API is the
    original, untouched one.
    """
    def _n(s):
        s = str(s or "").replace("/", "\\")
        while "\\\\" in s:
            s = s.replace("\\\\", "\\")
        # Lexically resolve "." and ".." BEFORE comparing. Without this, a
        # tampered spec could spell
        #   ...\Tcpip\Parameters\Interfaces\..\..\..\evil
        # which still *starts with* the Interfaces prefix and so passed the
        # prefix rule, while Windows would resolve it to a completely different
        # key. Purely lexical on purpose: the key need not exist to be compared.
        parts = []
        for seg in s.strip("\\").split("\\"):
            if seg in ("", "."):
                continue
            if seg == "..":
                if parts:
                    parts.pop()
                continue
            parts.append(seg)
        return "\\".join(parts).casefold()
    return (_n(hive), _n(path), _n(name))


# Pre-normalised form of _TASK_REG_ALLOWLIST, built once at import.
# This is a dict of per-task sets, NOT one flat union across all tasks. A flat
# union looks equivalent and is not: it would let `hags` "own"
# Control Panel\Mouse\MouseSpeed just because `mouse_accel` writes it, which
# is precisely the cross-task confusion the report forbids ("match the
# persisted snapshot to the exact expected task").
# Derived entries come from the source-verified table; the hand-transcribed
# ones are merged in afterwards, and both are enforced identically.
_TASK_REG_ALLOWED_NORM = {}
for _task_id, _specs in _TASK_REG_ALLOWLIST.items():
    _TASK_REG_ALLOWED_NORM[_task_id] = frozenset(
        _norm_reg_spec(*_s) for _s in _specs)
for _task_id, _specs in _TASK_REG_ALLOWLIST_MANUAL.items():
    _TASK_REG_ALLOWED_NORM[_task_id] = frozenset(
        _norm_reg_spec(*_s) for _s in _specs)
_TASK_REG_ALLOWLIST_IDS = frozenset(_TASK_REG_ALLOWED_NORM)


# Normalised prefix table, built AFTER _norm_reg_spec exists (it is used above,
# so it must not be computed inline at definition time).
_TASK_REG_ALLOW_PREFIX_NORM = {
    _task_id: tuple(_norm_reg_spec(h, p, n) for h, p, n in _entries)
    for _task_id, _entries in _TASK_REG_ALLOW_PREFIX.items()
}

_TASK_REG_ALLOW_PREFIX_IDS = frozenset(_TASK_REG_ALLOW_PREFIX)


def _spec_allowed_for_task(task_id, hive, path, name):
    """True if `task_id` is code-defined to write exactly (hive, path, name).

    A task with NO allowlist entry returns True here and is governed by the
    denylist alone. That is a documented, measured gap - see the scope note on
    _TASK_REG_ALLOWLIST - and it is deliberately not fail-closed, because
    refusing every unmapped task would break Undo for them outright, which is
    the MED-007 regression (an Undo that can never run) this table exists to
    make impossible. Narrowing the gap is additive: each task added here moves
    from denylist-only to allowlist without touching the others.
    """
    allowed = _TASK_REG_ALLOWED_NORM.get(task_id)
    if allowed is None:
        prefixes = _TASK_REG_ALLOW_PREFIX_NORM.get(task_id)
        if prefixes is None:
            return True          # unmapped task: denylist only (documented gap)
        _h, _p, _n = _norm_reg_spec(hive, path, name)
        for _ph, _pp, _pn in prefixes:
            # The prefix is stored without a trailing separator, and the
            # candidate is compared on SEGMENT boundaries so that
            # "...\InterfacesXyz" cannot pass as "...\Interfaces".
            if _h == _ph and _n == _pn and (_p == _pp or _p.startswith(_pp + "\\")):
                return True
        return False
    return _norm_reg_spec(hive, path, name) in allowed


def _restore_reg_values(ctx: TaskContext, task_id: str, value_type: str = "REG_DWORD"):
    """Restore a snapshot taken by _snap_reg_values (exact prior values;
    absent values are deleted back to Windows defaults). Per-value stored
    types win; `value_type` is the fallback for pre-fix snapshots.

    F15: snapshots live in user-writable config.json, so they are treated as
    UNTRUSTED DATA, never as authority. Two independent gates apply, and both
    must pass:

      1. prior SEC-001 - the per-task ALLOWLIST. `_spec_allowed_for_task`
         requires the persisted (hive, path, name) to be a location the task
         itself is code-defined to write. This is the primary control: it is
         what stops a tampered record naming an arbitrary non-persistence
         HKCU/HKLM/HKCR target. Matching normalises case and separators, so an
         alternate spelling cannot walk past it.
      2. the historical DENYLIST of persistence locations, retained underneath
         as defence in depth.

    Known, measured scope gap: the allowlist covers 40 of ~65 registry-backed
    tweaks. The rest (dynamic-path tasks such as disable_nagle, and tasks that
    write without a snapshot list) are still on the denylist alone. See
    _TASK_REG_ALLOWLIST and references/FINDINGS_LEDGER.txt.

    Returns the number of values actually put back (0 when there was no
    snapshot). BUG-017: the old version returned None, so a caller had no way
    to tell "restored everything" from "did nothing", and revert_taskbar_cleanup
    printed "Taskbar items restored." even when every write had failed. A
    value skipped as `denied` does NOT count — it was deliberately left alone.
    Any real write/delete failure still raises, as before.
    """
    from app.config_persist import get_tweak_snapshot, clear_tweak_snapshot
    snap = get_tweak_snapshot(task_id)
    if not snap or "specs" not in snap:
        ctx.log("  (no snapshot found — nothing to restore)")
        return 0
    _SENSITIVE = ("\\run", "\\runonce", "\\services", "winlogon",
                  "image file execution options", "appinit_dlls", "userinit",
                  "\\lsa", "\\sam", "credential")

    def _is_persistence_location(path: str) -> bool:
        """True if `path` is somewhere a snapshot must never be replayed into.

        The substring list above blocks "\\services" outright, because
        Services\\<name>\\Start and Services\\<name>\\ImagePath are genuine
        persistence: restoring a value there can make Windows launch something.

        But that substring also matches Services\\<name>\\Parameters\\..., which
        is where a service's ordinary CONFIGURATION lives. disable_nagle writes
        TcpAckFrequency / TCPNoDelay under
        SYSTEM\\CurrentControlSet\\Services\\Tcpip\\Parameters\\Interfaces\\<guid>
        - squarely a Parameters subtree, never a startup mechanism. Blocking it
        meant Undo for that tweak ALWAYS raised
        "Refusing to restore ... persistence location", i.e. the fix for
        MED-007 shipped a tweak whose Undo could not run at all.

        So the rule is narrowed to what is actually dangerous: a path directly
        under a service key (\\services\\<name>\\<value>) rather than inside its
        Parameters subtree. This does NOT weaken the guard for Start /
        ImagePath, which sit exactly one level under Services\\<name> and are
        still blocked. The full per-task exact-match table the docstring
        promises remains the right follow-up; this only removes a false
        positive that broke a legitimate restore.
        """
        low = "\\" + path.lower().strip("\\") + "\\"
        if "\\services\\" not in low:
            return False
        # Exempt only paths strictly INSIDE a service's Parameters subtree
        # (Services\<svc>\Parameters\<something>\...), never the Parameters key
        # itself: Services\<svc>\Parameters\ServiceDll is a load path for an
        # already-configured service and stays blocked. Tcpip's is two levels
        # deep, so disable_nagle is unaffected.
        idx = low.find("\\parameters\\")
        if idx >= 0 and not low.endswith("\\parameters\\"):
            return False
        return True

    for i, (hive, path, name) in enumerate(snap["specs"]):
        if hive not in ("HKCU", "HKLM", "HKCR") or not isinstance(path, str) or not path \
                or not isinstance(name, str) or not name or "\x00" in path or "\x00" in name:
            raise RuntimeError(f"Refusing to restore {task_id}: snapshot spec #{i} is malformed (config tampering?).")
        if not _spec_allowed_for_task(task_id, hive, path, name):
            raise RuntimeError(
                f"Refusing to restore {task_id}: the saved snapshot names "
                f"{hive}\\{path}\\{name}, which this tweak is not defined to "
                f"write. Treat the config as tampered.")
        if _is_persistence_location(path) or any(
                s in f"\\{path}\\".lower() + name.lower()
                for s in _SENSITIVE if s != "\\services"):
            raise RuntimeError(f"Refusing to restore {task_id}: snapshot target is a persistence location (config tampering?).")
        vtype = snap.get(f"{i}:type", value_type)
        if vtype not in ("REG_DWORD", "REG_SZ", "REG_QWORD", "REG_BINARY", "REG_MULTI_SZ", "REG_EXPAND_SZ"):
            raise RuntimeError(f"Refusing to restore {task_id}: bad value type (config tampering?).")
    restored = 0
    for i, (hive, path, name) in enumerate(snap["specs"]):
        if snap.get(f"{i}:denied"):
            # Prior existed but was unreadable — leave the current value in
            # place and say so (deleting it would destroy user data we never
            # saw; writing a guessed default would do the same). Not counted:
            # this value was NOT restored.
            ctx.log(f"  (kept {hive}\\{path}\\{name}: prior value was unreadable, leaving current)")
            continue
        if snap.get(f"{i}:present"):
            vtype = snap.get(f"{i}:type", value_type)
            _val = snap.get(f"{i}:value")
            # hex-encoded REG_BINARY priors (see _snap_reg_values)
            if isinstance(_val, dict) and isinstance(_val.get("__bytes_hex__"), str):
                try:
                    _val = bytes.fromhex(_val["__bytes_hex__"])
                except Exception:
                    raise RuntimeError(f"Refusing to restore {task_id}: corrupt snapshot value (config tampering?).")
            reg_set_value_checked(ctx, hive, path, name, _val, value_type=vtype)
            restored += 1
        else:
            if not reg_delete_value(ctx, hive, path, name):
                raise RuntimeError(f"Could not remove {hive}\\{path}\\{name} during revert.")
            restored += 1
    clear_tweak_snapshot(task_id)
    return restored


# --------------------------------------------------------------------------- #
# MED-007: snapshot-based apply/revert for the single-purpose registry tweaks
# --------------------------------------------------------------------------- #
# Fourteen of these tweaks used to Undo with a blind `reg_delete_value`, or by
# writing a hardcoded "Windows default" that was not the user's value. Both
# destroy a pre-existing different setting: a user who had already turned
# something off loses that choice, and one who had it on gets it silently
# changed. The class also falsifies the README promise that every tweak's Undo
# is a full, verified revert.
#
# These two helpers make the correct shape the easy one, so the remaining
# tweaks no longer have an excuse.


def _regn_apply(ctx: TaskContext, task_id: str, writes: "list[tuple]",
                error: str = "") -> None:
    """Snapshot every registry value in `writes`, then write them.

    `writes` is a list of (hive, path, name, value) or
    (hive, path, name, value, value_type).

    The snapshot is taken BEFORE the first write, and dropped again if any
    write fails - unless one already existed, in which case the older (true
    pre-tweak) record is kept. That is the BUG-015 ordering fix applied
    uniformly, so a failed apply can never leave an Undo record describing a
    change that never happened.
    """
    specs = [(w[0], w[1], w[2]) for w in writes]
    had_snapshot = bool(get_tweak_snapshot(task_id))
    _snap_reg_values(ctx, task_id, specs)
    try:
        for w in writes:
            vtype = w[4] if len(w) > 4 else "REG_DWORD"
            if not reg_set_value(ctx, w[0], w[1], w[2], w[3], value_type=vtype):
                raise RuntimeError(
                    error or "Could not write %s\\%s\\%s." % (w[0], w[1], w[2]))
    except Exception:
        if not had_snapshot:
            clear_tweak_snapshot(task_id)
        raise


def _regn_revert(ctx: TaskContext, task_id: str, label: str) -> int:
    """Undo a _regn_apply tweak by restoring what was actually there.

    Never blind-deletes and never substitutes a guessed default: a value that
    was absent before comes back absent, and a value the user had set comes
    back with the user's value. Raises when nothing could be restored, so a
    failed Undo is reported as the failure it is rather than logged as
    success.
    """
    had_snapshot = bool(get_tweak_snapshot(task_id))
    restored = _restore_reg_values(ctx, task_id)
    if restored <= 0:
        raise RuntimeError(
            "%s Undo could not restore anything%s. Nothing was changed."
            % (label,
               "" if had_snapshot else
               " - no snapshot was recorded, so the prior value is unknown and"
               " guessing would overwrite a setting you may have had")
        )
    ctx.log("%s reverted to its previous setting." % label)
    return restored


_ADV = "Software\\Microsoft\\Windows\\CurrentVersion\\Explorer\\Advanced"


def apply_file_extensions(ctx: TaskContext):
    """Show file extensions + hidden files (the modder's must-have)."""
    had_snapshot = bool(get_tweak_snapshot("file_extensions"))
    _snap_reg_values(ctx, "file_extensions", [("HKCU", _ADV, "HideFileExt"), ("HKCU", _ADV, "Hidden")])
    try:
        reg_set_value_checked(ctx, "HKCU", _ADV, "HideFileExt", 0)
        reg_set_value_checked(ctx, "HKCU", _ADV, "Hidden", 1)
    except Exception:
        if not had_snapshot:
            clear_tweak_snapshot("file_extensions")
        raise
    ctx.log("File extensions and hidden files are now shown.")

def revert_file_extensions(ctx: TaskContext):
    _restore_reg_values(ctx, "file_extensions")
    ctx.log("File visibility restored to its prior setting.")


def apply_menu_delay(ctx: TaskContext):
    """Menus pop in 100ms instead of 400ms."""
    had_snapshot = bool(get_tweak_snapshot("menu_delay"))
    _snap_reg_values(ctx, "menu_delay", [("HKCU", "Control Panel\\Desktop", "MenuShowDelay")])
    try:
        reg_set_value_checked(ctx, "HKCU", "Control Panel\\Desktop", "MenuShowDelay", "100", value_type="REG_SZ")
    except Exception:
        if not had_snapshot:
            clear_tweak_snapshot("menu_delay")
        raise
    ctx.log("Menu delay set to 100ms.")

def revert_menu_delay(ctx: TaskContext):
    from app.config_persist import get_tweak_snapshot, clear_tweak_snapshot
    snap = get_tweak_snapshot("menu_delay")
    if snap and snap.get("0:present"):
        reg_set_value_checked(ctx, "HKCU", "Control Panel\\Desktop", "MenuShowDelay",
                      snap.get("0:value"), value_type="REG_SZ")
    else:
        reg_delete_value(ctx, "HKCU", "Control Panel\\Desktop", "MenuShowDelay")
    clear_tweak_snapshot("menu_delay")
    ctx.log("Menu delay restored to its prior setting.")


def apply_aero_shake_off(ctx: TaskContext):
    """Disable shake-to-minimize (no more nuked desktop mid-game)."""
    had_snapshot = bool(get_tweak_snapshot("aero_shake"))
    _snap_reg_values(ctx, "aero_shake", [("HKCU", _ADV, "DisallowShaking")])
    try:
        reg_set_value_checked(ctx, "HKCU", _ADV, "DisallowShaking", 1)
    except Exception:
        if not had_snapshot:
            clear_tweak_snapshot("aero_shake")
        raise
    ctx.log("Shake-to-minimize disabled.")

def revert_aero_shake_off(ctx: TaskContext):
    _restore_reg_values(ctx, "aero_shake")
    ctx.log("Shake-to-minimize restored to its prior setting.")


def apply_lock_screen_off(ctx: TaskContext):
    """Skip the lock screen — straight to login (admin, policy key)."""
    had_snapshot = bool(get_tweak_snapshot("lock_screen"))
    _snap_reg_values(ctx, "lock_screen",
                     [("HKLM", "SOFTWARE\\Policies\\Microsoft\\Windows\\Personalization", "NoLockScreen")])
    try:
        reg_set_value_checked(ctx, "HKLM", "SOFTWARE\\Policies\\Microsoft\\Windows\\Personalization", "NoLockScreen", 1)
    except Exception:
        if not had_snapshot:
            clear_tweak_snapshot("lock_screen")
        raise
    ctx.log("Lock screen skipped (takes effect at next sign-in).")

def revert_lock_screen_off(ctx: TaskContext):
    _restore_reg_values(ctx, "lock_screen")
    ctx.log("Lock screen setting restored.")


def apply_edge_preload_off(ctx: TaskContext):
    """Stop Edge's startup boost + background mode (admin; Edge updates may
    re-add these — rerun if Edge gets chatty again)."""
    had_snapshot = bool(get_tweak_snapshot("edge_preload"))
    _snap_reg_values(ctx, "edge_preload",
                     [("HKLM", "SOFTWARE\\Policies\\Microsoft\\Edge", "StartupBoostEnabled"),
                      ("HKLM", "SOFTWARE\\Policies\\Microsoft\\Edge", "BackgroundModeEnabled")])
    try:
        reg_set_value_checked(ctx, "HKLM", "SOFTWARE\\Policies\\Microsoft\\Edge", "StartupBoostEnabled", 0)
        reg_set_value_checked(ctx, "HKLM", "SOFTWARE\\Policies\\Microsoft\\Edge", "BackgroundModeEnabled", 0)
    except Exception:
        if not had_snapshot:
            clear_tweak_snapshot("edge_preload")
        raise
    ctx.log("Edge preloading disabled.")

def revert_edge_preload_off(ctx: TaskContext):
    _restore_reg_values(ctx, "edge_preload")
    ctx.log("Edge preload setting restored.")


def apply_dark_mode(ctx: TaskContext):
    """Prefer dark app themes."""
    had_snapshot = bool(get_tweak_snapshot("dark_mode"))
    _snap_reg_values(ctx, "dark_mode",
                     [("HKCU", "Software\\Microsoft\\Windows\\CurrentVersion\\Themes\\Personalize", "AppsUseLightTheme")])
    try:
        reg_set_value_checked(ctx, "HKCU", "Software\\Microsoft\\Windows\\CurrentVersion\\Themes\\Personalize", "AppsUseLightTheme", 0)
    except Exception:
        if not had_snapshot:
            clear_tweak_snapshot("dark_mode")
        raise
    ctx.log("Dark app theme preferred.")

def revert_dark_mode(ctx: TaskContext):
    _restore_reg_values(ctx, "dark_mode")
    ctx.log("App theme restored to its prior setting.")


def apply_remote_assist_off(ctx: TaskContext):
    """Disable inbound Remote Assistance offers (admin)."""
    had_snapshot = bool(get_tweak_snapshot("remote_assist"))
    _snap_reg_values(ctx, "remote_assist",
                     [("HKLM", "SYSTEM\\CurrentControlSet\\Control\\Terminal Server", "fAllowToGetHelp")])
    try:
        reg_set_value_checked(ctx, "HKLM", "SYSTEM\\CurrentControlSet\\Control\\Terminal Server", "fAllowToGetHelp", 0)
    except Exception:
        if not had_snapshot:
            clear_tweak_snapshot("remote_assist")
        raise
    ctx.log("Remote Assistance disabled.")

def revert_remote_assist_off(ctx: TaskContext):
    _restore_reg_values(ctx, "remote_assist")
    ctx.log("Remote Assistance setting restored.")


def apply_verbose_boot(ctx: TaskContext):
    """Verbose boot/shutdown messages instead of the spinner (admin)."""
    had_snapshot = bool(get_tweak_snapshot("verbose_boot"))
    _snap_reg_values(ctx, "verbose_boot",
                     [("HKLM", "SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\Policies\\System", "VerboseStatus")])
    try:
        reg_set_value_checked(ctx, "HKLM", "SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\Policies\\System", "VerboseStatus", 1)
    except Exception:
        if not had_snapshot:
            clear_tweak_snapshot("verbose_boot")
        raise
    ctx.log("Verbose boot messages on (visible at next restart).")

def revert_verbose_boot(ctx: TaskContext):
    _restore_reg_values(ctx, "verbose_boot")
    ctx.log("Boot messages restored to normal.")


def apply_location_tracking_off(ctx: TaskContext):
    """Disable the location sensor via policy (admin)."""
    had_snapshot = bool(get_tweak_snapshot("location_tracking"))
    _snap_reg_values(ctx, "location_tracking",
                     [("HKLM", "SOFTWARE\\Policies\\Microsoft\\Windows\\LocationAndSensors", "DisableLocation")])
    try:
        reg_set_value_checked(ctx, "HKLM", "SOFTWARE\\Policies\\Microsoft\\Windows\\LocationAndSensors", "DisableLocation", 1)
    except Exception:
        if not had_snapshot:
            clear_tweak_snapshot("location_tracking")
        raise
    ctx.log("Location tracking disabled.")

def revert_location_tracking_off(ctx: TaskContext):
    _restore_reg_values(ctx, "location_tracking")
    ctx.log("Location setting restored.")


def apply_widgets_board_off(ctx: TaskContext):
    """Policy-level Widgets board off (goes further than hiding the taskbar
    icon: the board, news feed and its background activity stop entirely)."""
    had_snapshot = bool(get_tweak_snapshot("widgets_board_off"))
    _snap_reg_values(ctx, "widgets_board_off",
                     [("HKLM", "SOFTWARE\\Policies\\Microsoft\\Dsh", "AllowNewsAndInterests"),
                      ("HKCU", "Software\\Microsoft\\Windows\\CurrentVersion\\Feeds", "ShellFeedsTaskbarViewMode")])
    try:
        reg_set_value_checked(ctx, "HKLM", "SOFTWARE\\Policies\\Microsoft\\Dsh", "AllowNewsAndInterests", 0)
        reg_set_value_checked(ctx, "HKCU", "Software\\Microsoft\\Windows\\CurrentVersion\\Feeds", "ShellFeedsTaskbarViewMode", 2)
    except Exception:
        if not had_snapshot:
            clear_tweak_snapshot("widgets_board_off")
        raise
    ctx.log("Widgets board disabled (icon hide + news feed off).")

def revert_widgets_board_off(ctx: TaskContext):
    _restore_reg_values(ctx, "widgets_board_off")
    ctx.log("Widgets board restored.")


def apply_autoplay_off(ctx: TaskContext):
    """Disable AutoPlay/AutoRun for USB sticks and discs (plugging in a
    drive never auto-launches anything — classic USB-malware vector)."""
    had_snapshot = bool(get_tweak_snapshot("autoplay_off"))
    _snap_reg_values(ctx, "autoplay_off",
                     [("HKCU", "Software\\Microsoft\\Windows\\CurrentVersion\\Explorer\\AutoplayHandlers", "DisableAutoplay"),
                      ("HKCU", "Software\\Microsoft\\Windows\\CurrentVersion\\Policies\\Explorer", "NoDriveTypeAutoRun")])
    try:
        reg_set_value_checked(ctx, "HKCU", "Software\\Microsoft\\Windows\\CurrentVersion\\Explorer\\AutoplayHandlers", "DisableAutoplay", 1)
        reg_set_value_checked(ctx, "HKCU", "Software\\Microsoft\\Windows\\CurrentVersion\\Policies\\Explorer", "NoDriveTypeAutoRun", 255)
    except Exception:
        if not had_snapshot:
            clear_tweak_snapshot("autoplay_off")
        raise
    ctx.log("AutoPlay disabled for all drives.")

def revert_autoplay_off(ctx: TaskContext):
    _restore_reg_values(ctx, "autoplay_off")
    ctx.log("AutoPlay restored.")


def apply_snap_flyout_off(ctx: TaskContext):
    """Disable the Snap-layouts flyout that pops when hovering a window's
    maximize button mid-game (Win+arrows snapping keeps working)."""
    had_snapshot = bool(get_tweak_snapshot("snap_flyout_off"))
    _snap_reg_values(ctx, "snap_flyout_off",
                     [("HKCU", "Software\\Microsoft\\Windows\\CurrentVersion\\Explorer\\Advanced", "EnableSnapAssistFlyout")])
    try:
        reg_set_value_checked(ctx, "HKCU", "Software\\Microsoft\\Windows\\CurrentVersion\\Explorer\\Advanced", "EnableSnapAssistFlyout", 0)
    except Exception:
        if not had_snapshot:
            clear_tweak_snapshot("snap_flyout_off")
        raise
    ctx.log("Snap flyout off — maximize-hover no longer pops layouts.")

def revert_snap_flyout_off(ctx: TaskContext):
    _restore_reg_values(ctx, "snap_flyout_off")
    ctx.log("Snap flyout restored.")


_SENSE = ("HKCU",
          "Software\\Microsoft\\Windows\\CurrentVersion\\StorageSense"
          "\\Parameters\\StoragePolicy")

def apply_storage_sense(ctx: TaskContext):
    """Turn ON Windows' built-in Storage Sense (Settings > System >
    Storage) so junk gets cleaned automatically — the machine maintains
    itself between Cleaner Tool runs.

    Writes the documented StoragePolicy values (same ones the Settings
    app writes): '01'=Sense enabled, '04'=clean temp files, '2048'=clean
    recycle bin — with a monthly cadence ('256'=0x30 days, the default
    Windows picks). Deliberately does NOT touch Downloads cleanup
    ('08'/'512' family) — auto-deleting user Downloads can destroy
    someone's files; Sense keeps to temp/bin debris only.

    HKCU-only (per-user setting) — no admin needed, undoable."""
    had_snapshot = bool(get_tweak_snapshot("storage_sense"))
    _snap_reg_values(ctx, "storage_sense",
                     [(_SENSE[0], _SENSE[1], "01"),
                      (_SENSE[0], _SENSE[1], "04"),
                      (_SENSE[0], _SENSE[1], "256")])
    try:
        reg_set_value_checked(ctx, _SENSE[0], _SENSE[1], "01", 1)
        reg_set_value_checked(ctx, _SENSE[0], _SENSE[1], "04", 1)
        reg_set_value_checked(ctx, _SENSE[0], _SENSE[1], "256", 0)  # 0 = cadence picks default
    except Exception:
        if not had_snapshot:
            clear_tweak_snapshot("storage_sense")
        raise
    ctx.log("Storage Sense ON — Windows now auto-cleans temp junk (monthly, default cadence).")

def revert_storage_sense(ctx: TaskContext):
    _restore_reg_values(ctx, "storage_sense")
    ctx.log("Storage Sense restored to its prior setting.")


_DESKTOP = "Control Panel\\Desktop"


def apply_fast_app_close(ctx: TaskContext):
    """Fast shutdown: Windows waits up to 20 seconds (20000ms) for apps
    to close on their own before force-killing them — that is the
    'Shutting down…' hang after quitting a game. Cuts the waits:
      * WaitToKillAppTimeout  20000 -> 2000  (grace period per app)
      * HungAppTimeout        5000  -> 1000  (before 'not responding')
    Plus AutoEndTasks=1 so Windows actually ends them instead of
    sitting on the 'These apps are preventing shutdown' screen.

    HKCU-only, snapshot + revert, no admin. These values only take
    effect at the next sign-out/shutdown."""
    had_snapshot = bool(get_tweak_snapshot("fast_app_close"))
    _snap_reg_values(ctx, "fast_app_close",
                     [("HKCU", _DESKTOP, "WaitToKillAppTimeout"),
                      ("HKCU", _DESKTOP, "HungAppTimeout"),
                      ("HKCU", _DESKTOP, "AutoEndTasks")])
    try:
        reg_set_value_checked(ctx, "HKCU", _DESKTOP, "WaitToKillAppTimeout", "2000", value_type="REG_SZ")
        reg_set_value_checked(ctx, "HKCU", _DESKTOP, "HungAppTimeout", "1000", value_type="REG_SZ")
        reg_set_value_checked(ctx, "HKCU", _DESKTOP, "AutoEndTasks", "1", value_type="REG_SZ")
    except Exception:
        if not had_snapshot:
            clear_tweak_snapshot("fast_app_close")
        raise
    ctx.log("Fast app close on: shutdown waits drop from 20s to 2s before apps are ended.")

def revert_fast_app_close(ctx: TaskContext):
    from app.config_persist import get_tweak_snapshot, clear_tweak_snapshot
    snap = get_tweak_snapshot("fast_app_close")
    if not snap:
        ctx.log("  (no snapshot found — restoring Windows defaults)")
        reg_set_value_checked(ctx, "HKCU", _DESKTOP, "WaitToKillAppTimeout", "20000", value_type="REG_SZ")
        reg_set_value_checked(ctx, "HKCU", _DESKTOP, "HungAppTimeout", "5000", value_type="REG_SZ")
        reg_delete_value(ctx, "HKCU", _DESKTOP, "AutoEndTasks")
    else:
        _restore_reg_values(ctx, "fast_app_close", value_type="REG_SZ")
    clear_tweak_snapshot("fast_app_close")
    ctx.log("Shutdown waits restored.")


def apply_instant_alt_tab(ctx: TaskContext):
    """Instant window focus: Windows enforces a 200000ms (!) lockout
    during which a background window may NOT steal focus — that is the
    lag when alt-tabbing back into a fullscreen game (the click 'eats'
    for a moment, the shell feels sticky). Setting ForegroundLockTimeout
    to 0 removes the lockout delay so the switch is immediate. The
    official 'prevent focus stealing' config pairs this with
    ForegroundFlashCount=0 (no orange taskbar flashing fight).

    HKCU-only, snapshot + revert, no admin, takes effect at next
    sign-in (Explorer reads it at logon)."""
    had_snapshot = bool(get_tweak_snapshot("instant_alt_tab"))
    _snap_reg_values(ctx, "instant_alt_tab",
                     [("HKCU", _DESKTOP, "ForegroundLockTimeout"),
                      ("HKCU", _DESKTOP, "ForegroundFlashCount")])
    try:
        reg_set_value_checked(ctx, "HKCU", _DESKTOP, "ForegroundLockTimeout", 0)   # REG_DWORD needs an int, not "0"
        reg_set_value_checked(ctx, "HKCU", _DESKTOP, "ForegroundFlashCount", 0)
    except Exception:
        if not had_snapshot:
            clear_tweak_snapshot("instant_alt_tab")
        raise
    ctx.log("Alt-tab focus delay removed — windows switch instantly at next sign-in.")

def revert_instant_alt_tab(ctx: TaskContext):
    from app.config_persist import get_tweak_snapshot, clear_tweak_snapshot
    snap = get_tweak_snapshot("instant_alt_tab")
    if not snap:
        ctx.log("  (no snapshot found — restoring Windows defaults)")
        reg_set_value_checked(ctx, "HKCU", _DESKTOP, "ForegroundLockTimeout", 200000)
        reg_delete_value(ctx, "HKCU", _DESKTOP, "ForegroundFlashCount")
    else:
        _restore_reg_values(ctx, "instant_alt_tab")
    clear_tweak_snapshot("instant_alt_tab")
    ctx.log("Focus lockout restored.")


def apply_numlock_boot(ctx: TaskContext):
    """NumLock ON at every boot — for MMO/RPG players whose muscle memory
    expects the numpad. Windows remembers the numlock state per user,
    but fast startup, some BIOSes and certain keyboards reset it, and on
    fresh sign-ins it lands off. InitialKeyboardIndicators = "2" is the
    documented 'always on at logon' value (2147483648 variants exist for
    scroll lock combos; 2 is the standard numlock-only form).

    HKCU-only (per-user), REG_SZ, snapshot + revert, no admin.
    Takes effect at the next sign-in/boot."""
    had_snapshot = bool(get_tweak_snapshot("numlock_boot"))
    _snap_reg_values(ctx, "numlock_boot",
                     [("HKCU", _DESKTOP, "InitialKeyboardIndicators")])
    try:
        reg_set_value_checked(ctx, "HKCU", _DESKTOP, "InitialKeyboardIndicators", "2", value_type="REG_SZ")
    except Exception:
        if not had_snapshot:
            clear_tweak_snapshot("numlock_boot")
        raise
    ctx.log("NumLock will be ON at every boot from the next sign-in.")

def revert_numlock_boot(ctx: TaskContext):
    from app.config_persist import get_tweak_snapshot, clear_tweak_snapshot
    snap = get_tweak_snapshot("numlock_boot")
    if not snap:
        ctx.log("  (no snapshot found — restoring Windows default)")
        reg_set_value_checked(ctx, "HKCU", _DESKTOP, "InitialKeyboardIndicators", "2147483648", value_type="REG_SZ")
    else:
        _restore_reg_values(ctx, "numlock_boot", value_type="REG_SZ")
    clear_tweak_snapshot("numlock_boot")
    ctx.log("NumLock boot behavior restored.")


def apply_clipboard_sync_off(ctx: TaskContext):
    """Stop Windows from syncing your clipboard to the cloud and your
    phone: Win+V history is local, but with a Microsoft account the
    'Sync across devices' feature uploads copied text (and screenshots
    of it) to Microsoft servers. Privacy-relevant for anyone who copies
    passwords, keys or personal text. AllowCrossDeviceClipboard=1 is
    Microsoft's documented policy that disables the sync/cloud half
    while leaving local Win+V history working.

    HKLM policy key (admin), snapshot + revert."""
    had_snapshot = bool(get_tweak_snapshot("clipboard_sync_off"))
    _snap_reg_values(ctx, "clipboard_sync_off",
                     [("HKLM", "SOFTWARE\\Policies\\Microsoft\\Windows\\System",
                       "AllowCrossDeviceClipboard")])
    try:
        reg_set_value_checked(ctx, "HKLM", "SOFTWARE\\Policies\\Microsoft\\Windows\\System",
                              "AllowCrossDeviceClipboard", 1)
    except Exception:
        if not had_snapshot:
            clear_tweak_snapshot("clipboard_sync_off")
        raise
    ctx.log("Cloud clipboard sync disabled — Win+V keeps working, locally only.")

def revert_clipboard_sync_off(ctx: TaskContext):
    _restore_reg_values(ctx, "clipboard_sync_off")
    ctx.log("Cloud clipboard sync restored to its prior setting.")


_TaskbarDevSettings = "Software\\Microsoft\\Windows\\CurrentVersion\\Explorer\\Advanced\\TaskbarDeveloperSettings"


def apply_taskbar_endtask(ctx: TaskContext):
    """Right-click any taskbar icon to End Task — kill a frozen game in
    two clicks instead of opening Task Manager. Windows 11 23H2+ only
    (the value is inert on Win10); per-user, instant, no reboot."""
    had_snapshot = bool(get_tweak_snapshot("taskbar_endtask"))
    _snap_reg_values(ctx, "taskbar_endtask", [("HKCU", _TaskbarDevSettings, "TaskbarEndTask")])
    try:
        reg_set_value_checked(ctx, "HKCU", _TaskbarDevSettings, "TaskbarEndTask", 1)
    except Exception:
        if not had_snapshot:
            clear_tweak_snapshot("taskbar_endtask")
        raise
    ctx.log("Taskbar End Task on: right-click any app to force-close it.")

def revert_taskbar_endtask(ctx: TaskContext):
    _restore_reg_values(ctx, "taskbar_endtask")
    ctx.log("Taskbar End Task restored to its prior setting.")

def verify_taskbar_endtask() -> "bool | None":
    return _verify_value("HKCU", _TaskbarDevSettings, "TaskbarEndTask", 1)


_MIC_CONSENT = "Software\\Microsoft\\Windows\\CurrentVersion\\CapabilityAccessManager\\ConsentStore\\microphone"


def apply_mic_privacy_allow(ctx: TaskContext):
    """Let games and chat apps use the microphone again — flips the
    user-level mic privacy consent back to Allow (the classic 'nobody
    can hear me in Discord/VALORANT' fix when Windows revoked it).
    Per-user, snapshot + revert; app-level blocks are left alone."""
    had_snapshot = bool(get_tweak_snapshot("mic_privacy_allow"))
    _snap_reg_values(ctx, "mic_privacy_allow", [("HKCU", _MIC_CONSENT, "Value", "REG_SZ")])
    try:
        reg_set_value_checked(ctx, "HKCU", _MIC_CONSENT, "Value", "Allow", value_type="REG_SZ")
    except Exception:
        if not had_snapshot:
            clear_tweak_snapshot("mic_privacy_allow")
        raise
    ctx.log("Microphone access allowed for apps — check in-game voice settings too.")

def revert_mic_privacy_allow(ctx: TaskContext):
    _restore_reg_values(ctx, "mic_privacy_allow", value_type="REG_SZ")
    ctx.log("Microphone privacy consent restored to its prior setting.")

def verify_mic_privacy_allow() -> "bool | None":
    return _verify_value("HKCU", _MIC_CONSENT, "Value", "Allow")


def apply_clock_seconds(ctx: TaskContext):
    """Show seconds in the taskbar clock — handy for cooldown and queue
    timers. Per-user, instant, snapshot + revert."""
    had_snapshot = bool(get_tweak_snapshot("clock_seconds"))
    _snap_reg_values(ctx, "clock_seconds",
                     [("HKCU", _ADV, "ShowSecondsInSystemClock")])
    try:
        reg_set_value_checked(ctx, "HKCU", _ADV, "ShowSecondsInSystemClock", 1)
    except Exception:
        if not had_snapshot:
            clear_tweak_snapshot("clock_seconds")
        raise
    ctx.log("Taskbar clock now shows seconds.")

def revert_clock_seconds(ctx: TaskContext):
    _restore_reg_values(ctx, "clock_seconds")
    ctx.log("Taskbar clock restored to its prior setting.")

def verify_clock_seconds() -> "bool | None":
    return _verify_value("HKCU", _ADV, "ShowSecondsInSystemClock", 1)


def apply_taskbar_left(ctx: TaskContext):
    """Left-align the taskbar Start button (classic feel) instead of
    centered. Per-user, instant, snapshot + revert."""
    had_snapshot = bool(get_tweak_snapshot("taskbar_left"))
    _snap_reg_values(ctx, "taskbar_left", [("HKCU", _ADV, "TaskbarAl")])
    try:
        reg_set_value_checked(ctx, "HKCU", _ADV, "TaskbarAl", 0)
    except Exception:
        if not had_snapshot:
            clear_tweak_snapshot("taskbar_left")
        raise
    ctx.log("Taskbar aligned left — classic Start position.")

def revert_taskbar_left(ctx: TaskContext):
    _restore_reg_values(ctx, "taskbar_left")
    ctx.log("Taskbar alignment restored to its prior setting.")

def verify_taskbar_left() -> "bool | None":
    return _verify_value("HKCU", _ADV, "TaskbarAl", 0)


# --- Round 7 tasks (user request: Winhance-audit picks) --- #
# All snapshot + revert (+verify), Custom-only, presets untouched.


def apply_alt_tab_windows_only(ctx: TaskContext):
    """Alt+Tab flips between real windows and games only — no Microsoft
    Edge tabs crowding the switcher mid-game. Per-user, snapshot + revert."""
    had_snapshot = bool(get_tweak_snapshot("alt_tab_windows_only"))
    _snap_reg_values(ctx, "alt_tab_windows_only",
                     [("HKCU", _ADV, "MultiTaskingAltTabFilter")])
    try:
        reg_set_value_checked(ctx, "HKCU", _ADV, "MultiTaskingAltTabFilter", 3)
    except Exception:
        if not had_snapshot:
            clear_tweak_snapshot("alt_tab_windows_only")
        raise
    ctx.log("Alt+Tab now shows windows only — no browser tabs in the way.")

def revert_alt_tab_windows_only(ctx: TaskContext):
    _restore_reg_values(ctx, "alt_tab_windows_only")
    ctx.log("Alt+Tab filter restored to its prior setting.")

def verify_alt_tab_windows_only() -> "bool | None":
    return _verify_value("HKCU", _ADV, "MultiTaskingAltTabFilter", 3)


def apply_taskbar_never_combine(ctx: TaskContext):
    """Every open window keeps its own labeled taskbar button instead of
    piling up under one icon. Per-user, snapshot + revert (takes effect
    after Explorer restarts)."""
    had_snapshot = bool(get_tweak_snapshot("taskbar_never_combine"))
    _snap_reg_values(ctx, "taskbar_never_combine",
                     [("HKCU", _ADV, "TaskbarGlomLevel")])
    try:
        reg_set_value_checked(ctx, "HKCU", _ADV, "TaskbarGlomLevel", 2)
    except Exception:
        if not had_snapshot:
            clear_tweak_snapshot("taskbar_never_combine")
        raise
    ctx.log("Taskbar buttons never combine now (restart Explorer to see it).")

def revert_taskbar_never_combine(ctx: TaskContext):
    _restore_reg_values(ctx, "taskbar_never_combine")
    ctx.log("Taskbar combining restored to its prior setting.")

def verify_taskbar_never_combine() -> "bool | None":
    return _verify_value("HKCU", _ADV, "TaskbarGlomLevel", 2)


def apply_discord_no_ducking(ctx: TaskContext):
    """Discord calls stop auto-lowering game and media volume (Windows
    'ducking' off = do nothing). Per-user, snapshot + revert."""
    had_snapshot = bool(get_tweak_snapshot("discord_no_ducking"))
    _snap_reg_values(ctx, "discord_no_ducking",
                     [("HKCU", "Software\\Microsoft\\Multimedia\\Audio",
                       "UserDuckingPreference")])
    try:
        reg_set_value_checked(ctx, "HKCU", "Software\\Microsoft\\Multimedia\\Audio",
                              "UserDuckingPreference", 3)
    except Exception:
        if not had_snapshot:
            clear_tweak_snapshot("discord_no_ducking")
        raise
    ctx.log("Windows no longer ducks game audio during voice calls.")

def revert_discord_no_ducking(ctx: TaskContext):
    _restore_reg_values(ctx, "discord_no_ducking")
    ctx.log("Audio ducking preference restored to its prior setting.")

def verify_discord_no_ducking() -> "bool | None":
    return _verify_value("HKCU", "Software\\Microsoft\\Multimedia\\Audio",
                         "UserDuckingPreference", 3)


_CDM_POL = "Software\\Microsoft\\Windows\\CurrentVersion\\ContentDeliveryManager"


def apply_ads_master_off(ctx: TaskContext):
    """One master flick for the whole suggestions/ads engine (the three
    ContentDelivery master switches off). Sits above the per-value ad
    sweep; per-user, snapshot + revert."""
    had_snapshot = bool(get_tweak_snapshot("ads_master_off"))
    _snap_reg_values(ctx, "ads_master_off",
                     [("HKCU", _CDM_POL, "ContentDeliveryAllowed"),
                      ("HKCU", _CDM_POL, "SubscribedContentEnabled"),
                      ("HKCU", _CDM_POL, "FeatureManagementEnabled")])
    try:
        reg_set_value_checked(ctx, "HKCU", _CDM_POL, "ContentDeliveryAllowed", 0)
        reg_set_value_checked(ctx, "HKCU", _CDM_POL, "SubscribedContentEnabled", 0)
        reg_set_value_checked(ctx, "HKCU", _CDM_POL, "FeatureManagementEnabled", 0)
    except Exception:
        if not had_snapshot:
            clear_tweak_snapshot("ads_master_off")
        raise
    ctx.log("Content Delivery master switches off — suggestions engine quiet.")

def revert_ads_master_off(ctx: TaskContext):
    _restore_reg_values(ctx, "ads_master_off")
    ctx.log("Content Delivery switches restored to their prior settings.")

def verify_ads_master_off() -> "bool | None":
    return _verify_all_values([
        ("HKCU", _CDM_POL, "ContentDeliveryAllowed", 0),
        ("HKCU", _CDM_POL, "SubscribedContentEnabled", 0),
        ("HKCU", _CDM_POL, "FeatureManagementEnabled", 0),
    ])


_SEARCH_SETTINGS = "Software\\Microsoft\\Windows\\CurrentVersion\\SearchSettings"


def apply_device_search_history_off(ctx: TaskContext):
    """Windows stops remembering what you searched for on this PC.
    Per-user, snapshot + revert."""
    had_snapshot = bool(get_tweak_snapshot("device_search_history_off"))
    _snap_reg_values(ctx, "device_search_history_off",
                     [("HKCU", _SEARCH_SETTINGS, "IsDeviceSearchHistoryEnabled")])
    try:
        reg_set_value_checked(ctx, "HKCU", _SEARCH_SETTINGS,
                              "IsDeviceSearchHistoryEnabled", 0)
    except Exception:
        if not had_snapshot:
            clear_tweak_snapshot("device_search_history_off")
        raise
    ctx.log("On-device search history off — searches stay in the moment.")

def revert_device_search_history_off(ctx: TaskContext):
    _restore_reg_values(ctx, "device_search_history_off")
    ctx.log("Search history setting restored to its prior value.")

def verify_device_search_history_off() -> "bool | None":
    return _verify_value("HKCU", _SEARCH_SETTINGS, "IsDeviceSearchHistoryEnabled", 0)


def apply_long_paths(ctx: TaskContext):
    """Let games and mods use very long file and folder names without
    errors (32,767 chars instead of 260). Machine-wide, needs admin and
    a reboot to fully take effect. Snapshot + revert."""
    had_snapshot = bool(get_tweak_snapshot("long_paths"))
    _snap_reg_values(ctx, "long_paths",
                     [("HKLM", "SYSTEM\\CurrentControlSet\\Control\\FileSystem",
                       "LongPathsEnabled")])
    try:
        reg_set_value_checked(ctx, "HKLM", "SYSTEM\\CurrentControlSet\\Control\\FileSystem",
                              "LongPathsEnabled", 1)
    except Exception:
        if not had_snapshot:
            clear_tweak_snapshot("long_paths")
        raise
    ctx.log("Long file paths on — reboots to fully take effect for all apps.")

def revert_long_paths(ctx: TaskContext):
    _restore_reg_values(ctx, "long_paths")
    ctx.log("Long paths setting restored (reboot to fully apply).")

def verify_long_paths() -> "bool | None":
    return _verify_value("HKLM", "SYSTEM\\CurrentControlSet\\Control\\FileSystem",
                         "LongPathsEnabled", 1)


_LINK_PATH = "Software\\Microsoft\\Windows\\CurrentVersion\\Explorer"
_SHELL_ICONS = "SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\Explorer\\Shell Icons"


def apply_classic_shortcut_icons(ctx: TaskContext):
    """Clean desktop icons: no arrow overlay, no ' - Shortcut' suffix.
    Best-effort (the HKLM arrow half needs admin); the HKCU suffix half
    works everywhere. Snapshot + revert restores exact prior bytes."""
    from app.utils import reg_set_value as _set
    had_snapshot = bool(get_tweak_snapshot("classic_shortcut_icons"))
    _snap_reg_values(ctx, "classic_shortcut_icons",
                     [("HKLM", _SHELL_ICONS, "29", "REG_SZ"),
                      ("HKCU", _LINK_PATH, "link", "REG_BINARY")])
    ok, skipped = 0, []
    if _set(ctx, "HKLM", _SHELL_ICONS, "29", "", value_type="REG_SZ"):
        ok += 1
    else:
        skipped.append("HKLM arrow")
    try:
        from app.utils import reg_get_value as _get
        prior = _get(ctx, "HKCU", _LINK_PATH, "link")
        blank = bytes(len(prior)) if isinstance(prior, bytes) else bytes(16)
    except Exception:
        blank = bytes(16)
    if _set(ctx, "HKCU", _LINK_PATH, "link", blank, value_type="REG_BINARY"):
        ok += 1
    else:
        skipped.append("HKCU suffix")
    if ok == 0:
        if not had_snapshot:
            clear_tweak_snapshot("classic_shortcut_icons")
        raise RuntimeError("Could not clean shortcut icons (all writes blocked). Nothing was marked as applied.")
    if skipped:
        ctx.log(f"  (partially applied; skipped: {'; '.join(skipped)})")
    ctx.log("Shortcut icons cleaned — no arrows, no ' - Shortcut' suffix.")

def revert_classic_shortcut_icons(ctx: TaskContext):
    _restore_reg_values(ctx, "classic_shortcut_icons")
    ctx.log("Shortcut icons restored to their prior setting.")

def verify_classic_shortcut_icons() -> "bool | None":
    # applied state = all-zero bytes of whatever length Windows uses
    # here (4 on some builds, 16 on others) — compare content, not length
    try:
        v = __verify_read("HKCU", _LINK_PATH, "link")
        if v is None:
            return None
        if isinstance(v, bytes) and len(v) and all(b == 0 for b in v):
            return True
        return False
    except Exception:
        return None


_IPV4_PATH = "SYSTEM\\CurrentControlSet\\Services\\Tcpip6\\Parameters"
_IPV4_SPECS = [("HKLM", _IPV4_PATH, "DisabledComponents", "REG_DWORD")]


def apply_prefer_ipv4(ctx: TaskContext):
    """Prefer IPv4 over IPv6 for gaming (fixes lag on bad IPv6 routers).
    Sets Microsoft's documented 0x20 flag - IPv6 stays enabled, IPv4 just
    wins. Needs reboot. Snapshot + revert restores the exact prior value.

    BUG-015: this was the one apply_* of ~30 missing the failure branch the
    siblings all use. The snapshot was written and then the system change
    could raise, leaving a snapshot for a tweak that was never applied \u2014 so
    get_tweak_state() unions the snapshot into the applied set, the GUI showed
    a green "Active" badge for a change that never happened, and because
    save_tweak_snapshot refuses to overwrite an existing non-empty snapshot,
    a retry could not repair it either. Same had_snapshot/clear/raise
    wrapper as apply_visual_effects_perf and the rest."""
    had_snapshot = bool(get_tweak_snapshot("prefer_ipv4"))
    _snap_reg_values(ctx, "prefer_ipv4", _IPV4_SPECS)
    try:
        reg_set_value_checked(ctx, "HKLM", _IPV4_PATH, "DisabledComponents", 0x20)
    except Exception:
        if not had_snapshot:
            clear_tweak_snapshot("prefer_ipv4")
        raise
    ctx.log("IPv4 preferred for gaming (reboot to take effect).")


def revert_prefer_ipv4(ctx: TaskContext):
    _restore_reg_values(ctx, "prefer_ipv4")
    ctx.log("IP version preference restored (reboot to take effect).")


def verify_prefer_ipv4() -> "bool | None":
    try:
        v = __verify_read("HKLM", _IPV4_PATH, "DisabledComponents")
        if v is None:
            return None
        return int(v) == 0x20
    except Exception:
        return None


def apply_reserved_storage_off(ctx: TaskContext):
    """Free ~7 GB: Windows sets aside a chunk of your drive ('reserved
    storage') so future updates always have room. If your drive is
    tight, DISM /Set-ReservedStorageState 0 releases it immediately —
    Windows just downloads update payloads to normal free space instead.
    Fully reversible: /Set-ReservedStorageState 1 re-reserves it (it
    needs free space available to do so, which undo guidance logs).

    Admin required (DISM). The state is machine-wide disk config, not a
    per-user value, so there is no registry snapshot — the revert is
    the documented on switch, the exact inverse operation."""
    ctx.set_status("Releasing reserved storage (~7 GB)...")
    rc = run_cmd(ctx, "dism /Online /Set-ReservedStorageState 0", timeout=900)
    if rc == 740:
        raise RuntimeError("DISM needs Administrator rights — restart the app as Administrator.")
    if rc != 0:
        # verify the actual state before declaring failure (DISM can be
        # noisy about reboot-pending flags)
        chk = run_cmd(ctx, "dism /Online /Get-ReservedStorageState", timeout=300)
        if chk == 0:
            ctx.log("  (DISM reported an earlier flag, but the state query verified.)")
        else:
            raise RuntimeError(f"Could not release reserved storage (exit code {rc}).")
    ctx.log("Reserved storage released — ~7 GB back on your drive.")

def revert_reserved_storage_off(ctx: TaskContext):
    """Re-reserve the update storage (the documented inverse). Needs ~7 GB
    free; logs honestly when the drive is too full to restore it."""
    ctx.set_status("Re-reserving update storage...")
    rc = run_cmd(ctx, "dism /Online /Set-ReservedStorageState 1", timeout=900)
    if rc == 0:
        ctx.log("Reserved storage re-enabled — updates have their safety pad again.")
        return
    if rc == 740:
        raise RuntimeError("DISM needs Administrator rights — restart the app as Administrator.")
    raise RuntimeError(
        "Could not re-enable reserved storage — your drive may not have "
        "~7 GB free for Windows to set aside. Free up space and run Undo again."
    )


from app.tasks import Task  # noqa: E402


# --------------------------------------------------------------------------- #
# Tweak Health verify functions (user-approved feature 2, 2026-09)
#
# READ-ONLY drift detectors: each returns True (tweak still applied), False
# (Windows Update / a driver / the user reset it), or None (cannot tell).
# They read EXACTLY the values the matching apply_* function writes — no
# new registry knowledge, just the existing write specs read back. A tweak
# with no verify function simply shows 'applied' in the Health view (the
# config registry's word is trusted); False only ever comes from a
# definitive read of a tweaked value that is no longer the tweaked value.
# __verify_read is a no-log reader: the health check runs outside a run
# and must not pollute the run log.
# --------------------------------------------------------------------------- #

def __verify_read(hive: str, path: str, name: str):
    """reg_get_value without a TaskContext — silent, read-only, no log."""
    try:
        from app.utils import reg_get_value as _rgv
        class _NoCtx:
            @staticmethod
            def log(_m):
                pass
            @staticmethod
            def set_status(_m):
                pass
            @staticmethod
            def cancelled():
                return False
        return _rgv(_NoCtx, hive, path, name)  # type: ignore[arg-type]
    except Exception:
        return None


def _verify_all_values(specs_and_expected) -> "bool | None":
    """True when EVERY (hive, path, name, expected) tuple reads the
    expected value; None when the FIRST readable value doesn't match (or
    nothing could be read). Missing keys count as drifted: apply's writes
    make the values exist, so an absent value after an apply means it was
    removed. But if NOTHING is readable at all we return None — the honest
    'cannot tell' — rather than guessing False from a dead registry read."""
    saw_any = False
    for hive, path, name, expected in specs_and_expected:
        v = __verify_read(hive, path, name)
        if v is None:
            continue
        saw_any = True
        if v != expected and str(v) != str(expected):
            return False
    return True if saw_any else None


def _verify_value(hive: str, path: str, name: str, expected) -> "bool | None":
    return _verify_all_values([(hive, path, name, expected)])


def verify_ultimate_performance() -> "bool | None":
    """Active scheme is an Ultimate Performance duplicate."""
    try:
        guid = _active_scheme_guid()
        if not guid:
            return None
        return _guids_equal(guid, ULTIMATE_PERF_GUID) or _guids_equal(
            guid, _find_ultimate_scheme_guid() or "")
    except Exception:
        return None


def verify_classic_context_menu() -> "bool | None":
    v = __verify_read("HKCU", f"Software\\Classes\\CLSID\\{CONTEXT_MENU_CLSID}\\InprocServer32", "")
    return None if v is None else (str(v) == "")


def verify_disable_game_dvr() -> "bool | None":
    return _verify_all_values([
        ("HKCU", "System\\GameConfigStore", "GameDVR_Enabled", 0),
        ("HKCU", "Software\\Microsoft\\Windows\\CurrentVersion\\GameDVR", "AppCaptureEnabled", 0),
        ("HKLM", "SOFTWARE\\Policies\\Microsoft\\Windows\\GameDVR", "AllowGameDVR", 0),
    ])


def verify_game_mode() -> "bool | None":
    return _verify_all_values([
        ("HKCU", "Software\\Microsoft\\GameBar", "AutoGameModeEnabled", 1),
        ("HKCU", "Software\\Microsoft\\GameBar", "AllowAutoGameMode", 1),
    ])


def verify_windowed_optimize() -> "bool | None":
    return _verify_all_values([
        ("HKCU", "System\\GameConfigStore", "GameDVR_FSEBehaviorMode", 2),
        ("HKCU", "System\\GameConfigStore", "GameDVR_DXGIHonorFSEWindowsCompatible", 1),
        ("HKCU", "System\\GameConfigStore", "GameDVR_HonorUserFSEBehaviorMode", 1),
    ])


def verify_hags() -> "bool | None":
    return _verify_value("HKLM", "SYSTEM\\CurrentControlSet\\Control\\GraphicsDrivers", "HwSchMode", 2)


def verify_priority_separation() -> "bool | None":
    return _verify_value("HKLM", "SYSTEM\\CurrentControlSet\\Control\\PriorityControl", "Win32PrioritySeparation", 38)


def verify_power_throttling_off() -> "bool | None":
    return _verify_value("HKLM", "SYSTEM\\CurrentControlSet\\Control\\Power\\PowerThrottling", "PowerThrottlingOff", 1)


def verify_mouse_accel() -> "bool | None":
    return _verify_all_values([
        ("HKCU", "Control Panel\\Mouse", "MouseSpeed", "0"),
        ("HKCU", "Control Panel\\Mouse", "MouseThreshold1", "0"),
        ("HKCU", "Control Panel\\Mouse", "MouseThreshold2", "0"),
    ])


def verify_keyboard_tuning() -> "bool | None":
    return _verify_all_values([
        ("HKCU", "Control Panel\\Keyboard", "KeyboardDelay", "0"),
        ("HKCU", "Control Panel\\Keyboard", "KeyboardSpeed", "31"),
    ])


def verify_network_throttling() -> "bool | None":
    return _verify_value("HKLM", "SOFTWARE\\Microsoft\\Windows NT\\CurrentVersion\\Multimedia\\SystemProfile",
                         "NetworkThrottlingIndex", 0xFFFFFFFF)


def verify_games_priority() -> "bool | None":
    return _verify_all_values([
        ("HKLM", "SOFTWARE\\Microsoft\\Windows NT\\CurrentVersion\\Multimedia\\SystemProfile\\Tasks\\Games",
         "Scheduling Category", "High"),
        ("HKLM", "SOFTWARE\\Microsoft\\Windows NT\\CurrentVersion\\Multimedia\\SystemProfile\\Tasks\\Games",
         "GPU Priority", 8),
        ("HKLM", "SOFTWARE\\Microsoft\\Windows NT\\CurrentVersion\\Multimedia\\SystemProfile\\Tasks\\Games",
         "Priority", 6),
        ("HKLM", "SOFTWARE\\Microsoft\\Windows NT\\CurrentVersion\\Multimedia\\SystemProfile\\Tasks\\Games",
         "SFIO Priority", 8),
    ])


def verify_disable_nagle() -> "bool | None":
    """Nagle applies to every interface subkey — check the first readable
    one. TcpAckFrequency=1/TCPNoDelay=1 after apply; revert deletes them."""
    try:
        key = _open_interfaces_key()
        try:
            i = 0
            while True:
                try:
                    sub = winreg.EnumKey(key, i)
                except OSError:
                    break
                i += 1
                base = f"SYSTEM\\CurrentControlSet\\Services\\Tcpip\\Parameters\\Interfaces\\{sub}"
                v1 = __verify_read("HKLM", base, "TcpAckFrequency")
                v2 = __verify_read("HKLM", base, "TCPNoDelay")
                if v1 is None and v2 is None:
                    continue   # adapter with neither value (e.g. read-only)
                return (v1 == 1 and v2 == 1)
        finally:
            winreg.CloseKey(key)
    except Exception:
        return None
    return None


def verify_disable_sticky_keys() -> "bool | None":
    return _verify_all_values([
        ("HKCU", "Control Panel\\Accessibility\\StickyKeys", "Flags", "506"),
        ("HKCU", "Control Panel\\Accessibility\\Keyboard Response", "Flags", "122"),
        ("HKCU", "Control Panel\\Accessibility\\ToggleKeys", "Flags", "58"),
    ])


def verify_suppress_crash_popups() -> "bool | None":
    return _verify_value("HKCU", "Software\\Microsoft\\Windows\\Windows Error Reporting", "DontShowUI", 1)


def verify_hide_recent() -> "bool | None":
    return _verify_all_values([
        ("HKCU", "Software\\Microsoft\\Windows\\CurrentVersion\\Explorer", "ShowRecent", 0),
        ("HKCU", "Software\\Microsoft\\Windows\\CurrentVersion\\Explorer", "ShowFrequent", 0),
    ])


def verify_tdr_delay() -> "bool | None":
    return _verify_value("HKLM", "SYSTEM\\CurrentControlSet\\Control\\GraphicsDrivers", "TdrDelay", 8)


def verify_no_update_reboot() -> "bool | None":
    return _verify_value("HKLM", "SOFTWARE\\Policies\\Microsoft\\Windows\\WindowsUpdate\\AU",
                         "NoAutoRebootWithLoggedOnUsers", 1)


def verify_mpo_fix() -> "bool | None":
    return _verify_value("HKLM", "SOFTWARE\\Microsoft\\Windows\\Dwm", "OverlayTestMode", 5)


def verify_background_apps() -> "bool | None":
    return _verify_value("HKCU", "Software\\Microsoft\\Windows\\CurrentVersion\\BackgroundAccessApplications",
                         "GlobalUserDisabled", 1)


def verify_fullscreen_opt() -> "bool | None":
    return _verify_value("HKCU", "Software\\Microsoft\\Windows NT\\CurrentVersion\\AppCompatFlags\\Layers",
                         "DISABLEDXMAXIMIZEDWINDOWEDMODE", 1)


def verify_local_search() -> "bool | None":
    return _verify_value("HKCU", "Software\\Microsoft\\Windows\\CurrentVersion\\Search", "BingSearchEnabled", 0)


def verify_visual_effects() -> "bool | None":
    return _verify_value("HKCU", "Software\\Microsoft\\Windows\\CurrentVersion\\Themes\\Personalize",
                         "EnableTransparency", 0)


def verify_startup_delay() -> "bool | None":
    return _verify_value("HKCU", "Software\\Microsoft\\Windows\\CurrentVersion\\Explorer\\Serialize",
                         "StartupDelayInMSec", 0)


def verify_limit_telemetry() -> "bool | None":
    return _verify_value("HKLM", "SOFTWARE\\Policies\\Microsoft\\Windows\\DataCollection", "AllowTelemetry", 1)


def verify_shader_cache_10gb() -> "bool | None":
    """NVIDIA-only: RMShaderCacheSize=10240 (MB, 10 GB)."""
    return _verify_value("HKCU", "Software\\NVIDIA Corporation\\Global\\FTS", "RMShaderCacheSize", 10240)


def verify_max_cpu_power() -> "bool | None":
    """PERFBOOSTMODE=2 (Aggressive) on the AC side of the current scheme."""
    try:
        class _NoCtx:
            @staticmethod
            def log(_m): pass
            @staticmethod
            def set_status(_m): pass
            @staticmethod
            def cancelled(): return False
        from app.utils import powercfg_query_index as _pqi
        v = _pqi(_NoCtx, "sub_processor", "PERFBOOSTMODE")  # type: ignore[arg-type]
        return None if v is None else v == 2
    except Exception:
        return None


def verify_ssd_superfetch() -> "bool | None":
    """SysMain disabled — service start type read via the registry
    (locale-free, the same source sc_query_start_type uses)."""
    v = __verify_read("HKLM", "SYSTEM\\CurrentControlSet\\Services\\SysMain", "Start")
    if v is None:
        return None
    return v == 4          # 4 = disabled


TASKS = [
    Task("restore_point_tweak", "Safety Checkpoint", "Saves a restore point so you can undo changes", create_restore_point, default=True, admin_required=True),
    Task("ultimate_performance", "Max Performance Mode", "Turns on hidden fastest power plan for better FPS", apply_ultimate_performance, default=True, admin_required=True, revert=revert_ultimate_performance, verify=verify_ultimate_performance),
    # audit fix (12 mis-gated HKCU tweaks): these are pure HKCU in BOTH apply
    # and revert — the Task default admin_required=True needlessly skipped
    # them for every non-admin (limited-mode) user.
    Task("classic_context_menu", "Full Right-Click Menu", "Shows full menu right away, no extra click", apply_classic_context_menu, default=True, admin_required=False, revert=revert_classic_context_menu, verify=verify_classic_context_menu),
    Task("disable_game_dvr", "Stop Background Recording", "Stops Xbox recording games in background that slows you", apply_disable_game_dvr, default=True, admin_required=False, revert=revert_disable_game_dvr, verify=verify_disable_game_dvr),
    Task("game_mode", "Turn On Game Mode", "Lets Windows focus on your game, not background apps", apply_game_mode, default=True, admin_required=False, revert=revert_game_mode, verify=verify_game_mode),
    Task("windowed_optimize", "Smooth Windowed Games", "Makes games smoother when playing in a window", apply_windowed_optimize, default=True, admin_required=False, revert=revert_windowed_optimize, verify=verify_windowed_optimize),
    Task("hags", "Faster Graphics (HAGS)", "Lets graphics card handle memory faster, needs restart", apply_hags, default=False, admin_required=True, revert=revert_hags, verify=verify_hags, risk="REBOOT REQUIRED"),
    Task("priority_separation", "Prioritize Your Game", "Gives your open game more CPU power", apply_priority_separation, default=False, admin_required=True, revert=revert_priority_separation, verify=verify_priority_separation, risk="REBOOT REQUIRED"),
    Task("power_throttling_off", "Disable CPU Power Throttling", "Stops Windows from slowing CPU to save power", apply_power_throttling_off, default=False, admin_required=True, revert=revert_power_throttling_off, verify=verify_power_throttling_off, risk="REBOOT REQUIRED"),
    Task("ssd_trim", "Enable SSD TRIM", "Keeps your SSD fast and healthy", apply_ssd_trim, default=False, admin_required=True, revert=revert_ssd_trim, risk="REBOOT REQUIRED"),
    Task("ssd_superfetch", "Disable SysMain (Superfetch)", "Turns off old hard drive helper not needed for SSD", apply_ssd_superfetch, default=False, admin_required=True, revert=revert_ssd_superfetch, verify=verify_ssd_superfetch, risk="REBOOT REQUIRED"),
    Task("ssd_last_access", "Disable Last Access Updates", "Stops Windows writing every time you open a file", apply_ssd_last_access, default=False, admin_required=True, revert=revert_ssd_last_access, risk="REBOOT REQUIRED"),
    Task("ssd_prefetch", "Disable Prefetcher", "Turns off extra loading helper for fast drives", apply_ssd_prefetch, default=False, admin_required=True, revert=revert_ssd_prefetch, risk="REBOOT REQUIRED"),
    Task("disable_nagle", "Lower Ping (Advanced)", "Makes online games respond faster", apply_disable_nagle, default=False, admin_required=True, revert=revert_disable_nagle, verify=verify_disable_nagle, risk="ADVANCED"),
    Task("startup_delay", "Faster Startup", "Removes small delay before startup apps open", apply_startup_delay, default=False, admin_required=False, revert=revert_startup_delay, verify=verify_startup_delay),
    Task("max_cpu_power", "Max CPU Power", "Aggressive boost, no core parking, full speed while gaming", apply_max_cpu_power, default=False, admin_required=True, revert=revert_max_cpu_power, verify=verify_max_cpu_power),
    Task("taskbar_cleanup", "Remove Taskbar Junk", "Turns off Widgets, Chat icon, search highlights and Explorer ads", apply_taskbar_cleanup, default=False, admin_required=False, revert=revert_taskbar_cleanup),
    Task("local_search", "Fast Local Search", "Makes Start search instant with no Bing or web results", apply_local_search, default=False, admin_required=False, revert=revert_local_search, verify=verify_local_search),
    Task("stop_windows_ads", "Stop Windows Ads & Tips", "Blocks every suggestion, auto-install and lock-screen ad", apply_stop_windows_ads, default=False, admin_required=False, revert=revert_stop_windows_ads),
    Task("privacy_baseline", "Privacy Baseline", "One switch for ad ID, tracking, typing data and speech opt-outs", apply_privacy_baseline, default=False, admin_required=False, revert=revert_privacy_baseline),
    Task("stop_telemetry", "Stop Telemetry", "Turns off diagnostic services and tracking tasks safely", apply_stop_telemetry, default=False, admin_required=True, revert=revert_stop_telemetry),
    Task("nvidia_telemetry", "NVIDIA Telemetry Opt-Out", "Turns off NVIDIA's usage reports (driver untouched)", apply_nvidia_telemetry_optout, default=False, admin_required=True, revert=revert_nvidia_telemetry_optout),
    Task("ad_blocker", "System-Wide Ad Blocker", "Blocks ~78,000 ad/tracker domains via hosts (~2MB — may slow first-visit lookups on slow disks)", apply_ad_blocker, default=False, revert=revert_ad_blocker, admin_required=True, risk="ADVANCED"),
    Task("visual_effects", "Faster Animations", "Turns off transparency and animations for speed", apply_visual_effects_perf, default=False, admin_required=False, revert=revert_visual_effects_perf, verify=verify_visual_effects),
    Task("mouse_accel", "1:1 Mouse Aim", "Turns off mouse speedup so aim is steady", apply_disable_mouse_accel, default=False, admin_required=False, revert=revert_disable_mouse_accel, verify=verify_mouse_accel),
    Task("keyboard_tuning", "Faster Keyboard", "Makes keys repeat faster when you hold them", apply_keyboard_tuning, default=False, admin_required=False, revert=revert_keyboard_tuning, verify=verify_keyboard_tuning),
    Task("network_throttling", "Faster Online Gaming", "Removes speed limit Windows uses for videos", apply_network_throttling, default=False, admin_required=True, revert=revert_network_throttling, verify=verify_network_throttling),
    Task("games_priority", "Boost Game Priority", "Raises game priority for CPU and graphics", apply_games_priority, default=False, admin_required=True, revert=revert_games_priority, verify=verify_games_priority),
    Task("usb_suspend", "Fix USB Dropouts", "Stops Windows pausing USB mics and controllers", apply_usb_suspend, default=False, admin_required=True, revert=revert_usb_suspend),
    Task("disk_timeout", "Keep Drive Awake", "Stops drive from sleeping while you game", apply_disk_timeout, default=False, admin_required=True, revert=revert_disk_timeout),
    Task("disable_fast_startup", "Fix Boot Issues", "Turns off fast boot to fix driver problems", apply_fast_startup_fix, default=False, admin_required=True, revert=revert_fast_startup_fix, risk="REBOOT REQUIRED"),
    Task("limit_telemetry", "Limit Tracking", "Tells Windows to collect less info about you", apply_limit_telemetry, default=False, admin_required=True, revert=revert_limit_telemetry, verify=verify_limit_telemetry),
    Task("activity_history", "Disable Activity History", "Stops Windows saving your recent files and history", apply_activity_history_disable, default=False, admin_required=True, revert=revert_activity_history_disable),
    Task("consumer_features", "Disable Consumer Features", "Stops Windows installing suggested apps", apply_consumer_features_disable, default=False, admin_required=True, revert=revert_consumer_features_disable),
    Task("tweak_delivery_optimization", "Disable Delivery Optimization", "Stops sharing updates with other PCs", apply_delivery_optimization_disable, default=False, admin_required=True, revert=revert_delivery_optimization_disable),
    Task("explorer_auto_discovery", "No Explorer Auto Discovery", "Stops Explorer guessing folder types — IRREVERSIBLY clears all saved folder views/sorts (Undo cannot restore them)", apply_explorer_auto_discovery_disable, default=False, admin_required=False, revert=revert_explorer_auto_discovery_disable, risk="ADVANCED"),
    Task("background_apps", "Disable Background Apps", "Stops apps running in background so games get more power", apply_background_apps_disable, default=False, admin_required=False, revert=revert_background_apps_disable, verify=verify_background_apps),
    Task("shader_cache_10gb", "Shader Cache 10GB", "Sets shader cache to 10GB to stop stutter", apply_shader_cache_10gb, default=False, admin_required=False, revert=revert_shader_cache_10gb, verify=verify_shader_cache_10gb),
    Task("max_performance_gpu", "Prefer Max Performance", "Tells GPU to use max power for games", apply_nvidia_max_performance, default=False, admin_required=True, revert=revert_nvidia_max_performance),
    Task("fullscreen_opt", "Fullscreen Optimizations Off", "Fixes game lag in borderless window", apply_fullscreen_optimizations_disable, default=False, admin_required=False, revert=revert_fullscreen_optimizations_disable, verify=verify_fullscreen_opt),
    Task("disable_sticky_keys", "Stop Sticky Keys Popups", "Stops Shift-spam popups/beeps interrupting games", apply_disable_sticky_keys, default=False, revert=revert_disable_sticky_keys, verify=verify_disable_sticky_keys, admin_required=False),
    Task("suppress_crash_popups", "Stop Crash Popups", "Background app crashes no longer pause or cover your game", apply_suppress_crash_popups, default=False, revert=revert_suppress_crash_popups, verify=verify_suppress_crash_popups, admin_required=False),
    Task("no_update_reboot", "Block Update Reboots", "Windows Update won't force-restart your PC while you're using it", apply_no_update_reboot, default=False, revert=revert_no_update_reboot, verify=verify_no_update_reboot, admin_required=True),
    Task("mpo_fix", "Fix Monitor Flicker (MPO)", "Fixes black-screen flashes and stutter on multi-monitor setups", apply_mpo_fix, default=False, revert=revert_mpo_fix, verify=verify_mpo_fix, admin_required=True, risk="REBOOT REQUIRED"),

    # --- Round 2 feature tasks (user request) --- #
    Task("gaming_dns", "Gaming DNS (Cloudflare)", "Switches DNS to Cloudflare 1.1.1.1/1.0.0.1 — often the fastest for game servers; fully undoable", apply_gaming_dns, default=False, revert=revert_gaming_dns, admin_required=True),
    Task("refresh_rate_fix", "Max Refresh Rate", "Checks your monitor is running at its highest Hz — many 144Hz+ screens ship stuck at 60Hz", apply_refresh_rate_fix, default=False, revert=revert_refresh_rate_fix, admin_required=False),
    Task("gpu_preference_high", "Prefer Dedicated GPU", "Tells Windows to always run games on the dedicated GPU instead of the power-saving one (laptops)", apply_gpu_preference_high, default=False, revert=revert_gpu_preference_high, admin_required=False),

    # --- Round 3 tasks (user request) --- #
    Task("eee_disable", "Fix NIC Disconnects (EEE)", "Stops network card power-saving that drops packets mid-game; restores your exact prior settings on undo", apply_eee_disable, default=False, revert=revert_eee_disable, admin_required=True),
    Task("ntfs_8dot3", "Disable 8.3 Short Names", "Speeds up folders with huge numbers of files; restores your PC's prior setting on undo", apply_ntfs_8dot3_disable, default=False, revert=revert_ntfs_8dot3_disable, admin_required=True),
    Task("dynamic_tick_off", "Disable Dynamic Tick", "Legacy latency tweak for benchmarkers — debatable gains on modern PCs; fully undoable", apply_disable_dynamic_tick, default=False, revert=revert_disable_dynamic_tick, admin_required=True, risk="REBOOT REQUIRED"),

    # --- Round 4 tasks (user request: everyday usability + quiet privacy) --- #
    Task("file_extensions", "Show File Extensions", "Shows file extensions and hidden files — a must for editing configs and mods", apply_file_extensions, default=False, revert=revert_file_extensions, admin_required=False),
    Task("menu_delay", "Snappier Menus", "Menus pop in 100ms instead of 400ms — instant-feeling Start menu", apply_menu_delay, default=False, revert=revert_menu_delay, admin_required=False),
    Task("aero_shake", "No Shake-to-Minimize", "Stops windows minimizing everything when you grab and shake one mid-game", apply_aero_shake_off, default=False, revert=revert_aero_shake_off, admin_required=False),
    Task("lock_screen", "Skip Lock Screen", "Boots straight to the login prompt instead of the pretty lock screen", apply_lock_screen_off, default=False, revert=revert_lock_screen_off, admin_required=True),
    Task("edge_preload", "Stop Edge Preloading", "Stops Edge background boost processes if you never open Edge", apply_edge_preload_off, default=False, revert=revert_edge_preload_off, admin_required=True),
    Task("dark_mode", "Prefer Dark Apps", "Asks apps to use their dark theme for a consistent look", apply_dark_mode, default=False, revert=revert_dark_mode, admin_required=False),
    Task("remote_assist", "Disable Remote Assistance", "Closes the inbound-remote-help vector most gamers never use", apply_remote_assist_off, default=False, revert=revert_remote_assist_off, admin_required=True),
    Task("verbose_boot", "Verbose Boot Messages", "Shows what Windows is doing at boot and shutdown instead of the spinner", apply_verbose_boot, default=False, revert=revert_verbose_boot, admin_required=True),
    Task("location_tracking", "Disable Location Tracking", "Turns off the location sensor (breaks Find My Device and auto time-zone)", apply_location_tracking_off, default=False, revert=revert_location_tracking_off, admin_required=True),
    Task("widgets_board_off", "Disable Widgets Board", "Kills the Widgets news board entirely, not just its taskbar icon", apply_widgets_board_off, default=False, revert=revert_widgets_board_off, admin_required=True),
    Task("autoplay_off", "Disable USB AutoPlay", "Stops USB sticks auto-launching apps when plugged in", apply_autoplay_off, default=False, revert=revert_autoplay_off, admin_required=False),
    Task("snap_flyout_off", "No Snap Popups", "Stops layout popups when hovering maximize mid-game", apply_snap_flyout_off, default=False, revert=revert_snap_flyout_off, admin_required=False),

    # --- Round 5 tasks (user request: self-maintaining PC + responsiveness) --- #
    Task("storage_sense", "Auto Cleanup (Storage Sense)", "Windows cleans its own temp junk monthly — the PC maintains itself", apply_storage_sense, default=False, revert=revert_storage_sense, admin_required=False),
    Task("fast_app_close", "Fast App Close", "Shutdown stops waiting 20 seconds for frozen apps to close", apply_fast_app_close, default=False, revert=revert_fast_app_close, admin_required=False),
    Task("instant_alt_tab", "Instant Alt-Tab Focus", "Removes the window-focus lockout delay when alt-tabbing back into games", apply_instant_alt_tab, default=False, revert=revert_instant_alt_tab, admin_required=False),
    Task("numlock_boot", "NumLock at Boot", "Keeps the numpad ready at every boot for MMO and RPG muscle memory", apply_numlock_boot, default=False, revert=revert_numlock_boot, admin_required=False),
    Task("clipboard_sync_off", "Stop Cloud Clipboard Sync", "Stops copied text being uploaded to Microsoft's cloud — Win+V stays local", apply_clipboard_sync_off, default=False, revert=revert_clipboard_sync_off, admin_required=True),
    Task("reserved_storage_off", "Free Reserved Storage", "Releases the ~7 GB Windows locks away for updates — back when you undo", apply_reserved_storage_off, default=False, revert=revert_reserved_storage_off, admin_required=True),
    # --- Round 6 tasks (user request: taskbar usability + mic privacy) --- #
    # All HKCU-only (no admin), all snapshot + revert, all Custom-only.
    Task("taskbar_endtask", "Taskbar End Task Button", "Right-click any taskbar icon to force-close a frozen game, no Task Manager", apply_taskbar_endtask, default=False, revert=revert_taskbar_endtask, verify=verify_taskbar_endtask, admin_required=False),
    Task("mic_privacy_allow", "Allow Microphone For Apps", "Fixes 'nobody hears me in game/chat' when Windows revoked mic access", apply_mic_privacy_allow, default=False, revert=revert_mic_privacy_allow, verify=verify_mic_privacy_allow, admin_required=False),
    Task("clock_seconds", "Clock Shows Seconds", "Taskbar clock shows seconds — handy for cooldown and queue timers", apply_clock_seconds, default=False, revert=revert_clock_seconds, verify=verify_clock_seconds, admin_required=False),
    Task("taskbar_left", "Left-Align Taskbar", "Moves Start back to the left corner, classic style", apply_taskbar_left, default=False, revert=revert_taskbar_left, verify=verify_taskbar_left, admin_required=False),
    # --- Round 7 tasks (user request: Winhance-audit picks) --- #
    # All snapshot + revert (+verify), all Custom-only, presets untouched.
    Task("alt_tab_windows_only", "Alt+Tab Windows Only", "Alt+Tab flips between real windows and games, no Edge tabs in the way", apply_alt_tab_windows_only, default=False, revert=revert_alt_tab_windows_only, verify=verify_alt_tab_windows_only, admin_required=False),
    Task("taskbar_never_combine", "Never Combine Taskbar Buttons", "Every open window keeps its own labeled button instead of piling up", apply_taskbar_never_combine, default=False, revert=revert_taskbar_never_combine, verify=verify_taskbar_never_combine, admin_required=False),
    Task("discord_no_ducking", "Stop Discord Lowering Game Sound", "Calls no longer auto-lower your game and media volume", apply_discord_no_ducking, default=False, revert=revert_discord_no_ducking, verify=verify_discord_no_ducking, admin_required=False),
    Task("ads_master_off", "Master Switch: No Windows Ads", "One flick turns off the whole suggestions and ads engine at the source", apply_ads_master_off, default=False, revert=revert_ads_master_off, verify=verify_ads_master_off, admin_required=False),
    Task("device_search_history_off", "No On-Device Search History", "Windows stops remembering what you searched for on this PC", apply_device_search_history_off, default=False, revert=revert_device_search_history_off, verify=verify_device_search_history_off, admin_required=False),
    Task("long_paths", "Allow Long File Paths", "Lets games and mods use very long file names without errors", apply_long_paths, default=False, revert=revert_long_paths, verify=verify_long_paths, admin_required=True),
    Task("classic_shortcut_icons", "Classic Shortcut Icons", "Clean desktop icons: no arrow overlay, no ' - Shortcut' text", apply_classic_shortcut_icons, default=False, revert=revert_classic_shortcut_icons, verify=verify_classic_shortcut_icons, admin_required=False),
    Task("prefer_ipv4", "Prefer IPv4 For Gaming", "Fixes lag on bad IPv6 routers by preferring IPv4; IPv6 stays on", apply_prefer_ipv4, default=False, revert=revert_prefer_ipv4, verify=verify_prefer_ipv4, admin_required=True),
    Task("hide_recent", "Hide Recent Files", "Stops Explorer showing recent files and frequent folders", apply_hide_recent, default=False, revert=revert_hide_recent, verify=verify_hide_recent, admin_required=False),
    Task("tdr_delay", "Fix GPU Timeout Crashes", "Waits 8s instead of 2s before resetting a hung GPU — fixes DXGI hang crashes", apply_tdr_delay, default=False, revert=revert_tdr_delay, verify=verify_tdr_delay, admin_required=True, risk="REBOOT REQUIRED"),
]
