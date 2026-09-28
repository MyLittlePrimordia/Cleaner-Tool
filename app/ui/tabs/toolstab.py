"""ToolsTab — extracted verbatim from app/gui.py (H13).

A feature tab page. May use the widget set and the modal base, and
opens dialogs, but never imports another tab.
"""
from __future__ import annotations


import tkinter as tk
from tkinter import ttk, messagebox
from app.tools_shortcuts import TOOLS_SHORTCUTS
from app.ui.base import Tooltip
from app.ui.theme import COLORS, F, TAB_ACCENTS
from app.utils import resolve_asset_path


class ToolsTab(tk.Frame):
    """Tools tab (user redesign 2026-09): the 5th tab — pink accent.

    Feature cards in a 3-wide grid — the exact same card size,
    gaps and grid as the Tweak tab's 6 preset cards (PER_ROW=3,
    same pack/grid/pad). The old inline shortcuts box now lives in
    the 6th card's Quick Tools popup, so the tab itself is cards
    only, like Tweak's top."""

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
         "See which keys register — spot ghosting", TAB_ACCENTS["Clean"]),
        ("mouse", "🖱️", "Mouse Tester",
         "Buttons, polling + aim test", TAB_ACCENTS["Clean"]),
        ("monitor", "🖥️", "Monitor Test",
         "Dead pixels + display info", TAB_ACCENTS["Clean"]),
        ("webcam", "📷", "Webcam Test",
         "Check your camera works", TAB_ACCENTS["Clean"]),
        ("miccheck", "🎙️", "Mic Check",
         "Mics, permission + live level", TAB_ACCENTS["Tweak"]),
        ("speaker", "🔊", "Speaker Test",
         "Surround check on any output", TAB_ACCENTS["Clean"]),
    )

    # H12: the table itself now lives in app.tools_shortcuts so QuickToolsDialog
    # can read it without importing this tab. Kept as a class attribute so
    # `ToolsTab._SHORTCUTS` still resolves, to the same object.
    _SHORTCUTS = TOOLS_SHORTCUTS

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
        # the shortcuts box now lives in the Quick Tools popup — the
        # tab itself is cards only.
        # (Why no topbar: keeping it would push every card down vs
        # Tweak; exact position match requires the same top offset.)
        PER_ROW = 3
        card_area = tk.Frame(self, bg=COLORS["bg"])
        card_area.pack(fill="x", padx=26, pady=(14, 4))
        self._card_area_ref = card_area
        # A card count that is not a multiple of 3 would leave the final
        # card jammed against the left edge with two empty cells beside
        # it, which reads as a layout bug rather than a design. When
        # exactly one card is left over it goes in the MIDDLE column, so
        # the last row looks deliberate. Two left over already balance.
        n_cards = len(self._CARDS)
        lone_last = (n_cards % PER_ROW) == 1
        last_row = (n_cards - 1) // PER_ROW
        for i, (key, icon, title, blurb, card_accent) in enumerate(self._CARDS):
            row, col = divmod(i, PER_ROW)
            if lone_last and row == last_row:
                col = 1
            self._build_card(card_area, row, col, key, icon, title, blurb,
                             card_accent)
        for c in range(PER_ROW):
            card_area.grid_columnconfigure(c, weight=1, uniform="cards")
        # Kept for the smoke harness (it scans this for shortcut rows);
        # now points at the card grid — the rows themselves live in the
        # Quick Tools popup, which the harness opens separately.
        self._panel_ref = card_area

    def _build_shortcut_column(self, parent, col, title, items):
        """One bullet column: left-aligned header + hairline divider +
        bullet rows (pink dot + bold name, Install-row language)."""
        cell = tk.Frame(parent, bg=COLORS["bg_alt"])
        cell.grid(row=0, column=col, sticky="new", padx=8)
        tk.Label(cell, text=title, font=(F, 10, "bold"),
                 bg=COLORS["bg_alt"], fg=COLORS["text"],
                 anchor="w").pack(fill="x")
        tk.Frame(cell, bg=COLORS["hairline"], height=1).pack(
            fill="x", pady=(4, 6))
        for label, target in items:
            self._make_bullet(cell, label, target)

    def _make_bullet(self, parent, label, target):
        """One bullet row: pink dot + clickable name (hand cursor, pink
        hover). True list rows — no card chrome, no cell padding beyond
        a single pixel separator (the 10-row Settings column must fit
        the page with zero scrolling; 31px rows stay roomy)."""
        row = tk.Frame(parent, bg=COLORS["bg_alt"])
        row.pack(fill="x", pady=0)
        dot = tk.Label(row, text="•", font=(F, 9, "bold"),
                       bg=COLORS["bg_alt"], fg=TAB_ACCENTS["Tools"],
                       bd=0, highlightthickness=0)
        dot.pack(side="left", padx=(2, 4))
        lbl = tk.Label(row, text=label, font=(F, 9, "bold"),
                       bg=COLORS["bg_alt"], fg=COLORS["text"],
                       cursor="hand2", anchor="w",
                       bd=0, highlightthickness=0)
        lbl.pack(side="left", fill="x", expand=True)
        for w in (row, dot, lbl):
            w.bind("<Enter>", lambda e, l=lbl: l.config(
                fg=TAB_ACCENTS["Tools"]), add="+")
            w.bind("<Leave>", lambda e, l=lbl: l.config(
                fg=COLORS["text"]), add="+")
            w.bind("<Button-1>",
                   lambda e, t=target: self._open_target(t), add="+")

    def _build_card(self, parent, rr, cc, key, icon, title, blurb,
                      card_accent):
        """One feature card: full-color PNG icon (Twemoji) when present under
        assets/tools_icons/{key}.png, else accent-colored emoji glyph. Bold
        title; description lives in a hover tooltip. Grid pads match Tweak."""
        outer = tk.Frame(parent, bg=COLORS["hairline"], bd=0)
        outer.grid(row=rr, column=cc, sticky="nsew", padx=5, pady=4)
        inner = tk.Frame(outer, bg=COLORS["bg_alt"])
        inner.pack(fill="both", expand=True, padx=1, pady=1)
        head = tk.Frame(inner, bg=COLORS["bg_alt"])
        head.pack(fill="x", padx=12, pady=(6, 6))

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

        if photo is not None:
            icon_lbl = tk.Label(head, image=photo, bg=COLORS["bg_alt"],
                                bd=0, highlightthickness=0)
        else:
            icon_lbl = tk.Label(head, text=icon, font=("Segoe UI Emoji", 16),
                                bg=COLORS["bg_alt"], fg=card_accent)
        icon_lbl.pack(side="left")
        title_lbl = tk.Label(head, text=title, font=(F, 12, "bold"),
                             bg=COLORS["bg_alt"], fg=COLORS["text"])
        title_lbl.pack(side="left", padx=8)
        self._card_frames[key] = (outer, inner)

        if blurb:
            for _w in (outer, inner, head, icon_lbl, title_lbl):
                Tooltip(_w, blurb)

        def hover_on(_e):
            try:
                outer.config(bg=TAB_ACCENTS["Tools"])
            except Exception:
                pass

        def hover_off(_e):
            try:
                outer.config(bg=COLORS["hairline"])
            except Exception:
                pass

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

    def _open_target(self, target):
        try:
            if target.startswith("ms-settings:"):
                self.app._launch_settings(target)
            else:
                self.app._launch(target)
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
