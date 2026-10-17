"""ProgressStrip: the "this is still working" bar for slow tool scans.

Hashing a folder tree or reading every driver on the machine takes tens of
seconds to minutes. Four of the six Tools popups showed nothing at all
during that time beyond one frozen line of text, which reads as a hang.

ProgressStrip is a 4px rail with a caption line:

    ▬▬▬▬▬▬▬▬░░░░░░░░░░░░░░░░░░  Checking 4,102 of 18,402 files...

start() runs an indeterminate sweep for work whose size is unknown;
set(fraction, text) pins it to a real fraction once the count is known
(the dupe scanner knows how many files it has walked). stop() cancels the
timer, so a strip that is hidden mid-animation cannot keep redrawing a
destroyed canvas.

Depends only on app.ui.theme.
"""
from __future__ import annotations

import tkinter as tk

from app.ui.theme import COLORS, F, _hex_lerp

RAIL_H = 4
SEG_W = 90
SPEED = 0.045
TICK_MS = 24


class ProgressStrip(tk.Frame):
    """A thin progress rail plus a caption. Never raises out of any call."""

    def __init__(self, parent, accent=None, bg=None):
        self._page_bg = bg or (parent["bg"] if isinstance(
            parent, (tk.Frame, tk.Canvas)) else COLORS["bg"])
        super().__init__(parent, bg=self._page_bg, bd=0)
        self._accent = accent or COLORS["accent_sky"]
        self._pos = 0.0
        self._fraction = 0.0
        self._indeterminate = False
        self._after = None
        self._text = ""
        self._restore = None

        self.caption = tk.Label(self, text="", font=(F, 8),
                                bg=self._page_bg, fg=COLORS["subtext"],
                                anchor="w", justify="left", bd=0)
        self.caption.pack(fill="x", pady=(0, 3))

        self._cv = tk.Canvas(self, height=RAIL_H, highlightthickness=0, bd=0,
                             bg=self._page_bg)
        self._cv.pack(fill="x")
        self._cv.bind("<Configure>", lambda _e: self._draw())
        self._cv.bind("<Destroy>", lambda _e: self._cancel())

    # ---- api ---------------------------------------------------------- #

    def start(self, text=""):
        """Indeterminate. Use when the total is not knowable up front."""
        # BUGFIX 2026-10: this never called show(). ToolShell creates the
        # strip, packs it, and immediately hides it; nothing anywhere in the
        # app called show() either (verified by grep across all 129 modules).
        # So every progress caption in the six new Tools dialogs - "Checking
        # 4,102 of 18,402 files...", "Closed 2 programs.", "Copied 12 lines."
        # - was being written into a widget that was not on screen, and the
        # indeterminate sweep was invisible for the same reason. The dialogs
        # looked dead during exactly the operations that take minutes.
        self.show()
        self._indeterminate = True
        self._set_text(text)
        self._cancel()
        self._tick()

    def set_text(self, text):
        """Update the caption WITHOUT touching the animation.

        For a scan that cannot report a fraction - a file walk has no total
        until it finishes - the honest thing to show is a running count, and
        that means re-labelling the strip many times a second. Going through
        start()/stop() for that would restart the sweep on every tick.

        Also reveals the strip, because a running count is exactly the case
        where the user is waiting and the caption is the only feedback there
        is. Without this, the first call on a hidden strip stayed hidden.
        """
        self.show()
        self._set_text(text)

    def set(self, fraction, text=None):
        """Determinate. fraction is clamped to 0..1."""
        self.show()
        self._indeterminate = False
        self._fraction = max(0.0, min(1.0, float(fraction)))
        self._cancel()
        if text is not None:
            self._set_text(text)
        self._draw()

    def stop(self, text=None):
        """Stop the animation. The strip stays visible if given a caption."""
        # A terminal caption - "Closed 2 programs.", "No duplicates found." -
        # is the RESULT the user is waiting for, so it has to be on screen.
        # Reveal here too, for the same reason as start(): the strip is hidden
        # by default and this is often the only place the outcome appears.
        if text:
            self.show()
        self._cancel()
        self._indeterminate = False
        self._fraction = 1.0
        if text is not None:
            self._set_text(text)
        self._draw()

    def hide(self):
        """Unpack, remembering the exact pack arguments to restore.

        Re-packing with a fresh bare pack() would land the strip at the
        END of the zone stack - i.e. under the body panel, where nobody
        would ever see it. Capturing pack_info() and replaying it (including
        the sibling it was packed before) is what keeps it where the dialog
        put it.
        """
        self._cancel()
        self._set_text("")
        if self._restore is not None:
            return
        try:
            if not self.winfo_manager():
                return
            info = self.pack_info()
        except Exception:
            self._restore = {}
            return
        args = {}
        for key in ("side", "fill", "expand", "padx", "pady", "ipadx",
                    "ipady", "anchor"):
            try:
                value = info.get(key)
            except Exception:
                value = None
            if value not in (None, "", "none"):
                args[key] = value
        before = info.get("before")
        self._restore = {"args": args, "before": before or None}
        try:
            self.pack_forget()
        except Exception:
            pass

    def show(self):
        try:
            if self.winfo_manager() or not self._restore:
                return
            args = dict(self._restore.get("args") or {})
            before = self._restore.get("before")
            if before:
                args["before"] = before
            self.pack(**args)
        except Exception:
            pass

    # ---- internals ---------------------------------------------------- #

    def _set_text(self, text):
        self._text = str(text or "")
        try:
            self.caption.config(text=self._text)
        except Exception:
            pass

    def _cancel(self):
        if self._after is not None:
            try:
                self.after_cancel(self._after)
            except Exception:
                pass
            self._after = None

    def _tick(self):
        if not self._indeterminate:
            return
        try:
            if not self.winfo_exists():
                return
        except Exception:
            return
        self._pos = (self._pos + SPEED) % 1.2
        self._draw()
        try:
            self._after = self.after(TICK_MS, self._tick)
        except Exception:
            self._after = None

    def _draw(self):
        try:
            cv = self._cv
            cv.delete("all")
            w = cv.winfo_width()
            if w < 12:
                return
            cv.create_rectangle(0, 0, w, RAIL_H, outline="",
                                fill=COLORS["surface"])
            if self._indeterminate:
                x = -SEG_W + self._pos * (w + SEG_W * 2)
                cv.create_rectangle(x, 0, min(w, x + SEG_W), RAIL_H,
                                    outline="", fill=self._accent)
            else:
                fw = int(w * self._fraction)
                if fw > 0:
                    cv.create_rectangle(0, 0, fw, RAIL_H, outline="",
                                        fill=self._accent)
                # A dimmed remainder keeps the rail from looking broken when
                # the fraction is small.
                if fw < w:
                    cv.create_rectangle(fw, 0, w, RAIL_H, outline="",
                                        fill=_hex_lerp(COLORS["surface"],
                                                       self._accent, 0.18))
        except Exception:
            pass