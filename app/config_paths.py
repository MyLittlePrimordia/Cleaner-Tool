"""Where Cleaner Tool keeps its per-user state.

H3. Extracted from `app.config_persist` (930 lines) so the path resolution
has one owner and does not drag the whole config loader along with it.

Deliberately NOT made lazy. `CONFIG_DIR` and `CONFIG_FILE` are imported by
name elsewhere (`app.startup_manager`, `app.toast`), so deferring the
resolution would mean either a module-level __getattr__ shim or changing
every consumer — pure indirection to fix a non-bug, since nothing mutates
%LOCALAPPDATA% mid-process. Recorded as a considered-and-rejected option
rather than silently skipped.

Import direction: depends on nothing in the app.
"""
from __future__ import annotations

import os
from pathlib import Path


def get_config_dir() -> Path:
    """Resolve the per-user state directory.

    %LOCALAPPDATA% when it is a real directory, else the user's home, else
    %TEMP%, else the CWD. The trailing "." fallback is the weakest link in
    this chain: it would place config.json in whatever directory the process
    was started from. It is unreachable on a normal Windows launch (all three
    earlier branches resolve), but it is the first thing to tighten if state
    is ever seen to land somewhere unexpected.
    """
    base = os.environ.get("LOCALAPPDATA", "")
    if not base or not os.path.isdir(base):
        base = os.path.expanduser("~") or os.environ.get("TEMP", "") or "."
    return Path(base) / "CleanerTool"


CONFIG_DIR = get_config_dir()
CONFIG_FILE = CONFIG_DIR / "config.json"

# ARCH-001: one small append-only local event log for security-relevant
# events (config quarantines, elevation outcomes, backup rotations) that
# previously left no persistent record anywhere. Additive only — nothing
# reads this back into app behavior, it exists purely so a future
# debugging session has more than "it just reset" to go on. Capped to the
# last ~2000 lines so it can never grow unbounded over months.
EVENTS_LOG_FILE = CONFIG_DIR / "events.log"
