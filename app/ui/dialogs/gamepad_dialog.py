"""GamepadDialog — extracted verbatim from app/gui.py (H12, batch 2).

One dialog per module, mirroring app/ui/widgets. Depends only on
app.ui below it, plus the app.utils helpers it already used; it
knows nothing
about any other dialog except where noted.
"""
from __future__ import annotations

import tkinter as tk
from tkinter import ttk, messagebox
from app.ui.base import ThemedModal, Tooltip
from app.ui.hardware.mmdev import _PadReportStats
from app.ui.hardware.xinput import _XINPUT_BUTTONS, _XINPUT_RUMBLE_MS, _XINPUT_STICK_DEADZONE, _xinput_rumble, _xinput_state_fn
from app.ui.theme import COLORS, F, TAB_ACCENTS
from app.ui.widgets import AnimatedButton


class GamepadDialog(ThemedModal):
    """Gamepad Tester (user-requested Tools feature): live view of an
    XInput controller — buttons, triggers, both sticks. Auto follows
    the first live pad; the picker pins slots 1..4 for multi-pad rigs.

    Strictly read-only (GetState polls only; rumble/remap APIs are never
    bound). Polls on the Tk thread via after() — no worker threads, so no
    threading rules to break. Fits the shared 800x600 card."""

    _TICK_MS = 60

    def __init__(self, parent, app):
        self.app = app
        self._tick_after = None
        self._pad_btns = {}
        self._stats = _PadReportStats()
        self._slot = -1
        self._slot_want = -1  # -1 = Auto (first live pad), else 0..3 manual
        self._slot_var = None
        self._rest = None        # drift-calibration center (lx,ly,rx,ry)
        self._rest_samples = []  # gathering buffer (hands off!)
        self._weak_var = None
        self._strong_var = None
        self._rumble_after = None
        # UI fix: pad artwork read too small, and the LT/RT bars sat too
        # thin/close under their %-text. Bumped the artwork's subsample
        # from /4 (300px) to /3 (400px) — every overlay coordinate below
        # (buttons, sticks, trigger bars) was measured at the old /4 scale
        # and is multiplied by _SCALE at draw time so nothing drifts out
        # of alignment with the bigger image.
        self._SCALE = 4.0 / 3.0
        super().__init__(parent, title="Gamepad Tester",
                         accent=TAB_ACCENTS["Clean"])
        body = self.body
        self._status_lbl = tk.Label(body, text="Looking for a controller…",
                                    font=(F, 10, "bold"), bg=COLORS["bg"],
                                    fg=COLORS["subtext"], anchor="w")
        self._status_lbl.pack(fill="x", pady=(0, 2))
        # Slot picker: XInput has 4 slots. Auto = first live pad (old
        # behavior); 1..4 pins polling + rumble to that slot so
        # multi-controller setups are testable. DirectInput-only pads
        # stay invisible — labeled honestly below.
        slot_row = tk.Frame(body, bg=COLORS["bg"])
        slot_row.pack(fill="x", pady=(0, 2))
        tk.Label(slot_row, text="Controller:", font=(F, 9, "bold"),
                 bg=COLORS["bg"], fg=COLORS["text"]).pack(side="left")
        try:
            import tkinter.ttk as _ttk
            self._slot_var = tk.StringVar(value="Auto")
            _slot_combo = _ttk.Combobox(slot_row, textvariable=self._slot_var,
                                        values=["Auto", "1", "2", "3", "4"],
                                        state="readonly", width=8, font=(F, 9))
            _slot_combo.pack(side="left", padx=(6, 0))
            _slot_combo.bind("<<ComboboxSelected>>", self._on_slot_pick)
        except Exception:
            self._slot_var = None
        tk.Label(slot_row, text="XInput only — DirectInput pads won't show.",
                 font=(F, 8), bg=COLORS["bg"],
                 fg=COLORS["subtext"]).pack(side="left", padx=(10, 0))
        # Center piece: the pad artwork with horizontal trigger bars
        # drawn right above the shoulders (LT left, RT right). Artwork is
        # the white-outline Xbox stock (Noun Project, CC BY — credited in
        # code) at app/assets/gamepad_outline.png, 1200x1200, shown
        # subsampled /3 (400px). Button zones were measured off the
        # artwork's pixel clusters at /4 and scaled up by _SCALE.
        self._trig_views = []  # (fill_id, track_tuple, pct_item, canvas)
        _pad_px = int(300 * self._SCALE)
        self._pad_cv = tk.Canvas(body, width=_pad_px, height=_pad_px,
                                 bg=COLORS["bg"], bd=0, highlightthickness=0)
        self._pad_cv.pack(pady=(0, 2))
        self._overlay_triggers()
        self._pad_btns = {}   # name -> (shape_id, None); paint floods green
        self._stick_l = None
        self._stick_r = None
        # Re-zero sits in its own row directly above the stat strip,
        # centered over the DRIFT column (it calibrates drift, so it
        # reads as that column's control) rather than packed to the
        # dialog's outer right edge, which crowded the rounded corner
        # and read as clipped (audit fix). It uses its own 3-column
        # uniform group so it can't force the stat row's columns wider
        # the way sharing DRIFT's cell did previously.
        rez_row = tk.Frame(body, bg=COLORS["bg"])
        rez_row.pack(fill="x", pady=(4, 0))
        for c in range(3):
            rez_row.grid_columnconfigure(c, weight=1, uniform="padstats_rez")
        _rez = AnimatedButton(rez_row, text="Re-zero",
                              command=self._calibrate,
                              bg=COLORS["surface"], fg=COLORS["text"],
                              font=(F, 8, "bold"), padx=10, pady=3)
        _rez.grid(row=0, column=2)
        Tooltip(_rez, "Learns the sticks' rest position to measure drift — "
                      "take your thumbs off first.")
        # stats strip: three evenly spaced blocks
        stats = tk.Frame(body, bg=COLORS["bg"])
        stats.pack(fill="x", pady=(2, 0))
        for c in range(3):
            stats.grid_columnconfigure(c, weight=1, uniform="padstats")
        self._stat_rate_val, _ = self._stat_block(stats, 0, "POLLING")
        self._stat_ms_val, _ = self._stat_block(stats, 1, "INTERVAL")
        self._stat_drift_val, _drift_cell = self._stat_block(stats, 2, "DRIFT")
        # rumble row: themed sliders + test share one line
        rumble = tk.Frame(body, bg=COLORS["bg"])
        rumble.pack(fill="x", pady=(6, 0))
        tk.Label(rumble, text="Rumble", font=(F, 9, "bold"),
                 bg=COLORS["bg"], fg=COLORS["text"],
                 anchor="w").pack(side="left", padx=(0, 8))
        self._weak_var = tk.DoubleVar(value=0.5)
        self._strong_var = tk.DoubleVar(value=0.5)
        self._sliders = []
        self._slider_row(rumble, "Weak", self._weak_var)
        self._slider_row(rumble, "Strong", self._strong_var)
        self._rumble_btn = AnimatedButton(
            rumble, text="Test Rumble", command=self._test_rumble,
            bg=COLORS["surface"], fg=COLORS["text"],
            font=(F, 9, "bold"), padx=18, pady=6)
        self._rumble_btn.pack(side="left", padx=(12, 0))
        self._rumble_btn.set_enabled(False)
        self._pad_img = None
        try:
            self._load_pad_artwork()
        except Exception:
            self._pad_img = None
        if self._pad_img is None:
            try:
                self._status_lbl.config(
                    text="Controller artwork missing — metrics below still work.")
            except Exception:
                pass
        self.on_close(self._stop_all)
        self._tick()

    def _stat_block(self, parent, col, title):
        """One airy stat cell: small title + big value. Returns (value, cell)."""
        try:
            cell = tk.Frame(parent, bg=COLORS["bg"])
            cell.grid(row=0, column=col, sticky="nsew", padx=12)
            tk.Label(cell, text=title, font=(F, 8), bg=COLORS["bg"],
                     fg=COLORS["subtext"]).pack()
            val = tk.Label(cell, text="—", font=(F, 11, "bold"),
                           bg=COLORS["bg"], fg=COLORS["text"])
            val.pack()
            return val, cell
        except Exception:
            return None, None

    def _load_pad_artwork(self):
        """Image + highlight overlays. Raises if the asset is missing."""
        import tkinter as _tk
        path = None
        try:
            for cand in self.app._asset_candidates("gamepad_outline.png"):
                if cand.is_file():
                    path = cand
                    break
        except Exception:
            path = None
        if path is None:
            raise FileNotFoundError("gamepad_outline.png")
        img = _tk.PhotoImage(file=str(path)).subsample(3, 3)
        self._pad_img = img  # kept: Tk unmaps otherwise
        self._pad_cv.create_image(0, 0, anchor="nw", image=img)
        S = self._SCALE
        # overlays: transparent idle, green press (coords measured at
        # orig/4, scaled up by S to line up with the bigger orig/3 image)
        for cx, cy, name in ((229, 84, "Y"), (209, 104, "X"),
                             (249, 104, "B"), (229, 124, "A")):
            self._ov_circle(cx * S, cy * S, 12 * S, name)
        for x0, y0, x1, y1, name in ((105, 129, 124, 141, "Up"),
                                     (105, 163, 124, 175, "Down"),
                                     (91, 143, 103, 161, "Left"),
                                     (126, 143, 137, 161, "Right"),
                                     (124, 96, 139, 112, "Back"),
                                     (168, 96, 183, 112, "Start")):
            self._ov_rect(x0 * S, y0 * S, x1 * S, y1 * S, name)
        self._ov_poly([(x * S, y * S) for x, y in
                       ((68, 50), (89, 50), (98, 56), (98, 61),
                        (63, 66), (49, 66), (44, 61), (46, 55))], "LB")
        self._ov_poly([(x * S, y * S) for x, y in
                       ((233, 50), (211, 50), (202, 56), (202, 61),
                        (238, 66), (252, 66), (256, 61), (254, 55))], "RB")
        self._stick_l = self._ov_nub(78 * S, 104 * S)
        self._stick_r = self._ov_nub(191 * S, 149 * S)
        # stick-click flashes use the nub rings
        self._ov_circle(78 * S, 104 * S, 11 * S, "L-Stick")
        self._ov_circle(191 * S, 149 * S, 11 * S, "R-Stick")

    def _ov_circle(self, cx, cy, r, name):
        try:
            shape = self._pad_cv.create_oval(cx - r, cy - r, cx + r, cy + r,
                                             fill="", outline="", width=0)
            self._pad_btns[name] = (shape, None)
        except Exception:
            pass

    def _ov_rect(self, x0, y0, x1, y1, name):
        try:
            shape = self._pad_cv.create_rectangle(x0, y0, x1, y1, fill="",
                                                  outline="", width=0)
            self._pad_btns[name] = (shape, None)
        except Exception:
            pass

    def _ov_poly(self, points, name):
        try:
            flat = [c for pt in points for c in pt]
            shape = self._pad_cv.create_polygon(flat, fill="", outline="",
                                                width=0)
            self._pad_btns[name] = (shape, None)
        except Exception:
            pass

    def _ov_nub(self, cx, cy):
        try:
            r = 8 * self._SCALE
            nub = self._pad_cv.create_oval(cx - r, cy - r, cx + r, cy + r,
                                           fill=COLORS["accent_green"], outline="")
            return (cx, cy, nub)
        except Exception:
            return None

    def _overlay_triggers(self):
        """Horizontal LT/RT bars above the shoulders, drawn on the pad
        canvas itself: title + % share one caption row, fill sweeps
        left-to-right with pressure.

        UI fix: the bar used to be thin (8px) and sit right under the
        title/% row (9px gap), reading as cramped. Thickened it and
        pushed it further down for a clearer break from the text above —
        same orig/4-then-scaled-by-S coordinate system as the rest of
        the pad overlays."""
        cv = self._pad_cv
        S = self._SCALE
        text_y, bar_y0, bar_y1 = 13, 28, 40
        for title, x0, x1 in (("LT", 44, 140), ("RT", 160, 256)):
            try:
                x0s, x1s = x0 * S, x1 * S
                cv.create_text(x0s, text_y * S, text=title, font=(F, 9, "bold"),
                               fill=COLORS["subtext"], anchor="w")
                pct = cv.create_text(x1s, text_y * S, text="0%", font=(F, 9),
                                     fill=COLORS["subtext"], anchor="e")
                cv.create_rectangle(x0s, bar_y0 * S, x1s, bar_y1 * S,
                                    fill=COLORS["surface"], outline="")
                fill = cv.create_rectangle(x0s, bar_y0 * S, x0s, bar_y1 * S,
                                           fill=COLORS["accent_green"], outline="")
                self._trig_views.append((fill, (x0s, bar_y0 * S, x1s, bar_y1 * S), pct, cv))
            except Exception:
                continue

    def _slider_row(self, parent, title, var):
        """Theme-native slider: dark track, green fill, light knob —
        drag (or click) to set. Same look language as the app's pills."""
        row = tk.Frame(parent, bg=COLORS["bg"])
        row.pack(side="left", padx=(0, 12))
        tk.Label(row, text=title, font=(F, 9), bg=COLORS["bg"],
                 fg=COLORS["subtext"], anchor="w").pack(fill="x")
        box = tk.Frame(row, bg=COLORS["bg"])
        box.pack(fill="x")
        cv = tk.Canvas(box, width=150, height=18, bg=COLORS["bg"],
                       bd=0, highlightthickness=0)
        cv.pack(side="left")
        track = cv.create_rectangle(0, 4, 150, 14, fill=COLORS["surface"],
                                    outline="")
        fill = cv.create_rectangle(0, 4, 75, 14, fill=COLORS["accent_green"],
                                   outline="")
        knob = cv.create_rectangle(70, 1, 80, 17, fill=COLORS["text"],
                                   outline="")
        val = tk.Label(box, text="0.50", font=(F, 9), bg=COLORS["bg"],
                       fg=COLORS["text"], width=5, anchor="e")
        val.pack(side="left", padx=(6, 0))
        state = {"drag": False}

        def _paint(v):
            try:
                v = max(0.0, min(1.0, float(v)))
                x = v * 150.0
                cv.coords(fill, 0, 4, x, 14)
                cv.coords(knob, x - 5, 1, x + 5, 17)
                val.config(text=f"{v:.2f}")
            except Exception:
                pass

        def _set_from_x(x):
            try:
                var.set(round(max(0.0, min(1.0, x / 150.0)), 2))
                _paint(var.get())
            except Exception:
                pass

        def _down(e):
            state["drag"] = True
            _set_from_x(e.x)

        def _move(e):
            if state["drag"]:
                _set_from_x(e.x)

        def _up(_e):
            state["drag"] = False

        try:
            cv.bind("<Button-1>", _down)
            cv.bind("<B1-Motion>", _move)
            cv.bind("<ButtonRelease-1>", _up)
            var.trace_add("write", lambda *_: _paint(var.get()))
        except Exception:
            pass
        _paint(var.get())
        try:
            self._sliders.append((cv, var, val))
        except Exception:
            pass

    def _calibrate(self):
        """Restart drift rest-center sampling (user lifts thumbs first)."""
        try:
            self._rest = None
            self._rest_samples = []
            self._set_stat(self._stat_drift_val, "learning…")
        except Exception:
            pass

    def _set_stat(self, lbl, text, fg=None):
        try:
            if lbl is not None:
                lbl.config(text=text, fg=fg or COLORS["text"])
        except Exception:
            pass

    def _test_rumble(self):
        """User-pressed rumble burst (~1s), then motors cut. Close cuts too."""
        try:
            if self._slot < 0:
                return
            try:
                weak = float(self._weak_var.get())
            except Exception:
                weak = 0.5
            try:
                strong = float(self._strong_var.get())
            except Exception:
                strong = 0.5
            try:
                if self._rumble_after is not None:
                    self._dlg.after_cancel(self._rumble_after)
            except Exception:
                pass
            self._rumble_after = None
            if _xinput_rumble(self._slot, weak, strong):
                try:
                    self._rumble_after = self._dlg.after(
                        _XINPUT_RUMBLE_MS, self._stop_rumble)
                except Exception:
                    _xinput_rumble(self._slot, 0, 0)
        except Exception:
            pass

    def _stop_rumble(self):
        try:
            if self._rumble_after is not None:
                try:
                    self._dlg.after_cancel(self._rumble_after)
                except Exception:
                    pass
            self._rumble_after = None
        except Exception:
            pass
        try:
            if self._slot >= 0:
                _xinput_rumble(self._slot, 0, 0)
        except Exception:
            pass

    # -- image-pad overlays (see _load_pad_artwork above) -------------------- #
    # _paint_btn/_paint_trigger/_paint_stick below are shared by both pad
    # modes and operate purely on the registries.

    def _paint_btn(self, name, on):
        try:
            shape, text = self._pad_btns[name]
            self._pad_cv.itemconfig(shape,
                                    fill=COLORS["accent_green"] if on else "")
            if text is not None:
                self._pad_cv.itemconfig(text,
                                        fill=COLORS["black"] if on else COLORS["subtext"])
        except Exception:
            pass

    def _paint_pad_idle(self):
        for name in self._pad_btns:
            self._paint_btn(name, False)
        for _fill, _track, _pct, _cv in list(getattr(self, "_trig_views", []) or []):
            self._paint_trigger((_fill, _track, _pct, _cv), 0.0)
        if getattr(self, "_stick_l", None) is not None:
            self._paint_stick(self._stick_l, 0, 0)
        if getattr(self, "_stick_r", None) is not None:
            self._paint_stick(self._stick_r, 0, 0)

    def _paint_trigger(self, view, frac):
        """Paint one trigger view (fill, track, pct_item, canvas): the
        fill sweeps left-to-right with pressure (horizontal bars get
        horizontal meters)."""
        try:
            fill, track, pct, cv = view
            x0, y0, x1, y1 = track
            frac = max(0.0, min(1.0, frac))
            cv.coords(fill, x0, y0, x0 + (x1 - x0) * frac, y1)
            if pct is not None:
                try:
                    cv.itemconfig(pct, text=f"{int(round(frac * 100))}%")
                except Exception:
                    try:
                        pct.config(text=f"{int(round(frac * 100))}%")
                    except Exception:
                        pass
        except Exception:
            pass

    def _paint_stick(self, view, x, y):
        try:
            cx, cy, nub = view
            S = getattr(self, "_SCALE", 1.0)
            nx = max(-1.0, min(1.0, x / 32768.0)) * 10 * S
            ny = max(-1.0, min(1.0, y / 32768.0)) * 10 * S
            r = 8 * S
            self._pad_cv.coords(nub, cx + nx - r, cy - ny - r,
                                cx + nx + r, cy - ny + r)
        except Exception:
            pass

    def _stop_all(self):
        """Close path: stop the poll tick AND cut the rumble motors."""
        try:
            if self._tick_after is not None:
                self._dlg.after_cancel(self._tick_after)
        except Exception:
            pass
        self._tick_after = None
        self._stop_rumble()
        try:
            self._slot = -1
        except Exception:
            pass

    def _stop_tick(self):
        self._stop_all()

    def _on_slot_pick(self, _e=None):
        """Pin polling + rumble to Auto / 1..4. Resets per-pad state so
        report-rate + drift never mix samples from two pads."""
        try:
            want = (self._slot_var.get() if self._slot_var is not None else "Auto")
        except Exception:
            want = "Auto"
        try:
            new_want = int(want) - 1 if str(want).strip().isdigit() else -1
            if new_want not in (-1, 0, 1, 2, 3):
                new_want = -1
        except Exception:
            new_want = -1
        try:
            if new_want != getattr(self, "_slot_want", -1):
                self._stop_rumble()
            self._slot_want = new_want
            self._stats.reset()
            self._rest = None
            self._rest_samples = []
            self._slot = -1
        except Exception:
            pass

    def _tick(self):
        self._tick_after = None
        try:
            fn = _xinput_state_fn()
            state = None
            slot = -1
            want = int(getattr(self, "_slot_want", -1) or -1)
            if fn is not None:
                if want in (0, 1, 2, 3):
                    try:
                        state = fn(want)
                    except Exception:
                        state = None
                    slot = want if state is not None else -1
                else:
                    for s in range(4):
                        try:
                            state = fn(s)
                        except Exception:
                            state = None
                        if state is not None:
                            slot = s
                            break
            if state is None:
                if self._slot >= 0:
                    self._stop_rumble()
                self._slot = -1
                self._stats.reset()
                self._rest = None
                self._rest_samples = []
                if want in (0, 1, 2, 3):
                    self._status_lbl.config(
                        text=f"Controller {want + 1} not detected — pick Auto or connect it.",
                        fg=COLORS["subtext"])
                else:
                    self._status_lbl.config(
                        text="No controller detected — connect one and press any button.",
                        fg=COLORS["subtext"])
                self._paint_pad_idle()
                self._set_stat(self._stat_rate_val, "—")
                self._set_stat(self._stat_ms_val, "—")
                self._set_stat(self._stat_drift_val, "—")
                try:
                    self._rumble_btn.set_enabled(False)
                except Exception:
                    pass
            else:
                self._slot = slot
                try:
                    self._rumble_btn.set_enabled(True)
                except Exception:
                    pass
                self._status_lbl.config(
                    text=f"Controller {slot + 1} connected — live.",
                    fg=COLORS["accent_green"])
                pressed = state["buttons"]
                for name, mask in _XINPUT_BUTTONS:
                    self._paint_btn(name, bool(pressed & mask))
                _views = list(getattr(self, "_trig_views", []) or [])
                _fracs = (state["lt"] / 255.0, state["rt"] / 255.0)
                for _view, _frac in zip(_views, _fracs):
                    self._paint_trigger(_view, _frac)
                if getattr(self, "_stick_l", None) is not None:
                    self._paint_stick(self._stick_l, state["lx"], state["ly"])
                if getattr(self, "_stick_r", None) is not None:
                    self._paint_stick(self._stick_r, state["rx"], state["ry"])
                # report-rate stats from packet deltas (reference math)
                self._stats.update(state.get("packet", 0))
                # drift rest-center: 30 still samples (inside the deadzone
                # = thumbs off). Any moved sample restarts the count, so a
                # bad calibration is impossible — hold still and it learns.
                lx, ly, rx, ry = state["lx"], state["ly"], state["rx"], state["ry"]
                if self._rest is None:
                    if max(abs(lx), abs(ly), abs(rx), abs(ry)) <= _XINPUT_STICK_DEADZONE:
                        self._rest_samples.append((lx, ly, rx, ry))
                    else:
                        self._rest_samples = []
                    if len(self._rest_samples) >= 30:
                        n = len(self._rest_samples)
                        self._rest = tuple(
                            sum(s[i] for s in self._rest_samples) / n
                            for i in range(4))
                if self._rest is None:
                    drift_l = drift_r = None
                else:
                    drift_l = max(abs(lx - self._rest[0]),
                                  abs(ly - self._rest[1])) / 327.68
                    drift_r = max(abs(rx - self._rest[2]),
                                  abs(ry - self._rest[3])) / 327.68
                # stats strip paint (short values only)
                if self._stats.hz > 0:
                    hz = self._stats.hz
                    fg = COLORS["accent_green"] if hz >= 500 else (
                        COLORS["accent_yellow"] if hz >= 240 else COLORS["accent_red"])
                    self._set_stat(self._stat_rate_val, f"{hz:.0f} Hz", fg)
                    self._set_stat(self._stat_ms_val, f"{self._stats.avg_ms:.1f} ms")
                else:
                    self._set_stat(self._stat_rate_val, "—")
                    self._set_stat(self._stat_ms_val, "—")
                if drift_l is None:
                    n = len(self._rest_samples)
                    self._set_stat(self._stat_drift_val, f"hold still {n}/30…")
                else:
                    bad = drift_l > 5.0 or drift_r > 5.0
                    self._set_stat(
                        self._stat_drift_val,
                        f"L {drift_l:.1f}% · R {drift_r:.1f}%",
                        COLORS["accent_red"] if bad else None)
        except Exception:
            pass
        try:
            self._tick_after = self._dlg.after(self._TICK_MS, self._tick)
        except Exception:
            self._tick_after = None
