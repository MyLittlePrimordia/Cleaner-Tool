"""
Shared Task definition used by the clean / repair / tweak tab modules.

Each tab is just a list of Task objects -- the GUI introspects the list to
build checkboxes automatically, so adding a brand new cleaning task, repair
routine, or tweak is a matter of appending one Task(...) entry to the
relevant module. No GUI code needs to change.
"""

from dataclasses import dataclass
from typing import Callable, Optional

from app.utils import TaskContext


@dataclass
class Task:
    key: str                       # unique id, e.g. "shader_cache"
    label: str                     # short checkbox text
    description: str               # one-line tooltip / detail shown under the label
    run: Callable[[TaskContext], Optional[int]]
    # H-004: opt-in, not opt-out. This used to default to True, so a task
    # appended without the flag was silently CHECKED - which is fine for a
    # cache clear and not fine at all for the data-loss actions (Recycle Bin,
    # driver leftovers, log deletion). Every existing Task(...) names its
    # choice, and dev/tests/test_task_defaults.py pins the default-on set, so a
    # new task has to be added deliberately.
    default: bool = False          # checked by default?
    admin_required: bool = True
    risk: str = "SAFE"             # SAFE / ADVANCED / REBOOT REQUIRED (see config.py)
    min_build: int = 0             # minimum Windows build number (0 = any); N-004
    revert: Optional[Callable[[TaskContext], None]] = None  # tweaks only
    group: str = "Essentials"      # Install-tab Essentials section this task renders under
                                   # ("Essentials" or "LTSC Missing Components"); ignored elsewhere
    verify: Optional[Callable[[], "bool | None"]] = None
                                   # Tweak Health (2026-09): READ-ONLY check that the
                                   # tweak's applied state is still live on this machine.
                                   # Returns True (applied), False (drifted — Windows
                                   # Update / a driver reset it), or None (cannot tell —
                                   # no verify defined or the read failed). The health
                                   # check never writes anything; a missing verify just
                                   # leaves the row showing "applied" (registry of trust),
                                   # never a false drift alarm.
