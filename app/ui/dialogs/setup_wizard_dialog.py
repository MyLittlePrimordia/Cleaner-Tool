"""N-001: New-PC / fresh-LTSC setup wizard ("from zero to gaming").

The app already owns every primitive of a setup flow -- LTSC component
installers, runtime bundles, the app catalog, saved app sets, tweak
presets -- but they live on four tabs the user must discover in the right
order. The wizard is a guided SELECTOR over those same primitives: it
ticks the Install tab's own boxes, loads a saved app set through the
Install tab's own profile machinery, and picks the Tweak tab's own
preset. Execution then stays in the audited paths (the Install run,
the Tweak run) with their snapshots, gates and honest reporting --
the wizard itself changes nothing until the review step is confirmed,
and even then it only stages selections (plus an optional one-click
Install run through InstallTab._install_selected).

Steps: Welcome (edition-aware) -> Missing components -> Runtimes ->
Your apps (saved sets) -> Tweaks -> Review.
"""
from __future__ import annotations

import tkinter as tk

from app.ui.base import ThemedModal
from app.ui.theme import COLORS, TAB_ACCENTS, TYPE, SPACE_1, SPACE_2, SPACE_3
from app.ui.widgets.wrap import fit_wrap
from app.ui.metrics import CONTROL_GAP, TEXT_NUDGE
from app.ui.widgets import AnimatedButton, ScrollableRoundedPanel
from app.ui.widgets.checkbox import M3Checkbutton
from app.ui.widgets.dropdown import M3Dropdown
from app.ui.widgets.segmented import SegmentedTabs

ACCENT = TAB_ACCENTS["Install"]


def wizard_groups():
    """(components, runtimes): Task lists for steps 1-2. Pure data seam.

    Components are the LTSC Missing Components group in TASKS order
    (Store first: it unlocks the rest); runtimes are the Essentials
    group. The wizard never invents its own list.
    """
    try:
        from app.tab_presets import TABS
        tasks = list(TABS.get("Install", []) or [])
    except Exception:
        return [], []
    components = [t for t in tasks
                  if getattr(t, "group", "") == "LTSC Missing Components"]
    runtimes = [t for t in tasks
                if getattr(t, "group", "") == "Essentials"]
    return components, runtimes


def default_components():
    """Task keys pre-ticked on step 1: Store + winget bootstrap on LTSC."""
    try:
        from app.compat import is_ltsc_edition
        if is_ltsc_edition():
            return {"install_store", "install_winget_unigetui"}
    except Exception:
        pass
    return set()


