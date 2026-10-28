"""Tweak tab — Gaming and graphics tweaks: Game DVR and Game Bar capture, HAGS, Game Mode,
windowed optimisations, shader cache, NVIDIA max performance, fullscreen
optimisations, MPO/TDR, GPU preference and refresh-rate handling, mouse and
visual-effects work.

Split out of app/tasks/tweak_tasks.py (audit 2026-10, R-002 stage 2). Every
function here is one Task's apply/revert/verify implementation; the shared
constants and primitives live in tweak_common, and `TASKS` is assembled in
tweak_tasks.py so the registry still reads as one literal list.
"""
from __future__ import annotations

from app.tasks.tweak_common import (  # noqa: F401 - re-exported
    TaskContext,
    _FSE_SPECS,
    _GAMEBAR_CAPTURE_SPECS,
    _GAMES_PRIORITY_SPECS,
    _GAME_DVR_SPECS,
    _GAME_MODE_SPECS,
    _GPU_PREF_PATH,
    _GPU_PREF_VALUE,
    _HAGS_SPECS,
    _MOUSE_SPECS,
    _NVIDIA_MAX_PERF_FALLBACK,
    _NVIDIA_MAX_PERF_PAIRS,
    _PRIORITY_SEP_SPECS,
    _TDR_SPECS,
    _VISUAL_EFFECTS_DEFAULTS,
    _VISUAL_EFFECTS_SPECS,
    __verify_read,
    _devmode_class,
    _query_video_mode_list,
    _read_gpu_pref,
    _regn_apply,
    _regn_revert,
    _restore_reg_values,
    _snap_reg_values,
    _verify_all_values,
    _verify_value,
    clear_tweak_snapshot,
    get_tweak_snapshot,
    reg_delete_value,
    reg_set_value,
    reg_set_value_checked,
)
from app.utils import (  # noqa: F401 - re-exported
    TaskSkipped,
    reg_get_value,
    run_cmd_checked,
)
from app.tasks.ps import ps_argv
from app.tasks.tweak_snapshots import (  # noqa: F401 - re-exported
    _restore_powercfg_pairs,
    _snapshot_powercfg_pairs,
)
from app.config_persist import save_tweak_snapshot  # noqa: F401 - re-exported



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


def apply_gamebar_capture(ctx: TaskContext):
    """Turn Game Bar screenshots and clips back on (see the block comment).

    The two required writes are per-user (no admin); the policy clear needs
    admin and is best-effort, exactly like disable_game_dvr's HKLM write, so a
    limited-mode user still gets the part that actually matters on LTSC."""
    from app.utils import reg_set_value as _set
    had_snapshot = bool(get_tweak_snapshot("gamebar_capture"))
    _snap_reg_values(ctx, "gamebar_capture", _GAMEBAR_CAPTURE_SPECS)
    ok, skipped = 0, []
    for hive, path, name in (
        ("HKCU", "System\\GameConfigStore", "GameDVR_Enabled"),
        ("HKCU", "Software\\Microsoft\\Windows\\CurrentVersion\\GameDVR", "AppCaptureEnabled"),
    ):
        if _set(ctx, hive, path, name, 1):
            ok += 1
        else:
            skipped.append(f"{hive}\\{name}")
    # Clear a blocking policy — but never create one. Read first: only a
    # present 0 is the policy this app itself can leave behind.
    try:
        policy = reg_get_value(ctx, "HKLM",
                               "SOFTWARE\\Policies\\Microsoft\\Windows\\GameDVR",
                               "AllowGameDVR")
    except Exception:
        policy = None
    if policy == 0:
        if reg_delete_value(ctx, "HKLM",
                            "SOFTWARE\\Policies\\Microsoft\\Windows\\GameDVR",
                            "AllowGameDVR"):
            ok += 1
            ctx.log("  (removed the AllowGameDVR=0 policy that was blocking capture)")
        else:
            skipped.append("HKLM\\AllowGameDVR (admin needed)")
    if ok == 0:
        if not had_snapshot:
            clear_tweak_snapshot("gamebar_capture")
        raise RuntimeError("Could not turn on Game Bar capture (all writes blocked). Nothing was marked as applied.")
    if skipped:
        ctx.log(f"  (applied {ok} of {ok + len(skipped)}; skipped: {'; '.join(skipped)} — admin rights needed for HKLM)")
    ctx.log("Game Bar screenshots and clips on — Win+Alt+PrntScrn for a shot, Win+Alt+R to record.")


