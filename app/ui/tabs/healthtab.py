"""HealthTab: one plain checkup, one score, one short list of what to do.

The previous page tried to be four products at once: a score, a stored trend,
a decline detector with named causes, a cross-tool findings board and a Smart
Care runner. It auto-started a thirteen-sensor scan on first paint, so the
page could sit on "Checking..." for most of a minute, and the findings list
was buried under three layers of cards that most users never asked for.

This page keeps the part a checkup is for:

    Run a read-only check -> see a score -> see the few things worth fixing
    -> open the screen that owns each fix.

There is no automatic repair, no hidden plan and no trend to interpret. The
single primary button runs the check; after it finishes the same button
re-runs it. Each finding has one action that opens the tab or tool that can
change it. Nothing here writes to the machine.
"""
from __future__ import annotations

import threading
import time
import tkinter as tk

from app import health_badge, health_history, health_model
from app.ui.base import Tooltip
from app.ui.metrics import (
    CARD_BODY_INSET, COMPACT_INSET, TEXT_NUDGE, TIGHT_GAP,
)
from app.ui.theme import (
    COLORS,
    GRADE_COLORS,
    PAGE_BOTTOM,
    PAGE_GUTTER,
    PAGE_TOP,
    RADIUS_CARD,
    SPACE_1,
    SPACE_2,
    SPACE_3,
    TAB_ACCENTS,
    TYPE,
)
from app.ui.widgets import (
    AnimatedButton,
    FindingRow,
    PageActionBar,
    PageLead,
    RoundedCard,
    ScrollableRoundedPanel,
)
from app.utils import TaskContext

ACCENT = TAB_ACCENTS.get("Health", COLORS["accent_teal"])

#: Grade -> (text, rim). Unknown is a separate neutral, never red.
_GRADE_STYLE = {g: (c, c) for g, c in GRADE_COLORS.items()}


class _ScoreRing(tk.Canvas):
    """A small health dial: arc for the score, grade in the middle."""

    def __init__(self, parent, size=116, bg=None):
        self._size = int(size)
        super().__init__(parent, width=self._size, height=self._size,
                         bg=bg or COLORS["surface"], highlightthickness=0,
                         bd=0)
        self._score = None
        self._grade = "?"
        self._accent = COLORS["muted"]
        self._sub = None
        try:
            self.bind("<Configure>", lambda _e: self._draw())
        except Exception:
            pass
        self.after_idle(self._draw)

    def set(self, score, grade, accent, sub=None):
        self._score = score
        self._grade = str(grade or "?")
        self._accent = accent or COLORS["muted"]
        self._sub = sub
        self._draw()

    def _draw(self):
        try:
            self.delete("all")
            w = max(self._size, self.winfo_width())
            h = max(self._size, self.winfo_height())
            cx, cy = w / 2.0, h / 2.0
            r = min(w, h) / 2.0 - 10
            width = 9
            self.create_oval(cx - r, cy - r, cx + r, cy + r,
                             outline=COLORS["hairline"], width=width)
            if self._score is not None:
                try:
                    frac = max(0.0, min(1.0, float(self._score) / 100.0))
                except (TypeError, ValueError):
                    frac = 0.0
                if frac > 0.002:
                    self.create_arc(cx - r, cy - r, cx + r, cy + r,
                                    start=90, extent=-359.0 * frac,
                                    style="arc", outline=self._accent,
                                    width=width)
            self.create_text(cx, cy - 8, text=self._grade,
                             font=TYPE["metric_large"], fill=self._accent)
            if self._sub is not None:
                sub = self._sub
            elif self._score is None:
                sub = "no score"
            else:
                sub = "%d / 100" % int(self._score)
            self.create_text(cx, cy + 22, text=sub,
                             font=TYPE["caption"], fill=COLORS["subtext"])
        except Exception:
            pass


def _trend_color(grade):
    """The sparkline tone for the latest grade, never red for 'unknown'."""
    return _GRADE_STYLE.get(grade, (COLORS["muted"], COLORS["muted"]))[1]


