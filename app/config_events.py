"""Append-only local security/event log.

H2 (cycle break). `log_security_event` used to live in `app.config_persist`,
and `app.utils` imported it from there — one of the two edges that closed
the repository's only import cycle:

    utils -> config_persist -> tab_presets -> tasks -> utils

The event log is observability, not configuration, and it needs nothing from
the config loader except where to write. Splitting it out lets `app.utils`
depend on a leaf module instead of the 930-line config module.

`app.config_persist` re-exports `log_security_event`, so the four existing
callers (`app.elevation`, `app.gui`, `app.tasks.repair_tasks`) keep importing
it from where they always have.

Import direction: depends only on `app.config_paths`.
"""
from __future__ import annotations

import os
import threading

from app.config_paths import CONFIG_DIR, EVENTS_LOG_FILE

_EVENTS_LOCK = threading.Lock()


#: LOW-009. Size alone triggers the trim; the line cap is the RESULT, not a
#: second gate. Gating on both meant a >512 KB log of long messages (which is
#: what this app writes) never trimmed AND re-read the whole file on every
#: single append, because the size condition stayed true forever.
_EVENTS_MAX_BYTES = 512_000
#: The line cap is applied by app.logsafe once the size trigger fires
#: (logsafe._TRAIL_KEEP_LINES = 2000); only the size gates the work here.


def log_security_event(category: str, message: str) -> None:
    """Append one timestamped line to the local event log.

    Best-effort and never raises — this is observability, not a control
    path, so a failure here must never affect the caller's actual
    operation. The threading lock below serializes this process; the append
    itself takes an inter-process lock, so the GUI, its elevated twin and a
    scheduled run cannot interleave half a line or lose one to the trim.
    """
    try:
        from datetime import datetime
        stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        line = f"[{stamp}] [{category}] {message}\n"
        with _EVENTS_LOCK:
            try:
                os.makedirs(CONFIG_DIR, exist_ok=True)
            except Exception:
                pass
            # H-002: one append path, shared with the debug trails.
            # `append_text_no_follow` takes an inter-PROCESS lock (the
            # threading.Lock above only serializes this process: the GUI and
            # its elevated twin, plus a scheduled run, all write this file),
            # refuses a link planted at the path, and performs the LOW-009 size
            # trim atomically - temp sibling in the same directory, then
            # os.replace, so a crash mid-trim cannot lose the whole log.
            # `app.logsafe` imports nothing from the app, so this does not
            # reopen the utils -> config_persist -> config_events cycle that
            # the module split closed.
            from app.logsafe import append_text_no_follow
            append_text_no_follow(str(EVENTS_LOG_FILE), line,
                                  max_bytes=_EVENTS_MAX_BYTES)
    except Exception:
        pass


def read_events(limit: int = 400) -> "list[str]":
    """The most recent event-log lines, oldest first.

    U-001: the app has been writing this trail all along (elevation cookie
    failures, swallowed state-changing ops, ...) but nothing ever read it
    back, so it was invisible to a user filing a bug report. The log-export
    path calls this to append the tail, and it never raises — a missing or
    unreadable log is simply an empty trail, not an error."""
    try:
        text = EVENTS_LOG_FILE.read_text(encoding="utf-8", errors="ignore")
    except Exception:
        return []
    lines = text.splitlines()
    if limit and limit > 0:
        return lines[-limit:]
    return lines
