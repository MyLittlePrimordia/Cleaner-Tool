"""Drive enumeration + before/after free-space sampling.

H4. Extracted verbatim from app/gui.py. Tk-free.

BUG-001 (audit 2026-09): the scorecard's before/after capture used to
hard-code "C:\\", so on a machine where Windows lives on another letter the
scorecard measured a drive the run never touched and users saw 0 bytes freed
after a clean that worked. These resolve %SystemDrive% instead.
"""
from __future__ import annotations

import os
import string

def _system_drive_root() -> str:
    """Root of the drive Windows is actually installed on, e.g. 'C:\\'.

    BUG-001 fix (audit 2026-09): the scorecard's before/after capture and
    the Storage Insight write-benchmark hard-coded "C:\\". On the machines
    where Windows lives on another letter (dual-boot, custom installs,
    some OEM images) that made the scorecard measure a drive the run never
    touched — users saw 0 bytes freed after a clean that worked fine.
    %SystemDrive% is set by Windows itself on every supported version; the
    "C:" fallback keeps the old behaviour if it is ever missing.
    """
    try:
        drive = (os.environ.get("SystemDrive") or "C:").strip()
    except Exception:
        drive = "C:"
    if not drive:
        drive = "C:"
    if not drive.endswith("\\"):
        drive += "\\"
    return drive


def _snapshot_all_drives(query_drives, query_free_total):
    """{root: (free_bytes, total_bytes)} for every ready drive.

    Feature 4 (multi-drive scorecard): a Clean run can free space on more
    than one drive (games and shader caches often live on a second SSD),
    but the scorecard only ever measured one. Taking the same instant
    GetDiskFreeSpaceExW reading for every ready drive before and after the
    run lets the card show each drive that actually gained space.

    Both callables are passed in so this stays a plain module function
    (the readers are Application staticmethods). Never raises: a drive
    that fails to answer is simply absent from the snapshot, which the
    renderer treats as 'no data for that drive'.
    """
    out = {}
    try:
        drives = query_drives() or []
    except Exception:
        return out
    for entry in drives:
        try:
            root = entry[1]
            pair = query_free_total(root)
            if pair:
                out[root] = pair
        except Exception:
            continue
    return out


def _drive_gains(before_map, after_map):
    """[(root, gained_bytes)] for drives whose free space actually grew.

    Sorted biggest gain first. Drives missing from either snapshot, or
    that did not gain, are left out — the card never invents a number.
    """
    gains = []
    try:
        for root, before in (before_map or {}).items():
            after = (after_map or {}).get(root)
            if not before or not after:
                continue
            delta = (after[0] or 0) - (before[0] or 0)
            if delta > 0:
                gains.append((root, delta))
    except Exception:
        return []
    gains.sort(key=lambda g: g[1], reverse=True)
    return gains
