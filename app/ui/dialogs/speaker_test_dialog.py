"""SpeakerTestDialog — extracted verbatim from app/gui.py (H12, batch 3).

One dialog per module, mirroring app/ui/widgets. Depends only on
app.ui below it, plus the app.utils helpers it already used; it
knows nothing
about any other dialog except where noted.
"""
from __future__ import annotations

import threading

import tkinter as tk
from tkinter import ttk, messagebox
from app.ui.base import ThemedModal, Tooltip
from app.ui.theme import COLORS, F, TAB_ACCENTS, _hex_lerp
from app.ui.widgets import AnimatedButton


class SpeakerTestDialog(ThemedModal):
    """Speaker Test: 8-direction surround check (Dolby-Atmos style) on
    any connected output. Tones are real stereo WAVs panned in code
    (front = true pan, side/rear = level-stepped simulation, stated
    in-UI). The picker chooses which device is TESTED — playback goes
    straight to it via waveOut, so the Windows default is never
    flipped. Unmatched devices fall back to default output, flagged
    honestly in the status line."""

    _ARROWS = {"FL": "\u2196", "FC": "\u2191", "FR": "\u2197",
               "SL": "\u2190", "SR": "\u2192",
               "RL": "\u2199", "RC": "\u2193", "RR": "\u2198"}

    def __init__(self, parent, app):
        self.app = app
        self._out_var = tk.StringVar(value="")
        self._outs = []
        self._seq_after = None
        # CT-005 audit fix: play_wav_on_device/play_wav_bytes block for the
        # tone's duration + 2.0s in a busy-sleep. _play used to call them
        # directly on the Tk callback thread, freezing the entire dialog
        # (no repaint, no other clicks, can't even close it) for that whole
        # time — and Sweep chained eight of these back-to-back. A lock
        # keeps playback serialized (same audible behavior as before —
        # never two tones on the device at once) while the actual wait
        # happens off the Tk thread.
        self._play_lock = threading.Lock()
        self._sweep_active = False
        # CT-005 audit fix (thread-safety refinement): calling self._dlg.after()
        # directly FROM the background playback thread is not reliably safe —
        # confirmed empirically (the scheduled callback silently never fired
        # in testing) even though it raises no exception. Tkinter calls must
        # come from the Tk thread. Use this codebase's own established
        # pattern instead (see the test-tool worker/_poll pairs elsewhere in
        # this file): the worker thread only appends to a plain list; a
        # poll loop self-rescheduled FROM the Tk thread drains it.
        self._audio_inbox = []
        self._audio_poll_after = None
        self._dir_btns = {}  # play key -> AnimatedButton (for play highlight)
        super().__init__(parent, title="Speaker Test", accent=TAB_ACCENTS["Clean"])
        body = self.body
        tk.Label(body, text="Check each direction plays where it should.",
                 font=(F, 10, "bold"), bg=COLORS["bg"],
                 fg=COLORS["subtext"], anchor="w").pack(fill="x", pady=(0, 6))
        # Test-target picker — only when >1 active output exists.
        self._out_row = tk.Frame(body, bg=COLORS["bg"])
        self._stat_lbl = tk.Label(body, text="", font=(F, 9),
                                  bg=COLORS["bg"], fg=COLORS["subtext"],
                                  wraplength=640)
        self._stat_lbl.pack(pady=(0, 2))
        # L/R identity check — separate from the 8-dir surround
        # simulation below. Same tone, hard-panned: answers "is left
        # actually plugged into left" without pitch tricks. Fixed equal
        # widths so the pair reads symmetrical (text-measured canvases
        # sized each glyph differently, like the 3x3 grid fix below).
        lr = tk.Frame(body, bg=COLORS["bg"])
        lr.pack(pady=(4, 10))
        _lr_l = AnimatedButton(lr, text="\u25c0 LEFT",
                               command=lambda: self._play_identity("L"),
                               bg=COLORS["surface"], fg=COLORS["text"],
                               font=(F, 10, "bold"), width=120, padx=22, pady=6)
        _lr_l.pack(side="left", padx=4)
        self._dir_btns["L"] = _lr_l
        Tooltip(_lr_l, "Left speaker wiring check — same tone, hard-panned left")
        _lr_r = AnimatedButton(lr, text="RIGHT \u25b6",
                               command=lambda: self._play_identity("R"),
                               bg=COLORS["surface"], fg=COLORS["text"],
                               font=(F, 10, "bold"), width=120, padx=22, pady=6)
        _lr_r.pack(side="left", padx=4)
        self._dir_btns["R"] = _lr_r
        Tooltip(_lr_r, "Right speaker wiring check — same tone, hard-panned right")
        grid = tk.Frame(body, bg=COLORS["bg"])
        grid.pack(pady=(4, 4))
        try:
            from app.audio_out import DIRECTIONS as _DIRS
        except Exception:
            _DIRS = ()
        _cells = {"FL": (0, 0), "FC": (0, 1), "FR": (0, 2),
                  "SL": (1, 0), "SR": (1, 2),
                  "RL": (2, 0), "RC": (2, 1), "RR": (2, 2)}
        # UI fix (Rubik's-cube symmetry): AnimatedButton is a Canvas sized
        # from its own text measurement — with no explicit width/height it
        # sizes each glyph differently (↑ vs ↖ vs ● aren't the same width),
        # and a Canvas doesn't repaint its contents to fill extra space a
        # grid's sticky="nsew"/uniform stretching hands it. So every cell
        # was the same OUTER size but each button's drawn pill inside it
        # was a different size, reading as misaligned. Giving all nine
        # buttons the exact same fixed pixel square sidesteps that
        # entirely — no stretch/redraw mismatch is possible.
        _SQ = 54
        _SIM = {"SL", "SR", "RL", "RC", "RR"}
        for key, label, _pan, _db, _hz in _DIRS:
            r, c = _cells.get(key, (1, 1))
            b = AnimatedButton(grid, text=self._ARROWS.get(key, label),
                               command=lambda k=key: self._play(k),
                               bg=COLORS["surface"], fg=COLORS["text"],
                               font=(F, 16, "bold"), width=_SQ, height=_SQ)
            b.grid(row=r, column=c, padx=4, pady=4)
            self._dir_btns[key] = b
            Tooltip(b, label + (" (simulated on stereo)" if key in _SIM else ""))
        _ctr = AnimatedButton(grid, text="\u25cf",
                              command=lambda: self._play("FC", source="C"),
                              bg=COLORS["surface"], fg=COLORS["text"],
                              font=(F, 16, "bold"), width=_SQ, height=_SQ)
        _ctr.grid(row=1, column=1, padx=4, pady=4)
        self._dir_btns["C"] = _ctr
        Tooltip(_ctr, "Center channel")
        for ci in range(3):
            grid.grid_columnconfigure(ci, weight=1, uniform="spkdirs")
        for ri in range(3):
            grid.grid_rowconfigure(ri, weight=1, uniform="spkdirs")
        ctl = tk.Frame(body, bg=COLORS["bg"])
        ctl.pack(pady=(12, 0))
        AnimatedButton(ctl, text="Sweep",
                       command=self._play_all,
                       bg=COLORS["surface"], fg=COLORS["text"],
                       font=(F, 9, "bold"), padx=22, pady=6).pack(side="left", padx=4)
        AnimatedButton(ctl, text="Stop",
                       command=self._stop_sweep,
                       bg=COLORS["surface"], fg=COLORS["text"],
                       font=(F, 9, "bold"), padx=22, pady=6).pack(side="left", padx=4)
        self._build_out_row()
        self.on_close(self._stop)
        self._poll_audio()

    def _poll_audio(self):
        """Tk thread only: drain completions the background playback
        thread(s) posted, then reschedule. Mirrors this file's existing
        worker/_poll pairs — the only Tk-safe way to react to a background
        thread's results."""
        self._audio_poll_after = None
        try:
            inbox, self._audio_inbox = self._audio_inbox, []
        except Exception:
            inbox = []
        for fn in inbox:
            try:
                fn()
            except Exception:
                pass
        try:
            self._audio_poll_after = self._dlg.after(80, self._poll_audio)
        except Exception:
            self._audio_poll_after = None

    def _build_out_row(self):
        """Test-target picker (NOT a default flip — playback goes to the
        picked device directly, the Windows default stays untouched)."""
        try:
            from app.audio_out import (list_render_endpoints as _list,
                                       default_render_id as _def)
        except Exception:
            return
        try:
            eps = _list() or []
        except Exception:
            eps = []
        actives = [e for e in eps if (e or {}).get("state") == 1
                   and (e or {}).get("id")]
        if len(actives) <= 1:
            return  # single output — no picker, same as before
        self._outs = actives
        try:
            _defid = _def()
        except Exception:
            _defid = ""
        names = [(e or {}).get("name", "") for e in actives]
        pick = ""
        for e in actives:
            if (e or {}).get("id", "") == _defid:
                pick = (e or {}).get("name", "")
                break
        self._out_var.set(pick or (names[0] if names else ""))
        try:
            import tkinter.ttk as _ttk
            tk.Label(self._out_row, text="Output Device:", font=(F, 9, "bold"),
                     bg=COLORS["bg"], fg=COLORS["text"]).pack(pady=(0, 4))
            _ttk.Combobox(self._out_row, textvariable=self._out_var,
                          values=names, state="readonly",
                          width=44, font=(F, 9)).pack()
            self._out_row.pack(pady=(16, 0))
        except Exception:
            pass

    def _target_index(self):
        """waveOut index for the picked output, or None (=> default)."""
        try:
            from app.audio_out import match_waveout as _match
        except Exception:
            return None
        want = (self._out_var.get() or "").strip()
        if not want and len(getattr(self, "_outs", []) or []) <= 1:
            return None
        try:
            if want:
                idx = _match(want)
                if idx is not None:
                    return idx
        except Exception:
            pass
        return None

    def _say(self, text):
        try:
            self._stat_lbl.config(text=text)
        except Exception:
            pass

    def _highlight(self, key, on):
        """Light one button green while its tone plays (Tk thread only —
        called from button handlers and the inbox drain, never from the
        audio worker). Each button lights only itself."""
        try:
            try:
                b = (getattr(self, "_dir_btns", None) or {}).get(key)
                if b is None:
                    return
                if on:
                    b.set_style(bg=COLORS["accent_green"], fg=COLORS["black"],
                                hover_bg=_hex_lerp(COLORS["accent_green"], "#FFFFFF", 0.08))
                else:
                    b.set_style(bg=COLORS["surface"], fg=COLORS["text"],
                                hover_bg=_hex_lerp(COLORS["surface"], "#FFFFFF", 0.08))
            except Exception:
                pass
        except Exception:
            pass

    def _clear_highlights(self):
        try:
            for k in list((getattr(self, "_dir_btns", None) or {})):
                self._highlight(k, False)
        except Exception:
            pass

    def _play_identity(self, side, on_done=None):
        """Left/right wiring check: same 660Hz tone, hard-panned.

        Separate from the 8-dir surround grid (which varies pitch to
        fake position on stereo). Here position comes ONLY from pan,
        so a reversed image means swapped wiring. Reuses the same
        background-thread + inbox completion path as _play."""
        try:
            from app.audio_out import (directional_wav as _wav,
                                       play_wav_on_device as _playdev,
                                       play_wav_bytes as _playdef)
        except Exception:
            self._say("Audio engine unavailable on this PC.")
            if on_done:
                on_done()
            return
        side = "L" if str(side).upper().startswith("L") else "R"
        label = "Left speaker" if side == "L" else "Right speaker"
        pan = -1.0 if side == "L" else 1.0
        self._say(f"Playing: {label}")
        try:
            data = _wav(pan, 0.0, freq_hz=660.0)
        except Exception:
            data = None
        if not data:
            self._say(f"Could not build {label} tone — check Sound Settings.")
            if on_done:
                on_done()
            return
        idx = self._target_index()
        self._highlight(side, True)

        def _worker():
            with self._play_lock:
                try:
                    if idx is None:
                        ok = _playdef(data)
                    else:
                        ok, _used_default = _playdev(data, idx)
                except Exception:
                    ok = False

                def _finish():
                    self._highlight(side, False)
                    if not ok:
                        self._say(f"Could not play {label} — check Sound Settings.")
                    if on_done:
                        on_done()
                try:
                    self._audio_inbox.append(_finish)
                except Exception:
                    pass

        threading.Thread(target=_worker, daemon=True).start()

    def _play(self, key, on_done=None, source=None):
        try:
            from app.audio_out import (DIRECTIONS as _DIRS,
                                       directional_wav as _wav,
                                       play_wav_on_device as _playdev,
                                       play_wav_bytes as _playdef)
        except Exception:
            self._say("Audio engine unavailable on this PC.")
            if on_done:
                on_done()
            return
        spec = next((d for d in _DIRS if d[0] == key), None)
        if spec is None:
            if on_done:
                on_done()
            return
        _k, label, pan, db, hz = spec
        self._say(f"Playing: {label}")
        try:
            data = _wav(pan, db, freq_hz=hz)
        except Exception:
            data = None
        if not data:
            self._say(f"Could not build {label} tone — check Sound Settings.")
            if on_done:
                on_done()
            return
        idx = self._target_index()
        hl = source or key
        self._highlight(hl, True)

        # CT-005 audit fix: the actual device write + duration wait (up to
        # several seconds, busy-sleep) now happens on a background thread
        # instead of the Tk callback thread, so the dialog stays responsive
        # (repaints, other buttons, Stop/close) while a tone plays. The
        # lock keeps only one tone playing at a time, matching the old
        # strictly-serial behavior; on_done (Sweep) is invoked only once
        # this tone has actually finished, so the sweep's real cadence
        # still matches the original — it doesn't just fire every 700ms
        # regardless of whether the previous tone is still playing.
        def _worker():
            with self._play_lock:
                try:
                    if idx is None:
                        ok = _playdef(data)
                    else:
                        ok, _used_default = _playdev(data, idx)
                except Exception:
                    ok = False

                def _finish():
                    self._highlight(hl, False)
                    if not ok:
                        self._say(f"Could not play {label} — check Sound Settings.")
                    if on_done:
                        on_done()
                # CT-005 refinement: post to the inbox (plain list append,
                # safe from any thread under the GIL) instead of calling
                # self._dlg.after() from this background thread directly.
                try:
                    self._audio_inbox.append(_finish)
                except Exception:
                    pass  # dialog already torn down — nothing left to update

        threading.Thread(target=_worker, daemon=True).start()

    def _play_all(self, idx=0):
        try:
            from app.audio_out import DIRECTIONS as _DIRS
        except Exception:
            return
        if idx >= len(_DIRS):
            return
        # CT-005 audit fix: previously scheduled the next step on a blind
        # 700ms timer fired right after STARTING the current tone — now
        # that _play returns immediately (audio moved off the Tk thread),
        # that would race ahead of tones still actually playing. Chain off
        # on_done (real completion) instead, so the sweep's cadence still
        # matches the original one-at-a-time behavior. _sweep_active lets
        # Stop interrupt the chain even between a tone finishing and its
        # next 700ms gap elapsing.
        self._sweep_active = True

        def _advance():
            if not self._sweep_active:
                return
            try:
                self._seq_after = self._dlg.after(700, lambda: self._play_all(idx + 1))
            except Exception:
                pass

        self._play(_DIRS[idx][0], on_done=_advance)

    def _stop_sweep(self):
        self._sweep_active = False
        if self._seq_after is not None:
            try:
                self._dlg.after_cancel(self._seq_after)
            except Exception:
                pass
            self._seq_after = None
        self._clear_highlights()

    def _stop(self):
        self._sweep_active = False
        if self._seq_after is not None:
            try:
                self._dlg.after_cancel(self._seq_after)
            except Exception:
                pass
            self._seq_after = None
        if self._audio_poll_after is not None:
            try:
                self._dlg.after_cancel(self._audio_poll_after)
            except Exception:
                pass
            self._audio_poll_after = None
