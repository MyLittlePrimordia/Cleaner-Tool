"""InstallTab — extracted verbatim from app/gui.py (H13).

A feature tab page. May use the widget set and the modal base, and
opens dialogs, but never imports another tab.
"""
from __future__ import annotations

import time
import os
import subprocess
import threading

import tkinter as tk
from tkinter import ttk, messagebox
from app.ui.base import ThemedModal, Tooltip, _themed_showinfo
from app.ui.dialogs.install_profiles_dialog import InstallProfilesDialog
from app.ui.theme import COLORS, F, ICON_ROW_PX, icon_bg_rgb, TAB_ACCENTS
from app.ui.widgets import AnimatedButton, ScrollableRoundedPanel

# Catalog rows are COLORS["bg_alt"], so the icon plate must be that exact
# colour; default_icon_ppm() fills the whole tile, and a mismatched fill
# shows up as a square chip behind the icon (user-reported).
_icon_bg = icon_bg_rgb(COLORS["bg_alt"])


class InstallTab(tk.Frame):
    """Catalog browser: categories with checkboxes, [FOSS] badges, hover
    tooltips (description + link hint), per-category Select All, and one
    'Install Selected Apps' button that runs everything through the unified
    winget engine (network pre-check, silent, latest, fallback URLs).

    Layout (user request): app rows flow in TWO columns per category —
    big categories stop being a scroll marathon. Names are the links
    (hover-underline); 🔗 is the mirror link when a fallback exists."""

    _CAT_COLUMNS = 2

    def __init__(self, parent, app):
        super().__init__(parent, bg=COLORS["bg"])
        self.app = app
        self.tab_name = "Install"
        self.vars = {}            # winget id -> BooleanVar
        self._cat_frames = {}     # category -> (header_frame, body_frame, open)
        # category -> the collapse/expand callable, kept so a caller (the
        # smoke harness) can drive the exact function a header click runs
        # without synthesising a mouse event. Tk does not deliver
        # event_generate to an unmapped widget, and this page's content is
        # embedded in a ScrollableRoundedPanel canvas, so under a headless
        # (withdrawn) root a synthetic click is silently dropped.
        self._cat_toggles = {}
        self._app_rows = {}       # app id -> row widget (for installed badges)
        self._cat_sections = []  # (category, header, body, all_row_widgets) for the search filter
        self._bundle_rows = []   # (row_widget, searchable_text) — embedded bundle/task rows
        self._installed_ids = None  # cached set from install_tasks.get_installed_ids
        accent = TAB_ACCENTS["Install"]

        from app.app_catalog import APP_CATALOG, CATEGORY_ORDER, MANUAL_ONLY_APPS

        # User request: remove hint + search topbar so the catalog scroll
        # panel gains vertical space. Search still available via code path
        # if re-enabled later; for now catalog is browse-only.
        self._search_var = tk.StringVar(value="")
        self._search_placeholder = ""
        self._search_focused = False
        self._search_entry = None  # no UI; _apply_search / focus helpers guard

        # bottom-middle run row (user request: match the Clean/Repair/Tweak
        # Run buttons). Packed before the panel so the panel fills the
        # middle and the buttons can never be squeezed out of view.
        self.run_row = tk.Frame(self, bg=COLORS["bg"])
        self.run_row.pack(side="bottom", fill="x", padx=26, pady=(4, 10))
        _btns = tk.Frame(self.run_row, bg=COLORS["bg"])
        _btns.pack(anchor="center")
        # Update button: updates are an action, not an install selection —
        # live count from `winget upgrade` ("Update Apps (N)"). Starts in a
        # scanning state; the worker below paints the real count. Yellow so
        # it reads at a glance next to the green Install button.
        self.update_btn = AnimatedButton(
            _btns, text="Update Apps…", command=self._update_apps,
            bg=COLORS["accent_yellow"], fg=COLORS["black"], font=(F, 12, "bold"), padx=24, pady=11,
        )
        self.update_btn.pack(side="left", padx=6)
        self._update_count = None  # None = unknown/scanning, else int
        self.install_btn = AnimatedButton(
            _btns, text="Install", command=self._install_selected,
            bg=accent, fg=COLORS["black"], font=(F, 12, "bold"), padx=24, pady=11,
        )
        self.install_btn.pack(side="left", padx=6)
        self.install_btn.set_enabled(False)  # gray until something is picked
        # "My Setups" (feature 7) removed from the Install tab per user
        # request — the save/restore profile button next to Install/Update
        # wasn't needed. _open_profiles() and the profiles storage stay in
        # place (harmless, still reachable if a future surface wants them);
        # self._profiles_btn is simply never created, and the guard at
        # set_run_enabled() already handles that via getattr(..., None).

        panel = ScrollableRoundedPanel(self)
        panel.pack(fill="both", expand=True, padx=26, pady=(4, 4))
        self._panel = panel

        # --- Essentials: one-click tasks, split in two groups (user request):
        #   * "LTSC Missing Components" — Store, winget+UniGetUI, Xbox stack,
        #     Game Bar, Windows codecs (the bits LTSC strips out)
        #   * "Essentials" — the ALL-VC++/DirectX/.NET/Java/Classic runtime
        #     bundles (what used to be the "Runtimes & Dependencies" catalog
        #     category, merged so nobody guesses versions).
        # Styled as CHECKBOX ROWS to match the catalog below (user feedback:
        # the old button-grid looked different/inconsistent). Checking rows
        # selects them; the big 'Install Selected Apps' button runs the
        # checked Essentials TOGETHER with checked catalog apps. ---
        from app.tab_presets import TABS as ALL_TABS
        ess_tasks = list(ALL_TABS["Install"])
        self.ess_vars = {}
        self._ess_groups = {}       # group title -> {"body": frame, "arrow": label}
        self.bundle_vars = {}       # bundle key -> (BooleanVar, Task); rendered inside catalog categories
        self._cat_count_lbls = {}   # category -> "N apps" label (x/y selected)
        # group order follows TASKS order (Store stays first: brings winget)
        _seen_groups: list = []
        for _t in ess_tasks:
            _g = getattr(_t, "group", "Essentials")
            if _g not in _seen_groups:
                _seen_groups.append(_g)
        _group_subs = {
            "LTSC Missing Components": "— the Store, Xbox, Game Bar, codecs & everyday apps that LTSC strips out",
            "Essentials": "— one-click runtime bundles: check them all, no version guessing",
        }

        # F6(a): the catalog rows (~950 widgets) are NOT built on the
        # synchronous startup path anymore — the constructor above (chrome,
        # buttons, panel) is all the first frame pays for. The sections
        # below are queued and built in idle-time slices by
        # _start_catalog_build() (one Essentials group or one category per
        # slice, each ~5-20 ms), which Application kicks off after its
        # post-startup pre-warm chain. Until the queue drains, a lightweight
        # placeholder marks the content area; features that touch rows
        # (search, badges, counts) are all pre-build-safe by design — see
        # _catalog_finish for what is applied once rows exist.
        self._catalog_queue = []
        for _g in _seen_groups:
            _gtasks = [t for t in ess_tasks if getattr(t, "group", "Essentials") == _g]
            self._catalog_queue.append(
                lambda g=_g, ts=_gtasks: self._build_ess_group(
                    panel.inner, g, _group_subs.get(g, ""), accent, ts))
        for cat in CATEGORY_ORDER:
            apps = [a for a in APP_CATALOG if a["category"] == cat]
            manuals = [a for a in MANUAL_ONLY_APPS if a["category"] == cat]
            if not apps and not manuals:
                continue
            self._catalog_queue.append(
                lambda c=cat, ap=apps, mn=manuals: self._build_category(
                    panel.inner, c, ap, mn, accent))

        self._catalog_ready = False   # queue fully drained?
        self._catalog_after = None    # chained-slice after-id
        self._pending_badges = None   # installed-badge scan that landed mid-build
        # "Preparing catalog…" marker. Since the progressive-show change
        # (2026-09, user-approved) the page CAN be shown half-built: the
        # marker sits above the sections that have already landed and
        # _catalog_finish drops it (plus applies pending badge/search/count
        # state) when the queue drains. It also still guards direct/harness
        # construction before any slice runs.
        self._placeholder = tk.Label(
            panel.inner, text="Preparing catalog…", font=(F, 10),
            bg=COLORS["bg_alt"], fg=COLORS["subtext"])
        self._placeholder.pack(pady=24)
        try:
            panel.refresh_scroll()
        except Exception:
            pass

        # installed badges + update count: kick off the (slow) winget calls
        # in worker threads, then paint on the Tk thread when they land.
        # Both paints are pre-build-safe (badges defer until rows exist).
        self._start_installed_badge_scan()
        self._start_update_count_scan()
        self._refresh_install_count()

    # ---------------- F6(a): chunked catalog build ---------------- #

    _CATALOG_SLICE_GAP_MS = 8   # breathing room between idle slices

    def _start_catalog_build(self):
        """F6(a): drain the queued catalog slices in idle time. Called by
        Application's post-startup pre-warm chain — AFTER the Custom/Undo
        pre-warm steps, so the one-off Tk costs (font loads) are already
        paid and every slice here stays small. No-op when already built or
        when a slice run is in flight."""
        if self._catalog_ready:
            return
        try:
            if getattr(self, "_cat_t0", None) is None:
                self._cat_t0 = time.perf_counter()
                self._cat_slices = 0
                self._cat_work = 0.0
        except Exception:
            pass
        if self._catalog_after is not None:
            return  # a slice run is already in flight
        if not self._catalog_queue:
            # defensive: nothing queued but never finished — settle now
            self._catalog_finish()
            return
        self._catalog_step_timed()

    def _catalog_step_timed(self):
        """TIMING (diagnostic): run one slice and account for it.

        The Install tab was reported as taking seconds to come up on a switch.
        The chain gap is only 8 ms, so the delay is not scheduling - it is
        either the first map of a large widget tree or the finish-time pass.
        This measures which, instead of guessing.
        """
        _t0 = time.perf_counter()
        try:
            self._cat_slices = int(getattr(self, "_cat_slices", 0)) + 1
        except Exception:
            pass
        try:
            self._catalog_step()
        finally:
            try:
                self._cat_work = float(getattr(self, "_cat_work", 0.0)) \
                    + (time.perf_counter() - _t0) * 1000.0
            except Exception:
                pass

    def _catalog_log_timing(self, extra=""):
        """TIMING (diagnostic): one summary line per Install build."""
        try:
            t0 = getattr(self, "_cat_t0", None)
            if t0 is None:
                return
            self.app.log(
                "Install catalog: %d slices, %.0f ms slice work, %.0f ms wall%s"
                % (int(getattr(self, "_cat_slices", 0)),
                   float(getattr(self, "_cat_work", 0.0)),
                   (time.perf_counter() - t0) * 1000.0,
                   (" | " + extra) if extra else ""))
        except Exception:
            pass

    def _catalog_step(self):
        """Build one queued slice (an Essentials group or a catalog
        category) and chain the rest on a small gap — the UI thread never
        blocks more than the slice takes (~5-20 ms)."""
        self._catalog_after = None
        # flicker fix: registry check, not grab_current() — a Combobox
        # popdown inside an open dialog steals the grab while posted, which
        # made this read "no modal" and build/raise slices behind the
        # popup (the Auto-Pilot dropdown flicker).
        _modal = ThemedModal.any_open()
        if _modal:
            # a popup is up — building/mapping slices now would churn the
            # main window behind it (user bug report: first-open flicker
            # behind Auto-Pilot). Retry shortly; the queue keeps its place
            # and _ensure_catalog_built can still cancel/drain explicitly.
            try:
                self._catalog_after = self.after(250, self._catalog_step)
            except Exception:
                self._catalog_after = None
            return
        if self._catalog_queue:
            try:
                self._catalog_queue.pop(0)()
            except Exception as exc:
                # honest failure: log and keep the chain alive — a widget
                # build error must not take the idle loop down
                try:
                    self.app.log(f"Install catalog: a section failed to build: {exc}")
                except Exception:
                    pass
        if self._catalog_queue:
            # Progressive-show (2026-09): while the user is actually
            # LOOKING at this page mid-build, settle the scroll metrics
            # after each slice so the scrollbar enables the moment the real
            # content outgrows the viewport (and the offset stays clamped).
            # Skipped while the page is hidden/covered — that pass would
            # only churn the visible page; _catalog_finish still does the
            # final full refresh when the queue drains. flush=False is
            # deliberate: the full refresh's update_idletasks forced every
            # pending idle redraw of the mapped content per slice (bench:
            # ~90-220 ms each) — the flush-free metrics pass recomputes
            # requested sizes without painting (~1-2 ms).
            if getattr(getattr(self, "app", None), "active_tab", None) == "Install":
                try:
                    self._panel.refresh_scroll(flush=False)
                except Exception:
                    pass  # panel torn down mid-build (shutdown)
            try:
                self._catalog_after = self.after(self._CATALOG_SLICE_GAP_MS,
                                                 self._catalog_step_timed)
            except Exception:
                self._catalog_after = None  # root torn down mid-build
        else:
            self._catalog_finish()

    def _ensure_catalog_built(self):
        """F6(a): finish the queued catalog slices SYNCHRONOUSLY right now.

        Kept for direct/harness callers that need synchronous completeness
        (the repo smoke driver and measurement harness call it explicitly
        after switching to Install). The UI no longer calls this from
        _show_tab: reaching Install mid-build shows the page progressively
        instead of force-draining the queue on the click (the old call
        stalled the click 700-900 ms cold; measured 2026-09)."""
        if self._catalog_ready:
            return
        if self._catalog_after is not None:
            try:
                self.after_cancel(self._catalog_after)
            except Exception:
                pass
            self._catalog_after = None
        while self._catalog_queue:
            try:
                self._catalog_queue.pop(0)()
            except Exception as exc:
                try:
                    self.app.log(f"Install catalog: a section failed to build: {exc}")
                except Exception:
                    pass
        self._catalog_finish()

    def _catalog_finish(self):
        """F6(a): catalog fully built — drop the placeholder, then apply
        everything that arrived while the rows were still pending:
          * installed-badge scan result held in _pending_badges
          * a search query typed pre-build (fresh rows render filtered)
        and settle counts + scroll metrics on the real content."""
        try:
            self._catalog_log_timing()
        except Exception:
            pass
        self._catalog_ready = True
        try:
            if self._placeholder is not None and self._placeholder.winfo_exists():
                self._placeholder.destroy()
        except Exception:
            pass
        self._placeholder = None
        if self._pending_badges is not None:
            ids, self._pending_badges = self._pending_badges, None
            try:
                self._paint_installed_badges(ids)
            except Exception:
                pass
        try:
            q = self._search_var.get().strip()
            if q and not q.lower().startswith("search "):
                self._apply_search()
        except Exception:
            pass
        try:
            self._refresh_install_count()
            self._panel.refresh_scroll()
        except Exception:
            pass

    # ---------------- search box ---------------- #

    def _search_focus_in(self, _e):
        if not self._search_focused:
            self._search_focused = True
            if self._search_var.get().startswith("Search "):
                self._search_var.set("")

    def _search_focus_out(self, _e):
        self._search_focused = False
        if not self._search_var.get().strip():
            self._search_var.set(self._search_placeholder)

    def _apply_search(self):
        """Filter-as-you-type, Custom-tab semantics (user request): typing
        e.g. 'rufus' must CLEAR the page to only matching apps — no empty
        category dividers, no always-visible stragglers.

        Behavior:
          * empty/placeholder query -> full catalog back, Essentials back
          * otherwise: a category shows ONLY its matching rows (re-packed
            top-down per column, no gaps); a category with zero matches
            hides its whole block (divider + body) — an empty divider is
            noise, exactly the pre-fix bug
          * Essentials sections (LTSC components / runtime bundles) hide
            during a query too — they're one-click tasks, not catalog
            apps, so a name search must not leave them stranded mid-page
          * the embedded bundle rows (APO + Peace GUI, APO + FluidEQ,
            RustDesk, FreeFileSync) participate like any app: hidden
            unless the query matches their label/description

        2-column layout: rows live inside per-column frames. Hiding one
        leaves a gap in that column, so visible rows are RE-PACKED per
        column (top-down flow, no gaps) from the filter result."""
        q = self._search_var.get().strip().lower()
        show_all = (not q) or q.startswith("search ")
        from app.app_catalog import APP_CATALOG, MANUAL_ONLY_APPS
        by_id = {a["id"]: a for a in APP_CATALOG}
        by_id.update({m["id"]: m for m in MANUAL_ONLY_APPS})
        for cat, header, body, rows in getattr(self, "_cat_sections", []):
            visible_rows = []
            for app_id, row in rows:
                app = by_id.get(app_id)
                if app is None:
                    continue
                if show_all or (q in app["name"].lower()
                                or q in app["id"].lower()
                                or q in app["description"].lower()
                                or q in app.get("hardware", "").lower()):
                    visible_rows.append((app_id, row))
            any_visible = bool(visible_rows)
            # re-pack visible rows inside their OWN column frames, in original
            # order (pack_forget collapses, so no gaps). NOTE: rows must NEVER
            # move across columns: Tk forbids pack -in to any master that is
            # not the widget's parent or a descendant of it ("can't pack X
            # inside Y" — an uncle column is illegal), so the old round-robin
            # rebalance crashed on the first filtered search.
            col_frames = [c for c in body.winfo_children()
                          if isinstance(c, tk.Frame)]
            col_index = {str(c): i for i, c in enumerate(col_frames)}
            for app_id, row in rows:
                try:
                    row.pack_forget()  # parentage kept; re-pack below is legal
                except Exception:
                    pass
            buckets = [[] for _ in col_frames]
            for app_id, row in visible_rows:
                buckets[col_index.get(str(row.winfo_parent()), 0)].append(row)
            for col, bucket in zip(col_frames, buckets):
                for row in bucket:
                    row.pack(fill="x", padx=4, pady=4)
            # the whole category BLOCK hides when nothing matched — the
            # divider ('outer' frame: header + body + bundle row) packs
            # directly under the panel's inner frame, so pack_forget on it
            # removes divider AND body AND the bundle row in one move.
            outer_block = body.master
            bundle_visible = show_all or any(
                q in text for _row, text in getattr(self, "_bundle_rows", [])
                if _row.master is outer_block
            )
            hide_block = (not show_all) and not any_visible and not bundle_visible
            if hide_block:
                if outer_block.winfo_manager() == "pack":
                    outer_block.pack_forget()
            else:
                if outer_block.winfo_manager() != "pack":
                    outer_block.pack(fill="x", pady=(6, 4))
                # restore the body to the category's RECORDED open/collapsed
                # state (a search must not re-open a category the user
                # collapsed — the ▸/▾ arrow would otherwise desync).
                try:
                    cat_open = self._cat_frames[cat][2]
                except (KeyError, IndexError):
                    cat_open = True
                if cat_open and body.winfo_manager() != "pack":
                    body.pack(fill="x")
                elif not cat_open and body.winfo_manager() == "pack":
                    body.pack_forget()
            # embedded bundle rows (APO bundles etc.): filter by their own
            # searchable text; hide during a non-matching query
            for _row, text in getattr(self, "_bundle_rows", []):
                if _row.master is not outer_block:
                    continue
                try:
                    if show_all or (q in text):
                        _row.pack(fill="x", padx=10, pady=(2, 6))
                    else:
                        _row.pack_forget()
                except Exception:
                    pass
        # Essentials sections: one-click tasks, not catalog apps — hide
        # entirely during a query (Custom-tab semantics: only matches)
        for _title, group in getattr(self, "_ess_groups", {}).items():
            try:
                ess_block = group["body"].master  # the 'ess' frame
                if show_all:
                    if ess_block.winfo_manager() != "pack":
                        ess_block.pack(fill="x", pady=(2, 6))
                else:
                    if ess_block.winfo_manager() == "pack":
                        ess_block.pack_forget()
            except Exception:
                pass
        # keep the panel's scroll region honest after re-flow
        try:
            self._panel.refresh_scroll()
        except Exception:
            pass

    # ---------------- selection count ---------------- #

    def _refresh_install_count(self):
        """Live 'Install (N)' count + per-category 'x/y selected' labels
        (user request: show what you picked). Traced from every checkbox
        var, so All/None/search-independent sets all land. The Install
        button stays grayed out until something is picked (user request)."""
        try:
            sel = self.selected_apps()
            n = len(sel)
            btn = getattr(self, "install_btn", None)
            if btn is not None:
                btn.config_text(f"Install ({n})" if n else "Install")
                busy = bool(getattr(getattr(self, "app", None), "_busy", False))
                btn.set_enabled(bool(n) and not busy)
            from app.app_catalog import APP_CATALOG
            for cat, total_lbl in getattr(self, "_cat_count_lbls", {}).items():
                total = sum(1 for a in APP_CATALOG if a["category"] == cat)
                picked = sum(1 for k, s in sel if k == "app" and s["category"] == cat)
                # Essentials tasks counted on the button only (they have no
                # category header of their own to annotate)
                try:
                    total_lbl.config(text=f"{picked}/{total} selected" if picked else f"{total} apps")
                except Exception:
                    pass
        except Exception:
            pass

    def _trace_install_var(self, var):
        """One shared trace callback for every Install checkbox var."""
        try:
            var.trace_add("write", lambda *_: self._refresh_install_count())
        except Exception:
            pass

    # ---------------- installed badges ---------------- #

    def _post_to_tk(self, fn):
        """Schedule fn on the Tk thread. Worker-safe.

        InstallTab is a tk.Frame and has NO .root attribute — the original
        self.root.after(...) raised AttributeError on every scan (swallowed by
        the except), so the installed-badges and Update-count callbacks never
        ran: badges never appeared and the Update button sat at
        'Update Apps…' forever.

        BUG-001: the replacement resolved the real toplevel and called
        `tk_root.after(0, fn)` — but this runs ON THE WORKER (both _scan bodies
        call it), and after() is itself a Tk call. On Python 3.14 that raises
        RuntimeError("main thread is not in main loop") every time, so the fix
        traded one dead path for another and the badges and Update count still
        never appeared. The old note that "the worker never calls winfo_*" was
        true and beside the point: after() was the call that mattered.

        It now posts through the app's TkDispatcher, which is what that note
        should have said from the start.
        """
        try:
            self.app._dispatch.post(fn)
        except Exception:
            pass  # shutdown race — root already destroyed; nothing to paint

    def _start_installed_badge_scan(self):
        """winget list is slow (1-3s) — run it once in a worker thread and
        post the result back; badges paint in one pass on the Tk thread."""
        # F10: capture the Tk holder on THIS (Tk) thread so the worker
        # below never touches winfo_*.
        try:
            self._tk_holder = self.winfo_toplevel()
        except Exception:
            self._tk_holder = None

        def _scan():
            try:
                from app.tasks.install_tasks import get_installed_ids
                ids = get_installed_ids()
            except Exception:
                ids = None
            if ids is not None:
                # F-001 fix: route through the real root (self.root never
                # existed on a Frame — this call silently killed the scan).
                self._post_to_tk(lambda: self._paint_installed_badges(ids))

        threading.Thread(target=_scan, daemon=True).start()

    def _paint_installed_badges(self, installed_ids):
        """Show a green '✓ Installed' badge next to every catalog app that
        winget reports as present. Runs once per session (cached upstream).

        (Placement fix, user feedback 2026-09-06: the badge used to pack at
        the row's FAR-RIGHT edge — visually divorced from the name it
        belongs to. It now packs side="left" AFTER the existing left-side
        widgets, so it joins the name cluster directly (name | FOSS | OEM |
        🔗 | ✓ Installed) instead of floating at the row's end. Color note:
        green stays reserved for this badge alone — the FOSS chip was
        recolored sky blue (accent_sky) so the two never read as one.)"""
        self._installed_ids = installed_ids or set()
        if not self._catalog_ready:
            # F6(a): the badge scan is kicked off at construction, but the
            # catalog rows build later in idle slices — hold the result so
            # _catalog_finish can paint it on the real rows.
            self._pending_badges = installed_ids or set()
            return
        n = 0
        for app_id, row in getattr(self, "_app_rows", {}).items():
            if app_id.lower() not in self._installed_ids:
                continue
            n += 1
            b = tk.Label(row, text="✓ Installed", font=(F, 7, "bold"),
                         bg=COLORS["bg_alt"], fg=COLORS["accent_green"])
            # beside the name cluster, not exiled to the row's right edge —
            # packed left it lands right after the last badge (FOSS / OEM /
            # 🔗), keeping the marker adjacent to the app it belongs to
            b.pack(side="left", padx=(6, 0))
        if n:
            self.app.log(f"Install tab: {n} of your catalog apps are already installed (badges shown).")

    # ---------------- Update Apps button ---------------- #

    def _start_update_count_scan(self, refresh: bool = False):
        """`winget upgrade` is slow (5-30s) — count upgradable apps in a
        worker thread, then paint 'Update Apps (N)' on the Tk thread."""

        def _scan():
            try:
                from app.tasks.install_tasks import get_upgradable_count
                n = get_upgradable_count(refresh=refresh)
            except Exception:
                n = None
            # F-001 fix: same dead self.root reference as the badge scan —
            # the Update-count paint never ran, so the button never showed
            # its real number. Post through the real toplevel instead.
            self._post_to_tk(lambda: self._paint_update_count(n))

        threading.Thread(target=_scan, daemon=True).start()

    def _paint_update_count(self, n):
        """Paint the Update button label. None = unknown (no winget /
        offline / parse failed) — plain 'Update Apps', still clickable.
        Always yellow (user request): it must read at a glance next to the
        Install button."""
        self._update_count = n
        btn = getattr(self, "update_btn", None)
        if btn is None:
            return
        try:
            if n is None:
                btn.config_text("Update Apps")
            else:
                btn.config_text(f"Update Apps ({n})")
            btn.set_style(bg=COLORS["accent_yellow"], fg=COLORS["black"])
        except Exception:
            pass

    def refresh_update_count(self):
        """Re-scan upgrades in the background (called after any install /
        update run completes, so the button never shows a stale number)."""
        btn = getattr(self, "update_btn", None)
        if btn is not None:
            try:
                btn.config_text("Update Apps…")
            except Exception:
                pass
        self._start_update_count_scan(refresh=True)

    def _update_apps(self):
        """Dedicated Update button: runs `winget upgrade --all` through the
        standard worker flow (progress bar + Stop button)."""
        from app.tasks.install_tasks import UPDATE_ALL_TASK
        self.app.install_selected_mixed([], [UPDATE_ALL_TASK])

    def _build_ess_group(self, parent, title, subtitle, accent, tasks):
        """One collapsible Essentials section (LTSC group or runtime group).
        Rows register into the shared self.ess_vars / self.vars pools so
        'Install Selected Apps' includes them naturally, in TASKS order.
        Header is arrow + titles only (user request: no All/None buttons)."""
        ess = tk.Frame(parent, bg=COLORS["bg_alt"])
        ess.pack(fill="x", pady=(2, 6))
        ess_head = tk.Frame(ess, bg=COLORS["surface"])
        ess_head.pack(fill="x")
        _ess_title = tk.Label(ess_head, text=title, font=(F, 10, "bold"),
                              bg=COLORS["surface"], fg=accent)
        _ess_title.pack(side="left", padx=(8, 4), pady=5)
        _ess_sub = tk.Label(ess_head, text=subtitle,
                            font=(F, 8), bg=COLORS["surface"], fg=COLORS["subtext"])
        _ess_sub.pack(side="left")

        body = tk.Frame(ess, bg=COLORS["bg_alt"])
        body.pack(fill="x")
        # arrow on the RIGHT like every other Install divider (user request)
        arrow = tk.Label(ess_head, text="▾", font=(F, 10, "bold"),
                         bg=COLORS["surface"], fg=accent, width=2)
        arrow.pack(side="right", padx=(0, 8), pady=6)

        def toggle_ess(b=body, a=arrow):
            if b.winfo_ismapped():
                b.pack_forget()
                a.config(text="▸")
            else:
                b.pack(fill="x")
                a.config(text="▾")

        arrow.bind("<Button-1>", lambda e: toggle_ess())
        ess_head.bind("<Button-1>", lambda e: toggle_ess())
        # hover recolors the bar AND its labels together (user bug: only the
        # bar changed, leaving boxed-looking text on mismatched backgrounds)
        def _ess_hover(on, _e=None):
            _bg = COLORS["surface_hover"] if on else COLORS["surface"]
            try:
                ess_head.config(bg=_bg)
                for _w in (_ess_title, _ess_sub, arrow):
                    _w.config(bg=_bg)
            except Exception:
                pass
        ess_head.bind("<Enter>", lambda e: _ess_hover(True))
        ess_head.bind("<Leave>", lambda e: _ess_hover(False))
        self._ess_groups[title] = {"body": body, "arrow": arrow}

        # 2-column flow, matching every catalog divider (user request).
        # Essentials was the last divider still flowing its rows straight down
        # one column, so the runtime groups came out about twice as tall as
        # the categories around them and the tab read as ragged. Same
        # construction as _build_category: per-column frames on a uniform
        # grid, rows dealt round-robin, so the columns also line up with
        # every other divider instead of each group picking its own widths.
        cols = self._CAT_COLUMNS
        col_frames = []
        for c in range(cols):
            cf = tk.Frame(body, bg=COLORS["bg_alt"])
            cf.grid(row=0, column=c, sticky="nsew", padx=(6, 6))
            body.grid_columnconfigure(c, weight=1, uniform="catcols")
            col_frames.append(cf)

        for ci, task in enumerate(tasks):
            var = tk.BooleanVar(value=False)
            self.ess_vars[task.key] = var
            # also register in the shared vars pool so 'Install Selected
            # Apps' includes Essentials naturally
            self.vars[f"task:{task.key}"] = var
            self._trace_install_var(var)
            row = tk.Frame(col_frames[ci % cols], bg=COLORS["bg_alt"])
            row.pack(fill="x", padx=10, pady=2)
            cb = tk.Checkbutton(row, variable=var, bg=COLORS["bg_alt"],
                                fg=COLORS["text"], activebackground=COLORS["bg_alt"],
                                selectcolor=COLORS["surface"], onvalue=True, offvalue=False)
            cb.pack(side="left")
            # task icons (user bug: Essentials/LTSC rows never called
            # _row_icon — same helper + filename convention as the
            # catalog/bundle rows, keyed off the task label).
            self._row_icon(row, task.label)
            name_lbl = tk.Label(row, text=task.label, font=(F, 9, "bold"),
                                bg=COLORS["bg_alt"], fg=COLORS["text"])
            name_lbl.pack(side="left", padx=(6, 4))
            # admin shield only (user request: no "one-click" text — the
            # shield matches the Custom toggle rows)
            if getattr(task, "admin_required", False):
                tg = tk.Label(row, text="🛡️", font=("Segoe UI Emoji", 9),
                              bg=COLORS["bg_alt"], fg=COLORS["text"], cursor="hand2")
                tg.pack(side="left", padx=(6, 0))
                Tooltip(tg, "Admin Required — needs Administrator rights (skipped in limited mode)")
            Tooltip(name_lbl, task.description)
            Tooltip(cb, task.description)

    def set_run_enabled(self, enabled: bool):
        if getattr(self, "install_btn", None):
            # re-enabling still respects the empty-selection gray-out
            # (user request) — only a non-empty pick lights Install up
            if enabled:
                try:
                    n = len(self.selected_apps())
                except Exception:
                    n = 1
                self.install_btn.set_enabled(bool(n))
            else:
                self.install_btn.set_enabled(False)
        if getattr(self, "update_btn", None):
            self.update_btn.set_enabled(enabled)
        # Feature 7: loading a setup rewrites the tick boxes, so the
        # button is gated with the others while a run is in flight.
        if getattr(self, "_profiles_btn", None):
            self._profiles_btn.set_enabled(enabled)

    def selected_apps(self):
        """Checked catalog apps + checked Essentials tasks, in one batch."""
        from app.app_catalog import APP_CATALOG
        from app.tab_presets import TABS as ALL_TABS
        task_by_key = {t.key: t for t in ALL_TABS["Install"]}
        result = []
        # Essentials first (Store before everything: brings winget)
        for key, var in getattr(self, "ess_vars", {}).items():
            if var.get():
                t = task_by_key.get(key)
                if t:
                    result.append(("task", t))
        # catalog-embedded bundles (APO + Peace / APO + FluidEQ live in
        # Media; RustDesk / FreeFileSync in Utilities — any task in
        # EMBEDDED_TASKS_BY_CATEGORY)
        for key, (var, t) in getattr(self, "bundle_vars", {}).items():
            if var.get():
                result.append(("task", t))
        for a in APP_CATALOG:
            v = self.vars.get(a["id"])
            if v is not None and v.get():
                result.append(("app", a))
        return result

    # ---------------- My Setups (feature 7) ---------------- #

    def profile_ids(self):
        """Current picks as stable ids for a saved setup.

        Catalog apps keep their winget id; Essentials tasks and embedded
        bundles are stored as "task:<key>" so the two namespaces can
        never collide when a profile is loaded back."""
        ids = []
        try:
            for kind, item in self.selected_apps():
                if kind == "app":
                    app_id = item.get("id")
                    if app_id:
                        ids.append(str(app_id))
                else:
                    key = getattr(item, "key", None)
                    if key:
                        ids.append("task:" + str(key))
        except Exception:
            return []
        return ids

    def apply_profile_ids(self, ids):
        """Replace the current selection with `ids`. (applied, missing).

        Replaces rather than adds: a saved setup means exactly that set.
        Ids that no longer exist (an app pulled from the catalog since
        the profile was saved) are counted and reported, never silently
        dropped — the dialog tells the user how many did not match.

        Only the vars are touched, so every existing safety gate on the
        Install path (admin checks, winget verification) still applies
        exactly as if the boxes had been ticked by hand."""
        wanted = set(str(i) for i in (ids or []))
        applied = 0
        try:
            # clear everything first
            for var in list(getattr(self, "vars", {}).values()):
                try:
                    var.set(False)
                except Exception:
                    pass
            for var in list(getattr(self, "ess_vars", {}).values()):
                try:
                    var.set(False)
                except Exception:
                    pass
            for pair in list(getattr(self, "bundle_vars", {}).values()):
                try:
                    pair[0].set(False)
                except Exception:
                    pass
            # then tick what the profile asks for
            # NOTE: self.vars also holds "task:<key>" aliases for every
            # Essentials var (registered alongside self.ess_vars so the
            # shared "Install Selected Apps" count includes them) —
            # skip those here so a task id doesn't get counted once via
            # this loop and again via the ess_vars loop right below
            # (the double-count bug: (applied, missing) coming back as
            # (2, 0) for a single saved Essentials task).
            for app_id, var in getattr(self, "vars", {}).items():
                if str(app_id).startswith("task:"):
                    continue
                if str(app_id) in wanted:
                    try:
                        var.set(True)
                        applied += 1
                    except Exception:
                        pass
            for key, var in getattr(self, "ess_vars", {}).items():
                if ("task:" + str(key)) in wanted:
                    try:
                        var.set(True)
                        applied += 1
                    except Exception:
                        pass
            for key, pair in getattr(self, "bundle_vars", {}).items():
                if ("task:" + str(key)) in wanted:
                    try:
                        pair[0].set(True)
                        applied += 1
                    except Exception:
                        pass
        except Exception:
            pass
        try:
            self._refresh_install_count()
        except Exception:
            pass
        return applied, max(0, len(wanted) - applied)

    def _open_profiles(self):
        try:
            InstallProfilesDialog(self, self).wait()
        except Exception:
            pass

    def _install_selected(self):
        selected = self.selected_apps()
        if not selected:
            _themed_showinfo(self, "Nothing Selected",
                             "Check some Essentials or apps to install first.",
                             accent=COLORS["accent_green"])
            return
        apps = [s for kind, s in selected if kind == "app"]
        tasks = [s for kind, s in selected if kind == "task"]
        self.app.install_selected_mixed(apps, tasks)

    def _pad_icon_box(self, img, size):
        """Centre `img` in a `size` x `size` transparent box and return the
        box. Aspect ratio and the logo's own pixels are untouched; the only
        change is transparent padding around it.

        Why the padding is baked into the IMAGE and not set on the Label: a
        Label's width= option is measured in CHARACTERS of its font, not
        pixels, so reserving an icon's width that way renders at whatever the
        font's average character happens to be. That mistake blanked the
        Process Manager popup once already (5aba301) and must not be repeated
        here. Tk's photo-image `copy` preserves the source's alpha, so the
        padding is genuinely transparent rather than a black plate - which is
        asserted in tools/verify_install_row_alignment.py, because an opaque
        padding would draw a black square around every logo.

        An image that is already exactly size x size is returned unchanged.
        """
        try:
            iw, ih = img.width(), img.height()
            if iw == size and ih == size:
                return img
            box = tk.PhotoImage(width=size, height=size)   # starts transparent
            box.tk.call(box, "copy", img,
                        "-to", (size - iw) // 2, (size - ih) // 2)
            return box
        except Exception:
            # never lose the logo over cosmetics - an unpadded image is only
            # misaligned, a missing one is a blank row
            return img

    def _row_icon(self, row, app_name, size=None):
        size = ICON_ROW_PX if size is None else int(size)
        """App icon label for a catalog row (user call: logos next to
        names, big enough to read — bumped from 22 to 32px per user
        feedback that the old size was hard to make out). Loads
        app/assets/apps/<name>.png once and caches the PhotoImage (anti-GC,
        tab-icon pattern); subsamples to a ~size px box so every row stays
        the same height. Missing file -> fixed-width spacer so names
        never misalign.

        One widget per row, and exactly ICON_ROW_PX wide: the logo is padded
        into a fixed square by _pad_icon_box rather than packed at whatever
        width it happens to be. Without that, the icon slot - and so the app
        name's left edge - followed the logo's aspect ratio, and a logo whose
        short side was under `size` (a 16x16 icon was never scaled UP at all)
        shifted its name. Measured before this: icon slots 16-32 px wide and
        names starting at ten different x positions across 203 rows, which
        is the ragged column this now prevents."""
        try:
            cache = self.__dict__.setdefault("_icon_cache", {})
            if app_name not in cache:
                # Placeholder first so a slow/failed load can't be retried
                # forever, then always overwrite with a real image below —
                # either the app's own logo or a generic fallback swatch —
                # so no row is ever left permanently without an icon.
                cache[app_name] = None
                img = None
                try:
                    import math as _math
                    import re as _re
                    base = _re.sub(r"\s*\(.*?\)", "", str(app_name))
                    base = _re.sub(r"[^a-z0-9]", "", base.lower()) + ".png"
                    from app.utils import resolve_asset_path as _rap
                    path = _rap(os.path.join("apps", base))
                    if path:
                        img = tk.PhotoImage(file=path)
                        w, h = img.width(), img.height()
                        if w > 0 and h > 0:
                            f = max(1, int(_math.ceil(max(w, h) / float(size))))
                            if f > 1:
                                img = img.subsample(f, f)
                        else:
                            img = None
                except Exception:
                    img = None
                if img is None:
                    try:
                        from app.icon_extract import default_icon_ppm
                        img = tk.PhotoImage(data=default_icon_ppm(size, _icon_bg))
                    except Exception:
                        img = None
                # fixed square, so every row's name starts at the same x
                if img is not None:
                    img = self._pad_icon_box(img, size)
                cache[app_name] = img
            img = cache.get(app_name)
            if img is not None:
                lbl = tk.Label(row, image=img, bg=COLORS["bg_alt"],
                               bd=0, highlightthickness=0)
                lbl.pack(side="left", padx=(2, 0))
                return lbl
        except Exception:
            pass
        try:
            sp = tk.Frame(row, bg=COLORS["bg_alt"], width=size, height=size)
            sp.pack(side="left", padx=(2, 0))
            sp.pack_propagate(False)
        except Exception:
            pass
        return None

    def _build_category(self, parent, cat, apps, manuals, accent):
        outer = tk.Frame(parent, bg=COLORS["bg_alt"])
        outer.pack(fill="x", pady=(6, 4))

        header = tk.Frame(outer, bg=COLORS["surface"])
        header.pack(fill="x")
        # arrow on the RIGHT on every Install divider (user request)
        arrow = tk.Label(header, text="▾", font=(F, 10, "bold"), bg=COLORS["surface"],
                         fg=accent, width=2)
        arrow.pack(side="right", padx=(0, 8), pady=6)
        _cat_title = tk.Label(header, text=cat, font=(F, 10, "bold"), bg=COLORS["surface"],
                              fg=COLORS["text"])
        _cat_title.pack(side="left", padx=6)
        n_lbl = tk.Label(header, text=f"{len(apps) + len(manuals)} apps", font=(F, 9),
                         bg=COLORS["surface"], fg=COLORS["subtext"])
        n_lbl.pack(side="left", padx=4)
        self._cat_count_lbls[cat] = n_lbl

        def toggle_cat():
            body = self._cat_frames[cat][1]
            open_ = self._cat_frames[cat][2]
            if open_:
                body.pack_forget()
                arrow.config(text="▸")
                self._cat_frames[cat] = (self._cat_frames[cat][0], body, False)
            else:
                body.pack(fill="x")
                arrow.config(text="▾")
                self._cat_frames[cat] = (self._cat_frames[cat][0], body, True)

        # (user request: no All/None buttons on catalog category dividers —
        # pick apps individually; Essentials groups keep theirs)
        # hover recolors the bar AND its labels together (user bug: only the
        # bar changed, leaving boxed-looking text on mismatched backgrounds)
        def _cat_hover(on, _e=None):
            _bg = COLORS["surface_hover"] if on else COLORS["surface"]
            try:
                header.config(bg=_bg)
                for _w in (_cat_title, n_lbl, arrow):
                    _w.config(bg=_bg)
            except Exception:
                pass
        for w in (header, arrow):
            w.bind("<Button-1>", lambda e: toggle_cat())
            w.bind("<Enter>", lambda e: _cat_hover(True))
            w.bind("<Leave>", lambda e: _cat_hover(False))

        body = tk.Frame(outer, bg=COLORS["bg_alt"])
        body.pack(fill="x")
        cat_rows = []   # (app_id, row) — feeds the search filter
        cols = self._CAT_COLUMNS          # 2-column flow (user request)
        col_frames = []
        for c in range(cols):
            cf = tk.Frame(body, bg=COLORS["bg_alt"])
            cf.grid(row=0, column=c, sticky="nsew", padx=(6, 6))
            body.grid_columnconfigure(c, weight=1, uniform="catcols")
            col_frames.append(cf)

        def _make_name_link(parent, text, url, tip_text, base_fg=None, bold=True):
            """Clickable app name (user design): bold like the Custom task
            names, calm text color; hover turns it hyperlink-blue +
            underlined (click opens the primary link, 🔗 the mirror)."""
            fg = base_fg or COLORS["text"]
            _font = (F, 9, "bold") if bold else (F, 9)
            lbl = tk.Label(parent, text=text, font=_font,
                           bg=COLORS["bg_alt"], fg=fg, cursor="hand2")

            def _enter(_e):
                lbl.config(font=(F, 9, "bold", "underline") if bold else (F, 9, "underline"),
                           fg=COLORS["accent_blue"])

            def _leave(_e):
                lbl.config(font=_font, fg=fg)

            # Tooltip FIRST, hover binds with add="+": plain bind() replaces
            # (this exact ordering bug is why name hover highlighting silently
            # never worked — Tooltip's bind wiped it).
            if tip_text:
                Tooltip(lbl, tip_text)
            lbl.bind("<Enter>", _enter, add="+")
            lbl.bind("<Leave>", _leave, add="+")
            lbl.bind("<Button-1>", lambda e, u=url: self._open_url(u))
            return lbl

        def _checkbox_width_spacer(parent):
            """Width-matched invisible spacer holding the checkbox's slot in
            manual-only rows, so their names align exactly with the names in
            checkbox rows (user-reported misalignment). Sized from a real
            hidden Checkbutton — can't drift from theme/font changes the way
            a hardcoded pixel count would.

            Perf fix (2026-09-05, Install-tab lag report): this used to
            create AND destroy a real Checkbutton for every single
            manual-only row, on every category slice — dozens of throwaway
            widget creations (each a real OS window handle) during the
            idle-time catalog build, adding up to visible stutter switching
            into the tab. The probe measurement is identical every time
            (same theme/font for the whole tab lifetime), so it's now
            computed ONCE per InstallTab and cached on self — every
            subsequent manual row just reuses the cached width."""
            w = getattr(self, "_cb_spacer_width", None)
            if w is None:
                cb_probe = tk.Checkbutton(parent, bg=COLORS["bg_alt"],
                                           selectcolor=COLORS["surface"])
                w = cb_probe.winfo_reqwidth()
                cb_probe.destroy()
                self._cb_spacer_width = w
            sp = tk.Frame(parent, bg=COLORS["bg_alt"], width=w, height=1)
            sp.pack(side="left")
            sp.pack_propagate(False)   # hold the reserved width
            return sp

        for ci, app in enumerate(apps):
            var = tk.BooleanVar(value=False)
            self.vars[app["id"]] = var
            self._trace_install_var(var)
            row = tk.Frame(col_frames[ci % cols], bg=COLORS["bg_alt"])
            row.pack(fill="x", padx=4, pady=4)
            cb = tk.Checkbutton(row, variable=var, bg=COLORS["bg_alt"],
                                fg=COLORS["text"], activebackground=COLORS["bg_alt"],
                                selectcolor=COLORS["surface"], onvalue=True, offvalue=False)
            cb.pack(side="left")
            self._row_icon(row, app["name"])
            # tooltip: short description only (user request: no URLs in tips);
            # OEM-exclusive apps append their hardware tag (user request
            # 2026-09-06) so non-matching users are warned before installing
            tip = app["description"]
            hw = app.get("hardware", "")
            if hw:
                tip += f" ({hw})"
            name_lbl = _make_name_link(row, app["name"], app["url"], tip)
            name_lbl.pack(side="left", padx=(6, 4))
            if app["foss"]:
                # sky blue, NOT the tab accent: the accent IS green here, and
                # green is the "✓ Installed" badge's color — the two chips sat
                # next to each other in the same green (user feedback
                # 2026-09-06). accent_sky is the palette's designated Install
                # alt so FOSS/Installed read as different signals at a glance.
                foss = tk.Label(row, text="FOSS", font=(F, 7, "bold"),
                                bg=COLORS["bg_alt"], fg=COLORS["accent_sky"])
                foss.pack(side="left", padx=(4, 0))
            if hw:
                hw_lbl = tk.Label(row, text="⚠ OEM", font=(F, 7, "bold"),
                                  bg=COLORS["bg_alt"], fg=COLORS["accent_yellow"])
                Tooltip(hw_lbl, f"Hardware restricted — {hw}. Do not install this on other systems.")
                hw_lbl.pack(side="left", padx=(4, 0))
            # mirror link (user design): 🔗 beside the name as the SECONDARY
            # link when a verified fallback_url exists; the name stays the
            # primary. ↗ arrows are gone entirely.
            fallback = app.get("fallback_url", "")
            if fallback:
                self._make_mirror_link(row, fallback)
            Tooltip(cb, tip)
            cat_rows.append((app["id"], row))
            self._app_rows[app["id"]] = row

        # manual-only entries (no winget package exists): same row shape as
        # winget apps (user design) — NO checkbox (a width-matched spacer
        # keeps names aligned with checkbox rows, user-reported fix), normal
        # text color, name IS the link, FOSS badge when flagged, plus 🔗
        # when a mirror exists.
        for ci, app in enumerate(manuals):
            row = tk.Frame(col_frames[(len(apps) + ci) % cols], bg=COLORS["bg_alt"])
            row.pack(fill="x", padx=4, pady=4)
            _checkbox_width_spacer(row)
            self._row_icon(row, app["name"])
            fallback = app.get("fallback_url", "")
            # tooltip: short description only (user request: no URLs in tips;
            # the row has no checkbox, which already says "open the page")
            tip = app["description"]
            if fallback:
                tip += " (mirror available via 🔗)"
            name_lbl = _make_name_link(row, app["name"], app["url"], tip,
                                        base_fg=COLORS["text"])
            name_lbl.pack(side="left", padx=(6, 4))
            if app["foss"]:
                # sky blue to match the winget-row FOSS chips (green belongs
                # to the "✓ Installed" badge alone — see _paint_installed_badges)
                foss = tk.Label(row, text="FOSS", font=(F, 7, "bold"),
                                bg=COLORS["bg_alt"], fg=COLORS["accent_sky"])
                foss.pack(side="left", padx=(4, 0))
            if fallback:
                self._make_mirror_link(row, fallback)
            cat_rows.append((app["id"], row))

        # Embedded bundle/task rows (APO + Peace GUI, APO + FluidEQ, RustDesk,
        # FreeFileSync — anything in install_tasks.EMBEDDED_TASKS_BY_CATEGORY):
        # full-width checkbox rows at the END of the category, below the
        # columns. Search behavior (user request): these rows are NO LONGER
        # exempt from the filter — typing e.g. 'rufus' must clear the page
        # to ONLY matching apps, so they hide too unless the query matches
        # ('peace', 'fluideq', 'rustdesk'...). Each is registered as a
        # pseudo-app row the filter can match and hide.
        from app.tasks.install_tasks import EMBEDDED_TASKS_BY_CATEGORY as _EMBED
        for _bundle in _EMBED.get(cat, []):
            _bvar = tk.BooleanVar(value=False)
            self.bundle_vars[_bundle.key] = (_bvar, _bundle)
            self._trace_install_var(_bvar)
            _brow = tk.Frame(outer, bg=COLORS["bg_alt"])
            _brow.pack(fill="x", padx=10, pady=(2, 6))
            _bcb = tk.Checkbutton(_brow, variable=_bvar, bg=COLORS["bg_alt"],
                                   fg=COLORS["text"], activebackground=COLORS["bg_alt"],
                                   selectcolor=COLORS["surface"], onvalue=True, offvalue=False)
            _bcb.pack(side="left")
            self._row_icon(_brow, _bundle.label)
            _blbl = tk.Label(_brow, text=_bundle.label, font=(F, 9, "bold"),
                             bg=COLORS["bg_alt"], fg=COLORS["text"])
            _blbl.pack(side="left", padx=(6, 4))
            if _bundle.admin_required:
                _bsh = tk.Label(_brow, text="🛡️", font=("Segoe UI Emoji", 9),
                                bg=COLORS["bg_alt"], fg=COLORS["text"], cursor="hand2")
                _bsh.pack(side="left", padx=(6, 0))
                Tooltip(_bsh, "Admin Required — needs Administrator rights (skipped in limited mode)")
            Tooltip(_blbl, _bundle.description)
            Tooltip(_bcb, _bundle.description)
            # filter registration: searchable text from the bundle's own
            # label + description; stored on the OUTER block (its pack
            # master) so hiding one row never orphans the section logic.
            self._bundle_rows.append((_brow, f"{_bundle.label} {_bundle.description}".lower()))

        self._cat_frames[cat] = (header, body, True)
        self._cat_toggles[cat] = toggle_cat
        self._cat_sections.append((cat, header, body, cat_rows))

    def _make_mirror_link(self, row, fallback):
        """🔗 mirror link with a press-flash (user request: some animation /
        effect on click so it feels alive). Press lights it in the tab
        accent; release opens the page and restores it. Same emoji font and
        size as the 🛡️ badge; no underline, ever."""
        _font = ("Segoe UI Emoji", 9)
        mirror = tk.Label(row, text="🔗", font=_font,
                          bg=COLORS["bg_alt"], fg=COLORS["subtext"],
                          cursor="hand2")
        mirror.pack(side="left", padx=(4, 0))

        def _press(_e):
            try:
                mirror.config(fg=TAB_ACCENTS["Install"])
            except Exception:
                pass

        def _release(e, u=fallback):
            try:
                mirror.config(fg=COLORS["subtext"], font=_font)
            except Exception:
                pass
            self._open_url(u)

        mirror.bind("<ButtonPress-1>", _press)
        mirror.bind("<ButtonRelease-1>", _release)
        Tooltip(mirror, "Backup download (official mirror)")
        return mirror

    @staticmethod
    def _resolve_default_browser_cmd() -> "list[str] | None":
        """Resolve the default https browser's launch command from the
        registry (UserChoice ProgId -> shell\\open\\command), or None.

        Why: os.startfile opens the URL via ShellExecute — but when the
        browser is ALREADY RUNNING, Windows' focus-stealing prevention
        leaves it in the background (taskbar flash, no window raise).
        Launching the browser exe directly with the URL as argv raises
        its window reliably, even when it's already open (user-reported:
        'opens the link but doesn't bring up the browser in view').

        Registry reality (verified on a live machine): the ProgId command
        template is registered under HKEY_CLASSES_ROOT (the merged
        HKCU+HKLM view) — e.g. FirefoxURL-<hash>\\shell\\open\\command —
        and often NOT under HKCU\\Software\\Classes. Check both, HKCR
        first as the authoritative merged view."""
        try:
            import winreg
            import shlex
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                                r"Software\Microsoft\Windows\Shell\Associations\UrlAssociations\https\UserChoice") as k:
                prog_id, _ = winreg.QueryValueEx(k, "ProgId")
            cmd = None
            for root, prefix in ((winreg.HKEY_CLASSES_ROOT, prog_id),
                                 (winreg.HKEY_CURRENT_USER, rf"Software\Classes\{prog_id}")):
                try:
                    with winreg.OpenKey(root, rf"{prefix}\shell\open\command") as k:
                        cmd, _ = winreg.QueryValueEx(k, "")
                        break
                except OSError:
                    continue
            if not cmd:
                return None
            # template contains %1 (or empty) — strip it; shlex splits the
            # quoted exe path correctly
            cmd = cmd.replace("%1", "").replace("%*", "").strip()
            parts = shlex.split(cmd, posix=False)
            parts = [p.strip('"') for p in parts if p.strip('"')]
            return parts or None
        except Exception:
            return None

    @classmethod
    def _open_url(cls, url: str):
        """Open a URL in the user's browser AND bring the browser window to
        the foreground.

        Route 1 (new, user-reported fix): launch the default browser's exe
        directly with the URL as an argument — window raises even when the
        browser is already running (ShellExecute/startfile leave a running
        browser in the background behind focus-stealing prevention).
        Route 2: os.startfile / ShellExecuteW (works when no browser is
        running, or the exe launch failed).
        Route 3: cmd start (last resort). All failures are shown, never
        swallowed (the original 'links do nothing' bug on LTSC)."""
        import os
        import subprocess
        # Route 1: direct browser launch (raises window if already running)
        browser_cmd = cls._resolve_default_browser_cmd()
        if browser_cmd:
            try:
                subprocess.Popen([*browser_cmd, url],
                                  creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                return
            except Exception:
                pass  # fall through to the shell routes
        # Route 2: shell default handler
        try:
            os.startfile(url)
            return
        except Exception:
            pass
        # Route 3: explicit shell open
        try:
            subprocess.Popen(["cmd", "/c", "start", "", url], shell=False,
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            return
        except Exception as exc:
            messagebox.showerror("Could Not Open Link",
                                 f"Couldn't open this page:\n{url}\n\n{exc}")

    def _set_category(self, cat, on: bool):
        from app.app_catalog import APP_CATALOG
        for app in APP_CATALOG:
            if app["category"] == cat and app["id"] in self.vars:
                self.vars[app["id"]].set(on)
        try:
            self._refresh_install_count()
        except Exception:
            pass
