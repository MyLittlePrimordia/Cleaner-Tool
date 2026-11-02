"""Quiet watch (audit N-006): the app speaks first, once.

A cleaner you have to remember to open is a cleaner that never runs. This
module is the sentinel behind that idea: four conditions that are worth
interrupting a person for, and the rules that stop it becoming a notification
machine.

Why a separate module instead of three lines in the window: every decision
here is a pure function of readings plus a saved state file. A test can replay
a month of thirty-second ticks - crossing, hovering on the boundary,
recovering, crossing again - without a PC, a Task Scheduler, a clock or a
toast. The `app/ui/app.py` side only supplies the readings and shows what
comes back.

The quiet rules, in the order they save the user:

* One notice per tick, however many conditions are true. Three toasts stacking
  on top of each other is how people turn notifications off forever, so
  `notice_text` folds the list into one sentence with each clause named.
* One notice per EPISODE, not per tick. A drive at 9% is still at 9% thirty
  seconds later; the episode ends only when the reading recovers past the
  threshold plus `FREE_CLEAR_MARGIN` (hysteresis, so a drive hovering exactly
  on the line cannot chatter). A score drop re-arms only when a NEW checkup
  produces it, keyed by that checkup's timestamp. A driver finding re-arms
  when a checkup grades Drivers A or B again. A missed schedule re-arms
  when Windows reports a different run.
* A still-true condition may speak again after `REPEAT_AFTER`, so a machine
  left full for a week is mentioned more than once - but at most daily.
* Nothing is announced while the switch is off, and an unreadable reading is
  never turned into a claim: a schedule that cannot be queried says nothing
  rather than "has not run yet", and a checkup that graded a sensor "not
  measured" is never named as a cause.

The thresholds are settings, not constants: `watch_free_percent`,
`watch_score_drop`, `watch_missed_run` and `watch_enabled`. The defaults (10%
free, a drop of 8 points) are what the audit argued for; the chip in the
status row uses the same free-space number, so what the row marks and what the
notice says can never disagree.
"""
from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import dataclass

from app.config_paths import CONFIG_DIR
from app.config_schema import WATCH_FREE_RANGE, WATCH_SCORE_RANGE

#: Episode memory. Its own tiny file rather than a config key: it is
#: machine-generated, rewritten by a background tick, and losing it costs one
#: repeated notice - none of which is true of the user's settings.
#: B-006: bound at import time, this froze the first CONFIG_DIR forever -
#: a portable install or test that relocated the config dir after import
#: read/wrote two different watch.json files and repeated every notice.
#: Resolved lazily via state_file() now; STATE_FILE stays as a compat alias.
STATE_FILE = CONFIG_DIR / "watch.json"


def state_file():
    """Current episode-memory path, resolved lazily (B-006)."""
    try:
        from app.config_paths import get_config_dir
        return get_config_dir() / "watch.json"
    except Exception:
        return STATE_FILE

#: Percent of a drive that counts as nearly full. 10% is the audit's number
#: (5% is already an emergency), and it is the same threshold the status-row
#: chip uses.
FREE_DEFAULT = 10
FREE_MIN, FREE_MAX = WATCH_FREE_RANGE
#: Hysteresis: a drive must climb this far back above the threshold before the
#: episode is over. Without it a drive sitting at exactly 10.0% would announce
#: itself on every tick.
FREE_CLEAR_MARGIN = 2

#: Points of health score that are worth a notice. One bad sensor can move a
#: score by a few points on its own; 8 is a real step down.
SCORE_DROP_DEFAULT = 8
SCORE_DROP_MIN, SCORE_DROP_MAX = WATCH_SCORE_RANGE

#: How long a condition that is still true waits before it is mentioned again.
REPEAT_AFTER = 24 * 3600
#: Episode keys older than this are forgotten even if nothing cleared them.
KEEP_STATE = 7 * 24 * 3600
#: How often the Task Scheduler is asked. Each query is a PowerShell process
#: (~300 ms), so this is hours, not ticks - and a missed run is not an event
#: that needs second-level latency.
SCHEDULE_EVERY = 6 * 3600

#: One tick at a time. The schedule query can take seconds; a second tick
#: arriving meanwhile must be skipped, not queued.
_busy = threading.Lock()


@dataclass(frozen=True)
class Alert:
    """One condition crossing its threshold, with the screen that fixes it.

    `key` is the EPISODE identity: two readings of the same unbroken episode
    produce the same key, which is what `decide` uses to stay silent. `route`
    names the destination in the app's own vocabulary (the same words the
    Health rows and the Tools cards use) so the log line and a future
    click-through cannot drift from what the UI calls that screen.
    """

    key: str
    title: str
    body: str
    short: str
    route: str


