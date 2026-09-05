"""
Shared Task definition used by the clean / repair / tweak tab modules.

Each tab is just a list of Task objects -- the GUI introspects the list to
build checkboxes automatically, so adding a brand new cleaning task, repair
routine, or tweak is a matter of appending one Task(...) entry to the
relevant module. No GUI code needs to change.
"""

from dataclasses import dataclass, field
from typing import Callable, Optional

from app.utils import TaskContext


@dataclass
class Task:
    key: str                       # unique id, e.g. "shader_cache"
    label: str                     # short checkbox text
    description: str               # one-line tooltip / detail shown under the label
    run: Callable[[TaskContext], Optional[int]]
    default: bool = True           # checked by default?
    admin_required: bool = True
    risk: str = "SAFE"             # SAFE / ADVANCED / REBOOT REQUIRED (see config.py)
    revert: Optional[Callable[[TaskContext], None]] = None  # tweaks only
    column: int = 0                # deprecated: layout is auto-balanced round-robin in gui.py (kept for backward compat)
    group: str = "Essentials"      # Install-tab Essentials section this task renders under
                                   # ("Essentials" or "LTSC Missing Components"); ignored elsewhere
