"""SegmentedTabs: sliding one-pill tab switcher for dialogs.

H4. Extracted verbatim from app/gui.py (blueprint B1.2).
"""
from __future__ import annotations

import time
import tkinter as tk

from app.ui.theme import (
    COLORS, TYPE, _hex_lerp, _measure_font, _round_rect_points
)

class SegmentedTabs(tk.Canvas):
    """One-pill tab switcher for dialogs (Drive Toolkit + Process Manager).

    Same visual language as the main Clean/Repair/Tweak pill
    (Application._build_tab_switch): one bg_alt track, one accent thumb
    that slides + recolors, active label black/bold, inactive subtext.
    Items are created ONCE and only moved/recolored (no flicker), motion
    is time-based ease-out cubic at ~60fps, 160ms like the main switcher.

    API: SegmentedTabs(parent, labels=[...], keys=[...]|None,
    initial=0|key, accent=COLORS[...], command=fn(key), seg_w=None)
    .select(key_or_index), .selected_key, .selected_index.
    Never raises out of __init__ (dialog must always open).
    """

    ANIM_MS = 160

    def __init__(self, parent, labels, keys=None, initial=0,
                 accent=None, command=None, seg_w=None, height=36,
                 font=None, **kw):
        try:
            bg = parent["bg"] if isinstance(
                parent, (tk.Frame, tk.Canvas)) else COLORS["bg"]
        except Exception:
            bg = COLORS["bg"]
        self._labels = list(labels or ["One"])
        n = len(self._labels)
        self._keys = list(keys) if keys and len(keys) == n else list(
            range(n))
        self._accent = accent or COLORS["accent_blue"]
        self._command = command
        self._font = font or TYPE["label_medium"]
        self._font_off = (self._font[0], self._font[1]) \
            if len(self._font) >= 2 else TYPE["caption"]
        # Segment width: caller override, else measured max label + padding.
        # Measured with the shared cached font (no throwaway widgets).
        if seg_w is not None:
            self._seg_want = max(72, int(seg_w))
        else:
            try:
                fnt = _measure_font(parent.winfo_toplevel(), self._font)
                widest = max((fnt.measure(t) for t in self._labels),
                             default=60)
                self._seg_want = max(96, min(200, int(widest) + 44))
            except Exception:
                self._seg_want = 148
        # BUG-034: `_seg` is the EFFECTIVE width and may be smaller than
        # `_seg_want` when the parent cannot give us the label-derived width.
        # `want` is kept separately so the control can grow back when there is
        # room again. See _fit().
        self._seg = self._seg_want
        # NOTE: _w/_h are tkinter's reserved widget-path attribute names
        # (see AnimatedButton._measure) — pixel size lives under _bw/_bh.
        self._bw = self._seg * n
        self._bh = int(height)
        super().__init__(parent, width=self._bw, height=self._bh,
                         highlightthickness=0, bd=0, bg=bg, **kw)
        try:
            if isinstance(initial, str) and initial in self._keys:
                idx = self._keys.index(initial)
            else:
                idx = max(0, min(n - 1, int(initial)))
        except Exception:
            idx = 0
        self._index = idx
        self._pos = float(idx)
        self._anim = None
        self._anim_after = None
        # persistent items — created once, never deleted (flicker fix)
        try:
            self._track = self.create_polygon(
                _round_rect_points(0, 0, self._bw, self._bh,
                                   self._bh // 2),
                smooth=True, fill=COLORS["surface"], outline="")  # tonal track, no stroke
            x = self._thumb_x(self._pos)
            self._thumb = self.create_polygon(
                _round_rect_points(x, 3, x + self._seg - 6,
                                   self._bh - 3, (self._bh - 6) // 2),
                smooth=True, fill=_hex_lerp(COLORS["surface"], self._accent, 0.40),
                outline="")   # M3 selected segment: tonal container
            self._texts = []
            for i, lab in enumerate(self._labels):
                cx = self._seg * i + self._seg / 2
                self._texts.append(self.create_text(cx, self._bh // 2,
                                                    text=lab))
            self._style_labels()
        except Exception:
            self._track = self._thumb = None
            self._texts = []
        try:
            self.bind("<Button-1>", self._on_click)
            self.bind("<Motion>", self._on_motion)
            # BUG-034: fit to whatever width we are actually given. add="+"
            # so this composes with any handler already bound here.
            self.bind("<Configure>", lambda e: self._fit(e.width), add="+")
            self.bind("<Leave>",
                      lambda e: self.config(cursor="arrow"))
            self.configure(takefocus=1, highlightthickness=0)
            self.bind("<Left>", lambda e: self.select(
                max(0, self._index - 1)))
            self.bind("<Right>", lambda e: self.select(
                min(len(self._labels) - 1, self._index + 1)))
            self.bind("<Return>", lambda e: None)
            self.bind("<space>", lambda e: None)
        except Exception:
            pass

    # -- geometry -------------------------------------------------- #

    #: Below this a chip stops being readable. A caller with many segments
    #: needs fewer of them or a wider dialog, not thinner ones.
    MIN_SEG = 56

    def _fit(self, width=None):
        """Shrink the segment width so the control fits the space given.

        BUG-034 (user screenshot): __init__ derives the control's width from
        its labels (`_seg_want = max(96, min(200, widest + 44))`, so
        `_bw = _seg_want * n`) and the control is a fixed-size Canvas. When
        the parent hands it less than that, the Canvas silently CROPS its own
        items - measured in ServicesDialog at DPI scaling 2.0, where five
        chips wanted 1001 px and the canvas was 730, so 271 px of chip simply
        did not exist.

        This is genuinely hard to catch from outside: a Canvas reports an
        in-bounds `winfo_width()` while its items run past the edge, so
        walking the widget tree for overflowing children never sees it. Only
        `bbox("all")` does. Hence the fix lives here rather than in the
        dialog: the control should fit what it is given.

        `_seg` is the single source of truth for the track, the thumb, every
        label position, and click hit-testing, so re-deriving it here keeps
        all four consistent. Applied only when the effective width actually
        changes, which also stops the <Configure> feedback loop that
        reconfiguring the Canvas would otherwise create.
        """
        try:
            w = int(width if width is not None else self.winfo_width())
        except Exception:
            return
        if w <= 1:
            return
        n = max(1, len(self._labels))
        want = int(getattr(self, "_seg_want", self._seg))
        target = want if (w // n) >= want else max(self.MIN_SEG, w // n)
        if target == self._seg:
            return
        self._seg = target
        self._bw = target * n
        try:
            self.configure(width=self._bw)
        except Exception:
            pass
        if getattr(self, "_track", None) is None:
            return
        try:
            self.coords(self._track, *_round_rect_points(
                0, 0, self._bw, self._bh, self._bh // 2))
            self._place_thumb(getattr(self, "_pos", 0.0))
            for i, tid in enumerate(self._texts):
                self.coords(tid, target * i + target / 2, self._bh // 2)
            self._fit_labels(target)
        except Exception:
            pass

    def _fit_labels(self, seg_w):
        """Ellipsize any label wider than its own segment.

        _fit above shrinks the SEGMENT when the control is handed less room
        than the labels want, and until now it did nothing about the TEXT -
        so with five chips in a 358px column each segment came out at 71px
        while "Not Microsoft" is 74px wide. Measured: the labels overlapped
        their neighbours and the last one ran 17px past the canvas edge,
        which the canvas silently cropped.

        Ellipsizing is the honest response at that width: the alternative is
        overlapping text, and the full label is one hover away (the dialog
        owns the tooltip). The threshold is generous, so at a width where
        the labels genuinely fit this never fires and nothing is truncated.
        """
        try:
            budget = max(8, int(seg_w) - 10)
            for i, tid in enumerate(self._texts):
                label = self._labels[i]
                if _measure_font(self.winfo_toplevel(),
                                 self._font).measure(label) <= budget:
                    self.itemconfigure(tid, text=label)
                    continue
                text = label
                while text and _measure_font(
                        self.winfo_toplevel(),
                        self._font).measure(text + "…") > budget:
                    text = text[:-1]
                self.itemconfigure(tid, text=(text + "…") if text else "…")
        except Exception:
            pass

    def _thumb_x(self, t):
        return 3 + t * self._seg + 3

    def _place_thumb(self, t, fill=None):
        try:
            x = self._thumb_x(t)
            pts = _round_rect_points(x, 3, x + self._seg - 6,
                                     self._bh - 3, (self._bh - 6) // 2)
            self.coords(self._thumb, *pts)
            if fill is not None:
                self.itemconfigure(
                    self._thumb, fill=_hex_lerp(COLORS["surface"], fill, 0.40))
        except Exception:
            pass

    def _style_labels(self):
        for i, tid in enumerate(self._texts):
            try:
                active = (i == self._index)
                self.itemconfigure(
                    tid,
                    fill=COLORS["text"] if active else COLORS["subtext"],
                    font=self._font if active else self._font_off)
            except Exception:
                pass

    # -- interaction ------------------------------------------------ #

    def _on_click(self, event):
        try:
            seg = min(len(self._labels) - 1,
                      max(0, int(event.x // self._seg)))
            self.select(seg)
        except Exception:
            pass

    def _on_motion(self, event):
        try:
            seg = min(len(self._labels) - 1,
                      max(0, int(event.x // self._seg)))
            self.config(cursor="hand2" if seg != self._index else "arrow")
        except Exception:
            pass

    @property
    def selected_key(self):
        try:
            return self._keys[self._index]
        except Exception:
            return None

    @property
    def selected_index(self):
        return getattr(self, "_index", 0)

    def select(self, key_or_index, _fire=True):
        """Select by key or index. Fires command(key) unless _fire=False
        (used internally to sync without re-entering the switch)."""
        try:
            if key_or_index in self._keys and not isinstance(
                    key_or_index, int):
                target = self._keys.index(key_or_index)
            else:
                target = max(0, min(len(self._labels) - 1,
                                    int(key_or_index)))
        except Exception:
            return
        if target == getattr(self, "_index", -1) and self._anim is None:
            return
        self._index = target
        try:
            self._style_labels()
        except Exception:
            pass
        # cancel in-flight animation so fast re-clicks retarget cleanly
        try:
            if self._anim_after is not None:
                self.after_cancel(self._anim_after)
                self._anim_after = None
        except Exception:
            pass
        try:
            self._anim = {"from": self._pos, "to": float(target),
                          "start": time.monotonic()}
            self._tick()
        except Exception:
            try:
                self._pos = float(target)
                self._place_thumb(self._pos, self._accent)
            except Exception:
                pass
        if _fire and self._command is not None:
            try:
                self._command(self._keys[target])
            except Exception:
                pass

    def set_accent(self, accent):
        try:
            self._accent = accent
            self._place_thumb(self._pos, accent)
        except Exception:
            pass

    def _tick(self):
        try:
            if not self.winfo_exists():
                return
        except Exception:
            return
        anim = getattr(self, "_anim", None)
        if anim is None:
            return
        try:
            p = (time.monotonic() - anim["start"]) * 1000.0 / self.ANIM_MS
        except Exception:
            p = 1.0
        if p >= 1.0:
            self._pos = anim["to"]
            self._place_thumb(self._pos, self._accent)
            self._anim = None
            self._anim_after = None
            return
        eased = 1 - (1 - p) ** 3
        t = anim["from"] + (anim["to"] - anim["from"]) * eased
        self._pos = t
        self._place_thumb(t, self._accent)
        try:
            self._anim_after = self.after(16, self._tick)
        except Exception:
            self._anim_after = None