def revert_gamebar_capture(ctx: TaskContext):
    snap = get_tweak_snapshot("gamebar_capture")
    if snap and "specs" in snap:
        _restore_reg_values(ctx, "gamebar_capture")
    else:
        # No snapshot (applied by an older build, or config cleared). Put the
        # capture flags back to the pre-fix state this tweak exists to repair:
        # 0/0 with no policy. A snapshot, when present, always wins.
        reg_set_value_checked(ctx, "HKCU", "System\\GameConfigStore", "GameDVR_Enabled", 0)
        reg_set_value_checked(ctx, "HKCU", "Software\\Microsoft\\Windows\\CurrentVersion\\GameDVR", "AppCaptureEnabled", 0)
        reg_delete_value(ctx, "HKLM", "SOFTWARE\\Policies\\Microsoft\\Windows\\GameDVR", "AllowGameDVR")
        ctx.log("  (no snapshot found — restored the pre-fix Game Bar capture values instead)")


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
    # writes HKCU\...\Desktop\MenuShowDelay (snapshot + 100ms). Each apply
    # snapshots the prior value under ITS OWN task id, so undo restores
    # exactly what that apply overwrote.
    #
    # Residual order-dependence (documented, not fixable without merging the
    # tweaks): undoing both in apply order lands on the true original value;
    # undoing in reverse order lands on the other tweak's output.
    had_snapshot = bool(get_tweak_snapshot("visual_effects"))
    _snap_reg_values(ctx, "visual_effects", _VISUAL_EFFECTS_SPECS)
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
    # Restore every value the snapshot covers, using the type _snap_reg_values
    # recorded for each one (so the two REG_DWORD keys are not written back as
    # REG_SZ). Any value the snapshot does NOT cover -- an older build's
    # one-key snapshot, or a cleared config -- falls back to the documented
    # Windows default rather than being left at this tweak's output.
    snap = get_tweak_snapshot("visual_effects") or {}
    covered = set()
    for spec in (snap.get("specs") or []):
        if isinstance(spec, (list, tuple)) and len(spec) >= 3:
            covered.add((spec[0], spec[1], spec[2]))

    if covered:
        _restore_reg_values(ctx, "visual_effects")

    missing = [s for s in _VISUAL_EFFECTS_SPECS if s not in covered]
    if missing:
        ctx.log("  (%d of %d values had no snapshot — restoring the documented "
                "Windows default instead)" % (len(missing), len(_VISUAL_EFFECTS_SPECS)))
    for hive, path, name in missing:
        value, vtype = _VISUAL_EFFECTS_DEFAULTS[(hive, path, name)]
        if value is None:
            # Windows ships no value here; removing ours restores the default.
            reg_delete_value(ctx, hive, path, name)
        else:
            reg_set_value_checked(ctx, hive, path, name, value,
                                  value_type=vtype)


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
    # M-6 fix: snapshot both values (value OR absence) BEFORE writing, so Undo
    # restores the user's real prior setting instead of blind-deleting it. The
    # B1 units note still applies: RMShaderCacheSize is a REG_DWORD in MB (the
    # NVCP "Shader Cache Size" selector is MB; 10240 MB = 10 GB — the old
    # 10737418240 overflowed the DWORD and silently failed).
    _regn_apply(ctx, "shader_cache_10gb", [
        ("HKCU", "Software\\NVIDIA Corporation\\Global\\FTS", "EnableGR535", 1),
        ("HKCU", "Software\\NVIDIA Corporation\\Global\\FTS", "RMShaderCacheSize", 10240),
    ])
    ctx.log("Shader cache size set to 10 GB (10240 MB) for NVIDIA.")


