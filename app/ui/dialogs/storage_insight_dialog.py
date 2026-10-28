"""StorageInsightDialog — extracted verbatim from app/gui.py (H12, batch 1).

One dialog per module, mirroring app/ui/widgets. Depends only on
app.ui below it, plus the app.utils helpers it already used; it
knows nothing
about any other dialog except where noted.
"""
from __future__ import annotations

import os

import tkinter as tk

from app.ui.widgets.rounded_card import RoundedCard
import tkinter.font as tkfont
from app.ui.base import Tooltip
from app.ui.dialogs._tool_page import ToolPage, fit_label
from app.ui.hardware.drives import _system_drive_root
from app.ui.theme import (
    COLORS, F, RADIUS_SURFACE, TAB_ACCENTS, TYPE, _measure_font, SPACE_1, SPACE_2,
    SPACE_3,
)
from app.ui.metrics import (
    CARD_BODY_INSET, CONTROL_GAP, GROUP_GAP, LABEL_GAP, TEXT_NUDGE, TIGHT_GAP,
)
from app.ui.widgets import AnimatedButton, ScrollableRoundedPanel, ToggleSwitch
from app.utils import format_bytes


class StorageInsightDialog(ToolPage):
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

    Chrome: ToolPage (borderless rounded card, scrim, X, proportional
    size). The scan worker never touches widgets — every paint hops via
    the shared dispatcher.

    REDESIGN 2026-10: on the shared ToolPage shell — the 30pt hero
    number and its "can be freed up" caption are the Header banner
    (headline = the live total), and status + Rescan/Benchmark/Clean
    Selected are the shared footer.
    """

    # Five games, not eight (user call 2026-09: no scrollbar — the columns
    # area fits 5 fixed-height rows; the '+N more' line honestly counts
    # the rest, same as before).
    _MAX_GAMES = 5

    def __init__(self, parent, app):
        self.app = app
        self._scan_token = [True]   # replaced per scan; True = stopped
        self._scan_done = False
        #: (done, total) of the junk-category walk, or None outside it. The
        #: status bar shows a real fraction while this is set (AUDIT TV-006);
        #: per-task messages keep the same fill instead of resetting it.
        self._scan_step = None
        self._cat_vars = {}         # title -> BooleanVar
        self._cat_sizes = {}        # title -> measured bytes (None = pending)
        self._cat_unknown = {}      # title -> members that declined to look
        self._cat_size_lbls = {}    # title -> size Label
        self._cat_refits = {}       # title -> callable: re-fit the title
        self._cat_keys = {}         # title -> member task keys
        #: title -> per-member rows from the walk that produced the total
        #: (U-003): label, description, its own size, and any skip reason.
        self._cat_members = {}
        self._games = []            # measured game dicts

        accent = TAB_ACCENTS["Clean"]
        super().__init__(parent, title="Storage Insight", accent=accent,
                         scope="Estimates only what it can measure — a "
                               "cancelled scan says so.",
                         scroll=False)
        body = self.content

        # hero: live reclaimable total, now the banner headline
        self.banner.set_state("\U0001F4BE", accent)
        self.banner.set_title("Scanning…", COLORS["subtext"])
        self.banner.set_sub("can be freed up")

        # REDESIGN 2026-10: shell footer — Clean Selected is the one primary
        # action; Rescan and Benchmark are secondary. The scan status lives
        # in the shared StatusLine.
        self.status.stop("Starting scan…")
        self._clean_btn = self.add_button(
            "Clean Selected", self._clean_selected, kind="primary")
        self._clean_btn.set_enabled(False)
        self._bench_btn = self.add_button(
            "Benchmark", self._start_benchmark, kind="secondary")
        Tooltip(self._bench_btn, "Measures this drive's real write/read speed (32MB temp file, removed afterwards)")
        self.add_button("Rescan", self._start_scan, kind="secondary")

        # honest footnote: what the estimates do NOT cover
        try:
            from app.storage_scan import SCAN_EXCLUDED_NOTE as _note
        except Exception:
            _note = ""
        if _note:
            try:
                Tooltip(self.status.caption, _note)
            except Exception:
                pass
        self._bench_lbl = tk.Label(body, text="", font=TYPE["caption"],
                                   bg=COLORS["bg"], fg=COLORS["subtext"],
                                   anchor="w", justify="left")
        self._bench_lbl.pack(side="bottom", anchor="w")
        self._bench_token = [True]
        self._bench_busy = False

        # two symmetric columns in a scroll panel (uniform grid, like the
        # preset cards) — takes the remaining middle space; overflow
        # scrolls inside instead of pushing the footer out
        self._cols_panel = ScrollableRoundedPanel(body)
        self._cols_panel.pack(fill="both", expand=True, pady=(GROUP_GAP, 0))
        cols = tk.Frame(self._cols_panel.inner, bg=COLORS["bg"])
        cols.pack(fill="x")
        cols.grid_columnconfigure(0, weight=1, uniform="insight")
        cols.grid_columnconfigure(1, weight=1, uniform="insight")

        left = tk.Frame(cols, bg=COLORS["bg"])
        left.grid(row=0, column=0, sticky="new", padx=(0, CONTROL_GAP))
        tk.Label(left, text="Reclaimable junk", font=TYPE["label_large"],
                 bg=COLORS["bg"], fg=COLORS["text"]).pack(anchor="w", pady=(0, SPACE_1))
        self._junk_body = tk.Frame(left, bg=COLORS["bg"])
        self._junk_body.pack(fill="x")
        # N-002: the measured categories, ordered, in one window. A text
        # button rather than a fourth footer action: the footer already
        # overflows its row at the minimum window with three buttons, and this
        # belongs beside the numbers it orders anyway.
        self._plan_btn = AnimatedButton(
            left, text="Reclaim plan  \u203a", command=self._open_reclaim_plan,
            role="text", size="sm")
        self._plan_btn.pack(anchor="w", pady=(SPACE_2, 0))
        self._plan_btn.set_enabled(False)
        Tooltip(self._plan_btn,
                "The measured categories, biggest first, plus the tools that "
                "need their own look. Nothing runs until you pick a step.")

        right = tk.Frame(cols, bg=COLORS["bg"])
        right.grid(row=0, column=1, sticky="new", padx=(CONTROL_GAP, 0))
        tk.Label(right, text="Biggest games", font=TYPE["label_large"],
                 bg=COLORS["bg"], fg=COLORS["text"]).pack(anchor="w", pady=(0, SPACE_1))
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
            self._cat_unknown[title] = 0
            outer = RoundedCard(self._junk_body, fill=COLORS["bg_alt"],
                                bg=COLORS["bg"], radius=RADIUS_SURFACE, min_height=54)
            inner = outer.body
            ToggleSwitch(inner, var).pack(side="left", anchor="center",
                                          padx=(SPACE_3, CONTROL_GAP), pady=LABEL_GAP)
            lbl = tk.Label(inner, text=title, font=TYPE["card_title"],
                           bg=COLORS["bg_alt"], fg=COLORS["text"],
                           anchor="w", justify="left")
            lbl.pack(side="left", anchor="center")
            try:
                from app.tab_presets import TABS as _TABS
                _names = [t.label for t in _TABS.get("Clean", [])
                          if t.key in keys]
                Tooltip(lbl, tip + ("\n\nIncludes: " + ", ".join(_names)
                                    if _names else "")
                        + "\n\nClick the name or the size to see each "
                          "cleaner's own number.")
            except Exception:
                pass
            # U-003: the row is a door, not just a label. Clicking the title
            # or the size opens the per-cleaner breakdown for THIS walk; the
            # switch stays a switch (it is what ticks the category).
            for _w in (lbl,):
                try:
                    _w.bind("<Button-1>",
                            lambda _e, _t=title: self._show_category(_t))
                    _w.config(cursor="hand2")
                except Exception:
                    pass
            size = tk.Label(inner, text="…", font=TYPE["card_title"],
                            bg=COLORS["bg_alt"], fg=COLORS["subtext"],
                            anchor="e")
            size.pack(side="right", anchor="center", padx=(CONTROL_GAP, CARD_BODY_INSET))
            self._cat_size_lbls[title] = size
            try:
                size.bind("<Button-1>",
                          lambda _e, _t=title: self._show_category(_t))
                size.config(cursor="hand2")
            except Exception:
                pass
            # Width-fit pass 2026-10: the title is packed before the size pill
            # on the right, so at the minimum window a long title (237px of a
            # 241px budget) left the pill 4px and its text — the "…"
            # placeholder, later a real "12.4 GB" or "✓ clean" — was sheared
            # off. Fit the title to the room the switch and the pill need,
            # measured from the FONT and the pill's own text: a widget's
            # reqwidth lags a layout pass behind, and trusting it here left
            # the pill 8px short. The pill re-runs the fit whenever its text
            # grows (see _paint_category).
            _fm11 = _measure_font(inner.winfo_toplevel(), TYPE["card_title"])

            def _pill_room(_fm=_fm11, _lbl=size):
                try:
                    need = _fm.measure(str(_lbl.cget("text"))) + 2 + 20
                except Exception:
                    need = 120
                return 70 + need          # switch column + pill

            refit = fit_label(inner, lbl, title, reserve=_pill_room)
            self._cat_refits[title] = refit
            try:
                size.bind("<Configure>", refit, add="+")
            except Exception:
                pass
            outer.pack(fill="x", pady=TIGHT_GAP)

    def _build_games_placeholder(self):
        self._games_ph = tk.Label(self._games_body, text="Scanning libraries…",
                                  font=TYPE["caption"], bg=COLORS["bg"],
                                  fg=COLORS["subtext"])
        self._games_ph.pack(anchor="w", pady=CONTROL_GAP)

    # ---- scan worker ---------------------------------------------------- #

    def _ui(self, fn, *args):
        """Marshal a paint hop to the Tk thread (worker-safe).

        BUG-001: this used to be `self._dlg.after(0, lambda: fn(*args))`
        under a bare `except: pass`. Calling after() from a worker IS a Tk
        call on a foreign thread: on Python 3.14 it raises
        RuntimeError("main thread is not in main loop") every time, and the
        bare except swallowed it -- so the dialog silently stopped painting
        and then blamed the network. The old docstring stated the very rule
        this code was breaking.

        self._dispatch is the TkDispatcher ThemedModal owns, so close()
        stops it. post() also runs inline when the caller is already on the
        Tk thread, which keeps single-update() smoke checks working."""
        self._dispatch.post(lambda: fn(*args))

    def _start_scan(self):
        # stop any in-flight scan, then reset rows to pending
        try:
            self._scan_token[0] = True
        except Exception:
            pass
        token = self._scan_token = [False]
        self._scan_done = False
        self._scan_step = None
        try:
            self._clean_btn.set_enabled(False)
        except Exception:
            pass
        for title in self._cat_sizes:
            self._cat_sizes[title] = None
            self._cat_unknown[title] = 0
            try:
                self._cat_size_lbls[title].config(text="…",
                                                  fg=COLORS["subtext"])
            except Exception:
                pass
        try:
            self.banner.set_title("Scanning…", COLORS["subtext"])
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
   SCAN_CATEGORIES, acquire_scan, category_members, release_scan,
   store_estimate, publish_junk_finding, find_game_libraries,
   make_measure_ctx, measure_clean_tasks, sum_measured, sum_known,
   count_unmeasured)
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
            declined = []          # reasons the rows skipped, for the cache
            total_cats = len(SCAN_CATEGORIES)
            for idx, (title, keys, _tip) in enumerate(SCAN_CATEGORIES, 1):
                if dead():
                    return
                # Determinate: one category is one step of the walk, so the
                # bar shows how much is LEFT instead of looping (TV-006).
                self._ui(self._set_scan_step, idx, total_cats)
                self._ui(self._set_scan_status, f"Scanning {title}…")
                refused = {}
                try:
                    sizes = measure_clean_tasks(ctx, keys, refused)
                except Exception:
                    sizes = {}
                if dead():
                    return
                # MED-002: a category that could not be measured is unknown,
                # not zero. Keep the grand total honest: if any category is
                # unknown, the total is unknown, and the row says so rather
                # than painting "0 B" for something nobody looked at.
                # A cleaner that declined to look (Office/Steam live) is a
                # known skip, not a hole: its row says "X + ?" and the
                # headline stays a number. Only a real failure makes the
                # total unknown.
                cat_total = sum_measured(sizes, refused)
                unknown = count_unmeasured(sizes)
                # The rows say "+ ?" for a skip; the shared cache needs the
                # same fact, or Health might quote this walk's number as if
                # every folder had been looked at.
                declined.extend(refused.values())
                if grand is not None and cat_total is not None:
                    grand += cat_total
                else:
                    grand = None
                # Positional, NOT keyword: _ui() is (self, fn, *args) and
                # forwards with `fn(*args)`. Passing `unknown=` raised
                # TypeError inside the scan worker, which meant _scan_finished
                # never ran and the dialog hung on "Scanning..." forever.
                #
                # The row gets the KNOWN sum plus the count of members that
                # declined to look (Office/Steam/FiveM running), so one skip
                # reads "12.4 GB + ?" instead of blanking five real numbers -
                # and never as 0, which claimed the category was already clean.
                # U-003: the per-cleaner rows ride along with the total, so
                # the row can open the breakdown of the SAME walk rather than
                # measuring again (or reading the shared cache, which may be
                # another dialog's walk).
                self._ui(self._paint_category, title,
                         sum_known(sizes), unknown,
                         category_members(keys, sizes, refused))
            # a complete, uncancelled junk walk publishes the shared
            # estimate cache (Health + Nudge quote it instead of re-walking)
            if not dead():
                store_estimate(grand, declined)
                # TX-002: a finished walk also tells the rest of the app what
                # it found. Only complete walks publish; a cancel or a total
                # of None would be a claim the walk cannot make.
                try:
                    publish_junk_finding(grand, total_cats, len(declined))
                except Exception:
                    pass
        finally:
            release_scan()
        # phase 2: game libraries (read-only sizing) - no count to show, so
        # back to the honest indeterminate rail.
        if not dead():
            self._ui(self._set_scan_step, 0, 0)
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

    def _show_category(self, title):
        """Open the per-cleaner breakdown for one category row (U-003)."""
        members = self._cat_members.get(title)
        if not members:
            # Before the walk reaches this row there is nothing measured to
            # break down; say so instead of opening an empty list that reads
            # like "this category is empty".
            try:
                self.status.stop("Still measuring this category…")
            except Exception:
                pass
            return
        try:
            from app.ui.dialogs.storage_category_dialog import (
                StorageCategoryDialog)
            StorageCategoryDialog(self, title, members,
                                  total=self._cat_sizes.get(title)).wait()
        except Exception:
            pass

    # ---- paints (Tk thread only) ---------------------------------------- #

    def _set_scan_status(self, text):
        try:
            step = self._scan_step
            if step:
                # Inside the category walk: keep the determinate fill and put
                # this message in front of the count, so a per-task line like
                # "Scanning Edge cache…" does not blank the progress bar.
                self.status.progress(step[0], step[1], text)
            else:
                self.status.stop(text)
        except Exception:
            pass

    def _set_scan_step(self, done, total):
        """Tk-side: enter/leave the determinate part of the scan."""
        self._scan_step = (done, total) if total else None
        try:
            if self._scan_step:
                self.status.progress(done, total, "Scanning")
        except Exception:
            pass

    def _paint_category(self, title, total, unknown=0, members=None):
        self._cat_sizes[title] = total
        self._cat_unknown[title] = unknown
        if members is not None:
            self._cat_members[title] = list(members)
        try:
            lbl = self._cat_size_lbls.get(title)
            if lbl is not None and lbl.winfo_exists():
                if unknown:
                    # MED-002: "✓ clean" for a category we could not measure is
                    # the lie this finding is about. Say what is true.
                    lbl.config(
                        text=(f"{format_bytes(total)} + ?" if total
                              else "? unknown"),
                        fg=COLORS["accent_yellow"])
                elif total and total > 0:
                    lbl.config(text=format_bytes(total),
                               fg=COLORS["accent_green"])
                else:
                    lbl.config(text="✓ clean", fg=COLORS["accent_green"])
                # the pill's text is a different width now, and a config() that
                # keeps the widget's geometry identical sends no <Configure>:
                # re-fit the title once this layout pass has settled, so the
                # pill is never the thing that gets shaved
                refit = self._cat_refits.get(title)
                if refit is not None:
                    lbl.after_idle(refit)
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
                     font=TYPE["label_medium"], bg=COLORS["bg"],
                     fg=COLORS["text"]).pack(anchor="w", pady=(CONTROL_GAP, TEXT_NUDGE))
            tk.Label(self._games_body,
                     text="Custom or portable installs outside a launcher's "
                          "library folder won't show up here.",
                     font=TYPE["caption"], bg=COLORS["bg"], fg=COLORS["subtext"],
                     wraplength=320, justify="left").pack(anchor="w")
            return
        shown = self._games[:self._MAX_GAMES]
        for g in shown:
            outer = RoundedCard(self._games_body, fill=COLORS["bg_alt"],
                                bg=COLORS["bg"], radius=RADIUS_SURFACE, min_height=54)
            inner = outer.body
            drive_lbl = tk.Label(inner, text=g.get("drive", ""),
                                 font=TYPE["label_medium"],
                                 bg=COLORS["bg_alt"], fg=COLORS["subtext"],
                                 anchor="w")
            drive_lbl.pack(side="left", anchor="center", padx=(SPACE_2, TEXT_NUDGE),
                           pady=CONTROL_GAP)
            _gname = self._short_game_name(g.get("name", "?"))
            lbl = tk.Label(inner, text=_gname,
                           font=TYPE["card_title"],
                           bg=COLORS["bg_alt"], fg=COLORS["text"],
                           anchor="w", justify="left")
            lbl.pack(side="left", anchor="center")
            try:
                Tooltip(lbl, f"{g.get('name', '?')}\n{g.get('launcher', '')} — {g.get('path', '')}\n"
                             "Read-only: uninstall from its launcher.")
            except Exception:
                pass
            open_btn = AnimatedButton(
                inner, text="Open",
                command=lambda p=g.get("path", ""): self._open_game_folder(p),
                role="outlined", size="sm")
            open_btn.pack(side="right", anchor="center", padx=(0, SPACE_3))
            size_lbl = tk.Label(inner, text=format_bytes(g.get("bytes", 0)),
                                font=TYPE["body_medium"], bg=COLORS["bg_alt"],
                                fg=COLORS["text"], anchor="e")
            size_lbl.pack(side="right", anchor="center", padx=(0, SPACE_2))
            # Width-fit pass 2026-10: same shape as the junk rows above — the
            # name is packed before the drive chip, the size and the Open
            # button, so at the minimum window those were handed the leftovers
            # (the size was cut to a couple of pixels). The name now fits the
            # room that is left, so at every width the name is the one thing
            # that shortens (with an ellipsis) and the chips keep their pixels.
            #
            # All three are computed from the FONT and the text rather than
            # read back from the widgets: a widget's request is only
            # recomputed on a layout pass, and a reserve taken from a
            # half-updated one is exactly how a chip ends up clipped. Each
            # term carries 6px of slack, because a Label renders its text a
            # few pixels wider than tkfont.Font.measure() reports it
            # (measured: 93 vs 89 for "114.98 GB"), and a Label's
            # `width=<chars>` box uses Tk's own average character width, which
            # is not any single glyph's width — one more reason to size from
            # the text here.
            # drive = text + padding + pack (8,2) + 6; Open = text + the
            # button's 6px lead-in + 14px padx each side + pack (0,12) + 6;
            # size = text + padding + pack (0,8) + 6.
            _fm9 = _measure_font(inner.winfo_toplevel(), TYPE["label_medium"])
            _fm10 = _measure_font(inner.winfo_toplevel(), TYPE["body_medium"])
            _w_drive = _fm9.measure(g.get("drive", "")) + 2 + 8 + 2 + 6
            _w_open = _fm9.measure("Open") + 6 + 28 + 12 + 6
            _w_size = _fm10.measure(format_bytes(g.get("bytes", 0))) + 2 + 8 + 6

            # Default-bound: a closure over the loop locals would make EVERY
            # row measure its reserve from the LAST row's widths (B023).
            def _fixed_room(_d=_w_drive, _o=_w_open, _s=_w_size):
                return _d + _o + _s

            _fit_name = fit_label(inner, lbl, _gname, reserve=_fixed_room)
            try:
                inner.bind("<Configure>", _fit_name, add="+")
                # one deferred pass: the first Configure can land while the
                # row is still being sized, and this row's exactness is the
                # difference between a clipped chip and a whole one
                inner.after_idle(_fit_name)
            except Exception:
                pass
            outer.pack(fill="x", pady=TIGHT_GAP)
        rest = self._games[self._MAX_GAMES:]
        if rest:
            tk.Label(self._games_body,
                     text=f"+{len(rest)} more games ({format_bytes(sum(g.get('bytes', 0) for g in rest))} total)",
                     font=TYPE["caption"], bg=COLORS["bg"],
                     fg=COLORS["subtext"]).pack(anchor="w", pady=(SPACE_1, 0))
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
            checked = [t for t, v in self._cat_vars.items() if v.get()]
            total = sum(self._cat_sizes.get(t, 0) or 0 for t in checked)
            unknown = sum(1 for t in checked if self._cat_unknown.get(t))
            # "12.4 GB + ?" for a category where a cleaner declined to look:
            # the number is what WAS measured, the "?" is what nobody looked
            # at. Never "All clean" - that was the old claim, and it graded a
            # skipped Office cache as an already-empty folder.
            partial = (format_bytes(total) + " + ?") if total else "? unknown"
            if any(s is None for s in self._cat_sizes.values()):
                self.banner.set_title("Scanning…", COLORS["subtext"])
            elif unknown:
                self.banner.set_title(partial, COLORS["accent_yellow"])
            elif total > 0:
                self.banner.set_title(format_bytes(total),
                                      COLORS["accent_green"])
            else:
                self.banner.set_title("All clean", COLORS["accent_green"])
            try:
                if unknown:
                    self._clean_btn.config_text(
                        f"Clean Selected ({partial})")
                elif total > 0:
                    self._clean_btn.config_text(
                        f"Clean Selected ({format_bytes(total)})")
                else:
                    self._clean_btn.config_text("Clean Selected")
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
            try:
                self._plan_btn.set_enabled(True)
            except Exception:
                pass
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

    def _open_reclaim_plan(self):
        """Hand the measured numbers to the plan, which owns the next screen.

        Nobody re-scans: the plan is built from `_cat_sizes` / `_cat_keys` this
        walk already produced, so the two windows can never disagree. Storage
        Insight closes first - the plan is modal, and a Clean step it runs
        needs the main window, not two stacked dialogs.
        """
        from app import reclaim
        cats = []
        for title, size in self._cat_sizes.items():
            cats.append({
                "title": title,
                "bytes": size,
                "keys": self._cat_keys.get(title) or [],
                "unknown": self._cat_unknown.get(title) or 0,
            })
        plan = reclaim.build_plan(cats)
        self.close()
        try:
            from app.ui.dialogs.reclaim_plan_dialog import ReclaimPlanDialog
            ReclaimPlanDialog(self.app.root, self.app, plan).wait()
        except Exception as exc:
            try:
                self.app.log("! Could not open the reclaim plan: %s" % exc)
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
