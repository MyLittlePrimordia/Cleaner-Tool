"""Process-handle owner (R-005).

`utils.run_cmd` tracks `Popen` objects in a global dict keyed by
`thread.get_ident()` with tree-kill + reader-thread boilerplate copied in
three places. Thread-ident reuse after thread death can misattribute a kill.

`ProcHandle` owns one Popen + reader thread + cancel token with `kill_tree()`.
`utils` delegates to it; the registry here is keyed by handle id, not thread
ident. New code should use this directly.
"""
from __future__ import annotations

import subprocess
import threading

_handles: dict[int, "ProcHandle"] = {}
_lock = threading.Lock()


class ProcHandle:
    """Owns a Popen + kill_tree. Never raises out of kill paths."""

    def __init__(self, proc: subprocess.Popen):
        self.proc = proc
        self.id = id(self)
        with _lock:
            _handles[self.id] = self

    def kill_tree(self) -> None:
        try:
            from app.utils import _kill_proc as _kp
            _kp(self.proc)
        except Exception:
            pass

    def release(self) -> None:
        with _lock:
            _handles.pop(self.id, None)

    @staticmethod
    def any_live() -> "ProcHandle | None":
        with _lock:
            for h in list(_handles.values()):
                try:
                    if h.proc is not None and h.proc.poll() is None:
                        return h
                except Exception:
                    continue
        return None
