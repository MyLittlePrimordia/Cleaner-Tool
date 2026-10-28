"""HealthHistoryDialog — the dated checkups behind the Health tab's trend.

A-002/N-001. Every checkup appends one point to the score ledger; until this
window existed nothing read it, so the app paid to write a history it never
showed. The list answers three questions per row and nothing else:

    when      the local date and time of the checkup
    how good  the score and its grade
    why       the change from the checkup before, naming the finding that moved

Clearing is offered here because this is the only screen that shows what would
be cleared, and the confirmation says plainly that it changes nothing on the
PC: the ledger is a record of measurements, not a setting.
"""
from __future__ import annotations

import time

import tkinter as tk

from app.ui.base import _themed_askyesno
from app.ui.dialogs._tool_page import ToolPage
from app.ui.theme import COLORS, SPACE_1, SPACE_2, TYPE
from app.ui.widgets import EmptyState, FindingRow

ACCENT = COLORS["accent_teal"]

#: Grade -> finding severity. The same mapping the Health tab uses, so a "C"
#: is the same colour in both places.
_SEVERITY = {"A": "ok", "B": "ok", "C": "warn", "D": "bad", "F": "bad"}


def _sensor_labels():
    try:
        from app.health_scan import SENSORS
        return {key: title for key, title, *_rest in SENSORS}
    except Exception:
        return {}


def _when(ts, now=None) -> str:
    """A date that reads as a record, with "today"/"yesterday" when true."""
    now = time.time() if now is None else float(now)
    try:
        ts = float(ts)
    except (TypeError, ValueError):
        return "unknown"
    try:
        lt = time.localtime(ts)
        nt = time.localtime(now)
        if (lt.tm_year, lt.tm_yday) == (nt.tm_year, nt.tm_yday):
            day = "Today"
            # A same-day checkup was a few hours ago: the hour says more than
            # the date, and "Today" alone would make two rows look identical.
            return "%s, %s" % (day, time.strftime("%H:%M", lt))
        if lt.tm_year == nt.tm_year:
            return time.strftime("%b %d, %H:%M", lt)
        return time.strftime("%b %d %Y", lt)
    except (ValueError, OverflowError, OSError):
        return "unknown"


def _change(old, new, labels) -> str:
    """One sentence naming what moved, or "" for the first point.

    The score delta is always stated. A cause is named only when both checkups
    measured that finding, so a sensor that refused is never reported as a
    regression.
    """
    try:
        delta = int(new["s"]) - int(old["s"])
        nxt = int(new["s"])
        prev = int(old["s"])
    except (KeyError, TypeError, ValueError):
        return ""
    if delta == 0:
        text = "No change from %d." % prev
    elif delta > 0:
        text = "Up %d from %d (%d)." % (delta, prev, nxt)
    else:
        text = "Down %d from %d (%d)." % (-delta, prev, nxt)
    cause = ""
    from app import health_history
    try:
        causes = health_history.causes_between(old, new)
    except Exception:
        causes = []
    if causes:
        top = causes[0]
        name = labels.get(top.get("key"),
                          str(top.get("key") or "").replace("_", " "))
        cause = "%s fell %s \u2192 %s." % (name, top.get("from"), top.get("to"))
    return (text + (" " + cause if cause else "")).strip()


