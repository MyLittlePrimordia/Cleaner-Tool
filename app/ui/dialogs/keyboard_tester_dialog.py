"""KeyboardTesterDialog — extracted verbatim from app/gui.py (H12, batch 2).

One dialog per module, mirroring app/ui/widgets. Depends only on
app.ui below it, plus the app.utils helpers it already used; it
knows nothing
about any other dialog except where noted.
"""
from __future__ import annotations

import tkinter as tk
from tkinter import ttk, messagebox
from app.ui.base import ThemedModal
from app.ui.theme import COLORS, F, TAB_ACCENTS, _hex_lerp
from app.ui.widgets import AnimatedButton


class KeyboardTesterDialog(ThemedModal):
    """Keyboard Tester: QWERTY canvas that flashes each key while held and
    keeps a dim mark after release so gaps show at a glance. Simultaneous
    count gives a rough ghosting check. Pure Tk key events, zero Win32."""

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

    def __init__(self, parent, app):
        self.app = app
        self._key_shapes = {}
        self._held = set()
        self._tested = set()
        self._press_time = {}
        self._stuck_after = None
        self._STUCK_S = 10.0
        super().__init__(parent, title="Keyboard Tester", accent=TAB_ACCENTS["Clean"])
        body = self.body
        self._status_lbl = tk.Label(body, text="Press any key.",
                                    font=(F, 10, "bold"), bg=COLORS["bg"],
                                    fg=COLORS["subtext"], anchor="w")
        self._status_lbl.pack(fill="x", pady=(0, 6))
        cv_w = int(15 * self._UNIT_W)
        cv_h = len(self._ROWS) * (self._KEY_H + self._KEY_GAP) + self._KEY_GAP
        self._cv = tk.Canvas(body, width=cv_w, height=cv_h, bg=COLORS["bg"],
                             bd=0, highlightthickness=0)
        self._cv.pack(pady=(0, 6))
        self._draw_layout()
        stats = tk.Frame(body, bg=COLORS["bg"])
        stats.pack(fill="x", pady=(4, 0))
        for c in range(3):
            stats.grid_columnconfigure(c, weight=1, uniform="kbstats")
        self._stat_held, _ = self._stat_block(stats, 0, "KEYS HELD")
        self._stat_tested, _ = self._stat_block(stats, 1, "TESTED")
        reset_cell = self._stat_cell(stats, 2)
        _reset = AnimatedButton(reset_cell, text="Reset",
                                command=self._reset_tested,
                                bg=COLORS["surface"], fg=COLORS["text"],
                                font=(F, 8, "bold"), padx=10, pady=3)
        _reset.pack()
        tk.Label(body, text="Windows grabs Win/PrintScreen/media keys before we see them.",
                 font=(F, 8), bg=COLORS["bg"], fg=COLORS["subtext"],
                 wraplength=640, justify="left").pack(fill="x", pady=(8, 0))
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
                tk.Label(cell, text=title, font=(F, 8), bg=COLORS["bg"],
                         fg=COLORS["subtext"]).pack()
            val = tk.Label(cell, text="0", font=(F, 11, "bold"),
                           bg=COLORS["bg"], fg=COLORS["text"])
            val.pack()
            return val, cell
        except Exception:
            return None, None

    def _stat_cell(self, parent, col):
        """Button-only cell (no value label — the old empty-title block
        still created its '0' label, floating a stray 0 over Reset)."""
        try:
            cell = tk.Frame(parent, bg=COLORS["bg"])
            cell.grid(row=0, column=col, sticky="nsew", padx=12)
            return cell
        except Exception:
            return parent

    def _draw_layout(self):
        y = self._KEY_GAP
        for row in self._ROWS:
            x = self._KEY_GAP
            for label, keysym, units in row:
                w = units * self._UNIT_W - self._KEY_GAP
                try:
                    rect = self._cv.create_rectangle(
                        x, y, x + w, y + self._KEY_H,
                        fill=COLORS["surface"], outline=COLORS["hairline"])
                    text = self._cv.create_text(
                        x + w / 2, y + self._KEY_H / 2, text=label,
                        font=(F, 9, "bold"), fill=COLORS["subtext"])
                    self._key_shapes[keysym] = (rect, text)
                except Exception:
                    pass
                x += w + self._KEY_GAP
            y += self._KEY_H + self._KEY_GAP

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
                    self._status_lbl.config(
                        text=f"Stuck key? {names} — lift your finger or tap it again.",
                        fg=COLORS["accent_red"])
                except Exception:
                    pass
            else:
                try:
                    self._status_lbl.config(text="Press any key.",
                                            fg=COLORS["subtext"])
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
