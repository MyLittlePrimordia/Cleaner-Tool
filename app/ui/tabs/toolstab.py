"""ToolsTab — extracted verbatim from app/gui.py (H13).

A feature tab page. May use the widget set and the modal base, and
opens dialogs, but never imports another tab.
"""
from __future__ import annotations


import tkinter as tk
from tkinter import ttk, messagebox
from app.ui.base import Tooltip
from app.ui.theme import COLORS, F, TAB_ACCENTS, ellipsize, _hex_lerp
from app.ui.widgets import RoundedCard, ScrollableRoundedPanel
from app.utils import resolve_asset_path


class ToolsTab(tk.Frame):
    """Tools tab (user redesign 2026-09): the 5th tab — pink accent.

    Feature cards in a 3-wide grid — the exact same card size,
    gaps and grid as the Tweak tab's 6 preset cards (PER_ROW=3,
    same pack/grid/pad), so the tab itself is cards only, like
    Tweak's top."""

    # Logical groups (left→right, top→bottom):
    #   1) Storage / disk
    #   2) System performance & control
    #   3) PC info + network
    #   4) Gaming automation
    #   5) Peripheral testers (input → display → audio/video)
    _CARDS = (
        # Storage / disk
        ("storage", "💾", "Storage",
         "Free up disk space", TAB_ACCENTS["Clean"]),
        ("drivetoolkit", "💽", "Drive Toolkit",
         "Health, speed & real capacity checks", TAB_ACCENTS["Clean"]),
        ("uninstall", "🗑️", "Uninstall Programs",
         "Remove apps + their leftovers", TAB_ACCENTS["Clean"]),
        # System performance & control
        ("procman", "📋", "Process Manager",
         "See what's using CPU & RAM — close it", TAB_ACCENTS["Tweak"]),
        ("hwmonitor", "📊", "Hardware Monitor",
         "Live CPU, RAM, GPU, disk + network", TAB_ACCENTS["Tweak"]),
        ("startup", "🚀", "Startup Manager",
         "See what launches at sign-in", TAB_ACCENTS["Tweak"]),
        # PC info + network
        ("specs", "💻", "PC Specs",
         "Your PC at a glance", TAB_ACCENTS["Clean"]),
        ("dns", "⚡", "DNS Test",
         "Find your fastest DNS", TAB_ACCENTS["Tweak"]),
        ("speed", "📡", "Speed Test",
         "Check download, upload + ping", TAB_ACCENTS["Clean"]),
        ("gameping", "📶", "Server Ping",
         "Ping game servers before you queue", TAB_ACCENTS["Clean"]),
        # Gaming automation
        ("pilot", "🎮", "Auto-Pilot",
         "Auto-tweaks for games", TAB_ACCENTS["Tweak"]),
        # Peripheral testers
        ("gamepad", "🕹️", "Gamepad Tester",
         "Test buttons, sticks + triggers", TAB_ACCENTS["Clean"]),
        ("keyboard", "⌨️", "Keyboard Tester",
         "See which keys register", TAB_ACCENTS["Clean"]),
        ("mouse", "🖱️", "Mouse Tester",
         "Buttons, event rate + aim test", TAB_ACCENTS["Clean"]),
        ("monitor", "🖥️", "Monitor Test",
         "Dead pixels + display info", TAB_ACCENTS["Clean"]),
        ("webcam", "📷", "Webcam Test",
         "Check your camera works", TAB_ACCENTS["Clean"]),
        ("miccheck", "🎙️", "Mic Check",
         "Mics, permission + live level", TAB_ACCENTS["Tweak"]),
        ("speaker", "🔊", "Speaker Test",
         "Left / right / balance check", TAB_ACCENTS["Clean"]),
    )

    #: Cards added by Phases 2 and 7-11. Same 5-tuple shape as _CARDS, and
