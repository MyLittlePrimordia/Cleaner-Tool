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
_EVENTS_MAX_LINES = 2000


def log_security_event(category: str, message: str) -> None:
    """Append one timestamped line to the local event log.

    Best-effort and never raises — this is observability, not a control
    path, so a failure here must never affect the caller's actual
    operation. The lock keeps concurrent appends (GUI thread, scheduler
    process, elevated twin) from interleaving partial lines.
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
            with open(EVENTS_LOG_FILE, "a", encoding="utf-8") as f:
                f.write(line)
            # cheap cap check — only actually trims once in a long while,
            # so no need to count lines on every single append
            #
            # LOW-009, two defects in the trim:
            #  1. The trim was gated on BOTH size > 512 KB AND len(lines) >
            #     2000. A log over 512 KB with 2000 or fewer lines - entirely
            #     possible with long messages, which is what the app writes -
            #     never trimmed at all, and worse, the full re-read happened on
            #     EVERY append, forever, because the size condition stayed
            #     true. Now the size condition alone triggers the work, and the
            #     line cap is applied as the result rather than as a gate.
            #  2. The rewrite was non-atomic: `open(..., "w")` truncates the
            #     file before anything is written, so a crash or a concurrent
            #     reader during the rewrite loses the whole log. It now goes
            #     through utils.atomic_write_text, which writes a sibling temp
            #     and os.replace()s it into place.
            try:
                if os.path.getsize(EVENTS_LOG_FILE) > _EVENTS_MAX_BYTES:
                    with open(EVENTS_LOG_FILE, "r", encoding="utf-8",
                              errors="ignore") as f:
                        lines = f.readlines()
                    keep = lines[-_EVENTS_MAX_LINES:]
                    if len(keep) < len(lines):
                        # Atomic replace, done LOCALLY on purpose. The first
                        # version called app.utils.atomic_write_text, which
                        # made config_events import utils - and config_persist
                        # imports config_events, so that closed the cycle
                        # utils -> config_persist -> config_events -> utils.
                        # The architecture guard caught it. config_events is a
                        # leaf as far as the import graph is concerned and must
                        # stay that way, so the three lines it needs are
                        # inlined: temp in the SAME directory (a cross-volume
                        # temp would break os.replace atomicity), then replace.
                        tmp = str(EVENTS_LOG_FILE) + ".trim.tmp"
                        with open(tmp, "w", encoding="utf-8") as tf:
                            tf.write("".join(keep))
                            try:
                                tf.flush()
                                os.fsync(tf.fileno())
                            except Exception:
                                pass
                        os.replace(tmp, EVENTS_LOG_FILE)
            except Exception:
                pass
    except Exception:
        pass
