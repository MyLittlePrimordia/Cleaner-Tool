"""ScorecardDialog — extracted verbatim from app/gui.py (H12, batch 1).

One dialog per module, mirroring app/ui/widgets. Depends only on
app.ui below it, plus the app.utils helpers it already used; it
knows nothing
about any other dialog except where noted.
"""
from __future__ import annotations

import tkinter as tk
from app.ui.base import ThemedModal, Tooltip
from app.ui.hardware.drives import _drive_gains
from app.ui.theme import (
    COLORS, TAB_ACCENTS, TYPE, _hex_lerp, _round_rect_points, SPACE_1, SPACE_2,
    SPACE_3,
)
from app.ui.metrics import GROUP_GAP, HAIRLINE_NUDGE, INDENT, TEXT_NUDGE, TIGHT_GAP
from app.ui.widgets import EmptyState, ModalFooter, ScrollableRoundedPanel
from app.benchmark import compare as _measure_compare
from app.changes import change_detail, collapse, counts_text
from app.utils import format_bytes


class ScorecardDialog(ThemedModal):
    """Themed end-of-run results card (user-approved feature 2026-09).

    Replaces the old generic 'Done' messagebox after Clean / Repair /
    Tweak / Undo runs. One glance answers "what just happened to my
    PC?":
      * hero number — GB freed, big and green (runs that freed space)
      * before/after free-space bar for C: (three rounded segments:
        still used / freed this run / was already free — Animated-
        ProgressBar-style geometry)
      * status chips — done / skipped / failed (+ stopped early)
      * one plain-language line per task, status-colored
      * amber 'Restart needed' banner when a task flags REBOOT REQUIRED
      * 'Clean Again' re-runs the exact same task list (completed
        Clean-tab runs only — user-approved spec)

    Chrome matches the themed dialogs exactly: dark Toplevel + dark
    titlebar, transient + modal (wait()), Escape/Return to close,
    centered over the parent, AnimatedButton pills, accent dot title
    row. Per-task rows beyond _ROWS_INLINE flow into a rounded scroll
    panel whose floating scrollbar auto-hides when everything fits
    (only deep presets ever overflow).

    The modal wait() is split from __init__ on purpose so the smoke
    harness can assert on the built dialog (refs _dlg/_hero_lbl/
    _bar_canvas/_rows_panel/_reboot_banner/_counts) without entering
    the nested event loop.
    """

    _ROWS_INLINE = 12    # beyond this the row list scrolls
    _ROW_H = 26          # per-row height budget for the panel cap
    _CHANGE_ROWS = 8     # named change lines before "and N more"
    _ROW_CHANGE_LINES = 4  # change lines revealed under one task row

    def __init__(self, parent, *, tab_name, mode, cancelled, results,
                 total_bytes, before=None, after=None, needs_reboot=False,
                 on_run_again=None, ok_text="Close",
                 before_drives=None, after_drives=None, measurements=None,
                 changes=(), accounted=None):
        self._run_again_cb = on_run_again
        # N-010: removed-vs-regained, reconciled by the run worker from the
        # same two snapshots this dialog draws. None (older callers, smoke
        # fixtures) means "not reported", never "verified" - the card simply
        # has nothing to explain.
        self._accounted = accounted
        # Feature 3 (Copy results) needs the raw numbers after the widgets
        # are built, so they are kept on the instance rather than living
        # only as locals inside this constructor.
        self._tab_name = tab_name
        self._mode = mode
        self._cancelled = bool(cancelled)
        self._total_bytes = total_bytes or 0
        # Audit fix (Scorecard "why failed"): results grew a 4th element
        # (a short failure reason); R-005 adds a 5th (the task's own typed
        # changes). Existing callers - including several smoke-test fixtures -
        # still pass 3- and 4-tuples. Normalize once here instead of requiring
        # every caller to know about the new shape.
        results = [tuple(r) + (None,) * (5 - len(r)) if len(r) < 5 else tuple(r)
                   for r in (results or [])]
        self._results = list(results)
        self._needs_reboot = bool(needs_reboot)
        self._gains = _drive_gains(before_drives, after_drives)
        # N-002: (before, after) lightweight readings, or None. Rendered below
        # the chips; an empty compare() means "no measurable change".
        self._measurements = measurements
        # R-005: typed records the write layer collected during the run. One
        # row per (kind, target), first prior -> last target; a no-op run
        # records nothing and the whole block stays absent.
        self._changes = collapse(list(changes or ()))
        tab_accent = TAB_ACCENTS.get(tab_name, COLORS["accent_green"])
        if cancelled:
            self._title = ("Undo Stopped" if mode == "revert"
                           else f"{tab_name} Run Stopped")
            self._title_accent = COLORS["accent_yellow"]
        else:
            self._title = ("Undo Complete" if mode == "revert"
                           else f"{tab_name} Complete")
            self._title_accent = (COLORS["accent_green"] if mode == "revert"
                                  else tab_accent)

        # counts straight from the per-task results (never from stale
        # counters — the dialog owns its own truth)
        ok_n = sum(1 for _l, s, _b, _r, _c in results if s == "ok")
        skip_n = sum(1 for _l, s, _b, _r, _c in results if s == "skip")
        fail_n = sum(1 for _l, s, _b, _r, _c in results if s == "fail")
        stop_n = sum(1 for _l, s, _b, _r, _c in results if s == "stop")
        self._counts = (ok_n, skip_n, fail_n, stop_n)

        super().__init__(parent, title=self._title,
                         accent=self._title_accent)
        dlg = self._dlg
        body = self.body

        # hero number — freed space, the emotional payoff (skipped when
        # the run freed nothing: no fake hero, the chips tell the story)
        self._hero_lbl = None
        if total_bytes and total_bytes > 0:
            hero = tk.Frame(body, bg=COLORS["bg"])
            hero.pack(fill="x", pady=(TEXT_NUDGE, 0))
            self._hero_lbl = tk.Label(hero, text=format_bytes(total_bytes),
                                      font=TYPE["metric_large"], bg=COLORS["bg"],
                                      fg=COLORS["accent_green"])
            self._hero_lbl.pack()
            tk.Label(hero, text="disk space freed", font=TYPE["body_medium"],
                     bg=COLORS["bg"], fg=COLORS["subtext"]).pack()

        # before/after free-space bar — only when the run freed space AND
        # both C: snapshots were captured (honest: no invented numbers)
        self._bar_canvas = None
        if (total_bytes and total_bytes > 0 and before and after
                and before[1] and after[1]):
            b_avail = before[0] or 0
            a_avail, a_total = (after[0] or 0), after[1]
            if a_avail >= b_avail and a_total > 0:
                bar_wrap = tk.Frame(body, bg=COLORS["bg"])
                bar_wrap.pack(fill="x", pady=(SPACE_2, 0))
                tk.Label(bar_wrap,
                         text=f"C:  {format_bytes(b_avail)}  →  {format_bytes(a_avail)} free",
                         font=TYPE["label_medium"], bg=COLORS["bg"],
                         fg=COLORS["text"]).pack(anchor="w")
                self._bar_canvas = tk.Canvas(bar_wrap, height=16,
                                             highlightthickness=0, bd=0,
                                             bg=COLORS["bg"])
                self._bar_canvas.pack(fill="x", pady=(SPACE_1, 0))
                self._bar_canvas.bind(
                    "<Configure>",
                    lambda e, b=b_avail, a=a_avail, t=a_total:
                        self._draw_bar(self._bar_canvas, b, a, t))
                # draw once at the known packed width — a dialog whose
                # parent is withdrawn (headless harness) never receives
                # <Configure>, and _draw_bar falls back to a 16px height
                # when the canvas is still unmapped, so this initial pass
                # is safe in both environments. A later real <Configure>
                # just redraws at the true width.
                self._draw_bar(self._bar_canvas, b_avail, a_avail, a_total,
                               width=self._modal_w - 36)

        # N-010: the sentence that makes the number above checkable. Shown
        # only when there is something to explain (the run removed bytes and
        # the disk disagreed, or free space could not be read at all), so a
        # normal run's card gains no line.
        self._verified_lbl = None
        try:
            _note = accounted.note() if accounted is not None else ""
        except Exception:
            _note = ""
        if _note:
            self._verified_lbl = tk.Label(
                body, text=_note, font=TYPE["caption"], bg=COLORS["bg"],
                fg=COLORS["subtext"], anchor="w", justify="left",
                wraplength=self.body_width(26))
            self._verified_lbl.pack(fill="x", pady=(SPACE_2, 0))

        # status chips
        chips = tk.Frame(body, bg=COLORS["bg"])
        chips.pack(fill="x", pady=(GROUP_GAP, 0))
        tk.Label(chips, text=f"✓  {ok_n} done", font=TYPE["label_large"],
                 bg=COLORS["bg"], fg=COLORS["accent_green"]).pack(side="left")
        if skip_n:
            tk.Label(chips, text=f"⊘  {skip_n} skipped", font=TYPE["label_large"],
                     bg=COLORS["bg"], fg=COLORS["subtext"]).pack(side="left", padx=(SPACE_3, 0))
        if fail_n:
            tk.Label(chips, text=f"✕  {fail_n} failed", font=TYPE["label_large"],
                     bg=COLORS["bg"], fg=COLORS["accent_red"]).pack(side="left", padx=(SPACE_3, 0))
        if stop_n:
            tk.Label(chips, text="◦  stopped early", font=TYPE["label_large"],
                     bg=COLORS["bg"], fg=COLORS["accent_yellow"]).pack(side="left", padx=(SPACE_3, 0))

        # R-005: the run's own typed change list, before the measured block.
        self._change_rows = None
        self._change_lbl = None
        self._render_changes(body)

        # N-002: what else moved besides disk space (memory / restart flag).
        # Nothing is shown when the readings never happened, and "no
        # measurable change" is stated plainly rather than faked as a win.
        self._measure_rows = None
        self._measure_lbl = None
        self._render_measurements(body)

        # per-drive breakdown (feature 4) — only when MORE THAN ONE drive
        # actually gained space. One drive is the normal case and the hero
        # number already says it; listing "C: +2.1 GB" under a "2.1 GB
        # freed" headline would just be the same fact twice.
        self._drive_rows = None
        if len(self._gains) > 1:
            drow = self._drive_rows = tk.Frame(body, bg=COLORS["bg"])
            drow.pack(fill="x", pady=(SPACE_2, 0))
            tk.Label(drow, text="Space freed on each drive", font=TYPE["label_medium"],
                     bg=COLORS["bg"], fg=COLORS["subtext"]).pack(anchor="w")
            line = tk.Frame(drow, bg=COLORS["bg"])
            line.pack(fill="x", pady=(TIGHT_GAP, 0))
            for root, gained in self._gains[:6]:
                chip = tk.Frame(line, bg=COLORS["bg_alt"])
                chip.pack(side="left", padx=(0, SPACE_2))
                tk.Label(chip, text=str(root).rstrip("\\"), font=TYPE["label_medium"],
                         bg=COLORS["bg_alt"], fg=COLORS["text"]).pack(
                             side="left", padx=(SPACE_2, SPACE_1), pady=TIGHT_GAP)
                tk.Label(chip, text="+" + format_bytes(gained), font=TYPE["label_medium"],
                         bg=COLORS["bg_alt"],
                         fg=COLORS["accent_green"]).pack(
                             side="left", padx=(0, SPACE_2), pady=TIGHT_GAP)

        # H-011: ONE read of the ledger per card. The month line, the link and
        # the viewer all work from this list instead of each re-loading (and
        # re-copying) the whole config on the Tk thread.
        self._history_runs = self._load_history_runs()

        # "How is my PC doing?" (feature 10) — one friendly sentence, and
        # only once there is more than this single run to talk about.
        # Nothing to brag about yet = no line, rather than "freed 0 GB".
        self._month_lbl = None
        month_text = self._month_summary()
        if month_text:
            self._month_lbl = tk.Label(body, text=month_text, font=TYPE["caption"],
                                       bg=COLORS["bg"], fg=COLORS["subtext"])
            self._month_lbl.pack(pady=(SPACE_2, 0))

        # R-005: the ledger outlives the card now, so offer the one place to
        # look back at earlier runs. Only when there is something to show.
        self._history_lbl = None
        if self._history_available():
            self._history_lbl = tk.Label(body, text="View past runs \u203a",
                                         font=TYPE["caption"], bg=COLORS["bg"],
                                         fg=self._title_accent, cursor="hand2",
                                         takefocus=1)
            self._history_lbl.pack(pady=(SPACE_1, 0))
            self._history_lbl.bind("<Button-1>",
                                   lambda _e: self._open_past_runs())
            # I-008: the link is the only way into the ledger, so it is a Tab
            # stop with Return/Space as well as a click target. Both return
            # "break": the card binds Return to Close, and opening the viewer
            # must not also dismiss the card underneath it.
            def _open_from_keyboard(_event=None):
                self._open_past_runs()
                return "break"

            self._history_lbl.bind("<Return>", _open_from_keyboard)
            self._history_lbl.bind("<space>", _open_from_keyboard)

        # hairline separator
        tk.Frame(body, bg=COLORS["hairline"], height=1).pack(fill="x", pady=(GROUP_GAP, SPACE_1))

        # per-task rows — rounded panel capped at _ROWS_INLINE rows tall;
        # the floating scrollbar only appears when content overflows
        #
        # BUTTONS ARE PACKED FIRST, side="bottom", deliberately out of visual
        # order. ThemedModal mirrors the main window's geometry, so this card
        # is exactly as tall as the app window — and the body below (hero,
        # the before/after bar, the chips, the 30-day line) already spends
        # most of it. A long task list used to push the button row off the
        # bottom edge, leaving three half-drawn buttons the user could not
        # click (reported from a run with 8 tasks).
        #
        # In pack, side="bottom" reserves that space FIRST, and the rows
        # panel is then the only widget that can give ground — it shrinks and
        # its own scrollbar takes over. Packing it in visual order meant the
        # panel claimed its full height and the buttons lost.
        self._footer = footer = ModalFooter(body, accent=self._title_accent)
        footer.pack_footer()

        n_rows = len(results)
        panel_h = max(52, min(n_rows, self._ROWS_INLINE) * self._ROW_H + 16)
        self._rows_panel = panel = ScrollableRoundedPanel(body)
        panel.config(height=panel_h)   # after ctor (its Canvas init pins height=10)
        # fill="both" + expand: absorb whatever the body has left, and give
        # space back when the task list is short
        panel.pack(fill="both", expand=True, pady=(TEXT_NUDGE, 0))
        if not results:
            EmptyState(panel.inner, glyph="\u25cb", text="No tasks ran.",
                       detail="Nothing was selected, so nothing changed.",
                       bg=COLORS["bg_alt"],
                       wrap=max(240, self.body_width(26))).pack(fill="x")
        for label, status, nbytes, reason, changes in results:
            self._build_row(panel.inner, label, status, nbytes, reason, changes)
        # settle scroll metrics NOW (ctor-time) so scroll_enabled is
        # correct even before the canvas ever maps — the harness asserts
        # on it without pumping Configure events
        try:
            panel.refresh_scroll()
        except Exception:
            pass

        # reboot banner — amber, same tinted-banner pattern as Undo's
        self._reboot_banner = None
        if needs_reboot:
            banner = self._reboot_banner = tk.Frame(body, bg=COLORS["alert_amber_bg"])
            tk.Label(banner,
                     text="⚠  Restart needed — some changes only take effect after a reboot.",
                     font=TYPE["label_medium"], bg=COLORS["alert_amber_bg"],
                     fg=COLORS["accent_yellow"]).pack(anchor="w", padx=GROUP_GAP, pady=SPACE_2)
            banner.pack(fill="x", pady=(GROUP_GAP, 0))

        # buttons — Close primary (accent fill), the two secondary actions
        # to its left. The shared footer packs the primary first, so a
        # secondary that disappears (no "Clean Again" on an Undo run) cannot
        # move Close.
        footer.button(ok_text, self.close, kind="primary")
        # Feature 3: plain-English summary straight to the clipboard, so a
        # user can paste it into a Discord help thread without screenshots.
        self._copy_btn = footer.button("Copy results", self._copy_results,
                                       kind="secondary")
        if self._run_again_cb is not None:
            footer.button("Clean Again", self._run_again, kind="secondary")

        # Return mirrors Close (single primary action on this card)
        dlg.bind("<Return>", lambda _e: self.close())

    # ---- rows ---------------------------------------------------------- #

    @staticmethod
    def _glyphs(status):
        return {
            "ok": ("✓", COLORS["accent_green"]),
            "skip": ("⊘", COLORS["subtext"]),
            "fail": ("✕", COLORS["accent_red"]),
            "stop": ("◦", COLORS["accent_yellow"]),
        }.get(status, ("•", COLORS["subtext"]))

    def _build_row(self, parent, label, status, nbytes, reason=None,
                   changes=()):
        """One compact one-liner: status glyph | task label (+ plain-
        language suffix for skip/fail/stop) | bytes freed (right). Same
        bg_alt row look as the preset summary cells.

        Audit fix ("why failed"): a failed row used to just say "— failed
        (see log)" — technically true but meant re-opening the text log
        and hunting for the matching line to find out what actually went
        wrong. The real reason was already being captured (and logged)
        right at the point of failure; it just never rode along into the
        Scorecard's own results. Show a short version inline (tooltip
        carries the full text — some exception messages run long).

        R-005: a row whose task changed something can be clicked to reveal
        THAT task's own change lines, instead of leaving the run-wide list as
        the only detail. The affordance shows the count so a row is never a
        dead click; the detail panel is packed after the row on demand, which
        keeps a quiet run's card exactly as compact as it was."""
        glyph, gcolor = self._glyphs(status)
        row = tk.Frame(parent, bg=COLORS["bg_alt"])
        glyph_lbl = tk.Label(row, text=glyph, font=TYPE["label_medium"],
                             bg=COLORS["bg_alt"], fg=gcolor, width=2,
                             anchor="w")
        glyph_lbl.pack(side="left")
        text, fg = label, COLORS["text"]
        full_reason = None
        if status == "skip":
            text += "  — nothing to do"
            fg = COLORS["subtext"]
            full_reason = reason
        elif status == "fail":
            if reason:
                short = reason if len(reason) <= 60 else reason[:57] + "…"
                text += f"  — {short}"
                full_reason = reason
            else:
                text += "  — failed (see log)"
        elif status == "stop":
            text += "  — stopped"
            fg = COLORS["subtext"]
        lbl = tk.Label(row, text=text, font=TYPE["caption"], bg=COLORS["bg_alt"], fg=fg,
                       anchor="w", justify="left",
                       wraplength=self.body_width(28))
        lbl.pack(side="left", padx=(TEXT_NUDGE, 0))
        if full_reason:
            try:
                Tooltip(lbl, full_reason)
            except Exception:
                pass
        if nbytes and nbytes > 0:
            tk.Label(row, text=format_bytes(nbytes), font=TYPE["label_medium"],
                     bg=COLORS["bg_alt"],
                     fg=COLORS["accent_green"]).pack(side="right")
        row.pack(fill="x", pady=HAIRLINE_NUDGE)

        changes = collapse(tuple(changes or ()))
        if changes:
            self._attach_row_changes(parent, row, label, lbl, glyph_lbl, changes)

    def _attach_row_changes(self, parent, row, label, lbl, glyph_lbl, changes):
        """Add the click-to-reveal count and the hidden detail panel to a row."""
        count = len(changes)
        toggle = tk.Label(row, text="\u25b8 %d" % count, font=TYPE["caption"],
                          bg=COLORS["bg_alt"], fg=COLORS["subtext"],
                          cursor="hand2")
        toggle.pack(side="right", padx=(SPACE_2, 0))

        detail = tk.Frame(parent, bg=COLORS["bg_alt"])
        shown = list(changes[:self._ROW_CHANGE_LINES])
        for change in shown:
            tk.Label(detail, text="   %s" % self._change_line(change),
                     font=TYPE["caption"], bg=COLORS["bg_alt"], fg=COLORS["subtext"],
                     anchor="w", justify="left",
                     wraplength=self.body_width(24)).pack(
                         fill="x", padx=(INDENT, SPACE_2))
        extra = count - len(shown)
        if extra > 0:
            tk.Label(detail, text="   \u2026 and %d more" % extra,
                     font=TYPE["caption"], bg=COLORS["bg_alt"],
                     fg=COLORS["subtext"], anchor="w").pack(
                         fill="x", padx=(INDENT, SPACE_2), pady=(0, TEXT_NUDGE))

        state = {"open": False}

        def _toggle(_event=None):
            if state["open"]:
                detail.pack_forget()
            else:
                detail.pack(fill="x", after=row)
            state["open"] = not state["open"]
            toggle.config(text=("\u25be" if state["open"] else "\u25b8")
                          + " %d" % count)
            try:
                self._rows_panel.refresh_scroll()
            except Exception:
                pass

        for widget in (row, lbl, glyph_lbl, toggle):
            widget.bind("<Button-1>", _toggle)
            try:
                widget.config(cursor="hand2")
            except Exception:
                pass
        return detail

    # ---- before/after bar ---------------------------------------------- #

    def _draw_bar(self, canvas, before_avail, after_avail, total, width=None):
        """Three rounded segments proportional to the drive's real
        capacity: still used (muted) / freed this run (bright green) /
        was already free (muted green). Geometry mirrors the segmented
        AnimatedProgressBar so the bar reads as the same design family."""
        try:
            canvas.delete("all")
            w = width if width is not None else canvas.winfo_width()
            if w < 40 or total <= 0:
                return
            gap = 3
            avail = w - gap * 2
            used_after = max(0, total - after_avail)
            freed = max(0, after_avail - before_avail)
            free_before = max(0, min(before_avail, total))
            parts = [
                (used_after / total, COLORS["surface"]),
                (freed / total, COLORS["accent_green"]),
                (free_before / total,
                 _hex_lerp(COLORS["surface"], COLORS["accent_green"], 0.35)),
            ]
            x = 0
            h = canvas.winfo_height()
            if h < 4:                 # not mapped yet (winfo gives 1)
                h = 16
            for i, (frac, color) in enumerate(parts):
                seg_w = int(frac * avail)
                if seg_w <= 0:
                    continue          # a zero-width segment adds no gap
                if i > 0:
                    x += gap
                canvas.create_polygon(
                    _round_rect_points(x, 2, x + seg_w, h - 2, 3),
                    smooth=True, fill=color, outline="")
                x += seg_w
        except Exception:
            pass

    # ---- history (R-005) ------------------------------------------------ #

    @staticmethod
    def _load_history_runs():
        """The ledger, read ONCE per card (H-011). Never raises."""
        try:
            from app import history
            return history.runs()
        except Exception:
            return []

    def _history_available(self) -> bool:
        """True when the runs this card loaded contain at least one entry."""
        return bool(self._history_runs)

    def _open_past_runs(self):
        """Open the history viewer over this card with the list we already
        hold, then re-grab.

        Nested modals are an established pattern here (themed confirms do the
        same); the inner close releases the grab, so the scorecard has to
        take it back or Escape would stop closing it. The viewer hands back
        the one fact that can change while this card is up (it cleared the
        ledger), which is what `_sync_history_lines` acts on.
        """
        cleared = False
        try:
            from app.ui.dialogs.past_runs_dialog import PastRunsDialog
            viewer = PastRunsDialog(self._dlg, entries=self._history_runs)
            viewer.wait()
            cleared = bool(getattr(viewer, "cleared", False))
        except Exception:
            pass
        finally:
            try:
                self._dlg.grab_set()
            except Exception:
                pass
        if cleared:
            self._history_runs = []
            self._sync_history_lines()

    def _sync_history_lines(self):
        """Hide the history lines after the viewer cleared the ledger.

        No config read here: `self._history_runs` is the card's one snapshot,
        and the viewer already reported the only change possible while this
        card is up. They are only ever HIDDEN - the ledger cannot grow until
        the next run's card.
        """
        try:
            if self._month_lbl is not None:
                text = self._month_summary()
                if text:
                    self._month_lbl.config(text=text)
                else:
                    self._month_lbl.pack_forget()
        except Exception:
            pass
        try:
            if self._history_lbl is not None and not self._history_available():
                self._history_lbl.pack_forget()
        except Exception:
            pass

    # ---- typed changes (R-005) ----------------------------------------- #

    @staticmethod
    def _change_line(change) -> str:
        return "%s: %s" % (change.label, change_detail(change))

    def _render_changes(self, body):
        """The run's typed change list, with counts grouped by kind.

        Capped at _CHANGE_ROWS named lines: a privacy-baseline run touches
        dozens of values, and the card's job is the shape of the result rather
        than a registry dump. Copy results keeps every line.
        """
        if not self._changes:
            return
        wrap = tk.Frame(body, bg=COLORS["bg"])
        wrap.pack(fill="x", pady=(GROUP_GAP, 0))
        tk.Label(wrap, text="What changed", font=TYPE["label_medium"],
                 bg=COLORS["bg"], fg=COLORS["subtext"]).pack(anchor="w")
        self._change_lbl = tk.Label(
            wrap, text=counts_text(self._changes), font=TYPE["caption"],
            bg=COLORS["bg"], fg=COLORS["subtext"], anchor="w",
            justify="left", wraplength=self.body_width(26))
        self._change_lbl.pack(anchor="w", pady=(HAIRLINE_NUDGE, TEXT_NUDGE))
        self._change_rows = []
        for change in self._changes[:self._CHANGE_ROWS]:
            line = tk.Frame(wrap, bg=COLORS["bg"])
            line.pack(fill="x", pady=HAIRLINE_NUDGE)
            lbl = tk.Label(line, text=change.label, font=TYPE["caption"],
                           bg=COLORS["bg"], fg=COLORS["text"], anchor="w")
            lbl.pack(side="left")
            try:
                Tooltip(lbl, change.target)
            except Exception:
                pass
            tk.Label(line, text=change_detail(change),
                     font=TYPE["label_medium"], bg=COLORS["bg"],
                     fg=(COLORS["accent_green"] if change.after is not None
                         else COLORS["subtext"])).pack(side="right")
            self._change_rows.append(line)
        extra = len(self._changes) - len(self._change_rows)
        if extra > 0:
            tk.Label(wrap, text="\u2026 and %d more \u2014 Copy results lists "
                     "them all." % extra, font=TYPE["caption"],
                     bg=COLORS["bg"], fg=COLORS["subtext"],
                     anchor="w").pack(anchor="w", pady=(TEXT_NUDGE, 0))

    # ---- before/after readings (N-002) --------------------------------- #

    def _render_measurements(self, body):
        """The memory / restart before/after block, or nothing.

        Silent when no readings were taken (``compare`` returns None) so the
        card never claims a change it could not see; a real "nothing moved"
        is stated as such instead of being left blank."""
        if not self._measurements:
            return
        try:
            before, after = self._measurements
        except (TypeError, ValueError):
            return
        rows = _measure_compare(before, after)
        if rows is None:
            return
        wrap = tk.Frame(body, bg=COLORS["bg"])
        wrap.pack(fill="x", pady=(GROUP_GAP, 0))
        tk.Label(wrap, text="Before \u2192 after", font=TYPE["label_medium"],
                 bg=COLORS["bg"], fg=COLORS["subtext"]).pack(anchor="w")
        if not rows:
            self._measure_lbl = tk.Label(
                wrap, text="No measurable change to memory or restart state.",
                font=TYPE["caption"], bg=COLORS["bg"], fg=COLORS["subtext"],
                anchor="w", justify="left", wraplength=self.body_width(26))
            self._measure_lbl.pack(anchor="w", pady=(TIGHT_GAP, 0))
            return
        tones = {"good": COLORS["accent_green"], "bad": COLORS["accent_red"],
                 "warn": COLORS["accent_yellow"]}
        self._measure_rows = []
        for row in rows:
            line = tk.Frame(wrap, bg=COLORS["bg"])
            line.pack(fill="x", pady=HAIRLINE_NUDGE)
            tk.Label(line, text=row.get("label", ""), font=TYPE["caption"],
                     bg=COLORS["bg"], fg=COLORS["text"],
                     anchor="w").pack(side="left")
            tk.Label(line, text=row.get("text", ""), font=TYPE["label_medium"],
                     bg=COLORS["bg"],
                     fg=tones.get(row.get("tone"), COLORS["text"])).pack(
                         side="right")
            self._measure_rows.append(line)

    # ---- summaries ------------------------------------------------------ #

    def _month_summary(self):
        """Friendly one-liner about the last 30 days, or "" if it would
        not tell the user anything they cannot already see above.

        Deliberately vague about precision ("about 12 GB"): the history
        stores what each run reported, so it is a good running total, not
        an accountant's figure, and the wording should not pretend
        otherwise."""
        try:
            # H-011: the card's own read, through the one window rule.
            from app.history import reclaimed_in
            total, runs = reclaimed_in(self._history_runs, 30)
        except Exception:
            return ""
        # One run in the window IS this run — nothing extra to say.
        if runs < 2 or total <= 0:
            return ""
        # "runs", not "cleans": since R-005 a Tweak/Repair/Undo run is
        # recorded too, so the count is every run in the window.
        return ("You've freed about %s in the last 30 days, across %d runs."
                % (format_bytes(total), runs))

    def _summary_text(self):
        """The plain-English block that Copy results puts on the clipboard."""
        ok_n, skip_n, fail_n, stop_n = self._counts
        lines = ["Cleaner Tool — %s" % self._title]
        if self._total_bytes and self._total_bytes > 0:
            lines.append("Space freed: %s" % format_bytes(self._total_bytes))
        # N-010: the same qualification the card shows, so a pasted report
        # cannot carry a measured-looking number without its caveat.
        try:
            _note = self._accounted.note() if self._accounted is not None else ""
        except Exception:
            _note = ""
        if _note:
            lines.append("  " + _note)
        for root, gained in self._gains:
            lines.append("  %s  +%s free" % (str(root).rstrip("\\"),
                                             format_bytes(gained)))
        parts = ["%d done" % ok_n]
        if skip_n:
            parts.append("%d had nothing to do" % skip_n)
        if fail_n:
            parts.append("%d failed" % fail_n)
        if stop_n:
            parts.append("stopped early")
        lines.append("Result: " + ", ".join(parts))
        if self._needs_reboot:
            lines.append("Restart needed for some changes to take effect.")
        # R-005: the typed change list, complete (the card caps its rows).
        if self._changes:
            lines.append("What changed (%s):" % counts_text(self._changes))
            for change in self._changes:
                lines.append("  " + self._change_line(change))
        # N-002: mirror the card's honest before/after into the copyable text.
        if self._measurements:
            try:
                b, a = self._measurements
                mrows = _measure_compare(b, a)
            except (TypeError, ValueError):
                mrows = None
            if mrows is not None:
                if mrows:
                    lines.append("Before \u2192 after:")
                    for row in mrows:
                        lines.append("  %s: %s" % (row.get("label", ""),
                                                   row.get("text", "")))
                else:
                    lines.append("No measurable change to memory or restart "
                                 "state.")
        if self._results:
            lines.append("")
            for label, status, nbytes, reason, task_changes in self._results:
                word = {"ok": "done", "skip": "nothing to do",
                        "fail": "failed", "stop": "stopped"}.get(status, status)
                row = "  %s — %s" % (label, word)
                if nbytes and nbytes > 0:
                    row += " (%s)" % format_bytes(nbytes)
                # Audit fix ("why failed"): include the actual reason in the
                # copyable summary too — this text is what gets pasted into
                # a support request/bug report, so the reason belongs here
                # at least as much as in the dialog itself.
                if status == "fail" and reason:
                    row += ": %s" % reason
                lines.append(row)
                # R-005: the same per-task detail the card reveals on click,
                # but complete - the card caps its sub-rows.
                for change in collapse(task_changes or ()):
                    lines.append("      " + self._change_line(change))
        return "\n".join(lines)

    def _copy_results(self):
        """Clipboard copy with visible confirmation on the button itself.

        Tk's clipboard is owned by the app, so the text would vanish when
        Cleaner Tool exits; update() flushes it to the OS clipboard while
        the window is still alive."""
        try:
            self._dlg.clipboard_clear()
            self._dlg.clipboard_append(self._summary_text())
            self._dlg.update()
            self._copy_btn.config_text("Copied!")
            self._dlg.after(1600, lambda: self._copy_btn.config_text("Copy results"))
        except Exception:
            try:
                self._copy_btn.config_text("Couldn't copy")
            except Exception:
                pass

    # ---- actions -------------------------------------------------------- #

    def _run_again(self):
        cb = self._run_again_cb
        self.close()
        if cb is not None:
            try:
                cb()
            except Exception:
                pass

    def wait(self):
        """Modal wait (grab + wait_window) — split from __init__ so the
        smoke harness can assert on the built dialog without blocking."""
        try:
            self._dlg.grab_set()
        except Exception:
            pass
        try:
            self._dlg.wait_window()
        except Exception:
            pass
