"""MonitorTestDialog — extracted verbatim from app/gui.py (H12, batch 3).

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


class MonitorTestDialog(ThemedModal):
    """Monitor Test (Option A): Info shows the real current mode via the
    existing tweak_tasks query; Dead Pixels shows solid color fields with
    an optional fullscreen Start, auto-advance timer and gradient check.
    Fullscreen keeps an autohide control bar (taskbar-style) so edge
    pixels stay inspectable. No refresh measurement claim."""

    _COLORS_CYCLE = ("#000000", "#FFFFFF", "#FF0000", "#00FF00", "#0000FF", "#808080")
    _AUTO_MS_ON = 3000
    _FS_HIDE_MS = 2500

    def __init__(self, parent, app):
        self.app = app
        self._color_idx = 0
        self._fullscreen_win = None
        self._show_gradient = False
        self._auto_ms = 0
        self._auto_after = None
        self._auto_btn = None
        self._grad_btn = None
        self._mode_lbl = None
        self._fs_bar = None
        self._fs_auto_btn = None
        self._fs_grad_btn = None
        self._fs_mode_lbl = None
        self._fs_hide_after = None
        super().__init__(parent, title="Monitor Test", accent=TAB_ACCENTS["Clean"])
        body = self.body
        # UI fix: this used to be two tab pages (Info / Dead Pixels) for
        # what's really one small screen's worth of content — an extra
        # click for nothing. Everything now lives on one row: resolution,
        # max Hz at that resolution, and (when the panel's EDID is
        # readable) the monitor's own model name, evenly spaced.
        self._build_specs_row(body)
        self._pixel_cv = tk.Canvas(body, width=680, height=260,
                                   bg=self._COLORS_CYCLE[self._color_idx], bd=0,
                                   highlightthickness=0)
        self._pixel_cv.pack(pady=(4, 8))
        self._mode_lbl = None
        # One row, three buttons: Fullscreen / Cycle (solids + gradient)
        # / Auto On-Off at a fixed 3s cadence.
        row = tk.Frame(body, bg=COLORS["bg"])
        row.pack(pady=(4, 0))
        AnimatedButton(row, text="Fullscreen",
                       command=self._open_fullscreen_pixels,
                       bg=COLORS["surface"], fg=COLORS["text"],
                       font=(F, 9, "bold"), padx=16, pady=6).pack(side="left", padx=4)
        AnimatedButton(row, text="Cycle Colors", command=self._next_pixel_color,
                       bg=COLORS["surface"], fg=COLORS["text"],
                       font=(F, 9, "bold"), padx=16, pady=6).pack(side="left", padx=4)
        self._auto_btn = AnimatedButton(row, text="Auto Cycle: Off", command=self._toggle_auto,
                                        bg=COLORS["surface"], fg=COLORS["text"],
                                        font=(F, 9, "bold"), padx=16, pady=6)
        self._auto_btn.pack(side="left", padx=4)
        self._update_mode_ui()
        self.on_close(self._stop)

    def _build_specs_row(self, body):
        try:
            from app.tasks.tweak_tasks import (
                _query_video_mode_list, _query_precise_refresh_rate,
                _query_monitor_model)
            current, maximum = _query_video_mode_list()
            try:
                precise = _query_precise_refresh_rate()
            except Exception:
                precise = None
            try:
                model = _query_monitor_model()
            except Exception:
                model = None
        except Exception:
            current, maximum, precise, model = None, None, None, None
        rows = tk.Frame(body, bg=COLORS["bg"])
        rows.pack(fill="x", pady=(4, 2))
        for c in range(3):
            rows.grid_columnconfigure(c, weight=1, uniform="moninfo")

        def stat(col, title, value):
            cell = tk.Frame(rows, bg=COLORS["bg"])
            cell.grid(row=0, column=col, sticky="nsew", padx=12, pady=6)
            tk.Label(cell, text=title, font=(F, 8), bg=COLORS["bg"],
                     fg=COLORS["subtext"]).pack()
            tk.Label(cell, text=value, font=(F, 13, "bold"), bg=COLORS["bg"],
                     fg=COLORS["text"], wraplength=180).pack()

        if current and current[0] and current[1]:
            try:
                w, h, hz, _bits = current
                if precise and 20.0 < precise < 500.0:
                    stat(0, "RESOLUTION", f"{w}x{h} @ {precise:.2f}Hz")
                else:
                    stat(0, "RESOLUTION", f"{w}x{h} @ {hz}Hz")
            except Exception:
                stat(0, "RESOLUTION", "?")
        else:
            stat(0, "RESOLUTION", "?")
        if maximum:
            try:
                _mw, _mh, mhz, _b = maximum
                stat(1, "MAX Hz HERE", f"{mhz}Hz" if mhz else "?")
            except Exception:
                stat(1, "MAX Hz HERE", "?")
        else:
            stat(1, "MAX Hz HERE", "?")
        stat(2, "MONITOR", model or "?")

    def _render_swatch(self):
        """Paint the small swatch: gradient strips or the solid color."""
        try:
            cv = self._pixel_cv
            try:
                cv.delete("grad")
            except Exception:
                pass
            if getattr(self, "_show_gradient", False):
                try:
                    cv.config(bg="#000000")
                except Exception:
                    pass
                try:
                    w = int(cv.cget("width") or 680)
                    h = int(cv.cget("height") or 260)
                except Exception:
                    w, h = 680, 260
                steps = 64
                for i in range(steps):
                    v = int(i * 255 / max(1, steps - 1))
                    col = f"#{v:02x}{v:02x}{v:02x}"
                    x0 = w * i / steps
                    x1 = w * (i + 1) / steps
                    try:
                        cv.create_rectangle(x0, 0, x1, h, fill=col, outline="",
                                            tags="grad")
                    except Exception:
                        break
            else:
                try:
                    cv.config(bg=self._COLORS_CYCLE[self._color_idx])
                except Exception:
                    pass
        except Exception:
            pass
        self._update_mode_ui()

    def _mode_text(self):
        try:
            total = len(self._COLORS_CYCLE) + 1
            if getattr(self, "_show_gradient", False):
                return f"Gradient {total}/{total} — banding check"
            idx = int(getattr(self, "_color_idx", 0) or 0)
            col = self._COLORS_CYCLE[idx % len(self._COLORS_CYCLE)]
            return f"Solid {idx % len(self._COLORS_CYCLE) + 1}/{total} {col}"
        except Exception:
            return ""

    def _set_btn_text(self, btn, text):
        try:
            if btn is None:
                return
            try:
                btn.config_text(text)
            except Exception:
                try:
                    btn._text = text
                    try:
                        btn._measure()
                    except Exception:
                        pass
                    try:
                        btn._draw()
                    except Exception:
                        pass
                except Exception:
                    pass
        except Exception:
            pass

    def _update_mode_ui(self):
        """Keep swatch label + Auto buttons (main + fullscreen) showing
        the same state so the two surfaces never diverge."""
        try:
            txt = self._mode_text()
        except Exception:
            txt = ""
        try:
            if getattr(self, "_mode_lbl", None) is not None:
                self._mode_lbl.config(text=txt)
        except Exception:
            pass
        try:
            ms = int(getattr(self, "_auto_ms", 0) or 0)
            label = "Auto Cycle: Off" if not ms else "Auto Cycle: On"
            self._set_btn_text(getattr(self, "_auto_btn", None), label)
            self._set_btn_text(getattr(self, "_fs_auto_btn", None), label)
            # Green = on, red = off so the state reads at a glance.
            # Only restyle on actual change — redrawing the button canvas
            # on every color step flickered over the transition.
            for _b in (getattr(self, "_auto_btn", None),
                       getattr(self, "_fs_auto_btn", None)):
                try:
                    if _b is None:
                        continue
                    want_bg = COLORS["accent_green"] if ms else COLORS["accent_red"]
                    if getattr(_b, "_bg", None) == want_bg:
                        continue
                    if ms:
                        _b.set_style(bg=COLORS["accent_green"], fg=COLORS["black"],
                                     hover_bg=_hex_lerp(COLORS["accent_green"], "#FFFFFF", 0.08))
                    else:
                        _b.set_style(bg=COLORS["accent_red"], fg="#FFFFFF",
                                     hover_bg=_hex_lerp(COLORS["accent_red"], "#FFFFFF", 0.08))
                except Exception:
                    pass
        except Exception:
            pass
        try:
            if getattr(self, "_fs_mode_lbl", None) is not None:
                try:
                    if self._fs_mode_lbl.winfo_exists():
                        self._fs_mode_lbl.config(text=txt)
                except Exception:
                    pass
        except Exception:
            pass

    def _advance_color(self):
        """Step through all 7: 6 solids, then gradient, then back."""
        try:
            if getattr(self, "_show_gradient", False):
                self._show_gradient = False
                self._color_idx = (int(getattr(self, "_color_idx", 0) or 0) + 1) % len(self._COLORS_CYCLE)
            else:
                idx = int(getattr(self, "_color_idx", 0) or 0)
                if idx >= len(self._COLORS_CYCLE) - 1:
                    self._show_gradient = True
                else:
                    self._color_idx = idx + 1
        except Exception:
            pass
        self._render_swatch()
        self._sync_fullscreen()

    def _next_pixel_color(self):
        """Manual Cycle press: advance once and switch Auto off."""
        try:
            if int(getattr(self, "_auto_ms", 0) or 0):
                self._auto_ms = 0
                try:
                    if getattr(self, "_auto_after", None) is not None:
                        try:
                            self._dlg.after_cancel(self._auto_after)
                        except Exception:
                            pass
                    self._auto_after = None
                except Exception:
                    pass
        except Exception:
            pass
        self._advance_color()

    def _toggle_gradient(self):
        """Jump straight to/from gradient (G shortcut in fullscreen)."""
        self._show_gradient = not getattr(self, "_show_gradient", False)
        self._render_swatch()
        self._sync_fullscreen()

    def _toggle_auto(self):
        """Fixed-cadence auto-cycle On/Off (3s)."""
        try:
            cur = int(getattr(self, "_auto_ms", 0) or 0)
            self._auto_ms = 0 if cur else int(self._AUTO_MS_ON)
        except Exception:
            self._auto_ms = 0
        self._update_mode_ui()
        self._schedule_auto()

    def _schedule_auto(self):
        try:
            if getattr(self, "_auto_after", None) is not None:
                try:
                    self._dlg.after_cancel(self._auto_after)
                except Exception:
                    pass
            self._auto_after = None
        except Exception:
            pass
        try:
            ms = int(getattr(self, "_auto_ms", 0) or 0)
            if ms > 0:
                self._auto_after = self._dlg.after(ms, self._auto_tick)
        except Exception:
            self._auto_after = None

    def _auto_tick(self):
        self._auto_after = None
        try:
            self._advance_color()
        except Exception:
            pass
        self._schedule_auto()

    def _paint_fullscreen_gradient(self, win):
        """Draw a grayscale ramp on the fullscreen window for banding.

        Only the test canvas is rebuilt — the autohide control bar is a
        separate child and is left alone."""
        try:
            old = getattr(self, "_fs_canvas", None)
            try:
                if old is not None and old.winfo_exists():
                    old.destroy()
            except Exception:
                pass
            self._fs_canvas = None
            try:
                w = int(win.winfo_screenwidth() or 800)
                h = int(win.winfo_screenheight() or 600)
            except Exception:
                w, h = 800, 600
            cv = tk.Canvas(win, width=w, height=h, bd=0, highlightthickness=0,
                           bg="#000000")
            cv.pack(fill="both", expand=True)
            steps = 64
            for i in range(steps):
                v = int(i * 255 / max(1, steps - 1))
                col = f"#{v:02x}{v:02x}{v:02x}"
                try:
                    cv.create_rectangle(w * i / steps, 0, w * (i + 1) / steps, h,
                                        fill=col, outline="")
                except Exception:
                    break
            self._fs_canvas = cv
            try:
                self._raise_fs_bar()
            except Exception:
                pass
            return cv
        except Exception:
            return None

    def _clear_fullscreen_canvas(self):
        try:
            old = getattr(self, "_fs_canvas", None)
            self._fs_canvas = None
            try:
                if old is not None and old.winfo_exists():
                    old.destroy()
            except Exception:
                pass
        except Exception:
            pass

    def _sync_fullscreen(self):
        """Push current swatch state to the open fullscreen window."""
        try:
            win = getattr(self, "_fullscreen_win", None)
            if win is None:
                return
            try:
                if not win.winfo_exists():
                    self._fullscreen_win = None
                    return
            except Exception:
                return
            if getattr(self, "_show_gradient", False):
                try:
                    win.configure(bg="#000000")
                except Exception:
                    pass
                self._paint_fullscreen_gradient(win)
            else:
                self._clear_fullscreen_canvas()
                try:
                    win.configure(bg=self._COLORS_CYCLE[self._color_idx])
                except Exception:
                    pass
        except Exception:
            pass
        self._update_mode_ui()

    def _build_fs_bar(self, win):
        """Bottom-center controls overlay. Placed (not packed) so the test
        surface keeps its full size underneath. Mirrors the main 3-button
        row: Cycle / Auto / Exit."""
        try:
            bar = tk.Frame(win, bg=COLORS["bg"])
            self._fs_mode_lbl = None
            AnimatedButton(bar, text="Cycle", command=self._fs_cycle,
                           bg=COLORS["surface"], fg=COLORS["text"],
                           font=(F, 9, "bold"), padx=14, pady=6).pack(side="left", padx=4, pady=6)
            self._fs_auto_btn = AnimatedButton(bar, text="Auto Cycle: Off",
                                               command=self._toggle_auto,
                                               bg=COLORS["surface"], fg=COLORS["text"],
                                               font=(F, 9, "bold"), padx=14, pady=6)
            self._fs_auto_btn.pack(side="left", padx=4, pady=6)
            AnimatedButton(bar, text="Exit", command=self._close_fullscreen,
                           bg=COLORS["surface"], fg=COLORS["text"],
                           font=(F, 9, "bold"), padx=14, pady=6).pack(side="left", padx=4, pady=6)
            self._fs_bar = bar
            self._update_mode_ui()
        except Exception:
            self._fs_bar = None

    def _fs_event_on_bar(self, ev):
        """True if a fullscreen event came from the control bar (or one of
        its buttons) rather than the test surface itself."""
        try:
            bar = getattr(self, "_fs_bar", None)
            w = getattr(ev, "widget", None)
            if bar is None or w is None:
                return False
            try:
                if w == bar:
                    return True
            except Exception:
                pass
            try:
                return str(w).startswith(str(bar))
            except Exception:
                return False
        except Exception:
            return False

    def _fs_cycle(self, _e=None):
        try:
            self._next_pixel_color()
        except Exception:
            pass
        try:
            self._reset_fs_hide_timer()
        except Exception:
            pass

    def _raise_fs_bar(self):
        try:
            bar = getattr(self, "_fs_bar", None)
            if bar is not None and bar.winfo_exists():
                try:
                    bar.lift()
                except Exception:
                    pass
        except Exception:
            pass

    def _show_fs_bar(self):
        try:
            win = getattr(self, "_fullscreen_win", None)
            bar = getattr(self, "_fs_bar", None)
            if win is None or bar is None:
                return
            try:
                if not win.winfo_exists() or not bar.winfo_exists():
                    return
            except Exception:
                return
            try:
                # Idempotent: re-placing + lifting an already-visible bar
                # on every click caused visible flicker over the color
                # change, so skip the redraw when it's already up.
                if bar.winfo_manager() == "place":
                    return
            except Exception:
                pass
            try:
                bar.place(relx=0.5, rely=1.0, anchor="s", y=-18)
                bar.lift()
            except Exception:
                pass
        except Exception:
            pass

    def _hide_fs_bar(self):
        self._fs_hide_after = None
        try:
            bar = getattr(self, "_fs_bar", None)
            if bar is not None and bar.winfo_exists():
                try:
                    bar.place_forget()
                except Exception:
                    pass
        except Exception:
            pass

    def _cancel_fs_hide_timer(self):
        try:
            if getattr(self, "_fs_hide_after", None) is not None:
                try:
                    win = getattr(self, "_fullscreen_win", None)
                    if win is not None and win.winfo_exists():
                        win.after_cancel(self._fs_hide_after)
                    else:
                        self._dlg.after_cancel(self._fs_hide_after)
                except Exception:
                    pass
        except Exception:
            pass
        self._fs_hide_after = None

    def _reset_fs_hide_timer(self, _e=None):
        """Show the bar on activity, hide it after idle. Taskbar-style."""
        try:
            self._show_fs_bar()
            self._cancel_fs_hide_timer()
        except Exception:
            pass
        try:
            win = getattr(self, "_fullscreen_win", None)
            ms = int(getattr(self, "_FS_HIDE_MS", 2500) or 2500)
            if win is not None and win.winfo_exists():
                try:
                    self._fs_hide_after = win.after(ms, self._hide_fs_bar)
                    return
                except Exception:
                    pass
            self._fs_hide_after = self._dlg.after(ms, self._hide_fs_bar)
        except Exception:
            self._fs_hide_after = None

    def _close_fullscreen(self, _e=None):
        try:
            self._cancel_fs_hide_timer()
        except Exception:
            pass
        try:
            win = getattr(self, "_fullscreen_win", None)
            self._fullscreen_win = None
            self._fs_bar = None
            self._fs_auto_btn = None
            self._fs_grad_btn = None
            self._fs_mode_lbl = None
            self._fs_canvas = None
            if win is not None:
                try:
                    win.destroy()
                except Exception:
                    pass
        except Exception:
            pass

    def _open_fullscreen_pixels(self):
        try:
            if getattr(self, "_fullscreen_win", None) is not None:
                try:
                    if self._fullscreen_win.winfo_exists():
                        try:
                            self._fullscreen_win.focus_force()
                        except Exception:
                            pass
                        try:
                            self._reset_fs_hide_timer()
                        except Exception:
                            pass
                        return
                except Exception:
                    pass
                self._close_fullscreen()
            win = tk.Toplevel(self._dlg)
            win.attributes("-fullscreen", True)
            win.attributes("-topmost", True)
            win.configure(bg="#000000" if getattr(self, "_show_gradient", False)
                          else self._COLORS_CYCLE[self._color_idx])
            self._fullscreen_win = win
            self._fs_canvas = None
            self._fs_bar = None
            if getattr(self, "_show_gradient", False):
                self._paint_fullscreen_gradient(win)
            self._build_fs_bar(win)
            self._show_fs_bar()
            self._reset_fs_hide_timer()

            def cycle(_e=None):
                # Clicks/keys on the control bar must not also trigger the
                # window-level cycle — otherwise one press on Cycle/Auto/Exit
                # advanced twice (skipped color = perceived jitter).
                try:
                    if self._fs_event_on_bar(_e):
                        self._reset_fs_hide_timer()
                        return
                except Exception:
                    pass
                self._next_pixel_color()
                self._reset_fs_hide_timer()

            def gradient(_e=None):
                self._toggle_gradient()
                self._reset_fs_hide_timer()

            win.bind("<Button-1>", cycle)
            win.bind("<space>", cycle)
            win.bind("g", gradient)
            win.bind("G", gradient)
            win.bind("<Escape>", self._close_fullscreen)
            win.bind("<Motion>", self._reset_fs_hide_timer)
            win.bind("<Key>", self._reset_fs_hide_timer)
            try:
                win.focus_force()
            except Exception:
                pass
        except Exception:
            pass

    def _stop(self):
        try:
            if getattr(self, "_auto_after", None) is not None:
                try:
                    self._dlg.after_cancel(self._auto_after)
                except Exception:
                    pass
        except Exception:
            pass
        self._auto_after = None
        self._auto_ms = 0
        try:
            self._cancel_fs_hide_timer()
        except Exception:
            pass
        self._close_fullscreen()
