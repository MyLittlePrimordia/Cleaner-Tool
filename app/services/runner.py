"""Run-state FSM and cancellation token (STATE-001 remediation foundation).

Stdlib-only. The central run engine in app.gui can adopt these types
incrementally; existing bool flags remain as deprecated aliases until
the full migration is verified on Windows smoke tests.
"""
from __future__ import annotations

from enum import Enum, auto
from dataclasses import dataclass, field
import threading
from typing import Optional


class RunState(Enum):
    IDLE = auto()
    RUNNING = auto()
    CANCELLING = auto()
    DONE = auto()
    ERROR = auto()


@dataclass
class CancellationToken:
    """Single source of truth for cooperative cancel (replaces ad-hoc
    _stop / _token / _scan_token bool lists). Thread-safe."""
    _cancelled: bool = field(default=False, init=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False)

    def cancel(self) -> None:
        with self._lock:
            self._cancelled = True

    def is_cancelled(self) -> bool:
        with self._lock:
            return self._cancelled

    def reset(self) -> None:
        with self._lock:
            self._cancelled = False

    def __bool__(self) -> bool:  # allow `if token:`
        return self.is_cancelled()