class _Sparkline(tk.Canvas):
    """Seven checkups, one line, no axes - a shape, not a chart.

    N-001: the ledger had been written on every checkup with nothing reading
    it. This is deliberately smaller than the trend panel the page used to
    carry: a 40px line and one sentence. No gridlines, no legend, no axis
    labels; the score and the delta are stated in words beside it.
    """

    HEIGHT = 40
    MAX_POINTS = 7

    def __init__(self, parent, bg=None):
        super().__init__(parent, height=self.HEIGHT, bg=bg or COLORS["surface"],
                         highlightthickness=0, bd=0)
        self._scores = []
        self._color = COLORS["muted"]
        try:
            self.bind("<Configure>", lambda _e: self._draw())
        except Exception:
            pass

    def set(self, scores, color):
        self._scores = [max(0, min(100, int(s))) for s in (scores or [])]
        self._color = color or COLORS["muted"]
        self._draw()

    def _draw(self):
        try:
            self.delete("all")
            pts = self._scores[-self.MAX_POINTS:]
            w = int(self.winfo_width())
            h = int(self.HEIGHT)
            if len(pts) < 2 or w < 20:
                return
            pad = 4
            lo = max(0, min(pts) - 2)
            hi = min(100, max(pts) + 2)
            span = max(1, hi - lo)
            step = (w - 2 * pad) / float(len(pts) - 1)

            def xy(i, score):
                x = pad + i * step
                y = h - pad - (score - lo) / float(span) * (h - 2 * pad)
                return x, y

            coords = []
            for i, score in enumerate(pts):
                coords.extend(xy(i, score))
            # A fill under the line would be a second surface on a surface;
            # the line alone carries the shape at this size.
            self.create_line(*coords, fill=self._color, width=2,
                             smooth=False, capstyle="round",
                             joinstyle="round")
            x, y = xy(len(pts) - 1, pts[-1])
            self.create_oval(x - 3, y - 3, x + 3, y + 3,
                             fill=self._color, outline="")
        except Exception:
            pass


def _delta_text(trend):
    """'78 → 84, +6 since last check' or an honest first-checkup line."""
    last = trend.get("last")
    if last is None:
        return "No checkup recorded yet."
    delta = trend.get("delta")
    if delta is None:
        return "%d · first recorded checkup" % int(last)
    if delta == 0:
        return "%d · no change since the last check" % int(last)
    return "%d · %s%d since the last check" % (
        int(last), "+" if delta > 0 else "−", abs(int(delta)))


def _decline_text(trend):
    """The one-line warning when the last checks kept falling.

    A-?/N-001: names the finding that moved, not only the drop. The cause is
    only present when both ends measured it (health_history's rule), so an
    unmeasured sensor never reads as the reason.
    """
    decline = trend.get("decline")
    if not decline:
        return ""
    text = "Down %d over %d checkups" % (int(decline.get("drop") or 0),
                                          int(decline.get("streak") or 0) + 1)
    causes = decline.get("causes") or []
    if causes:
        top = causes[0]
        name = _sensor_titles().get(top.get("key"),
                                    str(top.get("key") or "").replace("_", " "))
        text += " · %s fell %s → %s" % (name, top.get("from"),
                                               top.get("to"))
    return text


def _sensor_titles():
    try:
        from app.health_scan import SENSORS
        return {key: title for key, title, *_rest in SENSORS}
    except Exception:
        return {}


#: A care target is not a destination. These are the screens that own the
#: same fix; the Health page never runs a repair itself.
_CARE_ROUTE = {
    "clean": "clean",
    "repair": "repair",
    # "time" and "maintenance" both name the KINDS of fix the Repair tab owns
    # (Fix Clock Sync, SSD Maintenance) rather than tabs of their own.
    "time": "repair",
    "maintenance": "repair",
    "windows_updates": "updates",
}


