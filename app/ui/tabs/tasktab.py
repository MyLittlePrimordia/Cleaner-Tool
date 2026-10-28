"""TaskTab — extracted verbatim from app/gui.py (H13).

A feature tab page. May use the widget set and the modal base, and
opens dialogs, but never imports another tab.
"""
from __future__ import annotations

import threading

import tkinter as tk
from app.tab_presets import PRESETS, TABS
from app.compat import task_supported, unsupported_reason
from app.ui.base import Tooltip, _themed_askyesno, _themed_showinfo
from app.ui.theme import (
    COLORS, EMOJI, ICON_CHIP, ICON_NAV, PAGE_GUTTER, RADIUS_CARD,
    RADIUS_SURFACE, SPACE_1, SPACE_2, SPACE_5, SURFACE_CARD, TAB_ACCENTS,
    TYPE, _TAB_STATUS, _hex_lerp, icon_target_px, scaled_photo,
)
from app.ui.metrics import (
    CARD_BODY_INSET, COMPACT_INSET, CONTROL_GAP, GROUP_GAP, HAIRLINE_NUDGE,
    TEXT_NUDGE, TIGHT_GAP,
)
from app.ui.widgets import (AnimatedButton, RoundedCard,
                            ScrollableRoundedPanel, ToggleSwitch,
                            flow_columns, grid_cards)
from app.ui.widgets.feature_card import feature_card_body

#: The Tweak run-row's Undo pill label. A CONSTANT, and that is the whole
#: point: it used to be built as an f-string carrying the applied count, and
#: because it sits beside Run in a CENTRED row, a count that changes on every
#: run physically moved the app's primary button. A number cannot live in a
#: fixed-width slot, so it moved to the status line instead (see
#: TaskTab._refresh_tweak_pills / _refresh_tweak_status). Module level so the
#: build site and the refresh site cannot disagree about the string.
UNDO_LABEL = "↩ Undo"


