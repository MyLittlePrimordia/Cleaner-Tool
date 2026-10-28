"""InstallProfilesDialog — extracted verbatim from app/gui.py (H12, batch 1).

One dialog per module, mirroring app/ui/widgets. Depends only on
app.ui below it, plus the app.utils helpers it already used; it
knows nothing
about any other dialog except where noted.
"""
from __future__ import annotations

import tkinter as tk
from app.ui.base import ThemedModal, _themed_askyesno
from app.ui.theme import COLORS, TAB_ACCENTS, TYPE, SPACE_1, SPACE_2, SPACE_3
from app.ui.metrics import CARD_BODY_INSET, CONTROL_GAP, PAGE_INSET, TEXT_NUDGE
from app.ui.widgets import (AnimatedButton, EmptyState, ModalFooter,
                            RoundedEntry, ScrollableRoundedPanel, ToolRow)


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
                 font=TYPE["label_large"], bg=COLORS["bg"], fg=COLORS["text"],
                 anchor="w").pack(fill="x", pady=(0, TEXT_NUDGE))
        tk.Label(body, text="Handy after a fresh Windows install — tick your "
                            "usual apps once, save them, load them next time.",
                 font=TYPE["caption"], bg=COLORS["bg"], fg=COLORS["subtext"],
                 anchor="w", wraplength=self.body_width(28), justify="left").pack(
                     fill="x", pady=(0, CARD_BODY_INSET))

        # --- save row ---
        save_box = tk.Frame(body, bg=COLORS["bg_alt"])
        save_box.pack(fill="x", pady=(0, CARD_BODY_INSET))
        inner = tk.Frame(save_box, bg=COLORS["bg_alt"])
        inner.pack(fill="x", padx=CARD_BODY_INSET, pady=SPACE_3)
        n_now = self._current_count()
        self._save_hint = tk.Label(
            inner, text=self._save_hint_text(n_now), font=TYPE["caption"],
            bg=COLORS["bg_alt"], fg=COLORS["subtext"], anchor="w")
        self._save_hint.pack(fill="x", pady=(0, CONTROL_GAP))
        entry_row = tk.Frame(inner, bg=COLORS["bg_alt"])
        entry_row.pack(fill="x")
        self._name_var = tk.StringVar(value="")
        self._entry = RoundedEntry(entry_row, textvariable=self._name_var,
                                   width=26, accent=TAB_ACCENTS["Install"])
        self._entry.pack(side="left")
        self._save_btn = AnimatedButton(
            entry_row, text="Save", command=self._save,
            role="filled", size="sm", accent=TAB_ACCENTS["Install"])
        self._save_btn.pack(side="left", padx=(SPACE_2, 0))
        self._save_btn.set_enabled(bool(n_now))
        try:
            self._entry.entry.bind("<Return>", lambda _e: self._save())
        except Exception:
            pass

        # --- saved list ---
        tk.Label(body, text="Saved setups", font=TYPE["label_medium"],
                 bg=COLORS["bg"], fg=COLORS["subtext"],
                 anchor="w").pack(fill="x", pady=(0, SPACE_1))
        self._panel = ScrollableRoundedPanel(body)
        self._panel.config(height=210)
        self._panel.pack(fill="x")
        self._rows_holder = self._panel.inner

        # Status and Close share the one footer: the label used to float
        # above a separate button row, which is a second bottom bar invented
        # for this dialog alone. It now lives in the footer's hint slot, in
        # the same place ToolPage puts its status line.
        self._footer = ModalFooter(body, accent=TAB_ACCENTS["Install"])
        self._footer.pack_footer()
        self._status = tk.Label(self._footer.hint_slot, text="",
                                font=TYPE["caption"], bg=COLORS["bg"],
                                fg=COLORS["subtext"], anchor="w")
        self._status.pack(side="left", fill="x", expand=True)
        self._footer.button("Close", self.close, kind="primary")

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
            EmptyState(holder, glyph="\u2606", text="No saved setups yet.",
                       detail="Name a set of ticked apps and it lands here.",
                       bg=COLORS["bg_alt"],
                       wrap=max(240, self.body_width(26))).pack(
                           fill="x", pady=(CARD_BODY_INSET, PAGE_INSET))
        else:
            for name in sorted(profiles, key=lambda n: n.lower()):
                self._build_row(holder, name, profiles[name])
        try:
            self._panel.refresh_scroll()
        except Exception:
            pass

    def _build_row(self, parent, name, ids):
        """One saved set: a ToolRow with its two actions in the action slot."""
        n = len(ids or [])
        row = ToolRow(parent, title=name,
                      sub="%d app%s" % (n, "" if n == 1 else "s"),
                      size="md", show_dot=False)
        # Right-to-left: the first packed action is the rightmost, so Delete
        # sits at the outer edge and Load immediately to its left - the same
        # order every other row's actions use.
        AnimatedButton(row.actions, text="Delete",
                       command=lambda nm=name, c=n: self._delete(nm, c),
                       role="text", size="sm").pack(side="right",
                                                    padx=(CONTROL_GAP, SPACE_1))
        AnimatedButton(row.actions, text="Load",
                       command=lambda nm=name, i=list(ids or []): self._load(nm, i),
                       role="filled", size="sm",
                       accent=TAB_ACCENTS["Install"]).pack(side="right")
        row.bind_content()
        row.pack(fill="x", pady=TEXT_NUDGE)

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

    def _confirm_delete(self, name, count):
        """Ask before a saved setup is thrown away. False means keep it.

        Delete was the one destructive action in this dialog with no
        question attached: one mis-aimed click on a row took a setup away
        for good, while every other destructive action in the app (Process
        Manager, Uninstall leftovers, service changes) asks first. The
        count goes in the message because the name alone does not say how
        much is going. A confirm that cannot be shown counts as No, so a
        broken dialog can never be read as consent.
        """
        msg = ""
        if count:
            msg = "It holds %d app%s.\n\n" % (
                count, "" if count == 1 else "s")
        msg += ("The apps themselves stay installed — only the saved "
                "setup is removed, and you can save the same picks again.")
        try:
            return bool(_themed_askyesno(
                self._dlg, "Delete '%s'?" % name, msg,
                accent=TAB_ACCENTS["Install"],
                yes_text="Delete", no_text="Keep it"))
        except Exception:
            return False

    def _delete(self, name, count=0):
        if not self._confirm_delete(name, count):
            return
        ok = False
        try:
            from app.config_persist import delete_install_profile
            ok = bool(delete_install_profile(name))
        except Exception as exc:
            # Used to be dropped on the floor, then "Deleted" was said
            # anyway: the row stayed on screen and the status line lied.
            try:
                from app.utils import log_or_swallow
                log_or_swallow(None, "could not delete the saved setup "
                               "%r" % name, exc)
            except Exception:
                pass
        self._refresh_rows()
        if ok:
            self._say("Deleted '%s'." % name)
        else:
            self._say("Couldn't delete '%s' — the saved setups file may be "
                      "in use. Try again in a moment." % name)
