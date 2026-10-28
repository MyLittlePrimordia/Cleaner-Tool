"""The system-tray menu: the resident half of the window.

Extracted from app/ui/app.py (audit 2026-10, R-001). The tray is the app's
second entry point - it can start a Quick Clean, flip Game Mode, ping or run a
speed test with no window on screen - and it had grown to 350 lines inside the
composition root, interleaved with chrome and tab routing.

The one rule that governs every method here: the tray runs on its OWN thread
and Tk does not. Callbacks reach the window only through `self._dispatch.post`
(a queue pumped on the Tk thread); a direct widget call from a tray thread is
the class of bug MED-014 describes, where a dropped cross-thread `after()`
left a busy flag stuck and a menu item dead for the session.

The tray also never opens a modal over a game: every action here reports with
a toast, and `quiet=True` runs skip the scorecard entirely.
"""
from __future__ import annotations

import threading

from app.tab_presets import TABS
from app.toast import show_toast
from app.ui.base import _themed_showinfo
from app.ui.theme import COLORS
from app.utils import format_bytes


class TrayControls:
    # ---------------- system tray (resident watchdog UI) ---------------- #

    def _ensure_tray(self) -> bool:
        """Start the tray icon on first need (lazy: no icon until the
        window hides once or --tray boots hidden — the suites never hide,
        so they never sprout icons). True while the icon lives."""
        try:
            tray = getattr(self, "_tray", None)
            if tray is not None and tray.running:
                return True
        except Exception:
            pass
        try:
            from app import tray as _traymod
            icon = _traymod.TrayIcon(
                tooltip="Cleaner Tool",
                items=[(101, "Open Cleaner Tool", self.tray_open),
                       (102, self._tray_game_label, self.tray_toggle_game_session),
                       (103, "\U0001f9f9 Quick Clean", self.tray_quick_clean),
                       (105, "\U0001f4f6 Check My Ping", self.tray_check_ping),
                       (106, "\U0001f680 Speed Test", self.tray_speed_test),
                       (None, None, None),
                       (104, "Quit", self.tray_quit)],
                on_ui_thread=lambda fn: self._tray_ui_hop(fn))
            self._tray_last = icon      # kept so a failure can say WHY
            if icon.start():
                self._tray = icon
                return True
            self._tray = None
        except Exception:
            try:
                self._tray = None
            except Exception:
                pass
        return False

    def _tray_ui_hop(self, fn) -> None:
        """Marshal a tray-thread callback onto the Tk thread. Best-effort:
        post-quit calls land nowhere instead of raising."""
        try:
            self._dispatch.post(fn)
        except Exception:
            pass

    def _stop_tray(self) -> None:
        try:
            tray = getattr(self, "_tray", None)
            self._tray = None
            if tray is not None:
                tray.stop()
        except Exception:
            pass

    def _hide_to_tray(self) -> bool:
        """Withdraw the window into the resident tray icon. False when no
        tray can exist (caller quits for real instead)."""
        try:
            if not self._ensure_tray():
                # Do NOT withdraw if there's no way back — leaving the
                # window open beats trapping the user with no tray icon
                # and no visible window (previously: silent no-op).
                try:
                    from app.config_persist import log_security_event
                    _why = getattr(getattr(self, "_tray_last", None),
                                   "last_error", "") or "unknown stage"
                    log_security_event("tray", "tray icon creation failed (%s);"
                                       " keeping window open instead of "
                                       "hiding" % _why)
                except Exception:
                    pass
                try:
                    _themed_showinfo(
                        self.root,
                        "System Tray Unavailable",
                        "The window will stay open so you can still use the app.\n"
                        "If this keeps happening, try restarting Explorer or the PC.",
                        accent=COLORS["accent_yellow"],
                    )
                except Exception:
                    pass
                return False
        except Exception:
            return False
        try:
            self.root.withdraw()
        except Exception:
            return False
        # first-hide explainer (once ever): X hides, Quit quits — never
        # trap a user who expected Close to mean close.
        try:
            from app.config_persist import has_seen_tip, mark_tip_seen
            if not has_seen_tip("tray_hide_once"):
                mark_tip_seen("tray_hide_once")
                try:
                    from app.toast import show_toast as _toast
                    _toast("Cleaner Tool keeps running",
                           "Minimized to the tray — monitors and Auto-Pilot "
                           "stay on. Quit from the tray icon menu.")
                except Exception:
                    pass
        except Exception:
            pass
        return True

    def _maybe_hide_for_tray(self) -> None:
        """--tray boot: withdraw right after the main UI builds (pre-
        mainloop, so nothing flashes). The Gate path never reaches here
        hidden — it builds the UI through its own callback first."""
        try:
            import sys as _sys
            if "--tray" in _sys.argv:
                self._hide_to_tray()
        except Exception:
            pass

    def tray_open(self) -> None:
        """Tray menu / single-click / second-launch focus target."""
        try:
            self.root.deiconify()
        except Exception:
            return
        try:
            self.root.lift()
        except Exception:
            pass
        try:
            self.root.focus_force()
        except Exception:
            pass

    def tray_quit(self) -> None:
        """The one true exit while resident (goes through the busy guard)."""
        try:
            self._on_close(force_quit=True)
        except Exception:
            pass

    def _tray_game_label(self) -> str:
        """Live tray label: shows the current state so the item reads as a
        plain On/Off switch. Evaluated each time the menu opens."""
        try:
            from app.config_persist import get_game_night
            on = bool(get_game_night().get("active"))
        except Exception:
            on = False
        return "\U0001f3ae Game Mode: " + ("On" if on else "Off")

    def tray_toggle_game_session(self) -> None:
        """Tray Game Mode: one click flips it. On applies the Game Session
        FPS tweaks, Off puts back exactly what it changed. Runs quietly (a
        tray click may come from inside a game — no modal over it) and
        reports the REAL outcome in one toast when the work is done."""
        try:
            if getattr(self, "_busy", False):
                self._toast_async("Cleaner Tool is busy",
                                  "A run is in progress — open the app to watch it.")
                self.tray_open()
                return
            from app.config_persist import get_game_night
            if bool(get_game_night().get("active")):
                self.end_game_night(quiet=True)
            else:
                self.start_game_night(quiet=True)
        except Exception:
            pass

    def _toast_async(self, title, msg) -> None:
        """Fire a toast without ever blocking the caller."""
        try:
            threading.Thread(target=show_toast, args=(title, msg),
                             daemon=True).start()
        except Exception:
            pass

    @staticmethod
    def _result_toast(kind, extra, total_bytes, completed, failed,
                      skipped_n, cancelled):
        """(title, body) for a finished tray-started run. Numbers come from
        the run itself — never a canned "done"."""
        extra = extra or {}
        if cancelled:
            return ("Stopped", "The run was stopped early.")
        if kind == "clean":
            if total_bytes and total_bytes > 0:
                body = "Quick Clean finished"
                if failed:
                    body += f" ({failed} failed)"
                return (f"{format_bytes(total_bytes)} freed", body)
            if failed and not completed:
                return ("Clean failed", "Open the app to see why.")
            return ("Nothing to clean", "Your PC is already tidy.")
        if kind == "gm_on":
            if completed > 0:
                body = f"{completed} settings boosted"
                if extra.get("admin_missing"):
                    body += " · run as admin for the rest"
                return ("\U0001f3ae Game Mode on", body)
            if failed:
                return ("Game Mode failed", "Open the app to see why.")
            return ("\U0001f3ae Game Mode on", "Already set up for gaming.")
        if kind == "gm_off":
            if failed and not completed:
                return ("Game Mode off failed", "Open the app to see why.")
            return ("Game Mode off", "Normal settings are back.")
        return ("Done", "")

    def tray_quick_clean(self) -> None:
        """Tray Quick Clean: the curated preset through the standard run
        engine (scorecard waits in the app for later — never a modal over
        a game). Bounces busily with a toast + open like its sibling."""
        try:
            if getattr(self, "_busy", False):
                try:
                    from app.toast import show_toast as _toast
                    _toast("Cleaner Tool is busy",
                           "A run is in progress — open the app to watch it.")
                except Exception:
                    pass
                self.tray_open()
                return
            from app.tab_presets import PRESETS as _PR
            by_key = {t.key: t for t in TABS.get("Clean", [])}
            keys = _PR.get("Clean", {}).get("Quick Clean", [])
            tasks = [by_key[k] for k in keys if k in by_key]
            if not tasks:
                try:
                    from app.toast import show_toast as _toast
                    _toast("Quick Clean", "No clean tasks available.")
                except Exception:
                    pass
                return
            self._toast_async("Quick Clean started", "Working on it\u2026")
            # quiet: no modal scorecard over a game, and the tray run never
            # overwrites the user's saved Clean selection. The result
            # toast reports how much space was actually freed.
            self.run_tasks("Clean", tasks, mode="run", quiet=True,
                           toast_kind="clean")
        except Exception:
            pass

    def tray_check_ping(self) -> None:
        """Tray quick action: one honest ICMP ping to a fast, reliable
        anycast host (Cloudflare 1.1.1.1) — a "how's my connection right
        now" glance for before you queue up, not a per-game/region test
        (that's the full Game Ping tool on the Tools tab, which needs a
        window to show its results table). Reuses gameping.probe_icmp and
        its verdict() bands so "Excellent"/"Playable"/"Poor" mean the same
        thing here as they do there. Runs off the UI thread — a stalled
        or unreachable network must never freeze the tray or the window —
        and answers with a toast, no window required. A second click
        while one is in flight is a no-op rather than stacking probes."""
        with self._tray_action_lock:
            if getattr(self, "_tray_ping_busy", False):
                return
            self._tray_ping_busy = True
        def _work():
            try:
                from app.gameping import probe_icmp, verdict
                _sent, _recv, avg = probe_icmp("1.1.1.1", count=4)
                word, good = verdict(avg)
                if avg is None:
                    title, msg = "No reply", "Couldn't reach the internet just now."
                else:
                    title = f"Ping: {avg:.0f} ms \u2014 {word}"
                    msg = ("Good time to queue up." if good
                           else "Might be a rough match right now.")
                from app.toast import show_toast as _toast
                _toast(title, msg)
            except Exception:
                pass
            finally:
                def _clear():
                    with self._tray_action_lock:
                        self._tray_ping_busy = False
                # MED-014: this was `self.root.after(0, _clear)`. A
                # cross-thread after() is not merely slow, it can be DROPPED
                # outright, and a dropped callback does not raise - so the
                # except branch that used to follow never ran,
                # _tray_ping_busy stayed True, and "Check My Ping" was dead
                # for the rest of the session. The dispatcher is the same fix
                # the run-completion hops use: a queue drained by a Tk-thread
                # pump, so a delivery cannot be silently lost.
                self._dispatch.post(_clear)
        import threading as _th
        _th.Thread(target=_work, daemon=True, name="TrayPingCheck").start()

    def tray_speed_test(self) -> None:
        """Tray quick action: one real download + upload round through the
        same Cloudflare-edge engine (app/speed_test.py) the Tools tab's
        Speed Test card uses — not a separate/fake number. Uses that
        engine's smallest round size (the same one the full dialog opens
        with before escalating on a fast link) so a tray click answers in
        a few seconds instead of running the full multi-round climb,
        which needs the dialog's live progress bar to make sense of.
        Same off-UI-thread + toast pattern as Check My Ping: a starting
        toast first since this takes longer than a ping, then the result;
        never freezes the tray or the window either way."""
        with self._tray_action_lock:
            if getattr(self, "_tray_speedtest_busy", False):
                return
            self._tray_speedtest_busy = True
        def _start_toast():
            # own thread: show_toast blocks ~1-2s on PowerShell startup and
            # neither the UI thread nor the test itself should wait on it.
            try:
                from app.toast import show_toast as _toast
                _toast("Speed Test started",
                       "Checking your download and upload speed\u2026")
            except Exception:
                pass
        def _work():
            title, msg = "Speed Test", "Couldn't finish the test. Try again."
            try:
                from app import speed_test as _st
                down = _st.download_parallel(_st.DOWNLOAD_ROUNDS[0], streams=_st.PARALLEL_STREAMS)
                if down is None:
                    down = _st.download_parallel(_st.DOWNLOAD_ROUNDS[0],
                                                 streams=_st.PARALLEL_STREAMS,
                                                 url=_st.FALLBACK_DOWNLOAD_URL)
                up = _st.upload_parallel(_st.UPLOAD_ROUNDS[0], streams=_st.PARALLEL_STREAMS)
                if down is None and up is None:
                    msg = "Couldn't reach the test server just now."
                else:
                    # numbers go in the bold title line (same pattern as the
                    # ping toast) so the result is the first thing you read
                    title = (f"{_st.format_mbps(down)} down \u00b7 "
                             f"{_st.format_mbps(up)} up")
                    msg = "Your internet speed just now."
            except Exception:
                pass
            try:
                from app.toast import show_toast as _toast2
                _toast2(title, msg)
            except Exception:
                pass
            def _clear():
                with self._tray_action_lock:
                    self._tray_speedtest_busy = False
            # MED-014: was `self.root.after(0, _clear)` from the worker.
            # A dropped cross-thread after() does not raise, so the fallback
            # never fired and the flag stayed True - "Speed Test" dead for
            # the session. Routed through the dispatcher instead.
            self._dispatch.post(_clear)
        import threading as _th2
        _th2.Thread(target=_start_toast, daemon=True,
                    name="TraySpeedStartToast").start()
        _th2.Thread(target=_work, daemon=True, name="TraySpeedTest").start()

