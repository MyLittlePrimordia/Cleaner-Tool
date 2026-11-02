"""Single cross-process file-lock owner (R-002 / H-007).

config_persist and quarantine each hand-rolled the same RLock + depth
counter + msvcrt.locking block with subtly different fail-open semantics.
One context manager now owns the pattern; both callers delegate to it.

Fail-open vs fail-closed stays the caller's choice via `fail_open`:
- config: fail_open=True (a lock failure must never block settings I/O;
  the unique-temp + atomic replace still keep each write safe).
- quarantine manifest append: callers pass fail_open=False semantics by
  treating a lock failure as \"delete instead of quarantine\" (H-006).

H-007: lock fallback is no longer silent — the first failure per process
emits a security-event trail line so a lost-update window is visible.
"""
from __future__ import annotations

import contextlib
import sys
import threading

_guard = threading.RLock()
_depth = 0
_warned: set[str] = set()


@contextlib.contextmanager
def locked(lock_path, *, warn_key: str = "filelock"):
    """Hold an advisory cross-process byte lock on `lock_path`.

    Reentrant within a process. Never raises; on lock failure yields
    anyway (fail-open) after logging once per `warn_key`.
    """
    global _depth
    from pathlib import Path as _Path
    with _guard:
        if _depth > 0:
            _depth += 1
            try:
                yield False  # already held in-process; not newly locked
            finally:
                _depth -= 1
            return
        handle = None
        locked_ok = False
        try:
            p = _Path(str(lock_path))
            p.parent.mkdir(parents=True, exist_ok=True)
            handle = open(str(p), "a+")
            if sys.platform.startswith("win"):
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
            locked_ok = True
        except Exception as exc:
            locked_ok = False
            if warn_key not in _warned:
                _warned.add(warn_key)
                try:
                    from app.config_events import log_security_event
                    log_security_event("filelock_fallback",
                        f"Cross-process lock unavailable for {lock_path} "
                        f"({type(exc).__name__}); continuing without it.")
                except Exception:
                    pass
        _depth = 1
        try:
            yield locked_ok
        finally:
            _depth = 0
            if handle is not None:
                try:
                    if locked_ok and sys.platform.startswith("win"):
                        import msvcrt
                        handle.seek(0)
                        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                except Exception:
                    pass
                try:
                    handle.close()
                except Exception:
                    pass
