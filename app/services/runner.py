"""DEPRECATED shim — re-exports the real run-state types.

ARCH-006 / I-013: the run-state FSM and the cancellation token live in
``app.runner.types``, the single source of truth used by both the GUI
(``app.ui.app``) and the headless scheduler. This module originally defined a
SECOND copy of ``RunState`` that was missing ``PREFLIGHT`` and the transition
table, so ``from app.services import RunState`` silently handed back the wrong
finite-state machine. It now re-exports the canonical types (and its own copy
of ``CancellationToken``), so any lingering importer gets the correct ones.

Import direction: ``app.runner.types`` depends on nothing in the app, so this
shim cannot create an import cycle.
"""
from __future__ import annotations

from app.runner.types import (  # noqa: F401
    RunState, CancellationToken, OUTCOME_STATES, LIVE_STATES,
    ALLOWED_TRANSITIONS,
)

__all__ = ["RunState", "CancellationToken", "OUTCOME_STATES", "LIVE_STATES",
           "ALLOWED_TRANSITIONS"]
