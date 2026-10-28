"""ToolsTab: every utility, as a card grid grouped by what it does.

The page is a catalogue of nineteen tiles: icon, name and one line saying what
the tool does, in four equal columns under four short group headings. Every
handful of seconds a user spends here is "which of these do I want", so the
page exists to be scanned, not searched - which is also why there is no search
box, no pinned recommendation band and no recency strip: nothing on the page
is a notification, a shortcut that runs on its own, or a second way to reach
the same window. Opening a tile does what its title says and nothing else.

The tile itself is `ToolCard` (one shared anatomy with the Tweak preset
cards). The grid is a FIXED four columns with equal weights, so every card is
the same width and the same height; a group with three cards leaves one cell
empty rather than stretching its card wider than its neighbours, which is the
asymmetry a "card grid" stops looking like a grid over.

The whole catalogue fits the page at the minimum window without a scroll
panel (tests pin this): four groups of cards, no wrapper band, and a card
height set by its 40px icon rather than by how long its description happens
to be.
"""
from __future__ import annotations

import tkinter as tk

from app.ui.base import Tooltip
from app.ui.theme import (
    COLORS,
    ICON_CARD_TOOL,
    PAGE_GUTTER,
    SPACE_1,
    TAB_ACCENTS,
    TYPE,
    icon_target_px,
    scaled_photo,
)
from app.ui.widgets import ToolCard, grid_cards
from app.utils import resolve_asset_path

#: Cards are grouped by the job the user is doing, not by the history of
#: which tab they once lived on. Each tool appears exactly once.
_CARDS = (
    ("storage", "\U0001F4BE", "Storage",
     "Free up disk space"),
    ("drivetoolkit", "\U0001F4BD", "Drive Toolkit",
     "Health, speed & real capacity checks"),
    ("uninstall", "\U0001F5D1\uFE0F", "Uninstall Programs",
     "Remove apps + their leftovers"),
    ("procman", "\U0001F4CB", "Process Manager",
     "See what's using CPU & RAM"),
    ("hwmonitor", "\U0001F4CA", "Hardware Monitor",
     "Live CPU, RAM, GPU, disk + network"),
    ("startup", "\U0001F680", "Startup Manager",
     "See what launches at sign-in"),
    ("specs", "\U0001F4BB", "PC Specs",
     "Your PC at a glance"),
    ("dns", "\u26A1", "DNS Test",
     "Find your fastest DNS"),
    ("speed", "\U0001F4E1", "Speed Test",
     "Check download, upload + ping"),
    ("gameping", "\U0001F4F6", "Server Ping",
     "Ping game servers before you queue"),
    ("pilot", "\U0001F3AE", "Auto-Pilot",
     "Auto-tweaks for games"),
    ("network_connections", "\U0001F310", "Network Connections",
     "See which programs are online"),
    ("services", "\u2699\uFE0F", "Services",
     "See what Windows runs in the background"),
    ("file_lock", "\U0001F512", "File Lock Finder",
     "Find out what is using a file"),
    ("duplicates", "\U0001F5C2\uFE0F", "Duplicate & Large Files",
     "Find copies you can remove"),
    ("restore_points", "\U0001F570\uFE0F", "Restore Points",
     "Your safety net: see it, and add to it"),
    ("safety_center", "\U0001F6E1\uFE0F", "Safety & Undo",
     "Restore points, undo and backups"),
    ("game_saves", "\U0001F4BE", "Game Saves",
     "Back up and restore save files"),
    ("game_readiness", "\U0001F3AE", "Game Readiness",
     "Seven checks before a smooth game"),
)

#: The four jobs. Every card lives in exactly one group; a guard test pins it.
_GROUPS = (
    ("Storage & cleanup",
     ("storage", "drivetoolkit", "uninstall", "duplicates")),
    ("System & safety",
     ("procman", "hwmonitor", "startup", "services", "file_lock", "specs",
      "restore_points", "safety_center")),
    ("Network",
     ("dns", "speed", "gameping", "network_connections")),
    ("Gaming",
     ("pilot", "game_readiness", "game_saves")),
)

#: Four columns, always. The titles are short enough that four fit at the
#: 980px minimum window, and a fixed count is what keeps every card the same
#: width - a fluid grid gives a three-card group wider cards than the
#: eight-card group above it, which reads as two different card sizes.
_COLUMNS = 4


