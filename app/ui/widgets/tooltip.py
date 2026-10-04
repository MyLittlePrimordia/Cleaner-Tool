"""Tooltip — a single shared tip window, moved here from app/ui/base.py.

It is a widget, not modal infrastructure: it has no dependency on
ThemedModal or the scrim, only on the theme. It lived in base.py because
the window chrome in app/ui/widgets/chrome.py attaches one to the
title-bar close button, and a widget importing a layer above itself is
exactly the inversion the architecture guard forbids. base.py re-exports
it, so `from app.ui.base import Tooltip` keeps working.
"""
from __future__ import annotations

import tkinter as tk

from app.ui.theme import COLORS, F


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

        # BUG-012: every Tooltip registered three brand-new Tcl commands and
        # appended a <Destroy> handler with add="+", and nothing ever removed
        # the previous Tooltip's. The Hardware Monitor's Network card rebuilds
        # its tooltip whenever the auto-picked NIC changes, and that pick is a
        # bare argmax with no hysteresis, so on any machine with two active
        # adapters (Wi-Fi + Ethernet, or Wi-Fi + a VPN) it flips whenever the
        # two totals cross -- every couple of seconds. Measured here: 200
        # alternating paints left 600 registered Tcl commands and ~21.8 KB of
        # bind script on ONE label, growing linearly and never reclaimed.
        #
        # bind() returns the Tcl funcid it registered, so remember all three
        # and retire them before installing this instance's. <Enter>/<Leave>
        # are bound without add="+", so their script text is already
        # overwritten -- only the orphaned commands need deleting.
        prev = getattr(widget, "_tooltip_funcids", None)
        if prev is not None:
            try:
                widget.unbind("<Destroy>", prev["destroy"])
            except Exception:
                pass
            for key in ("enter", "leave"):
                try:
                    widget.deletecommand(prev[key])
                except Exception:
                    pass

        enter_id = widget.bind("<Enter>", self._on_enter)
        leave_id = widget.bind("<Leave>", self._hide)
        # F8: a destroyed widget must not leave a pending timer behind --
        # Tk after-ids outlive the widget that scheduled them, so the timer
        # is cancelled here (add="+": never wipe another <Destroy> handler).
        destroy_id = widget.bind("<Destroy>", self._on_destroy, add="+")
        try:
            # Holding the strings keeps no widget alive; they are only needed
            # long enough to unbind/deletecommand the previous instance.
            widget._tooltip_funcids = {
                "enter": enter_id, "leave": leave_id, "destroy": destroy_id,
            }
        except Exception:
            pass

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
