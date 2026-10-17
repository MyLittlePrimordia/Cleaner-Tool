"""StartupManagerDialog — extracted verbatim from app/gui.py (H12, batch 4).

One dialog per module, mirroring app/ui/widgets. Depends only on
app.ui below it, plus the app.utils helpers it already used; it
knows nothing
about any other dialog except where noted.
"""
from __future__ import annotations

import tkinter as tk
from tkinter import ttk, messagebox
from app.ui.base import Tooltip
from app.ui.dialogs._tool_page import ToolPage
from app.ui.theme import COLORS, F, TAB_ACCENTS, ICON_ROW_PX, icon_bg_rgb
from app.ui.widgets import AnimatedButton, ScrollableRoundedPanel, ToggleSwitch
from app.ui.tkdispatch import TkDispatcher

# Rows sit directly on the page now (no per-row card fill), so the icon
# plate must match the PAGE colour exactly - otherwise every app icon shows
# the opaque square the extractor composites onto.
_icon_bg = icon_bg_rgb(COLORS["bg"])


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
        self.add_button("Refresh", self._refresh, kind="secondary")

        self._refresh()

    def _say(self, text):
        try:
            self.status.stop(text)
        except Exception:
            pass

    def _refresh(self):
        holder = self._panel.inner
        try:
            for w in list(holder.winfo_children()):
                w.destroy()
        except Exception:
            return
        self._rows = {}
        # BUGFIX 2026-10: the exception was swallowed into `items = []`, which
        # is the same value a genuinely-empty machine produces. Both rendered
        # one string - "Nothing found - or this PC couldn't be checked." - so
        # a user whose read failed was told, with equal confidence, that they
        # might have nothing, and had no way to tell the two apart or to retry
        # a read that had not actually happened.
        err = None
        try:
            from app.startup_manager import list_startup_items
            items = list_startup_items()
        except Exception as exc:
            items, err = None, str(exc)
        if err is not None:
            self._paint_error(holder, err)
            return
        if not items:
            tk.Label(holder, text="Nothing is set to start with Windows.",
                     font=(F, 9), bg=COLORS["bg"],
                     fg=COLORS["subtext"]).pack(pady=20)
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
                     font=(F, 10, "bold"), bg=COLORS["bg"],
                     fg=COLORS["accent_yellow"],
                     bd=0, highlightthickness=0).pack(pady=(22, 2))
            tk.Label(holder, text=short, font=(F, 9), bg=COLORS["bg"],
                     fg=COLORS["subtext"], wraplength=self.body_width(28),
                     justify="center", bd=0, highlightthickness=0).pack(pady=(0, 4))
            tk.Label(holder, text="Nothing was changed.",
                     font=(F, 9), bg=COLORS["bg"], fg=COLORS["subtext"],
                     bd=0, highlightthickness=0).pack()
            AnimatedButton(
                holder, text="Try again", command=self._refresh,
                bg=COLORS["surface"], fg=COLORS["text"],
                font=(F, 9, "bold"), padx=16, pady=6).pack(pady=(12, 20))
        except Exception:
            pass
        try:
            self._panel.refresh_scroll()
        except Exception:
            pass

    def _build_row(self, parent, item, admin):
        # AUDIT D2: this dialog predates ToolShell, so its rows never got
        # ToolRow._fit_title's ellipsis. Both labels below packed a name and
        # a source_label (often a full command line - "C:\Program Files\...\app.exe
        # --background") with no wraplength and no truncation, so pack sheared
        # them MID-CHARACTER. That reads as a rendering fault rather than as
        # truncation. Widths come from the row's own column, re-measured on
        # <Configure>, so they follow the window instead of a literal.
        row = tk.Frame(parent, bg=COLORS["bg"])
        var = tk.BooleanVar(value=item["enabled"])
        needs_admin = item["admin_required"] and not admin
        toggle = ToggleSwitch(
            row, variable=var,
            command=lambda it=item, v=var: self._on_toggle(it, v))
        toggle.pack(side="left", padx=(10, 8), pady=8)
        if needs_admin:
            try:
                toggle.set_enabled(False)
            except Exception:
                pass
        # Per-app icon, between the switch and the name. Startup rows come
        # in two shapes and both resolve: a Run value that is a command line
        # pointing at an exe, and a Startup-folder .lnk shortcut (resolved by
        # parsing the shell link). There was no icon here at all before.
        icon_lbl = tk.Label(row, bg=COLORS["bg"])
        icon_lbl.pack(side="left", padx=(0, 10), pady=8)
        self._set_row_icon(icon_lbl, item)
        text = tk.Frame(row, bg=COLORS["bg"])
        text.pack(side="left", fill="x", expand=True)
        name_row = tk.Frame(text, bg=COLORS["bg"])
        name_row.pack(fill="x")
        name_lbl = tk.Label(name_row, text=item["name"], font=(F, 9, "bold"),
                            bg=COLORS["bg"], fg=COLORS["text"],
                            anchor="w", justify="left", bd=0)
        name_lbl.pack(side="left", fill="x", expand=True)
        # The full text is never lost: it is the tooltip on both labels.
        Tooltip(name_lbl, item["name"])
        if needs_admin:
            shield = tk.Label(name_row, text="🛡️", font=(F, 9),
                              bg=COLORS["bg"])
            shield.pack(side="right", padx=(6, 0))
            Tooltip(shield, "Restart Cleaner Tool as Administrator to "
                           "change this one")
        src_lbl = tk.Label(text, text=item["source_label"], font=(F, 9),
                           bg=COLORS["bg"], fg=COLORS["subtext"],
                           anchor="w", justify="left", bd=0)
        src_lbl.pack(fill="x")
        Tooltip(src_lbl, item["source_label"])
        row.pack(fill="x", pady=2)
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
                _ppm = generic_app_icon_ppm(ICON_ROW_PX, _icon_bg)
                if _ppm is None:
                    from app.icon_extract import default_icon_ppm
                    _ppm = default_icon_ppm(ICON_ROW_PX, _icon_bg)
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
                ppm = icon_ppm_bytes(spec, size=ICON_ROW_PX, bg_rgb=_icon_bg)
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
        self._say(msg)
