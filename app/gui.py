"""
GUI layer — Phase 2 redesign for non-technical gamers.

Design goals:
  * One question answered in under 2 seconds: "which big button do I press?"
  * No console output, no command lines, no log window. A colored animated
    progress bar with plain-language status; the full log stays in memory
    and is exportable to .txt via Quick Tools > Export Logs.
  * Dark theme only (navy palette from user reference), accents per tab:
    mint = Clean, amber = Repair, violet = Tweak, red = Undo.
  * The 3 tabs are one pill-shaped switcher: a thumb slides between
    Clean / Repair / Tweak, recoloring to the selected tab.
  * Presets are big cards. Only "Custom" ever shows a toggle grid.
  * Toggles are animated pill switches (green on / red-muting off), with a
    separate "✓ Active" badge sourced from the real applied-tweak registry,
    so "will run" (toggle) never reads as "is active on this PC" (badge).
  * Run replaces itself with an in-place animated progress bar; Undo
    Tweaks is its own red card on the Tweak tab.
  * Thread-safe: the worker never touches widgets directly; every UI
    update hops to the Tk thread via root.after.

Phase 2 punch-list items implemented here: #12 (tab consolidation),
#13 (preset system), #14 (toggles + applied badges), #15 (Undo flow),
#16 (fading status + hidden full log), #17 (single "Run" label).
"""

import os
import pathlib
import subprocess
import sys
import threading
import time
import tkinter as tk
from tkinter import ttk, messagebox
import tkinter.font as tkfont

from app.config import (
    APP_NAME, WINDOW_SIZE, WINDOW_MIN_SIZE, COLORS, FONT_FAMILY,
)
from app.elevation import is_admin, relaunch_as_admin
from app.utils import TaskContext, TaskSkipped, TaskCancelled, format_bytes
from app.tab_presets import TABS, PRESETS, TAB_NAMES
from app.toast import notify_clean_complete
from app.warnings import check_dangerous_combos
from app.config_persist import get_tweak_state, mark_tweak_applied, mark_tweak_reverted


def _set_window_icon(root: tk.Tk):
    """Set colored 🧼 window icon next to title Cleaner — no black square."""
    try:
        candidates = []
        if getattr(sys, "_MEIPASS", None):
            candidates.append(pathlib.Path(sys._MEIPASS) / "app" / "assets" / "icon.ico")
            candidates.append(pathlib.Path(sys._MEIPASS) / "assets" / "icon.ico")
        candidates.append(pathlib.Path(__file__).with_name("assets") / "icon.ico")
        for p in candidates:
            if p.is_file():
                root.iconbitmap(str(p))
                return
    except Exception:
        pass


TAB_ACCENTS = {
    "Clean": COLORS["accent_sky"],
    "Repair": COLORS["accent_yellow"],
    "Tweak": COLORS["accent_blue"],
    "Install": COLORS["accent_green"],
}

F = FONT_FAMILY


# --------------------------------------------------------------------------- #
# Tooltip — shared single instance, dark, small
# --------------------------------------------------------------------------- #

class Tooltip:
    # F8: tips appear after a short hover-hold instead of instantly. Sweeping
    # the pointer across Install rows (up to 4 tooltips per row) used to
    # map/unmap the shared window on every row crossing; the delay collapses
    # that churn into one show per deliberate hover.
    SHOW_DELAY_MS = 350

    _shared_tip = None
    _shared_label = None
    _current_widget = None
    # F8: one pending (scheduled, not yet shown) tip at a time. Entering a
    # different tooltip-bearing widget cancels the previous widget's timer.
    _pending_after = None
    _pending_owner = None

    def __init__(self, widget, text: str):
        self.widget = widget
        self.text = text
        widget.bind("<Enter>", self._on_enter)
        widget.bind("<Leave>", self._hide)
        # F8: a destroyed widget must not leave a pending timer behind —
        # Tk after-ids outlive the widget that scheduled them, so the timer
        # is cancelled here (add="+": never wipe another <Destroy> handler)
        widget.bind("<Destroy>", self._on_destroy, add="+")

    @classmethod
    def _ensure_shared(cls, widget):
        """Create the shared tooltip window if needed — parented to the
        ROOT window, never to the hovered widget.

        Bug (user-reported crash spam): the shared Toplevel was parented to
        whichever label was first hovered. Task pages rebuild on every
        preset/mode switch, destroying those labels — and Tk destroys the
        tooltip window WITH its parent. The class kept holding the dead
        reference, so every later hover threw
        'invalid command name ...!toplevel.!label'. Parenting to root
        (which lives for the whole session) plus winfo_exists guards makes
        the tooltip immune to page rebuilds."""
        if cls._shared_tip is not None and not cls._shared_tip.winfo_exists():
            # stale handle (shouldn't happen with root parenting, but be
            # safe) — forget it and rebuild
            cls._shared_tip = None
            cls._shared_label = None
        if cls._shared_tip is None:
            root = widget.winfo_toplevel()
            cls._shared_tip = tw = tk.Toplevel(root)
            tw.wm_overrideredirect(True)
            tw.configure(bg=COLORS["hairline"])
            cls._shared_label = tk.Label(
                tw, text="", bg=COLORS["surface"], fg=COLORS["text"],
                font=(F, 9), justify="left", wraplength=420,
                padx=10, pady=7, bd=0,
            )
            cls._shared_label.pack()
            tw.withdraw()  # start hidden; _show positions + deiconifies

    # ---- F8: delayed show ------------------------------------------- #

    def _on_enter(self, event=None):
        """Hover starts the show-delay clock instead of showing at once."""
        Tooltip._cancel_pending_show()
        if Tooltip._current_widget is not None and Tooltip._current_widget is not self.widget:
            # the pointer reached a NEW tooltip-bearing widget while an old
            # tip is still visible — drop it now (no stale tip lingering
            # through the new widget's delay)
            Tooltip._hide_shared()
        try:
            if not self.widget.winfo_exists():
                return
        except Exception:
            return
        if not self.text:
            return
        Tooltip._pending_owner = self
        try:
            Tooltip._pending_after = self.widget.after(
                self.SHOW_DELAY_MS, self._fire_pending)
        except Exception:
            Tooltip._pending_after = None
            Tooltip._pending_owner = None

    @classmethod
    def _cancel_pending_show(cls):
        """Drop a scheduled-but-not-yet-shown tip (leave / destroy / a
        different widget entered first). after ids are interpreter-wide and
        outlive the widget, so cancelling through the owner is safe even
        when the owner is gone (guarded)."""
        if cls._pending_after is None:
            return
        try:
            if cls._pending_owner is not None:
                cls._pending_owner.widget.after_cancel(cls._pending_after)
        except Exception:
            pass
        cls._pending_after = None
        cls._pending_owner = None

    def _fire_pending(self):
        """F8: the delay elapsed. Show only if the pointer is STILL over
        this widget — a Leave that never arrived (e.g. the page scrolled
        under a stationary pointer) must not pop a stale tip."""
        Tooltip._pending_after = None
        Tooltip._pending_owner = None
        if not self._pointer_over():
            return
        self._show()

    def _pointer_over(self) -> bool:
        """True while the pointer is inside this widget or one of its
        children (root coords, same probe the row hover uses)."""
        try:
            if not self.widget.winfo_exists():
                return False
            x, y = self.widget.winfo_pointerxy()
            w = self.widget.winfo_containing(x, y)
            while w is not None and w is not self.widget:
                w = w.master
            return w is not None
        except Exception:
            return False

    def _on_destroy(self, _e=None):
        """F8: widget died — cancel its pending show and withdraw a tip it
        still owns."""
        if Tooltip._pending_owner is self:
            Tooltip._cancel_pending_show()
        if Tooltip._current_widget == self.widget:
            Tooltip._hide_shared()

    # ---- display ----------------------------------------------------- #

    def _show(self, event=None):
        if not self.text:
            return
        try:
            if not self.widget.winfo_exists():
                return
        except Exception:
            return
        if Tooltip._current_widget is not None and Tooltip._current_widget != self.widget:
            Tooltip._hide_shared()
        Tooltip._current_widget = self.widget
        x = self.widget.winfo_rootx() + 16
        y = self.widget.winfo_rooty() + self.widget.winfo_height() + 6
        # Readable tooltips (user feedback: one-line 80-char cap cut off
        # legit content, e.g. app descriptions + links). Cap at ~170 chars
        # with wraplength so tips flow over 2-3 lines instead of truncating.
        tip = self.text.strip()
        flat = " ".join(tip.split())
        if len(flat) > 170:
            flat = flat[:169].rstrip() + "…"
        Tooltip._ensure_shared(self.widget)
        if Tooltip._shared_tip is None or Tooltip._shared_label is None:
            return
        Tooltip._shared_label.config(text=flat)
        Tooltip._shared_tip.wm_geometry(f"+{x}+{y}")
        Tooltip._shared_tip.deiconify()

    def _hide(self, event=None):
        """Leave (or a programmatic hide): cancel this widget's pending
        show, then withdraw the tip if this widget owns the visible one."""
        if Tooltip._pending_owner is self:
            Tooltip._cancel_pending_show()
        if Tooltip._current_widget == self.widget:
            Tooltip._hide_shared()

    @classmethod
    def _hide_shared(cls):
        if cls._shared_tip is not None and cls._shared_tip.winfo_exists():
            cls._shared_tip.withdraw()
        cls._current_widget = None


# --------------------------------------------------------------------------- #
# Animated primitives
# --------------------------------------------------------------------------- #

def _lerp(a, b, t):
    # clamp t: spring overshoot can push t past 1.0, producing colors
    # outside 00-FF (invalid Tk color names) and off-canvas coords
    t = max(0.0, min(1.0, t)) if isinstance(t, float) else t
    return a + (b - a) * t


def _hex_lerp(c1, c2, t):
    """Blend two #rrggbb colors. t is clamped to [0, 1] — spring overshoot
    can pass 1.0 and would otherwise produce >FF channel values, which Tk
    rejects as invalid color names."""
    t = max(0.0, min(1.0, t))
    r1, g1, b1 = int(c1[1:3], 16), int(c1[3:5], 16), int(c1[5:7], 16)
    r2, g2, b2 = int(c2[1:3], 16), int(c2[3:5], 16), int(c2[5:7], 16)
    return f"#{int(_lerp(r1, r2, t)):02x}{int(_lerp(g1, g2, t)):02x}{int(_lerp(b1, b2, t)):02x}"


def _round_rect_points(x0, y0, x1, y1, r, steps=5):
    """Point list for a rounded rectangle (for Tk smooth polygons, which
    ARE antialiased on Windows — unlike create_rectangle)."""
    import math
    pts = []
    corners = [(x1 - r, y0 + r, -90), (x1 - r, y1 - r, 0),
               (x0 + r, y1 - r, 90), (x0 + r, y0 + r, 180)]
    for cx, cy, start in corners:
        for i in range(steps + 1):
            a = math.radians(start + 90 * i / steps)
            pts += [cx + r * math.cos(a), cy + r * math.sin(a)]
    return pts


class RoundedEntry(tk.Frame):
    """Text entry with rounded corners (user request: filter/search boxes).

    Tk Entries are always rectangular, so a canvas behind draws the rounded
    background + focus ring while a borderless Entry sits on top. Drop-in:
    pack/grid the RoundedEntry itself and use `.entry` exactly like the
    Entry it replaces (textvariable, binds, width all live there).
    Focus ring turns the tab accent on focus, hairline otherwise."""

    RADIUS = 9
    PAD_Y = 5

    def __init__(self, parent, textvariable=None, width=20, font=None,
                 entry_bg=None, accent=None, **kw):
        super().__init__(parent, bg=parent["bg"] if isinstance(parent, tk.Frame) else COLORS["bg"])
        self._accent = accent or COLORS["accent_green"]
        self._fill = entry_bg or COLORS["surface"]
        self._cv = tk.Canvas(self, highlightthickness=0, bd=0, bg=self["bg"])
        self._cv.place(relx=0, rely=0, relwidth=1, relheight=1)
        self.entry = tk.Entry(self, textvariable=textvariable, width=width,
                              bg=self._fill, fg=COLORS["text"],
                              insertbackground=COLORS["text"],
                              font=font or (F, 9), bd=0, relief="flat",
                              highlightthickness=0, **kw)
        self.entry.pack(fill="x", padx=(self.RADIUS + 2, self.RADIUS + 2),
                        pady=self.PAD_Y)
        self.bind("<Configure>", lambda e: self._draw(focused=False))
        # add="+" so call-site placeholder binds (FocusIn/Out) keep working
        self.entry.bind("<FocusIn>", lambda e: self._draw(focused=True), add="+")
        self.entry.bind("<FocusOut>", lambda e: self._draw(focused=False), add="+")
        self.after(0, lambda: self._draw(focused=False))

    def _draw(self, focused):
        try:
            w, h = self.winfo_width(), self.winfo_height()
            if w < 4 or h < 4:
                return
            self._cv.delete("all")
            self._cv.create_polygon(
                _round_rect_points(1, 1, w - 1, h - 1, self.RADIUS),
                smooth=True, fill=self._fill,
                outline=self._accent if focused else COLORS["hairline"], width=2)
        except Exception:
            pass



def _measure_font(root, font):
    """F7: ONE tkfont.Font per (root, font spec), cached on the root.

    _measure used to create + destroy a throwaway tk.Label per call just to
    ask the font how wide/tall the text is — and config_text runs on every
    run-count change, so All On/All Off paid one widget create/destroy +
    redraw per row. Font objects are cheap to keep and die with their root,
    so the cache lives ON the root object (a harness that destroys and
    recreates roots can never serve a stale handle)."""
    cache = getattr(root, "_measure_fonts", None)
    if cache is None:
        cache = root._measure_fonts = {}
    f = cache.get(font)
    if f is None:
        f = cache[font] = tkfont.Font(root=root, font=font)
    return f


class AnimatedButton(tk.Canvas):
    """Rounded button with press/relax animation and hover glow. Renders
    text inside the canvas so the whole pill animates as one piece."""

    def __init__(self, parent, text, command=None, bg=COLORS["surface"],
                 fg=COLORS["text"], hover_bg=None, font=None, padx=26, pady=10,
                 width=None, height=None, **kw):
        super().__init__(parent, highlightthickness=0, bd=0,
                         bg=parent["bg"] if isinstance(parent, (tk.Frame, tk.Canvas)) else COLORS["bg"],
                         **kw)
        self._text = text
        self._command = command
        self._bg = bg
        self._fg = fg
        self._hover_bg = hover_bg or _hex_lerp(bg, "#FFFFFF", 0.08)
        self._font = font or (F, 10, "bold")
        self._padx = padx
        self._pady = pady
        self._press = 0.0          # 0 relaxed .. 1 pressed
        self._hover = 0.0
        # Must exist BEFORE any <Enter>/<Leave> can fire — Tk can deliver
        # a hover event before a click ever happens (user-reported crash).
        self._pressing = False
        self._hovering = False
        self._current_bg = bg
        self._enabled = True
        self._fixed_w = width
        self._fixed_h = height
        self._focus_ring = False

        # keyboard accessibility (audit a11y finding): canvas widgets are
        # invisible to Tab focus by default — accept focus, activate on
        # Return/Space. Focus indication is drawn IN-CANVAS (user feedback:
        # Tk's native highlightthickness box read as a stray 'framework
        # outline' left behind after clicking) — no native focus rectangle.
        self.configure(takefocus=1, highlightthickness=0)
        self.bind("<ButtonPress-1>", self._on_press)
        self.bind("<ButtonRelease-1>", self._on_release)
        self.bind("<Enter>", self._on_enter)
        self.bind("<Leave>", self._on_leave)
        self.bind("<FocusIn>", lambda e: self._draw_focus(True))
        self.bind("<FocusOut>", lambda e: self._draw_focus(False))
        self.bind("<Return>", self._on_key_activate)
        self.bind("<space>", self._on_key_activate)
        self._measure()
        self._draw()

    def _measure(self):
        # F7: ask the cached shared font instead of a throwaway tk.Label.
        # A text-only Label's request size is font.measure(text)+6 wide by
        # font.metrics("linespace")+6 tall (Tk Label border/padding
        # defaults — verified equal to real Label widgets across every
        # in-app font/text pair). Single-line text only, like every button
        # label in this UI; multiline would need per-line measuring.
        fnt = _measure_font(self.winfo_toplevel(), self._font)
        w = fnt.measure(self._text) + 6
        h = fnt.metrics("linespace") + 6
        w += self._padx * 2
        h += self._pady * 2
        if self._fixed_w:
            w = self._fixed_w
        if self._fixed_h:
            h = self._fixed_h
        self.config(width=w, height=h)
        # NOTE: _w/_h are tkinter's reserved widget-path attribute names —
        # store pixel size under _bw/_bh instead (clobbering _w makes the
        # widget's Tcl path an integer and every later canvas call fails
        # with "invalid command name").
        self._bw, self._bh = w, h

    def _draw(self):
        self.delete("all")
        # Press: shrink 3% and darken slightly; hover: lighten slightly.
        t = self._press
        shrink = t * 0.04
        w = self._bw * (1 - shrink)
        h = self._bh * (1 - shrink)
        x0 = (self._bw - w) / 2
        y0 = (self._bh - h) / 2
        color = _hex_lerp(self._current_bg, self._hover_bg, self._hover)
        color = _hex_lerp(color, "#000000", t * 0.12)
        if not self._enabled:
            # disabled: muted flat fill, dimmed text
            color = _hex_lerp(color, COLORS["bg"], 0.45)
        r = h / 2 if h < 44 else 14
        self._round_rect(x0, y0, x0 + w, y0 + h, r, fill=color, outline="")
        if self._focus_ring and self._enabled:
            # in-canvas keyboard focus: 1px brightened outline just inside
            # the pill — visible only while actually Tab-focused
            self._round_rect(x0 + 1.5, y0 + 1.5, x0 + w - 1.5, y0 + h - 1.5,
                             max(1, r - 1.5), outline=_hex_lerp(color, "#FFFFFF", 0.55),
                             width=1)
        fg = self._fg if self._enabled else _hex_lerp(self._fg, COLORS["bg"], 0.45)
        self.create_text(
            self._bw / 2, self._bh / 2, text=self._text,
            font=self._font, fill=fg,
        )

    def _round_rect(self, x0, y0, x1, y1, r, **kw):
        points = [
            x0 + r, y0, x1 - r, y0, x1, y0, x1, y0 + r,
            x1, y1 - r, x1, y1, x1 - r, y1, x0 + r, y1,
            x0, y1, x0, y1 - r, x0, y0 + r, x0, y0,
        ]
        return self.create_polygon(points, smooth=True, **kw)

    def _draw_focus(self, on: bool):
        """Keyboard focus state — rendered in-canvas by _draw (no native
        Tk focus rectangle; user feedback called that a stray outline)."""
        self._focus_ring = on
        self._draw()

    # animation loop
    def _animate(self):
        target_p = 1.0 if self._pressing else 0.0
        target_h = 1.0 if self._hovering else 0.0
        moved = False
        if abs(self._press - target_p) > 0.02:
            self._press = _lerp(self._press, target_p, 0.35)
            moved = True
        if abs(self._hover - target_h) > 0.02:
            self._hover = _lerp(self._hover, target_h, 0.30)
            moved = True
        if moved:
            self._draw()
            self.after(16, self._animate)
        else:
            self._press, self._hover = target_p, target_h
            self._draw()

    def _on_press(self, _):
        if self._enabled:
            self._pressing = True
            self._animate()

    def _on_release(self, _):
        if self._enabled and getattr(self, "_pressing", False):
            self._pressing = False
            self._animate()
            # small haptic-feel delay then fire
            self.after(60, self._fire)

    def _on_key_activate(self, _):
        """Keyboard activation for a Tab-focused button (<Return>/<space>).

        M2 audit fix: this used to call _on_release directly, but that path
        only fires the command when <ButtonPress-1> has first set
        _pressing — so keyboard focus was advertised (takefocus=1) while
        Enter/Space never fired anything. The mouse path is press (set
        _pressing) -> release (fire); a key press has no separate press
        event, so synthesize the press half and run the exact same release
        sequence (same animation, same 60ms-then-fire). The `not
        self._pressing` guard stops held-key auto-repeat from re-firing the
        command."""
        if self._enabled and not self._pressing:
            self._pressing = True
            self._on_release(None)

    def _fire(self):
        if self._enabled and self._command:
            try:
                self._command()
            except Exception as exc:
                # audit fix: bare except made a buggy command a silent no-op
                # — the click appeared to do nothing with zero feedback.
                import traceback
                try:
                    messagebox.showerror(
                        "Unexpected Error",
                        f"Something went wrong running that button:\n\n{exc}\n\n"
                        f"(Full details are in the exported log: Quick Tools > Export Logs)",
                    )
                except Exception:
                    pass
                # surface to stderr too — captured by Export Logs when a
                # console exists (source runs), invisible in windowed builds
                traceback.print_exc()

    def _on_enter(self, _):
        if self._enabled:
            self._hovering = True
            self._animate()

    def _on_leave(self, _):
        self._hovering = False
        self._pressing = False
        self._animate()

    def set_style(self, bg=None, fg=None, hover_bg=None):
        if bg:
            self._bg = self._current_bg = bg
        if fg:
            self._fg = fg
        if hover_bg:
            self._hover_bg = hover_bg
        self._draw()

    def set_enabled(self, enabled: bool):
        self._enabled = enabled
        self._draw()

    def config_text(self, text: str):
        if text == self._text:
            return  # F7: unchanged label — skip re-measure + full redraw
        self._text = text
        self._measure()
        self._draw()


