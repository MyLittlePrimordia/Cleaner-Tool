"""PastRunsDialog - what earlier runs changed (R-005).

The rolling `run_history` has existed since N-006, but its dialog was removed
in the 2026-10 UI pass (one of the two weakest Tools cards; removing it is what
stopped that grid scrolling, and tests keep it removed). The DATA stayed, as
the scorecard's 30-day line reads it.

So this is not a resurrection of that page: it is a small viewer, opened from
the scorecard at the moment the user has just seen one run's changes and may
want to look back. Each row is one recorded run, and a row with typed changes
expands to say what they were.

The one thing it writes is "Clear History" — and that empties only the
ledger. A run's own effects live in the registry, the services and the disk,
never here, so the confirm says so before it deletes anything.

History keeps change detail only for its newest runs (see
`config_schema.RUN_HISTORY_CHANGE_RUNS`); an older entry simply has no list and
expands to nothing rather than pretending it made no changes.
"""
from __future__ import annotations

import time
import tkinter as tk

from app.changes import KIND_ORDER, kind_noun
from app.history import clear, deltas, kind_label, runs
from app.ui.base import (
    ThemedModal, Tooltip, _themed_askyesno, _themed_showinfo,
)
from app.ui.theme import COLORS, TYPE, SPACE_1, SPACE_2
from app.ui.metrics import CONTROL_GAP, HAIRLINE_NUDGE, INDENT, TEXT_NUDGE, TIGHT_GAP
from app.ui.widgets import (
    EmptyState, ModalFooter, ScrollableRoundedPanel
)
from app.ui.widgets.segmented import SegmentedTabs
from app.utils import format_bytes

_SHOW_RUNS = 24      # the viewer's own cap; history holds more
_CHANGE_LINES = 12   # expanded lines before "and N more"


def _change_counts(changes) -> str:
    """'2 registry values, 1 service' from stored change records."""
    counts = {}
    for change in changes or ():
        kind = change.get("k") if isinstance(change, dict) else None
        if isinstance(kind, str):
            counts[kind] = counts.get(kind, 0) + 1
    parts = []
    for kind in KIND_ORDER:
        n = counts.pop(kind, 0)
        if n:
            parts.append("%d %s" % (n, kind_noun(kind, n)))
    for kind, n in sorted(counts.items()):
        parts.append("%d %s" % (n, kind_noun(kind, n)))
    return ", ".join(parts)