class TaskTab(tk.Frame):
    """One of the three tab pages.

    Modes (Phase 2 #13):
      * preset mode — curated preset cards + Custom card; selecting a
        preset shows a plain-language summary; no checkboxes at all.
      * custom mode — full animated toggle grid (all tasks for the tab).
      * undo mode (Tweak only) — all-off grid of reversible tweaks; toggles
        choose what to revert using real snapshotted prior values.
    """

    PRESET_BLURBS = {
        "Quick Clean": "Everyday junk — fast and safe.",
        "Deep Clean": "Deeper junk, browsers, old files.",
        "Quick Repair": "Fast checks for common problems.",
        "Deep Repair": "Full fix stack — 30+ minutes.",
        "Minimal": "Safe speed basics, no risk.",
        "Recommended": "Best all-round setup + privacy.",
        "Game Session": "Max power for one play session.",
    }

    def __init__(self, parent, app, tab_name):
        super().__init__(parent, bg=COLORS["bg"])
        self.app = app
        self.tab_name = tab_name
        self.tasks = TABS[tab_name]
        self.task_by_key = {t.key: t for t in self.tasks}
        self.presets = PRESETS[tab_name]
        self.mode = "preset"          # preset | custom | undo
        self.vars = {}
        # UX (2026-10): Clean starts with Quick Clean selected, so the app's
        # "1-click" promise is literally true on first paint: Run is enabled
        # and the summary is already on screen. Previously nothing was
        # selected, Run was disabled, and every launch cost two clicks.
        self._selected_preset = (
            "Quick Clean"
            if tab_name == "Clean" and "Quick Clean" in self.presets
            else None)
        self._anim_jobs = []          # active after() ids owned by this page
        # grid engine state: cells built once, only re-gridded afterwards
        self._cells = None            # list[(widget, task)] in stable order
        self._cell_blocks = None      # [(group_header|None, [(cell, task)])]
        self._cell_cols = None
        self._regrid_after = None
        # body cache: built-once bodies per mode/preset (no laggy redraws)
        self._body_cache = {}
        self._body_vars = {}
        # F3: undo rows of the last-built undo grid, for in-place badge
        # repaints (each row stores its badge handle on the widget)
        self._undo_rows = []
        # TEXT-001: Application's run-in-progress gate arrives via
        # set_run_enabled() and is remembered here, so a later selection
        # change can re-apply it without Application needing to know about
        # per-tab selection state. Starts permissive: a freshly built tab is
        # not mid-run, and the selection gate is applied by
        # _refresh_run_count() at the end of _build_run_row().
        self._run_gate = True

        self._build()

    # ---------------- structure ---------------- #

    def _build(self):
        # Preset card area (top) — compact padding reclaims vertical space
        # so Clean/Repair Custom grids fit WITHOUT scrolling at default size
        self.preset_area = tk.Frame(self, bg=COLORS["bg"])
        self.preset_area.pack(fill="x", padx=PAGE_GUTTER, pady=(CARD_BODY_INSET, SPACE_1))

        # Summary / toggle-grid area (middle, swaps by mode)
        self.body_area = tk.Frame(self, bg=COLORS["bg"])

        # Run row (bottom) — packed FIRST (side=bottom) so it can never be
        # squeezed out of view (user-reported clipping: a tall content
        # frame used to push the Run button off-screen).
        self.run_row = tk.Frame(self, bg=COLORS["bg"])
        self.run_row.pack(side="bottom", fill="x", padx=PAGE_GUTTER, pady=(SPACE_1, GROUP_GAP))
        # Tweak run-row pills (3-card symmetry, 2026-09): the entries that
        # used to be Tweak's 2nd card row live beside Run as secondary
        # pills — Check, conditional Undo, Game Session preset. Built in
        # _build_run_row, painted by _refresh_tweak_pills. Tweak-only.
        self._rr_check = None
        self._rr_undo = None
        self._rr_session = None
        self.body_area.pack(side="top", fill="both", expand=True,
                            padx=PAGE_GUTTER, pady=(SPACE_1, SPACE_1))

        self._build_preset_cards()
        self._build_body()
        self._build_run_row()
        # F2: preset cards and the Run button are built HERE once and only
        # recolored/restyled on later mode switches (_highlight_cards /
        # _update_run_row) — never destroyed and recreated per switch.

    def _build_preset_cards(self):
        for w in self.preset_area.winfo_children():
            w.destroy()
        accent = TAB_ACCENTS[self.tab_name]

        # Tweak tab is 3 cards like every other tab (Minimal / Recommended /
        # Custom). Game Session lives in the Auto-Pilot dialog's manual
        # session; Undo Tweaks + Tweak Health live in the context strip
        # above the run row (see _build_context_strip) — cards are for
        # runnable presets, modes are not presets.
        cards = list(self.presets.keys())
        if self.tab_name == "Tweak":
            cards = [c for c in cards if c != "Game Session"] + ["Custom"]
        else:
            cards = cards + ["Custom"]
        self.preset_cards = {}
        # WRAP AT 3 PER ROW (user-reported clipping: a 5th card squeezed the
        # whole row). 3 across keeps every card readable at min window size;
        # extra cards flow to a second row instead of shrinking the rest.
        # Every tab has exactly 3 cards (one row): Clean 2 presets + Custom,
        # Repair 2 presets + Custom, Tweak Minimal/Recommended + Custom.
        #
        # The exact count is MEASURED through the same helper the Tools grid
        # uses (page.flow_columns), capped at 3: on a very narrow window a
        # third card would ellipsize its title, so two get a row and the
        # remainder is centred by grid_cards rather than stranded left.
        self._preset_order = []
        self._preset_cols = 0
        for col, name in enumerate(cards):
            # REDESIGN 2026-10: icon ASSET KEYS, not emoji glyphs.
            # These four cards were the last full-colour emoji left in the
            # app. Emoji cannot be themed - Tk renders them from the system
            # font - so they ignored the palette entirely, which is the one
            # thing a design system exists to prevent. The replacements are
            # monochrome PNGs in the owning tab's accent. The emoji map below
            # is retained as the FALLBACK, so a missing or unloadable asset
            # degrades to the old look rather than to a blank card.
            if name == "Custom":
                blurb, icon_key = "Pick exactly what runs.", "custom"
                color, fg = SURFACE_CARD, COLORS["text"]
                command = lambda: self._enter_custom()
            else:
                blurb = self.PRESET_BLURBS.get(name, "")
                icon_key = {"Quick Clean": "quickclean",
                            "Deep Clean": "deepclean",
                            "Quick Repair": "quickrepair",
                            "Deep Repair": "deeprepair",
                            "Minimal": "minimal",
                            "Recommended": "recommended",
                            "Game Session": "gamesession"}.get(name, "")
                color, fg = SURFACE_CARD, COLORS["text"]
                command = (lambda n=name: self._select_preset(n))
            card = self._make_card(self.preset_area, name, blurb, icon_key,
                                   color, fg, command, accent)
            self._preset_order.append(card)
            self.preset_cards[name] = card
        self._regrid_presets(self.preset_area.winfo_width())
        self.preset_area.bind("<Configure>", self._on_presets_configure,
                              add="+")

    def _on_presets_configure(self, event):
        self._regrid_presets(getattr(event, "width", 0))

    def _regrid_presets(self, width):
        """Re-flow the three preset cards at the measured column count."""
        width = int(width or 0)
        if width <= 1:
            width = 760
        cols = flow_columns(width, max_columns=3)
        if cols == self._preset_cols:
            return cols
        self._preset_cols = cols
        grid_cards(self.preset_area, self._preset_order, cols)
        return cols

    def _make_card(self, parent, title, blurb, icon_key, bg, fg, command, accent):
        # REDESIGN 2026-10: the card face is a RoundedCard, so it shares a
        # radius with the Tools cards, the list rows and the stat tiles. It
        # used to be a square Frame inside a 1px hairline Frame, which is why
        # these three cards - the first thing on screen on four of five tabs -
        # were the most visibly "not designed" surface in the app.
        # min_height pins every card to the same height from the first paint,
        # so the grid can no longer start small and then jump when the blurb
        # wraps (the "third card resizes" bug).
        face = SURFACE_CARD
        outer = RoundedCard(parent, fill=face, bg=COLORS["bg"],
                            radius=RADIUS_SURFACE, min_height=144)
        inner = outer.body
        _photo = self._preset_icon_image(icon_key)
        glyph = self._ICON_FALLBACK.get(icon_key, "\u2022")

        def _click():
            command()

        def _hover(on):
            self._card_hover(inner, outer, accent, on)

        head, title_lbl, blurb_lbl, icon_lbl, _status = feature_card_body(
            outer, title, blurb, accent, photo=_photo, glyph=glyph,
            compact=False, face=face, click=_click, hover=_hover)
        # The card is a keyboard target too (C-008): canvas cards used to be
        # mouse-only despite being the primary action on the page.
        try:
            outer.configure(takefocus=1)
            outer.bind("<Return>", lambda _e: command(), add="+")
            outer.bind("<space>", lambda _e: command(), add="+")
            outer.bind("<FocusIn>",
                      lambda _e: self._card_hover(inner, outer, accent, True),
                      add="+")
            outer.bind("<FocusOut>",
                      lambda _e: self._card_hover(inner, outer, accent, False),
                      add="+")
        except Exception:
            pass

        # Two tooltips, not seven (audit item 7): each one sits on the label
        # it completes, so hovering the card padding says nothing and
        # hovering an ellipsized title/blurb still gives the full text. The
        # old code attached the same string to every part of the card.
        for _w, _text in ((title_lbl, title), (blurb_lbl, blurb or title)):
            try:
                Tooltip(_w, _text)
            except Exception:
                pass

        # click + hover on the container itself as well: the helper binds the
        # inner parts, and drags that start on the rounded face still work.
        for w in (outer, inner):
            w.bind("<Button-1>", lambda e: command(), add="+")
            w.bind("<Enter>", lambda e: _hover(True), add="+")
            w.bind("<Leave>", lambda e: _hover(False), add="+")
        return outer

    #: Emoji fallbacks for the card icons (redesign 2026-10). Used ONLY when
    #: the PNG asset is missing or will not load, so a packaging mistake
    #: degrades to the previous look instead of an empty card. The PNGs live
    #: in app/assets/preset_icons/.
    _ICON_FALLBACK = {
        "quickclean": "🧹", "deepclean": "🧼", "quickrepair": "🩺",
        "deeprepair": "🔧", "minimal": "🚀", "recommended": "⭐",
        "gamesession": "🎮", "custom": "⚙",
    }

    #: Delivered PNG size is 48; the card renders at 24, matching the Tools
    #: icons (toolstab._build_card subsamples to <=24 too). One constant so
    #: the two icon sets cannot drift apart.
    CARD_ICON_PX = ICON_NAV

    def _preset_icon_image(self, icon_key):
        """A preset icon as a PhotoImage at CARD_ICON_PX, or None.

        Shared by the preset cards and the Game Session pill so both load the
        asset the same way and neither can end up on a different size. The
        image is cached on the instance: a widget holds only the image's NAME,
        so an uncached PhotoImage would be collected and render as nothing.
        """
        if not icon_key:
            return None
        if not hasattr(self, "_card_photos"):
            self._card_photos = {}
        if icon_key in self._card_photos:
            return self._card_photos[icon_key]
        photo = None
        try:
            from app.utils import resolve_asset_path
            p = resolve_asset_path("preset_icons/%s.png" % icon_key)
            if p:
                photo = tk.PhotoImage(file=str(p))
                # Snap the asset to the DPI-aware target (theme.icon_target_px)
                # rather than a constant: fonts scale, so an icon that stayed
                # 36 physical px at 200% read as tiny beside its own title.
                # scaled_photo reaches targets a bare subsample cannot (48px
                # from the 72px asset at 200%), where rounding used to pick
                # the 36px state and leave the mark undersized.
                photo = scaled_photo(
                    photo, icon_target_px(self.CARD_ICON_PX, self))
        except Exception:
            photo = None
        self._card_photos[icon_key] = photo
        return photo


    def _card_hover(self, inner, outer, accent, on):
        """Animated hover for preset cards (~140 ms ease).

        Skips animation when the card is the selected preset so selected
        state stays rock-solid. Otherwise eases surface → surface_hover and
        outline to the tab accent.
        """
        from app.ui.theme import _hex_lerp

        # Don't fight the selected-state paint.
        active = {"preset": self._selected_preset, "custom": "Custom"}.get(
            self.mode)
        try:
            # Identify this card by matching the RoundedCard in preset_cards.
            for name, card in getattr(self, "preset_cards", {}).items():
                if card is outer and name == active:
                    return  # selected card: leave highlight alone
        except Exception:
            pass

        # _highlight_cards() can create _hov_t/_hov_target before the first
        # hover, so each attribute is initialised on its own.
        if not hasattr(outer, "_hov_t"):
            outer._hov_t = 0.0
        if not hasattr(outer, "_hov_target"):
            outer._hov_target = 0.0
        if not hasattr(outer, "_hov_after"):
            outer._hov_after = None

        rest_fill = SURFACE_CARD
        hover_fill = COLORS["surface_hover"]
        rest_outline = ""     # M3 filled card: hover is a state layer only

        def _apply(t):
            try:
                fill = _hex_lerp(rest_fill, hover_fill, t)
                outline = rest_outline
                outer.set_fill(fill)
                outer._outline = outline
                outer._drawn = None
                outer._paint()
                for w in outer._descendants():
                    try:
                        w.config(bg=outer.fill())
                    except Exception:
                        pass
            except Exception:
                pass

        def _tick():
            target = outer._hov_target
            cur = outer._hov_t
            if abs(cur - target) < 0.03:
                outer._hov_t = target
                outer._hov_after = None
                _apply(target)
                return
            outer._hov_t = cur + (target - cur) * 0.32
            _apply(outer._hov_t)
            outer._hov_after = outer.after(16, _tick)

        outer._hov_target = 1.0 if on else 0.0
        if outer._hov_after is None:
            _tick()

    # ---------------- Tweak run-row pills (3-card symmetry) ---------------- #

    def _build_tweak_pills(self, btns):
        """Secondary pills beside the Tweak Run hero (same AnimatedButton
        language as Run/Preview, stepped down to 10pt so Run dominates).
        Commands are stable bound methods; each keeps a `._cmd` alias so
        the headless harness invokes the exact callable a click would run
        (event_generate is never delivered to unmapped widgets)."""
        # Preview (roadmap 2.3): the Tweak counterpart of Clean's Preview —
        # answers "what will this change?" before Run, via the read-only
        # app.tweak_introspect (static parse + read-only registry reads; it
        # never applies anything).
        self._rr_preview = AnimatedButton(
            btns, text="Preview", command=self._tweak_preview_clicked,
            role="outlined", size="lg")
        self._rr_preview._cmd = self._tweak_preview_clicked
        self._rr_check = AnimatedButton(
            btns, text="Check", command=self._enter_health,
            role="secondary", size="lg")
        self._rr_check._cmd = self._enter_health
        self._rr_undo = AnimatedButton(
            btns, text=UNDO_LABEL, command=self._enter_undo,
            role="danger", size="lg")
        self._rr_undo._cmd = self._enter_undo
        # The Game Session pill carries the SAME generated icon as the preset
        # cards (redesign 2026-10). It used to inline the 🎮 emoji, which
        # meant one system-font glyph sitting beside seven themed PNGs - the
        # exact inconsistency the icon set exists to remove. Falls back to the
        # emoji text if the asset is unavailable.
        _gs_img = self._preset_icon_image("gamesession")
        self._rr_session = AnimatedButton(
            btns,
            text="Game Session" if _gs_img is not None
            else self._ICON_FALLBACK["gamesession"] + " Game Session",
            command=lambda: self._select_preset("Game Session"),
            image=_gs_img, role="secondary", size="lg")
        self._rr_session._cmd = lambda: self._select_preset("Game Session")
        self._refresh_tweak_pills()

    def _context_applied_count(self) -> int:
        """Applied tweaks that still map to a real reversible task (same
        filter the health body uses — counts only what Undo/Health can
        actually act on). Reads the live registry through the app's read-only
        view (H11) — there is no cache left to lag; never raises."""
        try:
            state = self.app.tweak_state or {}
        except Exception:
            return 0
        n = 0
        try:
            for key in state:
                t = self.task_by_key.get(key)
                if t is not None and t.revert is not None:
                    n += 1
        except Exception:
            pass
        return n

    def _refresh_tweak_pills(self):
        """Paint the Tweak run-row pills.

        SYMMETRY (design audit 2026-10). Two width changes here used to move
        the app's primary button. The pills sit beside Run, so when a pill
        appeared or changed width the whole group re-centred and Run - the
        highest-frequency control in the app - slid sideways. Fitts's Law
        says a target that moves between attempts is a target you miss.

        The fix is NOT in this method. The run row is now a three-column grid
        (see _build_run_row) with the hero pinned in a zero-weight centre
        column between two equal flexible ones, and these pills live in the
        right-hand one. So they can now come and go, and change width, as
        much as they like without Run moving by a single pixel.

        What remains here is only the count: neither label may carry it,
        because a count that changes on every run is still a moving target
        for anyone scanning the row, and a fixed-width slot cannot hold a
        number of unknown length. The applied count moved to the STATUS LINE,
        where this app keeps live state and where a changing number belongs.

        No-op on other tabs and in health/undo modes (their run rows have no
        pills). Order is re-asserted every pass so a rebuild cannot reorder it.
        """
        if self.tab_name != "Tweak":
            return
        try:
            c = self._rr_check
            if c is None or not c.winfo_exists():
                return
        except Exception:
            return
        if getattr(self, "mode", "preset") not in ("preset", "custom"):
            return
        n = self._context_applied_count()
        try:
            for w in (self._rr_preview, self._rr_check, self._rr_undo,
                      self._rr_session):
                try:
                    if w is not None and w.winfo_exists() and w.winfo_manager() == "pack":
                        w.pack_forget()
                except Exception:
                    pass
            p = self._rr_preview
            p.config_text("Preview")
            p.pack(side="left", padx=CONTROL_GAP)
            c.config_text("Check")
            c.pack(side="left", padx=CONTROL_GAP)
            # Undo still hides with nothing applied - no permanently dead
            # control on screen. The grid above is what keeps Run still, so
            # the old reason for reserving a slot is gone.
            if n > 0:
                u = self._rr_undo
                if u is not None and u.winfo_exists():
                    u.config_text(UNDO_LABEL)
                    u.pack(side="left", padx=CONTROL_GAP)
            s = self._rr_session
            s.pack(side="left", padx=CONTROL_GAP)
        except Exception:
            pass
        self._tweak_applied = n
        self._refresh_tweak_status()

    def _refresh_tweak_status(self):
        """Show the applied-tweak count in the status line (Tweak only).

        The home the pill labels gave up (see _refresh_tweak_pills). It never
        overwrites a live run: while a run owns the status line the count is
        withheld, and the next pill refresh re-shows it.
        """
        n = getattr(self, "_tweak_applied", None)
        if n is None:
            return
        try:
            app = self.app
            if getattr(app, "active_tab", "") != "Tweak":
                return
            if getattr(app, "_busy", False):
                return
            # set_status already tints by the active tab's accent, so there is
            # no colour to pass and no second thing to keep in sync. Plural
            # is built here rather than with an f-string ternary inline,
            # because "1 tweaks applied" is the kind of thing that ships.
            n_word = "tweak" if n == 1 else "tweaks"
            app.set_status(("%d %s applied." % (n, n_word)) if n
                           else _TAB_STATUS.get("Tweak", ""))
        except Exception:
            pass

    def _tweak_preview_clicked(self):
        """Show what the currently selected tweaks will change (roadmap 2.3).

        Read-only end to end: app.tweak_introspect parses the task source and
        reads the current registry values, but never writes, so opening this
        from the run row cannot change the machine. The selection comes from the
        same selected_tasks() the run itself uses, so the preview matches what
        Run would apply."""
        selected = self.selected_tasks()
        if not selected:
            return
        keys = [t.key for t in selected]
        try:
            from app.tweak_introspect import preview_lines
            lines = preview_lines(keys)
        except Exception:
            lines = []
        try:
            from app.ui.dialogs.tweak_preview_dialog import show_tweak_preview
            show_tweak_preview(self.app.root, lines)
        except Exception:
            pass

    def _build_body(self):
        """Body cache (user feedback: mode switches redrew ~50 toggle
        widgets and looked laggy). Bodies are built once per mode and
        cached; switching modes just shows/hides — instant, and Custom
        toggle choices survive a trip to a preset and back.

        self.vars must follow the SHOWN body: each cached body remembers
        its own var dict (custom and undo bodies have different var sets),
        and showing a cached body restores its dict — otherwise Run would
        read the previous mode's vars (bug caught by the layout test).

        F8 (user bug report 2026-09-06: 'the Custom filter search box does
        nothing'): the cache-hit path restored ONLY vars. _cell_blocks /
        _cells still pointed at whatever grid was built LAST (the preset
        summary, or another mode's grid), so the filter's grid_remove()s
        landed on a HIDDEN grid while the visible one never changed —
        'it still shows all the toggles'. The cache now stores the full
        (vars, cells, cell_blocks) tuple and every cache hit restores all
        three, so _cell_blocks always describes the SHOWN body."""
        # hide all cached bodies
        for w in self.body_area.winfo_children():
            w.pack_forget()
        cache_key = self.mode if self.mode != "preset" else f"preset:{self._selected_preset}"
        if cache_key in self._body_cache:
            self._body_cache[cache_key].pack(fill="both", expand=True)
            # F8b: pack_forget/pack of a body whose panel embeds content
            # via create_window() leaves the embedded frame UNMAPPED (Tk
            # quirk — see ScrollableRoundedPanel.remap_children). The
            # re-assert must run AFTER the repack's geometry settles
            # (itemconfigure issued pre-mapping is ignored by Tk —
            # reproduced minimal, 2026-09-06), so it rides the idle
            # queue, one event-loop turn later.
            _panel_fix = None
            for _w in self._body_cache[cache_key].winfo_children():
                if isinstance(_w, ScrollableRoundedPanel):
                    _panel_fix = _w
                    break
            if _panel_fix is not None:
                def _remap(_p=_panel_fix):
                    try:
                        if _p.winfo_exists():
                            _p.remap_children()
                    except Exception:
                        pass
                try:
                    _panel_fix.after_idle(_remap)
                except Exception:
                    pass
            if cache_key in self._body_vars:
                cached = self._body_vars[cache_key]
                if isinstance(cached, tuple) and len(cached) == 3:
                    self.vars, self._cells, self._cell_blocks = cached
                else:
                    self.vars = cached   # legacy shape — defensive
            return
        wrap = tk.Frame(self.body_area, bg=COLORS["bg"])
        self._body_cache[cache_key] = wrap
        wrap.pack(fill="both", expand=True)
        if self.mode == "preset":
            self._build_summary_body(wrap)
        elif self.mode == "custom":
            self._build_toggle_grid(wrap, mode="run")
        elif self.mode == "health":
            self._build_health_body(wrap)
        else:
            self._build_toggle_grid(wrap, mode="undo")
        # Builders refresh self.vars — remember this body's FULL grid state
        # (F8) so a later cache hit can restore all of it.
        #
        # SUM-002: presets were deliberately excluded here on the assumption
        # that only the toggle grids had cells. They do have cells — the
        # summary chips — so switching Quick Clean -> Deep Clean -> Quick
        # Clean left `_cells`/`_cell_blocks` pointing at the HIDDEN Deep Clean
        # chips while the visible panel showed Quick Clean's. The panel's own
        # resize callback then re-gridded the wrong list into it. Every body
        # now records its grid state, whichever mode built it.
        self._body_vars[cache_key] = (
            self.vars, getattr(self, "_cells", None),
            getattr(self, "_cell_blocks", None))

    def _clear_body_cache(self):
        """Drop cached bodies (called when tab's tasks/state change — undo
        badges depend on live tweak state). MUST run on the Tk thread: it
        destroys widgets (worker-thread Tcl calls are unsafe) and the old
        dict-drop-only version leaked every cached wrap as a hidden child
        of body_area on each run. Rebuilds the visible mode immediately so
        the panel never shows blank and badges reflect post-run state."""
        for wrap in list(self._body_cache.values()):
            try:
                if wrap is not None and wrap.winfo_exists():
                    wrap.destroy()
            except Exception:
                pass
        self._body_cache = {}
        self._body_vars = {}
        try:
            self._build_body()
        except Exception:
            pass
        # health mode: its run row carries a live applied-count label
        # ('Re-apply All (N)'), so it must rebuild with the body — other
        # modes' run rows are count-agnostic until the next toggle
        if getattr(self, "mode", "") == "health":
            try:
                self._update_run_row()
            except Exception:
                pass
        # context strip counts follow post-run registry state
        try:
            self._refresh_tweak_pills()
        except Exception:
            pass

    def _prewarm_body(self, mode: str) -> bool:
        """F6(b): build one body into the cache WITHOUT showing it, so the
        first real click is a cache hit (~2 ms) instead of a 100-450 ms
        first build. Runs once from Application's idle pre-warm chain after
        the window is up; a no-op when that body already exists (the user
        got there first and the normal lazy build owns it).

        `mode` is "custom", "undo", or "preset:<name>". Preset summaries
        are the expensive ones on a big preset (Tweak's 27-task summary is
        a card, a canvas and a tooltip per task), and they were the last
        synchronous body build left on a click: switching Quick -> Deep
        froze the window long enough for Windows to ghost it. Pre-building
        them turns that switch into the same cheap show/hide as the others.

        The builder mutates page state as it goes (mode, _selected_preset,
        vars, _cells, _cell_blocks), so that state is snapshotted and
        restored. Only the cache entry (hidden widgets + its state tuple) is
        kept. _undo_rows is deliberately NOT restored: an undo pre-warm only
        runs while no undo body exists, so its rows are exactly the rows the
        next _enter_undo shows and badge-refreshes in place (F3).
        """
        preset = None
        if mode.startswith("preset:"):
            preset = mode.split(":", 1)[1]
            if preset not in self.presets:
                return False
        elif mode not in ("custom", "undo"):
            return False
        if mode in self._body_cache:
            return False
        saved = (self.mode, self._selected_preset, self.vars,
                 getattr(self, "_cells", None),
                 getattr(self, "_cell_blocks", None))
        try:
            wrap = tk.Frame(self.body_area, bg=COLORS["bg"])
            self._body_cache[mode] = wrap
            if preset is not None:
                self.mode = "preset"
                self._selected_preset = preset
                self._build_summary_body(wrap)
            else:
                self.mode = mode
                self._selected_preset = None
                self._build_toggle_grid(
                    wrap, mode="run" if mode == "custom" else "undo")
            # F8: store the FULL grid state — vars alone left _cell_blocks
            # stale on the cache-hit path, which is exactly why the Custom
            # filter search box appeared dead (it filtered a hidden grid).
            self._body_vars[mode] = (self.vars, self._cells, self._cell_blocks)
            # the hidden wrap measures ~1px wide, so labels were wrapped to
            # the narrow fallback — the grid's own debounced <Configure>
            # refit (_fit_toggle_labels) re-wraps them to the real column
            # width the moment this body is first shown.
            wrap.pack_forget()  # never packed — stays hidden until shown
            return True
        except Exception:
            # a failed pre-warm must never crash the idle chain — drop the
            # half-built cache entry; the lazy first-click build takes over
            self._body_cache.pop(mode, None)
            self._body_vars.pop(mode, None)
            return False
        finally:
            self.mode, self._selected_preset, self.vars, self._cells, \
                self._cell_blocks = saved

    def _build_summary_body(self, wrap):
        """Read-only plain-language summary of the selected preset (no
        checkboxes — punch-list #13 'no checkboxes for curated tiers')."""
        if self._selected_preset is None:
            # TEXT-001 / VOID-001 (design audit 2026-09-27). This used to
            # render a centered "Pick a <tab> preset above to see what it
            # does." label with pack(expand=True). Two problems: it repeated
            # what the status bar already said (three simultaneous statements
            # of one instruction, per the audit), and because body_area is
            # pack(side="top", fill="both", expand=True) a lone line of text
            # floating in the middle of the ~330px it expands into read as a
            # hole in the layout / a page that failed to load.
            #
            # The status bar is now the single source of truth and is
            # per-tab correct (_TAB_STATUS, applied in Application._show_tab),
            # so the body just reserves the space the task grid will occupy
            # once a preset is chosen. That reads as deliberate margin.
            return
        keys = self.presets[self._selected_preset]
        # User request: drop the "Preset — N tasks · blurb" header row so
        # the scroll panel gains vertical space (card above already names it).
        # OLED redesign: summary list floats on pure black.
        panel = ScrollableRoundedPanel(wrap)
        panel._fill = COLORS["bg"]
        panel.inner.configure(bg=COLORS["bg"])
        try:
            panel._redraw_bg()
        except Exception:
            pass
        panel.pack(fill="both", expand=True)

        tasks = [self.task_by_key[k] for k in keys]
        cells = []
        face = SURFACE_CARD
        for t in tasks:
            # Compact surface chips floating on black.
            cell = RoundedCard(panel.inner, fill=face,
                               bg=COLORS["bg"], radius=RADIUS_CARD,
                               min_height=34)
            # M3 assist-chip leading icon: a tonal circle with a check mark
            _acc = TAB_ACCENTS[self.tab_name]
            dot = tk.Canvas(cell.body, width=ICON_CHIP, height=ICON_CHIP,
                            bg=face, highlightthickness=0)
            dot.create_oval(1, 1, 19, 19, fill=_hex_lerp(face, _acc, 0.30),
                            outline="")
            dot.create_line(6, 10, 9, 13, 14, 7, fill=_acc, width=2,
                            capstyle="round", joinstyle="round")
            dot.pack(side="left", padx=(SPACE_1, SPACE_2),
                     anchor="center", pady=TIGHT_GAP)
            # bd=0 is load-bearing, not tidiness: tk.Label's DEFAULT
            # borderwidth is 1, which adds 2px to every single line. With the
            # card now sizing itself from its content, that 2px compounded
            # into 47px rows and pushed Tweak's 27-task preset into a scroll -
            # a regression this change caused. Same note as the health
            # report's cards (health_report_dialog.py, BUG-022).
            lbl = tk.Label(cell.body, text=t.label, font=TYPE["caption"],
                           bg=face, bd=0,
                           fg=COLORS["text"], anchor="w", justify="left",
                           wraplength=150)
            lbl.pack(side="left", anchor="w", pady=TEXT_NUDGE)
            cell._wrap_label = lbl  # so _regrid can retarget it per column width
            Tooltip(lbl, t.description)
            # N-004: risk / build chip, matching the toggle grid.
            if not task_supported(t):
                self._badge(cell.body, "Not for this build",
                            COLORS["accent_red"], COLORS["alert_red_bg"],
                            unsupported_reason(t), pady=TEXT_NUDGE)
            elif getattr(t, "risk", "SAFE") == "ADVANCED":
                self._badge(cell.body, "Advanced", COLORS["accent_yellow"],
                            COLORS["alert_amber_bg"],
                            "Advanced tweak — reversible.", pady=TEXT_NUDGE)
            elif getattr(t, "risk", "SAFE") == "REBOOT REQUIRED":
                self._badge(cell.body, "Reboot", COLORS["accent_orange"],
                            COLORS["alert_amber_bg"],
                            "Reboot Required — Windows needs a restart after "
                            "this one.", pady=TEXT_NUDGE)
            elif getattr(t, "risk", "SAFE") == "DATA LOSS":
                self._badge(cell.body, "Data loss", COLORS["accent_red"],
                            COLORS["alert_red_bg"],
                            "Deletes data that cannot be recovered. Review "
                            "before selecting.", pady=TEXT_NUDGE)
            cells.append((cell, t))
        self._cells = cells
        self._cell_blocks = [(None, cells)]   # summary has no group headers
        panel.resize_decide_cb = lambda: self._regrid(panel)
        self._regrid(panel, force=True)

    def _regrid(self, panel, force=False):
        """Re-grid existing summary cells into a fixed 3-column flow.

        No widget rebuilds -> no toggle redraw flicker, no state loss.
        Columns are FIXED at 3 (not fitted 3-5): the fitter squeezed wide
        presets (Recommended, Deep Clean) into 4-5 narrow columns whose
        labels wrapped mid-word, while small presets sat at 3 — every
        summary now shares one density and longer lists simply scroll in
        the panel that already exists for exactly that. Column width still
        retargets each cell's wraplength every pass, so text never clips
        into its neighbor at any window size.

        Layout unit is self._cell_blocks: [(header_or_None, [(cell, task)])].
        Headers span the full width; each block's cells flow below its
        header. (The preset summary sets a single headerless block.)"""
        blocks = getattr(self, "_cell_blocks", None)
        if not blocks:
            return
        # list-mode rows (Custom/Undo grids) are packed full-width, not
        # gridded — there is nothing to re-fit on resize.
        try:
            _probe = blocks[0][0] or (blocks[0][1][0][0] if blocks[0][1] else None)
            if _probe is not None and _probe.winfo_manager() == "pack":
                return
        except Exception:
            pass
        cells = [c for _h, bc in blocks for c in bc]
        if not cells:
            return
        # Defence in depth for the stale-state bug above: a cell can only be
        # gridded by the panel that owns it. Re-gridding another body's cells
        # here is what Tk would otherwise answer with a hard TclError inside
        # an `after` callback.
        try:
            if cells[0].master is not panel.inner:
                return
        except Exception:
            pass
        panel.update_idletasks()
        inner_w = max(10, panel.winfo_width() - panel.INSET_X - panel.SB_W - 2)
        cols = 3
        r = 0
        # Audit fix (text clipping): retarget every cell's label to the
        # REAL width this layout pass just chose for `cols`, instead of the
        # fixed 150/215px guess set at cell-creation time. Recomputed fresh
        # Recomputed fresh from inner_w/3 every pass. 12 = the cell's own grid padx
        # (6+6); ~30 = the dot canvas (8px) + its padx (6) + slack for the
        # optional trailing 🔄 reboot-required icon, so text never runs
        # into either neighbor.
        wrap_w = max(80, (inner_w // cols) - 12 - 30)
        for hdr, bcells in blocks:
            if hdr is not None:
                hdr.grid(row=r, column=0, columnspan=cols, sticky="ew",
                         padx=CONTROL_GAP, pady=(SPACE_2, TEXT_NUDGE))
                r += 1
            for i, (cell, _t) in enumerate(bcells):
                rr, cc = divmod(i, cols)
                cell.grid(row=r + rr, column=cc, sticky="nw", padx=CONTROL_GAP, pady=TEXT_NUDGE)
                lbl = getattr(cell, "_wrap_label", None)
                if lbl is not None:
                    try:
                        lbl.config(wraplength=wrap_w)
                    except Exception:
                        pass
            r += (len(bcells) + cols - 1) // cols
        # Re-measure the cards only AFTER every label has its final width.
        #
        # The cells are RoundedCards, which size themselves from their body's
        # requested height - and that request follows `wraplength`. Changing
        # wraplength does not recompute the request synchronously, so the old
        # per-cell `_sync()` here re-applied the height derived from the
        # PROVISIONAL 80px wrap the cells were built with (one line at 80px is
        # three lines). Long presets escaped it because the scroll panel's
        # extra geometry passes corrected them later; a short preset has no
        # further passes, so Quick Clean's 8 chips stayed 48-95px tall while
        # Deep Clean's 28 were 38px - the same component, two densities.
        #
        # One idle pass flushes every pending geometry request, then each card
        # converges in a single `_sync()`. SUM-001.
        try:
            panel.inner.update_idletasks()
        except Exception:
            pass
        for _hdr, bcells in blocks:
            for cell, _t in bcells:
                try:
                    cell._sync()
                except Exception:
                    pass
        for c in range(cols):
            panel.inner.grid_columnconfigure(c, weight=1, uniform="grid")
        for c in range(cols, 6):
            panel.inner.grid_columnconfigure(c, weight=0)
        self._cell_cols = cols
        panel.refresh_scroll()

    def _build_toggle_grid(self, wrap, mode="run"):
        """The only place toggles appear (Phase 2 #13/#14). Custom mode =
        pick what runs; Undo mode = pick what reverts (all default off)."""
        accent = TAB_ACCENTS[self.tab_name]

        if mode == "undo":
            # REDESIGN 2026-10: this was a full-width red banner reading
            # "↩  Undo — switches pick which tweaks to put back to how they
            # were." - twelve words of instruction, permanently on screen,
            # explaining what checkboxes do. It is replaced by a compact
            # state chip that says what the MODE is and carries the
            # explanation on hover. A banner that never changes says
            # nothing; a chip is a status, and status is what belongs there.
            banner = tk.Frame(wrap, bg=COLORS["alert_red_bg"])
            _chip = tk.Label(banner, text="↩  Undo", font=TYPE["label_medium"],
                             bg=COLORS["alert_red_bg"], fg=COLORS["accent_red"],
                             anchor="w", bd=0)
            _chip.pack(side="left", padx=GROUP_GAP, pady=COMPACT_INSET)
            Tooltip(_chip, "Pick which tweaks to put back to how they were.")
            banner.pack(anchor="center", pady=(0, CONTROL_GAP))

        # User request: remove All On / All Off / hint / Filter row so the
        # scroll panel gains vertical space on Clean/Repair/Tweak Custom.
        state = (self.app.tweak_state or {}) if mode == "undo" else {}

        # OLED redesign: panel floats on pure black (no grey tray).
        panel = ScrollableRoundedPanel(wrap)
        panel._fill = COLORS["bg"]
        panel.inner.configure(bg=COLORS["bg"])
        try:
            panel._redraw_bg()
        except Exception:
            pass
        panel.pack(fill="both", expand=True)
        self._grid_row = 0   # shared 2-column grid row cursor (grid mode)

        tasks = [t for t in self.tasks if (mode == "run" or t.revert is not None)]
        self.vars = {}
        # Custom grids are grouped by function (tab_presets.CUSTOM_GROUPS).
        # (User request, 2-column layout): each option is a compact single-
        # line settings row in a TWO-COLUMN grid — Install-tab density, so
        # tabs with dozens of options scroll roughly half as far. Details
        # live in the hover tooltip, not inline. The whole row stays
        # clickable — big targets, nothing truncated.
        from app.tab_presets import CUSTOM_GROUPS
        by_key = {t.key: t for t in tasks}
        grouped = CUSTOM_GROUPS.get(self.tab_name, [])
        blocks = []   # (header_or_None, [(row_outer, task), ...])
        cells = []
        # 2-column grid inside the panel; headers span both columns
        inner_grid = panel.inner
        # measure once: with 2 equal columns, each gets ~half the inner
        # width (minus scrollbar strip). Labels wrap to that, not a fixed
        # 560px that would clip in a column.
        inner_w = max(10, panel.winfo_width() - panel.INSET_X - panel.SB_W - 2)
        col_w = max(160, (inner_w - 24) // 2)   # 24 = grid padx total
        for _title, _keys in grouped:
            gtasks = [by_key[k] for k in _keys if k in by_key]
            if not gtasks:
                continue
            hdr = tk.Frame(inner_grid, bg=SURFACE_CARD)
            tk.Label(hdr, text=_title, font=TYPE["label_large"], bg=SURFACE_CARD,
                     fg=COLORS["text"]).pack(side="left", padx=(SPACE_2, 0), pady=SPACE_1)
            tk.Label(hdr, text=f"{len(gtasks)} options", font=TYPE["micro_regular"],
                     bg=SURFACE_CARD, fg=COLORS["subtext"]).pack(side="left", padx=CONTROL_GAP)
            hdr.grid(row=self._grid_row, column=0, columnspan=2,
                     sticky="ew", padx=CONTROL_GAP, pady=(GROUP_GAP, TEXT_NUDGE))
            self._grid_row += 1
            grows = []
            for i, t in enumerate(gtasks):
                row, col = divmod(i, 2)
                cell, _task = self._build_toggle_row(inner_grid, t, mode, state, accent, col_w)
                cell.grid(row=self._grid_row + row, column=col,
                          sticky="new", padx=CONTROL_GAP, pady=TEXT_NUDGE)
                grows.append((cell, t))
            self._grid_row += (len(gtasks) + 1) // 2
            blocks.append((hdr, grows))
            cells.extend([(c, t) for c, t in grows])
        # safety net (should be unreachable — tab_presets validates full
        # coverage): any task missing from the groups still shows up.
        _shown = {t.key for _h, grows in blocks for _c, t in grows}
        _missing = [t for t in tasks if t.key not in _shown]
        if _missing:
            grows = []
            for i, t in enumerate(_missing):
                row, col = divmod(i, 2)
                cell, _task = self._build_toggle_row(inner_grid, t, mode, state, accent, col_w)
                cell.grid(row=self._grid_row + row, column=col,
                          sticky="new", padx=CONTROL_GAP, pady=TEXT_NUDGE)
                grows.append((cell, t))
            self._grid_row += (len(_missing) + 1) // 2
            blocks.append((None, grows))
            cells.extend([(c, t) for c, t in grows])
        self._cells = cells
        self._cell_blocks = blocks
        # F3: remember the undo grid's rows so later Undo entries can
        # repaint '✓ Active' badges in place without rebuilding the grid
        if mode == "undo":
            self._undo_rows = list(cells)
        for c in (0, 1):
            inner_grid.grid_columnconfigure(c, weight=1, uniform="togglerows")
        # one-line guarantee (user request): col_w at build time is measured
        # before the window is laid out (often 1px), which froze every label
        # at a ~140px wrap and forced 2-line names. Refit label wraps to the
        # REAL column width on every grid resize (debounced) so names stay on
        # one row at any window size/DPI.
        _fit_after = {"id": None}

        def _fit_toggle_labels(*_):
            try:
                if _fit_after["id"] is not None:
                    inner_grid.after_cancel(_fit_after["id"])
            except Exception:
                pass

            def _do():
                _fit_after["id"] = None
                try:
                    w = max(10, panel.winfo_width() - panel.INSET_X - panel.SB_W - 2)
                    cw = max(160, (w - 24) // 2)
                    wrap = max(120, cw - 110)
                    for _h, _grows in blocks:
                        for _cell, _t in _grows:
                            _lbl = getattr(_cell, "_lbl", None)
                            if _lbl is not None and _lbl.winfo_exists():
                                _lbl.config(wraplength=wrap)
                except Exception:
                    pass
                # F6(b): bodies pre-warmed hidden measure ~1px wide, so rows
                # may have wrapped taller than they will at the real column
                # width — after the refit above, settle the scroll metrics
                # on the now-true content height.
                try:
                    panel.refresh_scroll()
                except Exception:
                    pass

            try:
                _fit_after["id"] = inner_grid.after(120, _do)
            except Exception:
                pass

        inner_grid.bind("<Configure>", _fit_toggle_labels)
        _fit_toggle_labels()
        panel.resize_decide_cb = None
        panel.refresh_scroll()

    def _badge(self, parent, text, fg, bg, tip, pady=0):
        """A compact pill for a task's risk / build status (N-004)."""
        lbl = tk.Label(parent, text=text, font=TYPE["label_small"], bg=bg, fg=fg,
                       padx=CONTROL_GAP, pady=HAIRLINE_NUDGE, bd=0, cursor="hand2")
        lbl.pack(side="left", anchor="center", padx=(CONTROL_GAP, 0), pady=pady)
        if tip:
            Tooltip(lbl, tip)
        return lbl

    def _build_toggle_row(self, parent, t, mode, state, accent, col_w=380):
        """One compact settings-row cell (2-column grid, Install-tab density):
        borderless box (user request: no outline frame), clickable anywhere (not just the switch), switch +
        single-line label. The full description lives in the hover tooltip
        (user request: inline 3-line descriptions doubled the scrolling).
        Hover never draws an outline or paints the row background (user
        request — a rectangular wash looked square around the pill toggle).
        The highlight lives only on the switch (its own hover brightening)
        and the task name (tinted toward the tab color). Registers its var;
        returns (outer, task)."""
        supported = (mode != "run") or task_supported(t)
        var = tk.BooleanVar(value=bool((t.default if mode == "run" else False)
                                       and supported))
        self.vars[t.key] = var
        try:
            var.trace_add("write", lambda *_: self._refresh_run_count())
        except Exception:
            pass
        # OLED redesign: rows sit on pure black (no grey tray cells).
        face = COLORS["bg"]
        outer = tk.Frame(parent, bg=face, cursor="hand2")
        inner = tk.Frame(outer, bg=face, cursor="hand2")
        inner.pack(fill="both", expand=True)
        sw = ToggleSwitch(inner, var)
        sw.pack(side="left", anchor="center", padx=(SPACE_2, TEXT_NUDGE), pady=CONTROL_GAP)
        if not supported:
            # N-004: a task that does not apply to this Windows build is
            # shown honestly rather than hidden, but cannot be switched on
            # (selected_tasks() refuses it even if a preset names it).
            try:
                sw.set_enabled(False)
            except Exception:
                pass
        txt = tk.Frame(inner, bg=face, cursor="hand2")
        txt.pack(side="left", anchor="center", fill="both", expand=True,
                 padx=(0, SPACE_2), pady=CONTROL_GAP)
        # single line: wraplength only guards ultra-long labels — most fit
        # one line at column width (switch 44 + its padx + row padx accounted)
        wrap = max(140, col_w - 44 - 10 - 16 - 12)
        lbl = tk.Label(txt, text=t.label, font=TYPE["label_medium"], bg=face,
                       fg=(COLORS["text"] if supported else COLORS["muted"]),
                       anchor="w", justify="left",
                       wraplength=wrap, cursor="hand2")
        lbl.pack(side="left", anchor="center")
        Tooltip(lbl, t.description)
        outer._lbl = lbl  # dynamic one-line fit (see _fit_toggle_labels)
        extra = []
        if mode == "run" and getattr(t, "admin_required", False):
            adm = tk.Label(txt, text="🛡️", font=EMOJI["inline"], bg=face,
                           fg=COLORS["text"], cursor="hand2")
            adm.pack(side="left", anchor="center", padx=(CONTROL_GAP, 0))
            Tooltip(adm, "Admin Required — needs Administrator rights (skipped in limited mode)")
            extra.append(adm)
        # N-004: risk tier + Windows-build compatibility, as compact chips.
        # SAFE shows nothing (the overwhelming majority — a chip on every row
        # is noise); ADVANCED and REBOOT REQUIRED get one, and a task that
        # does not apply to this build gets a red one and a disabled switch.
        if not supported:
            extra.append(self._badge(
                txt, "Not for this build", COLORS["accent_red"],
                COLORS["alert_red_bg"],
                unsupported_reason(t) + " It is switched off on this PC."))
        else:
            risk = getattr(t, "risk", "SAFE")
            if risk == "ADVANCED":
                extra.append(self._badge(
                    txt, "Advanced", COLORS["accent_yellow"],
                    COLORS["alert_amber_bg"],
                    "Advanced tweak — a bigger change than the safe defaults, "
                    "still fully reversible."))
            elif risk == "REBOOT REQUIRED":
                extra.append(self._badge(
                    txt, "Reboot", COLORS["accent_orange"],
                    COLORS["alert_amber_bg"],
                    "Reboot Required — Windows needs a restart after this one."))
            elif risk == "DATA LOSS":
                extra.append(self._badge(
                    txt, "Data loss", COLORS["accent_red"],
                    COLORS["alert_red_bg"],
                    "Deletes data that cannot be recovered (Recycle Bin, "
                    "captures, uninstalled apps). Review before selecting."))
        # F3: undo rows get a '✓ Active' badge label at build time, PACKED
        # only while the tweak is actually applied (state from the last
        # _build_toggle_grid call). The handle lives on the row so
        # _refresh_undo_badges can pack/unpack in place on later Undo
        # entries — the grid is never rebuilt just to refresh badges.
        if mode == "undo":
            b = tk.Label(txt, text="✓ Active", font=TYPE["label_small"],
                         bg=face, fg=COLORS["accent_green"], cursor="hand2")
            if t.key in state:
                b.pack(side="left", anchor="center", padx=(CONTROL_GAP, 0))
            extra.append(b)
            outer._undo_badge = b
        # Find-my-fastest-DNS entry (user-approved feature 5, 2026-09):
        # a mini "⚡ Test" button on the gaming_dns row only (Custom grid,
        # run mode). Deliberately NOT in `extra`: extra widgets inherit
        # the row's click-to-toggle binding, but this button must open
        # the tester instead of flipping the switch — its own binding is
        # the only one it carries (same structural exemption as the
        # ToggleSwitch itself).
        if mode == "run" and self.tab_name == "Tweak" and t.key == "gaming_dns":
            test_btn = AnimatedButton(
                txt, text="⚡ Test", command=self._open_dns_tester,
                bg=COLORS["surface_hover"], fg=COLORS["text"],
                size="sm")
            test_btn.pack(side="left", anchor="center", padx=(CONTROL_GAP, 0))
            # harness marker: the generic synth-events loop must skip this
            # button (a press+release would open a live tester dialog with
            # real scan workers mid-suite); its behavior is covered by the
            # dedicated dns tests instead
            test_btn._harness_skip = True
            Tooltip(test_btn, "Tests DNS providers on your connection.")

        # subtle hover (user request: NO outline, NO background wash — both
        # read as a square box around the pill toggle). Only the switch
        # brightens and the task name tints toward the tab color.
        _hi_fg = _hex_lerp(COLORS["text"], accent, 0.65)

        def _hover(on, ev=None):
            # Leave fires on every child crossing too — ignore those while
            # the pointer is still anywhere inside this row.
            if not on and ev is not None:
                try:
                    w = outer.winfo_containing(ev.x_root, ev.y_root)
                    p = w
                    while p is not None:
                        if p == outer:
                            return
                        p = p.master
                except Exception:
                    pass
            try:
                rest_fg = COLORS["text"] if supported else COLORS["muted"]
                lbl.config(fg=_hi_fg if on else rest_fg)
                sw._draw(hover=True) if on else sw._draw()
            except Exception:
                pass

        def _click(ev):
            if ev.widget is sw or not supported:
                return  # switch handles its own clicks; unsupported is inert
            var.set(not var.get())

        # add="+": the labels carry Tooltips (plain bind() would wipe them —
        # same ordering bug that once killed the Install name hover)
        for w in (outer, inner, txt, lbl, *extra):
            w.bind("<Enter>", lambda e: _hover(True, e), add="+")
            w.bind("<Leave>", lambda e: _hover(False, e), add="+")
            w.bind("<Button-1>", _click)
        return (outer, t)

    def _build_run_row(self):
        for w in self.run_row.winfo_children():
            w.destroy()
        # the health-mode Re-apply button and the Tweak secondary pills die
        # with the row above — drop the references so _update_run_row can
        # detect their absence and rebuild for the incoming mode
        self._health_reapply_btn = None
        self._rr_check = None
        self._rr_undo = None
        self._rr_session = None
        accent = TAB_ACCENTS[self.tab_name]

        # ONE ACTION BAR, PAGE-WIDE (C-002):
        #
        #     [ flexible hint ][ secondaries ][ PRIMARY ]
        #
        # The primary is packed in its own zero-weight column on the far
        # right, so a secondary appearing or disappearing can never move it -
        # the property the old centred grid existed to protect, now aligned
        # with every other page and every dialog footer. The flexible first
        # column absorbs the space; the secondaries sit immediately left.
        #
        # The grid is created here rather than in _build() because the row is
        # rebuilt on every mode switch (health and undo tear it down).
        self.run_row.grid_columnconfigure(0, weight=1)
        self.run_row.grid_columnconfigure(1, weight=0)
        self.run_row.grid_columnconfigure(2, weight=0)

        def _hero():
            """The far-right cell that owns the primary control(s)."""
            f = tk.Frame(self.run_row, bg=COLORS["bg"])
            f.grid(row=0, column=2, sticky="e")
            return f

        def _side():
            """The cell immediately left of the primary."""
            f = tk.Frame(self.run_row, bg=COLORS["bg"])
            f.grid(row=0, column=1, sticky="e", padx=(0, SPACE_2))
            return f

        def _hint():
            """The flexible left cell: run hints and the reboot badge."""
            f = tk.Frame(self.run_row, bg=COLORS["bg"])
            f.grid(row=0, column=0, sticky="w")
            return f

        if self.mode == "undo":
            self.run_btn = AnimatedButton(
                _hero(), text="Undo Selected", command=self._run_selected,
                role="filled", size="lg", accent=COLORS["accent_red"],
            )
            self.run_btn.pack()
        elif self.mode == "health":
            # two centred health buttons (same 12pt pill language as Run):
            # Check Health re-runs the read-only verifies; Re-apply All
            # replays every applied tweak through the standard run engine
            btns = _hero()
            self.run_btn = AnimatedButton(
                btns, text="Check Health", command=self._check_health_async,
                role="filled", size="lg", accent=accent,
            )
            self.run_btn.pack(side="left", padx=CONTROL_GAP)
            n = len(getattr(self, "_health", []) or [])
            self._health_reapply_btn = AnimatedButton(
                btns, text=f"Re-apply All ({n})" if n else "Re-apply All",
                command=self._reapply_health_all,
                role="secondary", size="lg",
            )
            self._health_reapply_btn.pack(side="left", padx=CONTROL_GAP)
        else:
            label = "Run"
            if self.tab_name == "Clean":
                # improvement-report 2.1: Preview-Before-You-Clean — reuses
                # the exact dry_run + measure_clean_tasks() machinery the
                # Storage Insight dialog already uses, just scoped to
                # whatever's currently selected instead of every category.
                # The button itself only shows once something is actually
                # selected (see _refresh_run_count) — nothing to preview
                # otherwise, so no popup is ever needed to say so.
                # Run is the hero; Preview is a secondary in the side column,
                # so Preview appearing cannot move Run.
                self.run_btn = AnimatedButton(
                    _hero(), text=label, command=self._run_selected,
                    role="filled", size="lg", accent=accent,
                )
                self.run_btn.pack()
                side = _side()
                self._preview_btn = AnimatedButton(
                    side, text="Preview", command=self._preview_clicked,
                    role="outlined", size="lg",
                )
                # not packed here — _refresh_run_count shows/hides it
            elif self.tab_name == "Tweak":
                # Tweak secondaries live BESIDE the hero (never above it), in
                # the side column: Check + conditional Undo + Game Session
                # preset entry. They share Run's button size now; what keeps
                # Run the obvious money action is its fill, not a smaller
                # type size. Because they are in their own column, any of
                # them appearing or disappearing leaves Run pinned.
                self.run_btn = AnimatedButton(
                    _hero(), text=label, command=self._run_selected,
                    role="filled", size="lg", accent=accent,
                )
                self.run_btn.pack()
                self._build_tweak_pills(_side())
            else:
                self.run_btn = AnimatedButton(
                    _hero(), text=label, command=self._run_selected,
                    role="filled", size="lg", accent=accent,
                )
                self.run_btn.pack()
        if self.tab_name == "Tweak" and self.mode in ("preset", "custom"):
            # Reboot badge: created but NOT packed until _refresh_run_count
            # finds reboot-required tasks. An empty packed label + pady was
            # permanently shrinking the Tweak scroll area vs Clean/Repair.
            self._reboot_badge = tk.Label(
                _hint(), text="", font=TYPE["caption"], bg=COLORS["bg"],
                fg=COLORS["accent_yellow"], anchor="w")
            # deliberately not packed here
        self._refresh_run_count()

    def _preview_clicked(self):
        """Estimate how much the currently checked/selected items would
        free, without deleting anything (improvement-report 2.1). Reuses
        the same dry_run ctx + measure_clean_tasks() Storage Insight
        already uses; a key outside SCAN_ALLOWLIST measures 0 because it is
        not estimated at all, so the result banner says so rather than
        implying a false total.
        The button only shows once something is selected (see
        _refresh_run_count), so there's nothing to prompt here — this
        guard is just a no-op safety net, not a user-facing path."""
        selected = self.selected_tasks()
        if not selected:
            return
        try:
            btn = self._preview_btn
            btn.config_text("Estimating…")
            btn.config(state="disabled")
        except Exception:
            pass
        keys = [t.key for t in selected]

        def _worker():
            # BUG-001: these four hops used to be
            # `self.app.root.after(0, ...)` called from THIS worker -- a Tk call
            # on a foreign thread. Python 3.14 raises RuntimeError for it every
            # time, so the Tweak-tab Preview silently never painted. They go
            # through the app's TkDispatcher now; the lambdas bind their
            # arguments because post() takes a zero-arg callable.
            try:
                from app.storage_scan import (
                    acquire_scan, release_scan, make_measure_ctx,
                    measure_clean_tasks, SCAN_ALLOWLIST, sum_measured)
            except Exception:
                self.app._dispatch.post(
                    lambda: self._preview_done(None, None))
                return
            if not acquire_scan(cancelled=lambda: False):
                self.app._dispatch.post(
                    lambda: self._preview_done("busy", None))
                return
            try:
                ctx = make_measure_ctx(
                    set_status=lambda m: self.app._dispatch.post(
                        lambda m=m: self.app.set_status(m)))
                refused = {}
                sizes = measure_clean_tasks(ctx, keys, refused)
                # MED-002: sum only what was actually measured. A category
                # that raised is None, not 0, and one unknown category makes
                # the whole total unknown - otherwise the Preview would
                # quote a confidently-too-small number for something it
                # never looked at. A cleaner that DECLINED to look (Office/
                # Steam live) is named separately: it is a known skip, so the
                # number is still worth quoting with a "+ ?" beside it.
                total = sum_measured(sizes, refused)
                unknown = sum(1 for k, v in sizes.items()
                              if v is None and k not in refused)
                skipped = [k for k in keys if k not in SCAN_ALLOWLIST]
                self.app._dispatch.post(
                    lambda: self._preview_done(total, skipped, unknown,
                                               len(refused)))
            finally:
                release_scan()

        threading.Thread(target=_worker, daemon=True).start()

    def _preview_done(self, total, skipped, unknown=0, refused=0):
        try:
            btn = self._preview_btn
            btn.config_text("Preview")
            btn.config(state="normal")
        except Exception:
            pass
        if total == "busy":
            self.app.set_status("A scan is already running — try Preview again in a moment.")
            return
        if total is None:
            self.app.set_status("Preview isn't available right now.")
            return
        from app.utils import format_bytes
        if total is None or unknown:
            # MED-002: say what is true. A partial measurement is not a
            # confident number, and "Preview isn't available" would be a lie
            # too - we measured some of it.
            self.app.set_status(
                "Preview is incomplete: %d selected item(s) could not be "
                "measured, so no reliable estimate is available yet. See the "
                "Storage Insight panel for detail." % (unknown or 1))
            return
        msg = f"Preview: about {format_bytes(total)} would be freed."
        if refused:
            msg += (f" ({refused} selected item(s) weren't measured — a cleaner "
                    "skipped while the app it cleans was running.)")
        if skipped:
            msg += (f" ({len(skipped)} selected item(s) aren't estimated — they "
                    "act through a service, a package or a command rather than a "
                    "folder walk, or they only report what they find.)")
        self.app.set_status(msg)

    def _selection_gated(self) -> bool:
        """True when the run row's primary button must stay disabled until
        the user has actually chosen something (TEXT-001).

        Health mode is excluded on purpose. Its button is "Check Health" --
        a read-only re-scan of tweaks that are already applied, with nothing
        to select. Gating it would make the mode's only button permanently
        dead, since a health body never has a selection."""
        return getattr(self, "mode", "") in ("preset", "custom", "undo")

    def _has_selection(self) -> bool:
        # Preset mode is special-cased on purpose. The preset summary body
        # is read-only and RETURNS EARLY without ever populating self.vars
        # (see _build_summary_body), so in preset mode self.vars still holds
        # whatever the last Custom/Undo visit left in it. Routing that
        # through selected_tasks() -- whose preset branch needs
        # _selected_preset to be truthy and otherwise falls through to the
        # vars loop -- would read that stale dict and could enable Run with
        # no preset chosen. Ask the preset itself instead; the invariant
        # holds regardless of what self.vars happens to contain.
        try:
            if getattr(self, "mode", "") == "preset":
                return bool(self._selected_preset)
            return bool(self.selected_tasks())
        except Exception:
            return False

    def _refresh_run_enabled(self):
        """Re-apply the button's enabled state after a selection change.

        Combines the two INDEPENDENT gates: Application's run-in-progress
        gate (`_run_gate`) and this tab's own selection gate. Called from
        _refresh_run_count, which every selection change already funnels
        through -- preset click, Custom/Undo/Health entry, any toggle, bulk
        select-all, filter, and the run-row rebuild itself."""
        self.set_run_enabled(self._run_gate)

    def _run_count(self) -> int:
        """Live selection size for the Run button (user request: show what
        you picked). Preset mode = preset length; custom/undo = toggles on."""
        try:
            return len(self.selected_tasks())
        except Exception:
            return 0

    def _refresh_run_count(self):
        """Repaint 'Run (N)' / 'Undo Selected (N)' — plain label when 0.
        Health mode has its own button labels (no selection count), so it
        skips here; its Re-apply count is refreshed at run-row build."""
        # TEXT-001: re-apply the combined enabled state on every selection
        # change. Placed BEFORE the health early-return deliberately. Health
        # mode is never selection-gated (see _selection_gated), so this call
        # is what RE-ENABLES its "Check Health" button after arriving from a
        # gated preset/custom/undo mode. Below this line it would stay stuck
        # disabled, because nothing else re-enables it.
        try:
            self._refresh_run_enabled()
        except Exception:
            pass
        if getattr(self, "mode", "") == "health":
            return
        try:
            btn = getattr(self, "run_btn", None)
            if btn is None or not btn.winfo_exists():
                return
            n = self._run_count()
            if self.mode == "undo":
                btn.config_text(f"Undo Selected ({n})" if n else "Undo Selected")
            else:
                btn.config_text(f"Run ({n})" if n else "Run")
        except Exception:
            pass
        # improvement-report 2.1 (user follow-up): the Preview button only
        # shows once something is actually selected — no more "nothing
        # selected, pick something first" popup, the button just isn't
        # there to click when there's nothing to preview.
        preview_btn = getattr(self, "_preview_btn", None)
        if preview_btn is not None:
            try:
                if preview_btn.winfo_exists():
                    if self._run_count() > 0:
                        if not preview_btn.winfo_ismapped():
                            preview_btn.pack(side="left", padx=CONTROL_GAP)
                    else:
                        if preview_btn.winfo_ismapped():
                            preview_btn.pack_forget()
            except Exception:
                pass
        # improvement-report 2.4: Reboot Planner badge. Keyed off the same
        # task.risk == "REBOOT REQUIRED" tag the end-of-run reminder
        # already uses (_run_tasks_worker) — a separate hand-maintained
        # key list here would just be a second copy to drift out of sync
        # (which is exactly what had happened: "Fix Boot Issues" was
        # missing this tag despite its own dangerous-combo warning saying
        # it needs a reboot — fixed alongside this badge).
        badge = getattr(self, "_reboot_badge", None)
        if badge is not None:
            try:
                if badge.winfo_exists():
                    n_reboot = sum(1 for t in self.selected_tasks()
                                  if getattr(t, "risk", "") == "REBOOT REQUIRED")
                    if n_reboot:
                        plural = "s" if n_reboot != 1 else ""
                        badge.config(text=f"🔁 {n_reboot} tweak{plural} need a reboot — one reboot covers all")
                        if not badge.winfo_ismapped():
                            badge.pack(side="left", pady=(SPACE_1, 0))
                    else:
                        badge.config(text="")
                        if badge.winfo_ismapped():
                            badge.pack_forget()
            except Exception:
                pass

    # ---------------- interactions ---------------- #

    def _select_preset(self, name):
        # F2: mode switches update IN PLACE. The preset cards and the Run
        # button are built once (see _build); switching modes only changes
        # mode state, the body (cache hit = instant show/hide), the card
        # highlight, and the Run button's colors/label — no widget
        # destroy/recreate churn per click.
        self.mode = "preset"
        self._selected_preset = name
        self._build_body()
        self._update_run_row()
        self._highlight_cards()

    def _enter_custom(self):
        self.mode = "custom"
        self._selected_preset = None
        self._build_body()
        self._update_run_row()
        self._highlight_cards()

    def _enter_health(self):
        """Tweak Health (user-approved feature 2, 2026-09): entered from the
        context strip above the run row. Shows every tweak the app believes is applied on this PC
        and checks whether Windows still honors it. Same enter pattern as
        _enter_undo — except the body is ALWAYS rebuilt fresh (rows depend
        on live applied-state; plain labels are cheap, unlike the animated
        toggle grid, so caching buys nothing and only risks stale rows)."""
        self.mode = "health"
        self._selected_preset = None
        old = self._body_cache.pop("health", None)
        try:
            if old is not None and old.winfo_exists():
                old.destroy()
        except Exception:
            pass
        self._body_vars.pop("health", None)
        self._build_body()       # cache miss -> fresh health rows + auto-check
        self._update_run_row()
        self._highlight_cards()

    def _refresh_health_state(self):
        """Rebuild self._health from the current applied-tweak registry:
        [(key, task, state)] for every applied key that still maps to a
        real Tweak task. State starts 'pending' (shown as ✓ Active, the
        registry's word — same as the Undo badges); the async check then
        promotes rows to active / drifted / unknown."""
        state = self.app.tweak_state or {}
        self._health = []
        for key in state:
            t = self.task_by_key.get(key)
            if t is not None and t.revert is not None:
                self._health.append([key, t, "pending"])
        # rows the health body built (repainted in place like F3)
        self._health_rows = getattr(self, "_health_rows", {})

    # ---------------- Tweak Health body (mode="health") ---------------- #

    def _build_health_body(self, wrap):
        """Status view, not a selection view (no checkboxes — the Phase 2
        rule): amber drift banner (hidden unless the check found drifted
        tweaks) + 2-column grid of applied-tweak status rows matching the
        Custom row geometry, all inside a rounded panel.

        Always starts from the LIVE applied-tweak registry (entry,
        undo-body-style cache clears, and post-run rebuilds all funnel
        here) and kicks the async verify at the end, so rows can never
        describe a machine state from before the last run."""
        accent = TAB_ACCENTS[self.tab_name]
        self._refresh_health_state()
        self._health_results = {}    # the auto-check below repopulates
        self._health_rows = {}
        # health has no toggle vars — explicitly clear, and give the body
        # cache empty cell lists so a later cache-hit restore never
        # resurrects another mode's grid handles (F8-class staleness)
        self.vars = {}
        self._cells = []
        self._cell_blocks = []

        # drift banner (pack_forget keeps it hidden until drift is found)
        banner = tk.Frame(wrap, bg=COLORS["alert_amber_bg"])
        tk.Label(banner, text="", font=TYPE["label_large"], bg=COLORS["alert_amber_bg"],
                 fg=COLORS["accent_yellow"]).pack(side="left", anchor="w",
                                                  padx=GROUP_GAP, pady=SPACE_2)
        self._health_fix_btn = AnimatedButton(
            banner, text="Fix Them", command=self._fix_health_drift,
            role="filled", size="sm", accent=accent)
        self._health_fix_btn.pack(side="right", padx=GROUP_GAP, pady=CONTROL_GAP)
        self._health_banner = banner
        self._health_banner_lbl = banner.winfo_children()[0]

        head = tk.Frame(wrap, bg=COLORS["bg"])
        head.pack(fill="x", pady=(CONTROL_GAP, SPACE_1))
        tk.Label(head, text=f"Your active tweaks — {len(self._health)} applied",
                 font=TYPE["card_title"], bg=COLORS["bg"],
                 fg=accent).pack(side="left")
        self._health_checked_lbl = tk.Label(head, text="",
                                            font=TYPE["caption"], bg=COLORS["bg"],
                                            fg=COLORS["subtext"])
        self._health_checked_lbl.pack(side="left", padx=GROUP_GAP)

        panel = ScrollableRoundedPanel(wrap)
        panel.pack(fill="both", expand=True)
        if not self._health:
            tk.Label(panel.inner,
                     text="No tweaks applied yet — run a preset or pick some in Custom first.",
                     font=TYPE["body_medium"], bg=COLORS["bg_alt"],
                     fg=COLORS["subtext"]).pack(expand=True, pady=SPACE_5)
        else:
            rows = []
            for key, t, _st in self._health:
                cell = self._build_health_row(panel.inner, t)
                rows.append((cell, t))
            # same 2-across flow as the Custom grid (Install-tab density)
            for i, (cell, _t) in enumerate(rows):
                rr, cc = divmod(i, 2)
                cell.grid(row=rr, column=cc, sticky="new", padx=CONTROL_GAP, pady=TEXT_NUDGE)
            for c in (0, 1):
                panel.inner.grid_columnconfigure(c, weight=1, uniform="healthrows")
        self._cells = []
        self._cell_blocks = []
        panel.refresh_scroll()
        self._paint_health_rows()
        # auto-check: ~25 fast registry reads in a worker (the one powercfg
        # query aside, every verify is sub-ms). The Check Health button
        # re-runs this same check on demand.
        self._check_health_async()

    def _build_health_row(self, parent, t):
        """One status row: status glyph + label + live-state text on the
        right. Same bg_alt cell geometry as _build_toggle_row (minus the
        switch — this is a display, not a control), so the Health view
        lines up with the Custom/Undo grids pixel-for-pixel."""
        outer = tk.Frame(parent, bg=COLORS["bg_alt"])
        inner = tk.Frame(outer, bg=COLORS["bg_alt"])
        inner.pack(fill="both", expand=True)
        dot = tk.Label(inner, text="·", font=TYPE["bullet"],
                       bg=COLORS["bg_alt"], fg=COLORS["subtext"])
        dot.pack(side="left", anchor="center", padx=(SPACE_2, TEXT_NUDGE), pady=CONTROL_GAP)
        lbl = tk.Label(inner, text=t.label, font=TYPE["label_medium"],
                       bg=COLORS["bg_alt"], fg=COLORS["text"],
                       anchor="w", justify="left",
                       wraplength=215)
        lbl.pack(side="left", anchor="center")
        Tooltip(lbl, t.description)
        state = tk.Label(inner, text="applied", font=TYPE["label_small"],
                         bg=COLORS["bg_alt"], fg=COLORS["subtext"],
                         anchor="e")
        state.pack(side="right", anchor="center", padx=(CONTROL_GAP, SPACE_2))
        self._health_rows[t.key] = (outer, dot, state)
        outer._lbl = lbl
        return outer

    def _paint_health_rows(self):
        """Repaint every health row from self._health in place (F3
        pattern): states are pending/active/drifted/unknown. Drifted
        rows get the amber ROW TINT (selection language, same as the
        DNS picker) — 'fix these' targets are obvious at a glance."""
        results = getattr(self, "_health_results", {}) or {}
        amber = _hex_lerp(COLORS["bg_alt"], COLORS["accent_yellow"], 0.22)
        for key, t, _st in getattr(self, "_health", []) or []:
            handles = getattr(self, "_health_rows", {}).get(key)
            if handles is None:
                continue
            _outer, dot, state = handles
            try:
                if not dot.winfo_exists():
                    continue
            except Exception:
                continue
            outcome = results.get(key)
            if outcome is False:
                try:
                    self._tint_health_row(handles, amber)
                    dot.config(text="⚠", fg=COLORS["accent_yellow"])
                    state.config(text="Reset by Windows",
                                 fg=COLORS["accent_yellow"])
                except Exception:
                    pass
            elif outcome is True:
                try:
                    self._tint_health_row(handles, COLORS["bg_alt"])
                    dot.config(text="✓", fg=COLORS["accent_green"])
                    state.config(text="Active",
                                 fg=COLORS["accent_green"])
                except Exception:
                    pass
            else:
                try:
                    self._tint_health_row(handles, COLORS["bg_alt"])
                    dot.config(text="✓" if _st != "unknown" else "·",
                               fg=(COLORS["accent_green"] if _st != "unknown"
                                   else COLORS["subtext"]))
                    state.config(text="Active" if _st != "unknown" else "Applied",
                                 fg=(COLORS["accent_green"] if _st != "unknown"
                                     else COLORS["subtext"]))
                except Exception:
                    pass
        self._paint_health_banner()

    @staticmethod
    def _tint_health_row(handles, bg):
        """Repaint a health row (outer + inner + every child) in a new
        background — the tint IS the state language now."""
        outer = handles[0]
        if outer is None or not outer.winfo_exists():
            return
        outer.config(bg=bg)
        for child in outer.winfo_children():
            try:
                child.config(bg=bg)
                for sub in child.winfo_children():
                    sub.config(bg=bg)
            except Exception:
                pass

    def _paint_health_banner(self):
        """Pack the amber drift banner only when verified-drifted rows
        exist (it stays hidden otherwise — no nagging)."""
        banner = getattr(self, "_health_banner", None)
        if banner is None:
            return
        try:
            if not banner.winfo_exists():
                return
            results = getattr(self, "_health_results", {}) or {}
            health = getattr(self, "_health", []) or []
            drifted = [k for k, _t, _s in health if results.get(k) is False]
            if drifted:
                self._health_banner_lbl.config(
                    text=f"⚠  Windows undid {len(drifted)} of your tweak{'s' if len(drifted) != 1 else ''}")
                self._health_fix_btn.config_text(
                    f"Fix Them ({len(drifted)})" if drifted else "Fix Them")
                if banner.winfo_manager() == "":
                    banner.pack(fill="x", pady=(0, CONTROL_GAP))
            else:
                if banner.winfo_manager() != "":
                    banner.pack_forget()
        except Exception:
            pass

    # ---------------- Tweak Health check + re-apply ---------------- #

    def _check_health_async(self):
        """Run every applied tweak's verify() in a worker thread (verify
        fns are read-only, but the one powercfg query takes ~100ms and
        Tk updates must never come from a worker — the worker writes a
        plain holder dict and a Tk-thread poller publishes, the same shape
        as _CatalogPickerDialog._check_sweep).

        C-4 audit fix: the old worker called self.winfo_exists() +
        self.after() directly — Tk calls from a foreign thread, which burn
        ~1s and can raise outside event dispatch (measured rule behind the
        picker's async design). The worker now touches no Tk at all."""
        health = list(getattr(self, "_health", []) or [])
        if not health:
            try:
                self._health_checked_lbl.config(text="")
            except Exception:
                pass
            self._paint_health_banner()
            return
        try:
            self._health_checked_lbl.config(text="Checking…")
        except Exception:
            pass
        # Generation-guarded holder: a fresh check replaces it, so a late
        # landing from a superseded sweep is dropped, never painted.
        holder = {"done": False, "res": {}}
        self._health_holder = holder

        def _worker():
            res = {}
            for key, t, _st in health:
                fn = getattr(t, "verify", None)
                if fn is None:
                    continue            # no verify: registry's word stands
                try:
                    v = fn()
                except Exception:
                    v = None
                res[key] = v
            # pure-Python publish — never a Tk call (see docstring)
            try:
                holder["res"] = res
            except Exception:
                pass
            try:
                holder["done"] = True
            except Exception:
                pass

        try:
            import threading as _th
            _th.Thread(target=_worker, daemon=True).start()
        except Exception:
            # no sweep in flight — publish the empty result now (already on
            # the Tk thread) instead of arming a poller that could never land
            self._finish_health_check({})
            return
        try:
            self.after(150, lambda: self._check_health(holder))
        except Exception:
            pass

    def _check_health(self, holder):
        """Tk-thread poller for one health sweep. Drops superseded results
        (a newer check replaced the holder), re-arms while the sweep flies,
        publishes exactly once via _finish_health_check."""
        try:
            if getattr(self, "_health_holder", None) is not holder:
                return              # superseded — drop silently
        except Exception:
            return
        try:
            if not self.winfo_exists():
                return
        except Exception:
            return
        try:
            done = holder.get("done")
        except Exception:
            return
        if not done:
            try:
                self.after(150, lambda: self._check_health(holder))
            except Exception:
                pass
            return
        try:
            res = holder.get("res") or {}
        except Exception:
            res = {}
        self._finish_health_check(res)

    def _finish_health_check(self, res):
        try:
            self._health_results = dict(res or {})
        except Exception:
            self._health_results = {}
        verified = sum(1 for v in self._health_results.values() if v is True)
        drifted = sum(1 for v in self._health_results.values() if v is False)
        try:
            self._health_checked_lbl.config(
                text=(f"Checked just now — {verified} active"
                      + (f", {drifted} reset by Windows" if drifted else "")))
        except Exception:
            pass
        self._paint_health_rows()
        # No status-line sentence here: the amber banner already says how many
        # tweaks Windows undid and carries the Fix Them button that undoes it,
        # and the caption above already says when it was checked. A third
        # statement in the footer would be the same news a second time, in a
        # line that has scrolled off with the tab by the time it is read.

    def _fix_health_drift(self):
        """Re-apply only the verified-drifted tweaks through the standard
        run engine (runs land on the scorecard like any other run)."""
        results = getattr(self, "_health_results", {}) or {}
        health = getattr(self, "_health", []) or []
        by_key = {t.key: t for _k, t, _s in health}
        drifted = [by_key[k] for k in results if results.get(k) is False and k in by_key]
        if not drifted:
            try:
                _themed_showinfo(self.app.root, "Nothing to Fix",
                                 "Every checkable tweak is still active.",
                                 accent=TAB_ACCENTS[self.tab_name])
            except Exception:
                pass
            return
        self.app.run_tasks(self.tab_name, drifted, mode="run")

    def _reapply_health_all(self):
        """Re-apply every applied tweak (the Phase-1 'Reinforce' button —
        one click restores anything a Windows Update quietly reset)."""
        health = getattr(self, "_health", []) or []
        tasks = [t for _k, t, _s in health]
        if not tasks:
            return
        self.app.run_tasks(self.tab_name, tasks, mode="run")

    def _enter_undo(self):
        self.mode = "undo"
        self._selected_preset = None
        # badges must reflect the machine's CURRENT applied state, not the
        # state from when the app was opened — repaint them in place on the
        # cached undo grid (F3); a missing cached body rebuilds fresh below
        self._refresh_undo_badges()
        self._build_body()
        self._update_run_row()
        self._highlight_cards()

    def _update_run_row(self):
        """F2: restyle the persistent Run button for the current mode
        instead of destroying/recreating it per switch (each rebuild also
        paid a temp-label font measure). The command never changes —
        _run_selected dispatches on self.mode — so only the colors and the
        base label differ per mode; _refresh_run_count adds the live count.
        Two modes always rebuild: health (its two-button row carries a live
        applied-count label) and undo (its dedicated row must shed the
        Tweak secondary pills, which belong to preset/custom only). Mode
        switches are rare; the body builds dominate cost."""
        if self.mode in ("health", "undo"):
            try:
                self._build_run_row()
            except Exception:
                pass
            try:
                self._refresh_tweak_pills()
            except Exception:
                pass
            return
        # linger fix: leaving health mode restyles the persistent Run
        # button in place below — but the health-only Re-apply button was
        # packed beside it and is NOT part of any other mode's row. If its
        # ghost survived (built pre-fix, or any path that skipped the
        # rebuild), rebuild the row cleanly instead of restyling around it.
        try:
            _ghost = getattr(self, "_health_reapply_btn", None)
            if _ghost is not None and _ghost.winfo_exists():
                self._health_reapply_btn = None
                self._build_run_row()
                try:
                    self._refresh_tweak_pills()
                except Exception:
                    pass
                return
        except Exception:
            pass
        # Tweak pills ride with preset/custom rows only: arriving from
        # undo/health rebuilt the row without them — rebuild again with
        # them rather than restyling a pill-less row.
        if self.tab_name == "Tweak" and self.mode in ("preset", "custom"):
            try:
                _pills_gone = True
                for _pn in ("_rr_check", "_rr_undo", "_rr_session"):
                    _pw = getattr(self, _pn, None)
                    if _pw is not None and _pw.winfo_exists():
                        _pills_gone = False
                        break
                if _pills_gone:
                    self._build_run_row()
                    try:
                        self._refresh_tweak_pills()
                    except Exception:
                        pass
                    return
            except Exception:
                pass
        btn = getattr(self, "run_btn", None)
        try:
            if btn is None or not btn.winfo_exists():
                self._build_run_row()
                return
        except Exception:
            self._build_run_row()
            return
        accent = TAB_ACCENTS[self.tab_name]
        if self.mode == "undo":
            btn.set_style(bg=COLORS["accent_red"], fg=COLORS["black"],
                          hover_bg=_hex_lerp(COLORS["accent_red"], "#FFFFFF", 0.08))
        else:
            btn.set_style(bg=accent, fg=COLORS["black"],
                          hover_bg=_hex_lerp(accent, "#FFFFFF", 0.08))
        self._refresh_run_count()
        # context strip follows every mode switch (it hides in undo/health)
        try:
            self._refresh_tweak_pills()
        except Exception:
            pass

    def _refresh_undo_badges(self):
        """F3: repaint the Undo grid's '✓ Active' badges IN PLACE from the
        machine's CURRENT applied-tweak state. Every undo row owns a badge
        label (created at grid build, packed only while the tweak is
        applied); entering Undo just packs/unpacks those handles — a couple
        of ms vs. rebuilding the ~60-row grid (~300+ ms) every entry. Rows
        are the ones captured when the undo grid was last built; after a
        run the whole body cache is cleared (see _clear_body_cache call in
        the run-completion path), so the next entry rebuilds once fresh."""
        state = self.app.tweak_state or {}
        for outer, t in getattr(self, "_undo_rows", ()) or ():
            try:
                if not outer.winfo_exists():
                    continue
                badge = getattr(outer, "_undo_badge", None)
                if badge is None or not badge.winfo_exists():
                    continue
                if t.key in state:
                    if badge.winfo_manager() == "":
                        badge.pack(side="left", anchor="center", padx=(CONTROL_GAP, 0))
                else:
                    if badge.winfo_manager() != "":
                        badge.pack_forget()
            except Exception:
                continue

    def _highlight_cards(self):
        """Mark the active preset card — strong, obvious selected state.

        Selected: noticeably lighter face + full tab-accent outline so the
        active card is unmistakable at a glance (Apple/Google chip style).
        Unselected: quiet surface + hairline. Hover is handled separately.
        """
        # Undo/health are modes, not cards — nothing highlights while in
        # them (highlighting an unrelated preset would lie about state).
        active = {"preset": self._selected_preset, "custom": "Custom"}.get(self.mode)
        accent = TAB_ACCENTS[self.tab_name]
        for name, card in getattr(self, "preset_cards", {}).items():
            if not isinstance(card, RoundedCard):
                continue
            sel = (name == active)
            try:
                # Cancel any in-flight hover so selected paint wins.
                if getattr(card, "_hov_after", None) is not None:
                    try:
                        card.after_cancel(card._hov_after)
                    except Exception:
                        pass
                    card._hov_after = None
                card._hov_t = 0.0
                card._hov_target = 0.0
                # Stronger selected fill than plain surface_hover so the
                # active card reads clearly against the pure-black canvas.
                from app.ui.theme import _hex_lerp as _hl
                # M3 selected card: tonal container tinted with the tab accent
                card.set_fill(_hl(SURFACE_CARD, accent, 0.24) if sel
                              else SURFACE_CARD)
                card._outline = accent if sel else ""
                try:
                    card._outline_w = 2 if sel else 1
                except Exception:
                    pass
                card._drawn = None
                card._paint()
                for w in card._descendants():
                    try:
                        w.config(bg=card.fill())
                    except Exception:
                        pass
            except Exception:
                pass


    def set_run_enabled(self, enabled: bool):
        # TEXT-001: remember the run-in-progress gate so a later selection
        # change can re-apply it, then AND it with this tab's own selection
        # gate.
        #
        # Before this, Run / Undo Selected rendered at full accent
        # saturation with nothing selected, so the first press was a
        # guaranteed dead click that answered with a "Nothing Selected"
        # dialog. AnimatedButton already had a correct disabled treatment
        # (_draw lerps both the fill and the label 45% toward the page
        # background, and suppresses the focus ring), so this only had to
        # start USING it -- no new visual state was invented.
        self._run_gate = bool(enabled)
        if self._selection_gated() and not self._has_selection():
            enabled = False
        if getattr(self, "run_btn", None):
            try:
                self.run_btn.set_enabled(enabled)
            except Exception:
                pass
        # health mode's second button (Re-apply All / Fix flows) must be
        # gated too, or a run could be launched twice
        hb = getattr(self, "_health_reapply_btn", None)
        if hb is not None:
            try:
                if hb.winfo_exists():
                    hb.set_enabled(enabled)
            except Exception:
                pass
        fb = getattr(self, "_health_fix_btn", None)
        if fb is not None:
            try:
                if fb.winfo_exists():
                    fb.set_enabled(enabled)
            except Exception:
                pass

    def selected_tasks(self):
        if self.mode == "preset" and self._selected_preset:
            keys = self.presets[self._selected_preset]
            chosen = [self.task_by_key[k] for k in keys]
        else:
            chosen = [t for t in self.tasks
                      if self.vars.get(t.key) and self.vars[t.key].get()]
        # N-004: never run a task that does not apply to this Windows build,
        # whatever a preset or a hand-edited config selects.
        return [t for t in chosen if task_supported(t)]

    def undo_selected_tasks(self):
        return [t for t in self.tasks if self.vars.get(t.key) and self.vars[t.key].get() and t.revert]

    def _run_selected(self):
        if self.mode == "undo":
            selected = self.undo_selected_tasks()
            if not selected:
                _themed_showinfo(self, "Nothing to Undo",
                                 "Turn on at least one tweak to undo first.",
                                 accent=COLORS["accent_yellow"])
                return
            n_undo = len(selected)
            suffix = "" if n_undo == 1 else "s"
            if not _themed_askyesno(
                    self, f"Undo {n_undo} tweak{suffix}?",
                    "This restores the exact values recorded when each tweak "
                    "ran.",
                    accent=COLORS["accent_red"],
                    yes_text="Undo Them", no_text="Go Back"):
                return
            self.app.run_tasks(self.tab_name, selected, mode="revert")
        else:
            selected = self.selected_tasks()
            if not selected:
                _themed_showinfo(self, "Nothing Selected",
                                 "Pick a preset or turn some switches on first.",
                                 accent=COLORS["accent_yellow"])
                return
            self.app.run_tasks(self.tab_name, selected, mode="run")

    def _open_dns_tester(self):
        """Find My Fastest DNS entry (the ⚡ Test button on the gaming_dns
        row). Modal dialog; applying runs through the standard engine."""
        try:
            self.app._open_dns_tester()
        except Exception:
            pass