class ToggleSwitch(tk.Canvas):
    """iOS-style toggle, MK3 (user feedback: 'low-res, unpolished, hover
    shows a framework outline').

    What changed vs MK2:
      * SIZE: 44x24 (iOS 'mini' proportions — same shape language, less
        vertical bulk, enables 2-column Custom grids).
      * ANTIALIASING: Tk's create_oval/create_rectangle are NOT antialiased
        on Windows — the capsule caps and knob had visibly jagged edges
        (the 'low-res' look). Everything is now drawn with smooth=True
        polygons (Tk antialiases those), so all edges are crisp.
      * NO HOVER RIM: the old hover affordance drew 3 outline shapes
        slightly OUTSIDE the capsule bounds — at this scale it read as a
        boxy 'framework outline' around the clickable area (user-reported).
        Hover now just brightens the track; keyboard focus brightens it
        slightly more. No outlines anywhere.

    Motion: unchanged critically-damped ease-out (~200ms), no overshoot.
    Track color crossfades muted slate → iOS green (#34C759); knob white
    when ON, gray when OFF so state reads at a glance."""

    # iOS-mini metrics (points ≈ px at 100% scaling)
    W, H = 44, 24
    INSET = 1.5
    KNOB_INSET = 2.5
    GREEN = "#34C759"        # Apple system green
    KNOB_ON = "#FFFFFF"
    KNOB_OFF = "#C3CAD4"     # gray when off — state reads at a glance
    SHADOW = "#000000"
    OFF_TRACK = "#3E4A5A"     # neutral slate, matches dark UI
    EASE = 0.32              # per-tick approach @16ms (settles ~200ms)

    def __init__(self, parent, variable: tk.BooleanVar, command=None,
                 accent=None, **kw):  # accent kept for API compat, ignored
        super().__init__(parent, highlightthickness=0, bd=0,
                         bg=parent["bg"] if isinstance(parent, (tk.Frame, tk.Canvas)) else COLORS["bg"],
                         width=self.W, height=self.H, cursor="hand2", **kw)
        self._var = variable
        self._command = command
        self._pos = 1.0 if variable.get() else 0.0   # 0 off .. 1 on
        self._vel = 0.0                              # kept for API compat (no overshoot now)
        self._target = self._pos
        self._enabled = True
        self._anim_after = None
        self._focused = False
        self.bind("<Button-1>", self._toggle)
        self.bind("<Enter>", lambda e: self._draw(hover=True))
        self.bind("<Leave>", lambda e: self._draw())
        # keyboard accessibility (audit a11y): space/Return toggle, Tab focuses
        self.configure(takefocus=1)
        self.bind("<space>", lambda e: self._toggle(e))
        self.bind("<Return>", lambda e: self._toggle(e))
        self.bind("<FocusIn>", self._on_focus_in)
        self.bind("<FocusOut>", self._on_focus_out)
        variable.trace_add("write", lambda *_: self._sync_from_var())
        self._draw()

    def _on_focus_in(self, _e=None):
        self._focused = True
        self._draw()

    def _on_focus_out(self, _e=None):
        self._focused = False
        self._draw()

    # -- interaction -------------------------------------------------- #

    def _toggle(self, _):
        if not self._enabled:
            return
        self._var.set(not self._var.get())
        if self._command:
            self.after(10, self._command)

    def _sync_from_var(self):
        self._target = 1.0 if self._var.get() else 0.0
        self._start_anim()

    def set_enabled(self, enabled: bool):
        self._enabled = enabled
        self._draw()

    # -- ease-out animation -------------------------------------------- #

    def _start_anim(self):
        if self._anim_after is not None:
            try:
                self.after_cancel(self._anim_after)
            except Exception:
                pass
        self._tick_anim()

    def _tick_anim(self):
        # critically damped: monotonic approach, settles exact, no wobble
        self._pos += (self._target - self._pos) * self.EASE
        if abs(self._target - self._pos) < 0.004:
            self._pos = self._target
            self._vel = 0.0
            self._draw()
            self._anim_after = None
            return
        self._draw()
        self._anim_after = self.after(16, self._tick_anim)

    # -- rendering ------------------------------------------------------ #

    @property
    def _knob_d(self):
        return self.H - self.KNOB_INSET * 2

    def _travel(self):
        return self.W - self.KNOB_INSET * 2 - self._knob_d

    def _draw(self, hover=False):
        self.delete("all")
        w, h = self.W, self.H
        ins = self.INSET
        self._focused_now = self._focused
        # track: ONE smooth capsule polygon (rect middle + semicircle ends
        # in a single antialiased shape) — MK2's rect+2-ovals had unantialiased
        # seams and jagged caps.
        r = (h - ins * 2) / 2
        x0, y0, x1, y1 = ins, ins, w - ins, h - ins
        track = _hex_lerp(self.OFF_TRACK, self.GREEN, self._pos)
        if hover and self._enabled:
            track = _hex_lerp(track, "#FFFFFF", 0.12)
        elif self._focused_now and self._enabled:
            track = _hex_lerp(track, "#FFFFFF", 0.08)
        self._capsule(x0, y0, x1, y1, r, fill=track)
        # knob: smooth-polygon circle (antialiased, unlike create_oval)
        kd = self._knob_d
        cx = self.KNOB_INSET + kd / 2 + self._pos * self._travel()
        knob = _hex_lerp(self.KNOB_OFF, self.KNOB_ON, self._pos)
        if self._enabled:
            self._circle(cx + 0.5, self.KNOB_INSET + kd / 2 + 1.5, kd,
                          fill=self.SHADOW, stipple="gray25")
        self._circle(cx, self.KNOB_INSET + kd / 2, kd, fill=knob)
        # NOTE (user feedback): the old hover rim — 3 outline shapes drawn
        # slightly outside the capsule — is GONE. It read as a 'framework
        # outline' around the clickable area at this scale.

    def _capsule(self, x0, y0, x1, y1, r, **kw):
        """One smooth polygon: rectangle with two TRUE semicircle caps. The
        old version listed only the cap corners and let smooth=True guess
        the curve, which flattened the ends (user feedback: ends weren't
        fully rounded around the knob). Now both ends are sampled along
        real arcs (8 segments each), so the track hugs the circular knob
        with perfect semicircles — still one antialiased shape."""
        import math
        pts = [x0 + r, y0, x1 - r, y0]                      # top edge
        cx, cy = x1 - r, (y0 + y1) / 2                      # right cap
        for i in range(9):
            a = math.radians(-90 + 180 * i / 8)
            pts += [cx + r * math.cos(a), cy + r * math.sin(a)]
        pts += [x1 - r, y1, x0 + r, y1]                      # bottom edge
        cx = x0 + r                                          # left cap
        for i in range(9):
            a = math.radians(90 + 180 * i / 8)
            pts += [cx + r * math.cos(a), cy + r * math.sin(a)]
        return self.create_polygon(pts, smooth=True, **kw)

    def _circle(self, cx, cy, d, **kw):
        """Antialiased circle: 16-point polygon with smooth=True. With few
        points + smooth, Tk renders a clean conic — crisper than
        create_oval's non-antialiased rasterization at small sizes."""
        import math
        pts = []
        for i in range(16):
            a = 2 * math.pi * i / 16
            pts.append(cx + d / 2 * math.cos(a))
            pts.append(cy + d / 2 * math.sin(a))
        return self.create_polygon(pts, smooth=True, **kw)


class AnimatedProgressBar(tk.Canvas):
    """Segmented progress bar with a sliding glow. Real fraction fills green
    segments; while a task is active an indeterminate shimmer runs on top."""

    def __init__(self, parent, accent=COLORS["accent_green"], height=14, **kw):
        super().__init__(parent, height=height, highlightthickness=0, bd=0,
                         bg=COLORS["bg_widget"], **kw)
        self._accent = accent
        self._fraction = 0.0
        self._indeterminate = False
        self._shimmer = 0.0
        self._segs = 20
        self.bind("<Configure>", lambda e: self._draw())

    def set_fraction(self, fraction: float):
        self._fraction = max(0.0, min(1.0, fraction))
        self._draw()

    def set_indeterminate(self, on: bool):
        self._indeterminate = on
        if on:
            self._shimmer_loop()
        # audit fix (minor): turning it off left the last scheduled
        # _shimmer_loop after() running one more frame (and redrawing the
        # shimmer highlight on top of a finished bar). Cancel any in-flight
        # tick and redraw once from the real fraction.
        else:
            try:
                if getattr(self, "_shimmer_after", None) is not None:
                    self.after_cancel(self._shimmer_after)
                    self._shimmer_after = None
            except Exception:
                pass
            self._draw()

    def _shimmer_loop(self):
        if not self._indeterminate:
            return
        self._shimmer = (self._shimmer + 0.04) % (1.0 + 0.3)
        self._draw()
        self._shimmer_after = self.after(30, self._shimmer_loop)

    def _draw(self):
        self.delete("all")
        w = self.winfo_width()
        h = self.winfo_height()
        if w < 10:
            return
        gap = 3
        seg_w = (w - gap * (self._segs - 1)) / self._segs
        lit = self._fraction * self._segs
        for i in range(self._segs):
            x = i * (seg_w + gap)
            # partial segment alpha via color blend
            seg_t = max(0.0, min(1.0, lit - i))
            if self._indeterminate:
                dist = abs(((i / self._segs) + 0.5) - self._shimmer) % 1.0
                dist = min(dist, 1.0 - dist)
                seg_t = max(seg_t, max(0.0, 0.65 - dist * 2.2))
            color = _hex_lerp(COLORS["surface"], self._accent, seg_t)
            self._round_rect(x, 2, x + seg_w, h - 2, 3, fill=color, outline="")

    def _round_rect(self, x0, y0, x1, y1, r, **kw):
        points = [
            x0 + r, y0, x1 - r, y0, x1, y0, x1, y0 + r,
            x1, y1 - r, x1, y1, x1 - r, y1, x0 + r, y1,
            x0, y1, x0, y1 - r, x0, y0 + r, x0, y0,
        ]
        return self.create_polygon(points, smooth=True, **kw)


# --------------------------------------------------------------------------- #
# Admin gate
# --------------------------------------------------------------------------- #

class AdminGateFrame(tk.Frame):
    def __init__(self, root, on_continue_limited):
        super().__init__(root, bg=COLORS["bg"])
        self.root = root
        self.on_continue_limited = on_continue_limited
        self.pack(fill="both", expand=True)
        wrapper = tk.Frame(self, bg=COLORS["bg"])
        wrapper.place(relx=0.5, rely=0.5, anchor="center")
        tk.Label(wrapper, text="🛡️", font=("Segoe UI Emoji", 40), bg=COLORS["bg"],
                 fg=COLORS["accent_yellow"]).pack(pady=(0, 10))
        tk.Label(wrapper, text="Administrator Privileges Required", font=(F, 15, "bold"),
                 bg=COLORS["bg"], fg=COLORS["text"]).pack()
        tk.Label(
            wrapper,
            text=("Most cleaning works without admin, but repairs and tweaks need it.\n"
                  "Click below to restart as Administrator (UAC will appear)."),
            font=(F, 10), bg=COLORS["bg"], fg=COLORS["subtext"], justify="center",
        ).pack(pady=(8, 18))
        btn_row = tk.Frame(wrapper, bg=COLORS["bg"])
        btn_row.pack()
        AnimatedButton(
            btn_row, text="Restart as Administrator", command=self._restart_elevated,
            bg=COLORS["accent_green"], fg=COLORS["black"], font=(F, 10, "bold"),
        ).pack(side="left", padx=6)
        AnimatedButton(
            btn_row, text="Continue Without Admin", command=self._continue_limited,
            bg=COLORS["surface"], fg=COLORS["text"], font=(F, 10),
        ).pack(side="left", padx=6)

    def _restart_elevated(self):
        if relaunch_as_admin():
            self._set_elevate_buttons_state("disabled")
            self._wait_status = tk.Label(self, text="Waiting for elevation (UAC)...", font=(F, 9),
                                         bg=COLORS["bg"], fg=COLORS["accent_blue"])
            self._wait_status.pack(pady=(10, 0))

            def _wait_thread():
                from app.elevation import wait_for_elevated_process
                success = wait_for_elevated_process(timeout=15.0)

                def _on_done():
                    if success:
                        try:
                            self.root.destroy()
                        except Exception:
                            pass
                        sys.exit(0)
                    else:
                        try:
                            if hasattr(self, "_wait_status") and self._wait_status.winfo_exists():
                                self._wait_status.destroy()
                        except Exception:
                            pass
                        self._set_elevate_buttons_state("normal")
                        try:
                            messagebox.showwarning("Elevation Cancelled",
                                                   "Administrator elevation was cancelled or timed out. Continuing in limited mode.")
                        except Exception:
                            pass
                        self._continue_limited()

                try:
                    self.root.after(0, _on_done)
                except Exception:
                    pass

            threading.Thread(target=_wait_thread, daemon=True).start()
        else:
            messagebox.showerror("Elevation Failed",
                                 "Could not request administrator rights. You can still continue in limited mode.")

    def _set_elevate_buttons_state(self, state: str):
        try:
            for wrapper in self.winfo_children():
                for child in wrapper.winfo_children():
                    if isinstance(child, tk.Frame):
                        for btn in child.winfo_children():
                            if isinstance(btn, AnimatedButton):
                                btn.set_enabled(state == "normal")
        except Exception:
            pass

    def _continue_limited(self):
        self.destroy()
        self.on_continue_limited()


def _round_pts(x0, y0, x1, y1, r):
    return [
        x0 + r, y0, x1 - r, y0, x1, y0, x1, y0 + r,
        x1, y1 - r, x1, y1, x1 - r, y1, x0 + r, y1,
        x0, y1, x0, y1 - r, x0, y0 + r, x0, y0,
    ]


class ScrollableRoundedPanel(tk.Canvas):
    """Rounded-corner content panel with a custom-drawn scrollbar.

    Why custom instead of tk.Scrollbar (user feedback): native scrollbars
    can't be recolored on Windows — they'd stay light-gray next to the
    dark theme. This panel draws its own track + rounded thumb as canvas
    items in theme colors, floating over the content edge (appearing or
    hiding never reflows the grid).

    Other user-feedback fixes baked in:
      * the scrollbar only hides when ALL content fits — it can no longer
        vanish at 'scrolled to bottom' (old bug: auto-hide tested
        last >= 1.0, which is also true at full scroll-down)
      * cells are created ONCE by the caller and only re-gridded — resize
        never rebuilds widgets, so there's no laggy toggle redraw and no
        state loss
      * the panel never demands its content's height — a Canvas asks for
        almost nothing, so the Run button can never be pushed out of view
    """

    RADIUS = 16
    INSET_X = 12
    INSET_Y = 8
    SB_W = 16          # reserved right strip for the floating scrollbar

    def __init__(self, parent, **kw):
        page_bg = parent["bg"] if isinstance(parent, (tk.Frame, tk.Canvas)) else COLORS["bg"]
        super().__init__(parent, bg=page_bg, highlightthickness=0, bd=0, height=10, **kw)
        self._fill = COLORS["bg_alt"]
        self._offset = 0
        self._content_h = 0
        self._view_h = 0
        self._drag = None
        # F4 coalesced-scroll state: bursty input (wheel deltas, drag
        # motions) records intent here and ONE scheduled flush applies it
        # per ~16 ms turn instead of _apply-ing per event.
        self._scroll_flush_id = None
        self._pending_wheel = 0
        self._pending_drag_y = None
        self._drag_snapshot = None   # press-time geometry kept for the release flush
        self.resize_decide_cb = None     # set by TaskTab (column decision)
        self._resize_after = None
        # persistent scrollbar items (smooth-scroll fix): created once in
        # _ensure_sb_items, only moved/recolor per tick — declared here so
        # every method can safely getattr before first paint.
        self._sb_track = None
        self._sb_thumb = None
        self.inner = tk.Frame(self, bg=self._fill)
        self._win = self.create_window(0, 0, window=self.inner, anchor="nw")
        self.bind("<Configure>", self._on_configure)
        self.bind("<ButtonPress-1>", self._on_press)
        self.bind("<B1-Motion>", self._on_motion)
        self.bind("<ButtonRelease-1>", self._on_release)

    # ---- geometry ---------------------------------------------------- #

    def view_height(self):
        return max(1, self.winfo_height() - self.INSET_Y * 2)

    @property
    def scroll_enabled(self):
        return self._content_h > self._view_h

    def _on_configure(self, _e):
        self._redraw_bg()
        w = self.winfo_width()
        self.itemconfigure(self._win, width=max(10, w - self.INSET_X - self.SB_W - 2))
        self.coords(self._win, self.INSET_X, self.INSET_Y)
        self.refresh_scroll()
        # debounce the column re-decision on resize
        if self.resize_decide_cb is not None:
            if self._resize_after is not None:
                try:
                    self.after_cancel(self._resize_after)
                except Exception:
                    pass
            self._resize_after = self.after(180, self._fire_resize_cb)

    def _fire_resize_cb(self):
        self._resize_after = None
        if self.resize_decide_cb is not None and self.winfo_exists():
            self.resize_decide_cb()

    def _redraw_bg(self):
        self.delete("bg")
        w, h = self.winfo_width(), self.winfo_height()
        if w > 6 and h > 6:
            self.create_polygon(_round_pts(0, 0, w, h, self.RADIUS), smooth=True,
                                fill=self._fill, outline="", tags="bg")
            self.tag_lower("bg")

    # ---- scroll ------------------------------------------------------ #

    def refresh_scroll(self, flush=True):
        """Re-measure the content and settle the scrollbar.

        flush=False skips the inner.update_idletasks() geometry pass. That
        flush is what makes the full refresh expensive on a MAPPED canvas
        (it forces every pending idle redraw of the just-grown content —
        measured ~90-220 ms per call while catalog slices build into a
        visible page, 2026-09 bench). Requested sizes recompute on demand
        (winfo_reqheight walks the geometry-request chain without
        allocating or painting), so flush=False is the right per-slice
        tick while content streams in; flush=True stays for settle points
        (catalog finish, searches, collapses, _catalog_finish)."""
        if flush:
            self.inner.update_idletasks()
        self._content_h = self.inner.winfo_reqheight()
        self._view_h = self.view_height()
        if self._content_h <= self._view_h:
            self._offset = 0
        self._apply()

    def _max_offset(self):
        return max(0, self._content_h - self._view_h)

    def _apply(self):
        max_off = self._max_offset()
        self._offset = max(0, min(self._offset, max_off))
        self.coords(self._win, self.INSET_X, self.INSET_Y - self._offset)
        # Scrollbar performance fix: items are created ONCE and only
        # moved/recolor via coords/itemconfigure. The old version did
        # delete("sb") + full recreate on EVERY scroll tick — each
        # recreate is a fresh smooth-polygon (conic spline) rasterization,
        # and the delete/recreate gap leaves a blank frame between them.
        # On the Install tab (the heaviest page) that blank-frame churn
        # per wheel event was the visible stutter/"can't draw fast
        # enough" feel. Persistent items let Tk repaint only the small
        # damaged region (same technique the tab-switcher thumb uses).
        self._ensure_sb_items()
        if not self.scroll_enabled:
            self._hide_sb()
            return
        w, h = self.winfo_width(), self.winfo_height()
        y0, y1 = self.INSET_Y, h - self.INSET_Y
        cx = w - 10
        frac = (self._view_h / self._content_h) if self._content_h else 1.0
        thumb_h = max(30, int(frac * (y1 - y0)))
        pos = (self._offset / max_off) if max_off else 0.0
        ty = y0 + pos * ((y1 - y0) - thumb_h)
        track_c = _hex_lerp(self._fill, "#FFFFFF", 0.06)
        self.itemconfigure(self._sb_track, fill=track_c)
        self.coords(self._sb_track, cx, y0, cx, y1)
        self.itemconfigure(self._sb_thumb, state="normal")
        self.coords(self._sb_thumb, *_round_pts(cx - 4, ty, cx + 4, ty + thumb_h, 4))

    def _ensure_sb_items(self):
        """Create the persistent scrollbar items on first use (or recreate
        if the canvas was cleared). find_withtag is the liveness check —
        an item id of a deleted item is falsy-safe here, but an int id
        that was valid at creation can be stale after a canvas-level
        delete, so verify it still resolves."""
        track_ok = False
        thumb_ok = False
        try:
            if self._sb_track is not None:
                track_ok = bool(self.find_withtag(self._sb_track))
            if self._sb_thumb is not None:
                thumb_ok = bool(self.find_withtag(self._sb_thumb))
        except Exception:
            pass
        if not track_ok:
            self._sb_track = self.create_line(0, 0, 0, 0, fill=self._fill, width=4,
                                              capstyle="round", tags="sb")
        if not thumb_ok:
            self._sb_thumb = self.create_polygon(_round_pts(0, 0, 0, 0, 4), smooth=True,
                                                  fill=COLORS["surface_hover"],
                                                  outline="", state="hidden", tags="sb")
        # keep them above the content window & bg at all times
        self.tag_raise(self._sb_track)
        self.tag_raise(self._sb_thumb)

    def _hide_sb(self):
        try:
            self.itemconfigure(self._sb_thumb, state="hidden")
            self.itemconfigure(self._sb_track, state="hidden")
        except Exception:
            pass

    def on_wheel(self, delta):
        if not self.scroll_enabled:
            return
        # Smooth-scroll fix: the old int(delta/120)*60 truncation turned
        # smooth/free-spin wheels and precision touchpads (small ±delta
        # events) into zero-pixel scrolls followed by 60px jumps — the
        # classic "can't keep up" feel. Scale proportionally instead: a
        # classic ±120 notch moves a sane 60px; a high-resolution wheel's
        # small deltas move small precise steps that sum smoothly.
        # (max(1,…) keeps even a tiny ±1 delta scrolling one pixel.)
        step = max(1, int(abs(delta) * 0.5))
        # F4: a wheel burst arriving inside one ~16 ms turn accumulates
        # here and is applied by a single flush — same total distance per
        # notch (60px classic, proportional for smooth wheels), but one
        # _apply per turn instead of one per event (unbounded rate vs
        # paint).
        self._pending_wheel += step if delta > 0 else -step
        self._schedule_scroll_flush()

    def _schedule_scroll_flush(self):
        """F4: coalesce bursty scroll input into at most one _apply per
        ~16 ms event-loop turn. after(16) (not after_idle) is used because
        Tk runs idle callbacks between every pair of queued events — under
        a motion flood after_idle would flush once PER event and coalesce
        nothing. The pending flag guarantees a single scheduled flush no
        matter how many events arrive before it fires."""
        if self._scroll_flush_id is None:
            try:
                self._scroll_flush_id = self.after(16, self._flush_scroll)
            except Exception:
                pass  # panel destroyed mid-event — nothing left to flush

    def _flush_scroll(self):
        """Apply whatever scroll intent accumulated since the last flush
        (F4). Drag semantics are preserved: the stored drag position is
        absolute (drag overrides), wheel deltas add to it (wheel adds),
        and _apply clamps to the content range. Also called from
        ButtonRelease so the final drag position lands immediately rather
        than waiting out the coalescing timer."""
        self._scroll_flush_id = None
        changed = False
        try:
            if self._pending_drag_y is not None:
                y = self._pending_drag_y
                self._pending_drag_y = None
                # during a live drag use its press-time tuple; after
                # ButtonRelease fall back to the snapshot taken there
                drag = self._drag if self._drag is not None else self._drag_snapshot
                if drag is not None:
                    self._drag_snapshot = None
                    self._offset = self._thumb_offset_for(drag, y)
                    changed = True
            if self._pending_wheel:
                self._offset -= self._pending_wheel
                self._pending_wheel = 0
                changed = True
            if changed:
                self._apply()
        except Exception:
            pass  # widget destroyed mid-flush (shutdown) — nothing to paint

    # ---- thumb drag / track click ------------------------------------ #

    def _sb_zone_x(self):
        return self.winfo_width() - self.SB_W - 8

    def _thumb_geom(self):
        h = self.winfo_height()
        y0, y1 = self.INSET_Y, h - self.INSET_Y
        frac = (self._view_h / self._content_h) if self._content_h else 1.0
        thumb_h = max(30, int(frac * (y1 - y0)))
        max_off = self._max_offset()
        pos = (self._offset / max_off) if max_off else 0.0
        ty = y0 + pos * ((y1 - y0) - thumb_h)
        return ty, thumb_h, y0, y1, max_off

    def _thumb_offset_for(self, drag, y):
        """Scroll offset that puts the thumb under pointer y, given a
        press-time drag tuple (grab_dy, thumb_h, y0, y1, max_off). Shared
        by the track-click jump and the coalesced drag flush so both paths
        use identical math."""
        grab_dy, thumb_h, y0, y1, max_off = drag
        ty = max(y0, min(y1 - thumb_h, y - grab_dy))
        span = (y1 - y0) - thumb_h
        return 0 if span <= 0 else ((ty - y0) / span) * max_off

    def _on_press(self, e):
        if not self.scroll_enabled or e.x < self._sb_zone_x():
            return
        self._drag_snapshot = None   # fresh drag: forget any old release snapshot
        ty, thumb_h, y0, y1, max_off = self._thumb_geom()
        if ty - 6 <= e.y <= ty + thumb_h + 6:
            self._drag = (e.y - ty, thumb_h, y0, y1, max_off)
        else:
            # track click: jump so the thumb centers under the pointer
            self._drag = (thumb_h / 2, thumb_h, y0, y1, max_off)
            self._set_thumb_centered(e.y)
        self.config(cursor="hand2")

    def _set_thumb_centered(self, y):
        self._offset = self._thumb_offset_for(self._drag, y)
        self._apply()

    def _on_motion(self, e):
        # F4: record the latest drag position and let the coalescing flush
        # apply it — a 1000 Hz mouse report flood no longer forces one
        # _apply per report (unbounded apply rate vs the display's paint).
        if self._drag is not None:
            self._pending_drag_y = e.y
            self._schedule_scroll_flush()

    def _on_release(self, _e):
        if self._drag is not None and self._pending_drag_y is not None:
            # keep the press-time geometry for one final flush so the last
            # drag position still lands after the button comes up
            self._drag_snapshot = self._drag
        self._drag = None
        self.config(cursor="")
        # F4: land any still-pending position/delta immediately — the
        # mouse is up, don't wait out the coalescing timer
        if self._pending_drag_y is not None or self._pending_wheel:
            self._flush_scroll()


