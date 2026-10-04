"""MicCheckDialog — extracted verbatim from app/gui.py (H12, batch 2).

One dialog per module, mirroring app/ui/widgets. Depends only on
app.ui below it, plus the app.utils helpers it already used; it
knows nothing
about any other dialog except where noted.
"""
from __future__ import annotations

import tkinter as tk
from tkinter import ttk, messagebox
from app.ui.base import ThemedModal, Tooltip
from app.ui.hardware.capture import _capture_inputs, _default_capture_id
from app.ui.hardware.mmdev import _InputMeter, _MM_STATE_NAMES, _mm_vfn
from app.ui.theme import COLORS, F, TAB_ACCENTS
from app.ui.widgets import AnimatedButton


class MicCheckDialog(ThemedModal):
    """Mic Check (user-requested Tools feature): one-glance
    verdict, the working microphones, the mic permission, and a live
    input-level bar. Strictly read-only — the fix path is the
    'Allow Microphone For Apps' tweak, linked below, never applied here."""

    _TICK_MS = 120

    def __init__(self, parent, app):
        self.app = app
        self._tick_after = None
        self._inputs = []
        self._meter = None
        self._needle = 0.0
        self._pick_var = tk.StringVar(value="")
        self._pick_busy = False
        try:
            self._pick_var.trace_add("write", self._pick_input)
        except Exception:
            pass
        super().__init__(parent, title="Mic Check",
                         accent=TAB_ACCENTS["Tweak"])
        body = self.body
        tk.Label(body, text="Pick a mic, speak, watch the dial.",
                 font=(F, 9), bg=COLORS["bg"], fg=COLORS["subtext"],
                 anchor="w").pack(fill="x")
        self._verdict_lbl = tk.Label(body, text="", font=(F, 12, "bold"),
                                     bg=COLORS["bg"], fg=COLORS["text"],
                                     anchor="w")
        self._verdict_lbl.pack(fill="x", pady=(8, 2))
        drow = tk.Frame(body, bg=COLORS["bg"])
        drow.pack(fill="x", pady=(2, 0))
        tk.Label(drow, text="Microphone:", font=(F, 9, "bold"),
                 bg=COLORS["bg"], fg=COLORS["text"],
                 anchor="w").pack(side="left")
        self._pick_box = tk.Frame(drow, bg=COLORS["bg"])
        self._pick_box.pack(side="left", padx=(8, 0))
        self._other_note = tk.Label(body, text="", font=(F, 8),
                                    bg=COLORS["bg"], fg=COLORS["subtext"],
                                    anchor="w")
        self._other_note.pack(fill="x")
        self._listen_lbl = tk.Label(body, text="", font=(F, 9, "bold"),
                                    bg=COLORS["bg"], fg=COLORS["text"],
                                    anchor="w")
        self._listen_lbl.pack(fill="x", pady=(4, 0))
        grow = tk.Frame(body, bg=COLORS["bg"])
        grow.pack(pady=(4, 0))
        self._gauge_cv = tk.Canvas(grow, width=220, height=132,
                                   bg=COLORS["bg"], bd=0,
                                   highlightthickness=0)
        self._gauge_cv.pack()
        self._draw_gauge()
        # level bar
        tk.Label(body, text="Input level — speak into your mic:",
                 font=(F, 9, "bold"), bg=COLORS["bg"], fg=COLORS["text"],
                 anchor="w").pack(fill="x", pady=(10, 2))
        self._level_cv = tk.Canvas(body, height=14, bg=COLORS["surface"],
                                   bd=0, highlightthickness=0)
        self._level_cv.pack(fill="x")
        self._level_item = self._level_cv.create_rectangle(
            0, 0, 0, 14, fill=COLORS["accent_green"], outline="")
        self._level_note = tk.Label(body, text="", font=(F, 9),
                                    bg=COLORS["bg"], fg=COLORS["subtext"],
                                    anchor="w")
        self._level_note.pack(fill="x")
        # actions row
        brow = tk.Frame(body, bg=COLORS["bg"])
        brow.pack(fill="x", pady=(12, 6))
        AnimatedButton(brow, text="Open Sound settings",
                       command=self._open_sound,
                       bg=COLORS["surface"], fg=COLORS["text"],
                       font=(F, 9, "bold"), padx=18, pady=7).pack(side="left")
        self._rec_btn = AnimatedButton(brow, text="Record 3s",
                                       command=self._record_take,
                                       bg=COLORS["surface"], fg=COLORS["text"],
                                       font=(F, 9, "bold"), padx=18, pady=7)
        self._rec_btn.pack(side="left", padx=(8, 0))
        Tooltip(self._rec_btn, "Records 3 seconds from the Windows default mic, then play it back to hear yourself")
        self._play_btn = AnimatedButton(brow, text="Play back",
                                        command=self._play_take,
                                        bg=COLORS["surface"], fg=COLORS["text"],
                                        font=(F, 9, "bold"), padx=18, pady=7)
        self._play_btn.pack(side="left", padx=(8, 0))
        self._play_btn.set_enabled(False)
        AnimatedButton(brow, text="Refresh",
                       command=self._refresh,
                       bg=COLORS["surface"], fg=COLORS["text"],
                       font=(F, 9, "bold"), padx=18, pady=7).pack(side="right")
        self._take = None
        self._recorder = None
        self._rec_busy = False
        tk.Label(body, text="Mic blocked? Tweak tab → Custom → "
                            "Allow Microphone For Apps (reversible).",
                 font=(F, 9), bg=COLORS["bg"], fg=COLORS["subtext"],
                 wraplength=680, justify="left").pack(anchor="w")
        self._rebuild_inputs()
        self.on_close(self._stop_tick)
        self._tick()

    def _draw_gauge(self):
        """Static dial furniture (Tk thread, once): zone arcs, ticks,
        labels. The needle (_needle item) moves in _tick."""
        try:
            cv = self._gauge_cv
            cx, cy, r = 110, 115, 85
            cv.create_arc(cx - r, cy - r, cx + r, cy + r, start=80,
                          extent=100, style="arc", outline=COLORS["accent_green"],
                          width=7)
            cv.create_arc(cx - r, cy - r, cx + r, cy + r, start=38,
                          extent=42, style="arc", outline=COLORS["accent_yellow"],
                          width=7)
            cv.create_arc(cx - r, cy - r, cx + r, cy + r, start=0,
                          extent=38, style="arc", outline=COLORS["accent_red"],
                          width=7)
            import math as _math
            for frac, label in ((0.0, "0"), (0.5, "50"), (1.0, "100")):
                deg = 180.0 - frac * 180.0
                rad = _math.radians(deg)
                x1, y1 = cx + 72 * _math.cos(rad), cy - 72 * _math.sin(rad)
                x2, y2 = cx + 85 * _math.cos(rad), cy - 85 * _math.sin(rad)
                cv.create_line(x1, y1, x2, y2, fill=COLORS["subtext"], width=2)
                cv.create_text(cx + 58 * _math.cos(rad), cy - 58 * _math.sin(rad),
                               text=label, font=(F, 8), fill=COLORS["subtext"])
            self._needle = cv.create_line(cx, cy, cx - 68, cy,
                                          fill=COLORS["accent_green"], width=3)
            cv.create_oval(cx - 6, cy - 6, cx + 6, cy + 6,
                           fill=COLORS["text"], outline="")
        except Exception:
            self._needle = None

    def _paint_needle(self, peak):
        try:
            import math as _math
            cx, cy = 110, 115
            deg = 180.0 - max(0.0, min(1.0, peak)) * 180.0
            rad = _math.radians(deg)
            self._gauge_cv.coords(self._needle, cx, cy,
                                  cx + 68 * _math.cos(rad),
                                  cy - 68 * _math.sin(rad))
            color = COLORS["accent_green"] if peak < 0.7 else (
                COLORS["accent_yellow"] if peak < 0.9 else COLORS["accent_red"])
            self._gauge_cv.itemconfig(self._needle, fill=color)
        except Exception:
            pass

    def _drop_inputs(self):
        """Release enumerated COM refs not owned by the live meter."""
        try:
            live = getattr(getattr(self, "_meter", None), "_dev_ptr", None)
        except Exception:
            live = None
        try:
            for e in getattr(self, "_inputs", []) or []:
                try:
                    d = (e or {}).get("dev")
                    if d is not None and d != live:
                        import ctypes as _ct
                        _mm_vfn(_ct, d, 2, _ct.c_ulong)(d)
                except Exception:
                    pass
        except Exception:
            pass

    def _rebuild_inputs(self):
        """Fresh enumerate + picker + verdict + meter for the pick."""
        try:
            if self._meter is not None:
                try:
                    self._meter.close()
                except Exception:
                    pass
            self._meter = None
            self._drop_inputs()
            self._inputs = _capture_inputs()
        except Exception:
            self._inputs = []
        try:
            from app.tasks.repair_tasks import read_mic_consent
            _consent, _apps = read_mic_consent()
        except Exception:
            _consent = None
        actives = [e for e in self._inputs if (e or {}).get("state") == 1]
        others = [e for e in self._inputs
                  if (e or {}).get("state") not in (1, 4)]
        try:
            for w in list(self._pick_box.winfo_children()):
                try:
                    w.destroy()
                except Exception:
                    pass
        except Exception:
            pass
        names = [(e or {}).get("name", "") for e in actives]
        # preselect the Windows default capture endpoint (what the user
        # actually talks into), not just the first enumerated input
        try:
            _defid = _default_capture_id()
        except Exception:
            _defid = ""
        _defname = ""
        if _defid:
            for e in actives:
                try:
                    if (e or {}).get("id", "") == _defid:
                        _defname = (e or {}).get("name", "")
                        break
                except Exception:
                    continue
        try:
            self._pick_var.set(_defname or (names[0] if names else ""))
            _om = ttk.Combobox(
                self._pick_box, textvariable=self._pick_var,
                values=names, state="readonly",
                width=42 if tk.TkVersion >= 8.6 else 38, font=(F, 9))
            _om.pack(side="left")
        except Exception:
            pass
        try:
            if _consent is not None and _consent != "Allow":
                self._verdict_lbl.config(
                    text="🔇 Mic is blocked — turn permission on below.",
                    fg=COLORS["accent_red"])
            elif not actives:
                self._verdict_lbl.config(
                    text="⚠️ No working mic found — check the cable.",
                    fg=COLORS["accent_yellow"])
            else:
                self._verdict_lbl.config(text="🎙️ Mic ready — pick one and speak.",
                                         fg=COLORS["accent_green"])
            _notes = []
            for e in others:
                _word = _MM_STATE_NAMES.get((e or {}).get("state"), "unknown")
                _notes.append(f"{(e or {}).get('name', '')} ({_word})")
            self._other_note.config(
                text=("Also present (not usable): " + "; ".join(_notes))
                if _notes else "")
        except Exception:
            pass
        self._pick_input()

    def _pick_input(self, *_a):
        """Bind the live meter to the dropdown pick (idempotent — a
        re-pick of the same input is a no-op so the rebuild's explicit
        call can't kill the trace-fired binding)."""
        if getattr(self, "_pick_busy", False):
            return
        self._pick_busy = True
        try:
            want = (self._pick_var.get() or "").strip()
            if self._meter is not None \
                    and getattr(self, "_meter_name", "") == want:
                return
            if self._meter is not None:
                try:
                    self._meter.close()
                except Exception:
                    pass
            self._meter = None
            self._meter_name = ""
            self._take = None
            try:
                self._play_btn.set_enabled(False)
            except Exception:
                pass
            for e in self._inputs:
                try:
                    if (e or {}).get("name") == want \
                            and (e or {}).get("state") == 1 \
                            and (e or {}).get("dev") is not None:
                        m = _InputMeter.open(e["dev"])
                        if m is not None:
                            e["dev"] = None  # ownership transferred
                            self._meter = m
                            self._meter_name = want
                            try:
                                m.start_stream((e or {}).get("id", ""))
                            except Exception:
                                pass
                            try:
                                self._listen_lbl.config(
                                    text=f"Listening to: {want[:52]}")
                            except Exception:
                                pass
                        break
                except Exception:
                    continue
            if self._meter is None:
                try:
                    self._listen_lbl.config(text="")
                except Exception:
                    pass
        except Exception:
            pass
        finally:
            self._pick_busy = False

    def _refresh(self):
        try:
            self._rebuild_inputs()
        except Exception:
            pass

    def _open_sound(self):
        try:
            self.app._launch_settings("ms-settings:sound")
        except Exception:
            pass

    def _record_take(self):
        """Record 3s from the DEFAULT mic on a worker (blocking winmm),
        then enable Play back. Records the Windows default input — the
        picker preselects it; say so when it doesn't match."""
        if getattr(self, "_rec_busy", False):
            try:
                if self._recorder is not None:
                    self._recorder.stop()
            except Exception:
                pass
            return
        self._rec_busy = True
        self._take = None
        try:
            self._play_btn.set_enabled(False)
            self._rec_btn.config_text("Stop")
        except Exception:
            pass
        self._level_note.config(text="Recording 3s — speak normally…")

        def _worker():
            from app.mic_test import Recorder
            rec = Recorder(seconds=3.0)
            self._recorder = rec
            try:
                data = rec.record_wav()
            except Exception:
                data = None
            finally:
                self._recorder = None

            def _land():
                self._rec_busy = False
                try:
                    self._rec_btn.config_text("Record 3s")
                except Exception:
                    pass
                if data:
                    self._take = data
                    try:
                        self._play_btn.set_enabled(True)
                    except Exception:
                        pass
                    self._level_note.config(
                        text="Recorded 3s from the Windows default mic — press Play back to hear yourself.")
                else:
                    self._level_note.config(
                        text="Recording failed — mic in use or permission blocked.")
            try:
                self._dispatch.post(_land)
            except Exception:
                pass

        try:
            import threading as _th
            _th.Thread(target=_worker, daemon=True,
                       name="MicCheckRecord").start()
        except Exception:
            self._rec_busy = False

    def _play_take(self):
        if not getattr(self, "_take", None):
            return
        data = self._take

        def _worker():
            try:
                from app.mic_test import play_wav_bytes
                play_wav_bytes(data)
            except Exception:
                pass

        try:
            import threading as _th
            _th.Thread(target=_worker, daemon=True,
                       name="MicCheckPlay").start()
        except Exception:
            pass

    def _stop_tick(self):
        try:
            if getattr(self, "_recorder", None) is not None:
                self._recorder.stop()
        except Exception:
            pass
        try:
            if self._tick_after is not None:
                self._dlg.after_cancel(self._tick_after)
        except Exception:
            pass
        self._tick_after = None
        try:
            if self._meter is not None:
                try:
                    self._meter.close()
                except Exception:
                    pass
            self._meter = None
            self._drop_inputs()
        except Exception:
            pass

    def _tick(self):
        self._tick_after = None
        try:
            level = None
            try:
                if self._meter is not None:
                    level = self._meter.read()
            except Exception:
                level = None
            try:
                self._needle_val = getattr(self, "_needle_val", 0.0)
                target = level if level is not None else 0.0
                self._needle_val += (target - self._needle_val) * 0.45
                self._paint_needle(self._needle_val)
            except Exception:
                pass
            if level is None:
                self._level_note.config(text="Level meter unavailable on this PC.")
                try:
                    self._level_cv.coords(self._level_item, 0, 0, 0, 14)
                except Exception:
                    pass
            else:
                try:
                    import math as _math
                    # dBFS scale: real gain language — 0dB is digital
                    # full-scale (clip); aim peaks -12..-6dB for
                    # Discord/OBS. Linear % hid clipping completely.
                    peak = max(0.0, min(1.0, float(level)))
                    db = 20.0 * _math.log10(peak) if peak > 0 else -60.0
                    db = max(-60.0, db)
                    # peak-hold with slow decay doubles as clip memory
                    hold = float(getattr(self, "_peak_hold_db", -60.0))
                    hold = db if db > hold else max(-60.0, hold - 1.5)
                    self._peak_hold_db = hold
                    w = max(1, int(self._level_cv.winfo_width() or 400))
                    frac = max(0.0, min(1.0, (db + 60.0) / 60.0))
                    self._level_cv.coords(self._level_item, 0, 0, w * frac, 14)
                    try:
                        fill = COLORS["accent_green"] if db < -12 else (
                            COLORS["accent_yellow"] if db < -3 else COLORS["accent_red"])
                        self._level_cv.itemconfig(self._level_item, fill=fill)
                    except Exception:
                        pass
                    if peak >= 0.99 or hold >= -1.0:
                        self._level_note.config(
                            text=f"CLIPPING at {hold:.0f}dB peak — turn the mic gain down.")
                    elif db <= -59.0:
                        self._level_note.config(text="-inf dB — Listening…")
                    elif hold < -24.0:
                        self._level_note.config(
                            text=f"{db:.0f}dB (peak {hold:.0f}dB) — very quiet, raise gain.")
                    elif hold <= -6.0:
                        self._level_note.config(
                            text=f"{db:.0f}dB (peak {hold:.0f}dB) — Signal OK, aim -12 to -6dB peaks.")
                    else:
                        self._level_note.config(
                            text=f"{db:.0f}dB (peak {hold:.0f}dB) — hot, back off a little.")
                except Exception:
                    pass
        except Exception:
            pass
        try:
            self._tick_after = self._dlg.after(self._TICK_MS, self._tick)
        except Exception:
            self._tick_after = None
