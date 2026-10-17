"""ToolPage: the popup layout the redesigned Tools dialogs share.

ToolShell (retired 2026-10; `_tool_shell.py` deleted) stacked seven zones and
wrapped the growing one in a rounded scroll panel that itself held a row of
StatTiles and another scroll panel. On the six newest tools that nesting
produced the problems users saw: a card inside a card inside a card, a stat row
taller than the list it introduced, two scrollbars, and panels that overlapped
when one asked for more height than its parent had.

REDESIGN 2026-10 (completion): ToolShell was already dead when the audit ran
(nothing subclassed it; Startup Manager only imported it), so it has been
removed. EVERY Tools popup now builds on ToolPage — the 2026-09 six, and the
2026-10 migration of the remaining nineteen.

ToolPage keeps what ToolShell got right (ThemedModal chrome, the worker ->
dispatcher threading contract, a single footer) and drops the nesting. It also
gained an opt-in `scroll=True`, because the shell's content zone deliberately
does NOT scroll (the six original dialogs put a self-scrolling DataTable there)
and a plain list/menu would otherwise be clipped by the header + footer:

    HEADER    badge, headline answering the question, one line of context
    TOOLBAR   one row: search / filters / toggles (optional)       (~36px)
    CONTENT   the only growing zone; the dialog fills it directly
    FOOTER    status line left, buttons right, 2px busy rail on top

Three content modes, picked by the subclass:

    (default)         the content frame fills the body directly. Right for a
                      dialog whose own content scrolls (a DataTable) -
                      nesting a table in a panel would give two scrollbars.
    scroll=True       the content is a plain list/menu; the panel hugs it and
                      scrolls. No stretch: the rows ARE the card.
    fit=True          AUTO-FIT. The content fills the viewport when it is
                      short and the panel scrolls when it is tall. Used by the
                      fixed-size dashboards and testers, whose content cannot
                      be guaranteed to fit every window the app allows (the
                      minimum window is 980x700) - without it the packer cut
                      the overflow off at the panel edge.

Subclass contract (unchanged from ToolShell):

    self.<state> = ...            # BEFORE super().__init__
    super().__init__(parent, title=..., scope=...)
    ... build toolbar / content ...
    self.on_close(self._stop)     # LAST
    self._start()
"""
from __future__ import annotations

import threading
import tkinter as tk

from app.ui.base import ThemedModal, Tooltip
from app.ui.theme import (COLORS, F, TAB_ACCENTS, _hex_lerp, _measure_font,
                          ellipsize)
from app.ui.widgets import AnimatedButton, ScrollableRoundedPanel
from app.ui.widgets.header import Header

GAP = 10
TOOL_ACCENT = TAB_ACCENTS["Tools"]

#: One button size per role. A secondary button is exactly as tall as the
#: primary one, so the footer reads as a row, not a staircase.
BTN = {"padx": 14, "pady": 6}
BTN_SM = {"padx": 10, "pady": 4}

RAIL_H = 2


def _ellipsize(font, text, avail):
    """Longest prefix of `text` that fits `avail` px, ending in an ellipsis."""
    if not text:
        return text
    ell = "\u2026"
    lo, hi = 0, len(text)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        try:
            fits = font.measure(text[:mid] + ell) <= avail
        except Exception:
            return text
        if fits:
            lo = mid
        else:
            hi = mid - 1
    return (text[:lo].rstrip() + ell) if lo > 0 else ell


