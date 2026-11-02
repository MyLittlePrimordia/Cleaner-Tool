"""GameProfilesDialog - N-003: the editor for per-game tweak profiles.

The store landed with no way to fill it. This window is that way: name a
set, say which game it belongs to, and save whatever is ticked on the Tweak
tab right now. Auto-Pilot then applies the match when it sees the game, so
there is no second switch to remember.

The dialog owns no tuning. Every save goes through `app.game_profiles`, which
already refuses an empty name or an empty key list; the wording here only
explains WHY a save was refused, and the list re-reads the store after every
change so it can never drift from what is on disk.
"""
from __future__ import annotations

import tkinter as tk

from app import game_profiles
from app.config_schema import GAME_PROFILES_MAX
from app.ui.base import _themed_askyesno
from app.ui.dialogs._tool_page import ToolPage
from app.ui.metrics import CONTROL_GAP, PAGE_INSET, TEXT_NUDGE
from app.ui.theme import COLORS, SPACE_1, SPACE_2, TAB_ACCENTS, TYPE
from app.ui.widgets import AnimatedButton, EmptyState, RoundedEntry, ToolRow

ACCENT = TAB_ACCENTS["Tweak"]


def describe(row) -> str:
    """One line for a stored profile: its game, then how many tweaks it holds."""
    if not isinstance(row, dict):
        return "no game bound \u00b7 0 tweaks"
    keys = row.get("keys")
    count = len(keys) if isinstance(keys, list) else 0
    exe = str(row.get("exe") or "").strip()
    return "%s \u00b7 %d tweak%s" % (exe or "no game bound", count,
                                     "" if count == 1 else "s")


def open_from(app, dialog):
    """Close the dialog that launched this one, then open the editor on top."""
    try:
        dialog.close()
    except Exception:
        pass
    try:
        GameProfilesDialog(app.root, app).wait()
    except Exception:
        pass


