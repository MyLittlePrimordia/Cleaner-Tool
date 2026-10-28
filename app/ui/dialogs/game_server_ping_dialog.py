"""GameServerPingDialog — extracted verbatim from app/gui.py (H12, batch 2).

One dialog per module, mirroring app/ui/widgets. Depends only on
app.ui below it, plus the app.utils helpers it already used; it
knows nothing
about any other dialog except where noted.
"""
from __future__ import annotations

import tkinter as tk

from app.ui.widgets.rounded_card import RoundedCard
from app.ui.widgets.entry import RoundedEntry

from app.ui.widgets.dropdown import M3Dropdown
from app.ui.dialogs._tool_page import ToolPage
from app.ui.theme import (
    COLORS, RADIUS_SURFACE, TAB_ACCENTS, TYPE, SPACE_1, SPACE_2, SPACE_4,
)
from app.ui.metrics import CARD_BODY_INSET, GROUP_GAP, TIGHT_GAP
from app.ui.base import Tooltip
from app.ui.widgets import AnimatedButton
from app.config_schema import GAMEPING_MAX


class _Row(RoundedCard):
    """RoundedCard whose winfo_children() lists the labels inside it, so the
    result-marking code that walks a plain row Frame keeps working."""

    def winfo_children(self):
        return self.body.winfo_children()


