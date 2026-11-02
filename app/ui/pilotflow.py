"""The Game Session Auto-Pilot: the GUI half of the watcher.

Extracted from app/ui/app.py (audit 2026-10, R-001), where the watchlist
build, the catalog sweep and the apply/revert paths sat between the launcher
allowlist and the maintenance tips.

Three layers, and the split follows them:

  * `app.session_pilot`      detection + session state, headless and tested
                             without a window
  * `app.ui.session`         SessionOrchestrator: the queue, the pump and
                             the one place that owns whether the pilot runs
  * this module              what the *window* does with that: resolve the
                             preset's tasks, call run_tasks, toast, repaint
                             the open dialog's live line

Threading contract, unchanged and load-bearing: the watcher runs on its own
thread and must never touch Tk. `run_tasks` is async and thread-safe by
design, and the catalog sweep writes a plain holder dict that only the
Tk-thread poller (`_check_merge`) publishes. Nothing here calls a widget
from a worker.

Every attribute this file reaches for (`_pilot_sess`, `_pilot_dialog`,
`_busy`, `_busy_lock`, `root`, `log`, `set_status`, `run_tasks`) is created
by Application.__init__; these methods are mixed back in as a base so no
call site changed.
"""
from __future__ import annotations

import os

from app.config_persist import get_tweak_state
from app.elevation import is_admin
from app.tab_presets import PRESETS, TABS
from app.toast import show_toast
from app.ui.base import ThemedModal
from app.ui.session import SessionState
from app.ui.theme import COLORS, TAB_ACCENTS


