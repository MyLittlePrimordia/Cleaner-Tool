"""GameServerPingDialog — extracted verbatim from app/gui.py (H12, batch 2).

One dialog per module, mirroring app/ui/widgets. Depends only on
app.ui below it, plus the app.utils helpers it already used; it
knows nothing
about any other dialog except where noted.
"""
from __future__ import annotations

import tkinter as tk
from tkinter import ttk, messagebox
from app.ui.base import ThemedModal
from app.ui.theme import COLORS, F, TAB_ACCENTS
from app.ui.widgets import AnimatedButton


class GameServerPingDialog(ThemedModal):
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
        try:
            from app import gameping as _gp0
            # user-reported: "Custom" sorted alphabetically landed in the
            # middle of the list (near the C's) instead of standing out as
            # the catch-all option. Sort everything else A-Z, then pin
            # Custom to the end where users expect a catch-all/"other"
            # entry to live.
            _rest = sorted((t for t, k in _gp0.GAME_TABS if k != "custom"),
                           key=lambda t: t.casefold())
            _games = tuple(_rest) + ("Custom",)
        except Exception:
            _games = ("Fortnite",)
        self._game_var = tk.StringVar(value=_games[0])
        super().__init__(parent, title="Game Server Ping",
                         accent=TAB_ACCENTS["Clean"])
        body = self.body
        tk.Label(body, text="Test before you queue — lower is better. "
                            "In-game ping usually reads a bit lower.",
                 font=(F, 9), bg=COLORS["bg"], fg=COLORS["subtext"],
                 anchor="w").pack(fill="x")
        top = tk.Frame(body, bg=COLORS["bg"])
        top.pack(fill="x", pady=(8, 2))
        try:
            # Option A (user-approved): the flat OptionMenu spread all 50
            # games into one menu taller than most screens — it spilled past
            # the bottom edge. A readonly Combobox scrolls its popdown and
            # gives typeahead (type "for" → Fortnite), so it always fits and
            # is far easier to navigate. Mirrors the preset picker above.
            self._game_combo = ttk.Combobox(
                top, textvariable=self._game_var, values=_games,
                state="readonly", width=42 if tk.TkVersion >= 8.6 else 38,
                font=(F, 9))
            self._game_combo.pack(side="left")
        except Exception:
            pass
        try:
            self._game_var.trace_add("write", lambda *_a: self._start_test())
        except Exception:
            pass
        AnimatedButton(top, text="Test", command=self._start_test,
                       bg=COLORS["accent_green"], fg=COLORS["black"],
                       font=(F, 9, "bold"), padx=18, pady=6).pack(side="right")
        self._method_lbl = tk.Label(body, text="", font=(F, 8),
                                    bg=COLORS["bg"], fg=COLORS["subtext"],
                                    anchor="w")
        self._method_lbl.pack(fill="x")
        self._custom_row = tk.Frame(body, bg=COLORS["bg"])
        self._rows_body = tk.Frame(body, bg=COLORS["bg"])
        self._rows_body.pack(fill="x", pady=(4, 0))
        self._verdict_lbl = tk.Label(body, text="", font=(F, 11, "bold"),
                                     bg=COLORS["bg"], fg=COLORS["text"],
                                     anchor="w")
        self._verdict_lbl.pack(fill="x", pady=(8, 0))
        self.on_close(self._cancel)
        self._start_test()

    # ---- target lists ------------------------------------------------- #

    def _tab_key(self):
        try:
            from app import gameping as _gp
            want = self._game_var.get()
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
                self._method_lbl.config(text=_gp.GAME_METHODS.get(key, ""))
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
            self._custom_row.pack(fill="x", pady=(4, 0))
            self._custom_entry = tk.Entry(
                self._custom_row, bg=COLORS["surface"], fg=COLORS["text"],
                insertbackground=COLORS["text"], font=(F, 9), bd=0,
                highlightthickness=1, highlightbackground=COLORS["hairline"],
                width=34)
            self._custom_entry.pack(side="left")
            AnimatedButton(self._custom_row, text="Add",
                           command=self._add_custom,
                           bg=COLORS["surface"], fg=COLORS["text"],
                           font=(F, 9, "bold"), padx=14,
                           pady=4).pack(side="left", padx=(8, 0))
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
                if saved not in hosts and len(hosts) < 64:
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
                row = tk.Frame(self._rows_body, bg=COLORS["bg"])
                row.pack(fill="x", pady=1)
                tk.Label(row, text=title, font=(F, 9, "bold"),
                         bg=COLORS["bg"], fg=COLORS["text"],
                         anchor="w").pack(side="left")
                if custom:
                    _rm = tk.Label(row, text="✕", font=(F, 9),
                                   bg=COLORS["bg"], fg=COLORS["subtext"],
                                   cursor="hand2")
                    _rm.pack(side="left", padx=(6, 0))
                    _rm.bind("<Button-1>",
                             lambda _e, raw=key: self._remove_custom(raw),
                             add="+")
                ms = tk.Label(row, text="…", font=(F, 9),
                              bg=COLORS["bg"], fg=COLORS["subtext"],
                              anchor="e")
                ms.pack(side="right")
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
        duplicate is removed; tools/verify_low001_dupes.py now fails if any
        class in app/ defines the same method twice.
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
