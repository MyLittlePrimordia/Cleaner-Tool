"""WebcamDialog — extracted verbatim from app/gui.py (H12, batch 3).

One dialog per module, mirroring app/ui/widgets. Depends only on
app.ui below it, plus the app.utils helpers it already used; it
knows nothing
about any other dialog except where noted.
"""
from __future__ import annotations

import tkinter as tk
from tkinter import ttk, messagebox
from app.ui.dialogs._tool_page import ToolPage
from app.ui.base import Tooltip
from app.ui.theme import COLORS, F, TAB_ACCENTS


class WebcamDialog(ToolPage):
    """Webcam Test: one button, live preview, camera released on close.

    The camera itself is opened by the user's browser via getUserMedia,
    served from a throwaway loopback page this app starts and stops (see
    app/webcam_web.py for why, and for the lifecycle contract). Nothing
    is installed, downloaded or written to disk — still zero dependency.

    This dialog owns only the plumbing: start the local page, hand it to
    a browser window, watch the heartbeat, and tear everything down the
    moment either side closes so the camera never stays claimed.

    REDESIGN 2026-10: on the shared ToolPage shell — the bespoke hero
    (icon + headline + line) is replaced by the Header banner, the live
    status label stays in self.content and the two actions are footer
    buttons. The poll/heartbeat engine is untouched.
    """

    _POLL_MS = 700

    def __init__(self, parent, app):
        self.app = app
        self._srv = None
        self._poll_after = None
        self._running = False
        self._announced = False
        super().__init__(
            parent, title="Webcam Test", accent=TAB_ACCENTS["Clean"],
            scope="Uses your browser's camera permission; nothing is "
                  "installed or written to disk.",
            scroll=False)
        self.banner.set_state("\u25C9", TAB_ACCENTS["Clean"])
        self.banner.set_title("Test your camera.")
        self.banner.set_sub("Opens in your browser \u2014 choose Allow when "
                            "asked.")
        body = self.content

        # Simplified: one status line, two actions. The banner carries the
        # headline; the buttons live in the shared footer.
        hero = tk.Frame(body, bg=COLORS["bg"])
        hero.pack(expand=True)
        self._toggle_btn = self.add_button("Start test", self._toggle,
                                           kind="primary")
        self._settings_btn = self.add_button("Privacy settings",
                                             self._open_privacy)
        Tooltip(self._toggle_btn,
                "Opens the test window and asks your browser for camera access")
        Tooltip(self._settings_btn,
                "Windows' own camera permission page")

        self._info_lbl = tk.Label(
            hero, text="Ready", font=(F, 10), bg=COLORS["bg"],
            fg=COLORS["subtext"], wraplength=self.body_width(120),
            justify="center")
        self._info_lbl.pack(pady=(22, 0))

        self.on_close(self._shutdown)

    # -- helpers -------------------------------------------------------- #

    def _set_info(self, text):
        try:
            self._info_lbl.config(text=text)
        except Exception:
            pass

    def _set_btn(self, text):
        try:
            self._toggle_btn.config_text(text)
        except Exception:
            try:
                self._toggle_btn.config(text=text)
            except Exception:
                pass

    # -- start / stop --------------------------------------------------- #

    def _toggle(self):
        if self._running:
            self._shutdown()
            self._set_info("Test stopped — your camera has been released.")
        else:
            self._start()

    def _start(self):
        try:
            from app.webcam_web import WebcamTestServer, open_test_window
        except Exception:
            self._set_info("Webcam test engine unavailable on this build.")
            return

        srv = WebcamTestServer()
        try:
            url = srv.start()
        except Exception:
            url = ""
        if not url:
            self._set_info("Couldn't open a local test page (no free loopback "
                           "port). Close other local servers and try again.")
            try:
                srv.stop()
            except Exception:
                pass
            return

        try:
            mode = open_test_window(url)
        except Exception:
            mode = ""
        if not mode:
            try:
                srv.stop()
            except Exception:
                pass
            self._set_info("No web browser found on this PC. The test window "
                           "needs one (Edge, Chrome, Firefox or Brave) — this "
                           "is common on LTSC installs with no browser.")
            return

        self._srv = srv
        self._running = True
        self._announced = False
        self._set_btn("Stop Test")
        self._set_info("Opening the test window… choose Allow when your "
                       "browser asks for the camera.")
        try:
            self.app.log("  > Webcam Test opened (%s)." % mode)
        except Exception:
            pass
        self._schedule_poll()

    def _schedule_poll(self):
        try:
            self._poll_after = self._dlg.after(self._POLL_MS, self._poll)
        except Exception:
            self._poll_after = None

    def _poll(self):
        """Watch the test page. When it goes, the test is over."""
        self._poll_after = None
        if not self._running or self._srv is None:
            return
        try:
            reached = self._srv.page_reached()
            alive = self._srv.page_open()
        except Exception:
            reached, alive = False, False
        if alive:
            if reached and not self._announced:
                self._announced = True
                self._set_info("Test window is open. Close it (or press Stop "
                               "Test) when you're done — that releases the "
                               "camera.")
            self._schedule_poll()
            return
        self._shutdown()
        if reached:
            self._set_info("Test window closed — your camera has been "
                           "released.")
        else:
            self._set_info("The test window never opened. Try your browser "
                           "manually, or check Camera Privacy Settings.")

    def _shutdown(self):
        """Stop everything. Safe to call repeatedly, never raises.

        Killing the server is also the signal the page watches for: it
        stops its own camera tracks within a second of losing us, so no
        stream can outlive this dialog."""
        if self._poll_after is not None:
            try:
                self._dlg.after_cancel(self._poll_after)
            except Exception:
                pass
            self._poll_after = None
        srv, self._srv = self._srv, None
        if srv is not None:
            try:
                srv.stop()
            except Exception:
                pass
        self._running = False
        self._announced = False
        self._set_btn("Start Webcam Test")

    def _open_privacy(self):
        try:
            self.app._launch_settings("ms-settings:privacy-webcam")
            self._set_info("Opened Windows camera permissions. 'Let desktop "
                           "apps access your camera' must be on for any "
                           "browser to see it.")
        except Exception as exc:
            self._set_info("Could not open Settings (%s)." % exc)
