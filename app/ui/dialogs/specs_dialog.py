"""SpecsDialog — extracted verbatim from app/gui.py (H12, batch 3).

One dialog per module, mirroring app/ui/widgets. Depends only on
app.ui below it, plus the app.utils helpers it already used; it
knows nothing
about any other dialog except where noted.
"""
from __future__ import annotations

import tkinter as tk

from app import facts
from app.ui.dialogs._tool_page import ToolPage
from app.ui.theme import COLORS, RADIUS_SURFACE, TAB_ACCENTS, TYPE, _hex_lerp, SPACE_2
from app.ui.metrics import CONTROL_GAP, TEXT_NUDGE
from app.ui.widgets import AnimatedButton, ScrollableRoundedPanel
from app.ui.widgets.rounded_card import RoundedCard


class SpecsDialog(ToolPage):
    """PC Specs: Speccy-style summary in plain words — left nav
    (Summary, OS, CPU, RAM, Board, Graphics, Storage, Audio, Network),
    one-line facts on the right, Copy Specs for support posts.
    Read-only engine (app/pc_specs.py); anything unreadable shows
    'Unknown', never a guess. No temperatures — those need a driver
    stdlib code can't honestly read.

    REDESIGN 2026-10: on the shared ToolPage shell — a Header banner,
    the footer's Copy Specs is the one primary action and its note is
    the shared StatusLine. The section nav stays anchored while the
    DETAIL pane scrolls on its own."""

    _TABS = ("Summary", "OS", "CPU", "RAM", "Board", "Graphics",
             "Storage", "Audio", "Network")

    def __init__(self, parent, app):
        self.app = app
        self._tab = "Summary"
        super().__init__(parent, title="PC Specs", accent=TAB_ACCENTS["Clean"],
                         scope="Read-only hardware facts — nothing here is guessed.",
                         scroll=False, fit=True)
        self.banner.set_state("\U0001F4BB", TAB_ACCENTS["Clean"])
        self.banner.set_title("What's inside this PC?")
        self.banner.set_sub("Plain words, no guesswork.")
        body = self.content
        cols = tk.Frame(body, bg=COLORS["bg"])
        cols.pack(fill="both", expand=True)
        # REDESIGN 2026-10: the section nav was a bare tk.Frame - a hard
        # grey rectangle with square corners sitting inside the rounded
        # dialog (user report: "some don't have rounded corners inside
        # their popup"). It is a RoundedCard now, and the ACTIVE section
        # gets a tonal pill the way an M3 navigation rail marks the
        # current destination (before this, no row was visibly selected).
        nav = RoundedCard(cols, fill=COLORS["bg_alt"], bg=COLORS["bg"],
                          radius=RADIUS_SURFACE, min_height=200)
        try:
            nav.configure(width=158)
        except Exception:
            pass
        nav.pack(side="left", fill="y", padx=(0, SPACE_2))
        self._nav_btns = {}
        for tab in self._TABS:
            b = AnimatedButton(nav.body, text=tab,
                               command=lambda t=tab: self._show(t),
                               bg=COLORS["bg_alt"], fg=COLORS["subtext"],
                               size="sm")
            b.pack(fill="x", padx=CONTROL_GAP, pady=TEXT_NUDGE)
            self._nav_btns[tab] = b
        self._detail_panel = ScrollableRoundedPanel(cols)
        self._detail_panel.pack(side="left", fill="both", expand=True)
        self._detail = self._detail_panel.inner
        self._detail.bind("<Configure>", self._wrap_detail, add="+")
        self._value_lbls = []

        # REDESIGN 2026-10: shared footer — the note is the StatusLine and
        # Copy Specs is the shell's one primary action.
        self.status.stop("Unknown = couldn't be read, never guessed.")
        self.add_button("Copy Specs", self._copy_specs, kind="primary")
        self.on_close(self._stop)
        self._show("Summary")

    def _rows(self, pairs):
        for child in self._detail.winfo_children():
            try:
                child.destroy()
            except Exception:
                pass
        self._value_lbls = []
        for title, value in pairs:
            tk.Label(self._detail, text=title, font=TYPE["card_title"],
                     bg=COLORS["bg"], fg=COLORS["text"],
                     anchor="w").pack(fill="x", pady=(SPACE_2, 0))
            v = tk.Label(self._detail, text=value or "Unknown", font=TYPE["caption"],
                         bg=COLORS["bg"], fg=COLORS["subtext"], anchor="w",
                         justify="left")
            v.pack(fill="x")
            self._value_lbls.append(v)
        # REDESIGN 2026-10: no literal wraplength — the values wrap to the
        # DETAIL pane's real width, re-measured on every <Configure>.
        self._wrap_detail()
        try:
            self._detail_panel.refresh_scroll()
        except Exception:
            pass

    def _paint_nav(self):
        """Tonal pill on the active section, plain rows elsewhere (M3)."""
        accent = TAB_ACCENTS["Clean"]
        for tab, b in self._nav_btns.items():
            sel = tab == self._tab
            try:
                if sel:
                    b.set_style(
                        bg=_hex_lerp(COLORS["bg_alt"], accent, 0.30),
                        fg=COLORS["text"],
                        hover_bg=_hex_lerp(COLORS["bg_alt"], accent, 0.40))
                else:
                    b.set_style(bg=COLORS["bg_alt"], fg=COLORS["subtext"],
                                hover_bg=COLORS["surface"])
            except Exception:
                pass

    def _wrap_detail(self, _event=None):
        """Bind the value labels to the DETAIL pane's actual width."""
        try:
            w = self._detail.winfo_width() - 6
        except Exception:
            return
        if w < 80:
            return                      # not laid out yet; Configure returns
        for lbl in getattr(self, "_value_lbls", []):
            try:
                lbl.config(wraplength=w)
            except Exception:
                pass

    def _show(self, tab):
        """Render a section, gathering the facts OFF the Tk thread.

        PERF. Every probe here is a registry or SMBIOS read costing hundreds of
        milliseconds, and this used to run them inline on the Tk thread, so
        opening or switching a section froze the whole app while it worked -
        worst on Summary and on Copy, which builds the full report. The
        gathering half touches no widget, so it is a pure function and belongs
        on a worker; only `self._rows` needs the Tk thread.

        `self.app` is the attribute name - reading "_app" silently yields None
        and would drop the whole path back onto the Tk thread.
        """
        self._tab = tab
        self._paint_nav()
        token = getattr(self, "_gather_seq", 0) + 1
        self._gather_seq = token
        self._rows([("Gathering...", "Reading this PC's hardware. One moment.")])

        import threading as _th

        def _work():
            try:
                rows = self._gather(tab)
            except Exception:
                rows = [("Error", "Could not read this section.")]
            try:
                # BUG-001: the `else: self.after(0, ...)` this replaces was a
                # Tk call from this worker -- RuntimeError on Python 3.14,
                # swallowed by the except, so the section never rendered. The
                # branch was also dead: Application always builds _dispatch.
                # ThemedModal now owns one, so there is a single hop and it is
                # the safe one.
                self._dispatch.post(lambda: self._render(token, rows))
            except Exception:
                pass

        try:
            _th.Thread(target=_work, daemon=True,
                       name="PcSpecsGather").start()
        except Exception:
            self._render(token, self._gather(tab))

    def _render(self, token, rows):
        """Tk-thread half. Ignores a result the user has already navigated past."""
        if token != getattr(self, "_gather_seq", 0):
            return
        try:
            self._rows(rows)
        except Exception:
            pass

    def _gather(self, tab):
        """The facts for one section, as (label, value) pairs. No Tk calls."""
        try:
            from app import pc_specs as _sp
        except Exception:
            return [("Error", "Specs engine unavailable.")]
        if tab == "Summary":
            lines = _sp.summary_lines() or ["Nothing readable."]
            return [("Your PC at a glance", "\n".join(lines))]
        # AUDIT TX-001: every section below is the SAME fact any other tool
        # reads through app/facts. Hardware Monitor's adapter list and Drive
        # Toolkit's picker come from the same two readers, so opening PC Specs
        # after either one is a cache hit instead of a second registry/WMI
        # pass. The engine imports above stay for Summary/_gb and the report.
        if tab == "OS":
            f = facts.get("hardware.os")
            if not f.ok:
                return [("Operating System", "Unknown"),
                        ("Version", f.error or "Unreadable")]
            return [("Operating System", f.value.get("caption") or "Unknown"),
                    ("Version", f.value.get("build") or "Unknown")]
        if tab == "CPU":
            f = facts.get("hardware.cpu")
            if not f.ok:
                return [("Processor", "Unknown"),
                        ("Speed · Cores", f.error or "Unreadable")]
            return [("Processor", f.value.get("name") or "Unknown"),
                    ("Speed · Cores", f.value.get("detail") or "Unknown")]
        if tab == "RAM":
            f = facts.get("hardware.ram")
            if not f.ok:
                return [("Memory", "Unknown"),
                        ("Available", f.error or "Unreadable")]
            return [("Memory", f.value.get("headline") or "Unknown"),
                    ("Available", f.value.get("detail") or "Unknown")]
        if tab == "Board":
            f = facts.get("hardware.board")
            if not f.ok:
                return [("Motherboard", "Unknown"),
                        ("Firmware", f.error or "Unreadable")]
            return [("Motherboard", f.value.get("name") or "Unknown"),
                    ("Firmware", f.value.get("firmware") or "Unknown")]
        if tab == "Graphics":
            f = facts.get("hardware.gpu")
            gpus = [g.get("name") for g in (f.value or [])
                    if isinstance(g, dict) and g.get("name")] if f.ok else []
            rows = [(f"GPU {i + 1}", g) for i, g in enumerate(gpus)] or [
                ("Graphics", "Unknown")]
            try:
                from app.tasks.tweak_tasks import _query_video_mode_list
                cur, _mx = _query_video_mode_list()
                # Audit fix: only show a real reading — the DEVMODE
                # struct bug that used to make this show "0x0 @ 0Hz" is
                # fixed at the source now, but keep this guard anyway
                # so a genuinely unreadable display never prints "0x0"
                # instead of just omitting the row.
                if cur and cur[0] and cur[1]:
                    rows.append(("Display now",
                                 f"{cur[0]}x{cur[1]} @ {cur[2]}Hz"))
            except Exception:
                pass
            return rows
        if tab == "Storage":
            f = facts.get("disks.usage")
            disks = f.value if f.ok else []
            return [(f"{d.get('drive', '?')} {d.get('label', '')}".strip(),
                     f"{_sp._gb(d.get('free', 0))} free of "
                     f"{_sp._gb(d.get('total', 0))}"
                     + (f" · {d['fs']}" if d.get("fs") else ""))
                    for d in (disks or [])] or [("Drives", "Unknown")]
        if tab == "Audio":
            names = _sp.audio_info()
            return [(f"Output {i + 1}", n) for i, n in enumerate(names)] or [
                ("Audio", "Unknown")]
        if tab == "Network":
            name, dns = _sp.net_info()
            return [("Computer", name), ("Network", dns)]
        return [("", "")]


    def _copy_specs(self):
        """Copy the full specs report (support-ticket use case)."""
        try:
            from app import pc_specs as _sp
            try:
                text = _sp.full_report_text()
            except Exception:
                text = "\n".join(_sp.summary_lines())
            if not text:
                raise ValueError("empty report")
            self._dlg.clipboard_clear()
            self._dlg.clipboard_append(text)
            self.status.stop("Copied!", COLORS["accent_green"])
        except Exception:
            try:
                self.status.stop("Copy failed.")
            except Exception:
                pass

    def _stop(self):
        pass
