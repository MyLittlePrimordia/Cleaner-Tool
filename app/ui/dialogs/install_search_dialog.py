"""InstallSearchDialog — find an app without a permanent top search bar (U-001).

The Install catalog is nearly 200 apps across ten categories, but the old
search row was removed on purpose to reclaim vertical space. This dialog is
the compromise: a keyboard-invoked overlay that filters the existing catalog
through its own `_apply_search()` path, then disappears. The filter itself
stays applied after the dialog closes, so the user sees the narrowed list.

It owns no catalog data and no install logic. It only writes the tab's search
variable and asks the tab to repaint.
"""
from __future__ import annotations

import tkinter as tk

from app.ui.base import ThemedModal
from app.ui.theme import COLORS, SPACE_1, SPACE_2, TAB_ACCENTS, TYPE
from app.ui.widgets import AnimatedButton, SearchField


def _matches(query: str) -> int:
    """How many catalog/manual apps the current filter would show."""
    q = str(query or "").strip().lower()
    try:
        from app.app_catalog import APP_CATALOG, MANUAL_ONLY_APPS
    except Exception:
        return 0
    if not q:
        return len(APP_CATALOG) + len(MANUAL_ONLY_APPS)
    count = 0
    for app in list(APP_CATALOG) + list(MANUAL_ONLY_APPS):
        haystack = " ".join(str(app.get(k) or "") for k in
                            ("name", "id", "description", "hardware",
                             "category")).lower()
        if q in haystack:
            count += 1
    return count


class InstallSearchDialog(ThemedModal):
    """Live filter for the Install catalog; Enter closes, Escape cancels."""

    def __init__(self, parent, tab):
        self._tab = tab
        self._var = tk.StringVar(value=getattr(tab, "_search_var", None).get()
                                 if getattr(tab, "_search_var", None) else "")
        super().__init__(parent, title="Find an app",
                         accent=TAB_ACCENTS["Install"])
        body = self.body
        tk.Label(body, text="Filter the app catalog",
                 font=TYPE["label_large"], bg=COLORS["bg"],
                 fg=COLORS["text"], anchor="w").pack(
                     fill="x", pady=(0, SPACE_1))
        tk.Label(body,
                 text="Type a name, category or winget ID. The catalog "
                      "behind this window narrows as you type.",
                 font=TYPE["caption"], bg=COLORS["bg"],
                 fg=COLORS["subtext"], anchor="w", justify="left",
                 wraplength=self.body_width(28)).pack(
                     fill="x", pady=(0, SPACE_2))

        self._field = SearchField(
            body, textvariable=self._var,
            placeholder="Search apps, categories or winget IDs",
            accent=TAB_ACCENTS["Install"], on_change=self._on_change)
        self._field.pack(fill="x", pady=(0, SPACE_1))
        self._count_lbl = tk.Label(
            body, text="", font=TYPE["caption"], bg=COLORS["bg"],
            fg=COLORS["subtext"], anchor="w")
        self._count_lbl.pack(fill="x", pady=(0, SPACE_2))

        row = tk.Frame(body, bg=COLORS["bg"])
        row.pack(fill="x")
        AnimatedButton(row, text="Clear filter", command=self._clear,
                       role="text", size="sm").pack(side="left")
        AnimatedButton(row, text="Done", command=self.close,
                       role="filled", size="md",
                       accent=TAB_ACCENTS["Install"]).pack(side="right")

        try:
            self._dlg.bind("<Return>", lambda _e: self.close())
        except Exception:
            pass
        self._on_change(self._var.get())
        try:
            self._dlg.after(50, self._field.focus)
        except Exception:
            self._field.focus()

    # ------------------------------------------------------------- filter
    def _on_change(self, text):
        q = str(text or "")
        try:
            self._tab._search_var.set(q)
            self._tab._apply_search()
        except Exception:
            pass
        try:
            n = _matches(q)
            self._count_lbl.config(
                text=("%d app%s match" % (n, "" if n == 1 else "s"))
                if q.strip() else
                ("Browsing all %d apps" % n))
        except Exception:
            pass

    def _clear(self):
        try:
            self._field.clear()
        except Exception:
            self._var.set("")
        self._on_change("")