class HealthTab(tk.Frame):
    """The Health destination. Read-only and intentionally small."""

    def __init__(self, parent, app):
        super().__init__(parent, bg=COLORS["bg"])
        self.app = app
        self._cards = {}
        self._scanning = False
        self._scan_seq = 0
        self._stop_token = [False]
        self._checked_at = 0.0
        self._run_enabled = True
        self._render_job = None
        self._build()
        try:
            self.bind("<Destroy>", self._on_destroy, add="+")
        except Exception:
            pass

    # ------------------------------------------------------------- build

    def _build(self):
        self._lead = lead = PageLead(
            self, accent=ACCENT, fill=COLORS["surface"], min_height=154,
            with_media=True)
        lead.pack(fill="x", padx=PAGE_GUTTER, pady=(PAGE_TOP, SPACE_1))

        self._ring = _ScoreRing(lead.media_slot, size=116,
                                bg=COLORS["surface"])
        self._ring.pack(side="left", padx=(TEXT_NUDGE, 0), pady=TEXT_NUDGE)

        self._headline = lead.headline_lbl
        self._sub = lead.sub_lbl
        self._meta = lead.meta_lbl
        lead.set(overline="PC HEALTH", headline="Check your PC",
                 sub="One read-only pass. Nothing is changed until you open "
                     "a fix.",
                 meta="No check has run in this session.")

        # N-001: the trend is one compact line above the findings list, not a
        # panel. It is created hidden and only appears once there is a second
        # point to draw a line between.
        self._trend_card = None
        self._trend = {}
        self._build_trend()

        section = tk.Frame(self, bg=COLORS["bg"])
        self._section_row = section
        section.pack(fill="x", padx=PAGE_GUTTER, pady=(SPACE_3, SPACE_1))
        tk.Label(section, text="What to look at", font=TYPE["section"],
                 bg=COLORS["bg"], fg=COLORS["subtext"],
                 anchor="w").pack(side="left")
        self._section_lbl = tk.Label(
            section, text="", font=TYPE["caption"], bg=COLORS["bg"],
            fg=COLORS["muted"], anchor="e")
        self._section_lbl.pack(side="right")

        # One action bar. The primary button is the only control on the page.
        self.bar = PageActionBar(self)
        self.bar.pack(side="bottom", fill="x", padx=PAGE_GUTTER,
                      pady=(SPACE_1, PAGE_BOTTOM))
        self._primary = AnimatedButton(
            self.bar.primary_slot, text="Check PC",
            command=self._on_primary, role="filled", size="lg",
            accent=ACCENT)
        self._primary.pack(side="right")

        self._panel = ScrollableRoundedPanel(self)
        self._panel._fill = COLORS["bg"]
        self._panel.inner.configure(bg=COLORS["bg"])
        try:
            self._panel._redraw_bg()
        except Exception:
            pass
        self._panel.pack(fill="both", expand=True, padx=PAGE_GUTTER,
                         pady=(0, SPACE_1))
        self._issue_area = tk.Frame(self._panel.inner, bg=COLORS["bg"])
        self._issue_area.pack(fill="x")

        self._empty_row("Not checked yet",
                        "Press Check PC to look at disk, memory, crashes, "
                        "security, startup, drives, Windows files and junk.")
        # Paint the saved trend last: it may change the headline from "Check
        # your PC" to "Last checkup" with a dated score, and it must not do
        # that before the static skeleton exists.
        self._refresh_trend()
        self._render()

    # ------------------------------------------------------------- trend

    def _build_trend(self):
        """The compact trend row: sparkline, delta, optional decline line.

        Clicking it opens the dated record. The whole card is one target, the
        same disclosure-card behaviour the Tools tiles use.
        """
        card = RoundedCard(self, fill=COLORS["surface"], bg=COLORS["bg"],
                           radius=RADIUS_CARD, min_height=56)
        self._trend_card = card
        body = tk.Frame(card.body, bg=COLORS["surface"])
        body.pack(fill="x", padx=CARD_BODY_INSET, pady=COMPACT_INSET)

        self._trend_line = _Sparkline(body, bg=COLORS["surface"])
        self._trend_line.pack(side="right", fill="x", expand=True,
                              padx=(SPACE_3, 0))
        col = tk.Frame(body, bg=COLORS["surface"])
        col.pack(side="left", fill="x", expand=True)
        tk.Label(col, text="SCORE TREND", font=TYPE["section"],
                 bg=COLORS["surface"], fg=COLORS["subtext"],
                 anchor="w").pack(fill="x")
        self._trend_delta = tk.Label(
            col, text="", font=TYPE["body_large"],
            bg=COLORS["surface"], fg=COLORS["text"], anchor="w")
        self._trend_delta.pack(fill="x")
        self._trend_decline = tk.Label(
            col, text="", font=TYPE["caption"], bg=COLORS["surface"],
            fg=COLORS["accent_yellow"], anchor="w", justify="left")

        self._trend_hint = tk.Label(
            body, text="History \u203a", font=TYPE["caption"],
            bg=COLORS["surface"], fg=COLORS["muted"], anchor="e")
        self._trend_hint.pack(side="right", padx=(SPACE_2, 0))

        for widget in (card, body, col, self._trend_delta):
            try:
                widget.bind("<Button-1>", lambda _e: self._open_history(),
                            add="+")
                widget.configure(cursor="hand2")
            except Exception:
                pass
        card.pack_forget()

    def _refresh_trend(self):
        """Read the ledger and paint the row. Never raises, never scans."""
        try:
            trend = health_history.trend()
        except Exception:
            trend = {}
        self._trend = trend or {}
        card = getattr(self, "_trend_card", None)
        if card is None:
            return
        count = int(self._trend.get("count") or 0)
        if count <= 0:
            try:
                card.pack_forget()
            except Exception:
                pass
            return
        try:
            if not card.winfo_manager():
                card.pack(fill="x", padx=PAGE_GUTTER, pady=(0, SPACE_1),
                          before=self._section_row)
        except Exception:
            pass
        grade = str((self._trend.get("points") or [{}])[-1].get("g") or "?")
        try:
            self._trend_line.set(self._trend.get("scores") or [],
                                 _trend_color(grade))
            self._trend_delta.config(text=_delta_text(self._trend))
            decline = _decline_text(self._trend)
            if decline:
                self._trend_decline.config(text=decline)
                if not self._trend_decline.winfo_manager():
                    self._trend_decline.pack(fill="x", pady=(COMPACT_INSET, 0))
            else:
                self._trend_decline.pack_forget()
        except Exception:
            pass

    def _open_history(self):
        try:
            self.app._open_health_history()
        except Exception:
            pass

    # ------------------------------------------------------------- lifecycle

    def on_show(self):
        """No automatic scan.

        The status-row badge already runs on the user's schedule or when they
        ask for it. Opening this page is not itself a request to measure the
        machine, and a scan that starts without a click is the behaviour that
        made the page feel stuck.

        The saved trend is re-read, though: a background checkup may have
        added a point while this page was hidden, and showing yesterday's line
        beside a fresh badge would be the provenance bug A-010 is about.
        """
        self._refresh_trend()
        return None

    def set_run_enabled(self, enabled: bool):
        """The run engine gates every tab; Health has one button to gate."""
        self._run_enabled = bool(enabled)
        try:
            if not enabled or self._scanning:
                self._primary.set_enabled(False)
            else:
                self._primary.set_enabled(True)
        except Exception:
            pass

    def _on_destroy(self, _event=None):
        self._stop_token[0] = True
        if self._render_job is not None:
            try:
                self.after_cancel(self._render_job)
            except Exception:
                pass
            self._render_job = None

    # ------------------------------------------------------------- scan

    def _on_primary(self):
        if self._scanning:
            return
        self._start_scan()

    def _start_scan(self):
        if self._scanning:
            return
        from app.health_scan import run_health_scan

        self._stop_token[0] = False
        self._scanning = True
        self._cards = {}
        self._checked_at = 0.0
        self._scan_seq += 1
        seq = self._scan_seq
        try:
            self._primary.config_text("Checking\u2026")
            self._primary.set_enabled(False)
            self._section_lbl.config(text="Checking\u2026")
        except Exception:
            pass
        self._render()

        def on_card(key, card):
            self._post(lambda k=key, c=card: self._land_card(seq, k, c))

        def worker():
            try:
                ctx = TaskContext(
                    log=lambda _m: None,
                    set_status=lambda m: self._post(
                        lambda text=m: self._scan_status(seq, text)),
                    cancelled=lambda: bool(self._stop_token[0]))
                run_health_scan(ctx, on_card=on_card)
            except Exception:
                pass
            finally:
                self._post(lambda: self._finish_scan(seq))

        threading.Thread(target=worker, daemon=True,
                         name="HealthTabScan").start()

    def _post(self, fn):
        """Marshal one worker callback to Tk.

        `TkDispatcher.post` takes exactly one callable. Passing extra
        positional arguments raised TypeError, which this method used to
        swallow before falling back to `after()` from a worker thread - the
        classic dropped-callback race. Bind the arguments in the closure
        instead, and keep the fallback for a stub app in tests.
        """
        try:
            self.app._dispatch.post(fn)
            return
        except Exception:
            pass
        try:
            self.after(0, fn)
        except Exception:
            pass

    def _scan_status(self, seq, text):
        if seq != self._scan_seq or not self._scanning:
            return
        try:
            self._section_lbl.config(text=str(text))
        except Exception:
            pass

    def _land_card(self, seq, key, card):
        if seq != self._scan_seq:
            return
        self._cards[key] = card
        # Coalesce a burst of fast sensors into one repaint. Rebuilding the
        # list once per card is what made a live scan feel like it was
        # flickering rather than filling in.
        if self._render_job is not None:
            return
        try:
            self._render_job = self.after(80, self._flush_render)
        except Exception:
            self._render_job = None
            self._render()

    def _flush_render(self):
        self._render_job = None
        self._render()

    def _finish_scan(self, seq):
        if seq != self._scan_seq:
            return
        self._scanning = False
        self._checked_at = time.time()
        summary = health_model.summarize(self._cards)
        grade = summary.get("grade") or "?"
        if grade in health_model.GRADE_POINTS:
            try:
                health_badge.record(grade, score=summary.get("score"))
            except Exception:
                pass
            try:
                self.app._refresh_health_badge()
            except Exception:
                pass
        try:
            self._primary.config_text("Check again")
            self._primary.set_enabled(self._run_enabled)
        except Exception:
            pass
        self._refresh_trend()
        self._render()

    # ------------------------------------------------------------- render

    def _render(self):
        try:
            summary = health_model.summarize(self._cards)
            scanning = self._scanning
            if scanning:
                grade = "\u2026"
                score = None
                headline = "Checking your PC\u2026"
                sub = "Results appear as each check finishes."
                total = len(_sensor_titles())
                meta = ("%d of %d checks finished" %
                        (len(self._cards), total))
                self._section_lbl.config(text="Checking\u2026")
            elif self._cards:
                grade = summary.get("grade") or "?"
                score = summary.get("score")
                headline = summary.get("headline") or "Check complete"
                sub = summary.get("detail") or ""
                meta = self._meta_text(summary)
                fixes = int(summary.get("fixes") or 0)
                critical = int(summary.get("critical") or 0)
                if critical:
                    self._section_lbl.config(
                        text="%d critical finding%s" %
                             (critical, "" if critical == 1 else "s"))
                elif fixes:
                    self._section_lbl.config(
                        text="%d finding%s" %
                             (fixes, "" if fixes == 1 else "s"))
                else:
                    self._section_lbl.config(text="All clear")
            else:
                # A-010: a score of unknown age is a claim, not a measurement.
                # With no session scan, the saved ledger still holds the last
                # real checkup, and this says exactly when it was taken.
                last = self._trend.get("last") if self._trend else None
                grade = "?"
                score = None
                headline = "Check your PC"
                sub = ("One read-only pass. Nothing is changed until you open "
                       "a fix.")
                meta = "No check has run in this session."
                if last is not None:
                    points = self._trend.get("points") or []
                    grade = str(points[-1].get("g") or "?")
                    score = int(last)
                    headline = "Last checkup"
                    sub = "From the saved record. Press Check PC for a fresh " \
                          "reading."
                    when = health_badge.age_text(
                        self._trend.get("last_ts") or 0)
                    meta = "Checked %s (saved)" % when
                self._section_lbl.config(text="")

            style_color = _GRADE_STYLE.get(
                grade, (COLORS["muted"], COLORS["muted"]))[1]
            ring_sub = ("checking" if scanning
                        else (None if score is not None else "no score"))
            self._ring.set(score, grade, style_color, sub=ring_sub)
            self._headline.config(text=headline)
            self._sub.config(text=sub)
            self._meta.config(text=meta)
            self._paint_findings(summary, scanning)
        except Exception:
            pass

    def _meta_text(self, summary):
        if not self._cards:
            return "No check has run in this session."
        when = health_badge.age_text(self._checked_at) if self._checked_at \
            else "not finished"
        bits = ["%d checks" % summary.get("total", 0)]
        known = summary.get("known", 0)
        if known != summary.get("total", 0):
            bits.append("%d measured" % known)
        bits.append(when)
        return "  \u00b7  ".join(bits)

    # ------------------------------------------------------------- findings

    def _clear_issues(self):
        for w in list(self._issue_area.winfo_children()):
            try:
                w.destroy()
            except Exception:
                pass

    #: U-001: a worklist, not a report. Past five rows the page stops being
    #: scannable and the lowest-leverage items push the top ones off-screen.
    MAX_ROWS = 5

    def _tool_findings(self, budget):
        """A-001: findings the TOOLS published, as rows the user can act on.

        The bus is written by Storage, Duplicates, Drive Toolkit, Game
        Readiness, Startup Manager and Services. Until this existed nothing in
        the app read it, so six tools measured problems no screen mentioned.
        Only warn/critical rows with a real route are shown: an info row is
        context, and a row whose target has no screen is a dead end.
        """
        if budget <= 0:
            return []
        try:
            from app import insights
            findings = insights.active(min_severity="warn")
        except Exception:
            return []
        rows = []
        seen = set()
        for finding in findings:
            action = getattr(finding, "action", {}) or {}
            target = str(action.get("target") or "") if isinstance(
                action, dict) else ""
            title = str(getattr(finding, "title", "") or "").strip()
            if not target or not title or title in seen:
                continue
            seen.add(title)
            detail = str(getattr(finding, "detail", "") or "").strip()
            severity = str(getattr(finding, "severity", "") or "warn")
            rows.append((title, detail, target, severity))
            if len(rows) >= budget:
                break
        return rows

    def _paint_findings(self, summary, scanning):
        self._clear_issues()
        if scanning and not self._cards:
            self._empty_row("Checking your PC\u2026",
                            "The first checks appear here as they land.")
            return
        priorities = summary.get("priorities") or []
        # A running scan has partial findings; the cap only applies to the
        # settled result, so a live list never hides a card that just landed.
        if not scanning and len(priorities) > self.MAX_ROWS:
            priorities = priorities[:self.MAX_ROWS]
        unknown = summary.get("unknown") or []
        if not priorities:
            if scanning:
                self._empty_row("Checking your PC\u2026",
                                "Findings appear here as each check lands.")
            elif not self._cards:
                if self._trend.get("last") is not None:
                    # A-010: the ring already shows the saved score, so "Not
                    # checked yet" beside it contradicted the page. The saved
                    # record has a score but no findings list for this
                    # session, and that is what this says.
                    self._empty_row(
                        "No findings from this session",
                        "The score above is from a saved checkup. Press "
                        "Check PC for a fresh list of what to look at.")
                else:
                    self._empty_row(
                        "Not checked yet",
                        "Press Check PC to look at disk, memory, crashes, "
                        "security, startup, drives, Windows files and junk.")
            elif unknown:
                self._empty_row(
                    "No action needed",
                    "The checks that ran passed. Some couldn't run on this PC, "
                    "so they are not counted as a pass.")
            else:
                self._empty_row("Nothing needs attention",
                                "Every check that ran passed.")
        else:
            for entry in priorities:
                self._finding_row(entry["key"], entry["card"], entry["grade"])
            known = int(summary.get("known", 0))
            passed = max(0, known - len(priorities))
            if passed:
                tk.Label(
                    self._issue_area,
                    text="%d other check%s passed" %
                         (passed, "" if passed == 1 else "s"),
                    font=TYPE["caption"], bg=COLORS["bg"],
                    fg=COLORS["muted"], anchor="w").pack(
                        fill="x", padx=SPACE_1, pady=(SPACE_1, SPACE_2))
        # A-001: what the Tools found, after the sensors and inside the same
        # five-row budget. A healthy checkup still shows these - a clean
        # sensor pass must not hide "172 duplicate files" from a tool run.
        self._paint_tool_findings(scanning,
                                 budget=max(0, self.MAX_ROWS - len(priorities)))
        if unknown:
            self._unknown_line(unknown)

    def _paint_tool_findings(self, scanning, budget):
        if scanning:
            return  # a live scan repaints; the bus rows are already stored
        rows = self._tool_findings(budget)
        if not rows:
            return
        tk.Label(self._issue_area, text="ALSO REPORTED BY TOOLS",
                 font=TYPE["section"], bg=COLORS["bg"],
                 fg=COLORS["subtext"], anchor="w").pack(
                     fill="x", padx=SPACE_1, pady=(SPACE_3, 0))
        for title, detail, target, severity in rows:
            try:
                from app.routes import label as route_label
                label = route_label(target, "Open")
            except Exception:
                label = "Open"
            row = FindingRow(
                self._issue_area, title=title, evidence=detail,
                severity="bad" if severity == "critical" else "warn",
                action_label=label,
                on_action=lambda t=target: self._route(t),
                wrap=640)
            row.pack(fill="x", padx=SPACE_1, pady=(SPACE_1, 0))
            try:
                Tooltip(row.action(), "Opens the tool that measured this. "
                                      "Nothing changes here.")
            except Exception:
                pass

    def _finding_row(self, key, card, grade):
        title = str(card.get("headline") or _sensor_titles().get(key) or key)
        detail = str(card.get("detail") or "").strip()
        note = str(card.get("note") or "").strip()
        evidence = "  ".join(x for x in (detail, note) if x)
        action = health_model.action_for(key, grade)
        target, label = self._action_for(action)
        row = FindingRow(
            self._issue_area, title=title, evidence=evidence,
            severity="bad" if grade in ("D", "F") else "warn",
            action_label=label if target else "",
            on_action=(lambda t=target: self._route(t)) if target else None,
            wrap=640)
        row.pack(fill="x", padx=SPACE_1, pady=(SPACE_1, 0))
        if target:
            try:
                Tooltip(row.action(), "Opens the screen where you decide "
                                      "what to do. Nothing changes here.")
            except Exception:
                pass
        return row

    @staticmethod
    def _action_for(action):
        """(route target, button label) for a finding, or ('', '')."""
        if not action:
            return "", ""
        target = str(action.get("target") or "")
        if not target:
            return "", ""
        if action.get("kind") == "care":
            target = _CARE_ROUTE.get(target, target)
        try:
            from app.routes import label as route_label
            label = route_label(target, "Open")
        except Exception:
            label = "Open"
        return target, label

    def _route(self, target):
        try:
            from app.routes import open_route
            open_route(self.app, target)
        except Exception:
            pass

    def _unknown_line(self, keys):
        """Unmeasured checks, told apart from checks this PC does not have.

        N-003: a desktop's battery sensor is "Not applicable", not a failure
        to measure. Reporting both as "couldn't run" would make every desktop
        look like it had a broken probe.
        """
        keys = list(keys or [])
        not_applicable = sum(
            1 for k in keys
            if str((self._cards.get(k) or {}).get("headline") or "")
            == "Not applicable")
        other = len(keys) - not_applicable
        bits = []
        if other:
            bits.append("%d check%s couldn't run on this PC."
                        % (other, "" if other == 1 else "s"))
        if not_applicable:
            bits.append("%d check%s does not apply to this PC."
                        % (not_applicable,
                           "" if not_applicable == 1 else "s"))
        text = " ".join(bits)
        lbl = tk.Label(self._issue_area, text=text, font=TYPE["caption"],
                       bg=COLORS["bg"], fg=COLORS["muted"], anchor="w")
        lbl.pack(fill="x", padx=SPACE_1, pady=(SPACE_2, 0))
        try:
            Tooltip(lbl, "Unmeasured checks are not failures, and they are "
                         "not counted as passes.")
        except Exception:
            pass

    def _empty_row(self, title, detail):
        card = RoundedCard(self._issue_area, fill=COLORS["surface"],
                           bg=COLORS["bg"], radius=RADIUS_CARD,
                           min_height=72)
        card.pack(fill="x", pady=(0, SPACE_2))
        body = tk.Frame(card.body, bg=COLORS["surface"])
        body.pack(fill="both", expand=True, padx=CARD_BODY_INSET, pady=SPACE_3)
        tk.Label(body, text=title, font=TYPE["title_small"],
                 bg=COLORS["surface"], fg=COLORS["text"], anchor="w").pack(
                     fill="x")
        detail_lbl = tk.Label(body, text=detail, font=TYPE["caption"],
                              bg=COLORS["surface"], fg=COLORS["subtext"],
                              anchor="w", justify="left")
        detail_lbl.pack(fill="x", pady=(TIGHT_GAP, 0))

        def _fit(_event=None):
            try:
                room = int(body.winfo_width()) - 28
            except Exception:
                return
            if room > 80:
                detail_lbl.config(wraplength=room)
        body.bind("<Configure>", _fit, add="+")
        _fit()
        return card


__all__ = ["HealthTab"]
