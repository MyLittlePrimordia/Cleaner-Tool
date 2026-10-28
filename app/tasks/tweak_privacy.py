"""Tweak tab — Privacy and telemetry tweaks: the telemetry services/scheduled tasks, activity
history, consumer features, delivery optimisation, background apps, Windows
and NVIDIA telemetry, ads/CDM, the hosts-based ad blocker, location, clipboard,
microphone consent, search history and Cortana/lock-screen tips.

Split out of app/tasks/tweak_tasks.py (audit 2026-10, R-002 stage 2). Every
function here is one Task's apply/revert/verify implementation; the shared
constants and primitives live in tweak_common, and `TASKS` is assembled in
tweak_tasks.py so the registry still reads as one literal list.
"""
from __future__ import annotations

from app.tasks.tweak_common import (  # noqa: F401 - re-exported
    TaskContext,
    _ACTIVITY_HISTORY_SPECS,
    _ADBLOCK_MARKER_END,
    _ADBLOCK_MARKER_START,
    _ADS_CDM,
    _ADS_CDM_NAMES,
    _CDM_POL,
    _CONTENT_DELIVERY,
    _CORTANA_KEY,
    _HOSTS_BACKUP_PATH,
    _HOSTS_PATH,
    _MIC_CONSENT,
    _PRIVACY_BASELINE_SPECS,
    _SEARCH_SETTINGS,
    _STOP_ADS_SPECS,
    _regn_apply,
    _regn_revert,
    _restore_reg_values,
    _snap_reg_values,
    _strip_adblock_block,
    _verify_all_values,
    _verify_value,
    _write_hosts_file,
    clear_tweak_snapshot,
    get_tweak_snapshot,
    os,
    reg_delete_value,
    reg_set_value_checked,
)
from app.utils import (  # noqa: F401 - re-exported
    TaskCancelled,
    TaskSkipped,
    exclusive_create_text,
    resolve_asset_path,
    run_cmd,
    sc_query_start_type,
)
from app.tasks.tweak_snapshots import (  # noqa: F401 - re-exported
    _NVIDIA_TASK_PATTERNS,
    _collect_schtasks_priors,
    _collect_task_priors,
    _merge_task_priors,
    _merge_tweak_snapshot,
    _set_task_enabled,
    _snapshot_validation_error,
    _start_types_equal,
    _telemetry_tasks,
    _valid_scheduled_task_name,
    _valid_snapshot_start_type,
)
from app.config_persist import save_tweak_snapshot  # noqa: F401 - re-exported



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
    # B-0xx: report honestly. The old code logged "reverted" even when there
    # was no snapshot at all (or nothing was restored), unlike its siblings
    # which raise when the restore count is zero — a false success.
    if _restore_reg_values(ctx, "activity_history_disable") <= 0:
        raise RuntimeError(
            "Nothing was restored for Activity History (no snapshot, or the "
            "restore did not run) — see the log.")
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
        if _svc not in _priors:
            # B-0xx: we could not read this service's start type, so there is
            # no undo record for it. Disabling it anyway made revert substitute
            # a guessed default later. Leave it untouched instead.
            ctx.log(f"  ! could not read {_svc}'s start type — leaving it "
                    "untouched (no undo record would exist).")
            continue
        if run_cmd(ctx, ["sc", "config", _svc, "start=", "disabled"],
                   shell=False, timeout=30) == 0:
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
        # argv + shell=False, same reason as the revert side (SEC-001): this
        # name is echoed out of schtasks output and then written into
        # config.json, so it must never be handed to a command interpreter.
        if not _valid_scheduled_task_name(_task):
            ctx.log("  ! refusing to disable a scheduled task with an "
                    f"implausible name: {_task!r}")
            continue
        if run_cmd(ctx, ["schtasks", "/change", "/tn", _task, "/disable"],
                   shell=False, timeout=30) == 0:
            _ok += 1
        else:
            ctx.log(f"  ! skipped (task absent): {_task}")
    if task_priors:
        _merge_task_priors("stop_telemetry", task_priors)
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
    _failed = 0
    for _svc, _t in _targets.items():
        if ctx.cancelled():
            raise TaskCancelled("Stop Telemetry restore stopped by user — snapshot kept for Undo.")
        if run_cmd(ctx, ["sc", "config", _svc, "start=", _t],
                   shell=False, timeout=30) == 0:
            _ok += 1
        else:
            _failed += 1
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
            # SEC-001: this name is config.json data. It used to be
            # interpolated into f'schtasks /change /tn "{_task}" /{_want}'
            # under an implicit shell=True, so a key of
            #   x" & calc.exe & rem "
            # executed calc.exe ELEVATED at Undo time. argv + shell=False means
            # cmd.exe never parses it; the shape check is defence in depth.
            if not _valid_scheduled_task_name(_task):
                ctx.log("  ! refusing to touch a scheduled task with an "
                        f"implausible name (not in config.json verbatim): {_task!r}")
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
            if run_cmd(ctx, ["schtasks", "/change", "/tn", _task, "/" + _want],
                       shell=False, timeout=30) == 0:
                _ok += 1
            else:
                _failed += 1
                ctx.log(f"  ! skipped (task absent): {_task}")
    else:
        ctx.log("  (no recorded per-task states — diagnostic tasks left "
                "untouched rather than re-enabled blindly.)")
    # B-0xx: clear the snapshot ONLY on a fully successful restore. The old
    # code cleared it whenever _ok > 0, so a single failed service/task
    # destroyed the undo record for every item that failed — leaving those
    # services permanently disabled with no way to retry.
    if _failed:
        raise RuntimeError(
            f"Only {_ok} of {_ok + _failed} telemetry item(s) could be "
            f"restored ({_failed} failed) — snapshot kept so Undo can be "
            "retried for the rest.")
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
    _nv_prior = sc_query_start_type(ctx, "NvTelemetryContainer")
    has_nv_service = _nv_prior is not None
    if not has_nv_key and not has_nv_service:
        # B5 audit fix: log + `return` used to badge this '✓ Active' with
        # nothing changed — TaskSkipped = completed-with-skip (no badge).
        raise TaskSkipped("No NVIDIA software detected — skipping telemetry opt-out (nothing to do).")
    if has_nv_key:
        # M-7 fix: snapshot these four values (value OR absence) under their own
        # task id BEFORE overwriting, so Undo restores exactly what was there
        # instead of deleting a user/org setting. A separate id keeps this clear
        # of the service/task snapshot the revert reads further down.
        _regn_apply(ctx, "nvidia_telemetry_regs", [
            ("HKCU", "Software\\NVIDIA Corporation\\Global\\NvControlPanel2\\Client", "OptInOrOutPreference", 0),
            ("HKCU", "Software\\NVIDIA Corporation\\Global\\FTS", "EnableRID44231", 0),
            ("HKCU", "Software\\NVIDIA Corporation\\Global\\FTS", "EnableRID64640", 0),
            ("HKCU", "Software\\NVIDIA Corporation\\Global\\FTS", "EnableRID66610", 0),
        ])
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
        prior_start_type = _nv_prior
        # M-7 fix: MERGE, not save. The registry snapshot above may already own
        # this task id, and save_tweak_snapshot refuses to overwrite — which
        # would silently drop the start type. _merge keeps both.
        _merge_tweak_snapshot("nvidia_telemetry", {"NvTelemetryContainer_start_type": prior_start_type})
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
    task_failed = 0
    for name, was_disabled in task_priors.items():
        if was_disabled:
            continue          # already off before we got here — leave it off
        if _set_task_enabled(ctx, name, False):
            task_hits += 1
        else:
            # B-0xx: report per-task failures instead of swallowing them — the
            # task previously claimed a full opt-out while some report tasks
            # were still armed.
            task_failed += 1
            ctx.log(f"  ! could not disable scheduled task {name}")
    if task_priors:
        _merge_task_priors("nvidia_telemetry", task_priors)
    if task_hits:
        changed.append(f"{task_hits} scheduled task"
                       f"{'s' if task_hits > 1 else ''}")
    if task_failed:
        ctx.log(f"  ({task_failed} NVIDIA report task(s) could not be disabled "
                "— they may be policy-protected.)")
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
    """M-7 mirror: restore the preference values apply() overwrote instead of
    deleting them. apply() snapshots the four values under its own task id
    ("nvidia_telemetry_regs"), so this restores exact priors (absence comes back
    as absence). The service/task snapshot is a SEPARATE id read further down,
    so restoring the registry half cannot clear it."""
    if get_tweak_snapshot("nvidia_telemetry_regs"):
        _restore_reg_values(ctx, "nvidia_telemetry_regs")
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
    task_failed = 0
    if isinstance(priors, dict) and priors:
        for name, was_disabled in priors.items():
            if not isinstance(name, str) or not name.strip():
                continue
            if _set_task_enabled(ctx, name, not was_disabled):
                restored += 1
            else:
                task_failed += 1
        if restored:
            ctx.log(f"  restored {restored} scheduled task"
                    f"{'s' if restored > 1 else ''} to their recorded state.")
    else:
        ctx.log("  (no recorded per-task states — scheduled tasks left "
                "untouched rather than re-enabled blindly.)")
    # B-0xx: a per-task failure must keep the snapshot too. The old code
    # counted only successes and cleared unconditionally below, so a task that
    # could not be re-enabled stayed disabled with its only undo record gone.
    if task_failed:
        raise RuntimeError(
            f"Only {restored} of {restored + task_failed} scheduled task(s) "
            "could be restored — snapshot kept so Undo can be retried for the "
            "rest. (The service start type was restored above.)")
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
        # B-022: read byte-faithfully. errors="ignore" silently dropped
        # non-UTF-8 bytes (ANSI/UTF-16 hosts entries), which both corrupted the
        # "original" backup and could fuse two lines into one, changing DNS
        # resolution permanently. surrogateescape round-trips every byte
        # exactly on the matching write below.
        with open(_HOSTS_PATH, "r", encoding="utf-8", errors="surrogateescape") as f:
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
        created = exclusive_create_text(_HOSTS_BACKUP_PATH, current,
                                        errors="surrogateescape")
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
        with open(_HOSTS_PATH, "r", encoding="utf-8", errors="surrogateescape") as f:
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