#: rendered into the SAME 3-wide grid (see _build) with no separate section
#: band, so all cards look identical. Kept as its own tuple so each phase's
#: verifier can still assert what that phase contributed.
    #: so _build_card serves both. Deliberately a SEPARATE table from
    #: _CARDS: tools/smoke/tools.py derives its grid-position assertions from
    #: _CARDS, so appending here leaves the original 18 exactly where they
    #: are and the existing checks keep asserting the same thing.
    _FIX_CARDS = (
        ("health", "\U0001FA7A", "PC Health Report",
         "One plain-English checkup of your whole PC",
         TAB_ACCENTS["Tools"]),
        ("network_connections", "\U0001F310", "Network Connections",
         "See which programs are online, and to where",
         TAB_ACCENTS["Tools"]),
        ("services", "\u2699\ufe0f", "Services",
         "See what Windows is running in the background",
         TAB_ACCENTS["Tools"]),
        ("file_lock", "\U0001F512", "File Lock Finder",
         "Find out what is using a file, and close it",
         TAB_ACCENTS["Tools"]),
        ("duplicates", "\U0001F5C2\ufe0f", "Duplicate & Large Files",
         "Find copies you can remove, and free up space",
         TAB_ACCENTS["Tools"]),
        ("restore_points", "\U0001F570\ufe0f", "Restore Point Manager",
         "Your safety net: see it, and add to it",
         TAB_ACCENTS["Tools"]),
        # U-002: one place to see and use every safety net.
        ("safety_center", "\U0001F6E1\ufe0f", "Safety & Undo",
         "Every safety net in one place \u2014 restore points, undo, backups",
         TAB_ACCENTS["Tools"]),
        # 2026-10 audit: the restore half of the Clean tab's save backup.
        ("game_saves", "\U0001F4BE", "Game Saves",
         "Back up and restore your save files, safely",
         TAB_ACCENTS["Tools"]),
        ("game_readiness", "\U0001F3AE", "Game Readiness",
         "Seven checks between your PC and a smooth game",
         TAB_ACCENTS["Tools"]),
    )

    def __init__(self, parent, app):
        super().__init__(parent, bg=COLORS["bg"])
        self.app = app
        self._card_frames = {}

        self._build()

    # ---------------- structure ---------------- #

    def _build(self):
        # Cards in a 3-wide grid — byte-identical geometry to the task
        # tabs' 3 preset cards (TaskTab._build_preset_cards): same parent
        # pack (fill=x, padx=26, pady=(14, 4)), same PER_ROW=3 divmod,
        # same card grid pads (padx=5, pady=4) and uniform columns, so
        # every card lands at the exact same size + position. The old
        # top helper label is gone (Tweak has none above its cards) and
        # nothing above the cards: the shortcuts box that used to live here
        # is gone, so the tab itself is cards only, like Tweak's top.
        # (Why no topbar: keeping it would push every card down vs
        # Tweak; exact position match requires the same top offset.)
        PER_ROW = 3
        # DESIGN AUDIT (2026-10) — the grid fits with no scrolling.
        #
        # It used to pack straight into the page with no scroll container, on
        # the reasoning that N cards in rows "fits". Measured at the 980x700
        # window floor that was false, so the grid was wrapped in the shared
        # ScrollableRoundedPanel: clipped cards with no way to reach them is
        # the worst failure mode a grid can have.
        #
        # USER CALL (2026-10): rather than scroll, drop the two weakest cards
        # (Quick Tools, Run History). Measured in the real main window at the
        # 1280x900 default: 29 cards (10 rows) needed 680px in a 661px panel
        # and DID draw the thumb; 27 cards (9 rows) need 612px, so the grid
        # now fits with ~49px to spare and the thumb stays hidden. One row of
        # three cards costs ~68px, so the next full row would bring the
        # scrollbar back - re-measure before adding cards, and remember the
        # 980x700 window floor still scrolls (461px panel) by design.
        #
        # The panel is still the shared ScrollableRoundedPanel (the same
        # widget every Tools popup uses, so the family stays one family). It
        # costs nothing now that the content fits: its thumb only draws when
        # the content overflows, which is also the safety valve for a raised
        # tk font scaling (the frozen build runs DPI-aware at scaling 2.0 while
        # a source run is DPI-unaware at ~1.33 - the split app/config.py warns
        # about).
        # OLED redesign: no outer grey tray. Panel and inner canvas are pure
        # black so cards float on the true-black canvas.
        panel = ScrollableRoundedPanel(self)
        panel._fill = COLORS["bg"]
        panel.inner.configure(bg=COLORS["bg"])
        try:
            panel._redraw_bg()
        except Exception:
            pass
        panel.pack(fill="both", expand=True, padx=26, pady=(14, 4))
        card_area = tk.Frame(panel.inner, bg=COLORS["bg"], bd=0)
        card_area.pack(fill="x")
        self._panel_ref = panel
        self.panel = panel
        # All cards live in ONE grid now (user request): the six added in
        # Phases 2 and 7-11 sit in the same 3-wide grid as the original 18,
        # with no "Fix & inspect" band above them. They are built from
        # `_CARDS + _FIX_CARDS` rather than being folded into `_CARDS`,
        # because the per-phase verifiers assert against `_FIX_CARDS` as the
        # record of what each phase added, and that record is worth keeping.
        # The two tuples are concatenated HERE, once, so there is still a
        # single source of truth for what the grid contains.
        all_cards = tuple(self._CARDS) + tuple(self._FIX_CARDS)
        # A card count that is not a multiple of 3 would leave the final
        # card jammed against the left edge with two empty cells beside
        # it, which reads as a layout bug rather than a design. When
        # exactly one card is left over it goes in the MIDDLE column, so
        # the last row looks deliberate. Two left over already balance.
        n_cards = len(all_cards)
        lone_last = (n_cards % PER_ROW) == 1
        last_row = (n_cards - 1) // PER_ROW
        for i, (key, icon, title, blurb, card_accent) in enumerate(all_cards):
            row, col = divmod(i, PER_ROW)
            if lone_last and row == last_row:
                col = 1
            self._build_card(card_area, row, col, key, icon, title, blurb,
                             card_accent)
        for c in range(PER_ROW):
            card_area.grid_columnconfigure(c, weight=1, uniform="cards")
        # Kept for the smoke harness; now points at the card grid.
        self._card_area_ref = card_area
        # One grid now, so the old second-area handle points at the same
        # frame. Anything still reading it gets the grid rather than None.
        self._fix_card_area_ref = card_area

    def _build_card(self, parent, rr, cc, key, icon, title, blurb,
                      card_accent):
        """One feature card: full-color PNG icon (Twemoji) when present under
        assets/tools_icons/{key}.png, else accent-colored emoji glyph. Bold
        title; description lives in a hover tooltip. Grid pads match Tweak."""
        # OLED redesign: cards use surface so they float cleanly on pure black.
        face = COLORS["surface"]
        outer = RoundedCard(parent, fill=face, bg=COLORS["bg"], radius=16)
        outer.grid(row=rr, column=cc, sticky="nsew", padx=5, pady=4)
        inner = outer.body
        head = tk.Frame(inner, bg=face)
        head.pack(fill="x", padx=10, pady=4)

        photo = None
        try:
            # Asset resolution goes through utils.resolve_asset_path, which
            # anchors on app/ (and the frozen _MEIPASS layouts) so it is correct
            # at any module depth. The obvious-looking
            # `Path(__file__).parent / "assets"` is NOT: it is only right while
            # the caller lives directly in app/, and this tab lives in app/ui/tabs.
            _p = resolve_asset_path(f"tools_icons/{key}.png")
            if _p:
                photo = tk.PhotoImage(file=_p)
                try:
                    w, h = photo.width(), photo.height()
                    if w > 28 or h > 28:
                        factor = max(1, max(w, h) // 24)
                        if factor > 1:
                            photo = photo.subsample(factor, factor)
                except Exception:
                    pass
                if not hasattr(self, "_card_photos"):
                    self._card_photos = {}
                self._card_photos[key] = photo
        except Exception:
            photo = None

        # M3 tonal icon container: a 40px circle tinted with the tab accent
        icon_lbl = tk.Canvas(head, width=40, height=40, bg=face,
                             highlightthickness=0, bd=0)
        icon_lbl.create_oval(0, 0, 39, 39,
                             fill=_hex_lerp(COLORS["bg"], card_accent, 0.22),
                             outline="")
        if photo is not None:
            icon_lbl.create_image(20, 20, image=photo)
        else:
            icon_lbl.create_text(20, 20, text=icon,
                                 font=("Segoe UI Emoji", 14), fill=card_accent)
        icon_lbl.pack(side="left")
        title_lbl = tk.Label(head, text=title, font=(F, 11, "bold"),
                             bg=face, fg=COLORS["text"],
                             anchor="w", bd=0)
        title_lbl.pack(side="left", padx=12, fill="x", expand=True)
        self._card_frames[key] = (outer, inner)

        # REDESIGN 2026-10: long titles truncate with an ellipsis instead of
        # being clipped. Three of the cards ("Duplicate & Large Files",
        # "Restore Point Manager", "Network Connections") are wider than a
        # third of the window at 12pt, and pack's answer was to shrink the
        # Label below its requested width - which, with the default CENTER
        # anchor, shears glyphs off BOTH ends. anchor="w" above stops the
        # left-hand loss; this stops the right-hand one. The full name is
        # already on the card's tooltip, so nothing is lost.
        _title_full = title

        def _fit_title(_lbl=title_lbl, _full=_title_full, _head=head):
            try:
                room = _head.winfo_width() - 14 - 8 - 26 - 8
                _lbl.config(text=ellipsize(_full, _lbl.cget("font"), room))
            except Exception:
                pass

        head.bind("<Configure>", lambda _e: _fit_title(), add="+")
        head.after_idle(_fit_title)

        if blurb:
            for _w in (outer, inner, head, icon_lbl, title_lbl):
                Tooltip(_w, blurb)

        # Animated hover: ease the face surface → surface_hover and outline to
        # the Tools accent over ~140 ms (same language as the tab switcher).
        # NOTE: `_hex_lerp` is already imported at module top. Do NOT
        # re-import it here — a function-local `from ... import _hex_lerp`
        # would (Python scoping) make every earlier `_hex_lerp` use in this
        # same function read as an unbound local and crash the tab build.
        outer._hov_t = 0.0
        outer._hov_target = 0.0
        outer._hov_after = None
        rest_fill = COLORS["surface"]
        hover_fill = COLORS["surface_hover"]
        rest_outline = ""     # M3 filled card: hover is a state layer only
        hover_outline = ""

        def _apply_hov(t):
            try:
                fill = _hex_lerp(rest_fill, hover_fill, t)
                # Outline snaps on once hover is past a threshold so it
                # doesn't fight the fill animation with a muddy mid-color.
                outline = hover_outline if t > 0.45 else rest_outline
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

        def _tick_hov():
            target = outer._hov_target
            cur = outer._hov_t
            if abs(cur - target) < 0.03:
                outer._hov_t = target
                outer._hov_after = None
                _apply_hov(target)
                return
            outer._hov_t = cur + (target - cur) * 0.32
            _apply_hov(outer._hov_t)
            outer._hov_after = outer.after(16, _tick_hov)

        def _start_hov(on):
            outer._hov_target = 1.0 if on else 0.0
            if outer._hov_after is None:
                _tick_hov()

        def hover_on(_e):
            _start_hov(True)

        def hover_off(_e):
            _start_hov(False)

        for w in (outer, inner, head):
            w.bind("<Button-1>", lambda e, k=key: self._mount(k))
            w.bind("<Enter>", hover_on, add="+")
            w.bind("<Leave>", hover_off, add="+")
        for child in inner.winfo_children():
            for c in child.winfo_children() or [child]:
                try:
                    c.bind("<Button-1>", lambda e, k=key: self._mount(k))
                    c.bind("<Enter>", hover_on, add="+")
                    c.bind("<Leave>", hover_off, add="+")
                except Exception:
                    pass

    # ---------------- feature launch ---------------- #

    def _mount(self, key):
        """A card click opens the feature's X-button popup (user call:
        the in-tab panel has no room at 1040x800 — the 800x600 modals are
        the right surface). This stays the single entry path so cards
        and cross-tab reroutes behave identically."""
        try:
            if key == "storage":
                self.app._open_storage_insight()
            elif key == "procman":
                self.app._open_process_manager()
            elif key == "dns":
                self.app._open_dns_tester()
            elif key == "pilot":
                self.app._open_pilot_dialog()
            elif key == "drivetoolkit":
                self.app._open_drive_toolkit()
            elif key == "speed":
                self.app._open_speed_test()
            elif key == "gamepad":
                self.app._open_gamepad_tester()
            elif key == "miccheck":
                self.app._open_mic_check()
            elif key == "gameping":
                self.app._open_game_server_ping()
            elif key == "keyboard":
                self.app._open_keyboard_tester()
            elif key == "monitor":
                self.app._open_monitor_test()
            elif key == "speaker":
                self.app._open_speaker_test()
            elif key == "webcam":
                self.app._open_webcam_test()
            elif key == "mouse":
                self.app._open_mouse_tester()
            elif key == "specs":
                self.app._open_pc_specs()
            elif key == "health":
                self.app._open_health_report()
            elif key == "network_connections":
                self.app._open_network_connections()
            elif key == "services":
                self.app._open_services()
            elif key == "file_lock":
                self.app._open_file_lock()
            elif key == "duplicates":
                self.app._open_duplicates()
            elif key == "restore_points":
                self.app._open_restore_points()
            elif key == "safety_center":
                self.app._open_safety_center()
            elif key == "game_saves":
                self.app._open_game_saves()
            elif key == "game_readiness":
                self.app._open_game_readiness()
            elif key == "hwmonitor":
                self.app._open_hw_monitor()
            elif key == "startup":
                self.app._open_startup_manager()
            elif key == "uninstall":
                self.app._open_uninstall_programs()
        except Exception:
            pass

    def set_run_enabled(self, enabled: bool):
        """Run-engine tab contract (A-1 audit fix): Application gates every
        tab through set_run_enabled when a run starts/finishes (run_tasks,
        install_selected_mixed, the install _done hop). TaskTab and
        InstallTab disable their Run/Install buttons; Tools hosts no
        runnable rows, so the correct behavior is to satisfy the contract
        with nothing to gate. The missing method made every tab loop raise
        AttributeError — wedging _busy True forever on any Install/Update
        click and silently hiding the Stop button + progress bar on every
        Clean/Repair/Tweak run."""
        return None
