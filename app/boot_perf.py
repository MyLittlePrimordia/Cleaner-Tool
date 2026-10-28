"""Boot timings and startup cost, read from Windows' own measurements.

U-002 / N-004. Startup Manager could list what launches at sign-in but not how
much any of it costs, which is the only number that makes a disable decision
reasonable. Windows already measures this: the
`Microsoft-Windows-Diagnostics-Performance/Operational` channel records every
boot (event 100) and, when an app / service / driver delays startup beyond the
normal threshold, the exact offender (events 101-110).

Nothing here estimates. A machine whose channel is unreadable (typically a
non-elevated process) returns `ok=False` with a reason, and the UI says it has
no measurement rather than inventing a rating. Event data is parsed by Data
NAME, not by position, so a Windows update reordering the fields cannot
silently turn a boot time into a version string.

Tk-free: the dialog decides how to draw this.
"""
from __future__ import annotations

import calendar
import datetime
import re
import sys
import xml.etree.ElementTree as ET

CHANNEL = "Microsoft-Windows-Diagnostics-Performance/Operational"
IS_WINDOWS = sys.platform.startswith("win")

#: Event ids this module understands.
BOOT_EVENT = 100          # a completed boot, with its duration broken down
SLOW_BOOT_EVENT = 101     # boot itself exceeded the degradation threshold
SLOW_APP_EVENT = 102      # an application delayed startup
SLOW_SERVICE_EVENT = 103  # a service delayed startup
SLOW_DRIVER_EVENT = 109   # a device/driver delayed startup
_OFFENDER_EVENTS = (SLOW_BOOT_EVENT, SLOW_APP_EVENT, SLOW_SERVICE_EVENT,
                    SLOW_DRIVER_EVENT)

#: How many boots to ask Windows for. Five is enough to show "was it always
#: this slow" without turning a dialog header into a log browser.
BOOT_SAMPLE = 5

#: How many offender events. Startup apps are the common case and a deep boot
#: can log dozens; 60 covers a bad machine without a 10-second query.
OFFENDER_SAMPLE = 60

_TIMEOUT = 20


# --------------------------------------------------------------------------- #
# Query + parse (pure enough to test with fixture XML)
# --------------------------------------------------------------------------- #
def _query_argv(event_ids, count):
    """wevtutil argv. The XPath keeps the channel's own noise out of the parse."""
    selector = " or ".join("EventID=%d" % int(i) for i in event_ids)
    xpath = "*[System[(%s)]]" % selector
    from app.exe_paths import resolve_exe
    return [resolve_exe("wevtutil"), "qe", CHANNEL, "/q:" + xpath,
            "/c:%d" % int(count), "/rd:true", "/f:xml"]


def _query_xml(event_ids, count):
    if not IS_WINDOWS:
        return "", "not Windows"
    try:
        from app.utils import run_capture
        rc, out = run_capture(_query_argv(event_ids, count), timeout=_TIMEOUT)
    except Exception as exc:
        return "", str(exc)
    if rc != 0:
        return "", (out or "").strip() or "Windows did not return boot events"
    return out or "", ""


def _local(tag):
    """The local name of a namespaced tag ('{...}Data' -> 'Data')."""
    return (tag or "").rsplit("}", 1)[-1]


def _text(value):
    return str(value if value is not None else "").strip()


def _int(value):
    try:
        return int(float(str(value).strip()))
    except (TypeError, ValueError, OverflowError):
        return None


def _event_time(system_time):
    """'2026-10-25T03:00:00.1234567Z' -> Unix seconds, or None.

    Windows always writes this field in UTC with a Z, so no locale or timezone
    parsing is involved; a value without a timezone is rejected rather than
    guessed at.
    """
    text = _text(system_time)
    if not text:
        return None
    head = text[:19]
    try:
        stamp = datetime.datetime.strptime(head, "%Y-%m-%dT%H:%M:%S")
    except ValueError:
        return None
    if text.endswith("Z"):
        return float(calendar.timegm(stamp.timetuple()))
    return None


def parse_events(xml_text):
    """wevtutil XML -> [{id, t, data}], newest first. Malformed rows dropped.

    `data` is keyed by the Data element's `Name`, with a positional fallback
    only when the event carries no names at all (some providers do that).
    """
    text = _text(xml_text)
    if not text:
        return []
    try:
        root = ET.fromstring("<events>%s</events>" % text)
    except ET.ParseError:
        return []
    out = []
    for event in root:
        if _local(event.tag) != "Event":
            continue
        event_id = None
        stamp = None
        data = {}
        position = 0
        for node in event.iter():
            name = _local(node.tag)
            if name == "EventID":
                event_id = _int(node.text)
            elif name == "TimeCreated":
                stamp = _event_time(node.get("SystemTime"))
            elif name == "Data":
                key = _text(node.get("Name")) or ("arg%d" % position)
                data[key] = _text(node.text)
                position += 1
        if event_id is None:
            continue
        out.append({"id": event_id, "t": stamp, "data": data})
    out.sort(key=lambda e: e["t"] or 0.0, reverse=True)
    return out