# --------------------------------------------------------------------------- #
# thresholds
# --------------------------------------------------------------------------- #

def _int_in(value, default, lo, hi):
    """A stored integer inside [lo, hi], else the default. bool is not an int.

    Deliberately total: a hand-edited `"watch_free_percent": "10"` or a stale
    `1000` must not decide that every drive is nearly full.
    """
    if isinstance(value, bool) or not isinstance(value, int):
        return default
    return value if lo <= value <= hi else default


def free_threshold(cfg=None):
    """Percent free at which a drive is marked and, once, mentioned."""
    return _int_in((cfg or {}).get("watch_free_percent"), FREE_DEFAULT,
                   FREE_MIN, FREE_MAX)


def clear_threshold(cfg=None):
    """Percent free that ends the episode: threshold plus the hysteresis band."""
    return free_threshold(cfg) + FREE_CLEAR_MARGIN


def score_drop_threshold(cfg=None):
    """Points a checkup may fall before it is worth a notice."""
    return _int_in((cfg or {}).get("watch_score_drop"), SCORE_DROP_DEFAULT,
                   SCORE_DROP_MIN, SCORE_DROP_MAX)


def enabled(cfg=None):
    """The master switch. Missing means on: this is the default experience."""
    value = (cfg or {}).get("watch_enabled")
    return True if value is None else bool(value)


def missed_runs_enabled(cfg=None):
    """Whether a schedule that stopped is worth mentioning."""
    value = (cfg or {}).get("watch_missed_run")
    return True if value is None else bool(value)


def startup_enabled(cfg=None):
    """Whether a program adding itself to startup is worth mentioning.

    On by default: a new login item is exactly the kind of quiet change a
    user would otherwise find only after wondering why sign-in got slower.
    The first reading is a BASELINE and never speaks; only additions after
    that do.
    """
    value = (cfg or {}).get("watch_startup")
    return True if value is None else bool(value)


# --------------------------------------------------------------------------- #
# readings -> alerts
# --------------------------------------------------------------------------- #

def _sensor_title(key):
    """The Health tab's own name for a sensor, so both say the same words."""
    key = str(key or "")
    try:
        from app.health_scan import SENSORS
        for name, title, *_rest in SENSORS:
            if name == key:
                return title
    except Exception:
        pass
    return key.replace("_", " ").strip().title() or "A sensor"


def _drive_alerts(drives, cfg, now):
    """One alert per drive at or below the threshold.

    `drives` is the monitor's own tuple shape - (letter, root, free_gb,
    total_gb) - so the caller passes exactly what it already has on screen, and
    the number in the notice is the number in the status row.
    """
    out = []
    limit = free_threshold(cfg)
    for drive in drives or ():
        try:
            # The monitor hands over a bare letter ("C"); normalise so copy
            # is written in one shape and "C:" from any other caller does not
            # render as "C::".
            letter = str(drive[0]).split(":", 1)[0].strip().upper() or "?"
            free_gb = float(drive[2])
            total_gb = float(drive[3])
        except (IndexError, TypeError, ValueError):
            continue
        if total_gb <= 0 or free_gb < 0:
            continue
        percent = free_gb / total_gb * 100.0
        if percent > limit:
            continue
        out.append(Alert(
            key="drive:%s" % letter,
            route="storage_insight",
            title="%s: is nearly full" % letter,
            body=("%.1f GB free of %.0f GB (%.0f%% free). "
                  "Open Storage Insight to see what is using it."
                  % (free_gb, total_gb, percent)),
            short="%s: only %.1f GB free" % (letter, free_gb),
        ))
    return out


def _score_alert(trend, cfg, now):
    """The latest checkup fell far enough below the one before it.

    `delta` is already "since the last check" in health_history's own words,
    and `points` is the same ledger, so the cause can be recomputed between
    exactly those two checkups. The key carries the newest point's timestamp:
    the same drop therefore speaks once and stays silent until a checkup adds
    a new point, at which time a new drop is genuinely new information.
    """
    if not isinstance(trend, dict):
        return None
    delta = trend.get("delta")
    last = trend.get("last")
    last_ts = trend.get("last_ts")
    if delta is None or last is None or last_ts is None:
        return None
    drop = score_drop_threshold(cfg)
    if delta > -drop:
        return None

    cause = ""
    points = trend.get("points") or []
    if len(points) >= 2:
        try:
            from app import health_history
            causes = health_history.causes_between(points[-2], points[-1])
        except Exception:
            causes = []
        if causes:
            top = causes[0]
            cause = " %s fell from %s to %s." % (
                _sensor_title(top.get("key")), top.get("from"), top.get("to"))

    return Alert(
        key="score:%d" % int(float(last_ts)),
        route="health",
        title="Health score dropped %d points" % abs(int(delta)),
        body=("%d now, down from %d at the previous checkup.%s Open Health to "
              "act on it." % (int(last), int(last) - int(delta), cause)),
        short="health score down %d" % abs(int(delta)),
    )


