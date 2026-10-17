"""MouseTesterDialog — extracted verbatim from app/gui.py (H12, batch 3).

One dialog per module, mirroring app/ui/widgets. Depends only on
app.ui below it, plus the app.utils helpers it already used; it
knows nothing
about any other dialog except where noted.
"""
from __future__ import annotations

import tkinter as tk

from app.ui.widgets.rounded_card import RoundedCard
from tkinter import ttk, messagebox
from app.ui.dialogs._tool_page import ToolPage
from app.ui.theme import COLORS, F, TAB_ACCENTS, _hex_lerp, _round_rect_points
from app.ui.widgets import AnimatedButton


class MouseTesterDialog(ToolPage):
    """Mouse Tester: Buttons tab (every button lights while held, wheel
    tick counter, live polling-rate from real event intervals,
    double-click gap) + Aim tab (10 targets, score from reaction ms +
    accuracy — Aim-Labs-lite, Tk ovals only). Pure Tk events, zero
    Win32, zero risk.

    REDESIGN 2026-10: on the shared ToolPage shell — the font-14 headline is
    now the Header banner and the mouse artwork lives in the content zone.
    scroll=False: the mouse canvas is a fixed interactive surface that must
    never scroll (its button zones are absolute canvas coordinates)."""

    _POLL_WINDOW_S = 1.0
    _AIM_ROUNDS = 10
    _AIM_R = 22

    _BTNS = (("Left", 1), ("Middle", 2), ("Right", 3),
             ("Side 1", 4), ("Side 2", 5))  # button numbers -> Tk event.num
    # num -> Win32 virtual-key code, for GetAsyncKeyState polling below.
    _POLL_VKS = {1: 0x01, 3: 0x02, 2: 0x04, 4: 0x05, 5: 0x06}
    _POLL_MS = 3  # ~330Hz — see _start_button_poll for why Tk events alone aren't enough

    def __init__(self, parent, app):
        self.app = app
        self._view = "buttons"
        self._btn_shapes = {}
        self._held = set()
        self._tested = set()
        self._wheel_ticks = 0
        self._move_times = []
        self._last_click_t = 0.0
        self._aim_running = False
        self._aim_left = 0
        self._aim_hits = 0
        self._aim_shots = 0
        self._aim_times = []
        self._aim_errs = []
        self._aim_spawn_t = 0.0
        self._aim_target = None
        # Real button-state polling via GetAsyncKeyState (see
        # _start_button_poll) instead of Tk's own ButtonPress/Release —
        # probed once here; falls back to plain Tk events if this isn't
        # Windows or the call fails for any reason.
        self._user32 = None
        try:
            import ctypes as _ct
            _u32 = _ct.windll.user32
            _u32.GetAsyncKeyState(0x01)  # sanity call — raises if unusable
            self._user32 = _u32
        except Exception:
            self._user32 = None
        self._vk_down = {}
        self._poll_after_id = None
        self._hires_timer = False
        super().__init__(parent, title="Mouse Tester",
                         accent=TAB_ACCENTS["Clean"],
                         scope="The aim test opens full-screen; Esc ends it "
                               "early.",
                         scroll=False, fit=True)
        self.banner.set_state("\U0001F5B1", TAB_ACCENTS["Clean"])
        self.banner.set_title("Does your mouse click right?")
        self.banner.set_sub("Click every button, scroll the wheel — or try "
                            "the aim test.")
        body = self.content
        tabs = tk.Frame(body, bg=COLORS["bg"])
        tabs.pack(pady=(0, 10))
        for key, label in (("buttons", "Buttons"), ("aim", "Aim Test")):
            AnimatedButton(tabs, text=label, command=lambda k=key: self._switch(k),
                           bg=COLORS["secondary_container"],
                           fg=COLORS["on_secondary_container"],
                           font=(F, 10, "bold"), padx=22, pady=8).pack(side="left", padx=4)
        self._content = tk.Frame(body, bg=COLORS["bg"])
        self._content.pack(fill="both", expand=True)
        self.on_close(self._stop)
        self._switch("buttons")

    # ---------------- view switching ---------------- #

    def _switch(self, key):
        self._stop_button_poll()
        self._view = key
        for child in self._content.winfo_children():
            try:
                child.destroy()
            except Exception:
                pass
        self._held.clear()
        if key == "buttons":
            self._build_buttons()
            if self._user32 is not None:
                self._start_button_poll()
        else:
            self._build_aim()
        self._bind_mouse()

    def _bind_mouse(self):
        # Rebind per view (old bindings die with the old content, but
        # Toplevel bindings persist — unbind first to avoid doubles).
        try:
            for seq in ("<ButtonPress>", "<ButtonRelease>", "<MouseWheel>",
                        "<Motion>", "<Double-Button-1>"):
                try:
                    self._dlg.unbind(seq)
                except Exception:
                    pass
            if self._view == "buttons":
                self._dlg.bind("<MouseWheel>", self._on_wheel, add="+")
                self._dlg.bind("<Motion>", self._on_move, add="+")
                if self._user32 is None:
                    # Not Windows (or GetAsyncKeyState unavailable) —
                    # fall back to plain Tk click events. Same
                    # rapid-click blind spot as before, but this only
                    # applies outside real Windows use.
                    self._dlg.bind("<ButtonPress>", self._on_tk_press_fallback, add="+")
                    self._dlg.bind("<ButtonRelease>", self._on_tk_release_fallback, add="+")
            else:
                self._dlg.bind("<ButtonPress>", self._on_aim_click, add="+")
            try:
                self._dlg.focus_force()
            except Exception:
                pass
        except Exception:
            pass

    # ---------------- real button-state polling ---------------- #
    #
    # Why this exists: Tk's <ButtonPress>/<ButtonRelease> come from
    # Windows translating raw clicks into synthetic Tk events, and for
    # a genuine fast double-click Windows itself merges the second
    # click into a single WM_LBUTTONDBLCLK message rather than a plain
    # WM_LBUTTONDOWN — exactly the kind of message Tk's event
    # translation can drop or mishandle depending on the Tcl/Tk build,
    # which is why the second click of a fast pair could fail to
    # register at all (not just measure wrong). GetAsyncKeyState reads
    # each button's real electrical state straight from Windows,
    # bypassing that whole translation path, so nothing about how
    # Windows decides to bundle rapid clicks into messages matters
    # anymore. Polled at ~330Hz (every 3ms) via a temporarily-raised
    # 1ms system timer resolution, which comfortably resolves clicks
    # far faster than any human or mechanical switch double-click.

    def _start_button_poll(self):
        self._vk_down = {num: False for num in self._POLL_VKS}
        try:
            import ctypes as _ct
            _ct.windll.winmm.timeBeginPeriod(1)
            self._hires_timer = True
        except Exception:
            self._hires_timer = False
        self._poll_buttons()

    def _poll_buttons(self):
        try:
            if self._user32 is not None:
                for num, vk in self._POLL_VKS.items():
                    down = bool(self._user32.GetAsyncKeyState(vk) & 0x8000)
                    was = self._vk_down.get(num, False)
                    if down and not was:
                        self._press(num)
                    elif was and not down:
                        self._release(num)
                    self._vk_down[num] = down
        except Exception:
            pass
        if self._view == "buttons":
            try:
                self._poll_after_id = self._dlg.after(self._POLL_MS, self._poll_buttons)
            except Exception:
                self._poll_after_id = None

    def _stop_button_poll(self):
        if self._poll_after_id:
            try:
                self._dlg.after_cancel(self._poll_after_id)
            except Exception:
                pass
            self._poll_after_id = None
        if self._hires_timer:
            try:
                import ctypes as _ct
                _ct.windll.winmm.timeEndPeriod(1)
            except Exception:
                pass
            self._hires_timer = False

    # ---------------- Buttons tab ---------------- #

    #: The artwork is authored inside this fixed box and SCALED to whatever
    #: the canvas is given, so the mouse shrinks with the window instead of
    #: forcing a scrollbar on the popup (2026-10 redesign: the canvas used to
    #: be a hard 372px, which is 16px more than a 998x738 popup's content zone
    #: has — one scrollbar, one starved dialog).
    _ART_W, _ART_H = 300, 372
    #: Requested canvas height. Small on purpose: this is the height the
    #: popup's layout must fit WITHOUT; the canvas then expands into whatever
    #: is left over (usually ~370px, i.e. the artwork's natural size).
    _ART_REQ_H = 180

    def _build_buttons(self):
        self._btn_shapes = {}
        self._cv_size = None
        self._mouse_cv = tk.Canvas(self._content, width=self._ART_W,
                                   height=self._ART_REQ_H,
                                   bg=COLORS["bg"], bd=0, highlightthickness=0)
        self._mouse_cv.pack(fill="both", expand=True, pady=(4, 4))
        self._mouse_cv.bind("<Configure>", self._on_canvas_resize)
        self._draw_mouse_wireframe()
        stats = tk.Frame(self._content, bg=COLORS["bg"])
        stats.pack(fill="x", pady=(8, 0))
        for c in range(4):
            stats.grid_columnconfigure(c, weight=1, uniform="mousestats")
        self._stat_tested, _ = self._mstat(stats, 0, "TESTED")
        self._stat_poll, _ = self._mstat(stats, 1, "POLLING")
        self._stat_wheel, _ = self._mstat(stats, 2, "WHEEL TICKS")
        self._stat_dbl, _ = self._mstat(stats, 3, "DOUBLE-CLICK")
        self._refresh_mstats()

    def _on_canvas_resize(self, event):
        """Re-fit the artwork when the popup changes size. Guarded on the
        exact size so the redraw happens once per real resize, not once per
        Configure event (Tk fires those for plenty of other reasons)."""
        if (event.width, event.height) == self._cv_size:
            return
        self._cv_size = (event.width, event.height)
        self._draw_mouse_wireframe()

    def _art_scale(self):
        """(k, ox, oy) — uniform scale factor and centring offsets that map
        the authored 300x372 box onto the canvas's actual size."""
        cv = self._mouse_cv
        try:
            w = cv.winfo_width()
            h = cv.winfo_height()
        except Exception:
            w = h = 1
        if w <= 1 or h <= 1:              # not mapped yet — use the request
            w, h = self._ART_W, self._ART_H
        k = min(w / float(self._ART_W), h / float(self._ART_H))
        return k, (w - self._ART_W * k) / 2.0, (h - self._ART_H * k) / 2.0

    def _draw_mouse_wireframe(self):
        """A top-down mouse whose buttons light up as you press them.

        Drawn as vector shapes instead of a bundled PNG (no third-party
        asset to license, themes exactly, crisp at any DPI).

        REDESIGN 2026-10 (second pass, from user feedback): the previous
        version traced the click buttons along the body's dome and let the
        wheel and the thumb buttons overlap them, which read as loose
        overlapping blobs. Now the body is the mouse IMAGE and every button
        is a plain rounded rectangle sitting INSIDE it, side by side, with a
        clear gap - nothing overlaps anything:

            body    capsule, x 60..240, y 14..358
            L / R   the two click buttons, at the top
            M       the scroll wheel, in the gap between them
            4 / 5   the two thumb buttons, lower left

        THIRD PASS: every coordinate below is in the authored box and is
        scaled by `_art_scale()` on the way out, so the whole drawing tracks
        the canvas instead of assuming 300x372. Nothing here hit-tests, so
        scaling can't break the buttons — a press is detected by polling
        GetAsyncKeyState, not by where the pointer is.
        """
        cv = self._mouse_cv
        cv.delete("all")
        self._btn_shapes = {}
        k, ox, oy = self._art_scale()

        def pts(x0, y0, x1, y1, r, steps):
            """Round-rect points for the authored box, mapped onto the canvas
            (the flat list is x, y, x, y…, so even indices scale with x)."""
            flat = _round_rect_points(x0, y0, x1, y1, r, steps=steps)
            return [ox + v * k if i % 2 == 0 else oy + v * k
                    for i, v in enumerate(flat)]

        def fs(size):
            return max(7, int(round(size * k)))

        # The mouse image.
        cv.create_polygon(pts(60, 14, 240, 358, 90, 16), smooth=True,
                          fill=COLORS["bg_alt"], outline="")

        def button(num, label, x0, y0, x1, y1, r, fsz):
            shape = cv.create_polygon(
                pts(x0, y0, x1, y1, r, 8),
                smooth=True, fill=COLORS["surface"], outline="")
            text = cv.create_text(ox + (x0 + x1) / 2 * k,
                                  oy + (y0 + y1) / 2 * k, text=label,
                                  font=(F, fs(fsz), "bold"),
                                  fill=COLORS["subtext"])
            self._btn_shapes[num] = (shape, text)

        button(1, "L", 74, 84, 140, 168, 15, 14)      # left click
        button(3, "R", 160, 84, 226, 168, 15, 14)     # right click
        button(2, "M", 143, 84, 157, 132, 7, 9)       # scroll wheel (middle)
        button(4, "4", 66, 172, 94, 210, 10, 10)      # thumb (back)
        button(5, "5", 66, 218, 94, 256, 10, 10)      # thumb (forward)

        # Keep any button that is held down right now lit through the resize.
        for num in sorted(getattr(self, "_held", None) or ()):
            self._paint_btn(num, True)

    def _mstat(self, parent, col, title):
        try:
            cell = RoundedCard(parent, fill=COLORS["bg_alt"], bg=COLORS["bg"],
                               radius=16, min_height=64)
            cell.grid(row=0, column=col, sticky="nsew", padx=6)
            tk.Label(cell.body, text=title, font=(F, 9),
                     bg=COLORS["bg_alt"],
                     fg=COLORS["subtext"]).pack(pady=(8, 0))
            val = tk.Label(cell.body, text="\u2014", font=(F, 14, "bold"),
                           bg=COLORS["bg_alt"], fg=COLORS["text"])
            val.pack(pady=(0, 8))
            return val, cell
        except Exception:
            return None, None

    def _paint_btn(self, num, held):
        got = self._btn_shapes.get(num)
        if not got:
            return
        shape, text = got
        color = COLORS["accent_green"] if held else (
            _hex_lerp(COLORS["accent_green"], COLORS["surface"], 0.7)
            if num in self._tested else COLORS["surface"])
        outline = ""
        try:
            self._mouse_cv.itemconfig(shape, fill=color, outline=outline)
            self._mouse_cv.itemconfig(
                text, fill=COLORS["black"] if held else COLORS["subtext"])
        except Exception:
            pass

    def _press(self, num):
        """Shared press handling — called from the GetAsyncKeyState poll
        (normal path) or the Tk-event fallback (non-Windows dev runs).
        Runs unconditionally on every detected down-transition; not
        gated behind "already held", so one missed/late release can't
        wedge a button and silently freeze the double-click stat."""
        if num not in self._btn_shapes:
            return
        try:
            import time as _time
            now = _time.perf_counter()
            if num == 1:
                if self._last_click_t:
                    gap = (now - self._last_click_t) * 1000.0
                    self._last_dbl_ms = gap
                    try:
                        if gap <= 1500.0:
                            self._stat_dbl.config(text=f"{gap:.0f}ms")
                        else:
                            self._stat_dbl.config(text="—")
                    except Exception:
                        pass
                self._last_click_t = now
        except Exception:
            pass
        self._held.discard(num)
        self._held.add(num)
        self._tested.add(num)
        self._paint_btn(num, True)
        self._refresh_mstats()

    def _release(self, num):
        self._held.discard(num)
        self._paint_btn(num, False)
        self._refresh_mstats()

    def _on_tk_press_fallback(self, event):
        try:
            num = int(getattr(event, "num", 0) or 0)
        except Exception:
            return
        self._press(num)

    def _on_tk_release_fallback(self, event):
        try:
            num = int(getattr(event, "num", 0) or 0)
        except Exception:
            return
        self._release(num)

    def _on_wheel(self, event):
        try:
            self._wheel_ticks += 1 if int(getattr(event, "delta", 0) or 0) >= 0 else 1
        except Exception:
            self._wheel_ticks += 1
        self._refresh_mstats()

    def _on_move(self, event):
        try:
            import time as _time
            now = _time.perf_counter()
            self._move_times.append(now)
            cutoff = now - self._POLL_WINDOW_S
            while self._move_times and self._move_times[0] < cutoff:
                self._move_times.pop(0)
            n = len(self._move_times)
            if n >= 3:
                span = self._move_times[-1] - self._move_times[0]
                hz = (n - 1) / span if span > 0 else 0.0
                try:
                    self._stat_poll.config(text=f"{hz:.0f}Hz")
                except Exception:
                    pass
        except Exception:
            pass

    def _refresh_mstats(self):
        try:
            self._stat_tested.config(
                text=f"{len(self._tested)}/{len(self._btn_shapes)}")
        except Exception:
            pass
        try:
            self._stat_wheel.config(text=str(self._wheel_ticks))
        except Exception:
            pass

    # ---------------- Aim tab ---------------- #

    # Aim-trainer canvas surfaces. REDESIGN 2026-10: these were blue-tinted
    # navy greys from the old theme and are now palette tiers, so the aim
    # canvas stops being the one screen in the app with a colour cast.
    _AIM_BG = COLORS["bg"]
    _AIM_GRID = COLORS["hairline"]

    def _build_aim(self):
        self._aim_running = False
        self._aim_target = None
        self._aim_win = None
        tk.Label(self._content, text="Full-screen aim trainer — 10 targets, "
                 "center hits score more.",
                 font=(F, 10, "bold"), bg=COLORS["bg"],
                 fg=COLORS["subtext"], anchor="w",
                 wraplength=self.body_width(28), justify="left").pack(fill="x", pady=(0, 10))
        self._aim_info = tk.Label(self._content,
                                  text="Press Start — the test opens full-screen. "
                                       "Esc quits early.",
                                  font=(F, 9), bg=COLORS["bg"],
                                  fg=COLORS["subtext"], wraplength=self.body_width(28),
                                  justify="left")
        self._aim_info.pack(pady=(0, 10))
        row = tk.Frame(self._content, bg=COLORS["bg"])
        row.pack(pady=(4, 0))
        self._aim_btn = AnimatedButton(row, text="Start",
                                       command=self._aim_start,
                                       bg=COLORS["surface"], fg=COLORS["text"],
                                       font=(F, 9, "bold"), padx=22, pady=6)
        self._aim_btn.pack()

    def _aim_start(self):
        self._aim_running = True
        self._aim_left = self._AIM_ROUNDS
        self._aim_hits = 0
        self._aim_shots = 0
        self._aim_times = []
        self._aim_errs = []
        try:
            self._aim_btn.config_text("Restart")
        except Exception:
            pass
        self._open_aim_fullscreen()

    def _open_aim_fullscreen(self):
        """Real full-screen test window (was a plain 680x320 inline
        canvas before — no room to actually test flick range, and it
        looked like a placeholder). Own Toplevel, own event loop
        bindings, closed on finish or Esc."""
        try:
            win = tk.Toplevel(self._dlg)
            self._aim_win = win
            win.configure(bg=self._AIM_BG)
            try:
                win.attributes("-fullscreen", True)
            except Exception:
                # Fallback for WMs that reject -fullscreen: cover the
                # whole primary display manually.
                sw, sh = win.winfo_screenwidth(), win.winfo_screenheight()
                win.overrideredirect(True)
                win.geometry(f"{sw}x{sh}+0+0")
            try:
                win.attributes("-topmost", True)
            except Exception:
                pass
            win.focus_force()
            sw = win.winfo_screenwidth()
            sh = win.winfo_screenheight()
            cv = tk.Canvas(win, width=sw, height=sh, bg=self._AIM_BG,
                           bd=0, highlightthickness=0, cursor="crosshair")
            cv.pack(fill="both", expand=True)
            self._aim_cv = cv
            self._aim_w, self._aim_h = sw, sh
            # Faint grid — plain black used to read as an empty/broken
            # window; this reads as an actual aim-trainer arena.
            step = 64
            for gx in range(0, sw, step):
                cv.create_line(gx, 0, gx, sh, fill=self._AIM_GRID, width=1)
            for gy in range(0, sh, step):
                cv.create_line(0, gy, sw, gy, fill=self._AIM_GRID, width=1)
            self._aim_hud = cv.create_text(
                sw // 2, 36, text="", fill=COLORS["text"],
                font=(F, 14, "bold"), anchor="n")
            self._aim_sub = cv.create_text(
                sw // 2, 64, text="Click the target — Esc to quit",
                fill=COLORS["subtext"], font=(F, 10), anchor="n")
            cv.bind("<Button-1>", self._on_aim_click)
            win.bind("<Escape>", lambda _e: self._aim_abort())
            win.protocol("WM_DELETE_WINDOW", self._aim_abort)
            self._update_aim_hud()
            self._aim_next()
        except Exception:
            self._aim_win = None
            self._aim_info.config(text="Couldn't open the full-screen test on this display.")

    def _update_aim_hud(self):
        try:
            done = self._AIM_ROUNDS - self._aim_left
            n = len(self._aim_times)
            avg = sum(self._aim_times) / n if n else 0.0
            self._aim_cv.itemconfig(
                self._aim_hud,
                text=f"Target {min(done + 1, self._AIM_ROUNDS)}/{self._AIM_ROUNDS}"
                     f"   ·   hits {self._aim_hits}   ·   avg {avg:.0f}ms" if n
                     else f"Target {min(done + 1, self._AIM_ROUNDS)}/{self._AIM_ROUNDS}")
        except Exception:
            pass

    def _aim_abort(self):
        self._aim_running = False
        self._aim_target = None
        try:
            if self._aim_win is not None:
                self._aim_win.destroy()
        except Exception:
            pass
        self._aim_win = None
        try:
            self._aim_info.config(text="Test cancelled. Press Start to try again.")
        except Exception:
            pass

    def _aim_next(self):
        cv = getattr(self, "_aim_cv", None)
        if cv is None:
            return
        try:
            cv.delete("target")
        except Exception:
            return
        if self._aim_left <= 0:
            self._aim_finish()
            return
        try:
            import random as _rand
            import time as _time
            r = self._AIM_R
            pad = r + 40
            x = _rand.randint(pad, max(pad + 1, self._aim_w - pad))
            y = _rand.randint(pad, max(pad + 1, self._aim_h - pad))
            # Layered rings read as a lit target instead of a flat dot —
            # outer glow ring, mid ring, bullseye, crosshair ticks.
            cv.create_oval(x - r - 10, y - r - 10, x + r + 10, y + r + 10,
                           outline=COLORS["accent_red"], width=2, tags="target")
            cv.create_oval(x - r, y - r, x + r, y + r,
                           fill=COLORS["accent_red"], outline="#ffffff",
                           width=2, tags="target")
            cv.create_oval(x - r * 2 // 3, y - r * 2 // 3,
                           x + r * 2 // 3, y + r * 2 // 3,
                           fill=COLORS["accent_yellow"], outline="",
                           tags="target")
            cv.create_oval(x - 5, y - 5, x + 5, y + 5,
                           fill="#FFFFFF", outline="", tags="target")
            tick = r + 16
            for dx, dy in ((tick, 0), (-tick, 0), (0, tick), (0, -tick)):
                cv.create_line(x + dx * 0.55, y + dy * 0.55, x + dx, y + dy,
                               fill=COLORS["muted"], width=2, tags="target")
            self._aim_target = (x, y)
            self._aim_spawn_t = _time.perf_counter()
            self._update_aim_hud()
        except Exception:
            pass

    def _on_aim_click(self, event):
        if not self._aim_running or self._aim_target is None:
            return
        try:
            import time as _time
            import math as _math
            x, y = self._aim_target
            px, py = event.x, event.y
            self._aim_shots += 1
            dist = _math.hypot(px - x, py - y)
            ms = (_time.perf_counter() - self._aim_spawn_t) * 1000.0
            if dist <= self._AIM_R:
                self._aim_hits += 1
                self._aim_times.append(ms)
                self._aim_errs.append(max(0.0, 1.0 - dist / self._AIM_R))
                self._aim_left -= 1
                self._aim_target = None
                try:
                    self._aim_cv.itemconfig(self._aim_sub,
                                            text=f"Hit! {ms:.0f}ms", fill=COLORS["accent_green"])
                except Exception:
                    pass
                self._aim_next()
            else:
                self._aim_errs.append(0.0)
                try:
                    self._aim_cv.itemconfig(self._aim_sub, text="Miss — try again",
                                            fill=COLORS["accent_red"])
                except Exception:
                    pass
        except Exception:
            pass

    def _aim_finish(self):
        self._aim_running = False
        self._aim_target = None
        try:
            import statistics as _st
            n = len(self._aim_times)
            avg = sum(self._aim_times) / n if n else 0.0
            best = min(self._aim_times) if n else 0.0
            acc = (sum(self._aim_errs) / self._aim_shots * 100.0) if self._aim_shots else 0.0
            score = int(100000.0 * (acc / 100.0) / avg) if avg > 0 else 0
            if avg <= 0:
                verdict = "No hits — try again!"
            elif avg < 250 and acc >= 70:
                verdict = "Cracked aim — streamer material."
            elif avg < 350:
                verdict = "Solid casual aim."
            else:
                verdict = "Warming up — run it back."
            summary = (f"Score {score} · avg {avg:.0f}ms · best {best:.0f}ms · "
                       f"accuracy {acc:.0f}% ({self._aim_hits}/{self._AIM_ROUNDS} hits). {verdict}")
        except Exception:
            summary = "Run finished."
        # Show the result on the full-screen canvas briefly, then close
        # it and land back on the modal with the same summary.
        try:
            cv = getattr(self, "_aim_cv", None)
            if cv is not None:
                cv.delete("target")
                cv.itemconfig(self._aim_hud, text="Run complete")
                cv.itemconfig(self._aim_sub, text=summary, fill=COLORS["text"])
                self._dlg.after(1400, self._close_aim_fullscreen)
            else:
                self._close_aim_fullscreen()
        except Exception:
            self._close_aim_fullscreen()
        try:
            self._aim_info.config(text=summary)
        except Exception:
            pass
        try:
            self._aim_btn.config_text("Play again")
        except Exception:
            pass

    def _close_aim_fullscreen(self):
        try:
            if self._aim_win is not None:
                self._aim_win.destroy()
        except Exception:
            pass
        self._aim_win = None

    def _stop(self):
        self._aim_running = False
        self._aim_target = None
        self._stop_button_poll()
        try:
            self._close_aim_fullscreen()
        except Exception:
            pass
