"""Fail-closed filesystem safety helpers.

BOUNDARY-001 / BUG-001 remediation lives in app.utils._is_reparse_point
(return True on any error so cleaners refuse rather than follow a junction).
This module is the documented home for future safe_walk / safe_remove
extractions without forcing every caller to import the large utils module.
"""
from __future__ import annotations

import os
from typing import Iterator, List, Tuple

from app.utils import _is_reparse_point, _is_protected_root


def safe_walk(top: str, topdown: bool = True) -> Iterator[Tuple[str, List[str], List[str]]]:
    """os.walk that never descends into reparse points (junctions/symlinks).

    Fail-closed: if the root itself is a reparse point, yields nothing.
    """
    if _is_reparse_point(top):
        return
    for root, dirs, files in os.walk(top, topdown=topdown, followlinks=False):
        # Prune reparse dirs in-place so os.walk does not descend
        dirs[:] = [d for d in dirs if not _is_reparse_point(os.path.join(root, d))]
        yield root, dirs, files


def is_safe_to_delete(path: str) -> bool:
    """True only when path exists, is not a reparse point, and is not a
    protected container root.

    BUG-014: this now consults utils._is_protected_root so the rule lives in
    exactly ONE place. Previously this function and clean_folder_contents each
    had their own idea of what was safe, which is how they drifted. The
    import direction stays utils <- fs_safe (fs_safe already imported
    _is_reparse_point from utils), so no new cycle is introduced."""
    try:
        if not os.path.exists(path):
            return False
        if _is_protected_root(path):
            return False
        return not _is_reparse_point(path)
    except Exception:
        return False
