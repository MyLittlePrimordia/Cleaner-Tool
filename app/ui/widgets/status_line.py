"""StatusLine: the one live sentence at the bottom of the window.

Why this is not a Label
-----------------------
A `tk.Label` clips long text at its right edge. The status strip carries the
task a run is on, and a DISM/SFC line easily outgrows the window; the old label
therefore cut the sentence off mid-word with no indication that more existed,
which users read as a frozen UI ("it's stuck on 'DISM RestoreHea…'").

This widget paints the same text on a canvas, centred while it fits, and pans
it only when it does not. The pan is a two-item loop with a gap, so it never
jumps back to the beginning mid-word. It is deliberately quiet: one line, the
same type token as before, no chrome.

Tk-thread only, like every other widget in this package. `config(text=...)`
and `config(fg=...)` are kept as the call shape because `LogSurface` already
writes status through them.
"""
from __future__ import annotations

import tkinter as tk
import tkinter.font as tkfont

from app.ui.theme import COLORS, SPACE_2, SPACE_6

#: Horizontal breathing room inside the strip. The text is centred in the
#: remaining width, not edge to edge.
PAD = SPACE_2

#: Marquee speed. 28 px/s is readable while still crossing a long DISM line
#: in a few seconds. Faster reads as a ticker and is harder to follow.
SPEED_PX_S = 28.0

#: Blank space between the two copies of the text while looping.
GAP = SPACE_6

#: How long a too-long line sits still before it starts moving, so a newly
#: set status is readable from the beginning first.
HOLD_MS = 900


class StatusLine(tk.Canvas):
    """A one-line status label that stays centred and pans when it overflows."""

    def __init__(self, parent, font, bg=None, fg=None, height=24, **kw):
        self._page_bg = bg or COLORS["bg"]
        super().__init__(parent, height=int(height), highlightthickness=0,
                         bd=0, bg=self._page_bg, **kw)
        self._font_spec = font
        try:
            self._font = tkfont.Font(root=self, font=font)
        except Exception:
            self._font = None
        self._text = ""
        self._fg = fg or COLORS["subtext"]
        self._job = None
        self._offset = 0.0
        self._text_w = 0
        self._hold_until = 0.0
        self._last_tick = 0.0
        # Two copies for a seamless loop. The second is hidden while static.
        self._item_a = self.create_text(0, 0, text="", font=font,
                                        fill=self._fg, anchor="center",
                                        state="hidden")
        self._item_b = self.create_text(0, 0, text="", font=font,
                                        fill=self._fg, anchor="w",
                                        state="hidden")
        self.bind("<Configure>", self._on_configure, add="+")
        self.bind("<Destroy>", self._on_destroy, add="+")

    # ---- Label-compatible surface ------------------------------------- #

    def config(self, cnf=None, **kw):
        """Accept the `text` / `fg` keywords callers already use.

        Everything else is a real Canvas option and is forwarded untouched.
        """
        text = kw.pop("text", None)
        fg = kw.pop("fg", None)
        if text is not None:
            self.set_text(text)
        if fg is not None:
            self._fg = fg
            try:
                self.itemconfigure(self._item_a, fill=fg)
                self.itemconfigure(self._item_b, fill=fg)
            except Exception:
                pass
        if cnf or kw:
            try:
                super().config(cnf, **kw)
            except Exception:
                pass

    configure = config

    def cget(self, key):
        if key == "text":
            return self._text
        if key == "fg":
            return self._fg
        return super().cget(key)

    def set_text(self, text):
        self._text = str(text or "")
        self._offset = 0.0
        self._hold_until = 0.0
        self._last_tick = 0.0
        self._cancel()
        self._layout()

    def text(self):
        return self._text

    # ---- painting / animation ----------------------------------------- #

    def _on_configure(self, _e=None):
        self._layout()

    def _on_destroy(self, _e=None):
        self._cancel()

    def _cancel(self):
        try:
            if self._job is not None:
                self.after_cancel(self._job)
        except Exception:
            pass
        self._job = None

    def _layout(self):
        """Recompute the text width and choose static or panning mode."""
        try:
            if not self.winfo_exists():
                return
            w = max(1, int(self.winfo_width()))
            h = max(1, int(self.winfo_height()))
            room = max(10, w - 2 * PAD)
            text = self._text
            if not text:
                self.itemconfigure(self._item_a, state="hidden")
                self.itemconfigure(self._item_b, state="hidden")
                self._cancel()
                return
            tw = self._measure(text)
            self._text_w = tw
            if tw <= room:
                self._cancel()
                self._offset = 0.0
                self.itemconfigure(self._item_a, text=text, state="normal",
                                   anchor="center", justify="center")
                self.itemconfigure(self._item_b, state="hidden")
                self.coords(self._item_a, w / 2.0, h / 2.0)
                return
            # Overflow: pan. Start at the left so the sentence is read from
            # its beginning; the gap + second copy make the loop continuous.
            self.itemconfigure(self._item_a, text=text, state="normal",
                               anchor="w", justify="left")
            self.itemconfigure(self._item_b, text=text, state="normal",
                               anchor="w", justify="left")
            import time
            self._hold_until = time.monotonic() + HOLD_MS / 1000.0
            self._paint_marquee()
            self._schedule()
        except Exception:
            pass

    def _measure(self, text):
        try:
            if self._font is not None:
                return int(self._font.measure(text))
            return int(tkfont.Font(root=self, font=self._font_spec)
                       .measure(text))
        except Exception:
            # A font can only fail to measure in a broken Tk install. A
            # conservative estimate still lets the pan path run instead of
            # silently hiding the text.
            return max(10, len(text) * 7)

    def _paint_marquee(self):
        try:
            h = max(1, int(self.winfo_height()))
            x = PAD - self._offset
            self.coords(self._item_a, x, h / 2.0)
            self.coords(self._item_b,
                        x + self._text_w + GAP, h / 2.0)
        except Exception:
            pass

    def _schedule(self):
        if self._job is not None:
            return
        try:
            self._job = self.after(33, self._tick)
        except Exception:
            self._job = None

    def _tick(self):
        self._job = None
        try:
            if not self.winfo_exists():
                return
            import time
            now = time.monotonic()
            if now < self._hold_until:
                self._last_tick = now
                self._schedule()
                return
            if self._last_tick <= 0:
                self._last_tick = now
            dt = min(0.25, max(0.0, now - self._last_tick))
            self._last_tick = now
            self._offset += SPEED_PX_S * dt
            span = self._text_w + GAP
            if span > 0 and self._offset >= span:
                self._offset -= span
            self._paint_marquee()
            self._schedule()
        except Exception:
            pass
