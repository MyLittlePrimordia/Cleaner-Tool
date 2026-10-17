"""RunHistoryDialog - "what has cleaning actually reclaimed?" (N-006).

Backed by app/history.py, which reads the rolling run_history the run
pipeline already writes. Three things, in order of usefulness:

    banner      the lifetime total and run count (and the last 7 days)
    sparkline   the last 30 days, one block per day
    list        the most recent runs, newest first

Read-only: it deletes nothing and changes nothing. When there is no history
yet it says so plainly instead of drawing a flat line and calling it a trend.
"""
from __future__ import annotations

import time
import tkinter as tk

from app.ui.dialogs._tool_page import ToolPage
from app.ui.theme import COLORS, F, FM
from app.ui.widgets import RoundedCard

_MONO = (FM, 13)      # the sparkline needs a fixed-width face
_SHOW = 24                    # rows listed before the "latest N of M" note
_DAYS = 30


class RunHistoryDialog(ToolPage):
    def __init__(self, parent, app):
        self.app = app
        self._stop_token = [False]
        self._spark_lbl = None
        self._list_rows = []
        super().__init__(
            parent, title="Run History",
            scope="Every clean this app has recorded. Read-only \u2014 it "
                  "changes nothing on your PC.",
            scroll=True)
        self.refresh_btn = self.add_button("Refresh", self._start)
        self.on_close(self._stop)
        self._start()

    # ------------------------------------------------------------- lifecycle
    def _stop(self):
        self._stop_token[0] = True

    def _clear(self):
        self._spark_lbl = None
        self._list_rows = []
        for w in list(self.content.winfo_children()):
            w.destroy()

    def _start(self):
        from app import history
        self._render(history.runs())

    # ---------------------------------------------------------------- render
    def _render(self, rs):
        from app import history
        from app.utils import format_bytes

        self._clear()
        total_bytes, count = history.total()
        if not count:
            self.banner.set_state("\u2022", COLORS["muted"])
            self.banner.set_title("No runs recorded yet.")
            self.banner.set_sub("Clean something and it will show up here.")
            return

        self.banner.set_state("\u2713", COLORS["accent_green"])
        self.banner.set_title(
            "%s reclaimed across %d clean%s." % (
                format_bytes(total_bytes), count, "" if count == 1 else "s"),
            COLORS["accent_green"])
        week_b, week_n = history.reclaimed_since(7)
        self.banner.set_sub(
            "Last 7 days: %s across %d clean%s." % (
                format_bytes(week_b), week_n, "" if week_n == 1 else "s")
            if week_n else "")

        # -- 30-day sparkline -------------------------------------------- #
        card = RoundedCard(self.content, fill=COLORS["surface"],
                           bg=COLORS["bg"], radius=14)
        card.pack(fill="x", padx=2, pady=5)
        body = card.body
        tk.Label(body, text="Last %d days" % _DAYS, font=(F, 10, "bold"),
                 bg=COLORS["surface"], fg=COLORS["text"], anchor="w").pack(
            fill="x", padx=12, pady=(9, 0))
        series = history.daily(_DAYS)
        self._spark_lbl = tk.Label(body, text=history.sparkline(series),
                                   font=_MONO, bg=COLORS["surface"],
                                   fg=COLORS["accent_green"], anchor="w")
        self._spark_lbl.pack(fill="x", padx=12, pady=(4, 0))
        peak = max(series) if series else 0
        tk.Label(body,
                 text=("Best day: %s" % format_bytes(peak)) if peak
                 else "Nothing reclaimed in the last %d days." % _DAYS,
                 font=(F, 9), bg=COLORS["surface"], fg=COLORS["subtext"],
                 anchor="w").pack(fill="x", padx=12, pady=(2, 10))

        # -- recent runs -------------------------------------------------- #
        card2 = RoundedCard(self.content, fill=COLORS["surface"],
                            bg=COLORS["bg"], radius=14)
        card2.pack(fill="x", padx=2, pady=5)
        body2 = card2.body
        tk.Label(body2, text="Recent runs", font=(F, 10, "bold"),
                 bg=COLORS["surface"], fg=COLORS["text"], anchor="w").pack(
            fill="x", padx=12, pady=(9, 4))
        for entry in list(reversed(rs))[:_SHOW]:
            row = tk.Frame(body2, bg=COLORS["surface"])
            row.pack(fill="x", padx=12, pady=1)
            tk.Label(row, text=self._when(entry["t"]), font=(F, 9),
                     bg=COLORS["surface"], fg=COLORS["text"],
                     anchor="w").pack(side="left")
            tk.Label(row, text=format_bytes(entry["b"]), font=(F, 9, "bold"),
                     bg=COLORS["surface"],
                     fg=COLORS["accent_green"]).pack(side="right")
            self._list_rows.append(row)
        if len(rs) > _SHOW:
            tk.Label(body2, text="Showing the latest %d of %d runs." % (
                _SHOW, len(rs)), font=(F, 9), bg=COLORS["surface"],
                fg=COLORS["subtext"], anchor="w").pack(
                    fill="x", padx=12, pady=(6, 0))
        tk.Frame(body2, bg=COLORS["surface"], height=8).pack()

    @staticmethod
    def _when(t):
        try:
            return time.strftime("%a %d %b \u00b7 %H:%M", time.localtime(t))
        except (OverflowError, OSError, ValueError):
            return "?"