def fit_label(container, label, full, reserve=0, min_room=56):
    """Ellipsize `label` to the width `container` really has.

    A Tk Label is CENTRE-anchored, so when pack hands it less width than its
    text it shears glyphs off BOTH ends instead of truncating them: that is
    how Auto-Pilot's watchlist headline lost its W and its final s, and how a
    Storage Insight row lost the size pill beside its title at the minimum
    window. Measuring the container and cutting with an ellipsis turns that
    into ordinary truncation, and the full text comes back as soon as there is
    room for it (theme.ellipsize returns the string unchanged when it fits).

    `reserve` is the room the row's other widgets take — an int, or a callable
    when it depends on the content. The label's own padding is subtracted as
    well, so the fitted text never overflows by the few pixels Tk adds around
    the text it is given.
    """
    state = {"room": -1}

    def _apply(event=None):
        try:
            ev_w = int(getattr(event, "width", 0) or 0)
            if event is not None and getattr(event, "widget", None) is not container:
                ev_w = 0        # a watched CHILD resized; the box did not
            w = ev_w or int(container.winfo_width())
        except Exception:
            return
        if w <= 24:
            return
        try:
            res = reserve() if callable(reserve) else int(reserve)
        except Exception:
            res = 0
        room = max(int(min_room), w - res)
        if room == state["room"]:
            return
        state["room"] = room
        try:
            fm = _measure_font(label.winfo_toplevel(), label.cget("font"))
            room -= max(0, int(label.winfo_reqwidth())
                        - int(fm.measure(str(label.cget("text")))))
            label.config(text=ellipsize(full, fm, max(int(min_room), room)))
        except Exception:
            pass

    try:
        container.bind("<Configure>", _apply, add="+")
    except Exception:
        pass
    # apply once right away: if the container already has its width (a row
    # built after the window settled), the fit is correct without waiting for
    # a resize; if it is still 1px, _apply returns and the Configure above
    # does the work
    _apply(None)
    return _apply