#: Health grades that mean the Drivers sensor found something worth a
#: notice. B is one old driver - explicitly not worth interrupting anyone
#: for - and "?" (not measured) is never stored in a trend point, so it
#: cannot arrive here at all. The sensor itself grades no worse than D,
#: but F stays in the tuple so a future harsher grade cannot slip through
#: silently.
DRIVER_ALERT_GRADES = ("C", "D", "F")


def _drivers_alert(trend, cfg, now):
    """A checkup that graded Drivers C or worse: missing or stale drivers.

    The fourth quiet-watch condition (N-004). It reads the verdict the
    Health tab already measured - the latest trend point's "drivers"
    finding - so the tick costs no probe: no PowerShell, no three-second
    driver query, just the config the trend already keeps.

    A point that never measured drivers (no key: "?" grades are not
    stored) says nothing rather than "all clear", matching the module's
    rule that an unreadable reading is never turned into a claim.

    The episode key is stable ("drivers"), drive-style rather than
    timestamped: while checkup after checkup keeps grading C/D, nothing
    new is said, and the episode ends when a checkup grades A or B again.
    Governed by the master `watch_enabled` switch only - one more
    condition, not one more setting.
    """
    if not isinstance(trend, dict):
        return None
    points = trend.get("points") or []
    if not points or not isinstance(points[-1], dict):
        return None
    findings = points[-1].get("f")
    if not isinstance(findings, dict):
        return None
    grade = findings.get("drivers")
    grade = grade.strip().upper() if isinstance(grade, str) else ""
    if grade not in DRIVER_ALERT_GRADES:
        return None
    return Alert(
        key="drivers",
        route="drivers",
        title="Driver issues found at the last checkup",
        body=("The last health check graded Drivers %s - a device with no "
              "driver installed, or drivers over 3 years old. Open the "
              "Drivers tab to look them up and install newer ones."
              % grade),
        short="driver issues found",
    )


def _schedule_alert(schedule, cfg, now):
    """A schedule Windows itself says has gone stale or failed.

    `schedule` is a dict the caller built from `scheduler.describe_schedule`
    (plus `kind` and the run timestamps). A missing or empty dict is "nothing
    known", never "fine" - and also never "broken": an unreadable task must not
    produce a claim, which is why the caller only passes a verdict it measured.
    """
    if not isinstance(schedule, dict) or not schedule:
        return None
    if not missed_runs_enabled(cfg):
        return None
    stale = bool(schedule.get("stale"))
    failed = bool(schedule.get("failed"))
    if not (stale or failed):
        return None
    kind = str(schedule.get("kind") or "clean")
    what = {"update": "app updates",
            "save": "save backup"}.get(kind, "maintenance")
    text = str(schedule.get("text") or "").strip()
    if failed:
        title = "The last scheduled %s run failed" % what
        short = "last scheduled %s run failed" % what
    else:
        title = "Scheduled %s has stopped running" % what
        short = "scheduled %s is overdue" % what
    body = text + (" " if text and not text.endswith(".") else "")
    body += "Open Settings \u2192 Maintenance to check the schedule."
    stamp = schedule.get("last_run") or schedule.get("next_run") or 0
    return Alert(
        key="schedule:%s:%s" % (kind, int(float(stamp or 0))),
        route="settings",
        title=title,
        body=body,
        short=short,
    )


def _startup_alerts(startup, cfg, now):
    """One alert per startup item added since the last baseline.

    `startup` is the LIST OF NEW ITEMS computed by `_startup_new`, not the
    whole startup list: `evaluate` stays pure and the baseline lives in the
    episode state. A missing/empty list is no news, never "nothing starts
    with Windows" (that is a separate fact, and not one worth a toast).
    """
    if not startup_enabled(cfg):
        return []
    out = []
    for item in startup or ():
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        item_id = str(item.get("id") or name).strip()
        if not name or not item_id:
            continue
        shown = name if len(name) <= 80 else name[:77] + "\u2026"
        out.append(Alert(
            key="startup:%s" % item_id,
            route="startup",
            title="\u201c%s\u201d was added to startup" % shown,
            body=("A program added itself to the startup list, so it may "
                  "now run whenever you sign in. Open Startup Manager to "
                  "see it and turn it off if you do not need it."),
            short="%s added to startup" % shown,
        ))
    return out


