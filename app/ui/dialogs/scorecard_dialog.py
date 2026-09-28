"""ScorecardDialog — extracted verbatim from app/gui.py (H12, batch 1).

One dialog per module, mirroring app/ui/widgets. Depends only on
app.ui below it, plus the app.utils helpers it already used; it
knows nothing
about any other dialog except where noted.
"""
from __future__ import annotations

import tkinter as tk
from tkinter import ttk, messagebox
from app.ui.base import ThemedModal, Tooltip
from app.ui.hardware.drives import _drive_gains
from app.ui.theme import COLORS, F, TAB_ACCENTS, _hex_lerp, _round_rect_points
from app.ui.widgets import AnimatedButton, ScrollableRoundedPanel
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

    WIDTH = 800                 # fixed shared popup footprint (ThemedModal)
    _ROWS_INLINE = 12    # beyond this the row list scrolls
    _ROW_H = 26          # per-row height budget for the panel cap

    def __init__(self, parent, *, tab_name, mode, cancelled, results,
                 total_bytes, before=None, after=None, needs_reboot=False,
                 on_run_again=None, ok_text="Close",
                 before_drives=None, after_drives=None):
        self._run_again_cb = on_run_again
        # Feature 3 (Copy results) needs the raw numbers after the widgets
        # are built, so they are kept on the instance rather than living
        # only as locals inside this constructor.
        self._tab_name = tab_name
        self._mode = mode
        self._cancelled = bool(cancelled)
        self._total_bytes = total_bytes or 0
        # Audit fix (Scorecard "why failed"): results grew a 4th element
        # (a short failure reason) but existing callers — including this
        # file's own construction site above and several smoke-test
        # fixtures — still pass 3-tuples. Normalize once here instead of
        # requiring every caller to know about the new shape.
        results = [tuple(r) + (None,) if len(r) < 4 else tuple(r)
                   for r in (results or [])]
        self._results = list(results)
        self._needs_reboot = bool(needs_reboot)
        self._gains = _drive_gains(before_drives, after_drives)
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
        ok_n = sum(1 for _l, s, _b, _r in results if s == "ok")
        skip_n = sum(1 for _l, s, _b, _r in results if s == "skip")
        fail_n = sum(1 for _l, s, _b, _r in results if s == "fail")
        stop_n = sum(1 for _l, s, _b, _r in results if s == "stop")
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
            hero.pack(fill="x", pady=(2, 0))
            self._hero_lbl = tk.Label(hero, text=format_bytes(total_bytes),
                                      font=(F, 26, "bold"), bg=COLORS["bg"],
                                      fg=COLORS["accent_green"])
            self._hero_lbl.pack()
            tk.Label(hero, text="disk space freed", font=(F, 10),
                     bg=COLORS["bg"], fg=COLORS["subtext"]).pack()

        # before/after free-space bar — only when the run freed space AND
        # both C: snapshots were captured (honest: no invented numbers)
        self._bar_canvas = None
        if (total_bytes and total_bytes > 0 and before and after
                and before[1] and after[1]):
            b_avail, b_total = (before[0] or 0), before[1]
            a_avail, a_total = (after[0] or 0), after[1]
            if a_avail >= b_avail and a_total > 0:
                bar_wrap = tk.Frame(body, bg=COLORS["bg"])
                bar_wrap.pack(fill="x", padx=18, pady=(8, 0))
                tk.Label(bar_wrap,
                         text=f"C:  {format_bytes(b_avail)}  →  {format_bytes(a_avail)} free",
                         font=(F, 9, "bold"), bg=COLORS["bg"],
                         fg=COLORS["text"]).pack(anchor="w")
                self._bar_canvas = tk.Canvas(bar_wrap, height=16,
                                             highlightthickness=0, bd=0,
                                             bg=COLORS["bg"])
                self._bar_canvas.pack(fill="x", pady=(4, 0))
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
                               width=self.WIDTH - 36)

        # status chips
        chips = tk.Frame(body, bg=COLORS["bg"])
        chips.pack(fill="x", padx=18, pady=(10, 0))
        tk.Label(chips, text=f"✓  {ok_n} done", font=(F, 10, "bold"),
                 bg=COLORS["bg"], fg=COLORS["accent_green"]).pack(side="left")
        if skip_n:
            tk.Label(chips, text=f"⊘  {skip_n} skipped", font=(F, 10, "bold"),
                     bg=COLORS["bg"], fg=COLORS["subtext"]).pack(side="left", padx=(12, 0))
        if fail_n:
            tk.Label(chips, text=f"✕  {fail_n} failed", font=(F, 10, "bold"),
                     bg=COLORS["bg"], fg=COLORS["accent_red"]).pack(side="left", padx=(12, 0))
        if stop_n:
            tk.Label(chips, text="◦  stopped early", font=(F, 10, "bold"),
                     bg=COLORS["bg"], fg=COLORS["accent_yellow"]).pack(side="left", padx=(12, 0))

        # per-drive breakdown (feature 4) — only when MORE THAN ONE drive
        # actually gained space. One drive is the normal case and the hero
        # number already says it; listing "C: +2.1 GB" under a "2.1 GB
        # freed" headline would just be the same fact twice.
        self._drive_rows = None
        if len(self._gains) > 1:
            drow = self._drive_rows = tk.Frame(body, bg=COLORS["bg"])
            drow.pack(fill="x", padx=18, pady=(8, 0))
            tk.Label(drow, text="Space freed on each drive", font=(F, 9, "bold"),
                     bg=COLORS["bg"], fg=COLORS["subtext"]).pack(anchor="w")
            line = tk.Frame(drow, bg=COLORS["bg"])
            line.pack(fill="x", pady=(3, 0))
            for root, gained in self._gains[:6]:
                chip = tk.Frame(line, bg=COLORS["bg_alt"])
                chip.pack(side="left", padx=(0, 8))
                tk.Label(chip, text=str(root).rstrip("\\"), font=(F, 9, "bold"),
                         bg=COLORS["bg_alt"], fg=COLORS["text"]).pack(
                             side="left", padx=(8, 4), pady=3)
                tk.Label(chip, text="+" + format_bytes(gained), font=(F, 9, "bold"),
                         bg=COLORS["bg_alt"],
                         fg=COLORS["accent_green"]).pack(
                             side="left", padx=(0, 8), pady=3)

        # "How is my PC doing?" (feature 10) — one friendly sentence, and
        # only once there is more than this single run to talk about.
        # Nothing to brag about yet = no line, rather than "freed 0 GB".
        self._month_lbl = None
        month_text = self._month_summary()
        if month_text:
            self._month_lbl = tk.Label(body, text=month_text, font=(F, 9),
                                       bg=COLORS["bg"], fg=COLORS["subtext"])
            self._month_lbl.pack(pady=(8, 0))

        # hairline separator
        tk.Frame(body, bg=COLORS["hairline"], height=1).pack(fill="x", padx=18, pady=(10, 4))

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
        brow = tk.Frame(body, bg=COLORS["bg"])
        brow.pack(side="bottom", fill="x", padx=18, pady=(12, 10))

        n_rows = len(results)
        panel_h = max(52, min(n_rows, self._ROWS_INLINE) * self._ROW_H + 16)
        self._rows_panel = panel = ScrollableRoundedPanel(body)
        panel.config(height=panel_h)   # after ctor (its Canvas init pins height=10)
        # fill="both" + expand: absorb whatever the body has left, and give
        # space back when the task list is short
        panel.pack(fill="both", expand=True, padx=18, pady=(2, 0))
        if not results:
            tk.Label(panel.inner, text="No tasks ran.", font=(F, 9),
                     bg=COLORS["bg_alt"], fg=COLORS["subtext"]).pack(pady=12)
        for label, status, nbytes, reason in results:
            self._build_row(panel.inner, label, status, nbytes, reason)
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
            banner = self._reboot_banner = tk.Frame(body, bg="#3A2C12")
            tk.Label(banner,
                     text="⚠  Restart needed — some changes only take effect after a reboot.",
                     font=(F, 9, "bold"), bg="#3A2C12",
                     fg=COLORS["accent_yellow"]).pack(anchor="w", padx=10, pady=8)
            banner.pack(fill="x", padx=18, pady=(10, 0))

        # buttons — Close primary (accent fill), Clean Again secondary
        if self._run_again_cb is not None:
            AnimatedButton(brow, text="Clean Again", command=self._run_again,
                           bg=COLORS["surface"], fg=COLORS["text"],
                           font=(F, 9, "bold"), padx=18,
                           pady=7).pack(side="right", padx=(8, 0))
        # Feature 3: plain-English summary straight to the clipboard, so a
        # user can paste it into a Discord help thread without screenshots.
        self._copy_btn = AnimatedButton(
            brow, text="Copy results", command=self._copy_results,
            bg=COLORS["surface"], fg=COLORS["text"],
            font=(F, 9, "bold"), padx=18, pady=7)
        self._copy_btn.pack(side="right", padx=(8, 0))
        AnimatedButton(brow, text=ok_text, command=self.close,
                       bg=self._title_accent, fg=COLORS["black"],
                       font=(F, 9, "bold"), padx=22,
                       pady=7).pack(side="right")

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

    def _build_row(self, parent, label, status, nbytes, reason=None):
        """One compact one-liner: status glyph | task label (+ plain-
        language suffix for skip/fail/stop) | bytes freed (right). Same
        bg_alt row look as the preset summary cells.

        Audit fix ("why failed"): a failed row used to just say "— failed
        (see log)" — technically true but meant re-opening the text log
        and hunting for the matching line to find out what actually went
        wrong. The real reason was already being captured (and logged)
        right at the point of failure; it just never rode along into the
        Scorecard's own results. Show a short version inline (tooltip
        carries the full text — some exception messages run long)."""
        glyph, gcolor = self._glyphs(status)
        row = tk.Frame(parent, bg=COLORS["bg_alt"])
        tk.Label(row, text=glyph, font=(F, 9, "bold"), bg=COLORS["bg_alt"],
                 fg=gcolor, width=2, anchor="w").pack(side="left")
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
        lbl = tk.Label(row, text=text, font=(F, 9), bg=COLORS["bg_alt"], fg=fg,
                       anchor="w", justify="left",
                       wraplength=400)
        lbl.pack(side="left", padx=(2, 0))
        if full_reason:
            try:
                Tooltip(lbl, full_reason)
            except Exception:
                pass
        if nbytes and nbytes > 0:
            tk.Label(row, text=format_bytes(nbytes), font=(F, 9, "bold"),
                     bg=COLORS["bg_alt"],
                     fg=COLORS["accent_green"]).pack(side="right")
        row.pack(fill="x", pady=1)

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

    # ---- summaries ------------------------------------------------------ #

    def _month_summary(self):
        """Friendly one-liner about the last 30 days, or "" if it would
        not tell the user anything they cannot already see above.

        Deliberately vague about precision ("about 12 GB"): the history
        stores what each run reported, so it is a good running total, not
        an accountant's figure, and the wording should not pretend
        otherwise."""
        try:
            from app.config_persist import freed_since
            total, runs = freed_since(30)
        except Exception:
            return ""
        # One run in the window IS this run — nothing extra to say.
        if runs < 2 or total <= 0:
            return ""
        return ("You've freed about %s in the last 30 days, across %d cleans."
                % (format_bytes(total), runs))

    def _summary_text(self):
        """The plain-English block that Copy results puts on the clipboard."""
        ok_n, skip_n, fail_n, stop_n = self._counts
        lines = ["Cleaner Tool — %s" % self._title]
        if self._total_bytes and self._total_bytes > 0:
            lines.append("Space freed: %s" % format_bytes(self._total_bytes))
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
        if self._results:
            lines.append("")
            for label, status, nbytes, reason in self._results:
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
