"""RoundedCard: the ONE card surface every panel, card and list row is drawn
with.

Why this exists
---------------
Before this, the app had two shape languages. Pills, buttons, the tab
switcher, StatTile and ScrollableRoundedPanel were all smoothly-rounded
polygons; but every actual CARD - the preset cards on the task tabs, every
Tools card, every ToolRow in every popup - was a square `tk.Frame` with a
1px `hairline` parent simulating a border. Put a Tools popup and a task tab
side by side and the difference is obvious: rounded numbers floating over
squares.

That inconsistency is most of why the app read as "a Tkinter app with dark
colours" rather than as a designed product. Rounded corners are not
decoration; they are the single strongest signal of a modern UI, and mixing
them with hard rectangles cancels the effect out.

Why it is a Canvas and not a Frame
----------------------------------
Tk has no border-radius on any widget. The options were:

  * draw the rounded face on a Canvas and put the content in a frame
    embedded via create_window - what this does;
  * fake it with four corner wedges - four extra widgets per card, and it
    breaks the moment the fill changes on hover;
  * leave the corners square.

`create_window` is the approach ScrollableRoundedPanel already uses for its
scrolling body, so this is not a new mechanism in this codebase - it is the
same one, specialised to a fixed-height card.

Costs, stated honestly
----------------------
Two things a plain Frame does for free and this does not, both handled:

  * Height must be REQUESTED. A Canvas asks for ~nothing, so a card whose
    content is taller than its request would clip. `request_height()` is
    therefore measured from the content frame and fed back to the canvas on
    every <Configure> of the body (see _sync).
  * Hover/selection repaints are canvas deletes + one polygon. That is the
    same trade AnimatedButton and the scrollbar already make, and it is why
    the repaint is skipped entirely when the colour has not changed.

Depends only on app.ui.theme, like every other widget in this package.
"""
from __future__ import annotations

import math
import tkinter as tk

from app.ui.theme import COLORS, SHAPE_M, _round_rect_points

#: One radius for the whole app. StatTile uses 10 and is the closest thing
#: this already had to a "card", so the card radius matches it rather than
#: inventing a second number - equal radii on adjacent surfaces is what makes
#: two panels read as siblings.
RADIUS = SHAPE_M   # M3 "medium" shape: 12px


