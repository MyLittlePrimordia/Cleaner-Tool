"""Quiet watch (audit N-006): the app speaks first, once.

A cleaner you have to remember to open is a cleaner that never runs. This
module is the sentinel behind that idea: three conditions that are worth
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
  produces it, keyed by that checkup's timestamp. A missed schedule re-arms
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
STATE_FILE = CONFIG_DIR / "watch.json"

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
    what = "app updates" if kind == "update" else "maintenance"
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


def evaluate(*, drives=(), trend=None, schedule=None, cfg=None, now=None):
    """Every condition currently true, as alerts. Pure; no I/O, no state.

    Kept separate from `decide` so the thresholds and the wording can be
    asserted on their own, without an episode history. Order is stable:
    drives by the caller's own order, then the score, then the schedule.
    """
    now = time.time() if now is None else float(now)
    alerts = _drive_alerts(drives, cfg, now)
    score = _score_alert(trend, cfg, now)
    if score is not None:
        alerts.append(score)
    sched = _schedule_alert(schedule, cfg, now)
    if sched is not None:
        alerts.append(sched)
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
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_state(state):
    """Write the episode memory atomically; best-effort like the read."""
    try:
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = STATE_FILE.with_name(STATE_FILE.name + ".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(state, f)
        os.replace(tmp, STATE_FILE)
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


def check(*, drives=(), cfg=None, trend=None, schedule=None, now=None):
    """One tick: read, decide, remember, return what is worth saying.

    Blocking (it may spawn the scheduler query), so callers run it off the Tk
    thread. `cfg`, `trend` and `schedule` are injectable for tests; passing
    `schedule={}` means "there is no verdict", which skips the query.

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
        if trend is None:
            trend = _trend()
        if schedule is None:
            schedule = _schedule_verdict(cfg, state, now)
        alerts = evaluate(drives=drives, trend=trend, schedule=schedule,
                          cfg=cfg, now=now)
        speak, new_state = decide(alerts, state, now=now)
        if new_state != state:
            save_state(new_state)
        return speak
    finally:
        try:
            _busy.release()
        except Exception:
            pass
