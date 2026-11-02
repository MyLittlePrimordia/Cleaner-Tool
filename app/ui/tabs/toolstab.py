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

Above the grid sit the two hub surfaces (A-001/U-001): a "Recommended for
this PC" band driven by `tool_hub.recommendations()` (evidence-captioned,
hidden when the PC is healthy) and a Recent strip driven by the recency list
`tool_hub.remember()` already stored on every card open. Both route through
`app.routes.open_route`, so a recommendation lands on the screen that owns
the fix. The whole catalogue fits the page at the minimum window without a
scroll panel (tests pin this): four groups of cards, no wrapper band, and a
card height set by its 40px icon rather than by how long its description
happens to be.
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
from app.ui.widgets import AnimatedButton, ToolCard, grid_cards
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

        # Hub surfaces: recommendations first (the answer to "where do I
        # look"), then recency. Both are empty-height when they have nothing
        # to say, so a healthy PC with no history sees the plain catalogue.
        self._reco_frame = tk.Frame(page, bg=COLORS["bg"])
        self._reco_frame.pack(fill="x", pady=(0, SPACE_1))
        self._recent_frame = tk.Frame(page, bg=COLORS["bg"])
        self._recent_frame.pack(fill="x", pady=(0, SPACE_1))
        self._first_section = None

        for index, (title, keys) in enumerate(_GROUPS):
            section = tk.Frame(page, bg=COLORS["bg"])
            if index == 0:
                self._first_section = section
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
        self._refresh_hub()
        # The catalogue must fit the band the shell gives a page. A Recent
        # strip is real content (it appears after opening any tool) and adds
        # ~31px, which overflows the 852x477 minimum band; recommendations
        # can add more. The strip is the least important surface, so it is
        # the one that yields at that size and returns when there is room.
        # Measured from REAL geometry, not winfo_reqheight: before the tab is
        # mapped the cards still request ~1px, so a build-time measurement
        # reports the strip as free. The idle pass after a Configure sees the
        # laid-out cards and their true bottom edge.
        self.bind("<Configure>",
                  lambda _e: self.after_idle(self._fit_hub), add="+")
        try:
            self.after_idle(self._fit_hub)
        except Exception:
            pass

    def _refresh_hub(self):
        """A-001/U-001: render the hub engine — recommendations + recency.

        Recommendations are facts with evidence ("C: is 8% free"), never
        generic tips; an empty engine renders nothing, so a healthy PC gets
        no band rather than three arbitrary cards. Every row opens through
        `app.routes` (the screen that owns the fix), falling back to the
        card's own mount when a tool key has no route. The Recent strip
        reads the list `_mount` already stored — previously written on every
        click and never shown anywhere.
        """
        for frame in (getattr(self, "_reco_frame", None),
                      getattr(self, "_recent_frame", None)):
            if frame is None:
                continue
            for child in frame.winfo_children():
                try:
                    child.destroy()
                except Exception:
                    pass
        try:
            from app import tool_hub
            recs = tool_hub.recommendations()
        except Exception:
            recs = []
        for key, reason, grade in recs or []:
            self._hub_row(key, reason, grade)
        try:
            keys = [k for k in tool_hub.recent(limit=5)
                    if k in self._meta]
        except Exception:
            keys = []
        self._recent_keys = keys
        self._recent_shrinking = False
        self._recent_hidden = False
        self._render_recent(5)
        if keys:
            try:
                if not self._recent_frame.winfo_manager():
                    self._recent_frame.pack(
                        fill="x", pady=(0, SPACE_1),
                        before=getattr(self, "_first_section", None))
            except Exception:
                pass

    def _render_recent(self, limit):
        """Draw the RECENT strip with at most `limit` chips (U-001).

        U-001: when the page band was short, the whole strip was dropped, so
        the user's own habits were the first thing lost. One chip is still a
        shortcut, so the strip compacts to two before it yields entirely.
        """
        frame = getattr(self, "_recent_frame", None)
        if frame is None:
            return False
        for child in frame.winfo_children():
            try:
                child.destroy()
            except Exception:
                pass
        keys = [k for k in getattr(self, "_recent_keys", [])
                if k in self._meta][:limit]
        self._recent_limit = len(keys)
        if not keys:
            return False
        try:
            tk.Label(frame, text="RECENT", font=TYPE["section"],
                     bg=COLORS["bg"], fg=COLORS["subtext"],
                     anchor="w").pack(side="left", padx=(0, SPACE_1))
        except Exception:
            pass
        for key in keys:
            title = self._meta[key]["title"]
            try:
                AnimatedButton(
                    frame, text=title,
                    command=lambda k=key: self._mount(k),
                    role="tonal", size="sm").pack(
                        side="left", padx=(0, SPACE_1))
            except Exception:
                pass
        return True

    def _fit_hub(self, event=None):
        """Hide the Recent strip when the page band cannot hold it.

        Measured, not a fixed breakpoint: `_hub_full_need` is the requested
        height with the strip packed (the strip's children are built), so a
        larger DPI, a longer recent list or extra recommendation rows all
        move the threshold with the content. Packing it back `before` the
        first section keeps the original order when the window grows.
        """
        recent = getattr(self, "_recent_frame", None)
        if recent is None or not recent.winfo_children():
            return
        # The first idle pass can run before the packer has given the cards
        # their real height. Forcing idle layout here is what makes the
        # measurement true in that pass; the flag stops the Configure this
        # produces from re-entering the same measurement.
        if getattr(self, "_hub_fitting", False):
            return
        self._hub_fitting = True
        try:
            self.update_idletasks()
            try:
                avail = int(getattr(event, "height", 0) or self.winfo_height())
            except Exception:
                return
            if avail <= 1:
                return
            if recent.winfo_manager():
                try:
                    bottom = max(card.winfo_rooty() + card.winfo_height()
                                 for card in self._cards.values()) \
                        - self.winfo_rooty()
                except Exception:
                    return
                if bottom > avail:
                    # Remember how tall the catalogue wanted to be (with
                    # the strip); growing back past it can show it again.
                    self._hub_full_need = max(bottom, avail)
                    # U-001: compact the strip before dropping it. Two chips
                    # still answer "where was I", and the next idle pass
                    # measures whether even that fits.
                    if (self._recent_limit > 2
                            and not getattr(self, "_recent_shrinking", False)):
                        self._recent_shrinking = True
                        self._render_recent(2)
                        try:
                            self.after_idle(self._fit_hub)
                        except Exception:
                            pass
                        return
                    recent.pack_forget()
                    self._recent_hidden = True
            elif avail >= int(getattr(self, "_hub_full_need", 0) or 0):
                try:
                    if getattr(self, "_recent_limit", 0) < 5:
                        self._recent_shrinking = False
                        self._render_recent(5)
                    self._recent_hidden = False
                    recent.pack(fill="x", pady=(0, SPACE_1),
                                before=getattr(self, "_first_section", None))
                except Exception:
                    pass
        finally:
            self._hub_fitting = False

    def _hub_row(self, key, reason, grade):
        """One recommendation row: severity dot, evidence, Open button."""
        meta = self._meta.get(key, {})
        title = meta.get("title", key)
        tone = (COLORS["accent_red"] if grade == "F"
                else COLORS["accent_yellow"])
        try:
            row = tk.Frame(self._reco_frame, bg=COLORS["bg"])
            row.pack(fill="x", pady=(0, SPACE_1))
            tk.Label(row, text="\u25cf", font=TYPE["label_medium"],
                     bg=COLORS["bg"], fg=tone).pack(side="left")
            tk.Label(row, text="%s — %s" % (title, reason),
                     font=TYPE["label_medium"], bg=COLORS["bg"],
                     fg=COLORS["text"], anchor="w",
                     justify="left").pack(side="left", fill="x", expand=True,
                                          padx=(SPACE_1, SPACE_1))
            AnimatedButton(row, text="Open",
                           command=lambda k=key: self._open_recommended(k),
                           role="tonal", size="sm").pack(side="right")
        except Exception:
            pass

    def _open_recommended(self, key):
        """Open a recommendation through the routes table (owned screen).

        Falls back to the card mount when the key has no route, and records
        the open in recency either way — a recommendation acted on is still
        a recent tool.
        """
        try:
            from app import routes as _routes
            if _routes.open_route(self.app, key):
                try:
                    from app import tool_hub
                    tool_hub.remember(key)
                except Exception:
                    pass
                return
        except Exception:
            pass
        self._mount(key)

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
        The hub band refreshes with it — a fixed problem clears its own
        recommendation instead of waiting for a restart.
        """
        self._refresh_badges()
        try:
            self._refresh_hub()
        except Exception:
            pass
        return None

    def set_run_enabled(self, enabled: bool):
        """Run-engine tab contract: this page hosts no runnable cards."""
        return None

    # ------------------------------------------------------------- launch

    def _mount(self, key):
        """A card opens the tool's window — through the one routes table.

        U-002: this used to be a 19-branch if/elif that had to agree with
        app.routes by hand. A card whose key had no branch raised; a route
        added for recommendations but not here was a silent difference between
        clicking a card and accepting a recommendation. The table is now the
        only mapping, and a guard test checks that every card in _CARDS
        resolves and that its Application method exists.
        """
        try:
            from app import tool_hub
            tool_hub.remember(key)
        except Exception:
            pass
        try:
            from app import routes
            if routes.open_route(self.app, key):
                return
            raise LookupError("no route is wired for %r" % (key,))
        except Exception as exc:
            # Never silent: a rename or a broken dialog used to make a click
            # do nothing at all, with no way to tell that from a missed click.
            try:
                self.app.log("! Could not open the %s tool: %r" % (key, exc))
            except Exception:
                pass