class StatusLine(tk.Frame):
    """Caption plus a 2px rail that only moves while something is running.

    Same four calls as ProgressStrip (start / set_text / stop / hide) so the
    dialogs' worker code does not change shape.
    """

    TICK_MS = 28

    def __init__(self, parent, accent=None):
        super().__init__(parent, bg=COLORS["bg"], bd=0)
        self._accent = accent or TOOL_ACCENT
        self._busy = False
        self._pos = 0.0
        self._after = None
        self._raw = ""
        self._rail = tk.Canvas(self, height=RAIL_H, bg=COLORS["bg"],
                               highlightthickness=0, bd=0)
        self._rail.pack(fill="x")
        self.caption = tk.Label(self, text="", font=(F, 9),
                                bg=COLORS["bg"], fg=COLORS["subtext"],
                                anchor="w", bd=0)
        self.caption.pack(fill="x", pady=(5, 0))
        self._rail.bind("<Configure>", lambda e: self._draw())
        self._rail.bind("<Destroy>", lambda e: self._cancel())
        # Re-fit the caption whenever the footer column changes width.
        self.caption.bind("<Configure>", lambda e: self._fit())

    # ---- caption ---------------------------------------------------- #
    def _set_caption(self, text, color=None):
        """Remember the FULL text; show as much of it as fits."""
        self._raw = str(text or "")
        if color is not None:
            try:
                self.caption.config(fg=color)
            except Exception:
                pass
        self._fit()

    def _fit(self):
        """Show the longest prefix of the caption that fits, then an ellipsis.

        AUDIT FIX (2026-10): the caption is a single unwrapped line in a
        footer column that shrinks as the buttons grow. A long status
        ("Scanned 47 folders and freed 12.4 GB") was sheared MID-CHARACTER at
        the column edge, which reads as a rendering fault rather than as
        truncation - the same defect this codebase already fixed once on the
        Startup Manager's source labels. Measure and end on an ellipsis
        instead. The full string is still what callers passed in, so nothing
        is lost except pixels.
        """
        raw = self._raw
        try:
            avail = int(self.caption.winfo_width())
        except Exception:
            return
        if avail <= 1:
            # Not laid out yet - show it whole; <Configure> re-fits later.
            text = raw
        else:
            try:
                import tkinter.font as _tkfont
                f = _tkfont.Font(font=self.caption.cget("font"))
                text = (raw if f.measure(raw) <= avail
                        else _ellipsize(f, raw, avail))
            except Exception:
                text = raw
        try:
            if self.caption.cget("text") != text:
                self.caption.config(text=text)
        except Exception:
            pass

    def start(self, text=""):
        self._set_caption(text, COLORS["subtext"])
        self._busy = True
        self._cancel()
        self._tick()

    def set_text(self, text):
        self._set_caption(text)

    def stop(self, text=None, color=None):
        self._busy = False
        self._cancel()
        if text is not None:
            self._set_caption(text, color or COLORS["subtext"])
        self._draw()

    def hide(self):
        self.stop("")

    def _cancel(self):
        if self._after is not None:
            try:
                self.after_cancel(self._after)
            except Exception:
                pass
            self._after = None

    def _tick(self):
        if not self._busy:
            return
        self._pos = (self._pos + 0.025) % 1.25
        self._draw()
        try:
            self._after = self.after(self.TICK_MS, self._tick)
        except Exception:
            self._after = None

    def _draw(self):
        try:
            c = self._rail
            c.delete("all")
            w = c.winfo_width()
            if w < 12:
                return
            c.create_rectangle(0, 0, w, RAIL_H, fill=COLORS["bg_alt"],
                               outline="")
            if self._busy:
                seg = max(60, w // 5)
                x = -seg + self._pos * (w + seg * 2)
                c.create_rectangle(max(0, x), 0, min(w, x + seg), RAIL_H,
                                   fill=self._accent, outline="")
        except Exception:
            pass


class ToolPage(ThemedModal):
    """ThemedModal + banner / toolbar / content / footer. See module doc."""

    def __init__(self, parent, *, title, accent=None, scope="",
                 with_toolbar=False, scroll=False, fit=False):
        super().__init__(parent, title=title, accent=accent or TOOL_ACCENT)
        self._accent = accent or TOOL_ACCENT
        body = self.body

        # footer is packed FIRST so its space is reserved (house rule)
        self.foot = tk.Frame(body, bg=COLORS["bg"], bd=0)
        self.foot.pack(side="bottom", fill="x", pady=(GAP, 0))
        self.foot.grid_columnconfigure(0, weight=1)
        self.status = StatusLine(self.foot, accent=self._accent)
        self.status.grid(row=0, column=0, sticky="ew", padx=(0, GAP))
        self.foot_btns = tk.Frame(self.foot, bg=COLORS["bg"], bd=0)
        self.foot_btns.grid(row=0, column=1, sticky="e")
        # kept for dialogs that still say self._progress
        self._progress = self.status

        self.scope_text = str(scope or "")
        self._info = None
        if scope:
            self._info = tk.Label(self.foot_btns, text="ⓘ", font=(F, 11),
                                  bg=COLORS["bg"], fg=COLORS["muted"],
                                  cursor="question_arrow", bd=0)
            self._info.pack(side="left", padx=(0, 10))
            try:
                Tooltip(self._info, self.scope_text)
            except Exception:
                pass

        self.banner = Header(body, accent=self._accent)
        self.banner.pack(fill="x", pady=(2, 0))

        self.toolbar = None
        if with_toolbar:
            self.toolbar = tk.Frame(body, bg=COLORS["bg"], bd=0)
            self.toolbar.pack(fill="x", pady=(GAP, 0))

        self.content = tk.Frame(body, bg=COLORS["bg"], bd=0)
        self._content_panel = None
        if scroll:
            # REDESIGN 2026-10: opt-in scrolling content zone. ToolPage's
            # content normally does NOT scroll, because the six dialogs it was
            # built for put a DataTable there and the table scrolls itself.
            # A dialog whose content is a plain list/menu (Quick Tools) does
            # need to scroll, or the header + footer squeeze it past the card
            # edge and the last rows are unreachable. Same shared panel the
            # Settings pane and Tools tab use, so the scrollbar matches.
            panel = ScrollableRoundedPanel(body)
            panel._fill = COLORS["bg"]
            panel.inner.configure(bg=COLORS["bg"])
            try:
                panel._redraw_bg()
            except Exception:
                pass
            panel.pack(fill="both", expand=True, pady=(GAP, 0))
            self._content_panel = panel
            self.content = tk.Frame(panel.inner, bg=COLORS["bg"], bd=0)
            self.content.pack(fill="x")
        elif fit:
            # AUDIT FIX (2026-10): auto-fit content zone. The content fills
            # the viewport when it is SHORT (so centred layouts and expand
            # children behave exactly as they did when the frame was packed
            # straight into the body) and the panel scrolls when it is TALL,
            # instead of the packer silently cutting the overflow off at the
            # panel edge. Used by the fixed-size dashboards and testers whose
            # content cannot be relied on to fit every window size the app
            # allows (the minimum window is 980x700). Table dialogs keep
            # scroll=False: their DataTable already owns its own scrolling,
            # and nesting it in a panel would give the popup two scrollbars.
            panel = ScrollableRoundedPanel(body, fill_inner=True)
            panel._fill = COLORS["bg"]
            panel.inner.configure(bg=COLORS["bg"])
            try:
                panel._redraw_bg()
            except Exception:
                pass
            panel.pack(fill="both", expand=True, pady=(GAP, 0))
            self._content_panel = panel
            self.content = tk.Frame(panel.inner, bg=COLORS["bg"], bd=0)
            self.content.pack(fill="both", expand=True)
        else:
            self.content.pack(fill="both", expand=True, pady=(GAP, 0))

    # ---- buttons ----------------------------------------------------- #
    def add_button(self, text, command, kind="secondary", **kw):
        """Footer button, packed right-to-left (first call = rightmost).

        kind: "primary" (accent fill), "secondary" (surface), "danger"
        (surface, red text), "warn" (surface, amber text).
        """
        # M3: one filled primary action, everything else outlined
        style = {
            "primary": (self._accent, COLORS["black"], (F, 10, "bold"), None),
            "secondary": (COLORS["bg"], COLORS["primary"], (F, 10, "bold"),
                          "outlined"),
            "danger": (COLORS["bg"], COLORS["accent_red"], (F, 10, "bold"),
                       "outlined"),
            "warn": (COLORS["bg"], COLORS["accent_yellow"], (F, 10, "bold"),
                     "outlined"),
        }[kind]
        b = AnimatedButton(self.foot_btns, text=text, command=command,
                           bg=style[0], fg=style[1], font=style[2],
                           variant=style[3],
                           **dict(dict(BTN, padx=22, pady=9), **kw))
        b.pack(side="right", padx=(8, 0))
        return b

    def toolbar_button(self, text, command, kind="secondary", side="left"):
        style = {
            "primary": (self._accent, COLORS["black"], (F, 10, "bold"), None),
            "secondary": (COLORS["bg"], COLORS["primary"], (F, 10, "bold"),
                          "outlined"),
            "warn": (COLORS["bg"], COLORS["accent_yellow"], (F, 10, "bold"),
                     "outlined"),
        }[kind]
        b = AnimatedButton(self.toolbar, text=text, command=command,
                           bg=style[0], fg=style[1], font=style[2],
                           variant=style[3], padx=18, pady=7)
        b.pack(side=side, padx=(0, 8) if side == "left" else (8, 0))
        return b

    # ---- helpers ----------------------------------------------------- #
    def _ui(self, fn, *args):
        """Worker -> Tk hop. BUG-001: never after(0) from a worker."""
        self._dispatch.post(lambda: fn(*args))

    def copy_text(self, text, ok="Copied.", fail="Could not copy."):
        try:
            self._dlg.clipboard_clear()
            self._dlg.clipboard_append(text)
            self.status.stop(ok)
            return True
        except Exception:
            self.status.stop(fail)
            return False

    def set_busy(self, busy):
        """Hook: subclasses disable their refresh button here."""

    def run_worker(self, fn, land, *args):
        """Run `fn(*args)` on a daemon thread; deliver its result to `land`,
        or the raised exception to the failure hook.

        R-003 / B-017: five dialogs threaded their engine call directly and
        then hopped back with self._ui(self._land, result) with NO try/except.
        Any raise inside the engine killed the thread before the hop, so
        set_busy(False) never ran and the popup stayed stuck busy (buttons
        disabled, status frozen on "Scanning...") for the rest of the session.
        Routing every worker through here guarantees the Tk-side state is
        always released.
        """
        def _work():
            try:
                result = fn(*args)
            except Exception as exc:  # noqa: BLE001 - report, never kill thread
                self._ui(self._worker_failed, exc)
                return
            self._ui(land, result)
        threading.Thread(target=_work, daemon=True).start()

    def _worker_failed(self, exc):
        """Land a failed background job: always clear busy, then say so."""
        try:
            self.set_busy(False)
        except Exception:
            pass
        try:
            self.status.stop("Something went wrong: %s" % (exc or "unknown error"),
                             COLORS["accent_red"])
        except Exception:
            pass
