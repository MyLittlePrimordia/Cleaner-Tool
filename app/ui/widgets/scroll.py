"""ScrollableRoundedPanel: rounded clip + wheel/drag scrollbar.

H4. Extracted verbatim from app/gui.py (blueprint B1.2).
"""
from __future__ import annotations

import tkinter as tk

from app.ui.theme import COLORS, _round_pts, F

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

    def __init__(self, parent, fill_inner=False, hug=True, **kw):
        page_bg = parent["bg"] if isinstance(parent, (tk.Frame, tk.Canvas)) else COLORS["bg"]
        super().__init__(parent, bg=page_bg, highlightthickness=0, bd=0, height=10, **kw)
        self._fill = COLORS["bg"]   # panel is page-coloured; rows are the cards
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
        # _ensure_sb_items, only moved/recolored per tick — declared here so
        # every method can safely getattr before first paint.
        self._sb_thumb = None
        # M3 overlay scrollbar: the thumb is a floating indicator that
        # fades out shortly after scrolling stops (and back in on wheel,
        # drag or hover). A permanent track+thumb read as a nested
        # scrollbar "inside" the popup - the wrong look for the dialogs
        # (user report). The track is gone entirely; only the thumb shows.
        self._sb_hide_id = None
        self._sb_vis = False
        # Last painted card height, so _sync_card can skip a repaint that
        # would be pixel-identical (see _sync_card).
        self._card_drawn = None
        # Whether the embedded frame is stretched to the VIEWPORT rather
        # than left at its requested height.
        #
        # Tk quirk this exists for: a canvas window created with
        # create_window(anchor="nw") and no explicit height keeps its
        # REQUESTED height, so the inner frame is only as tall as its
        # content - and every `expand=True` inside it then hands out
        # leftover CONTENT height, not leftover VIEWPORT height. Measured
        # with a two-pane tool: the whole body collapsed to 90px, because
        # the panes' own panels report a 10px default request.
        #
        # The fix is to stretch the inner frame to the viewport, NOT to
        # report the content's height upward: the canvas must still refuse
        # to demand its content's height, or a long list pushes the dialog's
        # footer and action bar out of view - which is the reason this
        # widget exists rather than a plain Frame.
        self._fill_inner = bool(fill_inner)
        # hug=True (default): the drawn card ends just under short content, so
        # a short list reads as a finished card rather than a half-empty box.
        # Pass hug=False only where the body is a single centred EMPTY STATE -
        # see _card_height() for why the two cases want opposite behaviour.
        self._hug = bool(hug)
        self.inner = tk.Frame(self, bg=self._fill)
        self._win = self.create_window(0, 0, window=self.inner, anchor="nw")
        self.bind("<Configure>", self._on_configure)
        self.bind("<ButtonPress-1>", self._on_press)
        self.bind("<B1-Motion>", self._on_motion)
        self.bind("<ButtonRelease-1>", self._on_release)
        # M3 overlay scrollbar: hovering the panel reveals the thumb so it
        # can be grabbed before it fades.
        self.bind("<Enter>", lambda _e: self._reveal_sb())
        # Cancel pending timers on teardown, or an armed fade fires against a
        # destroyed canvas and Tcl prints "invalid command name ...".
        self.bind("<Destroy>", self._on_destroy, add="+")

    # ---- geometry ---------------------------------------------------- #

    def view_height(self):
        return max(1, self.winfo_height() - self.INSET_Y * 2)

    def remap_children(self):
        """Force the canvas-window child (self.inner) to (re)map.

        Tk quirk (reproduced minimal, 2026-09-06): a frame embedded via
        create_window() does NOT remap when an ANCESTOR of the canvas is
        pack_forget()ten and re-pack()ed — the canvas maps again but the
        embedded window stays unmapped forever (winfo_ismapped()==0), so
        the body cache's re-shown grids rendered BLANK and every filter
        check on their rows saw nothing. Re-asserting the item's
        geometry (coords + itemconfigure) forces Tk through the geometry
        pass that remaps the embedded frame. Called by _build_body on
        every cache-hit re-show; cheap (two canvas ops, no rebuild)."""
        try:
            w = self.winfo_width()
            self.itemconfigure(self._win, width=max(10, w - self.INSET_X - self.SB_W - 2))
            self.coords(self._win, self.INSET_X, self.INSET_Y)
        except Exception:
            pass

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

    def _card_height(self):
        """How tall the painted card should be.

        DESIGN AUDIT (2026-10). This used to always be the full canvas
        height, which made a short list look like a large empty box rather
        than like a list: a 12-item preset drew a card 470px tall holding
        130px of rows, and the leftover face read as "this panel failed to
        load" rather than as breathing room.

        The fix is one polygon, not a layout change. `self.inner` is a plain
        Frame, so it has ALWAYS been only as tall as its content - the
        stretch was purely this background rectangle. So the card can hug
        its content without touching a single pack() call, which matters:
        the obvious alternative (sizing the panel to its content) would have
        meant the panel refusing expand=True, which is the one behaviour
        this widget exists to guarantee - see the class docstring and
        DetailPane's settle() notes on what propagate_height did to the
        dialogs.

        Consequence worth stating: the primary action row stays PINNED to
        the bottom of the page, so it does not move when the user switches
        between a 12-task preset and a 27-task one. That is deliberate. A
        primary button that chases its content breaks Fitts's Law - the
        muscle memory the user built on the first click is wrong on the
        second - and macOS Mail's toolbar / Material's bottom bar both hold
        their action in place regardless of list length.

        When the content overflows, or fills the viewport, the card goes
        full height: there is nothing to hug, and a full-bleed card is
        correct for a scrolling region.

        `hug` decides the rest, and it defaults to hugging. A card that ends
        just under a short top-aligned list reads as a deliberate, finished
        edge - the Repair preset is 20 tasks ending partway down, and a
        full-height card there left ~210px of blank space INSIDE a bordered
        box, which is worse than the seam it was meant to remove.

        But the same shape is wrong for an empty state. A centred "Nothing
        picked yet" in a short card, with more panel visible below it, reads
        as two disconnected things and as "more is coming" when nothing is.
        Those bodies pass hug=False, and that is the whole distinction:

          A GROWS.  PC Health Report, File Lock and Duplicates all show a
                    centred empty state first, and every one of them is
                    replaced by a real list the moment the user acts -
                    expand a section, pick a file, choose a folder. The space
                    below is reserved headroom for content that is about to
                    arrive, not waste, so filling reads correctly.

          A IS FIXED. The Repair preset is 20 tasks that never change. Its
                    body never grows, so a full-height card is ~210px of
                    blank space inside a bordered box for good - which is
                    worse than the seam hugging was avoiding.

        Guessing this globally rather than per-body was tried and reverted:
        filling fixed three first-run screens and broke every short preset.
        """
        if not self._hug:
            return self.winfo_height()
        h = self.winfo_height()
        try:
            need = self.inner.winfo_reqheight() + self.INSET_Y * 2
        except Exception:
            return h
        # No hug when it would be a rounding error, or so short that the
        # corner radius degenerates.
        floor = self.RADIUS * 2
        if need >= h - 1 or need <= floor:
            return h
        return need

    def _redraw_bg(self):
        self.delete("bg")
        w, h = self.winfo_width(), self.winfo_height()
        if w > 6 and h > 6:
            self.create_polygon(_round_pts(0, 0, w, self._card_height(),
                                           self.RADIUS),
                                smooth=True, fill=self._fill, outline="",
                                tags="bg")
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
        if self._fill_inner:
            # Stretch the embedded frame to the viewport, so `expand=True`
            # inside it hands out leftover VIEWPORT height.
            #
            # AUDIT FIX (2026-10): it is the LARGER of viewport and content,
            # never the viewport alone. Forcing the bare viewport height made
            # `fill_inner` panels CLIP any content taller than the panel - the
            # content frame was squeezed to the viewport, its children were
            # cut at the panel edge, and the scrollbar had nothing to scroll
            # to. Taking the max keeps the stretch-to-viewport behaviour for
            # short content and turns overflow into honest scrolling.
            self.itemconfigure(
                self._win,
                height=max(1, max(self._view_h, self._content_h)))
        if self._content_h <= self._view_h:
            self._offset = 0
        self._apply()
        self._sync_card()

    def _sync_card(self):
        """Repaint the card only when its height actually changed.

        refresh_scroll runs once per catalog build slice (Install streams
        ~950 widgets in idle chunks) and once per scroll settle. delete() +
        create_polygon() on every one of those is exactly the churn this
        module already removed from the scrollbar path (see _apply's note),
        and the card only MOVES when the content crosses a row boundary.
        One int compare is the whole cost when nothing changed.
        """
        want = self._card_height()
        if want == self._card_drawn:
            return
        self._card_drawn = want
        self._redraw_bg()

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
        # M3 overlay scrollbar: a slim floating thumb on a 3.81:1 grey
        # (muted, the same weight the app uses for de-emphasised
        # foregrounds) - clearly visible while it is up, quiet enough to
        # disappear. There is deliberately NO track: a permanent channel
        # read as a scrollbar nested "inside" the popup, which is not the
        # Material look. _reveal_sb shows it and schedules the fade.
        self.itemconfigure(self._sb_thumb, fill=COLORS["muted"])
        self.coords(self._sb_thumb, *_round_pts(cx - 4, ty, cx + 4, ty + thumb_h, 4))
        self._reveal_sb()

    def _reveal_sb(self):
        """Show the floating thumb and (re)arm the idle fade.

        Called on every scroll tick, on the panel's <Enter>, and after a
        geometry settle, so the indicator is up exactly while the user is
        looking at it and fades ~1.2s after they stop."""
        if not self.scroll_enabled:
            return
        try:
            self.itemconfigure(self._sb_thumb, state="normal")
        except Exception:
            return
        self._sb_vis = True
        if self._sb_hide_id is not None:
            try:
                self.after_cancel(self._sb_hide_id)
            except Exception:
                pass
        try:
            self._sb_hide_id = self.after(1200, self._conceal_sb)
        except Exception:
            self._sb_hide_id = None

    def _conceal_sb(self):
        self._sb_hide_id = None
        self._sb_vis = False
        try:
            self.itemconfigure(self._sb_thumb, state="hidden")
        except Exception:
            pass

    def _on_destroy(self, e):
        """Drop pending timers when the panel goes away.

        An armed `after` that fires against a destroyed canvas makes Tcl
        print `invalid command name "..._sb_conceal"` on teardown, so both
        the scrollbar fade and the existing coalesced-scroll/resize timers
        are cancelled here."""
        if getattr(e, "widget", None) is not self:
            return
        for attr in ("_sb_hide_id", "_scroll_flush_id", "_resize_after"):
            aid = getattr(self, attr, None)
            if aid is None:
                continue
            try:
                self.after_cancel(aid)
            except Exception:
                pass
            setattr(self, attr, None)

    def _ensure_sb_items(self):
        """Create the persistent scrollbar thumb on first use (or recreate
        if the canvas was cleared). find_withtag is the liveness check —
        an item id of a deleted item is falsy-safe here, but an int id
        that was valid at creation can be stale after a canvas-level
        delete, so verify it still resolves."""
        thumb_ok = False
        try:
            if self._sb_thumb is not None:
                thumb_ok = bool(self.find_withtag(self._sb_thumb))
        except Exception:
            pass
        if not thumb_ok:
            self._sb_thumb = self.create_polygon(_round_pts(0, 0, 0, 0, 4), smooth=True,
                                                  fill=COLORS["muted"],
                                                  outline="", state="hidden", tags="sb")
        # keep it above the content window & bg at all times
        self.tag_raise(self._sb_thumb)

    def _hide_sb(self):
        if self._sb_hide_id is not None:
            try:
                self.after_cancel(self._sb_hide_id)
            except Exception:
                pass
            self._sb_hide_id = None
        self._sb_vis = False
        try:
            self.itemconfigure(self._sb_thumb, state="hidden")
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
