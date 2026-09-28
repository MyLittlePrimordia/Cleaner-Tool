"""SpeedTestDialog — extracted verbatim from app/gui.py (H12, batch 2).

One dialog per module, mirroring app/ui/widgets. Depends only on
app.ui below it, plus the app.utils helpers it already used; it
knows nothing
about any other dialog except where noted.
"""
from __future__ import annotations

import tkinter as tk
from tkinter import ttk, messagebox
from app.ui.base import ThemedModal, Tooltip
from app.ui.theme import COLORS, F, TAB_ACCENTS, _hex_lerp
from app.ui.widgets import AnimatedButton, AnimatedProgressBar, GaugeDial


class SpeedTestDialog(ThemedModal):
    """Speed Test popup (5th Tools card): Ookla-style staged flow.

    The popup NEVER auto-starts (user call) — it opens on a big GO
    button sitting where the gauge will be. One click runs ping, then
    the FULL download leg (needle climbs, headline tracks it), then
    the gauge RESETS and climbs again through the FULL upload leg
    (Ookla behavior), with a horizontal phase bar under the headline
    throughout. When the run ends the dial hides and GO returns to
    the stage, so re-running replays the whole animation. The four
    stat cells + usage stars below keep every landed number; the
    centered server line names the server only.

    Same popup dimensions as every other feature dialog (ThemedModal
    800x600 default — no custom size) and the same worker/hop/
    watchdog/cancel skeleton as DnsTesterDialog. Read-only: no
    Apply, no snapshot. Legs run sequentially (ping, then download,
    then upload — parallel legs would fight over the same pipe).
    Unreachable legs show '—', never a fake 0. No API keys on any
    path (app/speed_test.py). No description label (user call): the
    title + GO button are self-explanatory."""

    LEGS = (("ping", "Ping"), ("download", "Download"), ("upload", "Upload"),
            ("stability", "Stability"))

    # overall-progress spans per measured leg (phase bar under the
    # headline): ping/stability/warmup run indeterminate; the two
    # measured legs split the determinate range.
    _SPAN_DOWN = (0.12, 0.60)
    _SPAN_UP = (0.60, 1.0)

    def __init__(self, parent, app):
        self.app = app
        self._stop_token = [False]
        self._results = {}          # leg -> float | None (landed)
        self._finished = False
        self._scan_gen = 0
        self._watchdog_after = None
        self._row_values = {}
        self._server_name = None
        self._server_ms = None
        self._stage_mode = "go"     # "go" (idle/done) | "gauge" (running)
        self._active_leg = None     # leg the headline currently tracks
        self._phase_span = None     # (base, end) for the phase bar
        # NOTE: no size= passed — exact same dimensions as DNS/Health/etc.
        super().__init__(parent, title="Speed Test",
                         accent=TAB_ACCENTS["Tools"])
        body = self.body

        # Stage (fixed height so GO <-> gauge swaps never move the rows
        # below): the dial + headline + phase bar while running, the big
        # GO button + headline when idle/done. No description label —
        # the title says what this is and GO says what to do.
        stage = self._stage = tk.Frame(body, bg=COLORS["bg"], height=270)
        stage.pack(fill="x", pady=(6, 0))
        try:
            stage.pack_propagate(False)
        except Exception:
            pass
        self._gauge = GaugeDial(stage, accent=TAB_ACCENTS["Tools"],
                                width=340, height=200)
        self._go_btn = AnimatedButton(
            stage, text="GO", command=self._start_test,
            bg=TAB_ACCENTS["Tools"], fg=COLORS["black"],
            font=(F, 14, "bold"), padx=44, pady=14)
        # harness alias: the smoke suite drives hover/press + existence
        # through _retest_btn (kept, now pointing at the stage button).
        self._retest_btn = self._go_btn
        # NOTE (user call): no Tooltip on the GO button on purpose —
        # Tooltip binds <Enter>/<Leave> without add="+", which wipes
        # AnimatedButton's own hover/press animation binds.
        self._big_lbl = tk.Label(stage, text="—", font=(F, 18, "bold"),
                                 bg=COLORS["bg"], fg=COLORS["text"])
        self._phase_bar = AnimatedProgressBar(stage,
                                              accent=TAB_ACCENTS["Tools"])
        self._show_go()

        # usage suitability (Ookla-style): 4 icons with 5 stars under
        # each — Browsing / Gaming / Streaming / Video call. PNGs from
        # app/assets (bundled into the exe); emoji fallback when a file
        # is missing. Stars repaint from results; tooltips read
        # "Streaming — Great" / "Browsing — Slow".
        self._usage = {}
        self._usage_imgs = {}
        urow = tk.Frame(body, bg=COLORS["bg"])
        urow.pack(fill="x", pady=(2, 0))
        for ci in range(4):
            urow.grid_columnconfigure(ci, weight=1, uniform="usecols")
        try:
            from app.speed_test import USAGE_ORDER as _UO
        except Exception:
            _UO = ("browsing", "gaming", "streaming", "videocall")
        for ci, use in enumerate(_UO):
            cell = tk.Frame(urow, bg=COLORS["bg"])
            cell.grid(row=0, column=ci, sticky="n")
            img = self._load_usage_icon(use)
            if img is not None:
                icon_lbl = tk.Label(cell, image=img, bg=COLORS["bg"],
                                    bd=0, highlightthickness=0)
            else:
                try:
                    from app.speed_test import USAGE_FALLBACK_EMOJI as _UFE
                    _emo = _UFE.get(use, "•")
                except Exception:
                    _emo = "•"
                icon_lbl = tk.Label(cell, text=_emo, font=("Segoe UI Emoji", 20),
                                    bg=COLORS["bg"], fg=COLORS["text"])
            icon_lbl.pack(anchor="center")
            dots = tk.Label(cell, text="☆☆☆☆☆", font=(F, 8, "bold"),
                            bg=COLORS["bg"], fg=COLORS["subtext"])
            dots.pack(anchor="center")
            try:
                from app.speed_test import USAGE_TITLES as _UT
                _title = _UT.get(use, use)
            except Exception:
                _title = use
            tips = []
            for w in (cell, icon_lbl, dots):
                try:
                    tips.append(Tooltip(w, f"{_title} — testing…"))
                except Exception:
                    pass
            self._usage[use] = {"dots": dots, "tips": tips, "title": _title}
        self._reset_usage()

        # server line — CENTERED (user call: it sat left while the
        # gauge/headline above it are centered, which read as a layout
        # bug). Names the server only; the four stat cells below already
        # carry the numbers, so repeating them here was redundant.
        self._status_lbl = tk.Label(body, text="Starting…", font=(F, 9),
                                    bg=COLORS["bg"], fg=COLORS["subtext"],
                                    anchor="center", justify="center")
        self._status_lbl.pack(fill="x", pady=(2, 0))

        # single stats bar, no divider lines (user call): plain cells
        # on the dialog bg, even columns only.
        self._rows_body = tk.Frame(body, bg=COLORS["bg"])
        self._rows_body.pack(fill="x", pady=(4, 0))
        self._build_rows()

        self.on_close(self._cancel_test)
        # Return runs / re-runs (Escape still closes via ThemedModal).
        try:
            self._dlg.bind("<Return>", lambda _e: self._start_test())
        except Exception:
            pass
        # No auto-start (user call): the popup waits on GO. Initial
        # paint is the idle stage (button + dashed cells).
        self._set_status("Press GO to start.")

    def _load_usage_icon(self, use):
        """~28px PhotoImage for a usage cell (512px asset subsampled;
        power-of-two factors can't hit 28 from 512, so the factor is
        computed directly). None on any failure so the caller falls
        back to emoji. Ref kept by the caller (anti-GC, tab pattern)."""
        try:
            from app.speed_test import USAGE_FILES as _UF
            fname = _UF.get(use)
            if not fname:
                return None
            from app.utils import resolve_asset_path as _rap
            path = _rap(fname)
            if not path:
                return None
            img = tk.PhotoImage(file=path)
            w, h = img.width(), img.height()
            f = max(1, int(round(min(w, h) / 28.0)))
            if f > 1:
                img = img.subsample(f, f)
            self._usage_imgs[use] = img
            return img
        except Exception:
            return None

    def _reset_usage(self):
        """Testing state: hollow stars + 'testing…' tips."""
        try:
            from app.speed_test import USAGE_ORDER as _UO
        except Exception:
            _UO = ("browsing", "gaming", "streaming", "videocall")
        for use in _UO:
            handles = (self._usage or {}).get(use)
            if not handles:
                continue
            try:
                if handles["dots"].winfo_exists():
                    handles["dots"].config(text="☆☆☆☆☆", fg=COLORS["subtext"])
            except Exception:
                pass
            for tip in handles.get("tips", []):
                try:
                    tip.text = f"{handles['title']} — testing…"
                except Exception:
                    pass

    def _paint_usage(self, finished=False):
        """Repaint stars + tooltips from self._results. Missing legs read
        'testing…' mid-run, 'No result' once finished. Tk thread only."""
        try:
            from app.speed_test import (usage_stars as _us, usage_word as _uw,
                                        USAGE_ORDER as _UO)
        except Exception:
            return
        down = self._results.get("download")
        up = self._results.get("upload")
        ms = self._results.get("ping")
        if not finished and down is None and up is None and ms is None:
            return  # nothing landed yet — keep the testing state
        for use in _UO:
            handles = (self._usage or {}).get(use)
            if not handles:
                continue
            try:
                s = _us(use, down, up, ms)
                dots_txt = "★" * max(0, min(5, s)) + "☆" * (5 - max(0, min(5, s)))
                # star scale (user call): 1 red -> 5 blue, like the gauge
                if s >= 5:
                    fg = COLORS["accent_sky"]
                elif s == 4:
                    fg = COLORS["accent_green"]
                elif s == 3:
                    fg = COLORS["accent_yellow"]
                elif s == 2:
                    fg = _hex_lerp(COLORS["accent_yellow"],
                                   COLORS["accent_red"], 0.5)
                elif s >= 1:
                    fg = COLORS["accent_red"]
                else:
                    fg = COLORS["subtext"]
                if handles["dots"].winfo_exists():
                    handles["dots"].config(text=dots_txt, fg=fg)
                for tip in handles.get("tips", []):
                    try:
                        tip.text = f"{handles['title']} — {_uw(s)}"
                    except Exception:
                        pass
            except Exception:
                pass

    # ---- lifecycle (DnsTesterDialog skeleton, read-only) ---------------- #

    def _ui(self, fn, *args):
        # F10: same as the other dialogs — no winfo_exists off-thread.
        try:
            self._dlg.after(0, lambda: fn(*args))
        except Exception:
            pass

    def _cancel_test(self):
        try:
            self._stop_token[0] = True
        except Exception:
            pass
        try:
            if self._watchdog_after is not None:
                self._dlg.after_cancel(self._watchdog_after)
        except Exception:
            pass
        self._watchdog_after = None
        try:
            self._gauge._on_destroy()
        except Exception:
            pass

    def _set_status(self, text):
        try:
            if self._status_lbl.winfo_exists():
                self._status_lbl.config(text=text)
        except Exception:
            pass

    # ---- stage (GO <-> gauge swap, Ookla-style) ------------------------- #

    def _show_go(self):
        """Idle/done stage: dial + phase bar hide, GO returns to where
        the gauge was, headline keeps the last verdict (or '—'). The
        fixed-height stage means rows below never move. Tk thread only."""
        try:
            for w in (self._gauge, self._phase_bar,
                      self._go_btn, self._big_lbl):
                try:
                    w.pack_forget()
                except Exception:
                    pass
            self._go_btn.pack(anchor="center", pady=(64, 0))
            self._big_lbl.pack(anchor="center", pady=(10, 0))
            self._stage_mode = "go"
        except Exception:
            pass

    def _show_gauge(self):
        """Running stage: GO hides, the dial + headline + phase bar take
        the stage. Tk thread only."""
        try:
            for w in (self._gauge, self._phase_bar,
                      self._go_btn, self._big_lbl):
                try:
                    w.pack_forget()
                except Exception:
                    pass
            self._gauge.pack(anchor="center")
            self._big_lbl.pack(anchor="center")
            self._phase_bar.pack(fill="x", padx=120, pady=(8, 0))
            self._stage_mode = "gauge"
        except Exception:
            pass

    def _set_phase(self, phase, _gen=None):
        """Worker-driven phase switch (Tk thread only, generation-guarded).

        ping/stability/warmup/race run the phase bar indeterminate
        (no meaningful fraction exists yet); download parks the needle
        at zero and owns the first determinate span; upload RESETS the
        needle to zero with a fresh headline so it climbs again from
        nothing — the Ookla two-climb behavior the stage is built for."""
        if _gen is not None and _gen != getattr(self, "_scan_gen", _gen):
            return
        try:
            if phase == "download":
                self._active_leg = "download"
                self._phase_span = tuple(self._SPAN_DOWN)
                try:
                    self._gauge.reset()
                except Exception:
                    pass
                try:
                    self._phase_bar.set_indeterminate(False)
                    self._phase_bar.set_fraction(self._SPAN_DOWN[0])
                except Exception:
                    pass
            elif phase == "upload":
                self._active_leg = "upload"
                self._phase_span = tuple(self._SPAN_UP)
                try:
                    self._gauge.reset()
                except Exception:
                    pass
                try:
                    self._big_lbl.config(text="—")
                except Exception:
                    pass
                try:
                    self._phase_bar.set_indeterminate(False)
                    self._phase_bar.set_fraction(self._SPAN_UP[0])
                except Exception:
                    pass
            else:
                try:
                    self._phase_bar.set_indeterminate(True)
                except Exception:
                    pass
        except Exception:
            pass

    def _build_rows(self):
        """One single stats row (user call): Ping / Download / Upload /
        Stability evenly spaced in uniform columns — each cell stacks its
        title over its value. _row_values keeps the leg -> value-label
        contract so paints are untouched."""
        for w in list(self._rows_body.winfo_children()):
            try:
                w.destroy()
            except Exception:
                pass
        self._row_values = {}
        for ci, (leg, title) in enumerate(self.LEGS):
            self._rows_body.grid_columnconfigure(ci, weight=1, uniform="statcols")
            cell = tk.Frame(self._rows_body, bg=COLORS["bg_alt"])
            cell.grid(row=0, column=ci, sticky="nsew", padx=3)
            tk.Label(cell, text=title, font=(F, 8),
                     bg=COLORS["bg_alt"], fg=COLORS["subtext"]).pack(pady=(5, 0))
            val = tk.Label(cell, text="…", font=(F, 10, "bold"),
                           bg=COLORS["bg_alt"], fg=COLORS["subtext"])
            val.pack(pady=(0, 5))
            self._row_values[leg] = val

    def _start_test(self):
        try:
            self._stop_token[0] = True
        except Exception:
            pass
        token = self._stop_token = [False]
        self._results = {}
        self._finished = False
        self._scan_gen = getattr(self, "_scan_gen", 0) + 1
        gen = self._scan_gen
        try:
            if self._watchdog_after is not None:
                self._dlg.after_cancel(self._watchdog_after)
        except Exception:
            pass
        self._watchdog_after = None
        try:
            self._show_gauge()
            self._gauge.reset()
            self._big_lbl.config(text="—")
            try:
                self._phase_bar.set_indeterminate(True)
                self._phase_bar.set_fraction(0.0)
            except Exception:
                pass
            self._active_leg = None
            self._phase_span = None
            for leg in self._row_values:
                try:
                    self._row_values[leg].config(text="…", fg=COLORS["subtext"])
                except Exception:
                    pass
            self._reset_usage()
            self._set_status("Finding closest server…")
        except Exception:
            pass
        try:
            import threading as _th
            _th.Thread(target=self._test_worker, args=(token, gen),
                       daemon=True).start()
        except Exception:
            pass
        try:
            # 180s: full Ookla-style flow on a slow link (warmup + 3
            # down rounds + 3 up rounds + 10-sample stability) can
            # legitimately run past two minutes; the old 120s watchdog
            # could "time out" a test that was still measuring.
            self._watchdog_after = self._dlg.after(
                180000, lambda _g=gen: self._finish(stalled=True, _gen=_g))
        except Exception:
            pass

    def _test_worker(self, token, gen):
        def dead():
            # F10: token-only — close handlers already set token on the Tk
            # thread; winfo_exists here burned ~1s off-thread per call.
            try:
                return bool(token[0])
            except Exception:
                return True

        try:
            from app import speed_test as _st
            from app.downloader import has_network
        except Exception:
            self._ui(self._set_status, "Speed test unavailable.")
            self._ui(self._finish, False, gen)
            return
        if dead():
            return
        try:
            online = has_network(timeout=8.0)
        except Exception:
            online = False
        if not online:
            if dead():
                return
            for leg in ("ping", "download", "upload", "stability"):
                self._ui(self._paint_result, leg, None, gen)
            self._ui(self._finish, False, gen)
            return
        # — closest server race (median latency per candidate; the
        # winner is named in the centered server line, Ookla-style) — #
        if dead():
            return
        self._ui(self._set_status, "Finding closest server…")
        self._ui(self._set_phase, "race", gen)
        try:
            server_name, base_url, _race_ms = _st.race_endpoints(
                cancelled=lambda: dead())
        except Exception:
            server_name, base_url = "Cloudflare edge", _st.DOWNLOAD_URL
        self._server_name = server_name
        self._server_ms = _race_ms
        if dead():
            return
        # — ping (quick, no progress) — #
        self._ui(self._set_status, f"Server: {server_name} · testing ping…")
        self._ui(self._set_phase, "ping", gen)
        try:
            ms = _st.ping_ms(cancelled=lambda: dead())
        except Exception:
            ms = None
        if dead():
            return
        self._ui(self._paint_result, "ping", ms, gen)
        # — stability (jitter + loss from 10 ping samples, ~15s worst
        # case — same host, real samples, honest Nones) — #
        if dead():
            return
        self._ui(self._set_status, f"Server: {server_name} · testing stability…")
        self._ui(self._set_phase, "stability", gen)
        try:
            stab = _st.ping_stability(cancelled=lambda: dead())
        except Exception:
            stab = (None, None)
        if dead():
            return
        self._ui(self._paint_result, "stability", stab, gen)
        # — warmup (one throwaway GET: TLS + congestion window warm so
        # the measured rounds don't pay slow-start) — #
        if dead():
            return
        self._ui(self._set_status, f"Server: {server_name} · warming up…")
        self._ui(self._set_phase, "warmup", gen)
        try:
            _st.warmup(url=base_url, cancelled=lambda: dead())
        except Exception:
            pass
        # — download (FULL sustained parallel rounds, then FULL upload
        # rounds — sequential legs, never interleaved, Ookla-style) — #
        if dead():
            return
        self._ui(self._set_status, f"Server: {server_name} · testing download…")
        self._ui(self._set_phase, "download", gen)
        down = self._run_parallel_rounds(_st, dead, gen, direction="download",
                                         base_url=base_url)
        if dead():
            return
        # — upload — #
        self._ui(self._set_status, f"Server: {server_name} · testing upload…")
        self._ui(self._set_phase, "upload", gen)
        up = self._run_parallel_rounds(_st, dead, gen, direction="upload",
                                       base_url=base_url)
        if dead():
            return
        self._ui(self._finish, False, gen)

    def _run_parallel_rounds(self, st, dead, gen, direction="download",
                             base_url=None):
        """Parallel multi-stream rounds; paint live + final.

        One TCP stream cannot fill a fast pipe and a lone small sample
        is mostly handshake — so each round runs PARALLEL_STREAMS
        concurrent streams and aggregates wall-clock throughput, and the
        verdict is the LARGEST round that landed, never the first.
        Uploads report chunk progress through the same live gauge as
        downloads (the engine's POST body is progress-wrapped). Slow
        links stop after the small round to save data; fast links run
        bigger rounds for accuracy. Returns last good value or None."""
        import threading as _th
        import time as _time
        is_down = (direction == "download")
        up_url = st.UPLOAD_URL
        if is_down:
            rounds = list(st.DOWNLOAD_ROUNDS)
            gate_min = getattr(st, "ROUND_MIN_MBPS", 25.0)
            gate_fast = getattr(st, "ROUND_FAST_MBPS", 250.0)
        else:
            rounds = list(st.UPLOAD_ROUNDS)
            gate_min = getattr(st, "UP_MIN_MBPS", 10.0)
            gate_fast = getattr(st, "UP_FAST_MBPS", 100.0)
        best = None
        for n in rounds:
            if dead():
                return best
            t0 = _time.monotonic()
            lock = _th.Lock()
            last_hop = [0.0]
            agg_got = [0]
            planned = n * st.PARALLEL_STREAMS

            def _prog(got, total, _t0=t0, _leg=direction):
                try:
                    with lock:
                        agg_got[0] += got
                        now = _time.monotonic()
                        if now - last_hop[0] < 0.12:
                            return
                        last_hop[0] = now
                        el = max(0.05, now - _t0)
                        live = (agg_got[0] * 8.0) / el / 1_000_000.0
                        frac = min(1.0, agg_got[0] / max(1, planned))
                    self._ui(self._paint_live, _leg, live, frac, gen)
                except Exception:
                    pass

            try:
                if is_down:
                    url = base_url or st.DOWNLOAD_URL
                    v = st.download_parallel(n, streams=st.PARALLEL_STREAMS,
                                             cancelled=lambda: dead(),
                                             progress_cb=_prog, url=url)
                    try:
                        is_cf = st._is_cloudflare_url(url)
                    except Exception:
                        is_cf = (url == st.DOWNLOAD_URL)
                    if v is None and is_cf:
                        v = st.download_parallel(
                            n, streams=st.PARALLEL_STREAMS,
                            cancelled=lambda: dead(), progress_cb=_prog,
                            url=st.FALLBACK_DOWNLOAD_URL)
                else:
                    v = st.upload_parallel(n, streams=st.PARALLEL_STREAMS,
                                           cancelled=lambda: dead(),
                                           progress_cb=_prog, url=up_url)
            except Exception:
                v = None
            if dead():
                return best
            if v is not None:
                best = v
                self._ui(self._paint_result, direction, v, gen)
            if dead():
                return best
            # adaptive gates (data saver): the FIRST round always runs
            # (a lone small sample is handshake-heavy, never the verdict);
            # bigger rounds only when the pipe proves fast.
            if best is None:
                continue  # blocked round — try the next size once
            if n == rounds[0]:
                if best < gate_min:
                    break
            elif len(rounds) > 2 and n == rounds[1]:
                if best < gate_fast:
                    break
        return best

    # ---- paints (Tk thread only) ---------------------------------------- #

    def _paint_live(self, leg, live_mbps, frac, _gen=None):
        if _gen is not None and _gen != getattr(self, "_scan_gen", _gen):
            return
        try:
            self._gauge.set_value(max(0.0, float(live_mbps or 0.0)))
        except Exception:
            pass
        # the headline tracks whichever measured leg is climbing (fresh
        # climb per leg — the upload phase resets it first, so the
        # number on screen always belongs to the needle in motion).
        if leg in ("download", "upload"):
            try:
                from app.speed_test import format_mbps as _fmt
                self._big_lbl.config(text=_fmt(live_mbps))
            except Exception:
                pass
            # overall progress: the round frac mapped into this leg's
            # span of the phase bar (ping/stability/warmup have no span
            # and stay indeterminate — this hop only ever fires for the
            # two measured legs).
            try:
                span = self._phase_span
                if span and self._phase_bar.winfo_exists():
                    base, end = float(span[0]), float(span[1])
                    f = max(0.0, min(1.0, float(frac or 0.0)))
                    self._phase_bar.set_fraction(base + f * (end - base))
            except Exception:
                pass

    def _paint_result(self, leg, value, _gen=None):
        if _gen is not None and _gen != getattr(self, "_scan_gen", _gen):
            return
        self._results[leg] = value
        try:
            self._paint_usage(finished=False)
        except Exception:
            pass
        try:
            from app.speed_test import format_mbps as _fmb, format_ping as _fp
            lbl = self._row_values.get(leg)
            if lbl is not None and lbl.winfo_exists():
                if leg == "ping":
                    txt = _fp(value)
                elif leg == "stability":
                    # value is a (jitter_ms|None, loss_pct|None) pair —
                    # honest Nones read "—", never a fake 0.
                    try:
                        jit, loss = value
                    except Exception:
                        jit, loss = None, None
                    if jit is None and loss is None:
                        txt = "—"
                    elif jit is None:
                        txt = f"? · {loss:.0f}% loss"
                    elif loss is None:
                        txt = f"±{jit:.0f}ms · ?"
                    else:
                        txt = f"±{jit:.0f}ms · {loss:.0f}% loss"
                else:
                    txt = _fmb(value)
                if value is None or value == (None, None):
                    lbl.config(text=txt, fg=COLORS["accent_red"])
                elif leg == "stability":
                    try:
                        jit, loss = value
                        bad = (loss is not None and loss > 0) or (jit is not None and jit > 20)
                        lbl.config(text=txt, fg=COLORS["accent_yellow"] if bad else COLORS["text"])
                    except Exception:
                        lbl.config(text=txt, fg=COLORS["text"])
                else:
                    lbl.config(text=txt, fg=COLORS["text"])
        except Exception:
            pass
        if leg == "download":
            try:
                from app.speed_test import format_mbps as _fmb
                self._big_lbl.config(text=_fmb(value))
                self._gauge.set_value(value or 0.0)
            except Exception:
                pass
            # snap the phase bar to the end of the download span — the
            # upload phase re-parks it at its own span start.
            try:
                self._phase_bar.set_fraction(self._SPAN_DOWN[1])
            except Exception:
                pass
        elif leg == "upload" and value is not None:
            try:
                from app.speed_test import format_mbps as _fmb
                self._big_lbl.config(text=_fmb(value))
                self._gauge.set_value(value)
            except Exception:
                pass
            try:
                self._phase_bar.set_fraction(self._SPAN_UP[1])
            except Exception:
                pass

    def _finish(self, stalled=False, _gen=None):
        if self._finished:
            return
        if _gen is not None and _gen != getattr(self, "_scan_gen", _gen):
            return
        self._finished = True
        try:
            if self._watchdog_after is not None:
                self._dlg.after_cancel(self._watchdog_after)
        except Exception:
            pass
        self._watchdog_after = None
        try:
            from app.speed_test import format_mbps as _fmb
            down, up, ms = (self._results.get("download"),
                            self._results.get("upload"),
                            self._results.get("ping"))
            try:
                self._paint_usage(finished=True)
            except Exception:
                pass
            try:
                server = getattr(self, "_server_name", None) or "speed server"
            except Exception:
                server = "speed server"
            if down is None and up is None and ms is None:
                self._set_status("Couldn't reach the speed test servers — "
                                 "check your connection, then press GO."
                                 if not stalled else
                                 "Timed out — check your connection, then press GO.")
                try:
                    self._big_lbl.config(text="—")
                except Exception:
                    pass
                try:
                    self._phase_bar.set_indeterminate(False)
                    self._phase_bar.set_fraction(0.0)
                except Exception:
                    pass
                self._show_go()
                return
            try:
                self._gauge.set_value(down if down is not None
                                      else (up or 0.0))
                self._big_lbl.config(text=_fmb(down))
            except Exception:
                pass
            # Server line names the server ONLY (user call): the four
            # stat cells already carry the numbers — repeating them
            # here was redundant. Stays centered like the gauge above.
            missing = [n for n, v in (("download", down), ("upload", up),
                                      ("ping", ms)) if v is None]
            if missing:
                self._set_status(f"Server: {server} · "
                                 + ", ".join(missing) + " unavailable.")
            elif stalled:
                self._set_status(f"Server: {server} · timed out.")
            else:
                self._set_status(f"Server: {server} · complete.")
            # run over: park the shimmer, hide the dial, return GO to
            # the stage — the next click replays the whole animation.
            try:
                self._phase_bar.set_indeterminate(False)
                self._phase_bar.set_fraction(1.0)
            except Exception:
                pass
            self._show_go()
        except Exception:
            pass
