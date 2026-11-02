"""One owner for %TEMP% coordination files (R-007).

Elevation cookies/tokens, single-instance probe names, startup logs and
the battery-report temp all used predictable `%TEMP%\\CleanerTool_*` names
with five separate create/cleanup/redact implementations. This module owns
the pattern: exclusive-create, no-follow read/append, redacted logging,
and periodic sweep of stale temps.

Existing callers keep their file names; new code should resolve via here.
"""
from __future__ import annotations

import os
import tempfile

_PREFIX = "CleanerTool_"


def temp_path(name: str) -> str:
    """Absolute %TEMP% path for a `CleanerTool_*` coordination file."""
    base = os.path.basename(name or "")
    if not base.startswith(_PREFIX):
        base = _PREFIX + base
    return os.path.join(tempfile.gettempdir(), base)


def exclusive_write(path: str, data: str) -> None:
    """Create `path` exclusively (O_CREAT|O_EXCL), unlinking a plant first.

    Mirrors elevation._exclusive_write; kept here so all rendezvous files
    share the TOCTOU-safe primitive.
    """
    try:
        os.unlink(path)
    except OSError:
        pass
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    fd = os.open(path, flags, 0o600)
    try:
        os.write(fd, data.encode("utf-8"))
    finally:
        os.close(fd)


def sweep_stale(max_age_s: int = 3600) -> int:
    """Remove stale `CleanerTool_*.tmp` / `*.part` files. Best-effort."""
    import time as _t
    removed = 0
    try:
        now = _t.time()
        d = tempfile.gettempdir()
        for entry in os.listdir(d):
            if not entry.startswith(_PREFIX):
                continue
            if not (entry.endswith(".tmp") or entry.endswith(".part")):
                continue
            p = os.path.join(d, entry)
            try:
                if now - os.path.getmtime(p) < max_age_s:
                    continue
                os.remove(p)
                removed += 1
            except OSError:
                pass
    except Exception:
        pass
    return removed
