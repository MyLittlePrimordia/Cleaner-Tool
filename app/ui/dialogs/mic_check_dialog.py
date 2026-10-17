"""MicCheckDialog — extracted verbatim from app/gui.py (H12, batch 2).

One dialog per module, mirroring app/ui/widgets. Depends only on
app.ui below it, plus the app.utils helpers it already used; it
knows nothing
about any other dialog except where noted.
"""
from __future__ import annotations

import tkinter as tk

from app.ui.widgets.dropdown import M3Dropdown
from tkinter import ttk, messagebox
from app.ui.base import Tooltip
from app.ui.dialogs._tool_page import ToolPage
from app.ui.hardware.capture import _capture_inputs, _default_capture_id
from app.ui.hardware.mmdev import _InputMeter, _MM_STATE_NAMES, _mm_vfn
from app.ui.theme import COLORS, F, TAB_ACCENTS
from app.ui.widgets.level_meter import LevelMeter


class MicCheckDialog(ToolPage):
    """Mic Check (user-requested Tools feature): one-glance
    verdict, the working microphones, the mic permission, and a live
    input-level bar. Strictly read-only — the fix path is the
    'Allow Microphone For Apps' tweak, linked below, never applied here.

    REDESIGN 2026-10: on the shared ToolPage shell — the banner takes the
    headline, the live verdict / level meter / dB read-out stay in
    self.content (they are numbers, not chrome), and the four actions are
    footer buttons. The capture and meter engine is untouched.
    """

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
        super().__init__(
            parent, title="Mic Check", accent=TAB_ACCENTS["Tweak"],
            scope="Read-only: it shows the working microphones and the live "
                  "level, it never changes a setting.",
            scroll=False)
        self.banner.set_state("\u25CF", TAB_ACCENTS["Tweak"])
        self.banner.set_title("Is your microphone working?")
        self.banner.set_sub("Speak and watch the level move.")
        body = self.content
        # Simplified, centred: status, picker, meter, readout, footer actions.
        wrap = tk.Frame(body, bg=COLORS["bg"])
        wrap.pack(expand=True, fill="x", padx=36)
        self._verdict_lbl = tk.Label(wrap, text="", font=(F, 15),
                                     bg=COLORS["bg"], fg=COLORS["text"])
        self._verdict_lbl.pack(pady=(0, 14))
        self._pick_box = tk.Frame(wrap, bg=COLORS["bg"])
        self._pick_box.pack(pady=(0, 20))
        # kept (unpacked) because the update code still writes to them
        self._other_note = tk.Label(wrap, text="")
        self._listen_lbl = tk.Label(wrap, text="")
        self._meter_w = LevelMeter(wrap)
        self._meter_w.pack(fill="x")
        self._readout = tk.Label(wrap, text="-inf dB", font=(F, 34),
                                 bg=COLORS["bg"], fg=COLORS["subtext"])
        self._readout.pack(pady=(14, 0))
        self._level_note = tk.Label(wrap, text="", font=(F, 10),
                                    bg=COLORS["bg"], fg=COLORS["subtext"])
        self._level_note.pack()
        # REDESIGN 2026-10: the four actions moved from a hand-built body
        # row onto the shared ToolPage footer, by role.
        self._rec_btn = self.add_button("Record 3s", self._record_take,
                                        kind="primary")
        Tooltip(self._rec_btn, "Records 3 seconds from the Windows default mic, then play it back to hear yourself")
        self._play_btn = self.add_button("Play back", self._play_take)
        self._play_btn.set_enabled(False)
        self.add_button("Refresh", self._refresh)
        self.add_button("Sound settings", self._open_sound)
        self._take = None
        self._recorder = None
        self._rec_busy = False
        self._rebuild_inputs()
        self.on_close(self._stop_tick)
        self._tick()

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
            _om = M3Dropdown(
                self._pick_box, textvariable=self._pick_var,
                values=names, state="readonly",
                width=42 if tk.TkVersion >= 8.6 else 38, font=(F, 9))
            _om.pack(side="left")
        except Exception:
            pass
        try:
            if _consent is not None and _consent != "Allow":
                self._verdict_lbl.config(
                    text="Microphone is blocked in Windows",
                    fg=COLORS["accent_red"])
            elif not actives:
                self._verdict_lbl.config(
                    text="No working microphone found",
                    fg=COLORS["accent_yellow"])
            else:
                self._verdict_lbl.config(text="Ready \u2014 say something",
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
            if level is None:
                self._level_note.config(text="Level meter unavailable on this PC.")
                try:
                    self._meter_w.set_level(-60.0, -60.0, False)
                    self._readout.config(text="n/a", fg=COLORS["subtext"])
                except Exception:
                    pass
            else:
                try:
                    import math as _math
                    # dBFS: 0 dB is digital full-scale (clip); aim peaks
                    # -12..-6 dB for Discord/OBS. Linear % hid clipping.
                    peak = max(0.0, min(1.0, float(level)))
                    db = 20.0 * _math.log10(peak) if peak > 0 else -60.0
                    db = max(-60.0, db)
                    # peak-hold with slow decay doubles as clip memory
                    hold = float(getattr(self, "_peak_hold_db", -60.0))
                    hold = db if db > hold else max(-60.0, hold - 1.5)
                    self._peak_hold_db = hold
                    clip = peak >= 0.99 or hold >= -1.0
                    self._meter_w.set_level(db, hold, clip)
                    col = (COLORS["accent_green"] if db < -12 else
                           COLORS["accent_yellow"] if db < -3 else
                           COLORS["accent_red"])
                    self._readout.config(
                        text=("-inf dB" if db <= -59.0 else "%.0f dB" % db),
                        fg=col if db > -59.0 else COLORS["subtext"])
                    if clip:
                        self._level_note.config(
                            text="CLIPPING at %.0f dB peak - turn the mic "
                                 "gain down." % hold)
                    elif db <= -59.0:
                        self._level_note.config(text="Listening...")
                    elif hold < -24.0:
                        self._level_note.config(
                            text="Peak %.0f dB - very quiet, raise the gain."
                                 % hold)
                    elif hold <= -6.0:
                        self._level_note.config(
                            text="Peak %.0f dB - signal OK. Aim for -12 to "
                                 "-6 dB peaks." % hold)
                    else:
                        self._level_note.config(
                            text="Peak %.0f dB - hot, back off a little."
                                 % hold)
                except Exception:
                    pass
        except Exception:
            pass
        try:
            self._tick_after = self._dlg.after(self._TICK_MS, self._tick)
        except Exception:
            self._tick_after = None
