"""StartupManagerDialog — extracted verbatim from app/gui.py (H12, batch 4).

One dialog per module, mirroring app/ui/widgets. Depends only on
app.ui below it, plus the app.utils helpers it already used; it
knows nothing
about any other dialog except where noted.
"""
from __future__ import annotations

import tkinter as tk

from app import facts
from app.ui.base import Tooltip
from app.ui.dialogs._tool_page import ToolPage
from app.ui.theme import (
    COLORS, ICON_ROW, TAB_ACCENTS, TYPE, icon_bg_rgb, icon_target_px,
    SPACE_1, SPACE_2, SPACE_3,
)
from app.ui.metrics import HERO_OFFSET, PAGE_INSET, SECTION_BREAK, TEXT_NUDGE
from app.ui.widgets import (AnimatedButton, EmptyState, ScrollableRoundedPanel,
                            ToggleSwitch, ToolRow)
from app.ui.tkdispatch import TkDispatcher

# Rows are ToolRows, so their face is the shared `bg_alt` row fill. The icon
# plate has to match THAT colour exactly - otherwise every app icon shows the
# opaque square the extractor composites onto.
_icon_bg = icon_bg_rgb(COLORS["bg_alt"])


class StartupManagerDialog(ToolPage):
    """Startup Manager (Tools tab card): everything that launches at
    sign-in, with one toggle each.

    Covers Registry Run keys and Startup-folder shortcuts (see
    app/startup_manager.py for why Task Scheduler is deliberately left
    out). Each row shows a plain sentence of WHERE the item runs from
    rather than a registry path, since the audience is non-technical.

    Every toggle is instantly reversible — disabling never deletes
    anything, it moves the original data into this app's own storage,
    and the row's own switch flips it right back. HKLM / shared-startup
    rows show a small shield + are disabled outright when not running
    as Administrator, the same visual language Install/Custom already
    use for admin-gated rows.

    REDESIGN 2026-10: on the shared ToolPage shell — the two intro
    labels are one Header banner, and the footer is the shell's
    StatusLine + button row (Close primary, Refresh secondary).
    """

    def __init__(self, parent, app):
        self.app = app
        self._rows = {}     # item_id -> {"toggle": ToggleSwitch, "var": BooleanVar}
        #: The last successful read, so a toggle can republish the enabled
        #: count without re-running the registry walk.
        self._items = []
        self._wrap_bound = False
        # icon state: command -> PhotoImage, plus the labels still waiting on
        # an extraction, so a Refresh never re-extracts the same file.
        self._icon_cache = {}
        self._icon_waiters = {}
        self._icon_default_img = None
        super().__init__(parent, title="Startup Manager",
                         accent=TAB_ACCENTS["Tools"],
                         scope="Nothing is deleted — every switch turns "
                               "back on.",
                         scroll=False)
        self._icon_px = icon_target_px(ICON_ROW, self)
        body = self.content
        self.banner.set_state("\U0001F680", TAB_ACCENTS["Tools"])
        self.banner.set_title("What starts with Windows?")
        self.banner.set_sub("Turn apps on or off at boot.")
        # One-way bridge onto the Tk thread. Icon extraction runs off-thread
        # (it is GDI work) and the swap has to happen on the Tk thread, so it
        # goes through a queue rather than calling .after() from the worker —
        # see app/ui/tkdispatch.py for why that call is not safe.
        self._dispatch = TkDispatcher(self._dlg)
        self._dispatch.start()

        self._panel = ScrollableRoundedPanel(body)
        self._panel.config(height=340)
        self._panel.pack(fill="both", expand=True)

        # REDESIGN 2026-10: shell footer — Close is the one primary action
        # (it was the accent-filled button before), Refresh is secondary.
        # _say() now writes to the shared StatusLine.
        self.add_button("Close", self.close, kind="primary")
        # An explicit Refresh must re-read, not replay the fact store's
        # five-minute cache; opening the dialog may use the cache.
        self.add_button("Refresh", lambda: self._refresh(force=True),
                        kind="secondary")

        self._refresh()

    def _say(self, text):
        try:
            self.status.stop(text)
        except Exception:
            pass

    def _refresh(self, force=False):
        holder = self._panel.inner
        try:
            for w in list(holder.winfo_children()):
                w.destroy()
        except Exception:
            return
        self._rows = {}
        # BUGFIX 2026-10: a failed read must never look like an empty
        # machine. AUDIT TX-001: the read is the shared `system.startup`
        # fact, so a failure keeps its reason (`ok=False`) instead of
        # collapsing into the same `[]` a clean README would produce.
        fact = facts.get("system.startup", max_age=0 if force else None)
        if not fact.ok:
            self._paint_error(holder, fact.error or "unknown error")
            return
        items = fact.value or []
        self._items = list(items)
        self._publish_finding()
        if not items:
            EmptyState(
                holder, glyph="\U0001F31E",
                text="Nothing is set to start with Windows.",
                wrap=max(240, self.body_width(24))).pack(
                    fill="x", pady=(PAGE_INSET, SECTION_BREAK))
            try:
                self._panel.refresh_scroll()
            except Exception:
                pass
            return
        from app.elevation import is_admin
        admin = is_admin()
        for it in items:
            self._build_row(holder, it, admin)
        try:
            self._panel.refresh_scroll()
        except Exception:
            pass
        n_on = sum(1 for i in items if i["enabled"])
        self._say("%d of %d start with Windows." % (n_on, len(items)))
        self._bind_wrap()
        # U-002: the measured cost arrives after the list, off the Tk thread.
        # Rows appear immediately with no number; they gain one when Windows'
        # own boot events have been read. A slow read never delays the list.
        self._load_boot_perf()

    def _load_boot_perf(self):
        """Read last-boot duration + per-app startup cost. Never blocks Tk."""
        def work():
            from app import boot_perf
            return boot_perf.summary()

        self.run_worker(work, self._land_boot_perf)

    def _land_boot_perf(self, summary):
        from app import boot_perf

        self._boot_perf = summary or {}
        if not summary or not summary.get("ok"):
            # Not measured is said, never rendered as "all good".
            try:
                self.banner.set_sub(
                    "Windows did not record boot timings (needs administrator); "
                    "impact is not measured.")
            except Exception:
                pass
            return
        last = boot_perf.format_ms(summary.get("last_ms"))
        avg = boot_perf.format_ms(summary.get("average_ms"))
        count = len(summary.get("boots") or [])
        try:
            self.banner.set_sub(
                "Last boot %s \u00b7 average %s over %d recorded boot%s. "
                "Impact is Windows' own measurement."
                % (last, avg, count, "" if count == 1 else "s"))
        except Exception:
            pass
        impact = summary.get("impact") or {}
        for item in self._items:
            hit = boot_perf.impact_for(item, impact)
            if not hit:
                continue
            slot = (self._rows.get(item.get("id")) or {}).get("row")
            if slot is None:
                continue
            try:
                slot.meta.config(text="%s at boot" % boot_perf.format_ms(
                    hit.get("ms")))
                Tooltip(slot.meta, "Windows measured this entry adding %s to "
                                   "startup and flagged it as a slowdown."
                                   % boot_perf.format_ms(hit.get("ms")))
            except Exception:
                pass

    def _publish_finding(self):
        """Carry the enabled-startup count to Health and the Tools hub."""
        try:
            from app.startup_manager import publish_startup_finding
            publish_startup_finding(self._items)
        except Exception:
            pass

    def _paint_error(self, holder, detail):
        """A failed read, shown as a failed read.

        Distinct from the empty state on purpose. "Nothing is set to start
        with Windows" is a confident claim about the machine; a failed read is
        a statement about this tool. Showing the former after the latter is
        how users end up disabling things that were working.
        """
        short = (detail or "").strip().splitlines()[0][:140] or "unknown error"
        try:
            tk.Label(holder, text="Couldn't read your startup items.",
                     font=TYPE["label_large"], bg=COLORS["bg"],
                     fg=COLORS["accent_yellow"],
                     bd=0, highlightthickness=0).pack(pady=(HERO_OFFSET, TEXT_NUDGE))
            tk.Label(holder, text=short, font=TYPE["caption"], bg=COLORS["bg"],
                     fg=COLORS["subtext"], wraplength=self.body_width(28),
                     justify="center", bd=0,
                     highlightthickness=0).pack(pady=(0, SPACE_1))
            tk.Label(holder, text="Nothing was changed.",
                     font=TYPE["caption"], bg=COLORS["bg"], fg=COLORS["subtext"],
                     bd=0, highlightthickness=0).pack()
            AnimatedButton(
                holder, text="Try again",
                command=lambda: self._refresh(force=True),
                role="secondary", size="sm").pack(pady=(SPACE_3, SECTION_BREAK))
        except Exception:
            pass
        try:
            self._panel.refresh_scroll()
        except Exception:
            pass

    def _build_row(self, parent, item, admin):
        # AUDIT D2 (fixed for good here): this dialog used to hand-pack a
        # name and a source_label - often a full command line - with no
        # truncation, so pack sheared them MID-CHARACTER. ToolRow owns the
        # anatomy now: leading slot for the icon, title + second line,
        # actions slot for the switch. ToolRow._fit_title ellipsizes the name
        # and the whole command line stays on the tooltip.
        needs_admin = item["admin_required"] and not admin
        var = tk.BooleanVar(value=item["enabled"])
        row = ToolRow(parent, title=item["name"], sub=item["source_label"],
                      size="lg", leading_px=self._icon_px + 4,
                      wrap=max(200, self.body_width(26)))
        toggle = ToggleSwitch(
            row.actions, variable=var,
            command=lambda it=item, v=var: self._on_toggle(it, v))
        toggle.pack(side="right", padx=(SPACE_2, 0))
        if needs_admin:
            try:
                toggle.set_enabled(False)
            except Exception:
                pass
            shield = tk.Label(row.actions, text="🛡️", font=TYPE["caption"],
                              bg=COLORS["bg_alt"])
            shield.pack(side="right", padx=(SPACE_2, 0))
            Tooltip(shield, "Restart Cleaner Tool as Administrator to "
                           "change this one")
        # Per-app icon in the row's leading slot. Startup rows come in two
        # shapes and both resolve: a Run value that is a command line
        # pointing at an exe, and a Startup-folder .lnk shortcut.
        icon_lbl = tk.Label(row.leading, bg=COLORS["bg_alt"])
        icon_lbl.pack(fill="both", expand=True)
        self._set_row_icon(icon_lbl, item)
        # The full text is never lost: it is the tooltip on both labels.
        Tooltip(row.title, item["name"])
        Tooltip(row.sub, item["source_label"])
        row.bind_content()
        row.pack(fill="x", pady=TEXT_NUDGE)
        self._rows[item["id"]] = {"toggle": toggle, "var": var, "row": row}

    def _bind_wrap(self):
        """Bind the row-text wrapper to the panel's width, once.

        On the PANEL rather than the dialog: the dialog's Configure fires
        before the scroll panel has been given its width, so a handler bound
        there measures 1px and bails, and the panel's Configure arrives too
        late to be caught by anything bound later.
        """
        if self._wrap_bound:
            return
        try:
            self._panel.inner.bind("<Configure>",
                                   self._on_panel_resize, add="+")
            self._wrap_bound = True
        except Exception:
            pass

    def _on_panel_resize(self, _event=None):
        self._wrap_rows()

    def _wrap_rows(self, _event=None):
        """Bound the row text to the panel's width, and let Tk do the work.

        AUDIT D2: this dialog predates ToolShell, so its rows never got
        ToolRow's ellipsis, and pack sheared a long name - or a full command
        line in source_label - MID-CHARACTER, which reads as a rendering fault
        rather than as truncation. The full text is on both tooltips.

        WHY wraplength AND NOT theme.ellipsize(). Four mechanisms were tried
        and measured before landing here, and every one of them failed:

          * ellipsize on <Configure> of the ROW  - a row reaches its final
            width one pass before the column inside it, so it always measured
            1px and bailed. 10 attempts, nothing truncated.
          * ellipsize on <Configure> of the COLUMN - the column is packed
            fill/expand, so before the panel is laid out its width TRACKS its
            contents: truncating shrank the column, which shrank the room,
            which truncated further. Room fell 470 -> 346 -> 247 -> ... -> 56
            then jumped to 428, looping indefinitely (6450 iterations in ten
            seconds, measured).
          * an after_idle retry loop, and then a re-entrancy flag - both hung
            the dialog's CONSTRUCTOR outright; the test process had to be
            killed both times.

        wraplength breaks the loop because it is a HINT to Tk's own layout,
        not a text rewrite driven by a measurement: Tk computes the wrap from
        the width pack already gave the row, so there is no feedback path from
        the text back into the width. Tk then wraps to a second line instead of
        shearing, and the row grows to fit.
        """
        try:
            wrap = self._panel.inner.winfo_width() - 130
        except Exception:
            return
        if wrap < 60:
            return                      # not laid out yet; Configure returns
        for w in self._walk(self._panel.inner):
            try:
                if w.winfo_class() == "Label":
                    w.config(wraplength=wrap)
            except Exception:
                pass

    @staticmethod
    def _walk(widget):
        stack = [widget]
        while stack:
            cur = stack.pop()
            yield cur
            try:
                stack.extend(cur.winfo_children())
            except Exception:
                pass

    def _icon_default_photo(self):
        if self._icon_default_img is None:
            try:
                from app.icon_resolve import generic_app_icon_ppm
                _ppm = generic_app_icon_ppm(self._icon_px, _icon_bg)
                if _ppm is None:
                    from app.icon_extract import default_icon_ppm
                    _ppm = default_icon_ppm(self._icon_px, _icon_bg)
                self._icon_default_img = tk.PhotoImage(data=_ppm)
            except Exception:
                self._icon_default_img = None
        return self._icon_default_img

    def _set_row_icon(self, label, item):
        """Generic glyph now, real icon once the off-thread extraction lands."""
        ph = self._icon_default_photo()
        if ph is not None:
            label.config(image=ph)
            label.image = ph
        try:
            from app.icon_resolve import icon_source_for_command
            spec = icon_source_for_command(item.get("command") or "")
        except Exception:
            spec = None
        if not spec:
            return
        cached = self._icon_cache.get(spec)
        if cached is not None:
            label.config(image=cached)
            label.image = cached
            return
        waiters = self._icon_waiters.setdefault(spec, [])
        waiters.append(label)
        if len(waiters) > 1:
            return          # already in flight for this file

        def work():
            ppm = None
            try:
                from app.icon_extract import icon_ppm_bytes
                ppm = icon_ppm_bytes(spec, size=self._icon_px, bg_rgb=_icon_bg)
            except Exception:
                ppm = None

            def apply():
                pending = self._icon_waiters.pop(spec, [])
                photo = None
                if ppm:
                    try:
                        photo = tk.PhotoImage(data=ppm)
                        self._icon_cache[spec] = photo
                    except Exception:
                        photo = None
                if photo is None:
                    return          # no icon resource; the glyph stays
                for lbl in pending:
                    try:
                        if lbl.winfo_exists():
                            lbl.config(image=photo)
                            lbl.image = photo
                    except Exception:
                        pass

            self._dispatch.post(apply)

        import threading
        threading.Thread(target=work, daemon=True).start()

    def _on_toggle(self, item, var):
        wanted = var.get()
        try:
            from app.startup_manager import set_item_enabled
            ok, msg = set_item_enabled(item["id"], wanted)
        except Exception as exc:
            ok, msg = False, str(exc)
        if not ok:
            # Snap the switch back — the write didn't happen, so the UI
            # must not claim it did.
            try:
                var.set(not wanted)
            except Exception:
                pass
        else:
            # The cached list still says the old state. Invalidate rather
            # than re-read here: the row already paints the change, and the
            # next tool that asks gets a real measurement.
            facts.invalidate("system.startup")
            # Keep this dialog's own copy honest too, so the finding reflects
            # the switch the user just flipped instead of the pre-toggle list.
            item["enabled"] = wanted
            self._publish_finding()
        self._say(msg)