def apply_clipboard_sync_off(ctx: TaskContext):
    """Stop Windows from syncing your clipboard to the cloud and your
    phone: Win+V history is local, but with a Microsoft account the
    'Sync across devices' feature uploads copied text (and screenshots
    of it) to Microsoft servers. Privacy-relevant for anyone who copies
    passwords, keys or personal text. AllowCrossDeviceClipboard=0 is
    Microsoft's documented value that blocks the sync/cloud half while
    leaving local Win+V history working (per MS Learn: 0 = Not allowed,
    1 = Allowed, and 1 is the Windows default — the old code wrote 1, which
    did the opposite of this label).

    HKLM policy key (admin), snapshot + revert."""
    had_snapshot = bool(get_tweak_snapshot("clipboard_sync_off"))
    _snap_reg_values(ctx, "clipboard_sync_off",
                     [("HKLM", "SOFTWARE\\Policies\\Microsoft\\Windows\\System",
                       "AllowCrossDeviceClipboard")])
    try:
        reg_set_value_checked(ctx, "HKLM", "SOFTWARE\\Policies\\Microsoft\\Windows\\System",
                              "AllowCrossDeviceClipboard", 0)
    except Exception:
        if not had_snapshot:
            clear_tweak_snapshot("clipboard_sync_off")
        raise
    ctx.log("Cloud clipboard sync disabled — Win+V keeps working, locally only.")