def startup_snapshot():
    """Enabled startup items as [{id, name}], or None when unreadable.

    Read on the caller's worker thread. Disabled items are excluded because
    they do not launch; a row Windows has switched off is not new cost at
    sign-in. `list_startup_items` has its own per-source error handling and
    returns [] rather than raising, so a broken registry view is an empty
    reading here too - and an empty reading never establishes a baseline.
    """
    try:
        from app.startup_manager import list_startup_items
        items = list_startup_items()
    except Exception:
        return None
    out = []
    for item in items or ():
        if not isinstance(item, dict) or not item.get("enabled"):
            continue
        item_id = str(item.get("id") or "").strip()
        name = str(item.get("name") or "").strip()
        if item_id and name:
            out.append({"id": item_id, "name": name})
    return out


def _startup_new(startup, state):
    """New enabled items since `state['startup_known']`; updates the memory.

    The first non-empty reading establishes the baseline and returns []. The
    known list is replaced with the current reading, so a removed item is
    forgotten and a later re-add is a genuinely new event.
    """
    if not startup:
        return []
    current = []
    seen = set()
    for item in startup:
        if not isinstance(item, dict):
            continue
        item_id = str(item.get("id") or "").strip()
        name = str(item.get("name") or "").strip()
        if not item_id or not name or item_id in seen:
            continue
        seen.add(item_id)
        current.append({"id": item_id, "name": name})
    if not current:
        return []
    known = state.get("startup_known")
    ids = [item["id"] for item in current]
    if not isinstance(known, list):
        state["startup_known"] = ids
        return []
    known_set = {str(x) for x in known}
    new = [item for item in current if item["id"] not in known_set]
    state["startup_known"] = ids
    return new


def evaluate(*, drives=(), trend=None, schedule=None, startup=None,
             cfg=None, now=None):
    """Every condition currently true, as alerts. Pure; no I/O, no state.

    Kept separate from `decide` so the thresholds and the wording can be
    asserted on their own, without an episode history. Order is stable:
    drives by the caller's own order, then the score, drivers, schedule,
    then startup additions.
    """
    now = time.time() if now is None else float(now)
    alerts = _drive_alerts(drives, cfg, now)
    score = _score_alert(trend, cfg, now)
    if score is not None:
        alerts.append(score)
    drivers = _drivers_alert(trend, cfg, now)
    if drivers is not None:
        alerts.append(drivers)
    sched = _schedule_alert(schedule, cfg, now)
    if sched is not None:
        alerts.append(sched)
    alerts.extend(_startup_alerts(startup, cfg, now))
    return alerts


# --------------------------------------------------------------------------- #
# episode memory
# --------------------------------------------------------------------------- #