class ToolsTab(tk.Frame):
    """The one destination for everything that is not a run."""

    _CARDS = _CARDS
    _FIX_CARDS = ()
    _GROUPS = _GROUPS

    def __init__(self, parent, app):
        super().__init__(parent, bg=COLORS["bg"])
        self.app = app
        self._photos = {}
        self._cards = {}
        self._meta = {
            key: {"icon": icon, "title": title, "blurb": blurb}
            for key, icon, title, blurb in _CARDS
        }
        self._build()

    # ------------------------------------------------------------- build

    def _build(self):
        page = tk.Frame(self, bg=COLORS["bg"])
        page.pack(fill="both", expand=True, padx=PAGE_GUTTER,
                  pady=(SPACE_1, SPACE_1))

        for index, (title, keys) in enumerate(_GROUPS):
            section = tk.Frame(page, bg=COLORS["bg"])
            # The first heading sits against the page's own top inset; the
            # rest get one gap, so the four sections read as four blocks
            # rather than as one list with bold lines in it.
            section.pack(fill="x", pady=(0 if index == 0 else SPACE_1, 0))
            tk.Label(section, text=title.upper(), font=TYPE["section"],
                     bg=COLORS["bg"], fg=COLORS["subtext"],
                     anchor="w").pack(fill="x", pady=(0, SPACE_1))
            body = tk.Frame(section, bg=COLORS["bg"])
            body.pack(fill="x")
            cards = []
            for key in keys:
                meta = self._meta.get(key)
                if meta is None:
                    continue
                card = self._build_card(body, key, meta)
                cards.append(card)
                self._cards[key] = card
            grid_cards(body, cards, _COLUMNS, equal=True, gap=SPACE_1)

        # After the grid exists: a badge mounted on a card that is not packed
        # yet would be laid out against a zero-width parent and never redraw.
        self._refresh_badges()

    def _refresh_badges(self):
        """A-004: put the findings bus on the cards it belongs to.

        The badge slot was built (CardStatusBadge) and never filled, so a tool
        that found nine issues looked exactly like one never opened. The data
        is already measured and stored by the tools themselves; this only
        renders it. A tool with no live finding gets no badge at all - a zero
        pill on eleven cards is noise that hides the two that matter.
        """
        try:
            from app import tool_hub
            badges = tool_hub.badges()
            notes = tool_hub.card_notes()
        except Exception:
            badges = {}
            notes = {}
        for key, card in self._cards.items():
            badge = getattr(card, "badge", None)
            if badge is None:
                continue
            data = badges.get(key)
            if not data:
                try:
                    badge.clear()
                except Exception:
                    pass
                continue
            count, severity = data
            text = "%d issue%s" % (count, "" if count == 1 else "s")
            tone = (COLORS["accent_red"] if severity == "critical"
                    else COLORS["accent_yellow"])
            try:
                badge.set(text, tone)
            except Exception:
                pass
            # The count alone is a mystery; the hover names the findings in
            # the bus's own worst-first order.
            titles = notes.get(key) or []
            if titles:
                try:
                    Tooltip(badge, "\n".join("\u2022 " + t for t in titles))
                except Exception:
                    pass

    def _build_card(self, parent, key, meta):
        card = ToolCard(
            parent, title=meta["title"], blurb=meta["blurb"],
            glyph=meta["icon"], photo=self._photo(key),
            accent=TAB_ACCENTS["Tools"], bg=COLORS["bg"],
            # The event argument comes FIRST and is thrown away: Tk calls a
            # <Button-1> binding with the event as the single positional
            # argument, so `lambda k=key: ...` would bind `k` to the event
            # object (and to None on the keyboard path) and every branch in
            # _mount would miss - which is exactly "clicking a tool does
            # nothing". The key is bound by keyword so nothing can overwrite it.
            on_click=lambda _e=None, k=key: self._mount(k))
        # Two tooltips, on the two labels that can be ellipsized: the full
        # name and the full sentence are always one hover away, and the blank
        # padding between them stays silent.
        Tooltip(card.title_lbl, meta["title"])
        Tooltip(card.blurb_lbl, meta["blurb"])
        return card

    def _photo(self, key):
        """The tool's 40px asset, decoded once and reused."""
        photo = self._photos.get(key)
        if photo is not None:
            return photo
        try:
            path = resolve_asset_path("tools_icons/%s.png" % key)
            if not path:
                return None
            photo = tk.PhotoImage(file=path)
            try:
                photo = scaled_photo(photo, icon_target_px(ICON_CARD_TOOL, self))
            except Exception:
                pass
            self._photos[key] = photo
            return photo
        except Exception:
            return None

    # ------------------------------------------------------------- lifecycle

    def on_show(self):
        """Refresh the live badges: a tool run while this page was hidden
        changes which cards need a look, and the counts expire with the bus.
        """
        self._refresh_badges()
        return None

    def set_run_enabled(self, enabled: bool):
        """Run-engine tab contract: this page hosts no runnable cards."""
        return None

    # ------------------------------------------------------------- launch

    def _mount(self, key):
        """A card opens the tool's window. One entry path for every card."""
        try:
            from app import tool_hub
            tool_hub.remember(key)
        except Exception:
            pass
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
            elif key == "gameping":
                self.app._open_game_server_ping()
            elif key == "specs":
                self.app._open_pc_specs()
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
            else:
                # A card whose key has no entry point is a wiring bug, not a
                # silent no-op: raise so the handler below logs it.
                raise LookupError("no tool is wired for %r" % (key,))
        except Exception as exc:
            # Never silent: a rename or a broken dialog used to make a click
            # do nothing at all, with no way to tell that from a missed click.
            try:
                self.app.log("! Could not open the %s tool: %r" % (key, exc))
            except Exception:
                pass