def revert_clipboard_sync_off(ctx: TaskContext):
    _restore_reg_values(ctx, "clipboard_sync_off")
    ctx.log("Cloud clipboard sync restored to its prior setting.")


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


def verify_background_apps() -> "bool | None":
    return _verify_value("HKCU", "Software\\Microsoft\\Windows\\CurrentVersion\\BackgroundAccessApplications",
                         "GlobalUserDisabled", 1)


def verify_limit_telemetry() -> "bool | None":
    return _verify_value("HKLM", "SOFTWARE\\Policies\\Microsoft\\Windows\\DataCollection", "AllowTelemetry", 1)


def apply_cortana_off(ctx: TaskContext):
    """Turn Cortana off for good (admin, policy key).

    `local_search` already kills Bing web results in the Start box, but
    Cortana itself keeps running in the background, waits on the wake word,
    and is a cloud data-collection path. Disabling it does NOT affect normal
    file/app/settings search - that is Windows Search, a separate service.
    """
    had_snapshot = bool(get_tweak_snapshot("cortana_off"))
    _snap_reg_values(ctx, "cortana_off",
                     [("HKLM", _CORTANA_KEY, "AllowCortana"),
                      ("HKLM", _CORTANA_KEY, "CortanaConsent")])
    try:
        reg_set_value_checked(ctx, "HKLM", _CORTANA_KEY, "AllowCortana", 0)
        reg_set_value_checked(ctx, "HKLM", _CORTANA_KEY, "CortanaConsent", 0)
    except Exception:
        if not had_snapshot:
            clear_tweak_snapshot("cortana_off")
        raise
    ctx.log("Cortana disabled (search, files and settings still work).")
    ctx.log("Takes effect after a restart.")


def revert_cortana_off(ctx: TaskContext):
    _restore_reg_values(ctx, "cortana_off")
    ctx.log("Cortana re-enabled.")


def verify_cortana_off() -> "bool | None":
    return _verify_value("HKLM", _CORTANA_KEY, "AllowCortana", 0)


def apply_lockscreen_tips_off(ctx: TaskContext):
    """Stop the lock screen showing rotating 'fun facts', tips and ads.

    Distinct from the existing `lock_screen` tweak, which skips the lock
    screen entirely (NoLockScreen). This one keeps the lock screen and
    removes only the rotating content Windows fetches for it - which is a
    network fetch and a small telemetry surface, on a screen every user
    sees dozens of times a day.
    """
    had_snapshot = bool(get_tweak_snapshot("lockscreen_tips_off"))
    _snap_reg_values(ctx, "lockscreen_tips_off",
                     [("HKCU", _CONTENT_DELIVERY,
                       "SubscribedContent-338387Enabled"),
                      ("HKCU", _CONTENT_DELIVERY,
                       "RotatingLockScreenOverlay")])
    try:
        reg_set_value_checked(ctx, "HKCU", _CONTENT_DELIVERY,
                              "SubscribedContent-338387Enabled", 0)
        reg_set_value_checked(ctx, "HKCU", _CONTENT_DELIVERY,
                              "RotatingLockScreenOverlay", 0)
    except Exception:
        if not had_snapshot:
            clear_tweak_snapshot("lockscreen_tips_off")
        raise
    ctx.log("Lock screen tips and rotating facts turned off.")


def revert_lockscreen_tips_off(ctx: TaskContext):
    _restore_reg_values(ctx, "lockscreen_tips_off")
    ctx.log("Lock screen tips restored to their prior setting.")


def verify_lockscreen_tips_off() -> "bool | None":
    return _verify_value("HKCU", _CONTENT_DELIVERY,
                         "SubscribedContent-338387Enabled", 0)
