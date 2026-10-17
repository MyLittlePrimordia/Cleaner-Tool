"""KeyboardTesterDialog — extracted verbatim from app/gui.py (H12, batch 2).

One dialog per module, mirroring app/ui/widgets. Depends only on
app.ui below it, plus the app.utils helpers it already used; it
knows nothing
about any other dialog except where noted.
"""
from __future__ import annotations

import tkinter as tk
from tkinter import ttk, messagebox
from app.ui.dialogs._tool_page import ToolPage
from app.ui.theme import (COLORS, F, TAB_ACCENTS, _hex_lerp,
                          _round_rect_points)


class KeyboardTesterDialog(ToolPage):
    """Keyboard Tester: QWERTY canvas that flashes each key while held and
    keeps a dim mark after release so gaps show at a glance. Simultaneous
    count gives a rough ghosting check. Pure Tk key events, zero Win32.

    REDESIGN 2026-10: on the shared ToolPage shell — the font-18 headline is
    now the Header banner, the stuck-key warning moved to the shared
    StatusLine, and Reset is a footer action. scroll=False: the key canvas is
    a fixed interactive surface that must never scroll (the click/key zones
    are absolute canvas coordinates)."""

    _ROWS = [
        [("Esc", "Escape", 1), ("F1", "F1", 1), ("F2", "F2", 1), ("F3", "F3", 1),
         ("F4", "F4", 1), ("F5", "F5", 1), ("F6", "F6", 1), ("F7", "F7", 1),
         ("F8", "F8", 1), ("F9", "F9", 1), ("F10", "F10", 1), ("F11", "F11", 1),
         ("F12", "F12", 1)],
        [("`", "grave", 1), ("1", "1", 1), ("2", "2", 1), ("3", "3", 1), ("4", "4", 1),
         ("5", "5", 1), ("6", "6", 1), ("7", "7", 1), ("8", "8", 1), ("9", "9", 1),
         ("0", "0", 1), ("-", "minus", 1), ("=", "equal", 1), ("Bksp", "BackSpace", 2)],
        [("Tab", "Tab", 1.5), ("Q", "q", 1), ("W", "w", 1), ("E", "e", 1), ("R", "r", 1),
         ("T", "t", 1), ("Y", "y", 1), ("U", "u", 1), ("I", "i", 1), ("O", "o", 1),
         ("P", "p", 1), ("[", "bracketleft", 1), ("]", "bracketright", 1), ("\\", "backslash", 1.5)],
        [("Caps", "Caps_Lock", 1.75), ("A", "a", 1), ("S", "s", 1), ("D", "d", 1),
         ("F", "f", 1), ("G", "g", 1), ("H", "h", 1), ("J", "j", 1), ("K", "k", 1),
         ("L", "l", 1), (";", "semicolon", 1), ("'", "apostrophe", 1), ("Enter", "Return", 2.25)],
        [("Shift", "Shift_L", 2.25), ("Z", "z", 1), ("X", "x", 1), ("C", "c", 1), ("V", "v", 1),
         ("B", "b", 1), ("N", "n", 1), ("M", "m", 1), (",", "comma", 1), (".", "period", 1),
         ("/", "slash", 1), ("Shift", "Shift_R", 2.75)],
        [("Ctrl", "Control_L", 1.5), ("Win", "Super_L", 1.25), ("Alt", "Alt_L", 1.25),
         ("Space", "space", 6), ("Alt", "Alt_R", 1.25), ("Ctrl", "Control_R", 1.5)],
    ]
    _KEY_H = 46
    _KEY_GAP = 4
    _UNIT_W = 48
    #: Widest row is 15 key units; 15.2 leaves the board a little air on the
    #: right. The canvas width is expressed in these units everywhere.
    _ROWS_UNITS = 15.2

    def __init__(self, parent, app):
        self.app = app
        self._key_shapes = {}
        self._held = set()
        self._tested = set()
        self._press_time = {}
        self._stuck_after = None
        self._STUCK_S = 10.0
        super().__init__(parent, title="Keyboard Tester",
                         accent=TAB_ACCENTS["Clean"],
                         scope="Escape is a test key here too — close with "
                               "the ✕ or Alt+F4.",
                         scroll=False, fit=True)
        self.banner.set_state("\u2328", TAB_ACCENTS["Clean"])
        self.banner.set_title("Does every key work?")
        self.banner.set_sub("Hold any key to light it up; keys you have "
                            "tested stay dimly marked.")
        body = self.content
        # Size the keys to the CONTENT ZONE, and keep them there.
        #
        # USER FIX 2026-10 (keyboard clipped on the right): this used to be
        # sized from body_width(0) -- the popup WINDOW width -- and the canvas
        # was packed WITHOUT fill. At the normal 998px popup that asked for a
        # 976px canvas inside a 932px content zone, so the packer clamped the
        # canvas and every row wider than 932px lost its right end: Bksp,
        # Enter and the right-hand Shift were visibly sheared off. The canvas
        # now fills the zone and rebuilds the board for the width it is
        # actually given, so no row can be cut at any window size.
        self._cv_size = (0, 0)
        _guess = self._zone_width_guess()
        self._UNIT_W = max(24.0, (_guess - self._KEY_GAP) / self._ROWS_UNITS)
        self._KEY_H = self._UNIT_W - self._KEY_GAP
        self._cv = tk.Canvas(body, width=int(_guess), height=self._cv_height(),
                             bg=COLORS["bg"], bd=0, highlightthickness=0)
        self._cv.pack(fill="x", pady=(0, 10))
        self._cv.bind("<Configure>", self._on_canvas_resize)
        self._draw_layout()
        stats = tk.Frame(body, bg=COLORS["bg"])
        stats.pack(fill="x", pady=(4, 0))
        for c in range(2):
            stats.grid_columnconfigure(c, weight=1, uniform="kbstats")
        self._stat_held, _ = self._stat_block(stats, 0, "KEYS HELD")
        self._stat_tested, _ = self._stat_block(stats, 1, "TESTED")
        # REDESIGN 2026-10: Reset is the dialog's one action, so it lives in
        # the shared footer instead of a third stat cell.
        self.add_button("Reset", self._reset_tested)
        try:
            # Escape carve-out: ThemedModal binds Escape to close, but Escape
            # is itself a test target — unbind close so it lights up instead.
            # Close via the X button.
            self._dlg.unbind("<Escape>")
            self._dlg.bind("<KeyPress>", self._on_press, add="+")
            self._dlg.bind("<KeyRelease>", self._on_release, add="+")
            self._dlg.focus_force()
        except Exception:
            pass
        self.on_close(self._stop)
        self._schedule_stuck()

    def _stat_block(self, parent, col, title):
        try:
            cell = tk.Frame(parent, bg=COLORS["bg"])
            cell.grid(row=0, column=col, sticky="nsew", padx=12)
            if title:
                tk.Label(cell, text=title, font=(F, 9), bg=COLORS["bg"],
                         fg=COLORS["subtext"]).pack()
            val = tk.Label(cell, text="0", font=(F, 11, "bold"),
                           bg=COLORS["bg"], fg=COLORS["text"])
            val.pack()
            return val, cell
        except Exception:
            return None, None

    def _stat_cell(self, parent, col):
        """Button-only cell (no value label — the old empty-title block
        still created its '0' label, floating a stray 0 over Reset).

        Retained (unused) from the pre-redesign layout: Reset moved to the
        shared footer, but the helper stays so nothing that referenced it
        breaks."""
        try:
            cell = tk.Frame(parent, bg=COLORS["bg"])
            cell.grid(row=0, column=col, sticky="nsew", padx=12)
            return cell
        except Exception:
            return parent

    def _zone_width_guess(self):
        """First guess at the canvas width, before the packer has run.

        The window width less the modal chrome (card inset + content
        padding). Only a starting value: <Configure> replaces it with the
        width the canvas is actually given, so a drifting chrome constant
        can never bring the clipping back."""
        try:
            return max(320, int(self.body_width(0)) - 66)
        except Exception:
            return 720

    def _cv_height(self):
        return int(len(self._ROWS) * self._UNIT_W) + self._KEY_GAP

    def _on_canvas_resize(self, event):
        """Rebuild the board for the width the canvas actually got.

        Guarded on width: setting the height below fires another <Configure>
        with the same width, which must not re-enter this."""
        w = int(getattr(event, "width", 0) or 0)
        if w <= 20 or w == self._cv_size[0]:
            return
        self._cv_size = (w, int(getattr(event, "height", 0) or 0))
        self._UNIT_W = max(24.0, (w - self._KEY_GAP) / self._ROWS_UNITS)
        self._KEY_H = self._UNIT_W - self._KEY_GAP
        try:
            self._cv.configure(height=self._cv_height())
        except Exception:
            pass
        self._draw_layout()

    def _repaint_states(self):
        """Re-apply held/tested colours after the board is redrawn."""
        for ks in list(self._tested):
            self._paint_key(ks, "held" if ks in self._held else "tested")
        for ks in list(self._held):
            self._paint_key(ks, "held")

    def _draw_layout(self):
        try:
            self._cv.delete("all")
        except Exception:
            pass
        self._key_shapes = {}
        y = self._KEY_GAP
        for row in self._ROWS:
            x = self._KEY_GAP
            for label, keysym, units in row:
                w = units * self._UNIT_W - self._KEY_GAP
                try:
                    rect = self._cv.create_polygon(
                        _round_rect_points(x, y, x + w, y + self._KEY_H, 10),
                        smooth=True, fill=COLORS["surface"], outline="")
                    text = self._cv.create_text(
                        x + w / 2, y + self._KEY_H / 2, text=label,
                        font=(F, 11 if self._UNIT_W >= 56 else 9, "bold"),
                        fill=COLORS["subtext"])
                    self._key_shapes[keysym] = (rect, text)
                except Exception:
                    pass
                x += w + self._KEY_GAP
            y += self._KEY_H + self._KEY_GAP
        self._repaint_states()

    def _paint_key(self, keysym, state):
        shapes = self._key_shapes.get(keysym)
        if not shapes:
            return
        rect, _text = shapes
        color = {
            "held": COLORS["accent_green"],
            "stuck": COLORS["accent_red"],
            "tested": _hex_lerp(COLORS["accent_green"], COLORS["surface"], 0.75),
            "idle": COLORS["surface"],
        }.get(state, COLORS["surface"])
        try:
            self._cv.itemconfig(rect, fill=color)
        except Exception:
            pass

    def _schedule_stuck(self):
        try:
            if self._stuck_after is not None:
                try:
                    self._dlg.after_cancel(self._stuck_after)
                except Exception:
                    pass
            self._stuck_after = self._dlg.after(500, self._check_stuck)
        except Exception:
            self._stuck_after = None

    def _check_stuck(self):
        """Flag keys held longer than _STUCK_S as stuck (red + status).

        Tk thread only via after(); auto-repeat KeyPress events must not
        reset the clock — only the first press counts."""
        self._stuck_after = None
        try:
            import time as _time
            now = _time.monotonic()
            stuck = []
            for ks in list(self._held):
                try:
                    t0 = float(self._press_time.get(ks, now))
                except Exception:
                    t0 = now
                held_s = now - t0
                if held_s >= float(getattr(self, "_STUCK_S", 10.0)):
                    stuck.append((ks, held_s))
                    self._paint_key(ks, "stuck")
            if stuck:
                stuck.sort(key=lambda kv: kv[0])
                names = ", ".join(f"{k} ({s:.0f}s)" for k, s in stuck[:4])
                if len(stuck) > 4:
                    names += f" +{len(stuck) - 4} more"
                try:
                    self.status.stop(
                        f"Stuck key? {names} — lift your finger or tap it again.",
                        COLORS["accent_red"])
                except Exception:
                    pass
            else:
                try:
                    self.status.stop("Press any key.")
                except Exception:
                    pass
        except Exception:
            pass
        self._schedule_stuck()

    def _on_press(self, event):
        ks = getattr(event, "keysym", "")
        if ks not in self._key_shapes:
            return
        if ks not in self._held:
            self._held.add(ks)
            self._tested.add(ks)
            try:
                import time as _time
                self._press_time[ks] = _time.monotonic()
            except Exception:
                pass
            self._paint_key(ks, "held")
            self._refresh_stats()

    def _on_release(self, event):
        ks = getattr(event, "keysym", "")
        self._held.discard(ks)
        try:
            self._press_time.pop(ks, None)
        except Exception:
            pass
        self._paint_key(ks, "tested" if ks in self._tested else "idle")
        self._refresh_stats()

    def _refresh_stats(self):
        try:
            self._stat_held.config(text=str(len(self._held)))
            self._stat_tested.config(text=f"{len(self._tested)}/{len(self._key_shapes)}")
        except Exception:
            pass

    def _reset_tested(self):
        self._tested.clear()
        for ks in self._key_shapes:
            if ks not in self._held:
                self._paint_key(ks, "idle")
        self._refresh_stats()

    def _stop(self):
        try:
            if getattr(self, "_stuck_after", None) is not None:
                try:
                    self._dlg.after_cancel(self._stuck_after)
                except Exception:
                    pass
        except Exception:
            pass
        self._stuck_after = None
        try:
            self._held.clear()
            self._press_time.clear()
        except Exception:
            pass