class PilotFlow:
    def _pilot_preset_tasks(self):
        """Task objects for the configured session preset (tolerant:
        unknown preset names / stale keys degrade to Game Session, then
        to whatever resolves — never raise)."""
        try:
            from app.config_persist import load_config as _load
            want = _load().get("session_pilot_preset", "Game Session")
        except Exception:
            want = "Game Session"
        try:
            presets = PRESETS.get("Tweak", {})
            keys = presets.get(want) or presets.get("Game Session", [])
            by_key = {t.key: t for t in TABS.get("Tweak", [])}
            tasks = [by_key[k] for k in keys if k in by_key]
            if tasks:
                return tasks
            return [by_key[k] for k in presets.get("Game Session", [])
                    if k in by_key]
        except Exception:
            return []

    def _pilot_ensure(self):
        """(Re)build the pilot object from current config (called on
        boot, toggle-on, and watchlist edits — cheap, idempotent).

        Phase-5 watchlist assembly (3 layers):
          1. built-in famous-title basenames (legacy contract)
          2. user picks — full PATHS when known (path-verified matching:
             a same-named exe in another folder never fires), bare names
             for legacy entries
          3. 'watch all installed games' (opt-in): every game found by
             app.game_catalog — resolved FRESH so a newly installed game
             is picked up on the next toggle/start, never a stale cache.
             Unresolvable-exe games contribute their folder's plausible
             exes as bare names (heuristic already filtered
             redists/launchers at catalog level).

        Flicker fix (user bug report): layer 3 is a DISK SWEEP and used
        to run synchronously here — on a big library that froze the Tk
        thread for seconds on toggle/boot. Now the pilot builds + starts
        immediately on layers 1-2 (fast, config-only) and layer 3 merges
        in from a worker thread (generation-guarded, see below)."""
        # H15: the queue and its pump belong to SessionOrchestrator, which
        # starts them idempotently and — unlike the pre-H15
        # `_pilot_pump_started` bool — can stop them again.
        self._pilot_sess.ensure_pump()
        try:
            from app.config_persist import load_config as _load
            from app.session_pilot import (SessionPilot,
                                           normalize_exe)
            cfg = _load()
            watched = self._pilot_watched_fast(cfg, normalize_exe)
            # dedupe: a game both hand-picked AND watch-all must not
            # double-register (set() at the pilot level handles it)
            old = getattr(self, "_pilot", None)
            try:
                if old is not None:
                    old.stop()
            except Exception:
                pass
            # The watcher gets the orchestrator's queue producers directly:
            # they are the only two methods it is allowed to call, and they
            # do nothing but queue.put (no cross-thread Tcl, ever).
            pilot = SessionPilot(
                watched=watched,
                on_apply=self._pilot_sess.post_apply,
                on_revert=self._pilot_sess.post_revert)
            self._pilot = pilot
            if cfg.get("session_pilot_watch_all"):
                self._pilot_merge_catalog(pilot)
        except Exception:
            pass

    @staticmethod
    def _pilot_watched_fast(cfg, normalize_exe):
        """Layers 1-2: built-ins + user picks. Pure config, no disk —
        safe on the Tk thread."""
        try:
            from app.session_pilot import KNOWN_GAME_EXES
        except Exception:
            return []
        watched = list(KNOWN_GAME_EXES)
        # user picks: paths stay paths (dir-pinned), names stay names
        for g in cfg.get("session_pilot_games", []) or []:
            try:
                n = normalize_exe(g)
                if n:
                    watched.append(n)
            except Exception:
                pass
        for pth in cfg.get("session_pilot_paths", []) or []:
            if isinstance(pth, str) and pth.strip():
                watched.append(pth)
        return watched

    def _pilot_merge_catalog(self, pilot):
        """Worker-thread layer-3 sweep: enumerate installed games off the
        Tk thread, then merge into the LIVE pilot via a Tk-thread poll.
        Generation-guarded: if the pilot was replaced/stopped meanwhile
        (rapid toggles, app closing), the stale result is dropped instead
        of resurrecting dead state. A game already running when the merge
        lands is caught by the next 15s poll.

        Threading contract (measured 2026-09): worker threads NEVER call
        Tk here — not even winfo_exists/after (a refused foreign Tk call
        burns ~1s then raises). The worker writes a plain holder dict;
        the poller below (Tk thread, scheduled by the caller on the Tk
        thread) publishes."""
        try:
            import threading as _th
        except Exception:
            return
        try:
            seq = getattr(self, "_merge_seq", 0) + 1
        except Exception:
            seq = 1
        self._merge_seq = seq
        holder = {"done": False, "extra": []}

        def _run():
            extra = []
            try:
                from app.game_catalog import installed_games
                for g in installed_games() or []:
                    gp = (g.get("path") or "").strip()
                    if gp:
                        extra.append(gp)
                        continue
                    folder = (g.get("folder") or "").strip()
                    try:
                        import glob as _glob
                        lvl1 = _glob.glob(os.path.join(folder, "*.exe"))
                        lvl2 = _glob.glob(
                            os.path.join(folder, "*", "*.exe"))
                        for cand in (lvl1 + lvl2)[:6]:
                            extra.append(os.path.basename(cand))
                    except Exception:
                        pass
            except Exception:
                pass
            try:
                holder["extra"] = extra
            except Exception:
                pass
            try:
                holder["done"] = True
            except Exception:
                pass

        try:
            _th.Thread(target=_run, daemon=True,
                       name="PilotCatalogMerge").start()
        except Exception:
            return
        try:
            self.root.after(
                150, lambda: self._check_merge(pilot, holder, seq))
        except Exception:
            pass

    def _check_merge(self, pilot, holder, seq):
        """Tk-thread poller for one catalog sweep. Drops superseded
        results (newer ensure bumped the sequence), re-arms while the
        sweep flies, lands exactly once."""
        try:
            if getattr(self, "_merge_seq", 0) != seq:
                return              # superseded — drop silently
        except Exception:
            return
        try:
            if not self.root.winfo_exists():
                return
        except Exception:
            return
        try:
            done = holder.get("done")
        except Exception:
            return
        if not done:
            try:
                self.root.after(
                    150, lambda: self._check_merge(pilot, holder, seq))
            except Exception:
                pass
            return
        self._land_catalog(pilot, holder.get("extra") or [])

    def _land_catalog(self, pilot, extra):
        """Tk thread: merge a swept catalog into the pilot IFF it is
        still the current one (else drop — superseded or stopped)."""
        try:
            if not extra:
                return
            if getattr(self, "_pilot", None) is not pilot:
                return
            try:
                from app.config_persist import load_config as _load
                from app.session_pilot import normalize_exe
                cfg = _load()
            except Exception:
                return
            # rebuild from CURRENT config (a pick made mid-sweep must
            # survive) + the swept layer, then swap atomically-ish
            # (single set_watched call; a concurrent poll may use mixed
            # tables for one tick at most — self-heals next poll)
            watched = self._pilot_watched_fast(cfg, normalize_exe)
            watched.extend(extra)
            try:
                pilot.set_watched(watched)
            except Exception:
                pass
            try:
                self.log("Game Session Auto-Pilot: installed-games "
                         "watchlist merged.")
            except Exception:
                pass
        except Exception:
            pass

    def _pilot_start(self):
        try:
            self._pilot_ensure()
            if self._pilot is not None:
                self._pilot_sess.start()
                self.log("Game Session Auto-Pilot: watching for game launches.")
                # one-time notice when watch-all is active (the new-config
                # default): the auto-detection sweep is disclosed, never a
                # surprise — and points at the exact switch that turns it off.
                try:
                    from app.config_persist import load_config as _lc
                    if _lc().get("session_pilot_watch_all"):
                        if not getattr(self, "_pilot_watchall_notice_shown", False):
                            self._pilot_watchall_notice_shown = True
                            self.set_status(
                                "Auto-Pilot is watching your installed games — "
                                "open the Auto-Pilot dialog to narrow the list.")
                except Exception:
                    pass
        except Exception:
            pass

    def _pilot_stop(self):
        # Stopping NEVER reverts: the user may still be playing, and app
        # close must be side-effect free. Applied tweaks stay badged
        # until Undo reverts them (said plainly in the dialog).
        try:
            # The stay-awake hold is released by _pilot_revert, and stopping
            # never reverts. Release it here when a session is live, or the
            # display/sleep stay suppressed for the rest of the process (the
            # user's PC "never sleeps" after turning Auto-Pilot off mid-game).
            _was_active = self._pilot_state is SessionState.ACTIVE
            # H15: also cancels the pump's pending after(). Pre-H15 the
            # 50ms drain loop rescheduled itself for the rest of the app's
            # life even with the feature switched off.
            self._pilot_sess.stop()
            if _was_active:
                self._set_stay_awake(False)
            self.log("Game Session Auto-Pilot: stopped watching.")
        except Exception as exc:
            from app.utils import log_or_swallow
            log_or_swallow(None, "ui.pilot_stop", exc)

    def _pilot_refresh_watchlist(self):
        """User added/removed an .exe: rebuild the watchlist live."""
        try:
            running = self._pilot_sess.is_running()
            self._pilot_ensure()
            if running:
                self._pilot_sess.start()
        except Exception:
            pass

    # ------------------------------------------------------------------ #
    # H15 forwarders. Each of these used to carry the logic; they now hand
    # off to SessionOrchestrator, which is the only thing that should own
    # the Auto-Pilot's state. Kept as named properties because this file
    # addresses them by name.
    # ------------------------------------------------------------------ #

    @property
    def _pilot(self):
        return self._pilot_sess.pilot

    @_pilot.setter
    def _pilot(self, value):
        self._pilot_sess.pilot = value

    @property
    def _pilot_applied_keys(self) -> list:
        return self._pilot_sess.applied_keys

    @_pilot_applied_keys.setter
    def _pilot_applied_keys(self, value):
        self._pilot_sess.applied_keys = value

    @property
    def _pilot_fresh_keys(self) -> list:
        return self._pilot_sess.fresh_keys

    @_pilot_fresh_keys.setter
    def _pilot_fresh_keys(self, value):
        self._pilot_sess.fresh_keys = value

    @property
    def _pilot_state(self) -> SessionState:
        return self._pilot_sess.state

    def _pilot_sink(self, text, color=None):
        """Live line repaint for the open pilot dialog (no-op closed)."""
        try:
            dlg = getattr(self, "_pilot_dialog", None)
            if dlg is not None:
                dlg.set_live_status(text, color)
        except Exception:
            pass

    def _pilot_apply(self, hits):
        """Tk thread: apply the session preset (or defer quietly if the
        user is mid-run — the pilot retries nothing; the session simply
        runs untweaked and the exit path no-ops via registry check)."""
        # Stay-awake covers every path below (busy/modal/no-tasks included)
        # — a game IS being played the moment this fires, whether or not
        # any tweak actually landed. session_pilot.py's own state machine
        # guarantees this fires exactly once per idle->active transition
        # (see _poll: only when leaving "idle"), so this can never
        # over-increment from a single session.
        self._set_stay_awake(True)
        try:
            tasks = self._pilot_preset_tasks()
        except Exception:
            tasks = []
        # H15: a detection IS an active session even when nothing could be
        # applied — the user is playing, which is what the state means.
        self._pilot_sess.note_session_active(True)
        if not tasks:
            return
        busy = False
        try:
            with self._busy_lock:
                busy = self._busy
        except Exception:
            pass
        # flicker fix: registry check (see _pilot_ui_free) — the old
        # grab_current() read False during any open dialog's dropdown
        modal = ThemedModal.any_open()
        runnable = list(tasks)
        if not is_admin():
            # limited mode: only what needs no admin, SILENTLY (a
            # background trigger must never pop the "Skipped N Admin-Only
            # Tasks" notice over someone's game — run_tasks would do that)
            runnable = [t for t in tasks if not t.admin_required]
        # Module-8: intent must reflect what will actually run — capturing
        # applied_keys pre-filter claimed admin tweaks the limited-mode run
        # never applied, leaving revert overbroad (saved only by the live
        # registry intersection downstream).
        try:
            self._pilot_applied_keys = [t.key for t in runnable]
        except Exception:
            self._pilot_applied_keys = []
        try:
            # Fresh-keys only: exclude tweaks that were ALREADY applied
            # before this session (the user may want those persistent —
            # reverting them at session end would undo the user's own
            # deliberate setup, not the pilot's work).
            before = set(get_tweak_state() or {})
        except Exception:
            before = set()
        try:
            self._pilot_fresh_keys = [k for k in self._pilot_applied_keys
                                      if k not in before]
        except Exception:
            self._pilot_fresh_keys = list(self._pilot_applied_keys)
        if busy or modal or not runnable:
            try:
                self.log("Game Session Auto-Pilot: game detected "
                         f"({(hits or ['?'])[0]}) but "
                         + ("a run is active — session runs untweaked."
                            if busy else
                            ("a dialog is open — session runs untweaked."
                             if modal else
                             "no runnable tweaks in limited mode — session runs untweaked.")))
            except Exception:
                pass
            return
        try:
            self.log(f"Game Session Auto-Pilot: applying session preset for {(hits or ['?'])[0]}.")
            self._pilot_sink(f"🎮 {(hits or ['?'])[0]} — session active",
                             COLORS["accent_green"])
            try:
                import threading as _th
                _th.Thread(target=show_toast,
                           args=("🎮 Game Mode ON",
                                 "Session tweaks applied — enjoy the game. "
                                 "They restore automatically when you quit."),
                           daemon=True).start()
            except Exception:
                pass
            self.run_tasks("Tweak", runnable, mode="run", quiet=True)
        except Exception:
            pass

    def _pilot_revert(self):
        """Tk thread: revert ONLY fresh session keys still marked applied
        (registry intersection). Excluded, and therefore never touched:
        keys the run never managed to apply (busy-skip, failure,
        user-undone mid-session) AND keys that were already applied
        before the session (the user's persistent setup — reverting those
        would undo deliberate choices, not pilot work). Silent when
        there's nothing to do."""
        # H15: the session is over as soon as reconciliation begins; the
        # orchestrator's state is what the dialog and the tray read.
        self._pilot_sess.note_session_active(False)

        # Release stay-awake unconditionally (before the fresh-keys early
        # return below) — the game session is over either way, whether or
        # not there happen to be any tweaks left to put back.
        self._set_stay_awake(False)
        try:
            fresh = list(getattr(self, "_pilot_fresh_keys", []) or [])
        except Exception:
            fresh = []
        if not fresh:
            return
        # UI-freedom guard (user bug report + audit find): a revert that
        # fires while a run owns the progress UI — or while ANY modal
        # dialog is open — must not choreograph shared UI behind the
        # user's context. Worse, run_tasks would pop an "already running" notice
        # (possibly over a game) AND the revert would be lost. Skip with
        # an honest log line instead; Undo Tweaks restores manually and
        # the applied-badges stay truthful about what is still on.
        # F01: intent is preserved (keys NOT cleared) so a later exit
        # event can still revert; keys clear only on the runnable path.
        _busy = False
        try:
            with self._busy_lock:
                _busy = self._busy
        except Exception:
            pass
        # flicker fix: registry check (see _pilot_ui_free) — the old
        # grab_current() read False during any open dialog's dropdown
        _modal = ThemedModal.any_open()
        if _busy or _modal:
            try:
                self.log("Game Session Auto-Pilot: game closed but the UI is "
                         "occupied (" + ("a run is active" if _busy else "a dialog is open") +
                         ") — session tweaks stay applied; Undo Tweaks restores them.")
            except Exception:
                pass
            try:
                self._pilot_sink("● Watching…", TAB_ACCENTS["Tweak"])
            except Exception:
                pass
            return
        try:
            current = set(get_tweak_state() or {})
        except Exception:
            current = set()
        keys = [k for k in fresh if k in current]
        if not keys:
            try:
                self._pilot_applied_keys = []
                self._pilot_fresh_keys = []
            except Exception:
                pass
            try:
                self.log("Game Session Auto-Pilot: session ended — settings already restored.")
            except Exception:
                pass
            try:
                self._pilot_sink("● Watching…", TAB_ACCENTS["Tweak"])
            except Exception:
                pass
            return
        try:
            by_key = {t.key: t for t in TABS.get("Tweak", [])}
            tasks = [by_key[k] for k in keys
                     if k in by_key and by_key[k].revert is not None]
        except Exception:
            tasks = []
        if not tasks:
            try:
                self._pilot_applied_keys = []
                self._pilot_fresh_keys = []
            except Exception:
                pass
            return
        try:
            self.log("Game Session Auto-Pilot: game closed — restoring session tweaks.")
            self._pilot_sink("Restoring settings…", COLORS["accent_yellow"])
            try:
                import threading as _th
                _th.Thread(target=show_toast,
                           args=("Game Mode OFF",
                                 "Session tweaks restored to your normal settings."),
                           daemon=True).start()
            except Exception:
                pass
            self.run_tasks("Tweak", tasks, mode="revert", quiet=True)
        except Exception:
            pass
        try:
            self._pilot_applied_keys = []
            self._pilot_fresh_keys = []
        except Exception:
            pass
        try:
            self._pilot_sink("● Watching…", TAB_ACCENTS["Tweak"])
        except Exception:
            pass