class HealthHistoryDialog(ToolPage):
    """Dated list of past checkups, newest first."""

    def __init__(self, parent, app):
        self.app = app
        self._points = []
        super().__init__(
            parent, title="Checkup History", accent=ACCENT,
            scope="Read-only record of past checkups. Clearing it changes "
                  "nothing on your PC.",
            scroll=True)

        self._list = tk.Frame(self.content, bg=COLORS["bg"], bd=0)
        self._list.pack(fill="x")

        self._clear_btn = self.add_button("Clear history", self._clear,
                                          kind="danger")
        self._done = self.add_button("Close", self.close, kind="primary")

        self.banner.set_state("\u2026", COLORS["muted"])
        self.banner.set_title("Reading the checkup record\u2026")
        self.run_worker(_gather, self._land)

    # ------------------------------------------------------------ data
    def _land(self, data):
        self._points = list(data.get("points") or [])
        self._render()
        self._say_summary(data)

    def _say_summary(self, data):
        n = len(self._points)
        if not n:
            self.banner.set_state("\u2014", COLORS["muted"])
            self.banner.set_title("No checkups recorded yet.")
            self.banner.set_sub("Press Check PC on the Health tab to start a "
                                "record.")
            self._clear_btn.set_enabled(False)
            return
        self._clear_btn.set_enabled(True)
        best = data.get("best")
        worst = data.get("worst")
        if n == 1:
            self.banner.set_state("1", ACCENT)
            self.banner.set_title("One checkup recorded.")
            self.banner.set_sub("A trend needs a second one.")
        else:
            self.banner.set_state(str(n), ACCENT)
            self.banner.set_title(
                "%d checkups \u00b7 scores %d to %d." % (n, worst, best))
            span = float(data.get("span_days") or 0.0)
            self.banner.set_sub(
                "Over the last %d day%s." % (max(1, round(span)),
                                             "" if round(span) == 1 else "s"))

    def _render(self):
        for w in list(self._list.winfo_children()):
            try:
                w.destroy()
            except Exception:
                pass
        if not self._points:
            EmptyState(self._list, glyph="\u2014",
                       text="Nothing recorded yet.",
                       detail="Every finished checkup is added here "
                              "automatically.",
                       wrap=self.body_width()).pack(fill="x")
            return
        labels = _sensor_labels()
        rows = list(reversed(self._points))  # newest first
        wrap = self.body_width(74)
        for index, point in enumerate(rows):
            previous = rows[index + 1] if index + 1 < len(rows) else None
            detail = _change(previous, point, labels) if previous else \
                "First recorded checkup \u2014 no earlier score to compare."
            grade = str(point.get("g") or "?")
            row = FindingRow(
                self._list,
                title="%s \u00b7 %d (%s)" % (_when(point.get("t")),
                                              int(point.get("s", 0)), grade),
                evidence=detail,
                severity=_SEVERITY.get(grade, "info"),
                wrap=wrap)
            row.pack(fill="x", pady=(SPACE_1, 0))
        tk.Label(self._list,
                 text="Only the score record is cleared. Nothing on your PC "
                      "is touched.",
                 font=TYPE["caption"], bg=COLORS["bg"],
                 fg=COLORS["muted"], anchor="w",
                 justify="left").pack(fill="x", padx=SPACE_2,
                                      pady=(SPACE_2, SPACE_1))

    # ------------------------------------------------------------ clear
    def _clear(self):
        if not self._points:
            return
        ok = _themed_askyesno(
            self._dlg, "Clear checkup history?",
            "This removes the %d recorded score%s and the trend line.\n\n"
            "It does not change, undo or clean anything on your PC. The next "
            "checkup starts a new record." % (
                len(self._points), "" if len(self._points) == 1 else "s"),
            accent=COLORS["accent_red"])
        if not ok:
            return
        try:
            from app import health_history
            health_history.clear()
        except Exception as exc:
            self.status.stop("Could not clear the record: %s" % (exc,),
                             COLORS["accent_red"])
            return
        self._points = []
        self._render()
        self.banner.set_state("\u2014", COLORS["muted"])
        self.banner.set_title("History cleared.")
        self.banner.set_sub("The next checkup starts a new record.")
        self._clear_btn.set_enabled(False)
        self.status.stop("Cleared. Nothing on your PC was touched.",
                         COLORS["accent_green"])


def _gather():
    """Worker body: read the ledger and the derived trend. No Tk calls."""
    from app import health_history
    return health_history.trend()


__all__ = ["HealthHistoryDialog"]