class PastRunsDialog(ThemedModal):
    """Newest-first list of recorded runs; click a row to expand its changes."""

    def __init__(self, parent, entries=None):
        """`entries` is the oldest-first run list the caller already read
        (H-011: the scorecard passes its own single read). None reads it here,
        for the case where this dialog is opened on its own."""
        super().__init__(parent, title="Past Runs",
                         accent=COLORS["accent_green"])
        self._rows = []
        source = runs() if entries is None else entries
        self._entries = list(reversed(list(source)))[:_SHOW_RUNS]
        #: N-007: one delta per displayed row, against the previous run of the
        #: same kind (None where there is nothing honest to compare). Not
        #: filtered: a row keeps its comparison when the filter is switched,
        #: because the baseline is a fact about the ledger, not the view.
        self._deltas = deltas(self._entries)
        #: U-010: which kind the list is showing. "all" at open - the filter
        #: exists so a Tweak run is not buried under a week of cleans.
        self._filter = "all"
        self._seg = None
        self._filter_row = None
        #: True once the ledger was emptied from here, so the card above can
        #: hide its 30-day line and link without reading config again.
        self.cleared = False
        body = self.body

        # Buttons reserved FIRST (house rule): the list below is the only
        # widget allowed to give ground, so a long history scrolls instead of
        # pushing Close off the card. The bar itself is the shared
        # ModalFooter, so this window's footer is the same height and uses
        # the same insets as every other plain popup.
        self._footer = ModalFooter(body, accent=COLORS["accent_green"])
        self._footer.pack_footer()
        self._footer.button("Close", self.close, kind="primary")

        # Destructive action, deliberately the quiet one: on the far side
        # from Close, and only present while there is a ledger to clear. It
        # still asks (see _confirm_clear).
        self._clear_btn = None
        if self._entries:
            self._clear_btn = self._footer.button(
                "Clear History", self._confirm_clear, kind="quiet")

        self._hint = tk.Label(
            body, text="Newest first. Click a run to see what it changed.",
            font=TYPE["caption"], bg=COLORS["bg"],
            fg=COLORS["subtext"], anchor="w")
        self._hint.pack(fill="x", pady=(TEXT_NUDGE, CONTROL_GAP))

        # Kind filter (U-010). Only worth a row when the ledger holds more
        # than one kind - a single-kind machine would get a pill with one
        # choice on it. Kinds come from the stored entries (title-cased by
        # history.kind_label), so a future surface appears here by itself.
        kinds = []
        for entry in self._entries:
            kind = self._kind_of(entry)
            if kind not in kinds:
                kinds.append(kind)
        if len(kinds) > 1:
            self._filter_row = tk.Frame(body, bg=COLORS["bg"])
            self._filter_row.pack(fill="x", pady=(0, CONTROL_GAP))
            try:
                self._seg = SegmentedTabs(
                    self._filter_row,
                    labels=["All runs"] + [kind_label(k) for k in kinds],
                    keys=["all"] + kinds, initial="all",
                    accent=COLORS["accent_green"],
                    command=self._set_filter)
                self._seg.pack(anchor="w")
            except Exception:
                self._seg = None

        panel = self._panel = ScrollableRoundedPanel(body)
        panel.pack(fill="both", expand=True)
        self._rows = []
        self._fill(self._entries)

        try:
            self._dlg.bind("<Return>", lambda _e: self.close())
        except Exception:
            pass
        # I-008: the clear action is a canvas-drawn button and cannot take
        # keyboard focus, so it also has a shortcut. Escape still closes.
        try:
            self._dlg.bind("<Delete>", lambda _e: self._confirm_clear())
        except Exception:
            pass

    # ---- the list ------------------------------------------------------- #

    def _fill(self, entries, *, emptied=False):
        """(Re)draw the run list. The only caller after __init__ is a clear,
        so every existing row — and every expanded detail frame with it — is
        destroyed rather than left behind under an empty state."""
        for child in list(self._panel.inner.winfo_children()):
            try:
                child.destroy()
            except Exception:
                pass
        self._rows = []
        everything = list(entries or ())
        entries = [e for e in everything
                   if self._filter in (None, "all")
                   or self._kind_of(e) == self._filter]
        if not entries:
            # Two different empty states: never recorded anything yet, and
            # "you just cleared it". The hint above carries the second half so
            # the box itself never has to repeat the word "cleared".
            # Three empty states now: nothing ever recorded, just cleared, and
            # "nothing of THIS kind" - the last one must not claim the ledger
            # is empty when it is only filtered.
            if emptied:
                text = "Nothing to show."
                detail = "New runs will appear here."
            elif everything:
                text = "No %s runs recorded." % kind_label(
                    self._filter).lower()
                detail = "Switch the filter to see the other runs."
            else:
                text = "No runs recorded yet."
                detail = "Run a preset and its changes land here."
            # The same empty state every other list uses (C-004): a glyph,
            # one sentence naming the cause, and at most one supporting line.
            # The three cases are genuinely different states, so they get
            # three different sentences rather than one grey shrug.
            EmptyState(self._panel.inner, glyph="\u21ba", text=text,
                       detail=detail, bg=COLORS["bg_alt"],
                       wrap=max(240, self.body_width(26))).pack(fill="x")
        for entry in entries:
            self._build_row(self._panel.inner, entry,
                            self._delta_for(entry))
        try:
            self._panel.refresh_scroll()
        except Exception:
            pass

    def _confirm_clear(self):
        """Ask, then empty the ledger (never the effects it describes).

        The count comes from the list this dialog was BUILT from - H-011: a
        fresh config read here was the second of three reads per open.
        """
        count = len(self._entries)
        if count <= 0:
            self._fill([], emptied=True)
            return
        ok = _themed_askyesno(
            self._dlg, "Clear Run History?",
            "Forget all %d recorded runs?\n\nThis empties the list only — "
            "Windows settings, files and services these runs touched stay "
            "exactly as they are, and no run is undone." % count,
            accent=COLORS["accent_yellow"], yes_text="Clear History",
            no_text="Keep")
        # The confirm is a nested modal: its close drops the grab, so take it
        # back or Escape would stop closing this window (BUG-010 pattern).
        try:
            self._dlg.grab_set()
        except Exception:
            pass
        if not ok:
            return
        if not clear():
            # Body says the CONSEQUENCE, not the title over again (the
            # popup-title guard in dev/tests/test_popup_titles.py is right: the
            # reader already knows it failed, they need to know what stayed).
            _themed_showinfo(
                self._dlg, "Couldn't Clear History",
                "Your settings file is read-only right now, so nothing was "
                "removed.", accent=COLORS["accent_yellow"], ok_text="OK")
            try:
                self._dlg.grab_set()
            except Exception:
                pass
            return
        self._entries = []
        self.cleared = True
        self._filter = "all"
        try:
            if self._filter_row is not None:
                self._filter_row.destroy()
        except Exception:
            pass
        self._filter_row = None
        self._seg = None
        self._fill([], emptied=True)
        self._hint.config(text="History cleared. New runs will appear here.")
        try:
            if self._clear_btn is not None:
                self._clear_btn.destroy()
        except Exception:
            pass
        self._clear_btn = None

    # ---- one run -------------------------------------------------------- #

    def _delta_for(self, entry):
        """This entry's stored comparison, found by identity in the source.

        The filter rebuilds the visible rows, so a position is not stable;
        the entry dict itself is.
        """
        try:
            index = next(i for i, e in enumerate(self._entries)
                         if e is entry)
        except (StopIteration, AttributeError):
            return None
        try:
            return self._deltas[index]
        except (IndexError, AttributeError, TypeError):
            return None

    def _build_row(self, parent, entry, delta=None):
        row = tk.Frame(parent, bg=COLORS["bg_alt"])
        left = tk.Frame(row, bg=COLORS["bg_alt"])
        left.pack(side="left", fill="x", expand=True, padx=(SPACE_2, SPACE_1), pady=TIGHT_GAP)
        title = tk.Label(left, text=self._when(entry["t"]),
                         font=TYPE["caption"], bg=COLORS["bg_alt"],
                         fg=COLORS["text"], anchor="w")
        title.pack(anchor="w")

        changes = list(entry.get("c") or ())
        sub_parts = [kind_label(entry.get("k"))]
        if changes:
            summary = _change_counts(changes)
            if summary:
                sub_parts.append(summary)
        elif entry.get("b", 0) <= 0:                    # pragma: no cover
            sub_parts.append("no change list recorded")
        if entry.get("b", 0) <= 0:
            # U-010: a Tweak or Undo run legitimately frees no bytes. Without
            # saying so, the empty right column read as a failure beside a
            # 4 GB Clean row - the same "0 means broken" confusion the run
            # history exists to prevent.
            sub_parts.append("freed no disk space")
        elif delta:
            # N-007: like-for-like with the previous run of this kind. Said as
            # a sentence, because "+1.2 GB" beside a 3 GB row reads as a
            # fourth number instead of a comparison.
            change = int(delta.get("bytes") or 0)
            noun = kind_label(entry.get("k")).lower()
            if change == 0:
                sub_parts.append("same as the previous %s" % noun)
            else:
                sub_parts.append(
                    "%s %s than the previous %s"
                    % (format_bytes(abs(change)),
                       "more" if change > 0 else "less", noun))
        sub = tk.Label(left, text=" \u00b7 ".join(sub_parts),
                       font=TYPE["caption"], bg=COLORS["bg_alt"],
                       fg=COLORS["subtext"], anchor="w", justify="left",
                       wraplength=self.body_width(30))
        sub.pack(anchor="w")

        if entry.get("b", 0) > 0:
            tk.Label(row, text=format_bytes(entry["b"]),
                     font=TYPE["label_medium"], bg=COLORS["bg_alt"],
                     fg=COLORS["accent_green"]).pack(side="right", padx=(SPACE_1, SPACE_2))
        row.pack(fill="x", pady=HAIRLINE_NUDGE)
        self._rows.append(row)

        if not changes:
            return
        detail = tk.Frame(parent, bg=COLORS["bg_alt"])
        for change in changes[:_CHANGE_LINES]:
            line = "   %s: %s" % (change.get("l", ""), change.get("d", ""))
            lbl = tk.Label(detail, text=line, font=TYPE["caption"],
                           bg=COLORS["bg_alt"], fg=COLORS["subtext"],
                           anchor="w", justify="left",
                           wraplength=self.body_width(26))
            lbl.pack(fill="x", padx=(INDENT, SPACE_2))
            path = change.get("p")
            if path:
                try:
                    Tooltip(lbl, path)
                except Exception:
                    pass
        extra = len(changes) - _CHANGE_LINES
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
            try:
                self._panel.refresh_scroll()
            except Exception:
                pass
            # The toplevel binds Return to Close; without this, expanding a
            # run from the keyboard would also shut the window (caught by the
            # keyboard test). "break" stops the event before that binding.
            return "break"

        for widget in (row, title, sub):
            widget.bind("<Button-1>", _toggle)
            try:
                widget.config(cursor="hand2")
            except Exception:
                pass

        # I-008: a row is one Tab stop. Return/Space toggles it, and the focus
        # ring is a highlight on the frame itself (children keep their own bg,
        # so recolouring the frame alone would be invisible).
        try:
            # Tk already draws `highlightcolor` while the widget has focus and
            # `highlightbackground` otherwise, so the ring needs no callbacks.
            row.configure(takefocus=1, highlightthickness=1,
                          highlightbackground=COLORS["bg_alt"],
                          highlightcolor=COLORS["accent_green"])
            row.bind("<Return>", _toggle)
            row.bind("<space>", _toggle)
            row.bind("<Button-1>", lambda _e: row.focus_set(), add="+")
        except Exception:
            pass
        row._detail = detail          # the smoke harness asserts on this

    @staticmethod
    def _kind_of(entry) -> str:
        kind = entry.get("k") if isinstance(entry, dict) else None
        return str(kind).strip().lower() if isinstance(kind, str) else "clean"

    def _set_filter(self, key):
        self._filter = str(key or "all")
        self._fill(self._entries)

    @staticmethod
    def _when(t):
        try:
            return time.strftime("%a %d %b \u00b7 %H:%M", time.localtime(t))
        except (OverflowError, OSError, ValueError):
            return "?"
