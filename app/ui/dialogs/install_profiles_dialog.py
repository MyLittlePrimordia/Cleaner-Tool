"""InstallProfilesDialog — extracted verbatim from app/gui.py (H12, batch 1).

One dialog per module, mirroring app/ui/widgets. Depends only on
app.ui below it, plus the app.utils helpers it already used; it
knows nothing
about any other dialog except where noted.
"""
from __future__ import annotations

import tkinter as tk
from tkinter import ttk, messagebox
from app.ui.base import ThemedModal
from app.ui.theme import COLORS, F, TAB_ACCENTS
from app.ui.widgets import AnimatedButton, RoundedEntry, ScrollableRoundedPanel


class InstallProfilesDialog(ThemedModal):
    """"My Setups" (feature 7): save the current Install picks under a
    friendly name and put them back in one click.

    Aimed squarely at the "I just reinstalled Windows" moment — the user
    ticks their usual apps once, names the set, and next time it is two
    clicks instead of hunting through the catalog again. Profiles store
    ONLY ids (winget ids for catalog apps, "task:<key>" for Essentials
    and bundles) so nothing about the machine is written down.

    Loading replaces the current selection rather than adding to it —
    "My Setup" should mean exactly that set, not that set plus whatever
    was already ticked.
    """

    _NAME_MAX = 40

    def __init__(self, parent, tab):
        self._tab = tab
        self._rows_holder = None
        super().__init__(parent, title="My Setups",
                         accent=TAB_ACCENTS["Install"])
        body = self.body

        tk.Label(body, text="Save the apps you have ticked, and put them "
                            "back any time.",
                 font=(F, 10, "bold"), bg=COLORS["bg"], fg=COLORS["text"],
                 anchor="w").pack(fill="x", pady=(0, 2))
        tk.Label(body, text="Handy after a fresh Windows install — tick your "
                            "usual apps once, save them, load them next time.",
                 font=(F, 9), bg=COLORS["bg"], fg=COLORS["subtext"],
                 anchor="w", wraplength=700, justify="left").pack(
                     fill="x", pady=(0, 14))

        # --- save row ---
        save_box = tk.Frame(body, bg=COLORS["bg_alt"])
        save_box.pack(fill="x", pady=(0, 14))
        inner = tk.Frame(save_box, bg=COLORS["bg_alt"])
        inner.pack(fill="x", padx=14, pady=12)
        n_now = self._current_count()
        self._save_hint = tk.Label(
            inner, text=self._save_hint_text(n_now), font=(F, 9),
            bg=COLORS["bg_alt"], fg=COLORS["subtext"], anchor="w")
        self._save_hint.pack(fill="x", pady=(0, 6))
        entry_row = tk.Frame(inner, bg=COLORS["bg_alt"])
        entry_row.pack(fill="x")
        self._name_var = tk.StringVar(value="")
        self._entry = RoundedEntry(entry_row, textvariable=self._name_var,
                                   width=26, accent=TAB_ACCENTS["Install"])
        self._entry.pack(side="left")
        self._save_btn = AnimatedButton(
            entry_row, text="Save", command=self._save,
            bg=TAB_ACCENTS["Install"], fg=COLORS["black"],
            font=(F, 9, "bold"), padx=20, pady=7)
        self._save_btn.pack(side="left", padx=(8, 0))
        self._save_btn.set_enabled(bool(n_now))
        try:
            self._entry.entry.bind("<Return>", lambda _e: self._save())
        except Exception:
            pass

        # --- saved list ---
        tk.Label(body, text="Saved setups", font=(F, 9, "bold"),
                 bg=COLORS["bg"], fg=COLORS["subtext"],
                 anchor="w").pack(fill="x", pady=(0, 4))
        self._panel = ScrollableRoundedPanel(body)
        self._panel.config(height=210)
        self._panel.pack(fill="x")
        self._rows_holder = self._panel.inner

        self._status = tk.Label(body, text="", font=(F, 9), bg=COLORS["bg"],
                                fg=COLORS["subtext"])
        self._status.pack(pady=(10, 0))

        brow = tk.Frame(body, bg=COLORS["bg"])
        brow.pack(fill="x", pady=(12, 0))
        AnimatedButton(brow, text="Close", command=self.close,
                       bg=TAB_ACCENTS["Install"], fg=COLORS["black"],
                       font=(F, 9, "bold"), padx=22,
                       pady=7).pack(side="right")

        self._refresh_rows()

    # ---- helpers -------------------------------------------------- #

    @staticmethod
    def _save_hint_text(n):
        if not n:
            return "Nothing is ticked right now — tick some apps first."
        return "You have %d app%s ticked. Give this set a name:" % (
            n, "" if n == 1 else "s")

    def _current_count(self):
        try:
            return len(self._tab.profile_ids())
        except Exception:
            return 0

    def _say(self, text):
        try:
            self._status.config(text=text)
        except Exception:
            pass

    def _refresh_rows(self):
        """Rebuild the saved-setup list from config."""
        holder = self._rows_holder
        if holder is None:
            return
        try:
            for w in list(holder.winfo_children()):
                try:
                    w.destroy()
                except Exception:
                    pass
        except Exception:
            return
        try:
            from app.config_persist import get_install_profiles
            profiles = get_install_profiles()
        except Exception:
            profiles = {}
        if not profiles:
            tk.Label(holder, text="No saved setups yet.", font=(F, 9),
                     bg=COLORS["bg_alt"], fg=COLORS["subtext"]).pack(pady=16)
        else:
            for name in sorted(profiles, key=lambda n: n.lower()):
                self._build_row(holder, name, profiles[name])
        try:
            self._panel.refresh_scroll()
        except Exception:
            pass

    def _build_row(self, parent, name, ids):
        row = tk.Frame(parent, bg=COLORS["bg_alt"])
        tk.Label(row, text=name, font=(F, 10, "bold"), bg=COLORS["bg_alt"],
                 fg=COLORS["text"], anchor="w").pack(side="left", padx=(10, 6),
                                                     pady=7)
        n = len(ids or [])
        tk.Label(row, text="%d app%s" % (n, "" if n == 1 else "s"),
                 font=(F, 9), bg=COLORS["bg_alt"],
                 fg=COLORS["subtext"]).pack(side="left")
        AnimatedButton(row, text="Delete",
                       command=lambda nm=name: self._delete(nm),
                       bg=COLORS["surface"], fg=COLORS["subtext"],
                       font=(F, 9, "bold"), padx=12,
                       pady=5).pack(side="right", padx=(6, 10), pady=5)
        AnimatedButton(row, text="Load",
                       command=lambda nm=name, i=list(ids or []): self._load(nm, i),
                       bg=TAB_ACCENTS["Install"], fg=COLORS["black"],
                       font=(F, 9, "bold"), padx=16,
                       pady=5).pack(side="right", pady=5)
        row.pack(fill="x", pady=2)

    # ---- actions -------------------------------------------------- #

    def _save(self):
        name = (self._name_var.get() or "").strip()[:self._NAME_MAX]
        if not name:
            self._say("Type a name first — for example 'My Setup'.")
            return
        try:
            ids = self._tab.profile_ids()
        except Exception:
            ids = []
        if not ids:
            self._say("Nothing is ticked, so there is nothing to save.")
            return
        try:
            from app.config_persist import save_install_profile
            ok = save_install_profile(name, ids)
        except Exception:
            ok = False
        if ok:
            self._name_var.set("")
            self._refresh_rows()
            self._say("Saved '%s'." % name)
        else:
            # The only refusal the store raises is the 20-profile cap;
            # anything else has already degraded to False the same way.
            self._say("Couldn't save — you may already have 20 setups. "
                      "Delete one and try again.")

    def _load(self, name, ids):
        try:
            applied, missing = self._tab.apply_profile_ids(ids)
        except Exception:
            self._say("Couldn't load that setup.")
            return
        if missing:
            self._say("Loaded '%s' — %d ticked, %d are no longer in the "
                      "catalog." % (name, applied, missing))
        else:
            self._say("Loaded '%s' — %d app%s ticked." %
                      (name, applied, "" if applied == 1 else "s"))
        try:
            n_now = self._current_count()
            self._save_hint.config(text=self._save_hint_text(n_now))
            self._save_btn.set_enabled(bool(n_now))
        except Exception:
            pass

    def _delete(self, name):
        try:
            from app.config_persist import delete_install_profile
            delete_install_profile(name)
        except Exception:
            pass
        self._refresh_rows()
        self._say("Deleted '%s'." % name)
