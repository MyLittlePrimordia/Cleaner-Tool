"""Apply the user's settings: one implementation, shared by every screen.

The scheduling / auto-elevate / tray logic used to live inside the body of the
"Auto Maintenance" dialog's Apply handler, mixed with the message boxes that
report its failures. The Settings tab needs exactly the same behaviour, and
two copies of "create a scheduled task, then fix up the elevation helper" is
how one of them drifts. This module is that logic with the UI taken out: it
RETURNS what happened and the caller decides how to show it.

Tk-free. Every step is independent and guarded - one failing (say, the task
scheduler refusing a task) never stops the others from being saved.
"""
from __future__ import annotations

from typing import Dict, List, Tuple

HEALTH_MODES = ("off", "startup", "daily", "weekly")


def apply_settings(values: Dict[str, object],
                   config: dict = None) -> Tuple[List[str], List[str]]:
    """Apply `values`; return (results, errors).

    values (all optional; a missing key leaves that setting alone):
      clean_schedule  bool   Auto Clean on a schedule
      update_apps     bool   `winget upgrade --all` on the schedule
      frequency       str    daily | weekly | monthly
      time            str    HH:MM (24h)
      auto_elevate    bool   approve admin once, no UAC at launch
      start_with_windows bool
      close_to_tray   bool
      health_mode     str    off | startup | daily | weekly
      drivers_folder  str    '' = the default folder next to the exe

    `results` are short past-tense phrases for a status line; `errors` are
    things the user must be told about (a task that could not be created).
    """
    from app.config_persist import load_config, update_config
    cfg = config if config is not None else load_config()
    results: List[str] = []
    errors: List[str] = []
    v = values

    # --- scheduled maintenance ------------------------------------------
    if "clean_schedule" in v or "update_apps" in v:
        try:
            from app.scheduler import disable_schedule, enable_schedule
            freq = str(v.get("frequency") or cfg.get("schedule_frequency",
                                                     "weekly"))
            when = str(v.get("time") or cfg.get("schedule_time", "03:00"))
            if "clean_schedule" in v:
                if v["clean_schedule"]:
                    ok, msg = enable_schedule(freq, when)
                    (results if ok else errors).append(
                        "Maintenance enabled" if ok else
                        "Could not create the maintenance task: %s" % msg)
                else:
                    ok, msg = disable_schedule()
                    (results if ok else errors).append(
                        "Maintenance disabled" if ok else
                        "Could not remove the maintenance task: %s" % msg)
            if "update_apps" in v:
                if v["update_apps"]:
                    ok, msg = enable_schedule(freq, when,
                                              extra_args=["--auto-update"])
                    (results if ok else errors).append(
                        "App updates enabled" if ok else
                        "Could not create the update task: %s" % msg)
                else:
                    ok, _msg = disable_schedule(["--auto-update"])
                    if ok:
                        results.append("App updates disabled")
        except Exception as exc:                      # noqa: BLE001
            errors.append("Scheduling failed: %s" % exc)

    # --- start as administrator -----------------------------------------
    if "auto_elevate" in v:
        try:
            from app import elevated_launch as el
            from app.elevation import is_admin
            want = bool(v["auto_elevate"])
            el.save_gate_prefs(want, bool(cfg.get("remember_limited", False)))
            if is_admin():
                ok, msg = el.reconcile()
                results.append(("Start as admin on" if want else
                                "Start as admin off") if ok else
                               "Start as admin flag saved (task: %s)" % msg)
            elif want:
                results.append("Start as admin will turn on the next time "
                               "you run as administrator")
            else:
                try:
                    el.delete_task()
                except Exception:
                    pass
                results.append("Start as admin off")
        except Exception as exc:                      # noqa: BLE001
            results.append("Start as admin saved (%s)" % exc)

    # --- start with Windows ---------------------------------------------
    if "start_with_windows" in v:
        try:
            from app.scheduler import disable_startup_tray, enable_startup_tray
            if v["start_with_windows"]:
                ok, msg = enable_startup_tray()
                (results if ok else errors).append(
                    "Start with Windows on" if ok else
                    "Start with Windows failed: %s" % msg)
            else:
                ok, msg = disable_startup_tray()
                (results if ok else errors).append(
                    "Start with Windows off" if ok else
                    "Could not turn off Start with Windows: %s" % msg)
        except Exception as exc:                      # noqa: BLE001
            errors.append("Start with Windows failed: %s" % exc)

    # --- plain config values --------------------------------------------
    plain = {}
    if "close_to_tray" in v:
        plain["close_to_tray_enabled"] = bool(v["close_to_tray"])
    if "health_mode" in v and str(v["health_mode"]) in HEALTH_MODES:
        plain["health_check_mode"] = str(v["health_mode"])
    if "drivers_folder" in v:
        plain["drivers_folder"] = str(v["drivers_folder"] or "").strip()
    if "quarantine_enabled" in v:
        plain["quarantine_enabled"] = bool(v["quarantine_enabled"])
    if plain:
        try:
            def _mut(c, _p=plain):
                c.update(_p)
            update_config(_mut)
            if "close_to_tray_enabled" in plain:
                results.append("Close to tray on" if
                               plain["close_to_tray_enabled"]
                               else "Close to tray off")
        except Exception as exc:                      # noqa: BLE001
            errors.append("Could not save settings: %s" % exc)
    return results, errors


def resync_flags() -> None:
    """Re-read the scheduler's real state back into the config flags so the
    toggles reflect what Windows actually has (a task removed by hand must
    not still read 'On'). Best effort."""
    try:
        from app.scheduler import resync_schedule_flag, resync_startup_flag
        resync_schedule_flag()
        resync_schedule_flag(["--auto-update"])
        resync_startup_flag()
    except Exception:
        pass