# --------------------------------------------------------------------------- #
# Per-tab page
# --------------------------------------------------------------------------- #

class TaskTab(tk.Frame):
    """One of the three tab pages.

    Modes (Phase 2 #13):
      * preset mode — curated preset cards + Custom card; selecting a
        preset shows a plain-language summary; no checkboxes at all.
      * custom mode — full animated toggle grid (all tasks for the tab).
      * undo mode (Tweak only) — all-off grid of reversible tweaks; toggles
        choose what to revert using real snapshotted prior values.
    """

    PRESET_BLURBS = {
        "Quick Clean": "Everyday junk — fast and safe.",
        "Deep Clean": "Deeper junk, browsers, old files.",
        "Quick Repair": "Fast checks for common problems.",
        "Deep Repair": "Full fix stack — 30+ minutes.",
        "Minimal": "Safe speed basics, no risk.",
        "Recommended": "Best all-round setup + privacy.",
        "Game Session": "Max power for one play session.",
    }

    def __init__(self, parent, app, tab_name):
        super().__init__(parent, bg=COLORS["bg"])
        self.app = app
        self.tab_name = tab_name
        self.tasks = TABS[tab_name]
        self.task_by_key = {t.key: t for t in self.tasks}
        self.presets = PRESETS[tab_name]
        self.mode = "preset"          # preset | custom | undo
        self.vars = {}
        self._selected_preset = None
        self._anim_jobs = []          # active after() ids owned by this page
        # grid engine state: cells built once, only re-gridded afterwards
        self._cells = None            # list[(widget, task)] in stable order
        self._cell_cols = None
        self._regrid_after = None
        # body cache: built-once bodies per mode/preset (no laggy redraws)
        self._body_cache = {}
        self._body_vars = {}
        # F3: undo rows of the last-built undo grid, for in-place badge
        # repaints (each row stores its badge handle on the widget)
        self._undo_rows = []
        # F7: _set_all() sets this while it bulk-writes every row var, so
        # the per-var traces skip the run-count refresh (one refresh after
        # the loop is enough); single toggles are never suppressed.
        self._suspend_count_refresh = False

        self._build()

    # ---------------- structure ---------------- #

    def _build(self):
        # Preset card area (top) — compact padding reclaims vertical space
        # so Clean/Repair Custom grids fit WITHOUT scrolling at default size
        self.preset_area = tk.Frame(self, bg=COLORS["bg"])
        self.preset_area.pack(fill="x", padx=26, pady=(14, 4))

        # Summary / toggle-grid area (middle, swaps by mode)
        self.body_area = tk.Frame(self, bg=COLORS["bg"])

        # Run row (bottom) — packed FIRST (side=bottom) so it can never be
        # squeezed out of view (user-reported clipping: a tall content
        # frame used to push the Run button off-screen).
        self.run_row = tk.Frame(self, bg=COLORS["bg"])
        self.run_row.pack(side="bottom", fill="x", padx=26, pady=(4, 10))
        self.body_area.pack(side="top", fill="both", expand=True, padx=26, pady=4)

        self._build_preset_cards()
        self._build_body()
        self._build_run_row()
        # F2: preset cards and the Run button are built HERE once and only
        # recolored/restyled on later mode switches (_highlight_cards /
        # _update_run_row) — never destroyed and recreated per switch.

    def _build_preset_cards(self):
        for w in self.preset_area.winfo_children():
            w.destroy()
        accent = TAB_ACCENTS[self.tab_name]

        # (Phase 2 #15) Tweak tab gets the red Undo card on the end
        cards = list(self.presets.keys()) + ["Custom"] + (["Undo Tweaks"] if self.tab_name == "Tweak" else [])
        self.preset_cards = {}
        # WRAP AT 3 PER ROW (user-reported clipping: a 5th card squeezed the
        # whole row). 3 across keeps every card readable at min window size;
        # extra cards flow to a second row instead of shrinking the rest.
        # Clean/Repair have 3 cards (one row, unchanged); Tweak has 5 (3+2).
        PER_ROW = 3
        for col, name in enumerate(cards):
            if name == "Custom":
                blurb, icon, color, fg = "Pick exactly what runs.", "⚙", COLORS["surface"], COLORS["text"]
                command = lambda: self._enter_custom()
            elif name == "Undo Tweaks":
                blurb, icon, color, fg = "Put back what the tweaks changed.", "↩", COLORS["surface"], COLORS["text"]
                command = lambda: self._enter_undo()
            else:
                blurb = self.PRESET_BLURBS.get(name, "")
                icons = {"Quick Clean": "🧹", "Deep Clean": "🧼", "Quick Repair": "🩺",
                         "Deep Repair": "🔧", "Minimal": "🚀", "Recommended": "⭐",
                         "Game Session": "🎮"}
                icon = icons.get(name, "•")
                color, fg = COLORS["surface"], COLORS["text"]
                command = (lambda n=name: self._select_preset(n))
            card = self._make_card(self.preset_area, name, blurb, icon, color, fg, command, accent)
            row, column = divmod(col, PER_ROW)
            card.grid(row=row, column=column, sticky="nsew", padx=5, pady=4)
            self.preset_cards[name] = card
        for c in range(PER_ROW):
            self.preset_area.grid_columnconfigure(c, weight=1, uniform="cards")

    def _make_card(self, parent, title, blurb, icon, bg, fg, command, accent):
        outer = tk.Frame(parent, bg=COLORS["hairline"], bd=0)
        inner = tk.Frame(outer, bg=bg)
        inner.pack(fill="both", expand=True, padx=1, pady=1)
        head = tk.Frame(inner, bg=bg)
        # Title-only cards (approved layout change): the one-line blurb is no
        # longer drawn on the face — shorter rows leave the Custom/Undo
        # panels below more room. The blurb text still rides the tooltip.
        head.pack(fill="x", padx=12, pady=(6, 6))
        icon_lbl = tk.Label(head, text=icon, font=("Segoe UI Emoji", 16), bg=bg, fg=accent)
        icon_lbl.pack(side="left")
        title_lbl = tk.Label(head, text=title, font=(F, 12, "bold"), bg=bg, fg=fg)
        title_lbl.pack(side="left", padx=8)
        if blurb:
            # one Tooltip per card part, same text: hovering anywhere on the
            # card shows the description (child crossings fire <Leave>, so a
            # single container-bound tip would die at the first label edge)
            for _w in (outer, inner, head, icon_lbl, title_lbl):
                Tooltip(_w, blurb)
        # click + hover (add="+": card parts may carry Tooltip <Enter>/<Leave>
        # hooks by now — plain bind() would wipe them, see _build_toggle_row)
        for w in (outer, inner, head):
            w.bind("<Button-1>", lambda e, c=command: c())
            w.bind("<Enter>", lambda e, i=inner, o=outer, a=accent: self._card_hover(i, o, a, True), add="+")
            w.bind("<Leave>", lambda e, i=inner, o=outer, a=accent: self._card_hover(i, o, a, False), add="+")
        for child in inner.winfo_children():
            for c in child.winfo_children() or [child]:
                try:
                    c.bind("<Button-1>", lambda e, c2=command: c2())
                    c.bind("<Enter>", lambda e, i=inner, o=outer, a=accent: self._card_hover(i, o, a, True), add="+")
                    c.bind("<Leave>", lambda e, i=inner, o=outer, a=accent: self._card_hover(i, o, a, False), add="+")
                except Exception:
                    pass
        return outer

    def _card_hover(self, inner, outer, accent, on):
        # subtle: hairline border brightens toward accent on hover
        outer.config(bg=accent if on else COLORS["hairline"])

    def _build_body(self):
        """Body cache (user feedback: mode switches redrew ~50 toggle
        widgets and looked laggy). Bodies are built once per mode and
        cached; switching modes just shows/hides — instant, and Custom
        toggle choices survive a trip to a preset and back.

        self.vars must follow the SHOWN body: each cached body remembers
        its own var dict (custom and undo bodies have different var sets),
        and showing a cached body restores its dict — otherwise Run would
        read the previous mode's vars (bug caught by the layout test)."""
        # hide all cached bodies
        for w in self.body_area.winfo_children():
            w.pack_forget()
        cache_key = self.mode if self.mode != "preset" else f"preset:{self._selected_preset}"
        if cache_key in self._body_cache:
            self._body_cache[cache_key].pack(fill="both", expand=True)
            if cache_key in self._body_vars:
                self.vars = self._body_vars[cache_key]
            return
        wrap = tk.Frame(self.body_area, bg=COLORS["bg"])
        self._body_cache[cache_key] = wrap
        wrap.pack(fill="both", expand=True)
        if self.mode == "preset":
            self._build_summary_body(wrap)
        elif self.mode == "custom":
            self._build_toggle_grid(wrap, mode="run")
        else:
            self._build_toggle_grid(wrap, mode="undo")
        # builders refresh self.vars — remember it as this body's dict
        if self.mode != "preset":
            self._body_vars[cache_key] = self.vars

    def _clear_body_cache(self):
        """Drop cached bodies (called when tab's tasks/state change — undo
        badges depend on live tweak state)."""
        self._body_cache = {}
        self._body_vars = {}

    def _prewarm_body(self, mode: str) -> bool:
        """F6(b): build one body (custom/undo) into the cache WITHOUT
        showing it, so the first real click on Custom/Undo is a cache hit
        (~2 ms) instead of a 100-450 ms first build. Runs once from
        Application's idle pre-warm chain after the window is up; a no-op
        when that body already exists (the user got there first and the
        normal lazy build owns it).

        The grid builder mutates page state as it goes (self.vars,
        _cells, _cell_blocks), so that state is snapshotted and restored —
        only the cache entry (hidden widgets + their var dict) is kept.
        _undo_rows is deliberately NOT restored: an undo pre-warm only
        runs while no undo body exists, so its rows are exactly the rows
        the next _enter_undo shows and badge-refreshes in place (F3)."""
        if mode not in ("custom", "undo"):
            return False
        if mode in self._body_cache:
            return False
        saved = (self.vars,
                 getattr(self, "_cells", None),
                 getattr(self, "_cell_blocks", None))
        try:
            wrap = tk.Frame(self.body_area, bg=COLORS["bg"])
            self._body_cache[mode] = wrap
            self._build_toggle_grid(wrap, mode="run" if mode == "custom" else "undo")
            self._body_vars[mode] = self.vars
            # the hidden wrap measures ~1px wide, so labels were wrapped to
            # the narrow fallback — the grid's own debounced <Configure>
            # refit (_fit_toggle_labels) re-wraps them to the real column
            # width the moment this body is first shown.
            wrap.pack_forget()  # never packed — stays hidden until shown
            self.vars, self._cells, self._cell_blocks = saved
            return True
        except Exception:
            # a failed pre-warm must never crash the idle chain — drop the
            # half-built cache entry; the lazy first-click build takes over
            self._body_cache.pop(mode, None)
            self._body_vars.pop(mode, None)
            self.vars, self._cells, self._cell_blocks = saved
            return False

    def _build_summary_body(self, wrap):
        """Read-only plain-language summary of the selected preset (no
        checkboxes — punch-list #13 'no checkboxes for curated tiers')."""
        if self._selected_preset is None:
            tk.Label(wrap, text=f"Pick a {self.tab_name} preset above to see what it does.",
                     font=(F, 11), bg=COLORS["bg"], fg=COLORS["subtext"]).pack(expand=True)
            return
        keys = self.presets[self._selected_preset]
        head = tk.Frame(wrap, bg=COLORS["bg"])
        head.pack(fill="x", pady=(2, 8))
        accent = TAB_ACCENTS[self.tab_name]
        tk.Label(head, text=f"{self._selected_preset} — {len(keys)} tasks",
                 font=(F, 13, "bold"), bg=COLORS["bg"], fg=accent).pack(anchor="w")
        tk.Label(head, text=self.PRESET_BLURBS.get(self._selected_preset, ""),
                 font=(F, 10), bg=COLORS["bg"], fg=COLORS["subtext"]).pack(anchor="w")

        panel = ScrollableRoundedPanel(wrap)
        panel.pack(fill="both", expand=True)

        tasks = [self.task_by_key[k] for k in keys]
        cells = []
        for t in tasks:
            cell = tk.Frame(panel.inner, bg=COLORS["bg_alt"])
            dot = tk.Canvas(cell, width=8, height=8, bg=COLORS["bg_alt"], highlightthickness=0)
            dot.create_oval(1, 1, 7, 7, fill=TAB_ACCENTS[self.tab_name], outline="")
            dot.pack(side="left", padx=(0, 6), anchor="n")
            lbl = tk.Label(cell, text=t.label, font=(F, 9), bg=COLORS["bg_alt"],
                           fg=COLORS["text"], anchor="w", justify="left", wraplength=215)
            lbl.pack(side="left", anchor="w")
            Tooltip(lbl, t.description)
            if getattr(t, "risk", "SAFE") == "REBOOT REQUIRED":
                r = tk.Label(cell, text=" 🔄", font=("Segoe UI Emoji", 9), bg=COLORS["bg_alt"],
                             fg=COLORS["text"])
                r.pack(side="left")
                Tooltip(r, "Reboot Required — Windows needs a restart after this one")
            cells.append((cell, t))
        self._cells = cells
        self._cell_blocks = [(None, cells)]   # summary has no group headers
        panel.resize_decide_cb = lambda: self._regrid(panel)
        self._regrid(panel, force=True)

    def _regrid(self, panel, force=False):
        """Re-grid existing cells (built once) into the best column count
        for the current panel size. No widget rebuilds -> no toggle redraw
        flicker, no state loss. Column choice tries to make content fit
        WITHOUT scrolling: it walks column counts from 3 up and picks the
        first whose resulting content height fits the view; only if none
        fit does scrolling engage.

        Layout unit is self._cell_blocks: [(header_or_None, [(cell, task)])].
        Headers span the full width; each block's cells flow below its
        header. (The preset summary sets a single headerless block.)"""
        blocks = getattr(self, "_cell_blocks", None)
        if not blocks:
            return
        # list-mode rows (Custom/Undo grids) are packed full-width, not
        # gridded — there is nothing to re-fit on resize.
        try:
            _probe = blocks[0][0] or (blocks[0][1][0][0] if blocks[0][1] else None)
            if _probe is not None and _probe.winfo_manager() == "pack":
                return
        except Exception:
            pass
        cells = [c for _h, bc in blocks for c in bc]
        if not cells:
            return
        panel.update_idletasks()
        inner_w = max(10, panel.winfo_width() - panel.INSET_X - panel.SB_W - 2)
        view_h = panel.view_height()
        # per-cell height measured from the first cell (stable once mapped)
        try:
            cell_h = max(20, cells[0][0].winfo_reqheight() + 8)  # +pady
        except Exception:
            cell_h = 44
        n_headers = sum(1 for h, _bc in blocks if h is not None)

        best = None
        for cols in (3, 4, 5):
            col_w = inner_w // cols
            if col_w < 170:
                continue  # cells would be unreadably narrow
            data_rows = sum((len(bc) + cols - 1) // cols for _h, bc in blocks)
            content_h = data_rows * cell_h + n_headers * 30 + panel.INSET_Y * 2
            if content_h <= view_h:
                best = cols
                break
        if best is None:
            # nothing fits height-wise: use the widest count that keeps
            # cells >= 170px so scrolling distance is minimized
            best = max(3, min(5, inner_w // 170))
            if best < 3:
                best = 3
        cols = best
        r = 0
        for hdr, bcells in blocks:
            if hdr is not None:
                hdr.grid(row=r, column=0, columnspan=cols, sticky="ew",
                         padx=6, pady=(8, 2))
                r += 1
            for i, (cell, _t) in enumerate(bcells):
                rr, cc = divmod(i, cols)
                cell.grid(row=r + rr, column=cc, sticky="nw", padx=6, pady=2)
            r += (len(bcells) + cols - 1) // cols
        for c in range(cols):
            panel.inner.grid_columnconfigure(c, weight=1, uniform="grid")
        for c in range(cols, 6):
            panel.inner.grid_columnconfigure(c, weight=0)
        self._cell_cols = cols
        panel.refresh_scroll()

    def _build_toggle_grid(self, wrap, mode="run"):
        """The only place toggles appear (Phase 2 #13/#14). Custom mode =
        pick what runs; Undo mode = pick what reverts (all default off)."""
        accent = TAB_ACCENTS[self.tab_name]

        if mode == "undo":
            banner = tk.Frame(wrap, bg="#3A2226")
            tk.Label(banner, text="↩  Undo — switches pick which tweaks to put back to how they were.",
                     font=(F, 10, "bold"), bg="#3A2226", fg=COLORS["accent_red"]).pack(anchor="w", padx=10, pady=8)
            banner.pack(fill="x", pady=(0, 6))

        topbar = tk.Frame(wrap, bg=COLORS["bg"])
        topbar.pack(fill="x", pady=(0, 6))
        AnimatedButton(topbar, "All On", command=lambda: self._set_all(True),
                       bg=COLORS["surface"], fg=COLORS["text"], font=(F, 9), padx=16, pady=6).pack(side="left", padx=3)
        AnimatedButton(topbar, "All Off", command=lambda: self._set_all(False),
                       bg=COLORS["surface"], fg=COLORS["text"], font=(F, 9), padx=16, pady=6).pack(side="left", padx=3)
        if mode == "run":
            hint = "Turn on what you want to run — click a row anywhere"
        else:
            hint = "Turn on what you want to undo"
        tk.Label(topbar, text=hint, font=(F, 9), bg=COLORS["bg"],
                 fg=COLORS["subtext"]).pack(side="left", padx=10)
        # search filter (user request: find one option among dozens fast) —
        # rounded corners, centered in the leftover space (user request)
        _ph = f"Filter {len(self.tasks)} options…"
        _sv = tk.StringVar(value=_ph)
        _sv.trace_add("write", lambda *_: self._apply_toggle_filter(
            _sv.get().strip(), _ph, mode))
        _mid = tk.Frame(topbar, bg=COLORS["bg"])
        _mid.pack(side="left", expand=True, fill="x")
        _re = RoundedEntry(_mid, textvariable=_sv, width=20, accent=accent)
        _re.pack(anchor="center")
        _se = _re.entry
        _se.bind("<FocusIn>", lambda e: _sv.set("") if _sv.get() == _ph else None)
        # audit fix (UI polish): InstallTab's search restores its placeholder
        # on blur; this one never did — after clicking away, the box stayed
        # permanently empty with no hint of what it was for.
        _se.bind("<FocusOut>", lambda e: _sv.set(_ph) if not _sv.get().strip() else None)

        state = self.app.tweak_state if mode == "undo" else {}

        panel = ScrollableRoundedPanel(wrap)
        panel.pack(fill="both", expand=True)
        self._grid_row = 0   # shared 2-column grid row cursor (grid mode)

        tasks = [t for t in self.tasks if (mode == "run" or t.revert is not None)]
        self.vars = {}
        # Custom grids are grouped by function (tab_presets.CUSTOM_GROUPS).
        # (User request, 2-column layout): each option is a compact single-
        # line settings row in a TWO-COLUMN grid — Install-tab density, so
        # tabs with dozens of options scroll roughly half as far. Details
        # live in the hover tooltip, not inline. The whole row stays
        # clickable — big targets, nothing truncated.
        from app.tab_presets import CUSTOM_GROUPS
        by_key = {t.key: t for t in tasks}
        grouped = CUSTOM_GROUPS.get(self.tab_name, [])
        blocks = []   # (header_or_None, [(row_outer, task), ...])
        cells = []
        # 2-column grid inside the panel; headers span both columns
        inner_grid = panel.inner
        # measure once: with 2 equal columns, each gets ~half the inner
        # width (minus scrollbar strip). Labels wrap to that, not a fixed
        # 560px that would clip in a column.
        inner_w = max(10, panel.winfo_width() - panel.INSET_X - panel.SB_W - 2)
        col_w = max(160, (inner_w - 24) // 2)   # 24 = grid padx total
        for _title, _keys in grouped:
            gtasks = [by_key[k] for k in _keys if k in by_key]
            if not gtasks:
                continue
            hdr = tk.Frame(inner_grid, bg=COLORS["surface"])
            tk.Label(hdr, text=_title, font=(F, 10, "bold"), bg=COLORS["surface"],
                     fg=COLORS["text"]).pack(side="left", padx=(8, 0), pady=4)
            tk.Label(hdr, text=f"{len(gtasks)} options", font=(F, 8),
                     bg=COLORS["surface"], fg=COLORS["subtext"]).pack(side="left", padx=6)
            hdr.grid(row=self._grid_row, column=0, columnspan=2,
                     sticky="ew", padx=6, pady=(10, 2))
            self._grid_row += 1
            grows = []
            for i, t in enumerate(gtasks):
                row, col = divmod(i, 2)
                cell, _task = self._build_toggle_row(inner_grid, t, mode, state, accent, col_w)
                cell.grid(row=self._grid_row + row, column=col,
                          sticky="new", padx=6, pady=2)
                grows.append((cell, t))
            self._grid_row += (len(gtasks) + 1) // 2
            blocks.append((hdr, grows))
            cells.extend([(c, t) for c, t in grows])
        # safety net (should be unreachable — tab_presets validates full
        # coverage): any task missing from the groups still shows up.
        _shown = {t.key for _h, grows in blocks for _c, t in grows}
        _missing = [t for t in tasks if t.key not in _shown]
        if _missing:
            grows = []
            for i, t in enumerate(_missing):
                row, col = divmod(i, 2)
                cell, _task = self._build_toggle_row(inner_grid, t, mode, state, accent, col_w)
                cell.grid(row=self._grid_row + row, column=col,
                          sticky="new", padx=6, pady=2)
                grows.append((cell, t))
            self._grid_row += (len(_missing) + 1) // 2
            blocks.append((None, grows))
            cells.extend([(c, t) for c, t in grows])
        self._cells = cells
        self._cell_blocks = blocks
        # F3: remember the undo grid's rows so later Undo entries can
        # repaint '✓ Active' badges in place without rebuilding the grid
        if mode == "undo":
            self._undo_rows = list(cells)
        for c in (0, 1):
            inner_grid.grid_columnconfigure(c, weight=1, uniform="togglerows")
        # one-line guarantee (user request): col_w at build time is measured
        # before the window is laid out (often 1px), which froze every label
        # at a ~140px wrap and forced 2-line names. Refit label wraps to the
        # REAL column width on every grid resize (debounced) so names stay on
        # one row at any window size/DPI.
        _fit_after = {"id": None}

        def _fit_toggle_labels(*_):
            try:
                if _fit_after["id"] is not None:
                    inner_grid.after_cancel(_fit_after["id"])
            except Exception:
                pass

            def _do():
                _fit_after["id"] = None
                try:
                    w = max(10, panel.winfo_width() - panel.INSET_X - panel.SB_W - 2)
                    cw = max(160, (w - 24) // 2)
                    wrap = max(120, cw - 110)
                    for _h, _grows in blocks:
                        for _cell, _t in _grows:
                            _lbl = getattr(_cell, "_lbl", None)
                            if _lbl is not None and _lbl.winfo_exists():
                                _lbl.config(wraplength=wrap)
                except Exception:
                    pass
                # F6(b): bodies pre-warmed hidden measure ~1px wide, so rows
                # may have wrapped taller than they will at the real column
                # width — after the refit above, settle the scroll metrics
                # on the now-true content height.
                try:
                    panel.refresh_scroll()
                except Exception:
                    pass

            try:
                _fit_after["id"] = inner_grid.after(120, _do)
            except Exception:
                pass

        inner_grid.bind("<Configure>", _fit_toggle_labels)
        _fit_toggle_labels()
        panel.resize_decide_cb = None
        panel.refresh_scroll()

    def _build_toggle_row(self, parent, t, mode, state, accent, col_w=380):
        """One compact settings-row cell (2-column grid, Install-tab density):
        borderless box (user request: no outline frame), clickable anywhere (not just the switch), switch +
        single-line label. The full description lives in the hover tooltip
        (user request: inline 3-line descriptions doubled the scrolling).
        Hover never draws an outline or paints the row background (user
        request — a rectangular wash looked square around the pill toggle).
        The highlight lives only on the switch (its own hover brightening)
        and the task name (tinted toward the tab color). Registers its var;
        returns (outer, task)."""
        var = tk.BooleanVar(value=(t.default if mode == "run" else False))
        self.vars[t.key] = var
        try:
            var.trace_add("write", lambda *_: self._refresh_run_count())
        except Exception:
            pass
        outer = tk.Frame(parent, bg=COLORS["bg_alt"], cursor="hand2")
        inner = tk.Frame(outer, bg=COLORS["bg_alt"], cursor="hand2")
        inner.pack(fill="both", expand=True)
        sw = ToggleSwitch(inner, var)
        sw.pack(side="left", anchor="center", padx=(8, 2), pady=6)
        txt = tk.Frame(inner, bg=COLORS["bg_alt"], cursor="hand2")
        txt.pack(side="left", anchor="center", fill="both", expand=True,
                 padx=(0, 8), pady=6)
        # single line: wraplength only guards ultra-long labels — most fit
        # one line at column width (switch 44 + its padx + row padx accounted)
        wrap = max(140, col_w - 44 - 10 - 16 - 12)
        lbl = tk.Label(txt, text=t.label, font=(F, 9, "bold"), bg=COLORS["bg_alt"],
                       fg=COLORS["text"], anchor="w", justify="left",
                       wraplength=wrap, cursor="hand2")
        lbl.pack(side="left", anchor="center")
        Tooltip(lbl, t.description)
        outer._lbl = lbl  # dynamic one-line fit (see _fit_toggle_labels)
        extra = []
        if mode == "run" and getattr(t, "admin_required", False):
            adm = tk.Label(txt, text="🛡️", font=("Segoe UI Emoji", 9), bg=COLORS["bg_alt"],
                           fg=COLORS["text"], cursor="hand2")
            adm.pack(side="left", anchor="center", padx=(6, 0))
            Tooltip(adm, "Admin Required — needs Administrator rights (skipped in limited mode)")
            extra.append(adm)
        if getattr(t, "risk", "SAFE") == "REBOOT REQUIRED":
            r = tk.Label(txt, text="🔄", font=("Segoe UI Emoji", 9), bg=COLORS["bg_alt"],
                         fg=COLORS["text"], cursor="hand2")
            r.pack(side="left", anchor="center", padx=(6, 0))
            Tooltip(r, "Reboot Required — Windows needs a restart after this one")
            extra.append(r)
        # F3: undo rows get a '✓ Active' badge label at build time, PACKED
        # only while the tweak is actually applied (state from the last
        # _build_toggle_grid call). The handle lives on the row so
        # _refresh_undo_badges can pack/unpack in place on later Undo
        # entries — the grid is never rebuilt just to refresh badges.
        if mode == "undo":
            b = tk.Label(txt, text="✓ Active", font=(F, 7, "bold"),
                         bg=COLORS["bg_alt"], fg=COLORS["accent_green"], cursor="hand2")
            if t.key in state:
                b.pack(side="left", anchor="center", padx=(6, 0))
            extra.append(b)
            outer._undo_badge = b

        # subtle hover (user request: NO outline, NO background wash — both
        # read as a square box around the pill toggle). Only the switch
        # brightens and the task name tints toward the tab color.
        _hi_fg = _hex_lerp(COLORS["text"], accent, 0.65)

        def _hover(on, ev=None):
            # Leave fires on every child crossing too — ignore those while
            # the pointer is still anywhere inside this row.
            if not on and ev is not None:
                try:
                    w = outer.winfo_containing(ev.x_root, ev.y_root)
                    p = w
                    while p is not None:
                        if p == outer:
                            return
                        p = p.master
                except Exception:
                    pass
            try:
                lbl.config(fg=_hi_fg if on else COLORS["text"])
                sw._draw(hover=True) if on else sw._draw()
            except Exception:
                pass

        def _click(ev):
            if ev.widget is sw:
                return  # the switch handles its own clicks
            var.set(not var.get())

        # add="+": the labels carry Tooltips (plain bind() would wipe them —
        # same ordering bug that once killed the Install name hover)
        for w in (outer, inner, txt, lbl, *extra):
            w.bind("<Enter>", lambda e: _hover(True, e), add="+")
            w.bind("<Leave>", lambda e: _hover(False, e), add="+")
            w.bind("<Button-1>", _click)
        return (outer, t)

    def _build_run_row(self):
        for w in self.run_row.winfo_children():
            w.destroy()
        accent = TAB_ACCENTS[self.tab_name]
        if self.mode == "undo":
            label, bg, fg = "Undo Selected", COLORS["accent_red"], "#FFFFFF"
        else:
            label, bg, fg = "Run", accent, COLORS["black"]
        self.run_btn = AnimatedButton(
            self.run_row, text=label, command=self._run_selected,
            bg=bg, fg=fg, font=(F, 12, "bold"), padx=42, pady=11,
        )
        self.run_btn.pack(anchor="center")
        self._refresh_run_count()

    def _run_count(self) -> int:
        """Live selection size for the Run button (user request: show what
        you picked). Preset mode = preset length; custom/undo = toggles on."""
        try:
            return len(self.selected_tasks())
        except Exception:
            return 0

    def _refresh_run_count(self):
        """Repaint 'Run (N)' / 'Undo Selected (N)' — plain label when 0."""
        if getattr(self, "_suspend_count_refresh", False):
            return  # F7: _set_all is coalescing — it refreshes once after
        try:
            btn = getattr(self, "run_btn", None)
            if btn is None or not btn.winfo_exists():
                return
            n = self._run_count()
            if self.mode == "undo":
                btn.config_text(f"Undo Selected ({n})" if n else "Undo Selected")
            else:
                btn.config_text(f"Run ({n})" if n else "Run")
        except Exception:
            pass

    # ---------------- interactions ---------------- #

    def _select_preset(self, name):
        # F2: mode switches update IN PLACE. The preset cards and the Run
        # button are built once (see _build); switching modes only changes
        # mode state, the body (cache hit = instant show/hide), the card
        # highlight, and the Run button's colors/label — no widget
        # destroy/recreate churn per click.
        self.mode = "preset"
        self._selected_preset = name
        self._build_body()
        self._update_run_row()
        self._highlight_cards()

    def _enter_custom(self):
        self.mode = "custom"
        self._selected_preset = None
        self._build_body()
        self._update_run_row()
        self._highlight_cards()

    def _enter_undo(self):
        self.mode = "undo"
        self._selected_preset = None
        # badges must reflect the machine's CURRENT applied state, not the
        # state from when the app was opened — repaint them in place on the
        # cached undo grid (F3); a missing cached body rebuilds fresh below
        self._refresh_undo_badges()
        self._build_body()
        self._update_run_row()
        self._highlight_cards()

    def _update_run_row(self):
        """F2: restyle the persistent Run button for the current mode
        instead of destroying/recreating it per switch (each rebuild also
        paid a temp-label font measure). The command never changes —
        _run_selected dispatches on self.mode — so only the colors and the
        base label differ per mode; _refresh_run_count adds the live count."""
        btn = getattr(self, "run_btn", None)
        try:
            if btn is None or not btn.winfo_exists():
                self._build_run_row()
                return
        except Exception:
            self._build_run_row()
            return
        accent = TAB_ACCENTS[self.tab_name]
        if self.mode == "undo":
            btn.set_style(bg=COLORS["accent_red"], fg="#FFFFFF",
                          hover_bg=_hex_lerp(COLORS["accent_red"], "#FFFFFF", 0.08))
        else:
            btn.set_style(bg=accent, fg=COLORS["black"],
                          hover_bg=_hex_lerp(accent, "#FFFFFF", 0.08))
        self._refresh_run_count()

    def _refresh_undo_badges(self):
        """F3: repaint the Undo grid's '✓ Active' badges IN PLACE from the
        machine's CURRENT applied-tweak state. Every undo row owns a badge
        label (created at grid build, packed only while the tweak is
        applied); entering Undo just packs/unpacks those handles — a couple
        of ms vs. rebuilding the ~60-row grid (~300+ ms) every entry. Rows
        are the ones captured when the undo grid was last built; after a
        run the whole body cache is cleared (see _clear_body_cache call in
        the run-completion path), so the next entry rebuilds once fresh."""
        try:
            self.app.tweak_state = get_tweak_state()
        except Exception:
            pass
        state = getattr(self.app, "tweak_state", None) or {}
        for outer, t in getattr(self, "_undo_rows", ()) or ():
            try:
                if not outer.winfo_exists():
                    continue
                badge = getattr(outer, "_undo_badge", None)
                if badge is None or not badge.winfo_exists():
                    continue
                if t.key in state:
                    if badge.winfo_manager() == "":
                        badge.pack(side="left", anchor="center", padx=(6, 0))
                else:
                    if badge.winfo_manager() != "":
                        badge.pack_forget()
            except Exception:
                continue

    def _highlight_cards(self):
        active = {"preset": self._selected_preset, "custom": "Custom", "undo": "Undo Tweaks"}.get(self.mode)
        for name, card in getattr(self, "preset_cards", {}).items():
            inner = card.winfo_children()[0]
            if name == active:
                accent = COLORS["accent_red"] if name == "Undo Tweaks" else TAB_ACCENTS[self.tab_name]
                inner.config(bg=COLORS["surface_hover"])
                for child in inner.winfo_children():
                    try:
                        child.config(bg=COLORS["surface_hover"])
                        for sub in child.winfo_children():
                            sub.config(bg=COLORS["surface_hover"])
                    except Exception:
                        pass
                card.config(bg=accent)
            else:
                inner.config(bg=COLORS["surface"])
                for child in inner.winfo_children():
                    try:
                        child.config(bg=COLORS["surface"])
                        for sub in child.winfo_children():
                            sub.config(bg=COLORS["surface"])
                    except Exception:
                        pass
                card.config(bg=COLORS["hairline"])

    def _set_all(self, on: bool):
        """F7: All On / All Off. Every var write fires its row trace, which
        refreshed the run-count button label once PER ROW (N redundant
        label re-measures + full canvas redraws). Suppress the per-row
        count refresh for the loop, then refresh exactly once. Everything
        else each row write does — the ToggleSwitch's own trace plays its
        animation — is untouched."""
        self._suspend_count_refresh = True
        try:
            for v in self.vars.values():
                v.set(on)
        finally:
            self._suspend_count_refresh = False
        self._refresh_run_count()

    def _apply_toggle_filter(self, q, placeholder, mode):
        """Filter the Custom/Undo rows as the user types: hide rows whose
        label/description doesn't match; hide empty group headers. 'All On/
        All Off' still respect hidden rows' vars (they act on self.vars
        directly), which is the honest behavior — hidden ≠ deselected.

        Grid mode (2-column): hidden cells use grid_remove() so their
        grid slot (row/column) is REMEMBERED — re-showing restores the
        exact position, and hidden rows leave no gap because grid_remove
        collapses empty rows."""
        q = (q or "").strip().lower()
        show_all = (not q) or q == placeholder.lower()
        blocks = getattr(self, "_cell_blocks", None) or []
        for _hdr, rows in blocks:
            if not rows:
                continue
            any_visible = False
            for _row, t in rows:
                managed = _row.winfo_manager() == "grid"
                if show_all or (q in t.label.lower() or q in t.description.lower()):
                    if not managed:
                        _row.grid()
                    any_visible = True
                else:
                    if managed:
                        _row.grid_remove()
            if _hdr is not None:
                hdr_managed = _hdr.winfo_manager() == "grid"
                if show_all or any_visible:
                    if not hdr_managed:
                        _hdr.grid()
                else:
                    if hdr_managed:
                        _hdr.grid_remove()
        # scroll metrics only recompute in refresh_scroll(); hiding rows
        # shrinks the inner frame — without this the thumb/offsets describe
        # the pre-filter content (overshoot scrolling, wrong thumb size).
        for w in self.body_area.winfo_children():
            if not w.winfo_ismapped():
                continue
            for child in w.winfo_children():
                if isinstance(child, ScrollableRoundedPanel):
                    child.refresh_scroll()

    def set_run_enabled(self, enabled: bool):
        if getattr(self, "run_btn", None):
            self.run_btn.set_enabled(enabled)

    def selected_tasks(self):
        if self.mode == "preset" and self._selected_preset:
            keys = self.presets[self._selected_preset]
            return [self.task_by_key[k] for k in keys]
        return [t for t in self.tasks if self.vars.get(t.key) and self.vars[t.key].get()]

    def undo_selected_tasks(self):
        return [t for t in self.tasks if self.vars.get(t.key) and self.vars[t.key].get() and t.revert]

    def _run_selected(self):
        if self.mode == "undo":
            selected = self.undo_selected_tasks()
            if not selected:
                messagebox.showinfo("Nothing to Undo", "Turn on at least one tweak to undo first.")
                return
            if not messagebox.askyesno("Confirm Undo",
                                       f"Put back {len(selected)} tweak(s) to how they were before?"):
                return
            self.app.run_tasks(self.tab_name, selected, mode="revert")
        else:
            selected = self.selected_tasks()
            if not selected:
                messagebox.showinfo("Nothing Selected", "Pick a preset or turn some switches on first.")
                return
            self.app.run_tasks(self.tab_name, selected, mode="run")


# --------------------------------------------------------------------------- #
# Install tab — Master App Catalog browser (Install.txt spec)
# --------------------------------------------------------------------------- #

class InstallTab(tk.Frame):
    """Catalog browser: categories with checkboxes, [FOSS] badges, hover
    tooltips (description + link hint), per-category Select All, and one
    'Install Selected Apps' button that runs everything through the unified
    winget engine (network pre-check, silent, latest, fallback URLs).

    Layout (user request): app rows flow in TWO columns per category —
    big categories stop being a scroll marathon. Names are the links
    (hover-underline); 🔗 is the mirror link when a fallback exists."""

    _CAT_COLUMNS = 2

    def __init__(self, parent, app):
        super().__init__(parent, bg=COLORS["bg"])
        self.app = app
        self.tab_name = "Install"
        self.vars = {}            # winget id -> BooleanVar
        self._cat_frames = {}     # category -> (header_frame, body_frame, open)
        self._app_rows = {}       # app id -> row widget (for installed badges)
        self._cat_sections = []  # (category, header, body, all_row_widgets) for the search filter
        self._bundle_rows = []   # (row_widget, searchable_text) — embedded bundle rows (APO+Peace)
        self._installed_ids = None  # cached set from install_tasks.get_installed_ids
        accent = TAB_ACCENTS["Install"]

        from app.app_catalog import APP_CATALOG, CATEGORY_ORDER, MANUAL_ONLY_APPS

        # top bar: hint + centered search. The action buttons live in a
        # bottom-middle run row (user request: match Clean/Repair/Tweak).
        topbar = tk.Frame(self, bg=COLORS["bg"])
        topbar.pack(fill="x", padx=26, pady=(14, 4))
        tk.Label(topbar, text="Pick apps to install — latest versions, silent install.",
                 font=(F, 10), bg=COLORS["bg"], fg=COLORS["subtext"]).pack(side="left")
        # search box (user request: big catalog, no way to find one).
        # Placeholder count stays true automatically (was hardcoded '147',
        # drifting every time the catalog changed).
        from app.app_catalog import APP_CATALOG as _AC, MANUAL_ONLY_APPS as _MO
        _search_ph = f"Search {len(_AC) + len(_MO)} apps…"
        # rounded + centered between the hint and the buttons (user request)
        search_frame = tk.Frame(topbar, bg=COLORS["bg"])
        search_frame.pack(side="left", expand=True, fill="x")
        self._search_var = tk.StringVar(value="")
        self._search_var.trace_add("write", lambda *_: self._apply_search())
        _sre = RoundedEntry(search_frame, textvariable=self._search_var, width=18,
                            accent=accent)
        _sre.pack(anchor="center")
        self._search_entry = _sre.entry
        self._search_entry.insert(0, _search_ph)
        self._search_placeholder = _search_ph
        self._search_focused = False
        self._search_entry.bind("<FocusIn>", self._search_focus_in)
        self._search_entry.bind("<FocusOut>", self._search_focus_out)

        # bottom-middle run row (user request: match the Clean/Repair/Tweak
        # Run buttons). Packed before the panel so the panel fills the
        # middle and the buttons can never be squeezed out of view.
        self.run_row = tk.Frame(self, bg=COLORS["bg"])
        self.run_row.pack(side="bottom", fill="x", padx=26, pady=(4, 10))
        _btns = tk.Frame(self.run_row, bg=COLORS["bg"])
        _btns.pack(anchor="center")
        # Update button: updates are an action, not an install selection —
        # live count from `winget upgrade` ("Update Apps (N)"). Starts in a
        # scanning state; the worker below paints the real count. Yellow so
        # it reads at a glance next to the green Install button.
        self.update_btn = AnimatedButton(
            _btns, text="Update Apps…", command=self._update_apps,
            bg=COLORS["accent_yellow"], fg=COLORS["black"], font=(F, 12, "bold"), padx=24, pady=11,
        )
        self.update_btn.pack(side="left", padx=6)
        self._update_count = None  # None = unknown/scanning, else int
        self.install_btn = AnimatedButton(
            _btns, text="Install", command=self._install_selected,
            bg=accent, fg=COLORS["black"], font=(F, 12, "bold"), padx=24, pady=11,
        )
        self.install_btn.pack(side="left", padx=6)
        self.install_btn.set_enabled(False)  # gray until something is picked

        panel = ScrollableRoundedPanel(self)
        panel.pack(fill="both", expand=True, padx=26, pady=6)
        self._panel = panel

        # --- Essentials: one-click tasks, split in two groups (user request):
        #   * "LTSC Missing Components" — Store, winget+UniGetUI, Xbox stack,
        #     Game Bar, Windows codecs (the bits LTSC strips out)
        #   * "Essentials" — the ALL-VC++/DirectX/.NET/Java/Classic runtime
        #     bundles (what used to be the "Runtimes & Dependencies" catalog
        #     category, merged so nobody guesses versions).
        # Styled as CHECKBOX ROWS to match the catalog below (user feedback:
        # the old button-grid looked different/inconsistent). Checking rows
        # selects them; the big 'Install Selected Apps' button runs the
        # checked Essentials TOGETHER with checked catalog apps. ---
        from app.tab_presets import TABS as ALL_TABS
        ess_tasks = list(ALL_TABS["Install"])
        self.ess_vars = {}
        self._ess_groups = {}       # group title -> {"body": frame, "arrow": label}
        self.bundle_vars = {}       # bundle key -> (BooleanVar, Task); rendered inside catalog categories
        self._cat_count_lbls = {}   # category -> "N apps" label (x/y selected)
        # group order follows TASKS order (Store stays first: brings winget)
        _seen_groups: list = []
        for _t in ess_tasks:
            _g = getattr(_t, "group", "Essentials")
            if _g not in _seen_groups:
                _seen_groups.append(_g)
        _group_subs = {
            "LTSC Missing Components": "— the Store, winget, Xbox, Game Bar & codecs that LTSC strips out",
            "Essentials": "— one-click runtime bundles: check them all, no version guessing",
        }

        # F6(a): the catalog rows (~950 widgets) are NOT built on the
        # synchronous startup path anymore — the constructor above (chrome,
        # buttons, panel) is all the first frame pays for. The sections
        # below are queued and built in idle-time slices by
        # _start_catalog_build() (one Essentials group or one category per
        # slice, each ~5-20 ms), which Application kicks off after its
        # post-startup pre-warm chain. Until the queue drains, a lightweight
        # placeholder marks the content area; features that touch rows
        # (search, badges, counts) are all pre-build-safe by design — see
        # _catalog_finish for what is applied once rows exist.
        self._catalog_queue = []
        for _g in _seen_groups:
            _gtasks = [t for t in ess_tasks if getattr(t, "group", "Essentials") == _g]
            self._catalog_queue.append(
                lambda g=_g, ts=_gtasks: self._build_ess_group(
                    panel.inner, g, _group_subs.get(g, ""), accent, ts))
        for cat in CATEGORY_ORDER:
            apps = [a for a in APP_CATALOG if a["category"] == cat]
            manuals = [a for a in MANUAL_ONLY_APPS if a["category"] == cat]
            if not apps and not manuals:
                continue
            self._catalog_queue.append(
                lambda c=cat, ap=apps, mn=manuals: self._build_category(
                    panel.inner, c, ap, mn, accent))

        self._catalog_ready = False   # queue fully drained?
        self._catalog_after = None    # chained-slice after-id
        self._pending_badges = None   # installed-badge scan that landed mid-build
        # "Preparing catalog…" marker. Since the progressive-show change
        # (2026-09, user-approved) the page CAN be shown half-built: the
        # marker sits above the sections that have already landed and
        # _catalog_finish drops it (plus applies pending badge/search/count
        # state) when the queue drains. It also still guards direct/harness
        # construction before any slice runs.
        self._placeholder = tk.Label(
            panel.inner, text="Preparing catalog…", font=(F, 10),
            bg=COLORS["bg_alt"], fg=COLORS["subtext"])
        self._placeholder.pack(pady=24)
        try:
            panel.refresh_scroll()
        except Exception:
            pass

        # installed badges + update count: kick off the (slow) winget calls
        # in worker threads, then paint on the Tk thread when they land.
        # Both paints are pre-build-safe (badges defer until rows exist).
        self._start_installed_badge_scan()
        self._start_update_count_scan()
        self._refresh_install_count()

    # ---------------- F6(a): chunked catalog build ---------------- #

    _CATALOG_SLICE_GAP_MS = 8   # breathing room between idle slices

    def _start_catalog_build(self):
        """F6(a): drain the queued catalog slices in idle time. Called by
        Application's post-startup pre-warm chain — AFTER the Custom/Undo
        pre-warm steps, so the one-off Tk costs (font loads) are already
        paid and every slice here stays small. No-op when already built or
        when a slice run is in flight."""
        if self._catalog_ready:
            return
        if self._catalog_after is not None:
            return  # a slice run is already in flight
        if not self._catalog_queue:
            # defensive: nothing queued but never finished — settle now
            self._catalog_finish()
            return
        self._catalog_step()

    def _catalog_step(self):
        """Build one queued slice (an Essentials group or a catalog
        category) and chain the rest on a small gap — the UI thread never
        blocks more than the slice takes (~5-20 ms)."""
        self._catalog_after = None
        if self._catalog_queue:
            try:
                self._catalog_queue.pop(0)()
            except Exception as exc:
                # honest failure: log and keep the chain alive — a widget
                # build error must not take the idle loop down
                try:
                    self.app.log(f"Install catalog: a section failed to build: {exc}")
                except Exception:
                    pass
        if self._catalog_queue:
            # Progressive-show (2026-09): while the user is actually
            # LOOKING at this page mid-build, settle the scroll metrics
            # after each slice so the scrollbar enables the moment the real
            # content outgrows the viewport (and the offset stays clamped).
            # Skipped while the page is hidden/covered — that pass would
            # only churn the visible page; _catalog_finish still does the
            # final full refresh when the queue drains. flush=False is
            # deliberate: the full refresh's update_idletasks forced every
            # pending idle redraw of the mapped content per slice (bench:
            # ~90-220 ms each) — the flush-free metrics pass recomputes
            # requested sizes without painting (~1-2 ms).
            if getattr(getattr(self, "app", None), "active_tab", None) == "Install":
                try:
                    self._panel.refresh_scroll(flush=False)
                except Exception:
                    pass  # panel torn down mid-build (shutdown)
            try:
                self._catalog_after = self.after(self._CATALOG_SLICE_GAP_MS,
                                                 self._catalog_step)
            except Exception:
                self._catalog_after = None  # root torn down mid-build
        else:
            self._catalog_finish()

    def _ensure_catalog_built(self):
        """F6(a): finish the queued catalog slices SYNCHRONOUSLY right now.

        Kept for direct/harness callers that need synchronous completeness
        (the repo smoke driver and measurement harness call it explicitly
        after switching to Install). The UI no longer calls this from
        _show_tab: reaching Install mid-build shows the page progressively
        instead of force-draining the queue on the click (the old call
        stalled the click 700-900 ms cold; measured 2026-09)."""
        if self._catalog_ready:
            return
        if self._catalog_after is not None:
            try:
                self.after_cancel(self._catalog_after)
            except Exception:
                pass
            self._catalog_after = None
        while self._catalog_queue:
            try:
                self._catalog_queue.pop(0)()
            except Exception as exc:
                try:
                    self.app.log(f"Install catalog: a section failed to build: {exc}")
                except Exception:
                    pass
        self._catalog_finish()

    def _catalog_finish(self):
        """F6(a): catalog fully built — drop the placeholder, then apply
        everything that arrived while the rows were still pending:
          * installed-badge scan result held in _pending_badges
          * a search query typed pre-build (fresh rows render filtered)
        and settle counts + scroll metrics on the real content."""
        self._catalog_ready = True
        try:
            if self._placeholder is not None and self._placeholder.winfo_exists():
                self._placeholder.destroy()
        except Exception:
            pass
        self._placeholder = None
        if self._pending_badges is not None:
            ids, self._pending_badges = self._pending_badges, None
            try:
                self._paint_installed_badges(ids)
            except Exception:
                pass
        try:
            q = self._search_var.get().strip()
            if q and not q.lower().startswith("search "):
                self._apply_search()
        except Exception:
            pass
        try:
            self._refresh_install_count()
            self._panel.refresh_scroll()
        except Exception:
            pass

    # ---------------- search box ---------------- #

    def _search_focus_in(self, _e):
        if not self._search_focused:
            self._search_focused = True
            if self._search_var.get().startswith("Search "):
                self._search_var.set("")

    def _search_focus_out(self, _e):
        self._search_focused = False
        if not self._search_var.get().strip():
            self._search_var.set(self._search_placeholder)

    def _apply_search(self):
        """Filter-as-you-type, Custom-tab semantics (user request): typing
        e.g. 'rufus' must CLEAR the page to only matching apps — no empty
        category dividers, no always-visible stragglers.

        Behavior:
          * empty/placeholder query -> full catalog back, Essentials back
          * otherwise: a category shows ONLY its matching rows (re-packed
            top-down per column, no gaps); a category with zero matches
            hides its whole block (divider + body) — an empty divider is
            noise, exactly the pre-fix bug
          * Essentials sections (LTSC components / runtime bundles) hide
            during a query too — they're one-click tasks, not catalog
            apps, so a name search must not leave them stranded mid-page
          * the embedded Equalizer APO + Peace GUI row participates like
            any app: hidden unless the query matches its label/description

        2-column layout: rows live inside per-column frames. Hiding one
        leaves a gap in that column, so visible rows are RE-PACKED per
        column (top-down flow, no gaps) from the filter result."""
        q = self._search_var.get().strip().lower()
        show_all = (not q) or q.startswith("search ")
        from app.app_catalog import APP_CATALOG, MANUAL_ONLY_APPS
        by_id = {a["id"]: a for a in APP_CATALOG}
        by_id.update({m["id"]: m for m in MANUAL_ONLY_APPS})
        for cat, header, body, rows in getattr(self, "_cat_sections", []):
            visible_rows = []
            for app_id, row in rows:
                app = by_id.get(app_id)
                if app is None:
                    continue
                if show_all or (q in app["name"].lower()
                                or q in app["id"].lower()
                                or q in app["description"].lower()):
                    visible_rows.append((app_id, row))
            any_visible = bool(visible_rows)
            # re-pack visible rows inside their OWN column frames, in original
            # order (pack_forget collapses, so no gaps). NOTE: rows must NEVER
            # move across columns: Tk forbids pack -in to any master that is
            # not the widget's parent or a descendant of it ("can't pack X
            # inside Y" — an uncle column is illegal), so the old round-robin
            # rebalance crashed on the first filtered search.
            col_frames = [c for c in body.winfo_children()
                          if isinstance(c, tk.Frame)]
            col_index = {str(c): i for i, c in enumerate(col_frames)}
            for app_id, row in rows:
                try:
                    row.pack_forget()  # parentage kept; re-pack below is legal
                except Exception:
                    pass
            buckets = [[] for _ in col_frames]
            for app_id, row in visible_rows:
                buckets[col_index.get(str(row.winfo_parent()), 0)].append(row)
            for col, bucket in zip(col_frames, buckets):
                for row in bucket:
                    row.pack(fill="x", padx=4, pady=2)
            # the whole category BLOCK hides when nothing matched — the
            # divider ('outer' frame: header + body + bundle row) packs
            # directly under the panel's inner frame, so pack_forget on it
            # removes divider AND body AND the bundle row in one move.
            outer_block = body.master
            bundle_visible = show_all or any(
                q in text for _row, text in getattr(self, "_bundle_rows", [])
                if _row.master is outer_block
            )
            hide_block = (not show_all) and not any_visible and not bundle_visible
            if hide_block:
                if outer_block.winfo_manager() == "pack":
                    outer_block.pack_forget()
            else:
                if outer_block.winfo_manager() != "pack":
                    outer_block.pack(fill="x", pady=(6, 4))
                # restore the body to the category's RECORDED open/collapsed
                # state (a search must not re-open a category the user
                # collapsed — the ▸/▾ arrow would otherwise desync).
                try:
                    cat_open = self._cat_frames[cat][2]
                except (KeyError, IndexError):
                    cat_open = True
                if cat_open and body.winfo_manager() != "pack":
                    body.pack(fill="x")
                elif not cat_open and body.winfo_manager() == "pack":
                    body.pack_forget()
            # embedded bundle rows (APO + Peace GUI): filter by their own
            # searchable text; hide during a non-matching query
            for _row, text in getattr(self, "_bundle_rows", []):
                if _row.master is not outer_block:
                    continue
                try:
                    if show_all or (q in text):
                        _row.pack(fill="x", padx=10, pady=(2, 6))
                    else:
                        _row.pack_forget()
                except Exception:
                    pass
        # Essentials sections: one-click tasks, not catalog apps — hide
        # entirely during a query (Custom-tab semantics: only matches)
        for _title, group in getattr(self, "_ess_groups", {}).items():
            try:
                ess_block = group["body"].master  # the 'ess' frame
                if show_all:
                    if ess_block.winfo_manager() != "pack":
                        ess_block.pack(fill="x", pady=(2, 6))
                else:
                    if ess_block.winfo_manager() == "pack":
                        ess_block.pack_forget()
            except Exception:
                pass
        # keep the panel's scroll region honest after re-flow
        try:
            self._panel.refresh_scroll()
        except Exception:
            pass

    # ---------------- selection count ---------------- #

    def _refresh_install_count(self):
        """Live 'Install (N)' count + per-category 'x/y selected' labels
        (user request: show what you picked). Traced from every checkbox
        var, so All/None/search-independent sets all land. The Install
        button stays grayed out until something is picked (user request)."""
        try:
            sel = self.selected_apps()
            n = len(sel)
            btn = getattr(self, "install_btn", None)
            if btn is not None:
                btn.config_text(f"Install ({n})" if n else "Install")
                busy = bool(getattr(getattr(self, "app", None), "_busy", False))
                btn.set_enabled(bool(n) and not busy)
            from app.app_catalog import APP_CATALOG
            for cat, total_lbl in getattr(self, "_cat_count_lbls", {}).items():
                total = sum(1 for a in APP_CATALOG if a["category"] == cat)
                picked = sum(1 for k, s in sel if k == "app" and s["category"] == cat)
                # Essentials tasks counted on the button only (they have no
                # category header of their own to annotate)
                try:
                    total_lbl.config(text=f"{picked}/{total} selected" if picked else f"{total} apps")
                except Exception:
                    pass
        except Exception:
            pass

    def _trace_install_var(self, var):
        """One shared trace callback for every Install checkbox var."""
        try:
            var.trace_add("write", lambda *_: self._refresh_install_count())
        except Exception:
            pass

    # ---------------- installed badges ---------------- #

    def _post_to_tk(self, fn):
        """Schedule fn on the Tk thread. InstallTab is a tk.Frame and has
        NO .root attribute — the old self.root.after(...) here raised
        AttributeError on every scan (swallowed by the except), so the
        installed-badges and Update-count callbacks NEVER ran: badges never
        appeared and the Update button sat at 'Update Apps…' forever.
        winfo_toplevel() resolves the real root window (the same trick
        Tooltip._ensure_shared uses), which lives for the whole session."""
        try:
            self.winfo_toplevel().after(0, fn)
        except Exception:
            pass  # shutdown race — root already destroyed; nothing to paint

    def _start_installed_badge_scan(self):
        """winget list is slow (1-3s) — run it once in a worker thread and
        post the result back; badges paint in one pass on the Tk thread."""

        def _scan():
            try:
                from app.tasks.install_tasks import get_installed_ids
                ids = get_installed_ids()
            except Exception:
                ids = None
            if ids is not None:
                # F-001 fix: route through the real root (self.root never
                # existed on a Frame — this call silently killed the scan).
                self._post_to_tk(lambda: self._paint_installed_badges(ids))

        threading.Thread(target=_scan, daemon=True).start()

    def _paint_installed_badges(self, installed_ids):
        """Show a green '✓ Installed' badge next to every catalog app that
        winget reports as present. Runs once per session (cached upstream).

        (Redesign note: rows no longer carry a '↗' link — names are the
        links now — so the badge simply packs at the row's right edge.)"""
        self._installed_ids = installed_ids or set()
        if not self._catalog_ready:
            # F6(a): the badge scan is kicked off at construction, but the
            # catalog rows build later in idle slices — hold the result so
            # _catalog_finish can paint it on the real rows.
            self._pending_badges = installed_ids or set()
            return
        n = 0
        for app_id, row in getattr(self, "_app_rows", {}).items():
            if app_id.lower() not in self._installed_ids:
                continue
            n += 1
            b = tk.Label(row, text="✓ Installed", font=(F, 7, "bold"),
                         bg=COLORS["bg_alt"], fg=COLORS["accent_green"])
            b.pack(side="right", padx=(0, 2))
        if n:
            self.app.log(f"Install tab: {n} of your catalog apps are already installed (badges shown).")

    # ---------------- Update Apps button ---------------- #

    def _start_update_count_scan(self, refresh: bool = False):
        """`winget upgrade` is slow (5-30s) — count upgradable apps in a
        worker thread, then paint 'Update Apps (N)' on the Tk thread."""

        def _scan():
            try:
                from app.tasks.install_tasks import get_upgradable_count
                n = get_upgradable_count(refresh=refresh)
            except Exception:
                n = None
            # F-001 fix: same dead self.root reference as the badge scan —
            # the Update-count paint never ran, so the button never showed
            # its real number. Post through the real toplevel instead.
            self._post_to_tk(lambda: self._paint_update_count(n))

        threading.Thread(target=_scan, daemon=True).start()

    def _paint_update_count(self, n):
        """Paint the Update button label. None = unknown (no winget /
        offline / parse failed) — plain 'Update Apps', still clickable.
        Always yellow (user request): it must read at a glance next to the
        Install button."""
        self._update_count = n
        btn = getattr(self, "update_btn", None)
        if btn is None:
            return
        try:
            if n is None:
                btn.config_text("Update Apps")
            else:
                btn.config_text(f"Update Apps ({n})")
            btn.set_style(bg=COLORS["accent_yellow"], fg=COLORS["black"])
        except Exception:
            pass

    def refresh_update_count(self):
        """Re-scan upgrades in the background (called after any install /
        update run completes, so the button never shows a stale number)."""
        btn = getattr(self, "update_btn", None)
        if btn is not None:
            try:
                btn.config_text("Update Apps…")
            except Exception:
                pass
        self._start_update_count_scan(refresh=True)

    def _update_apps(self):
        """Dedicated Update button: runs `winget upgrade --all` through the
        standard worker flow (progress bar + Stop button)."""
        from app.tasks.install_tasks import UPDATE_ALL_TASK
        self.app.install_selected_mixed([], [UPDATE_ALL_TASK])

    def _build_ess_group(self, parent, title, subtitle, accent, tasks):
        """One collapsible Essentials section (LTSC group or runtime group).
        Rows register into the shared self.ess_vars / self.vars pools so
        'Install Selected Apps' includes them naturally, in TASKS order.
        Header is arrow + titles only (user request: no All/None buttons)."""
        ess = tk.Frame(parent, bg=COLORS["bg_alt"])
        ess.pack(fill="x", pady=(2, 6))
        ess_head = tk.Frame(ess, bg=COLORS["surface"])
        ess_head.pack(fill="x")
        _ess_title = tk.Label(ess_head, text=title, font=(F, 10, "bold"),
                              bg=COLORS["surface"], fg=accent)
        _ess_title.pack(side="left", padx=(8, 4), pady=5)
        _ess_sub = tk.Label(ess_head, text=subtitle,
                            font=(F, 8), bg=COLORS["surface"], fg=COLORS["subtext"])
        _ess_sub.pack(side="left")

        body = tk.Frame(ess, bg=COLORS["bg_alt"])
        body.pack(fill="x")
        # arrow on the RIGHT like every other Install divider (user request)
        arrow = tk.Label(ess_head, text="▾", font=(F, 10, "bold"),
                         bg=COLORS["surface"], fg=accent, width=2)
        arrow.pack(side="right", padx=(0, 8), pady=6)

        def toggle_ess(b=body, a=arrow):
            if b.winfo_ismapped():
                b.pack_forget()
                a.config(text="▸")
            else:
                b.pack(fill="x")
                a.config(text="▾")

        arrow.bind("<Button-1>", lambda e: toggle_ess())
        ess_head.bind("<Button-1>", lambda e: toggle_ess())
        # hover recolors the bar AND its labels together (user bug: only the
        # bar changed, leaving boxed-looking text on mismatched backgrounds)
        def _ess_hover(on, _e=None):
            _bg = COLORS["surface_hover"] if on else COLORS["surface"]
            try:
                ess_head.config(bg=_bg)
                for _w in (_ess_title, _ess_sub, arrow):
                    _w.config(bg=_bg)
            except Exception:
                pass
        ess_head.bind("<Enter>", lambda e: _ess_hover(True))
        ess_head.bind("<Leave>", lambda e: _ess_hover(False))
        self._ess_groups[title] = {"body": body, "arrow": arrow}

        for task in tasks:
            var = tk.BooleanVar(value=False)
            self.ess_vars[task.key] = var
            # also register in the shared vars pool so 'Install Selected
            # Apps' includes Essentials naturally
            self.vars[f"task:{task.key}"] = var
            self._trace_install_var(var)
            row = tk.Frame(body, bg=COLORS["bg_alt"])
            row.pack(fill="x", padx=10, pady=2)
            cb = tk.Checkbutton(row, variable=var, bg=COLORS["bg_alt"],
                                fg=COLORS["text"], activebackground=COLORS["bg_alt"],
                                selectcolor=COLORS["surface"], onvalue=True, offvalue=False)
            cb.pack(side="left")
            name_lbl = tk.Label(row, text=task.label, font=(F, 9, "bold"),
                                bg=COLORS["bg_alt"], fg=COLORS["text"])
            name_lbl.pack(side="left", padx=(6, 4))
            # admin shield only (user request: no "one-click" text — the
            # shield matches the Custom toggle rows)
            if getattr(task, "admin_required", False):
                tg = tk.Label(row, text="🛡️", font=("Segoe UI Emoji", 9),
                              bg=COLORS["bg_alt"], fg=COLORS["text"], cursor="hand2")
                tg.pack(side="left", padx=(6, 0))
                Tooltip(tg, "Admin Required — needs Administrator rights (skipped in limited mode)")
            Tooltip(name_lbl, task.description)
            Tooltip(cb, task.description)

    def set_run_enabled(self, enabled: bool):
        if getattr(self, "install_btn", None):
            # re-enabling still respects the empty-selection gray-out
            # (user request) — only a non-empty pick lights Install up
            if enabled:
                try:
                    n = len(self.selected_apps())
                except Exception:
                    n = 1
                self.install_btn.set_enabled(bool(n))
            else:
                self.install_btn.set_enabled(False)
        if getattr(self, "update_btn", None):
            self.update_btn.set_enabled(enabled)

    def selected_apps(self):
        """Checked catalog apps + checked Essentials tasks, in one batch."""
        from app.app_catalog import APP_CATALOG
        from app.tab_presets import TABS as ALL_TABS
        task_by_key = {t.key: t for t in ALL_TABS["Install"]}
        result = []
        # Essentials first (Store before everything: brings winget)
        for key, var in getattr(self, "ess_vars", {}).items():
            if var.get():
                t = task_by_key.get(key)
                if t:
                    result.append(("task", t))
        # catalog-embedded bundles (APO + Peace lives in Media)
        for key, (var, t) in getattr(self, "bundle_vars", {}).items():
            if var.get():
                result.append(("task", t))
        for a in APP_CATALOG:
            v = self.vars.get(a["id"])
            if v is not None and v.get():
                result.append(("app", a))
        return result

    def _install_selected(self):
        selected = self.selected_apps()
        if not selected:
            messagebox.showinfo("Nothing Selected", "Check some Essentials or apps to install first.")
            return
        apps = [s for kind, s in selected if kind == "app"]
        tasks = [s for kind, s in selected if kind == "task"]
        self.app.install_selected_mixed(apps, tasks)

    def _build_category(self, parent, cat, apps, manuals, accent):
        outer = tk.Frame(parent, bg=COLORS["bg_alt"])
        outer.pack(fill="x", pady=(6, 4))

        header = tk.Frame(outer, bg=COLORS["surface"])
        header.pack(fill="x")
        # arrow on the RIGHT on every Install divider (user request)
        arrow = tk.Label(header, text="▾", font=(F, 10, "bold"), bg=COLORS["surface"],
                         fg=accent, width=2)
        arrow.pack(side="right", padx=(0, 8), pady=6)
        _cat_title = tk.Label(header, text=cat, font=(F, 10, "bold"), bg=COLORS["surface"],
                              fg=COLORS["text"])
        _cat_title.pack(side="left", padx=6)
        n_lbl = tk.Label(header, text=f"{len(apps) + len(manuals)} apps", font=(F, 9),
                         bg=COLORS["surface"], fg=COLORS["subtext"])
        n_lbl.pack(side="left", padx=4)
        self._cat_count_lbls[cat] = n_lbl

        def toggle_cat():
            body = self._cat_frames[cat][1]
            open_ = self._cat_frames[cat][2]
            if open_:
                body.pack_forget()
                arrow.config(text="▸")
                self._cat_frames[cat] = (self._cat_frames[cat][0], body, False)
            else:
                body.pack(fill="x")
                arrow.config(text="▾")
                self._cat_frames[cat] = (self._cat_frames[cat][0], body, True)

        # (user request: no All/None buttons on catalog category dividers —
        # pick apps individually; Essentials groups keep theirs)
        # hover recolors the bar AND its labels together (user bug: only the
        # bar changed, leaving boxed-looking text on mismatched backgrounds)
        def _cat_hover(on, _e=None):
            _bg = COLORS["surface_hover"] if on else COLORS["surface"]
            try:
                header.config(bg=_bg)
                for _w in (_cat_title, n_lbl, arrow):
                    _w.config(bg=_bg)
            except Exception:
                pass
        for w in (header, arrow):
            w.bind("<Button-1>", lambda e: toggle_cat())
            w.bind("<Enter>", lambda e: _cat_hover(True))
            w.bind("<Leave>", lambda e: _cat_hover(False))

        body = tk.Frame(outer, bg=COLORS["bg_alt"])
        body.pack(fill="x")
        cat_rows = []   # (app_id, row) — feeds the search filter
        cols = self._CAT_COLUMNS          # 2-column flow (user request)
        col_frames = []
        for c in range(cols):
            cf = tk.Frame(body, bg=COLORS["bg_alt"])
            cf.grid(row=0, column=c, sticky="nsew", padx=(6, 6))
            body.grid_columnconfigure(c, weight=1, uniform="catcols")
            col_frames.append(cf)

        def _make_name_link(parent, text, url, tip_text, base_fg=None, bold=True):
            """Clickable app name (user design): bold like the Custom task
            names, calm text color; hover turns it hyperlink-blue +
            underlined (click opens the primary link, 🔗 the mirror)."""
            fg = base_fg or COLORS["text"]
            _font = (F, 9, "bold") if bold else (F, 9)
            lbl = tk.Label(parent, text=text, font=_font,
                           bg=COLORS["bg_alt"], fg=fg, cursor="hand2")

            def _enter(_e):
                lbl.config(font=(F, 9, "bold", "underline") if bold else (F, 9, "underline"),
                           fg=COLORS["accent_blue"])

            def _leave(_e):
                lbl.config(font=_font, fg=fg)

            # Tooltip FIRST, hover binds with add="+": plain bind() replaces
            # (this exact ordering bug is why name hover highlighting silently
            # never worked — Tooltip's bind wiped it).
            if tip_text:
                Tooltip(lbl, tip_text)
            lbl.bind("<Enter>", _enter, add="+")
            lbl.bind("<Leave>", _leave, add="+")
            lbl.bind("<Button-1>", lambda e, u=url: self._open_url(u))
            return lbl

        def _checkbox_width_spacer(parent):
            """Width-matched invisible spacer holding the checkbox's slot in
            manual-only rows, so their names align exactly with the names in
            checkbox rows (user-reported misalignment). Sized from a real
            hidden Checkbutton — can't drift from theme/font changes the way
            a hardcoded pixel count would.

            Perf fix (2026-09-05, Install-tab lag report): this used to
            create AND destroy a real Checkbutton for every single
            manual-only row, on every category slice — dozens of throwaway
            widget creations (each a real OS window handle) during the
            idle-time catalog build, adding up to visible stutter switching
            into the tab. The probe measurement is identical every time
            (same theme/font for the whole tab lifetime), so it's now
            computed ONCE per InstallTab and cached on self — every
            subsequent manual row just reuses the cached width."""
            w = getattr(self, "_cb_spacer_width", None)
            if w is None:
                cb_probe = tk.Checkbutton(parent, bg=COLORS["bg_alt"],
                                           selectcolor=COLORS["surface"])
                w = cb_probe.winfo_reqwidth()
                cb_probe.destroy()
                self._cb_spacer_width = w
            sp = tk.Frame(parent, bg=COLORS["bg_alt"], width=w, height=1)
            sp.pack(side="left")
            sp.pack_propagate(False)   # hold the reserved width
            return sp

        for ci, app in enumerate(apps):
            var = tk.BooleanVar(value=False)
            self.vars[app["id"]] = var
            self._trace_install_var(var)
            row = tk.Frame(col_frames[ci % cols], bg=COLORS["bg_alt"])
            row.pack(fill="x", padx=4, pady=2)
            cb = tk.Checkbutton(row, variable=var, bg=COLORS["bg_alt"],
                                fg=COLORS["text"], activebackground=COLORS["bg_alt"],
                                selectcolor=COLORS["surface"], onvalue=True, offvalue=False)
            cb.pack(side="left")
            # tooltip: short description only (user request: no URLs in tips)
            tip = app["description"]
            name_lbl = _make_name_link(row, app["name"], app["url"], tip)
            name_lbl.pack(side="left", padx=(6, 4))
            if app["foss"]:
                foss = tk.Label(row, text="FOSS", font=(F, 7, "bold"),
                                bg=COLORS["bg_alt"], fg=accent)
                foss.pack(side="left", padx=(4, 0))
            # mirror link (user design): 🔗 beside the name as the SECONDARY
            # link when a verified fallback_url exists; the name stays the
            # primary. ↗ arrows are gone entirely.
            fallback = app.get("fallback_url", "")
            if fallback:
                self._make_mirror_link(row, fallback)
            Tooltip(cb, tip)
            cat_rows.append((app["id"], row))
            self._app_rows[app["id"]] = row

        # manual-only entries (no winget package exists): same row shape as
        # winget apps (user design) — NO checkbox (a width-matched spacer
        # keeps names aligned with checkbox rows, user-reported fix), normal
        # text color, name IS the link, FOSS badge when flagged, plus 🔗
        # when a mirror exists.
        for ci, app in enumerate(manuals):
            row = tk.Frame(col_frames[(len(apps) + ci) % cols], bg=COLORS["bg_alt"])
            row.pack(fill="x", padx=4, pady=2)
            _checkbox_width_spacer(row)
            fallback = app.get("fallback_url", "")
            # tooltip: short description only (user request: no URLs in tips;
            # the row has no checkbox, which already says "open the page")
            tip = app["description"]
            if fallback:
                tip += " (mirror available via 🔗)"
            name_lbl = _make_name_link(row, app["name"], app["url"], tip,
                                        base_fg=COLORS["text"])
            name_lbl.pack(side="left", padx=(6, 4))
            if app["foss"]:
                foss = tk.Label(row, text="FOSS", font=(F, 7, "bold"),
                                bg=COLORS["bg_alt"], fg=accent)
                foss.pack(side="left", padx=(4, 0))
            if fallback:
                self._make_mirror_link(row, fallback)
            cat_rows.append((app["id"], row))

        # Equalizer APO + Peace GUI bundle row (user request: lives at the
        # end of Media, not in its own section). Full-width below the
        # columns. Search behavior (user request): this row is NO LONGER
        # exempt from the filter — typing e.g. 'rufus' must clear the page
        # to ONLY matching apps, so the bundle hides too unless the query
        # matches it ('peace', 'eq', 'equalizer'...). Registered as a
        # pseudo-app row the filter can match and hide.
        if cat == "Media, Streaming & Audio":
            from app.tasks.install_tasks import APO_PEACE_TASK as _bundle
            _bvar = tk.BooleanVar(value=False)
            self.bundle_vars[_bundle.key] = (_bvar, _bundle)
            self._trace_install_var(_bvar)
            _brow = tk.Frame(outer, bg=COLORS["bg_alt"])
            _brow.pack(fill="x", padx=10, pady=(2, 6))
            _bcb = tk.Checkbutton(_brow, variable=_bvar, bg=COLORS["bg_alt"],
                                  fg=COLORS["text"], activebackground=COLORS["bg_alt"],
                                  selectcolor=COLORS["surface"], onvalue=True, offvalue=False)
            _bcb.pack(side="left")
            _blbl = tk.Label(_brow, text=_bundle.label, font=(F, 9, "bold"),
                             bg=COLORS["bg_alt"], fg=COLORS["text"])
            _blbl.pack(side="left", padx=(6, 4))
            _bsh = tk.Label(_brow, text="🛡️", font=("Segoe UI Emoji", 9),
                            bg=COLORS["bg_alt"], fg=COLORS["text"], cursor="hand2")
            _bsh.pack(side="left", padx=(6, 0))
            Tooltip(_bsh, "Admin Required — needs Administrator rights (skipped in limited mode)")
            Tooltip(_blbl, _bundle.description)
            Tooltip(_bcb, _bundle.description)
            # filter registration: searchable text from the bundle's own
            # label + description; stored on the OUTER block (its pack
            # master) so hiding one row never orphans the section logic.
            self._bundle_rows.append((_brow, f"{_bundle.label} {_bundle.description}".lower()))

        self._cat_frames[cat] = (header, body, True)
        self._cat_sections.append((cat, header, body, cat_rows))

    def _make_mirror_link(self, row, fallback):
        """🔗 mirror link with a press-flash (user request: some animation /
        effect on click so it feels alive). Press lights it in the tab
        accent; release opens the page and restores it. Same emoji font and
        size as the 🛡️ badge; no underline, ever."""
        _font = ("Segoe UI Emoji", 9)
        mirror = tk.Label(row, text="🔗", font=_font,
                          bg=COLORS["bg_alt"], fg=COLORS["subtext"],
                          cursor="hand2")
        mirror.pack(side="left", padx=(4, 0))

        def _press(_e):
            try:
                mirror.config(fg=TAB_ACCENTS["Install"])
            except Exception:
                pass

        def _release(e, u=fallback):
            try:
                mirror.config(fg=COLORS["subtext"], font=_font)
            except Exception:
                pass
            self._open_url(u)

        mirror.bind("<ButtonPress-1>", _press)
        mirror.bind("<ButtonRelease-1>", _release)
        Tooltip(mirror, "Backup download (official mirror)")
        return mirror

    @staticmethod
    def _resolve_default_browser_cmd() -> "list[str] | None":
        """Resolve the default https browser's launch command from the
        registry (UserChoice ProgId -> shell\\open\\command), or None.

        Why: os.startfile opens the URL via ShellExecute — but when the
        browser is ALREADY RUNNING, Windows' focus-stealing prevention
        leaves it in the background (taskbar flash, no window raise).
        Launching the browser exe directly with the URL as argv raises
        its window reliably, even when it's already open (user-reported:
        'opens the link but doesn't bring up the browser in view').

        Registry reality (verified on a live machine): the ProgId command
        template is registered under HKEY_CLASSES_ROOT (the merged
        HKCU+HKLM view) — e.g. FirefoxURL-<hash>\\shell\\open\\command —
        and often NOT under HKCU\\Software\\Classes. Check both, HKCR
        first as the authoritative merged view."""
        try:
            import winreg
            import shlex
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                                r"Software\Microsoft\Windows\Shell\Associations\UrlAssociations\https\UserChoice") as k:
                prog_id, _ = winreg.QueryValueEx(k, "ProgId")
            cmd = None
            for root, prefix in ((winreg.HKEY_CLASSES_ROOT, prog_id),
                                 (winreg.HKEY_CURRENT_USER, rf"Software\Classes\{prog_id}")):
                try:
                    with winreg.OpenKey(root, rf"{prefix}\shell\open\command") as k:
                        cmd, _ = winreg.QueryValueEx(k, "")
                        break
                except OSError:
                    continue
            if not cmd:
                return None
            # template contains %1 (or empty) — strip it; shlex splits the
            # quoted exe path correctly
            cmd = cmd.replace("%1", "").replace("%*", "").strip()
            parts = shlex.split(cmd, posix=False)
            parts = [p.strip('"') for p in parts if p.strip('"')]
            return parts or None
        except Exception:
            return None

    @classmethod
    def _open_url(cls, url: str):
        """Open a URL in the user's browser AND bring the browser window to
        the foreground.

        Route 1 (new, user-reported fix): launch the default browser's exe
        directly with the URL as an argument — window raises even when the
        browser is already running (ShellExecute/startfile leave a running
        browser in the background behind focus-stealing prevention).
        Route 2: os.startfile / ShellExecuteW (works when no browser is
        running, or the exe launch failed).
        Route 3: cmd start (last resort). All failures are shown, never
        swallowed (the original 'links do nothing' bug on LTSC)."""
        import os
        import subprocess
        # Route 1: direct browser launch (raises window if already running)
        browser_cmd = cls._resolve_default_browser_cmd()
        if browser_cmd:
            try:
                subprocess.Popen([*browser_cmd, url],
                                  creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                return
            except Exception:
                pass  # fall through to the shell routes
        # Route 2: shell default handler
        try:
            os.startfile(url)
            return
        except Exception:
            pass
        # Route 3: explicit shell open
        try:
            subprocess.Popen(["cmd", "/c", "start", "", url], shell=False,
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            return
        except Exception as exc:
            messagebox.showerror("Could Not Open Link",
                                 f"Couldn't open this page:\n{url}\n\n{exc}")

    def _set_category(self, cat, on: bool):
        from app.app_catalog import APP_CATALOG
        for app in APP_CATALOG:
            if app["category"] == cat and app["id"] in self.vars:
                self.vars[app["id"]].set(on)
        try:
            self._refresh_install_count()
        except Exception:
            pass


# --------------------------------------------------------------------------- #
# Main window
# --------------------------------------------------------------------------- #

class Application:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("Cleaner")
        self.root.geometry(WINDOW_SIZE)
        self.root.minsize(*WINDOW_MIN_SIZE)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self.root.configure(bg=COLORS["bg"])
        _set_window_icon(self.root)

        self._busy = False
        self._busy_lock = threading.Lock()
        self._cancel_requested = False
        self._worker_thread = None
        self.tweak_state = get_tweak_state()   # Applied-badge source (#14)

        self._build_style()
        if is_admin():
            self._build_main_ui()
        else:
            AdminGateFrame(self.root, on_continue_limited=self._build_main_ui)

    # ---------------- close guard (Phase 1 M3, kept) ---------------- #

    def _on_close(self):
        if self._busy:
            proceed = messagebox.askyesno(
                "Task Still Running",
                "A task is still running. Closing now may leave a Windows "
                "service stopped until you restart it manually.\n\n"
                "Stop the task and close anyway?",
                icon="warning",
            )
            if not proceed:
                return
            self._request_cancel()
            if self._worker_thread is not None:
                self._worker_thread.join(timeout=8.0)
                if self._worker_thread.is_alive():
                    self._log_full("  ! Task did not stop in time — closing anyway.")
        self._stop_disk_monitor()
        self.root.destroy()

    def _build_style(self):
        style = ttk.Style()
        style.theme_use("default")
        style.configure(".", background=COLORS["bg"], foreground=COLORS["text"])
        style.configure("TProgressbar", thickness=10, troughcolor=COLORS["surface"],
                        background=COLORS["accent_green"])
        # dark combobox + listbox (Auto Maintenance dialog)
        style.configure("TCombobox",
                        fieldbackground=COLORS["surface"], background=COLORS["surface"],
                        foreground=COLORS["text"], arrowcolor=COLORS["text"],
                        selectbackground=COLORS["surface_hover"], selectforeground=COLORS["text"],
                        bordercolor=COLORS["surface"], lightcolor=COLORS["surface"],
                        darkcolor=COLORS["surface"])
        style.map("TCombobox",
                  fieldbackground=[("readonly", COLORS["surface"]), ("disabled", COLORS["surface"])],
                  background=[("readonly", COLORS["surface"])],
                  foreground=[("readonly", COLORS["text"]), ("disabled", COLORS["subtext"])])
        self.root.option_add("*TCombobox*Listbox.background", COLORS["surface"])
        self.root.option_add("*TCombobox*Listbox.foreground", COLORS["text"])
        self.root.option_add("*TCombobox*Listbox.selectBackground", COLORS["surface_hover"])
        self.root.option_add("*TCombobox*Listbox.selectForeground", COLORS["text"])
        self.root.option_add("*TCombobox*Listbox.font", (F, 9))

    # ---------------- main UI ---------------- #

    def _build_main_ui(self):
        for widget in self.root.winfo_children():
            widget.destroy()

        self._build_menu()

        # Header: soap image + Cleaner + mode
        header = tk.Frame(self.root, bg=COLORS["bg"])
        header.pack(fill="x", padx=26, pady=(10, 0))
        hdr_img = None
        try:
            for p in self._asset_candidates("soap_header.png"):
                if p.is_file():
                    hdr_img = tk.PhotoImage(file=str(p))
                    break
        except Exception:
            hdr_img = None
        if hdr_img is not None:
            lbl_img = tk.Label(header, image=hdr_img, bg=COLORS["bg"])
            lbl_img.image = hdr_img
            lbl_img.pack(side="left", padx=(0, 8))
        tk.Label(header, text="Cleaner", font=(F, 16, "bold"), bg=COLORS["bg"],
                 fg=COLORS["text"]).pack(side="left")
        mode = "Administrator" if is_admin() else "Limited — cleaning only"
        mk = tk.Label(header, text=mode, font=(F, 9), bg=COLORS["bg"], fg=COLORS["subtext"])
        mk.pack(side="right", anchor="se", pady=(0, 2))

        # Pill switcher (Phase 2 #12: 5 tabs -> one sliding 3-way pill).
        # active_tab must exist before _build_tab_switch draws the thumb.
        self.active_tab = "Clean"
        self._build_tab_switch()

        # Tab pages (Install gets the catalog browser, others the preset UI)
        self.page_container = tk.Frame(self.root, bg=COLORS["bg"])
        self.page_container.pack(fill="both", expand=True, padx=4, pady=(0, 4))
        self.tabs = {}
        for name in TAB_NAMES:
            if name == "Install":
                self.tabs[name] = InstallTab(self.page_container, self)
            else:
                self.tabs[name] = TaskTab(self.page_container, self, name)
        self._show_tab("Clean")

        # Bottom: status line + details disclosure + progress + disk
        bottom = tk.Frame(self.root, bg=COLORS["bg"])
        bottom.pack(fill="x", padx=26, pady=(2, 10))

        stat_row = tk.Frame(bottom, bg=COLORS["bg"])
        stat_row.pack(fill="x")
        self.status_lbl = tk.Label(stat_row, text="Ready.", font=(F, 10, "bold"),
                                   bg=COLORS["bg"], fg=COLORS["subtext"], anchor="w")
        self.status_lbl.pack(side="left")
        self._status_fade_ids = []
        # drive chips (user request): one clickable chip per local drive /
        # USB stick, color-coded by fill level; click opens it in Explorer.
        # Up to MAX_INLINE_CHIPS inline; the rest live behind a "+N ▾" menu.
        self._drive_chips_frame = tk.Frame(stat_row, bg=COLORS["bg"])
        self._drive_chips_frame.pack(side="right")
        self._drive_chips: list = []      # [(label_widget, drive_root)]
        self._drive_extra: list = []      # drives behind the +N menu
        self._drive_last_key = None       # rebuild only when the set changes

        # progress overlay (segmented animated bar) — hidden until a run
        # starts (gamers see a clean window, not an empty progress track)
        self.progress_bar = AnimatedProgressBar(bottom, accent=COLORS["accent_green"])
        self._progress_hidden = True

        # Stop button — slots into the status row while a run is active
        self.cancel_btn = AnimatedButton(
            stat_row, text="✕ Stop", command=self._request_cancel,
            bg=COLORS["surface"], fg=COLORS["accent_red"], font=(F, 9, "bold"),
            padx=14, pady=6,
        )
        self.cancel_btn.set_enabled(False)

        self._build_log(bottom)
        self._start_disk_monitor()

        self.set_status("Ready. Pick a preset and press Run.")

        # F6: the window is up — pre-warm the first-entry Custom/Undo
        # bodies and the Install catalog in idle time instead of on the
        # user's first click / tab switch.
        self._schedule_idle_prewarm()

    # ---------------- F6: idle pre-warm chain ---------------- #

    _PREWARM_START_MS = 900    # let the window settle + first paint finish
    _PREWARM_STEP_GAP_MS = 250 # breathing room between monolithic builds

    def _schedule_idle_prewarm(self):
        """F6(b)+F6(a): after startup, one idle chain (a) pre-warms the
        four first-entry bodies — Clean/Repair/Tweak Custom + Tweak Undo —
        into the per-tab body caches and (b) hands off to the Install tab's
        chunked catalog builder.

        Each monolithic grid build is its own step and steps are spread
        ~250 ms apart, so a step never lands on top of user interaction
        that arrived during the previous one. The grid steps run FIRST on
        purpose: they pay the one-off per-process Tk costs (font loads,
        notably the ~200 ms first Segoe UI Emoji font) so every Install
        catalog slice that follows stays well under the ~30 ms budget.
        Guards: busy runs postpone the chain; a failing step is dropped;
        bodies the user already built are cache hits and skip instantly."""
        if getattr(self, "_idle_chain_scheduled", False):
            return
        self._idle_chain_scheduled = True
        steps = []
        for tab_name, mode in (("Clean", "custom"), ("Repair", "custom"),
                               ("Tweak", "custom"), ("Tweak", "undo")):
            page = self.tabs[tab_name]
            steps.append(lambda p=page, m=mode: p._prewarm_body(m))
        steps.append(lambda: self.tabs["Install"]._start_catalog_build())
        self._idle_steps = steps

        def _step():
            if not getattr(self, "_idle_chain_scheduled", False):
                return
            if self._busy:
                # a run is active — postpone rather than stall its UI
                try:
                    self.root.after(500, _step)
                except Exception:
                    pass
                return
            if self._idle_steps:
                step = self._idle_steps.pop(0)
                try:
                    step()
                except Exception:
                    pass  # a failed pre-warm must never crash the app
            if self._idle_steps:
                try:
                    self.root.after(self._PREWARM_STEP_GAP_MS, _step)
                except Exception:
                    pass

        try:
            self.root.after(self._PREWARM_START_MS, _step)
        except Exception:
            pass

    def _asset_candidates(self, fname):
        cands = []
        if getattr(sys, "_MEIPASS", None):
            cands.append(pathlib.Path(sys._MEIPASS) / "app" / "assets" / fname)
            cands.append(pathlib.Path(sys._MEIPASS) / "assets" / fname)
        cands.append(pathlib.Path(__file__).with_name("assets") / fname)
        cands.append(pathlib.Path(__file__).resolve().parents[1] / "app" / "assets" / fname)
        return cands

    # ---------------- pill switcher ---------------- #

    # User-approved faster switch (2026-09): 240 -> 160 ms. Still the same
    # time-based ease-out at ~60 fps ticks; the slide just completes sooner.
    SWITCH_ANIM_MS = 160

    def _build_tab_switch(self):
        """One pill, three labels, one thumb that slides + recolors (#12,
        user idea: 'combine the 3 tab buttons into one animated switch').

        Flicker fix (user feedback): all canvas items are created ONCE and
        the animation only moves/recolors them (coords/itemconfigure). The
        old version did delete("all") + full recreate every frame, which
        leaves a blank-frame window between delete and recreate and
        re-rasterizes the text glyphs each frame — that was the visible
        flicker/micro-stutter. Persistent items let Tk repaint only the
        small damaged region. Motion is also time-based (eased), so a late
        frame never changes the animation's speed — only its smoothness."""
        switch_wrap = tk.Frame(self.root, bg=COLORS["bg"])
        switch_wrap.pack(pady=(10, 2))
        # width scales with the number of tabs (4 tabs now: Install added)
        n_tabs = len(TAB_NAMES)
        self._switch_labels = TAB_NAMES
        self._switch_w = max(444, 148 * n_tabs)
        self.switch = tk.Canvas(switch_wrap, width=self._switch_w, height=44,
                                highlightthickness=0, bd=0, bg=COLORS["bg"])
        self.switch.pack()
        self._seg = self._switch_w / n_tabs
        self._switch_pos = float(TAB_NAMES.index(self.active_tab))
        self._switch_anim_after = None
        self.switch.bind("<Button-1>", self._on_switch_click)
        self.switch.bind("<Motion>", self._on_switch_motion)
        self.switch.bind("<Leave>", lambda e: self.switch.config(cursor="arrow"))
        # keyboard accessibility (audit a11y): the pill accepts Tab focus;
        # Left/Right arrows move between tabs. Focus is indicated by the
        # thumb's own canvas — no native highlightthickness rectangle
        # (user feedback: stray outline).
        self.switch.configure(takefocus=1, highlightthickness=0)
        self.switch.bind("<Left>", lambda e: self._switch_to(
            TAB_NAMES[max(0, TAB_NAMES.index(self.active_tab) - 1)]))
        self.switch.bind("<Right>", lambda e: self._switch_to(
            TAB_NAMES[min(len(TAB_NAMES) - 1, TAB_NAMES.index(self.active_tab) + 1)]))
        self.switch.bind("<Return>", lambda e: None)  # arrows are the activation
        self.switch.bind("<space>", lambda e: None)
        # persistent items — created once, never deleted
        self._switch_track = self._canvas_round_rect(
            self.switch, 0, 0, self._switch_w, 44, 22,
            fill=COLORS["bg_alt"], outline="")
        thumb_w = self._seg - 8
        x = self._thumb_x(self._switch_pos)
        self._switch_thumb = self._canvas_round_rect(
            self.switch, x, 4, x + thumb_w, 40, 18,
            fill=TAB_ACCENTS[self.active_tab], outline="")
        self._switch_texts = []
        for i, name in enumerate(self._switch_labels):
            cx = self._seg * i + self._seg / 2
            self._switch_texts.append(self.switch.create_text(cx, 22, text=name))
        self._style_switch_labels()

    def _thumb_x(self, t):
        thumb_w = self._seg - 8
        return 4 + t * self._seg + (self._seg - thumb_w - 8) / 2

    def _place_switch_thumb(self, t, fill):
        thumb_w = self._seg - 8
        x = self._thumb_x(t)
        points = self._round_points(x, 4, x + thumb_w, 40, 18)
        self.switch.coords(self._switch_thumb, *points)
        self.switch.itemconfigure(self._switch_thumb, fill=fill)

    def _style_switch_labels(self):
        for i, tid in enumerate(self._switch_texts):
            active = self._switch_labels[i] == self.active_tab
            self.switch.itemconfigure(
                tid,
                fill=COLORS["black"] if active else COLORS["subtext"],
                font=(F, 11, "bold") if active else (F, 10),
            )

    def _on_switch_click(self, event):
        seg = min(len(TAB_NAMES) - 1, max(0, int(event.x // self._seg)))
        self._switch_to(TAB_NAMES[seg])

    def _on_switch_motion(self, event):
        seg = min(len(TAB_NAMES) - 1, max(0, int(event.x // self._seg)))
        self.switch.config(cursor="hand2" if seg != TAB_NAMES.index(self.active_tab) else "arrow")

    def _switch_to(self, name):
        if self._busy or name == self.active_tab:
            # audit fix (P2-08): silently swallowing clicks while busy reads
            # as "app is frozen". Show the busy state instead.
            if self._busy:
                self.set_status("Busy — wait for the current task to finish (or press ✕ Stop).")
            return
        from_accent = TAB_ACCENTS[self.active_tab]
        self.active_tab = name
        # cancel any in-flight animation so a fast re-click retargets cleanly
        if self._switch_anim_after is not None:
            try:
                self.root.after_cancel(self._switch_anim_after)
            except Exception:
                pass
        self._switch_anim = {
            "start_t": self._switch_pos,
            "target_t": float(TAB_NAMES.index(name)),
            "start_ms": time.monotonic(),
            "from_accent": from_accent,
            "to_accent": TAB_ACCENTS[name],
        }
        self._style_switch_labels()
        # F5 (perf): swap the content page and flush its geometry BEFORE
        # the slide starts. The old order ticked the thumb first and laid
        # the new page out inside the 240 ms animation window — on heavy
        # pages (Install ~950 widgets) the synchronous layout/paint stalled
        # the slide, which then jumped to the end. update_idletasks() does
        # the page's geometry pass once so the animation's first frame runs
        # on an already-settled page.
        #
        # BENCHMARK A/B (2026-09): moving that flush out of the click
        # handler (letting idle-time layout land inside the animation
        # window) was measured as a net LOSS: click sync dropped ~10 ms,
        # but total click-to-settled grew ~12 ms and one update round
        # inside the slide absorbed the whole layout (~10 ms on the Tweak
        # grids, maxR 0.8 -> 12 ms) — a dropped frame right where the
        # slide should look smoothest. The flush stays here.
        #
        # Under the stacked-pages PROTO (see _show_tab) the warm-switch
        # flush is near-free (the raised page was already laid out, so
        # update_idletasks has ~nothing to do) but still required on a
        # page's FIRST visit, where place() maps the whole subtree: that
        # layout must settle before the slide's first frame, same as it
        # did under pack. Re-checked by measurement (2026-09 bench, see
        # the session report): warm flush cost stayed negligible, so the
        # flush is kept under the PROTO as well.
        self._show_tab(name)
        self.root.update_idletasks()
        self._tick_switch()

    def _tick_switch(self):
        anim = getattr(self, "_switch_anim", None)
        if anim is None:
            return
        # monotonic() is in SECONDS — convert to ms before dividing by the
        # ms-named duration (units bug made the slide take 240 *seconds*).
        p = (time.monotonic() - anim["start_ms"]) * 1000.0 / self.SWITCH_ANIM_MS
        if p >= 1.0:
            self._switch_pos = anim["target_t"]
            self._place_switch_thumb(self._switch_pos, anim["to_accent"])
            self._switch_anim = None
            self._switch_anim_after = None
            return
        # ease-out cubic: fast start, gentle landing — no visible snap
        eased = 1 - (1 - p) ** 3
        t = anim["start_t"] + (anim["target_t"] - anim["start_t"]) * eased
        self._switch_pos = t
        self._place_switch_thumb(t, _hex_lerp(anim["from_accent"], anim["to_accent"], eased))
        # ~60 fps ticks (F5): Tk cannot present faster than the display
        # refresh, and 100 wakeups/s during a 240 ms slide buys nothing —
        # each tick just re-times the eased position, so the pacing is
        # unchanged (time-based math above), only the wakeup rate drops.
        self._switch_anim_after = self.root.after(16, self._tick_switch)

    def _canvas_round_rect(self, c, x0, y0, x1, y1, r, **kw):
        return c.create_polygon(self._round_points(x0, y0, x1, y1, r), smooth=True, **kw)

    @staticmethod
    def _round_points(x0, y0, x1, y1, r):
        return [
            x0 + r, y0, x1 - r, y0, x1, y0, x1, y0 + r,
            x1, y1 - r, x1, y1, x1 - r, y1, x0 + r, y1,
            x0, y1, x0, y1 - r, x0, y0 + r, x0, y0,
        ]

    def _show_tab(self, name):
        # -------------------------------------------------------------
        # PROTO (user-approved experiment, 2026-09): always-mapped STACKED
        # tab pages. All pages live in page_container (the "stage" — it is
        # the shared container the pages were already direct children of;
        # nothing else is packed into it). A page is place()d once into the
        # stage on its FIRST show (relx/rely/relwidth/relheight=1 fills the
        # stage exactly, so a page's geometry equals the old pack result),
        # then switching only tkraise()s the target above its siblings —
        # no pack_forget/pack anywhere in the switch path, so switching
        # never unmaps a page and never triggers a full re-layout/re-raster
        # of the shown page ("I can watch the content render" stutter).
        # Every page root frame is opaque (bg=COLORS["bg"]), so a lower
        # page can never show through. Pages are stacked ONLY after their
        # first visit: an unvisited page is not managed at all (same as the
        # old pack_forget state — zero startup cost, and the Install page's
        # idle catalog slices build into an unmanaged frame exactly as
        # before). Rollback: restore the backup at
        #   ~/.openclaw-autoclaw/agents/auto-coder/workspace/.openclaw/tmp/
        #   gui.py.bak-20260905-054920  (sha256 4289ED3C...2132D8)
        # and revert the _switch_to flush note if kept.
        # -------------------------------------------------------------
        # F6(a)+progressive show (2026-09): the Install catalog builds in
        # idle-time chunks after startup. Reaching Install mid-build used to
        # force-drain the remaining queue synchronously HERE (measured
        # 700-900 ms cold on the click). Now the queue is NOT drained: we
        # only make sure the slice chain is running (or restart it if a
        # cancelled chain left the queue non-empty — the page must never
        # sit on its placeholder forever), then show the page as-is with
        # the "Preparing catalog…" placeholder; the queued slices keep
        # building in idle and _catalog_finish drops the placeholder and
        # applies pending badge/search/count state when they drain.
        if name == "Install":
            _start = getattr(self.tabs[name], "_start_catalog_build", None)
            if _start is not None:
                try:
                    _start()   # guarded: no-op when ready / already in flight
                except Exception:
                    pass
        target = self.tabs[name]
        if target.winfo_manager() != "place":
            target.place(relx=0, rely=0, relwidth=1, relheight=1)
        target.tkraise()
        # progress bar may not exist yet during initial build
        if hasattr(self, "progress_bar"):
            accent = TAB_ACCENTS[name]
            self.progress_bar._accent = accent
            self.progress_bar._draw()
        # one global wheel router: scroll whichever rounded panel on the
        # visible page contains the pointer (per-page bind_all would let the
        # LAST-bound page eat every wheel event for the whole app)
        if not getattr(self, "_wheel_bound", False):
            self._wheel_bound = True
            self.root.bind_all("<MouseWheel>", self._on_wheel_routed)

    def _on_wheel_routed(self, e):
        page = self.tabs.get(self.active_tab)
        if page is None or not page.winfo_ismapped():
            return
        target = None
        w = self.root.winfo_containing(e.x_root, e.y_root)
        node = w
        while node is not None:
            if isinstance(node, ScrollableRoundedPanel):
                target = node
                break
            node = node.master
        if target is not None:
            target.on_wheel(e.delta)

    # ---------------- menu ---------------- #

    def _build_menu(self):
        menubar = tk.Menu(self.root)
        tools_menu = tk.Menu(menubar, tearoff=0)
        tools_menu.add_command(label="Task Manager", command=lambda: self._launch("taskmgr"))
        tools_menu.add_command(label="Disk Cleanup", command=lambda: self._launch("cleanmgr"))
        tools_menu.add_command(label="Reliability Monitor", command=lambda: self._launch("perfmon /rel"))
        tools_menu.add_command(label="System Restore", command=lambda: self._launch("rstrui"))
        tools_menu.add_command(label="Power Options", command=lambda: self._launch("powercfg.cpl"))
        tools_menu.add_separator()
        # Windows Settings deep-links (ms-settings: URIs — must go through
        # the shell, not Popen: the URI scheme isn't an executable)
        tools_menu.add_command(label="Startup Apps", command=lambda: self._launch_settings("ms-settings:startupapps"))
        tools_menu.add_command(label="Uninstall Programs", command=lambda: self._launch_settings("ms-settings:appsfeatures"))
        tools_menu.add_command(label="Storage Sense", command=lambda: self._launch_settings("ms-settings:storagesense"))
        tools_menu.add_command(label="Windows Update", command=lambda: self._launch_settings("ms-settings:windowsupdate"))
        tools_menu.add_command(label="Network Settings", command=lambda: self._launch_settings("ms-settings:network-status"))
        tools_menu.add_command(label="Sound Settings", command=lambda: self._launch_settings("ms-settings:sound"))
        tools_menu.add_command(label="App Volume & Devices", command=lambda: self._launch_settings("ms-settings:apps-volume"))
        tools_menu.add_command(label="Graphics Settings", command=lambda: self._launch_settings("ms-settings:display-advancedgraphics"))
        tools_menu.add_separator()
        tools_menu.add_command(label="Auto Maintenance...", command=self._show_schedule_dialog)
        tools_menu.add_command(label="Export Logs", command=self.export_logs)
        menubar.add_cascade(label="Quick Tools", menu=tools_menu)
        # no Help menu (user request); About lives in Quick Tools now
        tools_menu.add_separator()
        tools_menu.add_command(label="About", command=self._show_about)
        self.root.config(menu=menubar)

    def _launch(self, command):
        try:
            subprocess.Popen(command, shell=True)
        except Exception as exc:
            messagebox.showerror("Could Not Launch", str(exc))

    def _launch_settings(self, uri: str):
        """Open a Windows Settings page via its ms-settings: URI."""
        try:
            os.startfile(uri)  # noqa: S606 — shell-resolved URI, not an exe
        except Exception as exc:
            messagebox.showerror("Could Not Open", f"Couldn't open Windows Settings:\n{exc}")

    def _show_about(self):
        messagebox.showinfo("About", f"{APP_NAME}\n\nSafe, reversible Windows cleaning & tuning.")

    # ---------------- Auto Maintenance dialog ---------------- #

    def _show_schedule_dialog(self):
        from app.config_persist import load_config
        from app.scheduler import (enable_schedule, disable_schedule,
                                   get_schedule_status, run_auto_update)

        config = load_config()
        enabled = config.get("schedule_enabled", False)
        update_enabled = config.get("schedule_update_enabled", False)
        freq = config.get("schedule_frequency", "weekly")
        time_str = config.get("schedule_time", "03:00")

        dlg = tk.Toplevel(self.root)
        dlg.title("Auto Maintenance")
        dlg.geometry("420x420")
        dlg.resizable(False, False)
        dlg.configure(bg=COLORS["bg"])
        dlg.transient(self.root)
        dlg.grab_set()
        dlg.update_idletasks()
        x = self.root.winfo_rootx() + (self.root.winfo_width() - dlg.winfo_width()) // 2
        y = self.root.winfo_rooty() + (self.root.winfo_height() - dlg.winfo_height()) // 2
        dlg.geometry(f"+{x}+{y}")

        enabled_var = tk.BooleanVar(value=enabled)
        # audit fix: held reference instead of dlg.winfo_children()[0] —
        # the positional lookup silently retargets if a widget is ever
        # added before the button (the label below was packed AFTER, but
        # the pattern was one reorder away from corrupting the wrong widget)
        main_toggle_btn = AnimatedButton(
            dlg, text="On" if enabled else "Off",
            command=lambda: enabled_var.set(not enabled_var.get()),
            bg=COLORS["accent_green"] if enabled else COLORS["surface"],
            fg=COLORS["black"] if enabled else COLORS["text"], font=(F, 10, "bold"),
        )
        main_toggle_btn.pack(pady=(16, 4))
        # keep the button label in sync without recursion
        def _sync_toggle(*_):
            on = enabled_var.get()
            main_toggle_btn.config_text("Maintenance: On" if on else "Maintenance: Off")
            main_toggle_btn.set_style(bg=COLORS["accent_green"] if on else COLORS["surface"],
                                      fg=COLORS["black"] if on else COLORS["text"])
        enabled_var.trace_add("write", _sync_toggle)
        _sync_toggle()

        # --- Update Everything schedule (user request: scheduler expansion):
        # a second scheduled task that runs winget upgrade --all headless. ---
        upd_frame = tk.Frame(dlg, bg=COLORS["bg"])
        upd_frame.pack(fill="x", padx=16, pady=(6, 2))
        upd_var = tk.BooleanVar(value=update_enabled)
        def _sync_upd(*_):
            on = upd_var.get()
            upd_btn.config_text("Update Everything: On" if on else "Update Everything: Off")
            upd_btn.set_style(bg=COLORS["accent_green"] if on else COLORS["surface"],
                              fg=COLORS["black"] if on else COLORS["text"])
        upd_btn = AnimatedButton(
            upd_frame, text="Update Everything: On" if update_enabled else "Update Everything: Off",
            command=lambda: upd_var.set(not upd_var.get()),
            bg=COLORS["accent_green"] if update_enabled else COLORS["surface"],
            fg=COLORS["black"] if update_enabled else COLORS["text"], font=(F, 9, "bold"),
        )
        upd_btn.pack(anchor="w")
        upd_var.trace_add("write", _sync_upd)
        _sync_upd()
        tk.Label(upd_frame, text="also runs 'winget upgrade --all' on the schedule below — apps stay current automatically",
                 font=(F, 8), bg=COLORS["bg"], fg=COLORS["subtext"],
                 wraplength=380, justify="left").pack(anchor="w")
        # 'update now' shortcut: headless update without scheduling anything
        AnimatedButton(upd_frame, text="Update Everything Now", command=lambda: self._run_update_now(dlg),
                       bg=COLORS["surface"], fg=COLORS["text"], font=(F, 9),
                       padx=12, pady=5).pack(anchor="w", pady=(4, 0))

        freq_frame = tk.Frame(dlg, bg=COLORS["bg"])
        freq_frame.pack(fill="x", padx=16, pady=6)
        tk.Label(freq_frame, text="How often:", bg=COLORS["bg"], fg=COLORS["text"],
                 font=(F, 9)).pack(side="left")
        freq_var = tk.StringVar(value=freq)
        freq_combo = tk.OptionMenu(freq_frame, freq_var, "daily", "weekly", "monthly")
        freq_combo.config(bg=COLORS["surface"], fg=COLORS["text"],
                          activebackground=COLORS["surface_hover"], activeforeground=COLORS["text"],
                          highlightthickness=0, bd=0, relief="flat",
                          font=(F, 9), width=12, indicatoron=True)
        try:
            menu = freq_combo["menu"]
            menu.config(bg=COLORS["surface"], fg=COLORS["text"],
                        activebackground=COLORS["surface_hover"], activeforeground=COLORS["text"],
                        bd=0, relief="flat", font=(F, 9))
        except Exception:
            pass
        freq_combo.pack(side="right")

        time_frame = tk.Frame(dlg, bg=COLORS["bg"])
        time_frame.pack(fill="x", padx=16, pady=6)
        tk.Label(time_frame, text="Time (24h):", bg=COLORS["bg"], fg=COLORS["text"],
                 font=(F, 9)).pack(side="left")
        time_var = tk.StringVar(value=time_str)
        tk.Entry(time_frame, textvariable=time_var, width=10,
                 bg=COLORS["surface"], fg=COLORS["text"], insertbackground=COLORS["text"],
                 font=(F, 9), bd=0, highlightthickness=1,
                 highlightbackground=COLORS["hairline"], highlightcolor=COLORS["accent_green"]).pack(side="right")

        task_frame = tk.Frame(dlg, bg=COLORS["bg"])
        task_frame.pack(fill="x", padx=16, pady=8)
        total_selected = sum(len(v) for v in config.get("selected_tasks", {}).values())
        tk.Label(task_frame, text=f"Tasks to run: {total_selected}",
                 bg=COLORS["bg"], fg=COLORS["subtext"], font=(F, 9)).pack(anchor="w")
        tk.Label(task_frame, text="(Tasks come from the last thing you ran)",
                 bg=COLORS["bg"], fg=COLORS["subtext"], font=(F, 9)).pack(anchor="w")

        status_var = tk.StringVar(value="")
        tk.Label(dlg, textvariable=status_var, bg=COLORS["bg"],
                 fg=COLORS["accent_blue"], font=(F, 9), wraplength=340, justify="left").pack(anchor="w", padx=16)

        def refresh_status():
            ok, out = get_schedule_status()
            ok_upd, _ = get_schedule_status(["--auto-update"])
            parts = []
            parts.append("Auto maintenance: scheduled." if ok else "Auto maintenance: not scheduled.")
            parts.append("Update Everything: scheduled." if ok_upd else "Update Everything: not scheduled.")
            status_var.set("  ".join(parts))

        def apply():
            results = []
            # NOTE (audit HIGH fix): this dialog captured its config dict at
            # open time. Saving that WHOLE stale copy rolled back everything
            # a concurrent run wrote meanwhile (applied_tweaks, snapshots) —
            # and the four save_config() calls below were redundant anyway:
            # enable_schedule/disable_schedule already load a FRESH config
            # and persist exactly these schedule fields (scheduler.py).
            # So this function now only calls the scheduler helpers and
            # never persists the stale dict.
            # main maintenance schedule
            if enabled_var.get():
                ok, msg = enable_schedule(freq_var.get(), time_var.get())
                if ok:
                    results.append("Maintenance enabled")
                else:
                    messagebox.showerror("Failed", f"Could not create maintenance task:\n{msg}", parent=dlg)
            else:
                ok, msg = disable_schedule()
                if ok:
                    results.append("Maintenance disabled")
                else:
                    messagebox.showerror("Failed", f"Could not remove maintenance task:\n{msg}", parent=dlg)
            # update-everything schedule (separate schtasks entry)
            if upd_var.get():
                ok, msg = enable_schedule(freq_var.get(), time_var.get(), extra_args=["--auto-update"])
                if ok:
                    results.append("Update Everything enabled")
                else:
                    messagebox.showerror("Failed", f"Could not create update task:\n{msg}", parent=dlg)
            else:
                ok, msg = disable_schedule(["--auto-update"])
                if ok:
                    results.append("Update Everything disabled")
            if results:
                status_var.set("; ".join(results))
            refresh_status()

        btn_frame = tk.Frame(dlg, bg=COLORS["bg"])
        btn_frame.pack(fill="x", padx=16, pady=(10, 16))
        AnimatedButton(btn_frame, text="Apply", command=apply,
                       bg=COLORS["accent_green"], fg=COLORS["black"], font=(F, 10, "bold"),
                       ).pack(side="right", padx=4)
        AnimatedButton(btn_frame, text="Close", command=dlg.destroy,
                      bg=COLORS["surface"], fg=COLORS["text"], font=(F, 10),
                      ).pack(side="right")
        refresh_status()

    def _run_update_now(self, parent_dlg=None):
        """'Update Everything Now' — runs the Install tab's Update button task
        through the standard worker flow (progress bar + Stop button),
        without touching any schedules."""
        from app.tasks.install_tasks import UPDATE_ALL_TASK
        if parent_dlg is not None:
            try:
                parent_dlg.destroy()
            except Exception:
                pass
        self.install_selected_mixed([], [UPDATE_ALL_TASK])

    # ---------------- disk monitor (drive chips) ---------------- #
    # User request: show EVERY drive (internal, USB, SD — anything with a
    # drive letter), color-coded by fill level, click opens it in Explorer.
    # Rebuilt each tick so a freshly plugged USB stick shows up within one
    # poll (30s) without a restart. MTP phone storage is deliberately out
    # of scope: no drive letter, slow/flaky free-space queries.

    _MAX_INLINE_CHIPS = 3

    @staticmethod
    def _query_drives():
        """All ready drives with free/total bytes. Uses GetLogicalDrives +
        GetDiskFreeSpaceExW (both instant, no WMI) — returns
        [(letter, root_path, free_gb, total_gb)], C: first, rest sorted."""
        import ctypes
        drives = []
        bitmask = ctypes.windll.kernel32.GetLogicalDrives()
        if not bitmask:
            return drives
        for i in range(26):
            if not (bitmask >> i) & 1:
                continue
            letter = chr(ord("A") + i)
            root = f"{letter}:\\"
            free = ctypes.c_ulonglong(0)
            total = ctypes.c_ulonglong(0)
            avail = ctypes.c_ulonglong(0)
            # drives with 0 total = no media (empty card reader slots) — skip
            res = ctypes.windll.kernel32.GetDiskFreeSpaceExW(
                ctypes.c_wchar_p(root),
                ctypes.byref(avail), ctypes.byref(total), ctypes.byref(free),
            )
            if res and total.value > 0:
                gb = 1024 ** 3
                drives.append((letter, root, free.value / gb, total.value / gb))
        # C: first (always the one people glance for), then alphabetical
        drives.sort(key=lambda d: (d[0] != "C", d[0]))
        return drives

    @staticmethod
    def _drive_fill_color(free_gb, total_gb):
        """Green normally, amber under ~15% free, red under ~5% — the
        'will this game fit?' glance check."""
        if total_gb <= 0:
            return COLORS["subtext"]
        ratio = free_gb / total_gb
        if ratio <= 0.05:
            return COLORS["accent_red"]
        if ratio <= 0.15:
            return COLORS["accent_yellow"]
        return COLORS["accent_green"]

    def _open_drive(self, root):
        """Open a drive in Explorer via os.startfile (ShellExecute) — the
        right route for folders; the browser-raising _open_url path is for
        web URLs only. Works on stripped Windows where webbrowser-based
        opens fail silently."""
        import os as _os
        try:
            _os.startfile(root)
        except Exception as exc:
            messagebox.showerror("Could Not Open Drive",
                                 f"Couldn't open {root}\n\n{exc}")

    def _show_extra_drives_menu(self):
        """The '+N ▾' overflow menu: every drive that didn't fit inline,
        with free space and the same color rules; click opens in Explorer."""
        if not self._drive_extra:
            return
        menu = tk.Menu(self.root, tearoff=0, bg=COLORS["surface"],
                       fg=COLORS["text"], activebackground=COLORS["surface_hover"],
                       activeforeground=COLORS["text"], bd=0,
                       font=(F, 9))
        for i, (letter, root, free_gb, total_gb) in enumerate(self._drive_extra):
            col = self._drive_fill_color(free_gb, total_gb)
            menu.add_command(
                label=f"{letter}:  {free_gb:.0f} GB free of {total_gb:.0f} GB",
                command=lambda r=root: self._open_drive(r),
            )
            try:
                menu.entryconfig(i, foreground=col)
            except Exception:
                pass
        try:
            menu.tk.call("tk_popup", menu, self.root.winfo_pointerx(),
                          self.root.winfo_pointery() + 12)
        except Exception:
            pass

    def _rebuild_drive_chips(self, drives):
        """Recreate the inline chips (only when the drive set changed —
        labels update in place otherwise). C: always inline; overflow
        collapses into the '+N' menu chip."""
        for w in self._drive_chips_frame.winfo_children():
            w.destroy()
        self._drive_chips = []
        self._drive_extra = []
        inline, extra = drives[:self._MAX_INLINE_CHIPS], drives[self._MAX_INLINE_CHIPS:]
        for letter, root, free_gb, total_gb in inline:
            col = self._drive_fill_color(free_gb, total_gb)
            # fixed width stops the status row from shifting every poll as
            # the free-GB number changes digit count (audit minor CLS)
            lbl = tk.Label(self._drive_chips_frame,
                            text=f" {letter}: {free_gb:5.0f} GB free ",
                            font=(F, 9, "bold"), bg=COLORS["bg"], fg=col,
                            cursor="hand2")
            lbl.pack(side="left", padx=(6, 0))
            lbl.bind("<Button-1>", lambda e, r=root: self._open_drive(r))
            Tooltip(lbl, f"{root} — {free_gb:.0f} GB free of {total_gb:.0f} GB. Click to open in Explorer.")
            self._drive_chips.append((lbl, root))
        if extra:
            self._drive_extra = extra
            more = tk.Label(self._drive_chips_frame,
                            text=f" +{len(extra)} drives ▾ ", font=(F, 9, "bold"),
                            bg=COLORS["bg"], fg=COLORS["subtext"], cursor="hand2")
            more.pack(side="left", padx=(6, 0))
            more.bind("<Button-1>", lambda e: self._show_extra_drives_menu())
            Tooltip(more, "More connected drives:\n" +
                    "\n".join(f"{d[0]}: {d[2]:.0f} GB free" for d in extra))

    def _start_disk_monitor(self):
        if hasattr(self, "_disk_monitor_after_id") and self._disk_monitor_after_id:
            try:
                self.root.after_cancel(self._disk_monitor_after_id)
            except Exception:
                pass
        self._disk_monitor_running = True

        def update_disk():
            if not getattr(self, "_disk_monitor_running", False):
                return
            try:
                drives = self._query_drives()
                # rebuild widgets only when the drive SET changes (plug /
                # unplug); color+text refresh in place on every tick
                key = tuple(d[0] for d in drives)
                if key != self._drive_last_key:
                    self._drive_last_key = key
                    self._rebuild_drive_chips(drives)
                for lbl, root in self._drive_chips:
                    for letter, r2, free_gb, total_gb in drives:
                        if r2 == root:
                            lbl.config(
                                text=f" {letter}: {free_gb:5.0f} GB free ",
                                fg=self._drive_fill_color(free_gb, total_gb),
                            )
                            break
            except Exception:
                pass
            try:
                if getattr(self, "_disk_monitor_running", False) and self.root.winfo_exists():
                    self._disk_monitor_after_id = self.root.after(30000, update_disk)
            except Exception:
                pass

        try:
            self._disk_monitor_after_id = self.root.after(0, update_disk)
        except Exception:
            pass

    def _stop_disk_monitor(self):
        self._disk_monitor_running = False
        if hasattr(self, "_disk_monitor_after_id") and self._disk_monitor_after_id:
            try:
                self.root.after_cancel(self._disk_monitor_after_id)
            except Exception:
                pass
            self._disk_monitor_after_id = None

    # ---------------- log (in-memory only, #16 simplified per user) ------- #
    # No log window in the UI anymore — the log accumulates in memory and
    # "Export Logs" (Quick Tools menu) saves it to a .txt for debugging.

    def _build_log(self, parent):
        self._log_lines = []
        self._log_lines.append("Welcome to Cleaner Tool.")

    def log(self, text):
        def _do():
            self._log_lines.append(f"[{time.strftime('%H:%M:%S')}] {text}")
            if len(self._log_lines) > 8000:
                del self._log_lines[:2000]
        try:
            if threading.current_thread() is threading.main_thread():
                _do()
            else:
                self.root.after(0, _do)
        except Exception:
            pass

    # keep old internal name working for close-guard message
    def _log_full(self, text):
        self.log(text)

    def export_logs(self):
        """Save the full in-memory log to a .txt the user picks (user
        request: replaces the old View Details window — keeps debugging
        possible without console clutter in the UI)."""
        from tkinter import filedialog
        try:
            default_name = f"CleanerTool_Log_{time.strftime('%Y%m%d_%H%M%S')}.txt"
            path = filedialog.asksaveasfilename(
                parent=self.root,
                title="Export Logs",
                defaultextension=".txt",
                initialfile=default_name,
                filetypes=[("Text files", "*.txt"), ("All files", "*.*")],
            )
            if not path:
                return
            header = (
                f"Cleaner Tool log export\n"
                f"Exported: {time.strftime('%Y-%m-%d %H:%M:%S')}\n"
                f"Edition: {self._windows_edition_str()}\n"
                f"{'=' * 60}\n"
            )
            with open(path, "w", encoding="utf-8") as f:
                f.write(header + "\n".join(self._log_lines) + "\n")
            # user bug: exporting mid-run overwrote the live "Running X…"
            # status with "Log saved to …" and it never came back. When a
            # run is active the status belongs to the run — log only.
            if not getattr(self, "_busy", False):
                self.set_status(f"Log saved to {path}")
            self.log(f"Log exported to {path}")
        except Exception as exc:
            messagebox.showerror("Export Failed", f"Could not save the log:\n{exc}")

    def _windows_edition_str(self) -> str:
        try:
            import winreg
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                                r"SOFTWARE\Microsoft\Windows NT\CurrentVersion") as k:
                product = winreg.QueryValueEx(k, "ProductName")[0]
                display = winreg.QueryValueEx(k, "DisplayVersion")[0]
            return f"{product} ({display})"
        except Exception:
            return "Windows"

    # ---------------- status line (fading, #16) ---------------- #

    def set_status(self, text):
        def _do():
            try:
                if hasattr(self, "status_lbl") and self.status_lbl.winfo_exists():
                    # cancel pending fades
                    for aid in getattr(self, "_status_fade_ids", []):
                        try:
                            self.root.after_cancel(aid)
                        except Exception:
                            pass
                    self._status_fade_ids = []
                    self.status_lbl.config(text=text, fg=TAB_ACCENTS.get(self.active_tab, COLORS["accent_green"]))
                    # fade back to muted after 4s (unless running)
                    if not self._busy:
                        aid = self.root.after(4000, lambda: self._fade_status())
                        self._status_fade_ids.append(aid)
            except Exception:
                pass
        try:
            if threading.current_thread() is threading.main_thread():
                _do()
            else:
                self.root.after(0, _do)
        except Exception:
            pass

    def _fade_status(self):
        try:
            self.status_lbl.config(fg=COLORS["subtext"])
        except Exception:
            pass

    # ---------------- progress ---------------- #

    def _set_progress(self, value, maximum=None):
        def _do():
            try:
                if maximum:
                    self.progress_bar.set_fraction(value / maximum)
                else:
                    self.progress_bar.set_fraction(value)
            except Exception:
                pass
        try:
            self.root.after(0, _do)
        except Exception:
            pass

    def _stop_indeterminate(self):
        """Turn off the shimmer and paint the full bar — ON THE TK THREAD.

        F-003 fix: _run_tasks_worker used to call
        progress_bar.set_indeterminate(False) directly from the worker
        thread. set_indeterminate(False) does after_cancel + Canvas _draw
        (delete/all + item creation) — real Tcl calls. Tkinter is not
        thread-safe; the identical Install-tab path (install_selected_mixed
        _done) was already fixed to marshal via root.after for exactly this
        reason, but this call site never got the fix. Same shape as
        _set_progress above: try/except only guards the shutdown race."""
        def _do():
            try:
                self.progress_bar.set_indeterminate(False)
            except Exception:
                pass
        try:
            self.root.after(0, _do)
        except Exception:
            pass

    # ---------------- run flow ---------------- #

    @staticmethod
    def _preflight_check(tasks):
        """Plain-language notices before a long run: pending reboot, low
        disk, admin rights, network (only when a task looks like it needs
        the internet — install/repair-download tasks). Returns a list of
        notice strings; empty = all clear. Non-blocking (ask-to-continue
        in the caller, never a hard stop)."""
        notices = []

        # pending reboot (CBS RebootPending) — repair tasks work better after
        try:
            import winreg as _wr
            with _wr.OpenKey(_wr.HKEY_LOCAL_MACHINE,
                             r"SOFTWARE\Microsoft\Windows\CurrentVersion\Component Based Servicing\RebootPending"):
                notices.append("Windows is waiting for a restart — restarting first makes repairs more reliable.")
        except OSError:
            pass
        except Exception:
            pass

        # free disk on C:
        try:
            import ctypes as _ct
            free = _ct.c_ulonglong(0)
            total = _ct.c_ulonglong(0)
            avail = _ct.c_ulonglong(0)
            if _ct.windll.kernel32.GetDiskFreeSpaceExW(_ct.c_wchar_p("C:\\"),
                                                       _ct.byref(avail),
                                                       _ct.byref(total),
                                                       _ct.byref(free)):
                gb = free.value / (1024 ** 3)
                if gb < 5:
                    notices.append(f"Only {gb:.1f} GB free on C: — cleaning needs breathing room; free up space first.")
        except Exception:
            pass

        # admin rights when selected tasks need them (limited mode would
        # silently skip them — better said up front)
        if not is_admin():
            admin_tasks = [t for t in tasks if t.admin_required]
            if admin_tasks:
                notices.append(f"{len(admin_tasks)} task(s) need Administrator — they'll be skipped in limited mode "
                               "(restart the app as Administrator for the full run).")

        # network when a task is an internet task (Install tab always is;
        # repair DISM RestoreHealth downloads too)
        _net_keys = {t.key for t in tasks}
        _needs_net = any(k.startswith("install_") for k in _net_keys) \
                     or "dism_restorehealth" in _net_keys
        if _needs_net:
            try:
                from app.downloader import has_network
                if not has_network():
                    notices.append("No internet connection detected — some tasks need to download files.")
            except Exception:
                pass

        return notices

    def _set_buttons_enabled(self, enabled: bool):
        def _do():
            try:
                for tab in self.tabs.values():
                    tab.set_run_enabled(enabled)
                if getattr(self, "cancel_btn", None) is not None:
                    self.cancel_btn.set_enabled(not enabled)
                if not enabled:
                    self._progress_show()
                else:
                    self._progress_hide()
            except Exception:
                pass
        try:
            self.root.after(0, _do)
        except Exception:
            pass

    def _progress_show(self):
        try:
            # bottom frame = the progress bar's own parent; pack at its top
            self.progress_bar.pack(fill="x", pady=(6, 2))
            self.cancel_btn.pack(side="right")
            self._progress_hidden = False
        except Exception:
            pass

    def _progress_hide(self):
        try:
            self.progress_bar.pack_forget()
            self.cancel_btn.pack_forget()
            self._progress_hidden = True
        except Exception:
            pass

    def install_selected_mixed(self, apps, tasks):
        """Install-tab runner: checked Essentials tasks FIRST (Store brings
        winget, bundles before individual apps), then catalog apps — one
        worker, one progress bar, fully cancelable via the Stop button
        (install commands are registered with the cancel registry now)."""
        with self._busy_lock:
            if self._busy:
                messagebox.showinfo("Busy", "Please wait for the current operation to finish.")
                return
            self._busy = True

        # Admin gating for Essentials that need it (same rule as other tabs)
        if not is_admin():
            admin_blocked = [t for t in tasks if t.admin_required]
            if admin_blocked:
                names = ", ".join(t.label for t in admin_blocked)
                self.log(f"Limited mode: skipping {len(admin_blocked)} admin-only installer(s): {names}")
                tasks = [t for t in tasks if not t.admin_required]
                if tasks or apps:
                    messagebox.showwarning(
                        "Administrator Required",
                        f"Skipped {len(admin_blocked)} installer(s) that need admin:\n{names}\n\n"
                        "Continuing with the rest.",
                    )
            if not tasks and not apps:
                messagebox.showinfo("Nothing to Run", "All selected installers need Administrator rights.")
                with self._busy_lock:
                    self._busy = False
                return

        self._cancel_requested = False
        for tab in self.tabs.values():
            tab.set_run_enabled(False)
        # user bug: the bar + Stop button never appeared during install /
        # update runs (only the tab buttons disabled) — a 10-minute
        # `winget upgrade --all` looked dead. Show them like run_tasks does.
        # Determinate % is impossible for winget batch runs, so indeterminate
        # + live per-package status lines (see _live_status_ctx) is the
        # honest signal.
        self._progress_show()
        self.cancel_btn.set_enabled(True)
        self.progress_bar.set_fraction(0.0)
        self.progress_bar.set_indeterminate(True)
        total = len(apps) + len(tasks)
        self.set_status(f"Installing {total} item(s)...")
        self.log(f"===== Installing {total} selected item(s) =====")

        ctx = TaskContext(
            log=self.log, set_status=self.set_status,
            cancelled=lambda: self._cancel_requested,
        )

        def _done(ok, summary, ok_n=0, fail_n=0):
            # audit fix: this ran on the WORKER thread — set_indeterminate/
            # set_run_enabled touch Tk widgets/Canvases, and Tkinter is not
            # thread-safe (rare non-deterministic crashes at install end).
            # Hop to the Tk thread first, like _set_buttons_enabled does.
            def _ui():
                self.progress_bar.set_indeterminate(False)
                self._set_progress(1, 1)
                self._progress_hide()
                self.cancel_btn.set_enabled(False)
                with self._busy_lock:
                    self._busy = False
                self._worker_thread = None
                self._cancel_requested = False
                for tab in self.tabs.values():
                    tab.set_run_enabled(True)
                # installs/updates change what `winget upgrade` reports —
                # re-scan so "Update Apps (N)" never shows a stale number.
                try:
                    install_tab = self.tabs.get("Install")
                    if install_tab is not None and hasattr(install_tab, "refresh_update_count"):
                        install_tab.refresh_update_count()
                except Exception:
                    pass
                self.set_status(summary)
                messagebox.showinfo("Install Done", summary)
            self.root.after(0, _ui)

        def _worker():
            ok_n, fail_n, stopped = 0, 0, False
            # 1) Essentials tasks (sequential, honest per-task errors)
            for task in tasks:
                if ctx.cancelled():
                    stopped = True
                    break
                self.set_status(f"Running {task.label}...")
                self.log(f"--- {task.label} ---")
                try:
                    task.run(ctx)
                    ok_n += 1
                except TaskCancelled as exc:
                    # audit minor 2: a cancelled update is stopped, not a
                    # failure — don't count it in the error box
                    stopped = True
                    self.log(f"  {task.label} was cancelled: {exc}")
                    break
                except Exception as exc:
                    fail_n += 1
                    self.log(f"  ! {task.label} failed: {exc}")
            # 2) Catalog apps — per-app honesty (audit fix: the old code did
            # ok_n += len(apps) whenever install_selected_apps didn't raise,
            # counting failed apps as installed; the runner now reports the
            # real per-app outcome)
            if not stopped and apps:
                from app.tasks.install_tasks import install_selected_apps as _runner
                app_ok, app_fail = _runner(ctx, apps)
                ok_n += len(app_ok)
                fail_n += len(app_fail)
            if stopped:
                self.log("Stopped — remaining items were skipped.")
                _done(False, f"Stopped: {ok_n} finished before stopping.", ok_n, fail_n)
            else:
                summary = f"Install complete: {ok_n} succeeded" + (f", {fail_n} failed" if fail_n else "") + "."
                _done(True, summary, ok_n, fail_n)

        thread = threading.Thread(target=_worker, daemon=True)
        self._worker_thread = thread
        thread.start()

    def run_tasks(self, tab_name, tasks, mode="run"):
        all_selected = list(tasks)  # H1: capture real selection before filtering
        if not is_admin():
            admin_tasks = [t for t in tasks if t.admin_required]
            if admin_tasks:
                names = ", ".join(t.label for t in admin_tasks)
                self.log(f"Limited mode: skipping {len(admin_tasks)} admin-only task(s): {names}")
                tasks = [t for t in tasks if not t.admin_required]
                messagebox.showwarning(
                    "Administrator Required",
                    f"Skipped {len(admin_tasks)} admin-only task(s):\n{names}\n\n"
                    "Running the rest in limited mode.",
                )
            if not tasks:
                messagebox.showinfo(
                    "Nothing to Run",
                    "All selected tasks require Administrator rights. Restart as Administrator to run them.",
                )
                return

        with self._busy_lock:
            if self._busy:
                messagebox.showinfo("Busy", "Please wait for the current operation to finish.")
                return
            self._busy = True

        # ---- Pre-flight checks (user request): fail fast with plain
        # language instead of dying mid-DISM. Long-run = 5+ tasks or any
        # task flagged long-running by the Repair tab's nature. ----
        try:
            preflight = self._preflight_check(tasks)
        except Exception:
            preflight = []
        if preflight:
            msg = "Before we start:\n\n" + "\n".join(f"  • {p}" for p in preflight)
            if not messagebox.askyesno("Pre-flight Check", msg + "\n\nStart anyway?", icon="warning"):
                with self._busy_lock:
                    self._busy = False
                return
            self.log("Pre-flight notices: " + " | ".join(preflight))

        warnings = check_dangerous_combos(tasks)
        if warnings:
            warnings = list(dict.fromkeys(warnings))
            msg = "⚠️ Potentially problematic task combinations detected:\n\n" + "\n\n".join(warnings)
            msg += "\n\nDo you want to continue?"
            if not messagebox.askyesno("Confirm Task Combination", msg, icon="warning"):
                with self._busy_lock:
                    self._busy = False
                return

        # Save selection for scheduler (H1: run mode only, full pre-filter list)
        if mode == "run":
            try:
                from app.config_persist import load_config, save_config
                config = load_config()
                config.setdefault("selected_tasks", {})[tab_name] = [t.key for t in all_selected]
                save_config(config)
            except Exception:
                pass

        self._cancel_requested = False
        self._set_buttons_enabled(False)
        self.progress_bar.set_fraction(0.0)
        self.progress_bar.set_indeterminate(True)
        self.set_status(f"Starting {len(tasks)} task(s)...")

        thread = threading.Thread(target=self._run_tasks_worker, args=(tab_name, tasks, mode), daemon=True)
        self._worker_thread = thread
        thread.start()

    def _request_cancel(self):
        if not self._busy:
            return
        self._cancel_requested = True
        try:
            from app.utils import cancel_current_command
            cancel_current_command()
        except Exception:
            pass
        self.log("Stop requested — finishing the current task, then stopping.")

    def _run_tasks_worker(self, tab_name, tasks, mode):
        verb = "Undoing" if mode == "revert" else "Running"
        self.log(f"===== {verb} {len(tasks)} {tab_name} task(s) =====")

        ctx = TaskContext(log=self.log, set_status=self.set_status,
                          cancelled=lambda: self._cancel_requested)

        total_bytes = 0
        completed, failed = 0, 0
        skipped_n = 0
        cancelled = False
        self._set_progress(0, len(tasks))

        for idx, task in enumerate(tasks):
            if ctx.cancelled():
                self.log("Cancelled by user — remaining tasks were skipped.")
                cancelled = True
                break
            self.set_status(f"{verb}: {task.label}...")
            func = task.revert if mode == "revert" else task.run

            if func is None:
                self.log(f"  ! No {'revert' if mode=='revert' else 'run'} for '{task.label}'")
                failed += 1
                self._set_progress(idx + 1, len(tasks))
                continue
            try:
                result = func(ctx)
                if isinstance(result, int) and not isinstance(result, bool):
                    total_bytes += result
                elif isinstance(result, bool) and not result:
                    raise RuntimeError("Task returned False")
                completed += 1
                # Phase 2 (#14): keep the applied-tweak registry truthful
                if mode == "revert":
                    mark_tweak_reverted(task.key)
                else:
                    if task.revert is not None:  # it's a tweak
                        mark_tweak_applied(task.key)
            except TaskSkipped as exc:
                # B5 audit fix: a skip means nothing changed on this
                # machine — count it as completed-with-skip, log the
                # reason, but do NOT record the tweak as applied (the
                # '✓ Active' badge must show what is actually active).
                skipped_n += 1
                self.log(f"  (skipped) {task.label}: {exc}")
            except Exception as exc:
                failed += 1
                self.log(f"  ! ERROR in '{task.label}': {exc}")
            self._set_progress(idx + 1, len(tasks))

        # refresh applied state for badges
        try:
            self.tweak_state = get_tweak_state()
        except Exception:
            pass
        # audit fix: cached Undo bodies hold stale "✓ Active" badges built
        # from the PRE-run tweak_state. _clear_body_cache existed for exactly
        # this but had zero call sites — _enter_undo only popped the 'undo'
        # key, leaving preset/custom bodies stale after a revert. Drop all
        # cached bodies now that the machine state has actually changed.
        try:
            tweak_tab = self.tabs.get("Tweak")
            if tweak_tab is not None:
                tweak_tab._clear_body_cache()
        except Exception:
            pass

        needs_reboot = any(getattr(t, "risk", "") == "REBOOT REQUIRED" for t in tasks)
        # B5: skipped tasks (nothing to do on this machine) get their own
        # honest count instead of being lumped into 'succeeded'.
        summary_core = f"{verb} {'stopped early' if cancelled else 'complete'}: {completed} succeeded"
        if skipped_n:
            summary_core += f", {skipped_n} skipped (nothing to change)"
        summary_core += f", {failed} failed."
        summary_lines = [summary_core]
        if cancelled:
            summary_lines.append("Remaining tasks were skipped (cancelled by user).")
        if total_bytes > 0:
            summary_lines.append(f"Disk space freed: {format_bytes(total_bytes)}")
        if needs_reboot:
            summary_lines.append("Reboot system for changes to take effect.")
        summary = "\n".join(summary_lines)
        self.log("=" * 48)
        self.log(summary)
        if needs_reboot:
            self.log("⚠️ Reboot system for changes to take effect.")
        self.log("=" * 48)

        # F-003 fix: marshal through the Tk thread — was a direct
        # cross-thread Canvas call (see _stop_indeterminate docstring).
        self._stop_indeterminate()
        self._set_progress(1, 1)
        self.set_status(summary_lines[0] + (" — Reboot needed" if needs_reboot else ""))

        with self._busy_lock:
            self._busy = False
        self._worker_thread = None
        self._cancel_requested = False
        self._set_buttons_enabled(True)

        def _done_popup():
            if cancelled:
                messagebox.showinfo("Stopped", summary)
            else:
                messagebox.showinfo("Done", summary)
        self.root.after(0, _done_popup)

        # audit fix (A3-M1): the "Clean Complete — freed X" toast fired for
        # Repair and Tweak/Undo runs too (with a misleading "Freed 0 Bytes"
        # copy). Scope it to Clean-tab run mode, where freed-space is real.
        if not cancelled and tab_name == "Clean" and mode == "run":
            try:
                # audit minor 3: pass the failure count so the toast copy is
                # truthful (only claims a clean completion when nothing failed)
                threading.Thread(target=notify_clean_complete,
                                 args=(total_bytes, completed, failed), daemon=True).start()
            except Exception:
                pass


def launch():
    root = tk.Tk()
    Application(root)
    root.mainloop()
