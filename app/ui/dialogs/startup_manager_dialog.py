"""StartupManagerDialog — extracted verbatim from app/gui.py (H12, batch 4).

One dialog per module, mirroring app/ui/widgets. Depends only on
app.ui below it, plus the app.utils helpers it already used; it
knows nothing
about any other dialog except where noted.
"""
from __future__ import annotations

import tkinter as tk
from tkinter import ttk, messagebox
from app.ui.base import ThemedModal, Tooltip
from app.ui.theme import COLORS, F, TAB_ACCENTS, ICON_ROW_PX, icon_bg_rgb
from app.ui.widgets import AnimatedButton, ScrollableRoundedPanel, ToggleSwitch
from app.ui.tkdispatch import TkDispatcher

# Rows here are COLORS["bg_alt"]; the icon plate must match it exactly.
_icon_bg = icon_bg_rgb(COLORS["bg_alt"])


class StartupManagerDialog(ThemedModal):
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
    """

    def __init__(self, parent, app):
        self.app = app
        self._rows = {}     # item_id -> {"toggle": ToggleSwitch, "var": BooleanVar}
        # icon state: command -> PhotoImage, plus the labels still waiting on
        # an extraction, so a Refresh never re-extracts the same file.
        self._icon_cache = {}
        self._icon_waiters = {}
        self._icon_default_img = None
        super().__init__(parent, title="Startup Manager",
                         accent=TAB_ACCENTS["Tools"])
        body = self.body
        # One-way bridge onto the Tk thread. Icon extraction runs off-thread
        # (it is GDI work) and the swap has to happen on the Tk thread, so it
        # goes through a queue rather than calling .after() from the worker —
        # see app/ui/tkdispatch.py for why that call is not safe.
        self._dispatch = TkDispatcher(self._dlg)
        self._dispatch.start()

        tk.Label(body, text="Turn off apps that launch when Windows "
                            "starts, without uninstalling anything.",
                 font=(F, 10, "bold"), bg=COLORS["bg"], fg=COLORS["text"],
                 anchor="w").pack(fill="x", pady=(0, 2))
        tk.Label(body, text="Switch anything off here any time — nothing "
                            "is deleted, and you can turn it back on with "
                            "the same switch.",
                 font=(F, 9), bg=COLORS["bg"], fg=COLORS["subtext"],
                 anchor="w", wraplength=700).pack(fill="x", pady=(0, 12))

        self._panel = ScrollableRoundedPanel(body)
        self._panel.config(height=340)
        self._panel.pack(fill="both", expand=True)

        brow = tk.Frame(body, bg=COLORS["bg"])
        brow.pack(fill="x", pady=(10, 0))
        self._status = tk.Label(brow, text="", font=(F, 9), bg=COLORS["bg"],
                                fg=COLORS["subtext"], wraplength=560,
                                justify="left")
        self._status.pack(side="left", fill="x", expand=True)
        AnimatedButton(brow, text="Refresh", command=self._refresh,
                       bg=COLORS["surface"], fg=COLORS["text"],
                       font=(F, 9, "bold"), padx=16,
                       pady=6).pack(side="right", padx=(8, 0))
        AnimatedButton(brow, text="Close", command=self.close,
                       bg=TAB_ACCENTS["Tools"], fg=COLORS["black"],
                       font=(F, 9, "bold"), padx=20,
                       pady=6).pack(side="right")

        self._refresh()

    def _say(self, text):
        try:
            self._status.config(text=text)
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
        try:
            from app.startup_manager import list_startup_items
            items = list_startup_items()
        except Exception:
            items = []
        if not items:
            tk.Label(holder, text="Nothing found — or this PC couldn't be "
                                  "checked.", font=(F, 9), bg=COLORS["bg_alt"],
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
        self._say(f"{n_on} of {len(items)} start with Windows.")

    def _build_row(self, parent, item, admin):
        row = tk.Frame(parent, bg=COLORS["bg_alt"])
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
        icon_lbl = tk.Label(row, bg=COLORS["bg_alt"])
        icon_lbl.pack(side="left", padx=(0, 10), pady=8)
        self._set_row_icon(icon_lbl, item)
        text = tk.Frame(row, bg=COLORS["bg_alt"])
        text.pack(side="left", fill="x", expand=True)
        name_row = tk.Frame(text, bg=COLORS["bg_alt"])
        name_row.pack(fill="x")
        tk.Label(name_row, text=item["name"], font=(F, 9, "bold"),
                 bg=COLORS["bg_alt"], fg=COLORS["text"],
                 anchor="w").pack(side="left")
        if needs_admin:
            shield = tk.Label(name_row, text="🛡️", font=(F, 8),
                              bg=COLORS["bg_alt"])
            shield.pack(side="left", padx=(6, 0))
            Tooltip(shield, "Restart Cleaner Tool as Administrator to "
                           "change this one")
        tk.Label(text, text=item["source_label"], font=(F, 8),
                 bg=COLORS["bg_alt"], fg=COLORS["subtext"],
                 anchor="w").pack(fill="x")
        row.pack(fill="x", pady=2)
        self._rows[item["id"]] = {"toggle": toggle, "var": var, "row": row}

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
