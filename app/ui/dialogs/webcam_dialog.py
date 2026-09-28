"""WebcamDialog — extracted verbatim from app/gui.py (H12, batch 3).

One dialog per module, mirroring app/ui/widgets. Depends only on
app.ui below it, plus the app.utils helpers it already used; it
knows nothing
about any other dialog except where noted.
"""
from __future__ import annotations

import tkinter as tk
from tkinter import ttk, messagebox
from app.ui.base import ThemedModal, Tooltip
from app.ui.theme import COLORS, F, TAB_ACCENTS
from app.ui.widgets import AnimatedButton


class WebcamDialog(ThemedModal):
    """Webcam Test: one button, live preview, camera released on close.

    The camera itself is opened by the user's browser via getUserMedia,
    served from a throwaway loopback page this app starts and stops (see
    app/webcam_web.py for why, and for the lifecycle contract). Nothing
    is installed, downloaded or written to disk — still zero dependency.

    This dialog owns only the plumbing: start the local page, hand it to
    a browser window, watch the heartbeat, and tear everything down the
    moment either side closes so the camera never stays claimed.
    """

    _POLL_MS = 700

    def __init__(self, parent, app):
        self.app = app
        self._srv = None
        self._poll_after = None
        self._running = False
        self._announced = False
        super().__init__(parent, title="Webcam Test", accent=TAB_ACCENTS["Clean"])
        body = self.body

        tk.Label(body, text="Check your camera before you go live.",
                 font=(F, 10, "bold"), bg=COLORS["bg"],
                 fg=COLORS["text"], anchor="w").pack(fill="x", pady=(0, 2))
        tk.Label(body,
                 text=("Opens a private test window on this PC. Your browser "
                       "asks for camera permission — choose Allow, and you "
                       "should see yourself straight away."),
                 font=(F, 9), bg=COLORS["bg"], fg=COLORS["subtext"],
                 anchor="w", justify="left", wraplength=700).pack(
                     fill="x", pady=(0, 14))

        # What the test actually reports, so the card is not a black box
        # before it is pressed.
        card = tk.Frame(body, bg=COLORS["bg_alt"], bd=0, highlightthickness=1,
                        highlightbackground=COLORS["hairline"])
        card.pack(fill="x", pady=(0, 16))
        for _line in (
                "\u2022  Live picture — proves the camera, driver and cable all work",
                "\u2022  Resolution, megapixels and shape of the picture it sends",
                "\u2022  Frames per second, measured from the real stream",
                "\u2022  A plain-English reason when it will not start",
        ):
            tk.Label(card, text=_line, font=(F, 9), bg=COLORS["bg_alt"],
                     fg=COLORS["subtext"], anchor="w").pack(
                         fill="x", padx=16, pady=(9 if _line.endswith("work")
                                                  else 0, 9))

        row = tk.Frame(body, bg=COLORS["bg"])
        row.pack()
        self._toggle_btn = AnimatedButton(row, text="Start Webcam Test",
                                          command=self._toggle,
                                          bg=COLORS["accent_green"],
                                          fg=COLORS["black"],
                                          font=(F, 10, "bold"), padx=26, pady=9)
        self._toggle_btn.pack(side="left", padx=4)
        self._settings_btn = AnimatedButton(
            row, text="Camera Privacy Settings",
            command=self._open_privacy,
            bg=COLORS["surface"], fg=COLORS["text"],
            font=(F, 9, "bold"), padx=16, pady=9)
        self._settings_btn.pack(side="left", padx=4)
        Tooltip(self._toggle_btn,
                "Opens the test window and asks your browser for camera access")
        Tooltip(self._settings_btn,
                "Windows' own camera permission page — needed if access is "
                "blocked system-wide")

        self._info_lbl = tk.Label(
            body, text="Ready. Closing the test window releases your camera "
                       "for OBS, Discord or Teams.",
            font=(F, 9), bg=COLORS["bg"], fg=COLORS["subtext"],
            wraplength=700, justify="center")
        self._info_lbl.pack(pady=(14, 0))

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