#: The Data names Windows uses for the boot breakdown. They are stable across
#: Windows 10 and 11, but a missing one is reported as missing, not as 0.
_BOOT_FIELDS = {
    "boot": ("BootTime", "BootDuration"),
    "main": ("MainPathBootTime",),
    "post": ("BootPostBootTime",),
}


def _first(data, names):
    for name in names:
        if name in data:
            value = _int(data[name])
            if value is not None:
                return value
    return None


def parse_boots(events):
    """Event 100 rows -> [{t, boot_ms, main_ms, post_ms}], newest first."""
    boots = []
    for event in events:
        if event.get("id") != BOOT_EVENT:
            continue
        data = event.get("data") or {}
        boot_ms = _first(data, _BOOT_FIELDS["boot"])
        if boot_ms is None:
            continue
        boots.append({
            "t": event.get("t"),
            "boot_ms": boot_ms,
            "main_ms": _first(data, _BOOT_FIELDS["main"]),
            "post_ms": _first(data, _BOOT_FIELDS["post"]),
        })
    boots.sort(key=lambda b: b.get("t") or 0.0, reverse=True)
    return boots


def parse_offenders(events):
    """Slow-startup events -> {stem: {ms, event, label}}.

    The key is a lowercase file stem so a Run value pointing at
    ``C:\\...\\Steam.exe`` matches an event naming ``steam.exe``. Only the
    slowest measurement per stem is kept: one app can be flagged on several
    boots, and the worst one is the number worth showing.
    """
    out = {}
    for event in events:
        if event.get("id") not in _OFFENDER_EVENTS:
            continue
        data = event.get("data") or {}
        ms = _first(data, ("TotalTime", "BootTime", "DegradationTime"))
        if ms is None:
            continue
        stem = _stem(data.get("FileName")) or _stem(data.get("Path"))
        if not stem:
            # A service or task has no executable path in the event; its
            # display name IS the identity, so it is used as the key rather
            # than dropping the measurement.
            stem = _text(data.get("Name")).lower()
        if not stem:
            continue
        prev = out.get(stem)
        if prev is None or ms > prev["ms"]:
            out[stem] = {"ms": ms, "event": event.get("id"),
                         "label": _text(data.get("Name")) or stem}
    return out


def _stem(value):
    """'C:\\Program Files\\X\\Steam.exe --silent' -> 'steam'."""
    text = _text(value)
    if not text:
        return ""
    match = re.search(r"([^\\/\s\"']+)\.(?:exe|lnk)\b", text, re.I)
    if match:
        return match.group(1).lower()
    return ""


def boots(limit=BOOT_SAMPLE):
    """The newest boots Windows recorded. [] when unavailable."""
    xml, _err = _query_xml((BOOT_EVENT,), max(1, int(limit)))
    return parse_boots(parse_events(xml))[:max(1, int(limit))]


def offenders():
    """Measured slow starters. {} when unavailable."""
    xml, _err = _query_xml(_OFFENDER_EVENTS, OFFENDER_SAMPLE)
    return parse_offenders(parse_events(xml))


def summary():
    """Everything the Startup Manager header needs.

    ``ok`` is about THIS READ, never about the machine: an unreadable channel
    means "not measured", and the UI must not render that as "all good".
    """
    xml, err = _query_xml((BOOT_EVENT,) + _OFFENDER_EVENTS,
                          BOOT_SAMPLE + OFFENDER_SAMPLE)
    events = parse_events(xml)
    rows = parse_boots(events)
    impact = parse_offenders(events)
    if not rows and not impact:
        return {"ok": False, "error": err or "Windows recorded no boot events",
                "boots": [], "impact": {}, "last_ms": None, "average_ms": None}
    average = None
    if rows:
        average = int(round(sum(b["boot_ms"] for b in rows) / float(len(rows))))
    return {"ok": True, "error": "", "boots": rows[:BOOT_SAMPLE],
            "impact": impact,
            "last_ms": rows[0]["boot_ms"] if rows else None,
            "average_ms": average}


def format_ms(value):
    """'24.3 s' for a boot, '1.4 s' for an app. None -> 'not measured'."""
    ms = _int(value)
    if ms is None or ms < 0:
        return "not measured"
    if ms < 1000:
        return "%d ms" % ms
    return "%.1f s" % (ms / 1000.0)


def impact_for(item, impact):
    """The measured startup cost for one `list_startup_items()` row, or None.

    Matching uses the command line's exe/lnk stem first and the visible name
    second, because a Run value's name and its executable often differ
    ("Steam Client Bootstrapper" runs Steam.exe).
    """
    if not item or not isinstance(impact, dict) or not impact:
        return None
    for raw in (item.get("command"), item.get("name")):
        stem = _stem(raw)
        if stem and stem in impact:
            return impact[stem]
    name = _text(item.get("name")).lower()
    if name and name in impact:
        return impact[name]
    return None


__all__ = [
    "BOOT_EVENT", "CHANNEL", "boots", "format_ms", "impact_for", "offenders",
    "parse_boots", "parse_events", "parse_offenders", "summary",
]