class SetupWizardDialog(ThemedModal):
    """Six-step guided setup. See module docstring for the contract."""

    _TITLES = (
        "Welcome",
        "1 of 5 — Missing Windows pieces",
        "2 of 5 — Runtimes games need",
        "3 of 5 — Your apps",
        "4 of 5 — Tweaks",
        "5 of 5 — Review",
    )

    def __init__(self, parent, app):
        self.app = app
        self._step = 0
        self._vars = {}
        # The app-set picker is a themed dropdown (a saved set has no natural
        # radio pair and a long profile name would clip a horizontal control).
        # `_profile_display` holds the row label; `_profile_map` resolves it
        # back to the profile name the Install tab's own machinery expects.
        self._profile_display = tk.StringVar(
            value="Skip -- I will pick by hand")
        self._profile_map = {"Skip -- I will pick by hand": ""}
        self._tweak_var = tk.StringVar(value="Recommended")
        super().__init__(parent, title="New PC setup",
                         accent=ACCENT)
        try:
            body = self.body
            self._head = tk.Label(body, text="", font=TYPE["title_small"],
                                  bg=COLORS["bg"], fg=COLORS["text"],
                                  anchor="w")
            self._head.pack(fill="x", pady=(0, SPACE_1))
            self._sub = tk.Label(body, text="", font=TYPE["caption"],
                                 bg=COLORS["bg"], fg=COLORS["subtext"],
                                 anchor="w", justify="left",
                                 wraplength=self.body_width(4))
            self._sub.pack(fill="x", pady=(0, SPACE_2))
            self._panel = ScrollableRoundedPanel(body)
            self._panel.config(height=300)
            self._panel.pack(fill="both", expand=True)
            self._holder = self._panel.inner
            nav = tk.Frame(body, bg=COLORS["bg"])
            nav.pack(fill="x", pady=(SPACE_3, 0))
            self._back = AnimatedButton(nav, text="Back",
                                        command=self._go_back,
                                        role="secondary", size="md")
            self._back.pack(side="left")
            self._next = AnimatedButton(nav, text="Next",
                                        command=self._go_next,
                                        role="filled", size="md",
                                        accent=ACCENT)
            self._next.pack(side="right")
            self._render()
        except Exception:
            pass

    # ------------------------------------------------------------ render

    def _render(self):
        try:
            self._head.config(text=self._TITLES[self._step])
        except Exception:
            pass
        for w in list(self._holder.winfo_children()):
            try:
                w.destroy()
            except Exception:
                pass
        try:
            [
                self._page_welcome,
                self._page_components,
                self._page_runtimes,
                self._page_apps,
                self._page_tweaks,
                self._page_review,
            ][self._step]()
        except Exception:
            pass
        try:
            self._back.set_enabled(self._step > 0)
            self._next.config_text("Set up this PC" if self._step == 5
                                   else "Next")
        except Exception:
            pass

    def _page_welcome(self):
        try:
            from app.compat import edition_label, is_ltsc_edition
            ed = edition_label() if is_ltsc_edition() else ""
        except Exception:
            ed = ""
        if ed:
            sub = ("%s detected -- the Store, winget and the Xbox pieces "
                   "are not preinstalled here. This walks you through "
                   "bringing them back, then runtimes, your apps and "
                   "tweaks. Nothing is installed until the last step."
                   % ed)
        else:
            sub = ("This walks you through the pieces games need -- "
                   "Windows components, runtimes, your apps and tweaks. "
                   "Nothing is installed until the last step.")
        self._sub.config(text=sub)
        _lbl = tk.Label(self._holder,
                        text="Five short steps. You can go back at any "
                             "point; closing the window changes nothing.",
                        font=TYPE["body_medium"], bg=COLORS["bg"],
                        fg=COLORS["text"], anchor="w", justify="left",
                        wraplength=0)
        _lbl.pack(fill="x", pady=SPACE_1)
        fit_wrap(_lbl, self._holder)

    def _check_list(self, tasks, pre=()):
        for t in tasks:
            try:
                var = self._vars.get(t.key)
                if var is None:
                    var = tk.BooleanVar(value=(t.key in pre))
                    self._vars[t.key] = var
                row = tk.Frame(self._holder, bg=COLORS["bg"])
                row.pack(fill="x", pady=TEXT_NUDGE)
                M3Checkbutton(row, variable=var, bg=COLORS["bg"],
                              fg=COLORS["text"],
                              activebackground=COLORS["bg"],
                              selectcolor=COLORS["surface"],
                              onvalue=True, offvalue=False).pack(side="left")
                tk.Label(row, text=t.label, font=TYPE["label_medium"],
                         bg=COLORS["bg"], fg=COLORS["text"],
                         anchor="w").pack(side="left",
                                          padx=(CONTROL_GAP, 0))
            except Exception:
                continue

    def _page_components(self):
        self._sub.config(text="Tick what this PC is missing. On a stripped "
                              "edition that is usually the whole list; on "
                              "Pro, installed pieces are skipped "
                              "automatically when the run starts.")
        tasks, _runtimes = wizard_groups()
        self._check_list(tasks, pre=default_components())

    def _page_runtimes(self):
        self._sub.config(text="One-click bundles so nobody guesses versions. "
                              "Already-installed parts are skipped by the "
                              "run itself.")
        _components, runtimes = wizard_groups()
        self._check_list(runtimes)

    def _page_apps(self):
        self._sub.config(text="A saved app set from My Setups ticks the "
                              "catalog for you. Skip this if you would "
                              "rather pick apps by hand on the Install tab.")
        try:
            from app.config_profiles import get_install_profiles
            profiles = get_install_profiles() or {}
        except Exception:
            profiles = {}
        skip = "Skip -- I will pick by hand"
        self._profile_map = {skip: ""}
        for name in sorted(profiles):
            ids = profiles.get(name) or []
            self._profile_map["%s (%d apps)" % (name, len(ids))] = name
        # Keep an already-made choice visible when the user steps back here.
        current = self._profile_display.get()
        if current not in self._profile_map:
            self._profile_display.set(skip)
        tk.Label(self._holder, text="Use a saved app set:",
                 font=TYPE["label_medium"], bg=COLORS["bg"],
                 fg=COLORS["text"], anchor="w").pack(
                     fill="x", pady=(SPACE_2, SPACE_1))
        # DESIGN: native Radiobutton is banned app-wide (design_guard); the
        # themed dropdown is the control the Install tab itself uses for a
        # profile pick, and it also fits a long saved-set name.
        M3Dropdown(
            self._holder, textvariable=self._profile_display,
            values=list(self._profile_map), width=36,
            bg=COLORS["bg"], font=TYPE["body_medium"]).pack(fill="x")
        if not profiles:
            _lbl = tk.Label(
                self._holder,
                text="No saved sets yet -- make one on the Install tab "
                     "with My Setups, then come back.",
                font=TYPE["caption"], bg=COLORS["bg"],
                fg=COLORS["subtext"], anchor="w",
                justify="left", wraplength=0)
            _lbl.pack(fill="x", pady=SPACE_2)
            fit_wrap(_lbl, self._holder)

    def _page_tweaks(self):
        self._sub.config(text="The Tweak tab preset this PC will use. This "
                              "only selects it -- you still press Run on "
                              "the Tweak tab, with its Preview first.")
        # DESIGN: a three-way choice is a segmented control, not three native
        # radios (design_guard bans tk.Radiobutton). `_tweak_var` still holds
        # the real preset key ("" means skip) for `_staged`.
        SegmentedTabs(
            self._holder,
            labels=("Minimal", "Recommended", "Skip"),
            keys=("Minimal", "Recommended", ""),
            initial=self._tweak_var.get() or "",
            accent=ACCENT,
            command=lambda key: self._tweak_var.set(key or ""),
        ).pack(anchor="w", pady=(SPACE_2, SPACE_1))
        _lbl = tk.Label(
            self._holder,
            text="Minimal: safe speed basics.  Recommended: the best "
                 "all-round setup.  Skip: leave tweaks for later.",
            font=TYPE["caption"], bg=COLORS["bg"],
            fg=COLORS["subtext"], anchor="w", justify="left",
            wraplength=0)
        _lbl.pack(fill="x")
        fit_wrap(_lbl, self._holder)

    def _staged(self):
        tasks, runtimes = wizard_groups()
        by_key = {t.key: t for t in (tasks + runtimes)}
        picked = [by_key[k] for k, v in self._vars.items()
                  if v.get() and k in by_key]
        try:
            profile = self._profile_map.get(
                self._profile_display.get(), "") or ""
        except Exception:
            profile = ""
        try:
            tweak = self._tweak_var.get() or ""
        except Exception:
            tweak = ""
        return picked, profile, tweak

    def _page_review(self):
        picked, profile, tweak = self._staged()
        lines = ["%d Windows pieces/runtimes ticked."
                 % len(picked)]
        lines.append("App set: %s." % (profile or "none (pick by hand)"))
        lines.append("Tweak preset: %s." % (tweak or "skipped"))
        self._sub.config(text="Check it once. Confirming ticks the Install "
                              "tab, loads the app set, selects the tweak "
                              "preset, then starts the Install run -- the "
                              "Tweak run stays yours to press.")
        for line in lines:
            tk.Label(self._holder, text="\u2022  " + line,
                     font=TYPE["body_medium"], bg=COLORS["bg"],
                     fg=COLORS["text"], anchor="w",
                     justify="left").pack(fill="x", pady=TEXT_NUDGE)

    # ------------------------------------------------------------ nav

    def _go_back(self):
        if self._step > 0:
            self._step -= 1
            self._render()

    def _go_next(self):
        if self._step < 5:
            self._step += 1
            self._render()
            return
        self._finish()

    def _finish(self):
        """Stage everything, then hand execution to the audited paths."""
        picked, profile, tweak = self._staged()
        staged = []
        try:
            install_tab = self.app.tabs.get("Install")
        except Exception:
            install_tab = None
        try:
            if install_tab is not None:
                for t in picked:
                    try:
                        var = install_tab.ess_vars.get(t.key)
                        if var is not None:
                            var.set(True)
                            staged.append(t.label)
                    except Exception:
                        continue
                try:
                    install_tab._refresh_install_count()
                except Exception:
                    pass
                if profile:
                    try:
                        from app.config_profiles import get_install_profiles
                        ids = (get_install_profiles() or {}).get(profile)
                        if ids:
                            applied, missing = \
                                install_tab.apply_profile_ids(ids)
                            staged.append("%s app set (%d ticked%s)"
                                          % (profile, applied,
                                             ", %d no longer in the catalog"
                                             % missing if missing else ""))
                    except Exception:
                        pass
            if tweak:
                try:
                    tweak_tab = self.app.tabs.get("Tweak")
                    if tweak_tab is not None:
                        tweak_tab._select_preset(tweak)
                        staged.append("Tweak preset: " + tweak)
                except Exception:
                    pass
        except Exception:
            pass
        try:
            self.close()
        except Exception:
            pass
        # Hand the run to the Install tab's own path (admin gates, winget
        # verification, per-app honesty all apply). The Tweak run is left
        # for the user to press, with Preview first.
        try:
            if install_tab is not None and install_tab.selected_apps():
                try:
                    self.app._switch_to("Install")
                except Exception:
                    pass
                install_tab._install_selected()
        except Exception:
            pass


__all__ = ["SetupWizardDialog", "wizard_groups", "default_components"]