class GameServerPingDialog(ToolPage):
    """Game Server Ping (user-approved Tools feature): ping before you
    queue. Fortnite regions go to Epic's official per-region endpoints
    (ICMP echo); company rows measure the TCP path to each publisher's
    edge, honestly labeled as such; Custom covers anything else
    (`hostname` = ping, `hostname:port` = TCP).

    Workers never touch Tk (F10): each probe posts its row via after();
    a token cancels stale rounds; close stops everything. Read-only
    measurement — no busy-guard needed, same rationale as DNS/Speed."""

    def __init__(self, parent, app):
        self.app = app
        self._token = [False]
        self._rows = {}
        self._results = {}
        self._inbox = []       # worker postings (token, key, avg) — plain
        self._landed = [0]     # painted this round (Tk thread only)
        self._total = [0]
        self._poll_after = None
        # U-008: order/remember live in the data module (pure and testable);
        # this dialog owns only the widget. Until the installed-games scan
        # lands, `order_titles()` is the same A-Z list with Custom pinned to
        # the end (the fix for Custom landing among the C's).
        self._quiet = False          # True while WE set the picker value
        self._installed = ()         # titles of games found on this PC
        try:
            from app import gameping as _gp0
            _games = _gp0.order_titles()
        except Exception:
            _games = ("Fortnite",)
        self._saved_pick = self._saved_game()
        self._game_var = tk.StringVar(
            value=self._initial_game(_games, self._saved_pick))
        super().__init__(parent, title="Game Server Ping",
                         accent=TAB_ACCENTS["Clean"],
                         scope="Ping is a measurement only — nothing is "
                               "changed on your PC.",
                         with_toolbar=True, scroll=True)
        # REDESIGN 2026-10: on the shared ToolPage shell. The descriptive
        # headline became the Header banner, the game picker moved to the
        # shared toolbar and Test became the footer's primary action.
        body = self.content
        self.banner.set_state("\u25CE", TAB_ACCENTS["Clean"])
        self.banner.set_title("Check your ping before you queue")
        self.banner.set_sub("Lower is better.")

        try:
            # Option A (user-approved): the flat OptionMenu spread all 50
            # games into one menu taller than most screens — it spilled past
            # the bottom edge. A readonly Combobox scrolls its popdown and
            # gives typeahead (type "for" → Fortnite), so it always fits and
            # is far easier to navigate. Mirrors the preset picker above.
            self._game_combo = M3Dropdown(
                self.toolbar, textvariable=self._game_var, values=_games,
                state="readonly", width=42 if tk.TkVersion >= 8.6 else 38,
                font=TYPE["body_large"])
            self._game_combo.pack(side="left")
            Tooltip(self._game_combo,
                    "Games already installed on this PC are listed first, "
                    "marked \u25cf. Custom covers anything not listed.")
        except Exception:
            pass
        try:
            self._game_var.trace_add("write", lambda *_a: self._on_game_change())
        except Exception:
            pass
        self._test_btn = self.add_button("Test", self._start_test,
                                         kind="primary")
        # kept (unpacked): the update code still writes the method note here
        self._method_lbl = tk.Label(body, text="")
        self._custom_row = tk.Frame(body, bg=COLORS["bg"])
        self._rows_body = tk.Frame(body, bg=COLORS["bg"])
        self._rows_body.pack(fill="x", pady=(SPACE_1, 0))
        self._verdict_lbl = tk.Label(body, text="", font=TYPE["readout_small"],
                                     bg=COLORS["bg"], fg=COLORS["text"])
        self._verdict_lbl.pack(pady=(SPACE_4, 0))
        self.on_close(self._cancel)
        self._start_test()
        self._order_by_installed()

    # ---- target lists ------------------------------------------------- #

    # ---- remembering the pick / ordering by what is installed (U-008) -- #

    @staticmethod
    def _saved_game():
        """The bare title saved by an earlier session, or "".

        Stored bare (no ● marker) because the marker depends on what is
        installed today, not on what was installed when it was saved.
        """
        try:
            from app.config_persist import load_config
            return str(load_config().get("gameping_last_game") or "")
        except Exception:
            return ""

    @staticmethod
    def _initial_game(games, saved):
        """The pick to open on: the saved game while it still exists.

        A title that is gone (renamed tab, hand-edited config) falls back to
        the first row rather than being handed to the picker.
        """
        if saved and saved in games:
            return saved
        return games[0] if games else "Custom"

    def _on_game_change(self):
        self._remember_pick()
        self._start_test()

    def _remember_pick(self):
        """Save the pick for next time. Best-effort: a failed write must not
        swallow the ping the user just asked for."""
        if getattr(self, "_quiet", False):
            return
        try:
            from app import gameping as _gp
            from app.config_persist import update_config
            title = _gp.bare_title(self._game_var.get())
            if not title:
                return

            def _mut(cfg):
                cfg["gameping_last_game"] = title
            update_config(_mut)
        except Exception:
            pass

    def _order_by_installed(self):
        """U-008: float the games this PC already has to the top.

        Off the Tk thread: discovering installed games reads the registry and
        every Steam library manifest, which is seconds on a big library.
        Until it lands the picker is the plain A-Z list.
        """
        def _scan():
            try:
                from app.game_catalog import installed_games
                names = [(g.get("name") or "") for g in installed_games() or []]
            except Exception:
                return
            if not names:
                return
            try:
                self.after(0, lambda: self._apply_installed_order(names))
            except Exception:
                pass
        try:
            import threading
            threading.Thread(target=_scan, daemon=True,
                             name="gameping-installed").start()
        except Exception:
            pass

    def _apply_installed_order(self, names):
        try:
            from app import gameping as _gp
            # This page is a ThemedModal, not a Widget: the window is _dlg.
            # Nothing may touch Tk from the scan thread after close().
            if getattr(self, "_modal_closed", False):
                return
            dlg = getattr(self, "_dlg", None)
            if dlg is not None and not dlg.winfo_exists():
                return
            hits = _gp.installed_titles(names)
            if not hits or set(hits) == set(self._installed):
                return
            first_scan = not self._installed
            self._installed = hits
            current = _gp.bare_title(self._game_var.get())
            if first_scan and not self._saved_pick:
                # Nothing was ever picked, so the page opened on whatever
                # sorted first. Now that we know what is installed, start on
                # the top row instead of pinging a game nobody owns.
                current = hits[0]
            values = _gp.picker_values(names)
            self._quiet = True
            try:
                self._game_combo.configure(values=values)
                if current:
                    self._game_var.set(_gp.mark_installed(current, hits))
            finally:
                self._quiet = False
        except Exception:
            pass

    def _tab_key(self):
        try:
            from app import gameping as _gp
            want = _gp.bare_title(self._game_var.get())
            for title, key in _gp.GAME_TABS:
                if title == want:
                    return key
        except Exception:
            pass
        return "fortnite"

    def _targets(self):
        """[(key, title, kind, host, port_or_None)] for the picked game."""
        try:
            from app import gameping as _gp
            from app.config_persist import load_config as _load
            key = self._tab_key()
            try:
                self._method_lbl.config(text=_gp.method_note(key))
            except Exception:
                pass
            if key == "custom":
                return _gp.targets_for(key, (_load().get("gameping_hosts") or []))
            return _gp.targets_for(key)
        except Exception:
            return []

    def _refresh_custom_row(self, show):
        try:
            for w in list(self._custom_row.winfo_children()):
                try:
                    w.destroy()
                except Exception:
                    pass
            try:
                self._custom_row.pack_forget()
            except Exception:
                pass
            if not show:
                return
            self._custom_row.pack(pady=(SPACE_2, 0))
            _ce = RoundedEntry(self._custom_row, width=34, font=TYPE["body_large"])
            _ce.pack(side="left")
            self._custom_entry = _ce.entry
            AnimatedButton(self._custom_row, text="Add",
                           command=self._add_custom,
                           role="tonal", size="md").pack(side="left",
                                                         padx=(SPACE_2, 0))
        except Exception:
            pass

    def _add_custom(self):
        try:
            from app import gameping as _gp
            from app.config_persist import update_config as _update
            raw = (self._custom_entry.get() or "").strip()
            host, port = _gp.split_custom(raw)
            if not host:
                try:
                    self._verdict_lbl.config(
                        text="That doesn't look like a hostname.",
                        fg=COLORS["accent_yellow"])
                except Exception:
                    pass
                return
            saved = port if port is not None else host

            def _mut(cfg, saved=saved):
                hosts = cfg.get("gameping_hosts")
                if not isinstance(hosts, list):
                    hosts = cfg["gameping_hosts"] = []
                # I-007: the schema's cap, not a second literal 64. A drifted
                # copy would make the dialog reject targets the loader accepts
                # (or the reverse) with nothing to catch it.
                if saved not in hosts and len(hosts) < GAMEPING_MAX:
                    hosts.append(saved)
            _update(_mut)
            try:
                self._custom_entry.delete(0, "end")
            except Exception:
                pass
            self._start_test()
        except Exception:
            pass

    def _remove_custom(self, raw):
        try:
            from app.config_persist import update_config as _update

            def _mut(cfg, raw=raw):
                hosts = cfg.get("gameping_hosts")
                if isinstance(hosts, list) and raw in hosts:
                    hosts.remove(raw)
            _update(_mut)
            self._start_test()
        except Exception:
            pass

    # ---- test orchestration (inbox pattern) ------------------------- #
    # Python 3.14 forbids Tk calls off the creating thread
    # (RuntimeError: main thread is not in main loop) — so workers
    # NEVER touch Tk, not even after(). They append plain tuples to
    # _inbox; the Tk-thread poller below drains and paints. Thread
    # safety comes from single ops under the GIL (append, swap).

    def _cancel(self):
        try:
            self._token[0] = True
        except Exception:
            pass
        # LOW-001: this cancel block was present TWICE, identically. The second
        # copy was unreachable by construction - the first sets
        # `self._poll_after = None`, so the second copy's
        # `if self._poll_after is not None` could never be true. Harmless, but
        # it is a merge artifact that invites a future edit to the wrong copy.
        try:
            if self._poll_after is not None:
                self._dlg.after_cancel(self._poll_after)
        except Exception:
            pass
        self._poll_after = None


    def _start_test(self):
        try:
            self._token[0] = True
        except Exception:
            pass
        token = self._token = [False]
        targets = self._targets()
        custom = self._game_var.get() == "Custom"
        self._refresh_custom_row(custom)
        try:
            for w in list(self._rows_body.winfo_children()):
                try:
                    w.destroy()
                except Exception:
                    pass
        except Exception:
            pass
        self._rows = {}
        self._results = {}
        for key, title, _kind, _host, _port in targets:
            try:
                row = _Row(self._rows_body, fill=COLORS["bg_alt"],
                           bg=COLORS["bg"], radius=RADIUS_SURFACE, min_height=48)
                row.pack(fill="x", pady=TIGHT_GAP)
                rb = row.body
                tk.Label(rb, text=title, font=TYPE["card_title"],
                         bg=COLORS["bg_alt"], fg=COLORS["text"],
                         anchor="w").pack(side="left", padx=(CARD_BODY_INSET, 0), pady=GROUP_GAP)
                if custom:
                    _rm = tk.Label(rb, text="\u2715", font=TYPE["body_medium"],
                                   bg=COLORS["bg_alt"], fg=COLORS["subtext"],
                                   cursor="hand2")
                    _rm.pack(side="left", padx=(SPACE_2, 0))
                    _rm.bind("<Button-1>",
                             lambda _e, raw=key: self._remove_custom(raw),
                             add="+")
                ms = tk.Label(rb, text="\u2026", font=TYPE["body_large"],
                              bg=COLORS["bg_alt"], fg=COLORS["subtext"],
                              anchor="e")
                ms.pack(side="right", padx=(0, CARD_BODY_INSET))
                self._rows[key] = {"ms": ms, "row": row}
            except Exception:
                pass
        try:
            self._verdict_lbl.config(text="Testing…", fg=COLORS["subtext"])
        except Exception:
            pass
        if not targets:
            try:
                self._verdict_lbl.config(
                    text="Add a host above, then Test." if custom else "Nothing to test.",
                    fg=COLORS["subtext"])
            except Exception:
                pass
            return
        self._results = {}
        self._inbox = []
        self._landed = [0]
        self._total = [len(targets)]
        try:
            import threading as _th
            for key, title, kind, host, port in targets:
                _th.Thread(target=self._probe_one,
                           args=(token, key, kind, host, port),
                           daemon=True).start()
        except Exception:
            pass
        try:
            self._poll_after = self._dlg.after(80, self._poll)
        except Exception:
            self._poll_after = None

    def _probe_one(self, token, key, kind, host, port):
        """Worker thread: probe, then post a plain tuple. No Tk here —
        not even after() (3.14 forbids it off-thread)."""
        try:
            from app import gameping as _gp
            try:
                dead = bool(token[0])
            except Exception:
                dead = True
            if dead:
                return
            cancelled = lambda: bool(token[0])
            if kind == "tcp":
                _sent, _ok, avg = _gp.probe_tcp(host, port or 443,
                                                cancelled=cancelled)
            else:
                _sent, _ok, avg = _gp.probe_icmp(host, cancelled=cancelled)
            try:
                self._inbox.append((token, key, avg))
            except Exception:
                pass
        except Exception:
            try:
                self._inbox.append((token, key, None))
            except Exception:
                pass

    def _poll(self):
        """Tk thread: drain postings, paint, reschedule while live.

        LOW-001: this method was DEFINED TWICE in this class, with functionally
        identical bodies - only the docstring differed. The second definition
        silently won, so the first was dead code. The verdict is benign rather
        than a bug, but it is a maintainability trap: a future fix applied to
        the wrong copy does nothing at all, with no error to hint at it. The
        duplicate is removed. The lesson stands: a method defined twice in a
        class silently keeps only the last body.
        """
        self._poll_after = None
        try:
            inbox, self._inbox = self._inbox, []
        except Exception:
            inbox = []
        for tok, key, avg in inbox:
            try:
                if tok is self._token:
                    self._land_one(tok, key, avg)
            except Exception:
                pass
        try:
            if not self._token[0] and (self._landed[0] or 0) < (self._total[0] or 0):
                self._poll_after = self._dlg.after(80, self._poll)
        except Exception:
            self._poll_after = None

    def _land_one(self, token, key, avg):
        """Tk thread: paint one row; crown the best when all landed."""
        try:
            if token is not self._token:
                return
        except Exception:
            return
        try:
            from app import gameping as _gp
            self._results[key] = avg
            slot = self._rows.get(key)
            if slot is not None:
                if avg is None:
                    slot["ms"].config(text="no reply", fg=COLORS["subtext"])
                else:
                    word, good = _gp.verdict(avg)
                    slot["ms"].config(
                        text=f"{avg:.0f} ms · {word}",
                        fg=COLORS["accent_green"] if good else (
                            COLORS["accent_yellow"] if (avg or 0) < 100
                            else COLORS["accent_red"]))
            try:
                self._landed[0] += 1
            except Exception:
                pass
            if (self._landed[0] or 0) >= (self._total[0] or 0):
                self._crown_best()
        except Exception:
            pass

    def _crown_best(self):
        try:
            from app import gameping as _gp
            ranked = sorted(((k, v) for k, v in self._results.items()
                             if v is not None), key=lambda kv: kv[1])
            if not ranked:
                self._verdict_lbl.config(text="No reply from anywhere — "
                                              "check your connection.",
                                         fg=COLORS["accent_red"])
                return
            best, ms = ranked[0]
            word, _good = _gp.verdict(ms)
            slot = self._rows.get(best)
            if slot is not None:
                try:
                    for w in list(slot["row"].winfo_children()):
                        if isinstance(w, tk.Label) and w is not slot["ms"]:
                            w.config(text="★ " + str(w.cget("text")))
                            break
                except Exception:
                    pass
            self._verdict_lbl.config(text=f"Best: {best} — {ms:.0f} ms ({word}).",
                                     fg=COLORS["accent_green"])
        except Exception:
            pass
