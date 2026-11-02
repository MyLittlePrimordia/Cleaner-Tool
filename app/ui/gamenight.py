"""The Game Night controller: one press to prepare, one to put it back.

Extracted from app/ui/app.py (Phase 4) exactly as PilotFlow was, so the
window code that calls it did not change. Mixed back into Application as a
base, which is why every `self.` here (run_tasks, set_status, log, _pilot,
_set_stay_awake, _toast_async) still resolves against the window.

The rules are the ones the methods already carried: apply and revert go
through run_tasks so cancel, the Admin Gate and snapshots behave normally,
and only tweaks Game Night itself turned on are reverted at the end.
"""
from __future__ import annotations

from app.config_persist import get_tweak_state
from app.elevation import is_admin
from app.tab_presets import TABS


class GameNightFlow:
    # ---------------- Game Night (feature 9) ---------------- #
    # One press to get the PC ready to play, one press to put it back.
    # Deliberately built on the same two rules Session Pilot already
    # proved out (see _pilot_apply / _pilot_revert):
    #   * apply and revert go through run_tasks(), so cancel, the Admin
    #     Gate, snapshots and applied-tweak bookkeeping all behave as
    #     normal — there is no second code path that changes Windows
    #   * only tweaks Game Night itself turned on are reverted at the
    #     end. Anything the user already had applied is left alone,
    #     because undoing it would undo their own setup, not ours

    def _game_night_tasks(self, keys=None):
        """Task objects for the Game Session preset. [] if none resolve.

        `keys` (N-003) replaces the preset with a profile's list; the lookup
        still goes through the live Tweak tab, so a key whose tweak is gone
        is skipped instead of raising during a session start.
        """
        try:
            if keys is None:
                from app.game_night import preset_task_keys
                keys = preset_task_keys()
            by_key = {t.key: t for t in TABS.get("Tweak", [])}
            return [by_key[k] for k in keys if k in by_key]
        except Exception:
            return []

    def _game_in_progress(self):
        """True when the Auto-Pilot watcher currently sees a game running.

        Used only to decide whether the run may pop a scorecard: a modal
        window over someone's fullscreen game is hostile, so a Game Night
        pressed mid-session reports through the log and a toast instead."""
        try:
            pilot = getattr(self, "_pilot", None)
            # active_hit is a PROPERTY on SessionPilot (the exe name, or
            # None when idle) — calling it would raise, and the except
            # below would quietly turn that into "no game running".
            return bool(pilot is not None and pilot.active_hit)
        except Exception:
            return False

    def start_game_night(self, quiet=None, keys=None):
        """Apply the Game Session preset and remember what we turned on.

        `keys` starts a profile's set instead of the preset. With neither
        argument, a profile bound to the game Auto-Pilot currently sees wins
        (N-003) — the same detection that already drives the session, so no
        second watcher and no extra polling.

        quiet=None (default) preserves the old contract: a modal scorecard
        unless a game is already in progress. The tray passes quiet=True
        explicitly — a tray click may come from inside a game, so it never
        risks a modal over it (one state toast instead)."""
        if keys is None:
            try:
                pilot = getattr(self, "_pilot", None)
                current = getattr(pilot, "active_hit", None) if pilot else None
                if current:
                    from app.game_profiles import for_exe
                    keys = for_exe(current) or None
            except Exception:
                keys = None
        tasks = self._game_night_tasks(keys)
        if not tasks:
            self.set_status("Game Night: no gaming tweaks available.")
            if quiet:
                self._toast_async("Game Mode", "No gaming settings available.")
            return
        runnable = list(tasks)
        if not is_admin():
            # Limited mode: run what needs no admin rather than refusing
            # outright, and say so — the session still gets most of it.
            runnable = [t for t in tasks if not t.admin_required]
            if len(runnable) != len(tasks):
                self.log("Game Night: not running as Administrator — "
                         f"applying {len(runnable)} of {len(tasks)} settings.")
        if not runnable:
            self.set_status("Game Night needs Administrator for these settings.")
            if quiet:
                self._toast_async("Game Mode needs admin",
                                  "Restart as Administrator to turn it on.")
            return
        # Fresh keys only (the pilot's rule): tweaks already applied before
        # this press stay applied when Game Night ends.
        try:
            before = set(get_tweak_state() or {})
        except Exception:
            before = set()
        fresh = [t.key for t in runnable if t.key not in before]
        try:
            from app.config_persist import set_game_night
            set_game_night(True, fresh)
            # Stay-awake activates only once the session is actually
            # recorded active (matches end_game_night's unconditional
            # release below — the UI only ever calls end_game_night after
            # a successful start, so these stay balanced).
            self._set_stay_awake(True)
        except Exception:
            pass
        # Optional background quieting — only apps the user switched on.
        try:
            from app.config_persist import get_game_night
            from app.game_night import close_apps
            chosen = get_game_night().get("close_apps") or []
            if chosen:
                self.log("Game Night: asking the apps you picked to close…")
                closed = close_apps(chosen, log=self.log)
                if closed:
                    self.log("Game Night: closed " + ", ".join(closed) + ".")
        except Exception:
            pass
        quiet = self._game_in_progress() if quiet is None else bool(quiet)
        try:
            self.log("===== Game Night: getting your PC ready to play =====")
            self.run_tasks("Tweak", runnable, mode="run", quiet=quiet,
                           toast_kind=("gm_on" if quiet else None),
                           toast_extra={"admin_missing": len(tasks) - len(runnable)})
        except Exception:
            pass
        self._refresh_pilot_dialog_manual_btn()

    def end_game_night(self, quiet=None):
        """Put back exactly the tweaks Game Night turned on.

        quiet=None preserves the old auto contract; the tray passes
        quiet=True explicitly (same no-modal-over-games rationale)."""
        # Unconditional (covers the "already back" early-return path too):
        # matches the single _set_stay_awake(True) in start_game_night, so
        # the reference count stays balanced regardless of which path this
        # takes. Only end_game_night ever calls this — never a bare
        # decrement anywhere else for the manual session.
        self._set_stay_awake(False)
        try:
            from app.config_persist import get_game_night, set_game_night
            saved = list(get_game_night().get("keys") or [])
        except Exception:
            saved = []
        if not saved:
            try:
                set_game_night(False)
            except Exception:
                pass
            self.set_status("Game Night ended — your settings were already back.")
            if quiet:
                self._toast_async("Game Mode off", "Normal settings are back.")
            return
        # Intersect with what is STILL applied: the user may have undone
        # some of it manually in the meantime, and reverting a tweak that
        # is already off is both pointless and confusing in the log.
        try:
            current = set(get_tweak_state() or {})
        except Exception:
            current = set()
        keys = [k for k in saved if k in current]
        try:
            by_key = {t.key: t for t in TABS.get("Tweak", [])}
            tasks = [by_key[k] for k in keys
                     if k in by_key and by_key[k].revert is not None]
        except Exception:
            tasks = []
        try:
            set_game_night(False)
        except Exception:
            pass
        if not tasks:
            self.set_status("Game Night ended — your settings were already back.")
            if quiet:
                self._toast_async("Game Mode off", "Normal settings are back.")
            return
        quiet = self._game_in_progress() if quiet is None else bool(quiet)
        try:
            self.log("===== Game Night: putting your settings back =====")
            self.run_tasks("Tweak", tasks, mode="revert", quiet=quiet,
                           toast_kind=("gm_off" if quiet else None))
        except Exception:
            pass
        self._refresh_pilot_dialog_manual_btn()
