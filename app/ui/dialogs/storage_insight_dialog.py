"""StorageInsightDialog — extracted verbatim from app/gui.py (H12, batch 1).

One dialog per module, mirroring app/ui/widgets. Depends only on
app.ui below it, plus the app.utils helpers it already used; it
knows nothing
about any other dialog except where noted.
"""
from __future__ import annotations

import os

import tkinter as tk
import tkinter.font as tkfont
from tkinter import ttk, messagebox
from app.ui.base import ThemedModal, Tooltip
from app.ui.hardware.drives import _system_drive_root
from app.ui.theme import COLORS, F, TAB_ACCENTS
from app.ui.widgets import AnimatedButton, ScrollableRoundedPanel, ToggleSwitch
from app.utils import format_bytes


class StorageInsightDialog(ThemedModal):
    """Storage Insight (user-approved feature 3, 2026-09): 'what's eating
    my drives?' — machine-wide reclaimable-junk estimates beside a
    biggest-games list, in one screen with no scrolling.

    Left column — Reclaimable junk: one ToggleSwitch row per scan
    category (5 fixed rows, so the layout never grows). Sizes stream in
    live as the worker measures; 'Clean Selected' runs the checked
    categories' real tasks through the standard run engine (which ends
    on the Scorecard like any other run).

    Right column — Biggest games: read-only rows (drive chip + name +
    size + Open-folder button), top 5 by size plus a '+N more' summary
    line. Names display-truncated to one row (full name + path ride the
    row tooltip) so every row is a constant 39px and the columns area
    never outgrows the dialog — no scrollbar, ever. Deliberately NO
    delete button here: uninstalling a whole game is a human decision
    that belongs in its launcher.

    Honesty rules: the junk scan only covers the audited file-based
    allowlist (see app/storage_scan.py) and the dialog footnotes what it
    does NOT estimate; a cancelled scan labels its numbers partial.

    Chrome: ThemedModal (borderless rounded card, scrim, X, fixed size).
    The scan worker never touches widgets — every paint hops via after().
    """

    WIDTH = 800                 # fixed shared popup footprint (ThemedModal)
    # Five games, not eight (user call 2026-09: no scrollbar — the columns
    # area fits 5 fixed-height rows; the '+N more' line honestly counts
    # the rest, same as before).
    _MAX_GAMES = 5

    def __init__(self, parent, app):
        self.app = app
        self._scan_token = [True]   # replaced per scan; True = stopped
        self._scan_done = False
        self._cat_vars = {}         # title -> BooleanVar
        self._cat_sizes = {}        # title -> measured bytes (None = pending)
        self._cat_size_lbls = {}    # title -> size Label
        self._cat_keys = {}         # title -> member task keys
        self._games = []            # measured game dicts

        super().__init__(parent, title="Storage Insight",
                         accent=TAB_ACCENTS["Clean"])
        dlg = self._dlg
        body = self.body

        # intro line (title row carries the accent dot + name)
        tk.Label(body, text="See what's eating your drives.",
                 font=(F, 9), bg=COLORS["bg"],
                 fg=COLORS["subtext"], wraplength=680,
                 justify="left").pack(anchor="w")

        # hero: live reclaimable total
        hero = tk.Frame(body, bg=COLORS["bg"])
        hero.pack(fill="x", pady=(8, 0))
        self._hero_lbl = tk.Label(hero, text="Scanning…", font=(F, 20, "bold"),
                                  bg=COLORS["bg"], fg=COLORS["subtext"])
        self._hero_lbl.pack()
        tk.Label(hero, text="estimated reclaimable junk", font=(F, 9),
                 bg=COLORS["bg"], fg=COLORS["subtext"]).pack()

        # footer FIRST (packed side=bottom): the action buttons are
        # guaranteed visible no matter how tall the columns grow (user
        # bug report: long game names wrapped rows and squeezed Rescan /
        # Clean Selected out of the fixed 600px dialog). The columns
        # live in a scroll panel taking exactly the remaining space.
        foot = tk.Frame(body, bg=COLORS["bg"])
        foot.pack(side="bottom", fill="x", pady=(10, 6))
        # UI fix (Rescan clipping): status_lbl had no width limit, so a
        # long status line could eat into the room the three buttons
        # needed and push Rescan (packed closest to the label) partly
        # off the edge of the fixed-width card. Capping it to wrap onto
        # a second line instead guarantees the buttons always have their
        # full width available.
        self._status_lbl = tk.Label(foot, text="Starting scan…", font=(F, 9),
                                    bg=COLORS["bg"], fg=COLORS["subtext"],
                                    anchor="w", justify="left", wraplength=340)
        self._status_lbl.pack(side="left")
        self._clean_btn = AnimatedButton(
            foot, text="Clean Selected", command=self._clean_selected,
            bg=TAB_ACCENTS["Clean"], fg=COLORS["black"],
            font=(F, 9, "bold"), padx=16, pady=7)
        self._clean_btn.pack(side="right")
        self._clean_btn.set_enabled(False)
        self._bench_btn = AnimatedButton(foot, text="Benchmark", command=self._start_benchmark,
                                         bg=COLORS["surface"], fg=COLORS["text"],
                                         font=(F, 9, "bold"), padx=16,
                                         pady=7)
        self._bench_btn.pack(side="right", padx=(0, 8))
        Tooltip(self._bench_btn, "Measures this drive's real write/read speed (32MB temp file, removed afterwards)")
        AnimatedButton(foot, text="Rescan", command=self._start_scan,
                       bg=COLORS["surface"], fg=COLORS["text"],
                       font=(F, 9, "bold"), padx=16,
                       pady=7).pack(side="right", padx=(0, 8))

        # honest footnote: what the estimates do NOT cover (above footer)
        try:
            from app.storage_scan import SCAN_EXCLUDED_NOTE as _note
        except Exception:
            _note = ""
        if _note:
            tk.Label(body, text="Note: " + _note, font=(F, 8),
                     bg=COLORS["bg"], fg=COLORS["subtext"],
                     wraplength=self.WIDTH - 36, justify="left").pack(
                         side="bottom", anchor="w", pady=(8, 0))
        self._bench_lbl = tk.Label(body, text="", font=(F, 8),
                                   bg=COLORS["bg"], fg=COLORS["subtext"],
                                   anchor="w", justify="left")
        self._bench_lbl.pack(side="bottom", anchor="w")
        self._bench_token = [True]
        self._bench_busy = False

        # two symmetric columns in a scroll panel (uniform grid, like the
        # preset cards) — takes the remaining middle space; overflow
        # scrolls inside instead of pushing the footer out
        self._cols_panel = ScrollableRoundedPanel(body)
        self._cols_panel.pack(fill="both", expand=True, pady=(10, 0))
        cols = tk.Frame(self._cols_panel.inner, bg=COLORS["bg"])
        cols.pack(fill="x")
        cols.grid_columnconfigure(0, weight=1, uniform="insight")
        cols.grid_columnconfigure(1, weight=1, uniform="insight")

        left = tk.Frame(cols, bg=COLORS["bg"])
        left.grid(row=0, column=0, sticky="new", padx=(0, 6))
        tk.Label(left, text="Reclaimable junk", font=(F, 10, "bold"),
                 bg=COLORS["bg"], fg=COLORS["text"]).pack(anchor="w", pady=(0, 4))
        self._junk_body = tk.Frame(left, bg=COLORS["bg"])
        self._junk_body.pack(fill="x")

        right = tk.Frame(cols, bg=COLORS["bg"])
        right.grid(row=0, column=1, sticky="new", padx=(6, 0))
        tk.Label(right, text="Biggest games", font=(F, 10, "bold"),
                 bg=COLORS["bg"], fg=COLORS["text"]).pack(anchor="w", pady=(0, 4))
        self._games_body = tk.Frame(right, bg=COLORS["bg"])
        self._games_body.pack(fill="x")

        self._build_junk_rows()
        self._build_games_placeholder()
        self._refresh_cols_scroll()

        # closing cancels the scan (the base close hook runs it)
        self.on_close(self._cancel_scan)
        self._start_scan()

    def _cancel_scan(self):
        try:
            self._scan_token[0] = True
        except Exception:
            pass
        try:
            self._bench_token[0] = True
        except Exception:
            pass

    def _start_benchmark(self):
        """Isolated C: write/read benchmark (own thread + stop token —
        closing the dialog cancels it; temp file always removed)."""
        try:
            if self._bench_busy:
                self._bench_token[0] = True
                return
        except Exception:
            pass
        token = self._bench_token = [False]
        self._bench_busy = True
        try:
            self._bench_btn.config_text("Stop")
        except Exception:
            pass
        try:
            self._bench_lbl.config(text="Benchmarking C: (32MB)…")
        except Exception:
            pass

        def _prog(frac):
            try:
                pct = int(frac * 100)
                self._ui(lambda: self._bench_lbl.config(
                    text=f"Benchmarking C: (32MB)… {pct}%"))
            except Exception:
                pass

        def _worker():
            try:
                from app.storage_scan import write_benchmark
                w, r = write_benchmark(_system_drive_root(), 32,
                                       cancelled=lambda: token[0],
                                       progress_cb=_prog)
            except Exception:
                w, r = None, None
            def _land():
                self._bench_busy = False
                try:
                    self._bench_btn.config_text("Benchmark")
                except Exception:
                    pass
                try:
                    if token[0]:
                        self._bench_lbl.config(text="Benchmark stopped — temp file removed.")
                    elif w is None or r is None:
                        self._bench_lbl.config(text="Benchmark failed — drive busy or no temp space.")
                    else:
                        self._bench_lbl.config(
                            text=f"C: speed — write {w:.0f} MB/s · read {r:.0f} MB/s "
                                 "(sequential 32MB; SSDs usually 300+).")
                except Exception:
                    pass
            try:
                self._ui(_land)
            except Exception:
                pass

        try:
            import threading as _th
            _th.Thread(target=_worker, daemon=True).start()
        except Exception:
            self._bench_busy = False

    # ---- rows ---------------------------------------------------------- #

    def _build_junk_rows(self):
        """One ToggleSwitch row per scan category (fixed count of 5 —
        the layout never grows, so no scrollbar is ever needed)."""
        try:
            from app.storage_scan import SCAN_CATEGORIES as _cats
        except Exception:
            _cats = ()
        for title, keys, tip in _cats:
            self._cat_keys[title] = list(keys)
            var = tk.BooleanVar(value=True)
            var.trace_add("write", lambda *_: self._refresh_estimate())
            self._cat_vars[title] = var
            self._cat_sizes[title] = None
            outer = tk.Frame(self._junk_body, bg=COLORS["bg_alt"])
            inner = tk.Frame(outer, bg=COLORS["bg_alt"])
            inner.pack(fill="both", expand=True)
            ToggleSwitch(inner, var).pack(side="left", anchor="center",
                                          padx=(8, 2), pady=6)
            lbl = tk.Label(inner, text=title, font=(F, 9, "bold"),
                           bg=COLORS["bg_alt"], fg=COLORS["text"],
                           anchor="w", justify="left")
            lbl.pack(side="left", anchor="center")
            try:
                from app.tab_presets import TABS as _TABS
                _names = [t.label for t in _TABS.get("Clean", [])
                          if t.key in keys]
                Tooltip(lbl, tip + ("\n\nIncludes: " + ", ".join(_names)
                                    if _names else ""))
            except Exception:
                pass
            size = tk.Label(inner, text="…", font=(F, 9, "bold"),
                            bg=COLORS["bg_alt"], fg=COLORS["subtext"])
            size.pack(side="right", anchor="center", padx=(6, 8))
            self._cat_size_lbls[title] = size
            outer.pack(fill="x", pady=2)

    def _build_games_placeholder(self):
        self._games_ph = tk.Label(self._games_body, text="Scanning libraries…",
                                  font=(F, 9), bg=COLORS["bg"],
                                  fg=COLORS["subtext"])
        self._games_ph.pack(anchor="w", pady=6)

    # ---- scan worker ---------------------------------------------------- #

    def _ui(self, fn, *args):
        """Marshal a paint hop to the Tk thread (worker-safe).

        F10: never touch Tk from the worker (even winfo_exists burns
        ~1s off-thread) — schedule unconditionally; a destroyed dialog
        raises inside after() and is swallowed below."""
        try:
            self._dlg.after(0, lambda: fn(*args))
        except Exception:
            pass

    def _start_scan(self):
        # stop any in-flight scan, then reset rows to pending
        try:
            self._scan_token[0] = True
        except Exception:
            pass
        token = self._scan_token = [False]
        self._scan_done = False
        try:
            self._clean_btn.set_enabled(False)
        except Exception:
            pass
        for title in self._cat_sizes:
            self._cat_sizes[title] = None
            try:
                self._cat_size_lbls[title].config(text="…",
                                                  fg=COLORS["subtext"])
            except Exception:
                pass
        try:
            self._hero_lbl.config(text="Scanning…", fg=COLORS["subtext"])
            self._set_scan_status("Starting scan…")
        except Exception:
            pass
        for w in list(self._games_body.winfo_children()):
            try:
                w.destroy()
            except Exception:
                pass
        self._build_games_placeholder()
        try:
            import threading as _th
            _th.Thread(target=self._scan_worker, args=(token,),
                       daemon=True).start()
        except Exception:
            pass

    def _scan_worker(self, token):
        def dead():
            # F10: token-only — close handlers already set token on the Tk
            # thread; winfo_exists here burned ~1s off-thread per call.
            try:
                return bool(token[0])
            except Exception:
                return True

        try:
            from app.storage_scan import (
   SCAN_CATEGORIES, acquire_scan, release_scan, store_estimate,
   find_game_libraries, make_measure_ctx, measure_clean_tasks,
   sum_measured, count_unmeasured)
        except Exception:
            self._ui(self._set_scan_status, "Scan unavailable.")
            self._ui(self._scan_finished, True)
            return

        # Phase-6: the junk phase runs under the shared scan slot — a
        # Health dialog or nudge estimate never walks the same folders
        # at the same time. Give-up is honest ("scan busy"); the dialog
        # simply keeps whatever landed. The slot covers phase 1 only;
        # the (fast, cancellable) game sizing runs outside it.
        if not acquire_scan(cancelled=dead):
            self._ui(self._set_scan_status, "Another scan is running — Rescan in a moment.")
            self._ui(self._scan_finished, True)
            return
        try:
            ctx = make_measure_ctx(
                set_status=lambda m: self._ui(self._set_scan_status, m),
                cancelled=dead)
            # phase 1: junk, one category at a time (live row updates)
            grand = 0
            for title, keys, _tip in SCAN_CATEGORIES:
                if dead():
                    return
                self._ui(self._set_scan_status, f"Scanning {title}…")
                try:
                    sizes = measure_clean_tasks(ctx, keys)
                except Exception:
                    sizes = {}
                if dead():
                    return
                # MED-002: a category that could not be measured is unknown,
                # not zero. Keep the grand total honest: if any category is
                # unknown, the total is unknown, and the row says so rather
                # than painting "0 B" for something nobody looked at.
                cat_total = sum_measured(sizes)
                unknown = count_unmeasured(sizes)
                if grand is not None and cat_total is not None:
                    grand += cat_total
                else:
                    grand = None
                # Positional, NOT keyword: _ui() is (self, fn, *args) and
                # forwards with `fn(*args)`. Passing `unknown=` raised
                # TypeError inside the scan worker, which meant _scan_finished
                # never ran and the dialog hung on "Scanning..." forever.
                self._ui(self._paint_category, title,
                         cat_total if cat_total is not None else 0, unknown)
            # a complete, uncancelled junk walk publishes the shared
            # estimate cache (Health + Nudge quote it instead of re-walking)
            if not dead():
                store_estimate(grand)
        finally:
            release_scan()
        # phase 2: game libraries (read-only sizing)
        if not dead():
            self._ui(self._set_scan_status, "Sizing installed games…")
        try:
            games = find_game_libraries(
                cancelled=dead,
                on_game=lambda n: self._ui(self._set_scan_status,
                                           f"Sizing {n}…"))
        except Exception:
            games = []
        if dead():
            return
        cancelled = bool(token[0])
        self._ui(self._paint_games, games)
        self._ui(self._scan_finished, cancelled)

    # ---- paints (Tk thread only) ---------------------------------------- #

    def _set_scan_status(self, text):
        try:
            if self._status_lbl.winfo_exists():
                self._status_lbl.config(text=text)
        except Exception:
            pass

    def _paint_category(self, title, total, unknown=0):
        self._cat_sizes[title] = total
        try:
            lbl = self._cat_size_lbls.get(title)
            if lbl is not None and lbl.winfo_exists():
                if unknown:
                    # MED-002: "✓ clean" for a category we could not measure is
                    # the lie this finding is about. Say what is true.
                    lbl.config(
                        text=(f"{format_bytes(total)}+ ?" if total else "? unknown"),
                        fg=COLORS["accent_yellow"])
                elif total and total > 0:
                    lbl.config(text=format_bytes(total),
                               fg=COLORS["accent_green"])
                else:
                    lbl.config(text="✓ clean", fg=COLORS["accent_green"])
        except Exception:
            pass
        self._refresh_estimate()

    _game_name_font = None      # cached F,9-bold pixel measurer

    @classmethod
    def _short_game_name(cls, name):
        """Display-truncate a game name to one row (ellipsis + full name
        in the row tooltip). Pixel-exact via font.measure — character
        counts lie with proportional fonts, and an unwrapped long name
        is what grew rows to 2-3 lines and fed the scrollbar. 150px
        fits the name column at 760px dialog width on any DPI (the
        layout uses absolute pixels, so measure in the same units)."""
        try:
            name = name or "?"
            if cls._game_name_font is None:
                cls._game_name_font = tkfont.Font(family=F, size=9,
                                                  weight="bold")
            if cls._game_name_font.measure(name) <= 150:
                return name
            ell = "…"
            while len(name) > 1 and cls._game_name_font.measure(name + ell) > 150:
                name = name[:-1]
            return (name + ell) if name else "…"
        except Exception:
            try:
                return (name or "?")[:22]
            except Exception:
                return "?"

    def _paint_games(self, games):
        self._games = list(games or [])
        for w in list(self._games_body.winfo_children()):
            try:
                w.destroy()
            except Exception:
                pass
        if not self._games:
            # Audit note: verified this path is always reached (scan
            # errors already collapse to games=[] in _scan_worker, not a
            # stuck "Scanning…"). Wording enhanced per feedback to explain
            # the likely reason (custom/portable installs aren't in the
            # launcher libraries this scans) instead of a bare "not found".
            tk.Label(self._games_body, text="No game libraries detected on this PC.",
                     font=(F, 9, "bold"), bg=COLORS["bg"],
                     fg=COLORS["text"]).pack(anchor="w", pady=(6, 2))
            tk.Label(self._games_body,
                     text="Custom or portable installs outside a launcher's "
                          "library folder won't show up here.",
                     font=(F, 8), bg=COLORS["bg"], fg=COLORS["subtext"],
                     wraplength=320, justify="left").pack(anchor="w")
            return
        shown = self._games[:self._MAX_GAMES]
        for g in shown:
            outer = tk.Frame(self._games_body, bg=COLORS["bg_alt"])
            inner = tk.Frame(outer, bg=COLORS["bg_alt"])
            inner.pack(fill="both", expand=True)
            tk.Label(inner, text=g.get("drive", ""), font=(F, 8, "bold"),
                     bg=COLORS["bg_alt"], fg=COLORS["subtext"],
                     width=3, anchor="w").pack(side="left", anchor="center",
                                              padx=(8, 2), pady=6)
            lbl = tk.Label(inner, text=self._short_game_name(g.get("name", "?")),
                           font=(F, 9, "bold"),
                           bg=COLORS["bg_alt"], fg=COLORS["text"],
                           anchor="w", justify="left", wraplength=175)
            lbl.pack(side="left", anchor="center")
            try:
                Tooltip(lbl, f"{g.get('name', '?')}\n{g.get('launcher', '')} — {g.get('path', '')}\n"
                             "Read-only: uninstall from its launcher.")
            except Exception:
                pass
            AnimatedButton(inner, text="Open",
                           command=lambda p=g.get("path", ""): self._open_game_folder(p),
                           bg=COLORS["surface"], fg=COLORS["text"],
                           font=(F, 8), padx=10, pady=3).pack(
                               side="right", anchor="center", padx=(0, 8))
            tk.Label(inner, text=format_bytes(g.get("bytes", 0)),
                     font=(F, 9), bg=COLORS["bg_alt"],
                     fg=COLORS["text"]).pack(side="right", anchor="center",
                                             padx=(0, 6))
            outer.pack(fill="x", pady=2)
        rest = self._games[self._MAX_GAMES:]
        if rest:
            tk.Label(self._games_body,
                     text=f"+{len(rest)} more games ({format_bytes(sum(g.get('bytes', 0) for g in rest))} total)",
                     font=(F, 8), bg=COLORS["bg"],
                     fg=COLORS["subtext"]).pack(anchor="w", pady=(4, 0))
        self._refresh_cols_scroll()

    def _refresh_cols_scroll(self):
        """Settle the columns scroll panel after rows change (the panel
        takes exactly the remaining middle space; overflow scrolls
        inside instead of squeezing the footer)."""
        try:
            panel = getattr(self, "_cols_panel", None)
            if panel is not None:
                panel.refresh_scroll()
        except Exception:
            pass

    def _refresh_estimate(self):
        """Hero + footer estimate from checked categories (Tk thread)."""
        try:
            total = sum(self._cat_sizes.get(t, 0) or 0
                        for t, v in self._cat_vars.items() if v.get())
            if any(s is None for s in self._cat_sizes.values()):
                self._hero_lbl.config(text="Scanning…", fg=COLORS["subtext"])
            elif total > 0:
                self._hero_lbl.config(text=format_bytes(total),
                                      fg=COLORS["accent_green"])
            else:
                self._hero_lbl.config(text="All clean", fg=COLORS["accent_green"])
            try:
                self._clean_btn.config_text(
                    f"Clean Selected ({format_bytes(total)})" if total > 0
                    else "Clean Selected")
            except Exception:
                pass
        except Exception:
            pass

    def _scan_finished(self, cancelled):
        self._scan_done = True
        try:
            if cancelled:
                self._set_scan_status("Scan stopped — sizes shown are partial.")
            else:
                self._set_scan_status("Scan complete — pick what to clean.")
            self._refresh_estimate()
            self._clean_btn.set_enabled(True)
        except Exception:
            pass

    # ---- actions ---------------------------------------------------------- #

    def _open_game_folder(self, path):
        import os as _os
        try:
            if path and os.path.isdir(path):
                _os.startfile(path)
        except Exception:
            pass

    def _clean_selected(self):
        """Close the dialog, then run the checked categories' real tasks
        through the standard engine (progress bar + Stop + Scorecard).
        Tasks resolve live from the Clean table — tolerant, like the
        scheduler: unknown keys are skipped, never crash."""
        if not self._scan_done:
            return
        keys = []
        for title, var in self._cat_vars.items():
            try:
                if var.get():
                    keys.extend(self._cat_keys.get(title, []))
            except Exception:
                pass
        if not keys:
            try:
                self._set_scan_status("Nothing checked — turn on a category first.")
            except Exception:
                pass
            return
        try:
            from app.tab_presets import TABS as _TABS
            by_key = {t.key: t for t in _TABS.get("Clean", [])}
            tasks = [by_key[k] for k in keys if k in by_key]
        except Exception:
            tasks = []
        if not tasks:
            return
        self.close()
        try:
            self.app.run_tasks("Clean", tasks, mode="run")
        except Exception:
            pass

    def wait(self):
        """Modal wait (grab + wait_window) — split from __init__ like the
        Scorecard so the harness can assert on the built dialog."""
        try:
            self._dlg.grab_set()
        except Exception:
            pass
        try:
            self._dlg.wait_window()
        except Exception:
            pass
