"""DnsTesterDialog — extracted verbatim from app/gui.py (H12, batch 1).

One dialog per module, mirroring app/ui/widgets. Depends only on
app.ui below it, plus the app.utils helpers it already used; it
knows nothing
about any other dialog except where noted.
"""
from __future__ import annotations

import tkinter as tk
from tkinter import ttk, messagebox
from app.ui.base import ThemedModal, Tooltip
from app.ui.theme import COLORS, F, TAB_ACCENTS, _hex_lerp
from app.ui.widgets import AnimatedButton, AnimatedProgressBar


class DnsTesterDialog(ThemedModal):
    """Find My Fastest DNS (user-approved feature 5, 2026-09): one click
    tests the well-known gaming-friendly resolvers plus the machine's
    current DNS when publicly testable (router IPs are held back with an
    honesty footnote — timing them can only fake-alarm) and shows which
    is fastest FOR THIS connection.

    The user picks ANY answering server — the fastest is crowned ★ and
    pre-selected, but the choice stays theirs (never forced). Apply runs
    the real gaming_dns task with the pick's (primary, secondary) pair
    through the standard run engine (snapshot + undo + scorecard all
    work). Restore runs the same task's revert, putting back the exact
    pre-apply DNS (DHCP included) on every adapter the snapshot covers.

    Rows stream in live as each resolver answers (parallel probe
    threads, ~1-3s typical); unreachable resolvers show 'no reply'
    instead of a fake number. Closing mid-test stops the probes.
    Chrome matches the themed dialogs (dark Toplevel + dark titlebar,
    transient + modal, Escape closes, centered over the parent).
    """

    def __init__(self, parent, app):
        self.app = app
        self._stop_token = [False]
        self._results = {}        # ip -> ms or None (landed)
        self._expected = 0        # total probes this round
        self._done_n = 0          # landed count (Tk thread only)
        self._finished = False
        self._scan_gen = 0        # round generation (stale-hop guard)
        self._selected_ip = None  # the user's pick (defaults to fastest)
        self._row_widgets = {}    # ip -> {"row","ms","badge","pick","name","current"}
        self._watchdog_after = None

        super().__init__(parent, title="Find My Fastest DNS",
                         accent=TAB_ACCENTS["Tweak"])
        dlg = self._dlg
        body = self.body

        # plain-language intro (title row carries the accent dot + name)
        # F09: honest methodology — TCP:53 handshake ranking (stdlib-only),
        # a good proxy for, but not a real UDP DNS query.
        tk.Label(body, text="Tests which DNS server responds fastest for YOUR "
                            "connection (TCP handshake to port 53 — a close "
                            "proxy for real DNS speed) — then applies it.",
                 font=(F, 9), bg=COLORS["bg"],
                 fg=COLORS["subtext"], wraplength=680,
                 justify="left").pack(anchor="w")

        # progress (indeterminate — probe count is known but per-probe
        # latency isn't; the honest signal is rows landing below)
        self._bar = AnimatedProgressBar(body, accent=TAB_ACCENTS["Tweak"])
        self._bar.pack(fill="x", pady=(10, 0))
        self._bar.set_indeterminate(True)
        self._status_lbl = tk.Label(body, text="Starting…", font=(F, 9),
                                    bg=COLORS["bg"], fg=COLORS["subtext"],
                                    anchor="w")
        self._status_lbl.pack(fill="x", pady=(2, 0))

        tk.Frame(body, bg=COLORS["hairline"], height=1).pack(
            fill="x", pady=(10, 4))
        self._rows_body = tk.Frame(body, bg=COLORS["bg"])
        self._rows_body.pack(fill="x")
        tk.Frame(body, bg=COLORS["hairline"], height=1).pack(
            fill="x", pady=(4, 0))

        # Custom DNS (user-requested): test/apply a provider that isn't
        # in the built-in list — some players run a household/VPN
        # resolver, or just want to try one before committing. Primary
        # is required; secondary is optional (falls back to primary-only
        # if left blank, same as any single-address custom host).
        custom_row = tk.Frame(body, bg=COLORS["bg"])
        custom_row.pack(fill="x", pady=(6, 0))
        tk.Label(custom_row, text="Custom:", font=(F, 9),
                 bg=COLORS["bg"], fg=COLORS["subtext"]).pack(side="left")
        self._custom_primary_entry = tk.Entry(
            custom_row, bg=COLORS["surface"], fg=COLORS["text"],
            insertbackground=COLORS["text"], font=(F, 9), bd=0,
            highlightthickness=1, highlightbackground=COLORS["hairline"],
            width=15)
        self._custom_primary_entry.pack(side="left", padx=(6, 0))
        Tooltip(self._custom_primary_entry, "Primary DNS IP (required)")
        self._custom_secondary_entry = tk.Entry(
            custom_row, bg=COLORS["surface"], fg=COLORS["text"],
            insertbackground=COLORS["text"], font=(F, 9), bd=0,
            highlightthickness=1, highlightbackground=COLORS["hairline"],
            width=15)
        self._custom_secondary_entry.pack(side="left", padx=(4, 0))
        Tooltip(self._custom_secondary_entry, "Secondary DNS IP (optional)")
        AnimatedButton(custom_row, text="Add & Test",
                       command=self._add_custom_dns,
                       bg=COLORS["surface"], fg=COLORS["text"],
                       font=(F, 9, "bold"), padx=12,
                       pady=4).pack(side="left", padx=(6, 0))
        for _e in (self._custom_primary_entry, self._custom_secondary_entry):
            try:
                _e.bind("<Return>", lambda _ev: self._add_custom_dns())
            except Exception:
                pass
        # per-ip (primary, secondary) overrides for custom entries whose
        # secondary the built-in DNS_RESOLVERS table has no way to know
        self._custom_pairs = {}

        # buttons (Apply stays a bare verb — the status line + the
        # highlighted row say WHAT it will apply; Restore reverts both
        # primary + secondary to the pre-apply snapshot via the real
        # gaming_dns revert, same engine as Tweak-tab Undo)
        brow = tk.Frame(body, bg=COLORS["bg"])
        brow.pack(fill="x", pady=(12, 6))
        self._apply_btn = AnimatedButton(
            brow, text="Apply", command=self._apply_selected,
            bg=TAB_ACCENTS["Tweak"], fg=COLORS["black"],
            font=(F, 9, "bold"), padx=18, pady=7)
        self._apply_btn.pack(side="right")
        self._apply_btn.set_enabled(False)
        AnimatedButton(brow, text="Re-test", command=self._start_test,
                       bg=COLORS["surface"], fg=COLORS["text"],
                       font=(F, 9, "bold"), padx=18,
                       pady=7).pack(side="right", padx=(0, 8))
        self._restore_btn = AnimatedButton(
            brow, text="Restore", command=self._restore_default,
            bg=COLORS["surface"], fg=COLORS["text"],
            font=(F, 9, "bold"), padx=18, pady=7)
        self._restore_btn.pack(side="left")
        Tooltip(self._restore_btn, "Restores your DNS to default")

        # closing cancels probes + the watchdog (base close hook)
        self.on_close(self._cancel_probes)
        self._start_test()

    def _cancel_probes(self):
        try:
            self._stop_token[0] = True
        except Exception:
            pass
        try:
            if self._watchdog_after is not None:
                self._dlg.after_cancel(self._watchdog_after)
        except Exception:
            pass
        self._watchdog_after = None
        try:
            self._bar.set_indeterminate(False)
        except Exception:
            pass

    # ---- test orchestration -------------------------------------------- #

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

    def _start_test(self):
        try:
            self._stop_token[0] = True
        except Exception:
            pass
        token = self._stop_token = [False]
        self._results = {}
        self._done_n = 0
        self._finished = False
        self._selected_ip = None
        # round generation: every hop below carries THIS round's gen so a
        # previous round's late prober can never corrupt the new round's
        # counts (rapid Re-test used to mix results across rounds)
        self._scan_gen = getattr(self, "_scan_gen", 0) + 1
        gen = self._scan_gen
        try:
            if self._watchdog_after is not None:
                self._dlg.after_cancel(self._watchdog_after)
        except Exception:
            pass
        self._watchdog_after = None
        for w in list(self._rows_body.winfo_children()):
            try:
                w.destroy()
            except Exception:
                pass
        self._row_widgets = {}
        try:
            self._apply_btn.set_enabled(False)
            self._apply_btn.config_text("Apply")
            self._bar.set_indeterminate(True)
            self._set_status("Reading your current DNS…")
        except Exception:
            pass
        try:
            import threading as _th
            _th.Thread(target=self._test_worker, args=(token, gen),
                       daemon=True).start()
        except Exception:
            pass
        # watchdog: finish with whatever landed (a hung prober must never
        # wedge the dialog — every probe is socket-timeout bounded, this
        # is belt and suspenders). Carries the round gen so a previous
        # round's watchdog can't finish the new round early.
        try:
            self._watchdog_after = self._dlg.after(
                25000, lambda _g=gen: self._finish(stalled=True, _gen=_g))
        except Exception:
            pass

    def _test_worker(self, token, gen):
        def dead():
            # F10: token-only — close handlers already set token on the Tk
            # thread; winfo_exists here burned ~1s off-thread per call.
            try:
                return bool(token[0])
            except Exception:
                return True

        try:
            from app.dns_test import (DNS_RESOLVERS, build_targets,
                                      get_current_dns, time_resolver)
        except Exception:
            self._ui(self._set_status, "DNS tester unavailable.")
            self._ui(self._finish, False, gen)
            return
        if dead():
            return
        current = []
        try:
            current = get_current_dns()
        except Exception:
            current = []
        if dead():
            return
        # rows: public resolvers always; the machine's current DNS only
        # when publicly testable (build_targets holds router IPs back —
        # timing them can only produce a misleading 'no reply').
        try:
            targets, skipped = build_targets(current)
        except Exception:
            targets, skipped = ([(n, ip, False) for n, ip, _s in DNS_RESOLVERS], [])
        self._expected = len(targets)
        self._ui(self._build_rows, targets, skipped, gen)
        import threading as _th

        def _probe(name, ip):
            if dead():
                return
            try:
                ms = time_resolver(ip, cancelled=lambda: dead())
            except Exception:
                ms = None
            if dead():
                return
            self._ui(self._paint_result, ip, ms, gen)

        for _name, _ip, _cur in targets:
            if dead():
                return
            _th.Thread(target=_probe, args=(_name, _ip),
                       daemon=True).start()

    # ---- paints (Tk thread only) ---------------------------------------- #

    def _set_status(self, text, error=False):
        try:
            if self._status_lbl.winfo_exists():
                self._status_lbl.config(
                    text=text,
                    fg=COLORS["accent_red"] if error else COLORS["subtext"])
        except Exception:
            pass

    def _pair_for(self, ip):
        """(primary, secondary) for a row — a user-typed custom secondary
        (self._custom_pairs) wins over the built-in DNS_RESOLVERS lookup,
        so Add & Test / Apply always use exactly what was entered."""
        custom = (getattr(self, "_custom_pairs", None) or {}).get(ip)
        if custom:
            return (ip, custom)
        try:
            from app.dns_test import get_dns_pair as _pair
            return _pair(ip)
        except Exception:
            return (ip, ip)

    def _add_row(self, name, ip, is_current):
        """Build one server row (shared by the built-in list and a
        user-added Custom entry) and register it in self._row_widgets."""
        row = tk.Frame(self._rows_body, bg=COLORS["bg_alt"])
        try:
            _pri, _sec = self._pair_for(ip)
            _pair_txt = f"{_pri} / {_sec}" if _sec != _pri else _pri
        except Exception:
            _pair_txt = ip
        title = f"{name}  {_pair_txt}" + ("  (current)" if is_current else "")
        name_lbl = tk.Label(row, text=title, font=(F, 9, "bold"),
                            bg=COLORS["bg_alt"], fg=COLORS["text"],
                            anchor="w")
        name_lbl.pack(side="left", padx=(8, 0), pady=6)
        badge = tk.Label(row, text="", font=(F, 8, "bold"),
                         bg=COLORS["bg_alt"], fg=COLORS["accent_green"])
        badge.pack(side="left", padx=(6, 0))
        pick = tk.Label(row, text="", font=(F, 10, "bold"),
                        bg=COLORS["bg_alt"], fg=TAB_ACCENTS["Tweak"])
        pick.pack(side="left", padx=(6, 0))
        ms = tk.Label(row, text="…", font=(F, 9, "bold"),
                      bg=COLORS["bg_alt"], fg=COLORS["subtext"])
        ms.pack(side="right", padx=(0, 8))
        self._row_widgets[ip] = {"row": row, "ms": ms, "badge": badge,
                                 "pick": pick, "name": name,
                                 "current": is_current}
        # rows become clickable once they answer (unanswered rows
        # ignore clicks — picking a dead server can only fail Apply)
        for w in (row, name_lbl, badge, pick, ms):
            try:
                w.bind("<Button-1>",
                       lambda e, _ip=ip: self._select_ip(_ip), add="+")
            except Exception:
                pass
        row.pack(fill="x", pady=2)
        return row

    def _build_rows(self, targets, skipped_current=None, _gen=None):
        # stale-round guard: a previous round's late hop must never
        # rebuild the current round's rows (rapid Re-test races it)
        if _gen is not None and _gen != getattr(self, "_scan_gen", _gen):
            return
        for w in list(self._rows_body.winfo_children()):
            try:
                w.destroy()
            except Exception:
                pass
        self._row_widgets = {}
        self._selected_ip = None
        self._custom_pairs = {}
        try:
            self._apply_btn.set_enabled(False)
            self._apply_btn.config_text("Apply")
        except Exception:
            pass
        for name, ip, is_current in targets:
            self._add_row(name, ip, is_current)
        # (user call 2026-09: the router-DNS footnote is gone — held-back
        # router IPs are simply absent from the list, no explanation owed)
        try:
            self._set_status(f"Testing {len(targets)} servers…")
        except Exception:
            pass

    def _probe_custom(self, ip, gen):
        """Time one user-added custom IP — same TCP:53 probe as the
        built-in rows, just launched outside the initial round's batch
        so Add & Test can fire any time without disturbing it."""
        def dead():
            try:
                return bool(self._stop_token[0])
            except Exception:
                return True

        def _run():
            if dead():
                return
            try:
                from app.dns_test import time_resolver
                ms = time_resolver(ip, cancelled=dead)
            except Exception:
                ms = None
            if dead():
                return
            self._ui(self._paint_result, ip, ms, gen)

        try:
            import threading as _th
            _th.Thread(target=_run, daemon=True).start()
        except Exception:
            pass

    def _add_custom_dns(self):
        """Add & Test: validate the typed primary/secondary, add a row
        for it (reusing the same click-to-select / Apply / Restore flow
        every built-in row already has), and start its probe."""
        try:
            from app.dns_test import is_valid_ipv4
        except Exception:
            return
        try:
            primary = (self._custom_primary_entry.get() or "").strip()
            secondary = (self._custom_secondary_entry.get() or "").strip()
            if not is_valid_ipv4(primary):
                self._set_status(
                    "Enter a valid primary DNS IP (e.g. 9.9.9.9).", error=True)
                return
            if secondary and not is_valid_ipv4(secondary):
                self._set_status(
                    "Secondary IP doesn't look valid — leave it blank to skip.",
                    error=True)
                return
            if primary in (self._row_widgets or {}):
                self._set_status(f"{primary} is already in the list above.",
                                 error=True)
                return
            if secondary:
                self._custom_pairs = getattr(self, "_custom_pairs", None) or {}
                self._custom_pairs[primary] = secondary
            self._add_row("Custom", primary, False)
            self._expected = getattr(self, "_expected", 0) + 1
            gen = getattr(self, "_scan_gen", None)
            try:
                self._custom_primary_entry.delete(0, "end")
                self._custom_secondary_entry.delete(0, "end")
            except Exception:
                pass
            self._set_status(f"Testing {primary}…")
            self._probe_custom(primary, gen)
        except Exception:
            pass



    def _paint_result(self, ip, ms, _gen=None):
        if _gen is not None and _gen != getattr(self, "_scan_gen", _gen):
            return
        self._results[ip] = ms
        self._done_n += 1
        try:
            handles = self._row_widgets.get(ip)
            if handles is not None and handles["ms"].winfo_exists():
                if ms is None:
                    handles["ms"].config(text="no reply",
                                         fg=COLORS["accent_red"])
                else:
                    handles["ms"].config(text=f"{ms:.0f} ms",
                                         fg=COLORS["text"])
                    # answered rows are pickable — afford the click
                    try:
                        handles["row"].config(cursor="hand2")
                        for _w in handles["row"].winfo_children():
                            _w.config(cursor="hand2")
                    except Exception:
                        pass
            remaining = max(0, self._expected - self._done_n)
            if remaining:
                self._set_status(f"{remaining} server(s) left…")
        except Exception:
            pass
        if self._done_n >= self._expected:
            self._finish()

    @staticmethod
    def _row_tint(selected):
        """Selection language (user redesign): a selected row is TINTED,
        not annotated — accent-mixed bg the eye can't miss, same trick
        as hover states. Deselected rows return to plain bg_alt."""
        if selected:
            return _hex_lerp(COLORS["bg_alt"], TAB_ACCENTS["Tweak"], 0.30)
        return COLORS["bg_alt"]

    def _tint_row(self, ip, selected):
        """Repaint one row (frame + every child label) in/out of the
        selection tint. The old '✓ selected' text label is gone —
        highlight IS the signal now."""
        try:
            handles = (self._row_widgets or {}).get(ip)
            if handles is None:
                return
            bg = self._row_tint(selected)
            row = handles["row"]
            if row.winfo_exists():
                row.config(bg=bg)
                for w in row.winfo_children():
                    try:
                        w.config(bg=bg)
                    except Exception:
                        pass
                # the pick dot glyph (radio-style ●) rides the name label
                if selected:
                    handles["pick"].config(text="●")
                else:
                    handles["pick"].config(text="")
        except Exception:
            pass

    def _select_ip(self, ip):
        """Pick a server row (user choice — the fastest is only the
        default). Unanswered rows ignore clicks: picking a dead server
        could only fail Apply. Picking the already-current server shows
        an honest 'nothing to change' state instead of a no-op Apply.
        The status names the full (primary / secondary) pair Apply
        will set, so dual-DNS is visible before the click."""
        try:
            if self._results.get(ip) is None:
                return
            self._selected_ip = ip
            for _ip in (self._row_widgets or {}):
                self._tint_row(_ip, _ip == ip)
            handles = (self._row_widgets or {}).get(ip, {})
            name = handles.get("name", ip)
            try:
                _pri, _sec = self._pair_for(ip)
                _pair_txt = f"{_pri} / {_sec}" if _sec != _pri else _pri
            except Exception:
                _pair_txt = ip
            try:
                wms = self._results.get(ip)
                if handles.get("current"):
                    self._set_status(f"You're already using {name} "
                                     f"({wms:.0f} ms) — nothing to change.")
                    self._apply_btn.set_enabled(False)
                else:
                    self._set_status(f"Selected: {name} ({_pair_txt}, {wms:.0f} ms).")
                    self._apply_btn.set_enabled(True)
            except Exception:
                pass
        except Exception:
            pass

    def _finish(self, stalled=False, _gen=None):
        if self._finished:
            return
        # stale-round guard (see _build_rows): only the current round
        # may finish the dialog
        if _gen is not None and _gen != getattr(self, "_scan_gen", _gen):
            return
        self._finished = True
        try:
            if self._watchdog_after is not None:
                self._dlg.after_cancel(self._watchdog_after)
        except Exception:
            pass
        self._watchdog_after = None
        try:
            self._bar.set_indeterminate(False)
            self._bar.set_fraction(1.0)
        except Exception:
            pass
        try:
            from app.dns_test import pick_winner
            winner = pick_winner(self._results)
        except Exception:
            winner = None
        if not winner:
            self._set_status("Couldn't reach any DNS server — check your connection, then Re-test."
                             if not stalled else
                             "Timed out waiting for replies — check your connection, then Re-test.")
            return
        # crown the winner row (fastest answers first — the default pick)
        try:
            handles = self._row_widgets.get(winner)
            if handles is not None and handles["badge"].winfo_exists():
                handles["badge"].config(text="★ fastest")
        except Exception:
            pass
        # default-select the winner; the user may still pick any other
        # answering server (their choice is never forced)
        try:
            self._select_ip(winner)
        except Exception:
            pass

    # ---- actions ---------------------------------------------------------- #

    def _apply_selected(self):
        """Close, then run the REAL gaming_dns task with the pick's
        (primary, secondary) pair through the standard engine
        (preflight, admin gate, snapshot/undo, scorecard).
        dataclasses.replace keeps the applied key 'gaming_dns' so
        badges, Undo and Tweak Health all work."""
        try:
            winner = self._selected_ip
            if not winner or self._results.get(winner) is None:
                return
            primary, secondary = self._pair_for(winner)
        except Exception:
            return
        try:
            from app.tab_presets import TABS as _TABS
            task = next(t for t in _TABS.get("Tweak", []) if t.key == "gaming_dns")
        except Exception:
            return
        try:
            import dataclasses as _dc

            def _run(ctx, _w=primary, _s=secondary):
                from app.tasks.tweak_tasks import apply_gaming_dns as _apply
                return _apply(ctx, servers=(_w, _s))

            winner_task = _dc.replace(task, run=_run)
        except Exception:
            return
        self.close()
        try:
            self.app.run_tasks("Tweak", [winner_task], mode="run")
        except Exception:
            pass

    def _restore_default(self):
        """Close, then run the REAL gaming_dns revert through the
        standard engine — restores each snapshotted adapter's exact
        prior DNS (static pair or DHCP via -ResetServerAddresses),
        so both primary + secondary go back. Same key/mode as
        Tweak-tab Undo; with no snapshot the revert reports honestly
        instead of guessing."""
        try:
            from app.tab_presets import TABS as _TABS
            task = next(t for t in _TABS.get("Tweak", []) if t.key == "gaming_dns")
        except Exception:
            return
        if getattr(task, "revert", None) is None:
            return
        self.close()
        try:
            self.app.run_tasks("Tweak", [task], mode="revert")
        except Exception:
            pass