def load_state():
    """The saved episode memory, or {} when there is none to read.

    Tolerant on purpose: this file is disposable. A corrupt one costs one
    repeated notice, which must never be worth an error dialog.
    """
    try:
        with open(state_file(), "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_state(state):
    """Write the episode memory atomically; best-effort like the read."""
    try:
        _sf = state_file()
        _sf.parent.mkdir(parents=True, exist_ok=True)
        tmp = _sf.with_name(_sf.name + ".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(state, f)
        os.replace(tmp, _sf)
    except Exception:
        pass


def decide(alerts, state=None, now=None):
    """Split `alerts` into what to say now and the memory to keep.

    Returns (to_speak, new_state). The memory holds only conditions that are
    true RIGHT NOW: a drive that recovered, or a checkup that has been
    superseded, drops out, which is what re-arms its episode. A condition that
    is active but was already announced keeps its original stamp, so
    `REPEAT_AFTER` measures from the first mention rather than sliding forward
    on every tick.
    """
    now = time.time() if now is None else float(now)
    old = dict((state or {}).get("announced") or {})
    speak = []
    active = {}
    for alert in alerts or ():
        try:
            stamp = float(old.get(alert.key))
        except (TypeError, ValueError):
            stamp = None
        if stamp is None or (now - stamp) >= REPEAT_AFTER:
            speak.append(alert)
            active[alert.key] = now
        else:
            active[alert.key] = stamp
    keep = {}
    for key, stamp in active.items():
        try:
            if now - float(stamp) < KEEP_STATE:
                keep[key] = stamp
        except (TypeError, ValueError):
            continue
    new_state = dict(state or {})
    new_state["announced"] = keep
    # Drop keys a previous build wrote that this one no longer understands, so
    # the file can never grow an unbounded museum of condition names.
    new_state.pop("version", None)
    return speak, new_state


def notice_text(alerts):
    """(title, body) for ONE notification covering every alert, or None.

    One is the whole design: three separate toasts about the same moment is
    the pattern users mute. With several conditions the title carries the
    count and the body names each one, which is enough to decide whether to
    open the app now.
    """
    alerts = [a for a in (alerts or ()) if a is not None]
    if not alerts:
        return None
    if len(alerts) == 1:
        return alerts[0].title, alerts[0].body
    return (
        "Cleaner Tool noticed %d things" % len(alerts),
        "  \u00b7  ".join(a.short for a in alerts),
    )


# --------------------------------------------------------------------------- #
# the tick
# --------------------------------------------------------------------------- #

def _trend():
    try:
        from app import health_history
        return health_history.trend()
    except Exception:
        return {}


def _schedule_verdict(cfg, state, now):
    """Ask Windows about the enabled schedule(s), at most every 6 hours.

    Returns None when nothing is due to be asked, nothing is wrong, or the
    task cannot be read. Only a measured stale/failed verdict is returned; the
    timestamp of the query is stamped into `state` so the next tick does not
    spawn another PowerShell process.
    """
    wanted = []
    if cfg.get("schedule_enabled"):
        wanted.append(("clean", None))
    if cfg.get("schedule_update_enabled"):
        wanted.append(("update", ["--auto-update"]))
    # The save-backup schedule is its own task, so its failure is its own
    # verdict: a week of failed backups must not stay silent just because
    # Auto Clean happened to run fine.
    if cfg.get("schedule_saves_enabled"):
        wanted.append(("save", ["--auto-save-backup"]))
    if not wanted or not missed_runs_enabled(cfg):
        return None
    try:
        last = float(state.get("schedule_checked") or 0)
    except (TypeError, ValueError):
        last = 0.0
    if now - last < SCHEDULE_EVERY:
        return None
    state["schedule_checked"] = now
    try:
        from app import scheduler
    except Exception:
        return None
    frequency = str(cfg.get("schedule_frequency") or "weekly")
    for kind, extra in wanted:
        try:
            info = scheduler.schedule_info(extra)
        except Exception:
            continue
        if not info:
            # Unreadable: say nothing. "Has not run yet" would be a claim about
            # a task we failed to look at.
            continue
        try:
            desc = scheduler.describe_schedule(info, frequency=frequency,
                                              now=now)
        except Exception:
            continue
        if desc.get("stale") or desc.get("failed"):
            out = dict(desc)
            out["kind"] = kind
            out["last_run"] = info.get("last_run")
            out["next_run"] = info.get("next_run")
            return out
    return None


def check(*, drives=(), cfg=None, trend=None, schedule=None, startup=None,
          now=None):
    """One tick: read, decide, remember, return what is worth saying.

    Blocking (it may spawn the scheduler query), so callers run it off the Tk
    thread. `cfg`, `trend`, `schedule` and `startup` are injectable for tests;
    passing `schedule={}` means "there is no verdict", which skips the query.
    Passing `startup=None` means "this caller does not read startup items" -
    the GUI passes `watch.startup_snapshot()`, so a unit test never alerts on
    the machine's real login entries.

    Returns a list of Alerts - usually empty, which is the point.
    """
    if not _busy.acquire(blocking=False):
        return []
    try:
        now = time.time() if now is None else float(now)
        if cfg is None:
            try:
                from app.config_persist import load_config
                cfg = load_config()
            except Exception:
                cfg = {}
        if not enabled(cfg):
            return []
        state = load_state()
        # `_schedule_verdict` and `_startup_new` mutate `state` in place, so
        # comparing the post-decide copy to the already-mutated state would
        # say "unchanged" and never persist a new baseline or the schedule
        # probe timestamp. Keep the pre-mutation copy for the write test.
        before = dict(state)
        if trend is None:
            trend = _trend()
        if schedule is None:
            schedule = _schedule_verdict(cfg, state, now)
        new_startup = _startup_new(startup, state) if startup is not None else []
        alerts = evaluate(drives=drives, trend=trend, schedule=schedule,
                          startup=new_startup, cfg=cfg, now=now)
        speak, new_state = decide(alerts, state, now=now)
        if new_state != before:
            save_state(new_state)
        return speak
    finally:
        try:
            _busy.release()
        except Exception:
            pass