class RoundedCard(tk.Canvas):
    """A rounded, hairline-bordered surface that hosts one content frame.

        card = RoundedCard(parent, fill=COLORS["bg_alt"])
        card.body.pack(fill="both", expand=True, padx=12, pady=6)
        card.set_fill(COLORS["surface_hover"])   # hover / selection

    `body` is the frame callers put their content in, exactly like the `inner`
    it replaces - so a call site swaps the widget type and the line that
    builds its children, and nothing else.
    """

    def __init__(self, parent, *, fill=None, radius=RADIUS, pad=1,
                 bg=None, min_height=1, **kw):
        page_bg = bg
        if page_bg is None:
            page_bg = parent["bg"] if isinstance(
                parent, (tk.Frame, tk.Canvas)) else COLORS["bg"]
        super().__init__(parent, highlightthickness=0, bd=0, bg=page_bg,
                         height=min_height, **kw)
        self._radius = int(radius)
        # `pad` is the hairline's inset: the outline is stroked on the
        # polygon edge, so the body is inset by 1px to sit inside it and
        # never paint over the border.
        self._pad = int(pad)
        # The body Frame is square. Inset it by the depth of the corner arc
        # (r * (1 - 1/sqrt2) ~= 0.29r) so its corners sit INSIDE the rounded
        # face instead of painting square corners over it.
        self._inset = self._pad + int(math.ceil(int(radius) * 0.29))
        self._min_h = int(min_height)
        self._fill = fill or COLORS["bg_alt"]
        # M3 filled card: tonal fill, no stroke. Pass set_outline() for
        # an outlined card (outline-variant).
        self._outline = ""
        self._drawn = None

        self.body = tk.Frame(self, bg=self._fill, bd=0)
        self._win = self.create_window(self._inset, self._inset, window=self.body,
                                       anchor="nw")
        # The body growing must grow the card, or long content clips. Tk gives
        # no "size me to my child" for a Canvas, so the body's own <Configure>
        # drives the card's request height.
        self.body.bind("<Configure>", self._on_body_configure, add="+")
        self.bind("<Configure>", self._on_configure, add="+")
        self.after_idle(self._sync)

    # ---- geometry ---------------------------------------------------- #

    def _on_body_configure(self, _e=None):
        self._sync()

    def _on_configure(self, _e=None):
        self._sync()

    def _sync(self):
        """Size the card to its content and its body to the card, then paint.

        Both directions are needed and neither is optional:
          * the CARD must track the BODY (a Canvas otherwise asks for almost
            nothing and the content is clipped);
          * the BODY must track the CARD (otherwise a card widened by its
            grid leaves unpainted canvas to its right, which on a rounded
            shape shows as a flat bite out of the corner radius).

        The height is set in BOTH directions. An earlier grow-only version
        looked safe and was not: the task-summary cells are created with a
        narrow provisional wraplength, then _regrid widens it, so the
        content SHRINKS after the card has already grown - and a grow-only
        card can never come back down. The result was 47px rows locked in
        from a two-line wrap, which pushed Tweak's 27-task preset into a
        scrollbar. Setting it exactly converges instead: the height is
        derived from the body, not from the canvas, so one pass is enough and
        the <Configure> it triggers is a no-op.

        `min_height` is the floor, so a caller can keep a card from
        collapsing to nothing on a transiently-empty body.
        """
        try:
            bw = max(1, self.winfo_width())
            bh = max(1, self.winfo_height())
            want_h = max(self._min_h,
                         self.body.winfo_reqheight() + self._inset * 2)
            if bh != want_h:
                self.configure(height=want_h)
                bh = want_h
            self.itemconfigure(self._win,
                               width=max(1, bw - self._inset * 2),
                               height=max(1, bh - self._inset * 2))
            self.coords(self._win, self._inset, self._inset)
            self._paint(bw, bh)
        except Exception:
            pass

    # ---- paint -------------------------------------------------------- #

    def _paint(self, w=None, h=None):
        try:
            w = w or self.winfo_width()
            h = h or self.winfo_height()
            if w < 4 or h < 4:
                return
            # Skip a repaint that would be pixel-identical. Hover fires on
            # every row of a list, and this widget is per-row.
            key = (w, h, self._fill, self._outline,
                   getattr(self, "_outline_w", 1))
            if key == self._drawn:
                return
            self._drawn = key
            self.delete("card")
            self.create_polygon(
                _round_rect_points(1, 1, w - 2, h - 2, self._radius),
                smooth=True, fill=self._fill, outline=self._outline,
                width=getattr(self, "_outline_w", 1), tags="card")
            # Keep the body above the face. tag_lower is the cheap way and
            # does not disturb any item a caller added to the canvas.
            self.tag_lower("card")
        except Exception:
            pass

    # ---- api ---------------------------------------------------------- #

    def set_fill(self, color):
        """Repaint the face (and the body's own background) in `color`.

        The body background has to move with the face or the card shows a
        two-tone rectangle. This is why callers pass colours here instead of
        setting `bg` on the content themselves.
        """
        try:
            if color == self._fill:
                return
            self._fill = color
            self.body.config(bg=color)
            self._drawn = None
            self._paint()
        except Exception:
            pass

    def fill(self):
        return self._fill

    def set_outline(self, color):
        """Turn the card into an M3 *outlined* card (or back with "")."""
        try:
            if color == self._outline:
                return
            self._outline = color or ""
            self._drawn = None
            self._paint()
        except Exception:
            pass

    def restyle_body(self, **kw):
        """Forward a bg change on the content frame (used when a caller
        re-tints several children at once, e.g. ToolRow's hover tint)."""
        try:
            self.body.config(**kw)
        except Exception:
            pass

    def _descendants(self):
        """Every widget inside the content frame, recursively.

        A rounded face is painted ON the canvas, so any descendant left at the
        old background shows through as a hard-edged rectangle inside the
        curve - most visible at the corners, which is exactly where the eye
        goes. Callers re-tint through this rather than reaching into
        winfo_children() twice, which is what the old square-frame code did
        and which silently missed anything nested three deep.
        """
        out = []
        stack = [self.body]
        while stack:
            w = stack.pop()
            out.append(w)
            try:
                stack.extend(w.winfo_children())
            except Exception:
                pass
        return out