class GameProfilesDialog(ToolPage):
    """Name a tweak set per game; list, apply and delete what is saved."""

    def __init__(self, parent, app):
        self.app = app
        self._holder = None
        super().__init__(parent, title="Game profiles", accent=ACCENT,
                         scope="Saves a tweak set. Nothing is ticked or changed "
                               "until a profile starts.",
                         fit=True)
        body = self.content
        self.banner.set_state("\U0001F3AE", ACCENT)
        self.banner.set_title("The tweaks each game starts with", COLORS["text"])
        self.banner.set_sub("Auto-Pilot applies the matching profile when it "
                            "sees the game, so a different game no longer "
                            "needs the Tweak tab re-ticked by hand.")

        self.add_button("Close", self.close, kind="primary")

        # ---- save row ---------------------------------------------------- #
        box = tk.Frame(body, bg=COLORS["bg_alt"])
        box.pack(fill="x", pady=(PAGE_INSET, SPACE_2))
        inner = tk.Frame(box, bg=COLORS["bg_alt"])
        inner.pack(fill="x", padx=SPACE_2, pady=SPACE_2)

        self._hint = tk.Label(inner, text="", font=TYPE["caption"],
                              bg=COLORS["bg_alt"], fg=COLORS["subtext"],
                              anchor="w", justify="left",
                              wraplength=self.body_width(30))
        self._hint.pack(fill="x", pady=(0, CONTROL_GAP))

        entry_row = tk.Frame(inner, bg=COLORS["bg_alt"])
        entry_row.pack(fill="x")
        self._name_var = tk.StringVar(value="")
        self._exe_var = tk.StringVar(value="")
        self._name = RoundedEntry(entry_row, textvariable=self._name_var,
                                  width=18, accent=ACCENT)
        self._name.pack(side="left")
        self._exe = RoundedEntry(entry_row, textvariable=self._exe_var,
                                 width=20, accent=ACCENT)
        self._exe.pack(side="left", padx=(SPACE_1, 0))
        self._save_btn = AnimatedButton(entry_row, text="Save profile",
                                        command=self._save, role="filled",
                                        size="sm", accent=ACCENT)
        self._save_btn.pack(side="left", padx=(SPACE_2, 0))
        for widget in (self._name, self._exe):
            try:
                widget.entry.bind("<Return>", lambda _e: self._save())
            except Exception:
                pass

        # ---- saved list -------------------------------------------------- #
        tk.Label(body, text="Saved profiles", font=TYPE["label_medium"],
                 bg=COLORS["bg"], fg=COLORS["subtext"],
                 anchor="w").pack(fill="x", pady=(0, SPACE_1))
        self._holder = tk.Frame(body, bg=COLORS["bg"])
        self._holder.pack(fill="x")

        self._refresh()

    # ---- list ------------------------------------------------------------ #
    def _refresh(self):
        holder = self._holder
        if holder is None:
            return
        for child in list(holder.winfo_children()):
            try:
                child.destroy()
            except Exception:
                pass
        self._update_hint()
        try:
            table = game_profiles.profiles()
        except Exception:
            table = {}
        if not table:
            EmptyState(holder, glyph="\u2606", text="No game profiles yet.",
                       detail="Name one above and it lands here.",
                       bg=COLORS["bg"],
                       wrap=max(240, self.body_width(26))).pack(
                           fill="x", pady=(TEXT_NUDGE, 0))
            return
        for name in sorted(table, key=lambda n: str(n).lower()):
            self._build_row(name, table[name])

    def _build_row(self, name, row_data):
        row = ToolRow(self._holder, title=str(name), sub=describe(row_data),
                      size="md", show_dot=False)
        # Right-to-left, the app's usual row order: Delete at the outer edge.
        AnimatedButton(row.actions, text="Delete",
                       command=lambda n=name: self._delete(n),
                       role="text", size="sm").pack(
                           side="right", padx=(CONTROL_GAP, SPACE_1))
        AnimatedButton(row.actions, text="Use now",
                       command=lambda n=name: self._use(n),
                       role="filled", size="sm", accent=ACCENT).pack(side="right")
        row.bind_content()
        row.pack(fill="x", pady=TEXT_NUDGE)

    def _update_hint(self):
        n = len(self._current_keys())
        if n:
            text = ("Saves the %d tweak%s ticked on the Tweak tab. Name it, "
                    "then enter the game's exe (for example cs2.exe)."
                    % (n, "" if n == 1 else "s"))
        else:
            text = ("Tick at least one tweak on the Tweak tab first - a "
                    "profile with no tweaks would do nothing when it starts.")
        try:
            self._hint.config(text=text)
            self._save_btn.set_enabled(bool(n))
        except Exception:
            pass

    # ---- actions --------------------------------------------------------- #
    def _current_keys(self):
        try:
            tab = (getattr(self.app, "tabs", {}) or {}).get("Tweak")
            keys = [t.key for t in tab.selected_tasks() if getattr(t, "key", "")]
        except Exception:
            return []
        out = []
        for key in keys:
            if key not in out:
                out.append(key)
        return out

    def _save(self):
        label = (self._name_var.get() or "").strip()
        exe = (self._exe_var.get() or "").strip()
        keys = self._current_keys()
        if not label:
            self.status.stop("Give the profile a name first.", COLORS["accent_yellow"])
            return
        if not exe:
            self.status.stop("Enter the game's exe - for example cs2.exe.",
                             COLORS["accent_yellow"])
            return
        if not keys:
            self.status.stop("Nothing is ticked on the Tweak tab to save.",
                             COLORS["accent_yellow"])
            return
        ok = game_profiles.save(label, exe, keys)
        if not ok:
            self.status.stop("Couldn't save - you may already have %d profiles. "
                             "Delete one and try again." % GAME_PROFILES_MAX,
                             COLORS["accent_yellow"])
            return
        self._name_var.set("")
        self._exe_var.set("")
        self._refresh()
        self.status.stop("Saved '%s' - %d tweak%s, bound to %s."
                         % (label, len(keys), "" if len(keys) == 1 else "s",
                            exe), COLORS["accent_green"])

    def _use(self, name):
        keys = game_profiles.keys_for(name)
        if not keys:
            self.status.stop("'%s' has no tweaks left to apply." % name,
                             COLORS["accent_yellow"])
            return
        self.close()
        try:
            self.app.start_game_night(quiet=True, keys=keys)
        except Exception:
            pass

    def _delete(self, name):
        if not self._confirm_delete(name):
            return
        if game_profiles.delete(name):
            self._refresh()
            self.status.stop("Deleted '%s'." % name)
        else:
            self.status.stop("Couldn't delete '%s' - try again in a moment."
                             % name, COLORS["accent_yellow"])

    def _confirm_delete(self, name):
        try:
            return bool(_themed_askyesno(
                self._dlg, "Delete '%s'?" % name,
                "The tweaks themselves are not changed. Only the profile is "
                "removed, and you can save it again from the Tweak tab.",
                accent=ACCENT, yes_text="Delete", no_text="Keep it"))
        except Exception:
            return False

    def wait(self):
        try:
            self._dlg.grab_set()
        except Exception:
            pass
        try:
            self._dlg.wait_window()
        except Exception:
            pass


__all__ = ["GameProfilesDialog", "describe", "open_from"]
