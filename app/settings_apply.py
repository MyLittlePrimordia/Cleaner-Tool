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
#: Window animations: follow the OS setting, or force it for this app.
ANIMATION_MODES = ("system", "on", "off")


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
      window_animations str  system (follow Windows) | on | off
      ui_scale        str    system (display + 125% floor) | 100..200
      watch_enabled   bool   N-006: quiet watch may notify at all
      watch_free      int    percent free that marks a drive
      watch_drop      int    health-score points worth a notice
      watch_missed    bool   mention a schedule that stopped or failed

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
    if "quarantine_days" in v:
        # The dropdown carries the schema's numbers as ints; anything else is
        # "keep until emptied" rather than a number nobody can see.
        try:
            days = int(v["quarantine_days"])
        except (TypeError, ValueError):
            days = 0
        from app.config_schema import QUARANTINE_DAYS
        plain["quarantine_days"] = days if days in QUARANTINE_DAYS else 0
    if "window_animations" in v and str(v["window_animations"]) in ANIMATION_MODES:
        plain["window_animations"] = str(v["window_animations"])
    # --- quiet watch (N-006) --------------------------------------------
    # Clamped here as well as in the schema so what is written is what the
    # Settings row said; a value outside the range falls back to the watch
    # module's own default instead of silently becoming the nearest edge.
    if "watch_enabled" in v:
        plain["watch_enabled"] = bool(v["watch_enabled"])
    if "watch_missed" in v:
        plain["watch_missed_run"] = bool(v["watch_missed"])
    if "watch_free" in v:
        from app import watch as _watch
        try:
            chosen = int(v["watch_free"])
        except (TypeError, ValueError):
            chosen = _watch.FREE_DEFAULT
        plain["watch_free_percent"] = (
            chosen if _watch.FREE_MIN <= chosen <= _watch.FREE_MAX
            else _watch.FREE_DEFAULT)
    if "watch_drop" in v:
        from app import watch as _watch
        try:
            chosen = int(v["watch_drop"])
        except (TypeError, ValueError):
            chosen = _watch.SCORE_DROP_DEFAULT
        plain["watch_score_drop"] = (
            chosen if _watch.SCORE_DROP_MIN <= chosen <= _watch.SCORE_DROP_MAX
            else _watch.SCORE_DROP_DEFAULT)
    if "ui_scale" in v:
        # The dropdown stores the bare percentage ("150"); tolerate a hand-set
        # "150%" but never write a value the schema would reject on next load.
        from app.config_schema import UI_SCALES
        scale = str(v["ui_scale"] or "").strip().lower()
        if scale.endswith("%"):
            scale = scale[:-1].strip()
        if scale in UI_SCALES:
            plain["ui_scale"] = scale
    if plain:
        try:
            def _mut(c, _p=plain):
                c.update(_p)
            update_config(_mut)
            if "close_to_tray_enabled" in plain:
                results.append("Close to tray on" if
                               plain["close_to_tray_enabled"]
                               else "Close to tray off")
            if "watch_enabled" in plain:
                results.append("Quiet watch on" if plain["watch_enabled"]
                               else "Quiet watch off")
            if "quarantine_days" in plain:
                results.append(
                    "Quarantine keeps files for %d days" % plain["quarantine_days"]
                    if plain["quarantine_days"] else
                    "Quarantine keeps files until you empty it")
            if "window_animations" in plain:
                results.append("Window animations " + {
                    "system": "follow Windows",
                    "on": "on",
                    "off": "off",
                }[plain["window_animations"]])
            if "ui_scale" in plain and str(
                    cfg.get("ui_scale", "system")) != plain["ui_scale"]:
                # Honest about WHEN it applies: Tk's scale is read before the
                # first widget exists, so a live change would reflow the page
                # under the user's cursor. Only when it actually changed - a
                # Save for an unrelated toggle must not keep asking for a
                # restart.
                results.append(
                    "Interface scale: " + (
                        "Automatic" if plain["ui_scale"] == "system"
                        else plain["ui_scale"] + "%") +
                    " (restart to apply)")
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