def revert_shader_cache_10gb(ctx: TaskContext):
    # M-6 fix: restore the snapshotted priors (both values) instead of deleting
    # them — a user who had a custom cache size or EnableGR535 state keeps it.
    _regn_revert(ctx, "shader_cache_10gb", "Shader cache size")


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
        # H1-class quoting fix (kept): shell=False argv + PS single quotes.
        # L-2 fix: run_cmd_checked — the write's exit code used to be discarded,
        # so a genuinely failed PowerMizer write was still badged 'Active'. The
        # `if (Test-Path ...)` guard means a missing FTS key (non-NVIDIA or a
        # driver without it) exits 0 and is NOT treated as a failure; only a
        # real write error raises.
        run_cmd_checked(ctx, ps_argv(
            "if (Test-Path 'HKCU:\\Software\\NVIDIA Corporation\\Global\\FTS\\') { "
            "Set-ItemProperty -Path 'HKCU:\\Software\\NVIDIA Corporation\\Global\\FTS\\' "
            "-Name 'PowerMizerEnable' -Value 0 -ErrorAction Stop }"),
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
    ctx.log("GPU timeout restored to the Windows default (2s).")


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
    # UNDO-002: the snapshot used to be written AFTER ChangeDisplaySettingsW,
    # with none of the had_snapshot/try/clear guard the other 59 applies
    # carry. If the mode change succeeded and the save then failed (or the
    # process died), the display was left changed and unrecorded -- and
    # because save_tweak_snapshot refuses to overwrite, a retry could not
    # repair it. That is the exact BUG-015 pattern this same file fixed
    # everywhere else; see apply_prefer_ipv4's docstring.
    #
    # Record first, mutate second, and drop the record if the mutation fails.
    had_snapshot = bool(get_tweak_snapshot("refresh_rate_fix"))
    save_tweak_snapshot("refresh_rate_fix", {"mode": current})
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
    except BaseException:
        # Nothing was changed, so there is nothing to undo — drop the record
        # we just took unless a previous good one was already on file (which
        # is what makes a failed re-apply retryable rather than destructive).
        if not had_snapshot:
            try:
                clear_tweak_snapshot("refresh_rate_fix")
            except Exception:
                pass
        raise
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


def apply_gpu_preference_high(ctx: TaskContext):
    """Tell Windows to run everything on the dedicated GPU (per-user, no
    admin): sets DirectXUserGlobalSettings' GPU preference to 2. Snapshots
    the exact prior string (usually 'GPUPreference=0;' or empty) for a
    precise revert."""
    ctx.set_status("Setting games to prefer the dedicated GPU...")
    had_snapshot = bool(get_tweak_snapshot("gpu_preference_high"))
    prior = _read_gpu_pref()
    new = "GPUPreference=2;"
    # B-020: snapshot BEFORE the write. The old order wrote the registry, then
    # saved the snapshot; a save failure left the change live with no undo
    # record, and a retry snapshotted the tweak's own output as "before".
    try:
        save_tweak_snapshot("gpu_preference_high", {"value": prior})
        ok = reg_set_value(ctx, "HKCU", _GPU_PREF_PATH, _GPU_PREF_VALUE, new, value_type="REG_SZ")
        if not ok:
            raise RuntimeError("Could not write GPU preference.")
    except Exception:
        if not had_snapshot:
            clear_tweak_snapshot("gpu_preference_high")
        raise
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
        # B-0xx: check the delete. The old code discarded the bool and cleared
        # the snapshot anyway, so a failed delete left GPUPreference=2 live with
        # no undo record — and the next apply then snapshot the tweak's own
        # output as "prior".
        if not reg_delete_value(ctx, "HKCU", _GPU_PREF_PATH, _GPU_PREF_VALUE):
            raise RuntimeError(
                "Could not remove the GPU preference value — snapshot kept so "
                "Undo can be retried.")
    clear_tweak_snapshot("gpu_preference_high")
    ctx.log("GPU preference restored to previous setting.")


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


def verify_disable_game_dvr() -> "bool | None":
    return _verify_all_values([
        ("HKCU", "System\\GameConfigStore", "GameDVR_Enabled", 0),
        ("HKCU", "Software\\Microsoft\\Windows\\CurrentVersion\\GameDVR", "AppCaptureEnabled", 0),
        ("HKLM", "SOFTWARE\\Policies\\Microsoft\\Windows\\GameDVR", "AllowGameDVR", 0),
    ])


def verify_gamebar_capture() -> "bool | None":
    base = _verify_all_values([
        ("HKCU", "System\\GameConfigStore", "GameDVR_Enabled", 1),
        ("HKCU", "Software\\Microsoft\\Windows\\CurrentVersion\\GameDVR", "AppCaptureEnabled", 1),
    ])
    if base is False:
        return False
    # Both capture flags are on, but a lingering AllowGameDVR=0 policy still
    # disables Game DVR machine-wide — capture stays greyed out, so that is
    # drift too. An absent or already-1 policy is fine.
    if __verify_read("HKLM", "SOFTWARE\\Policies\\Microsoft\\Windows\\GameDVR",
                     "AllowGameDVR") == 0:
        return False
    return base


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


def verify_mouse_accel() -> "bool | None":
    return _verify_all_values([
        ("HKCU", "Control Panel\\Mouse", "MouseSpeed", "0"),
        ("HKCU", "Control Panel\\Mouse", "MouseThreshold1", "0"),
        ("HKCU", "Control Panel\\Mouse", "MouseThreshold2", "0"),
    ])


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


def verify_tdr_delay() -> "bool | None":
    return _verify_value("HKLM", "SYSTEM\\CurrentControlSet\\Control\\GraphicsDrivers", "TdrDelay", 8)


def verify_mpo_fix() -> "bool | None":
    return _verify_value("HKLM", "SOFTWARE\\Microsoft\\Windows\\Dwm", "OverlayTestMode", 5)


def verify_fullscreen_opt() -> "bool | None":
    return _verify_value("HKCU", "Software\\Microsoft\\Windows NT\\CurrentVersion\\AppCompatFlags\\Layers",
                         "DISABLEDXMAXIMIZEDWINDOWEDMODE", 1)


def verify_visual_effects() -> "bool | None":
    return _verify_value("HKCU", "Software\\Microsoft\\Windows\\CurrentVersion\\Themes\\Personalize",
                         "EnableTransparency", 0)


def verify_shader_cache_10gb() -> "bool | None":
    """NVIDIA-only: RMShaderCacheSize=10240 (MB, 10 GB)."""
    return _verify_value("HKCU", "Software\\NVIDIA Corporation\\Global\\FTS", "RMShaderCacheSize", 10240)
