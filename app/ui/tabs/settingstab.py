"""SettingsTab — modern minimal OLED redesign.

Content floats on pure black. No grey panel boxes. Short labels.
Modern dropdowns. About section removed (user request).

REDESIGN 2026-10 (layout). The page used to be THREE equal columns of cards
inside a private scroller (`_VScroll`). Measured off-screen at every supported
window size, that layout overflowed on every one of them — content 832px
against a 610-795px viewport — so it always scrolled. It also clipped long
supporting text: the Quarantine tip wants 425px and was handed 244-373px,
because a narrow column simply cuts a Label that has no `wraplength`. Worst of
all it was unbalanced by construction: the right column held four cards
(832px) while the left held two (399px), so the overflow was structural, not a
few pixels of rounding.

The fix is a different shape, not a tuning pass: a two-pane Settings page, the
familiar Windows/macOS arrangement. A quiet category rail on the left selects
ONE category; the right pane shows that category's cards, full width. A
category is one short card, so nothing scrolls at any size, and every card is
full width, so nothing is clipped.

The right pane is the app's shared `ScrollableRoundedPanel`. Two things fall
out of that beyond a consistent scrollbar: the global wheel router
(`Application._on_wheel_routed`) finds it by walking up from the widget under
the pointer, so wheel scrolling works with no private binding — the old
`_VScroll` was invisible to that router — and a high-DPI machine that does make
a card taller than the pane scrolls it smoothly instead of cutting it off.
"""
from __future__ import annotations

import os
import time
import tkinter as tk

from app.ui.base import _themed_askyesno, _themed_showinfo
from app.ui.theme import COLORS, F, SHAPE_M, _hex_lerp, _round_rect_points
from app.ui.widgets import AnimatedButton, Tooltip
from app.ui.widgets.rounded_card import RoundedCard
from app.ui.widgets.dropdown import M3Dropdown
from app.ui.widgets.scroll import ScrollableRoundedPanel
from app.ui.widgets.toggle import ToggleSwitch

ACCENT = COLORS["accent_pink"]
#: Settings sections are M3 filled cards on this surface tone.
CARD = COLORS["bg_alt"]


_FREQ = (("Daily", "daily"), ("Weekly", "weekly"), ("Monthly", "monthly"))
_HEALTH = (("Off", "off"), ("At startup", "startup"), ("Daily", "daily"),
           ("Weekly", "weekly"))

#: (key, rail label) in rail order. One category per card group, so the rail
#: reads as the page's table of contents and the pane never shows more than a
#: couple of cards — the whole reason this layout cannot scroll or clip.
CATEGORIES = (
    ("general", "General"),
    ("maintenance", "Maintenance"),
    ("health", "PC Health"),
    ("drivers", "Drivers"),
    ("data", "Data & logs"),
    ("tweaks", "Tweaks & backup"),
    ("quarantine", "Quarantine"),
)

#: MINIMUM rail width. The real width is measured in `_rail_width()` from the
#: widest category label at the CURRENT font/DPI, because a fixed pixel width
#: is only wide enough for the font it was tuned against. "Tweaks & backup"
#: needs 210px at tk scaling 2.0 (a DPI-aware 200% display) but was handed a
#: hardcoded 196 — so the label was clipped and the last glyph cut off. Tk
#: sizes text in POINTS, which scale with DPI; a pixel constant does not.
RAIL_MIN_W = 196


class _NavItem(tk.Canvas):
    """One row of the Settings category rail.

    A rounded M3 navigation item: invisible on the page, a quiet hover surface,
    and a tonal pink selected container with a small leading accent tick.
    Drawn the way AnimatedButton and SegmentedTabs draw themselves (a smooth
    polygon plus text), so the rail reads as part of the same widget family
    rather than as a bolted-on list box.
    """

    HEIGHT = 40
    RADIUS = SHAPE_M

    def __init__(self, parent, text, command):
        super().__init__(parent, height=self.HEIGHT, highlightthickness=0,
                         bd=0, bg=parent["bg"], takefocus=1)
        self._text = text
        self._command = command
        self._selected = False
        self._hover = False
        self._paint()
        self.bind("<Configure>", lambda _e: self._paint())
        self.bind("<Enter>", self._enter)
        self.bind("<Leave>", self._leave)
        self.bind("<Button-1>", self._click)
        self.bind("<Return>", self._click)
        self.bind("<space>", self._click)

    def set_selected(self, on):
        on = bool(on)
        if on == self._selected:
            return
        self._selected = on
        self._paint()

    def _enter(self, _e):
        self._hover = True
        try:
            self.config(cursor="hand2")
        except Exception:
            pass
        self._paint()

    def _leave(self, _e):
        self._hover = False
        self._paint()

    def _click(self, _e=None):
        if callable(self._command):
            try:
                self._command()
            except Exception:
                pass

    def _paint(self):
        try:
            self.delete("all")
            w = max(1, self.winfo_width())
            h = self.HEIGHT
            if self._selected:
                fill = _hex_lerp(COLORS["bg"], ACCENT, 0.16)
            elif self._hover:
                fill = COLORS["surface_hover"]
            else:
                fill = COLORS["bg"]
            if self._selected or self._hover:
                self.create_polygon(
                    _round_rect_points(0, 1, w, h - 1, self.RADIUS),
                    smooth=True, fill=fill, outline="", tags="face")
            if self._selected:
                self.create_polygon(
                    _round_rect_points(0, h / 2 - 9, 3, h / 2 + 9, 1.5),
                    smooth=True, fill=ACCENT, outline="", tags="tick")
            self.create_text(
                16, h / 2, text=self._text, anchor="w",
                font=(F, 11, "bold") if self._selected else (F, 11),
                fill=COLORS["text"] if self._selected else COLORS["subtext"])
        except Exception:
            pass


class SettingsTab(tk.Frame):
    def __init__(self, parent, app):
        super().__init__(parent, bg=COLORS["bg"])
        self.app = app
        self._vars = {}
        self._dirty = False
        self._menus = []          # keep refs so cascade menus stay themed
        #: Labels whose `wraplength` is kept in step with the pane width, so
        #: supporting text wraps instead of being clipped at any window/DPI.
        self._wrap_labels = []
        self._cats = {}           # category key -> content frame
        self._nav_items = {}      # category key -> _NavItem
        self._active_cat = None

        from app.config_persist import load_config
        # PERF (2026-10): resync_flags() (three schtasks probes) used to run
        # here on the Tk thread. Application now runs it on a worker at
        # startup and calls refresh_scheduler_flags() when it lands; the page
        # also re-checks shortly after first build as a safety net.
        cfg = load_config()

        pad = tk.Frame(self, bg=COLORS["bg"])
        pad.pack(fill="both", expand=True, padx=32, pady=(12, 12))

        # ---- footer (Save + status) — quiet, minimal
        foot = tk.Frame(pad, bg=COLORS["bg"])
        foot.pack(side="bottom", fill="x", pady=(16, 0))
        self.status = tk.Label(
            foot, text="", font=(F, 9), bg=COLORS["bg"],
            fg=COLORS["subtext"], anchor="w")
        self.status.pack(side="left", fill="x", expand=True)
        self.save_btn = AnimatedButton(
            foot, text="Save", command=self._save, bg=ACCENT,
            fg=COLORS["black"], font=(F, 11, "bold"), padx=30, pady=12,
            radius=16)
        self.save_btn.pack(side="right")
        self.save_btn.set_enabled(False)

        # ---- two panes: category rail (fixed) | content pane (fills)
        body = tk.Frame(pad, bg=COLORS["bg"])
        body.pack(fill="both", expand=True)

        rail = tk.Frame(body, bg=COLORS["bg"], width=self._rail_width())
        rail.pack(side="left", fill="y")
        # fixed width: never let a long label widen the rail and squeeze the
        # pane (which would change every card's wraplength)
        rail.pack_propagate(False)

        pane = tk.Frame(body, bg=COLORS["bg"])
        pane.pack(side="left", fill="both", expand=True, padx=(18, 0))

        panel = ScrollableRoundedPanel(pane)
        panel._fill = COLORS["bg"]
        panel.inner.configure(bg=COLORS["bg"])
        try:
            panel._redraw_bg()
        except Exception:
            pass
        panel.pack(fill="both", expand=True)
        self._panel = panel
        panel.bind("<Configure>", self._on_pane_cfg, add="+")

        # ---- rail items
        for key, label in CATEGORIES:
            item = _NavItem(rail, label, lambda k=key: self._show_cat(k))
            item.pack(fill="x", pady=(0, 4))
            self._nav_items[key] = item

        # ---- one content frame per category; each holds its own card(s)
        for key, _label in CATEGORIES:
            self._cats[key] = tk.Frame(panel.inner, bg=COLORS["bg"])

        # ---- cards, grouped by category
        self._build_general(cfg)
        self._build_maintenance(cfg)
        self._build_health(cfg)
        self._build_drivers(cfg)
        self._build_data()
        self._build_tweaks()
        self._build_quarantine(cfg)

        # About section intentionally removed (user request).

        self._show_cat("general")
        # Safety net for an instant Settings visit: the async resync may not
        # have posted yet. Cheap (config read) and skipped once the user edits.
        try:
            self.after(700, self.refresh_scheduler_flags)
        except Exception:
            pass

    def refresh_scheduler_flags(self):
        """Apply a completed scheduler resync to the maintenance toggles.

        Only touches untouched controls: a user mid-edit must not have their
        choice overwritten by a background probe that started before it.
        """
        if getattr(self, "_dirty", False):
            return
        try:
            from app.config_persist import load_config
            cfg = load_config()
        except Exception:
            return
        for key, cfg_key in (("clean_schedule", "schedule_enabled"),
                             ("update_apps", "schedule_update_enabled")):
            var = self._vars.get(key)
            if var is not None:
                try:
                    var.set(bool(cfg.get(cfg_key, False)))
                except Exception:
                    pass
        var = self._vars.get("frequency")
        if var is not None:
            try:
                var.set(str(cfg.get("schedule_frequency", "weekly")))
            except Exception:
                pass
        var = self._vars.get("time")
        if var is not None:
            try:
                var.set(str(cfg.get("schedule_time", "03:00")))
            except Exception:
                pass

    def _rail_width(self):
        """Width of the category rail, measured for the current font/DPI.

        Labels are drawn in points, so their pixel width grows with `tk
        scaling`. Measuring the widest label in its SELECTED (bold) face — the
        widest of the two faces used — is what keeps "Tweaks & backup" whole on
        a 200% display, where a hardcoded 196px clipped it."""
        try:
            import tkinter.font as tkfont
            bold = tkfont.Font(font=(F, 11, "bold"))
            widest = max(bold.measure(label) for _key, label in CATEGORIES)
            return max(RAIL_MIN_W, 16 + widest + 16)
        except Exception:
            return RAIL_MIN_W

    # ------------------------------------------------------------ builders
    def _build_general(self, cfg):
        g = self._section(self._cats["general"], "General")
        self._toggle(g, "start_with_windows", "Start with Windows",
                     bool(cfg.get("startup_tray_enabled", False)),
                     "Launch hidden in the tray at sign-in.")
        self._toggle(g, "close_to_tray", "Close to tray",
                     bool(cfg.get("close_to_tray_enabled", False)),
                     "X hides the window instead of quitting.")
        self._toggle(g, "auto_elevate", "Start as administrator",
                     bool(cfg.get("auto_elevate", False)),
                     "Approve once; later launches skip the prompt.")

    def _build_maintenance(self, cfg):
        m = self._section(self._cats["maintenance"], "Automatic maintenance")
        self._toggle(m, "clean_schedule", "Auto Clean",
                     bool(cfg.get("schedule_enabled", False)),
                     "Runs your last Clean selection on a schedule.")
        self._toggle(m, "update_apps", "Update apps",
                     bool(cfg.get("schedule_update_enabled", False)),
                     "Runs winget upgrade on the same schedule.")
        self._choice(m, "frequency", "How often", _FREQ,
                     str(cfg.get("schedule_frequency", "weekly")))
        self._entry(m, "time", "Time", str(cfg.get("schedule_time", "03:00")))

    def _build_health(self, cfg):
        h = self._section(self._cats["health"], "PC Health")
        self._choice(h, "health_mode", "Check automatically", _HEALTH,
                     str(cfg.get("health_check_mode", "startup")))

    def _build_drivers(self, cfg):
        d = self._section(self._cats["drivers"], "Drivers")
        self._folder_lbl = self._wrap_lbl(d, "")
        row = tk.Frame(d, bg=CARD)
        row.pack(fill="x", pady=(8, 0))
        self._btn(row, "Change folder\u2026", self._pick_folder).pack(side="left")
        self._btn(row, "Open folder", self._open_drivers).pack(
            side="left", padx=(8, 0))
        self._vars["drivers_folder"] = tk.StringVar(
            value=str(cfg.get("drivers_folder", "")))
        self._refresh_folder()

    def _build_data(self):
        t = self._section(self._cats["data"], "Logs & data")
        row = tk.Frame(t, bg=CARD)
        row.pack(fill="x")
        self._btn(row, "Export logs", lambda: self.app.export_logs()).pack(
            anchor="w")
        self._btn(row, "Open data folder", self._open_data).pack(
            anchor="w", pady=(8, 0))

    def _build_tweaks(self):
        # The undo history is the one thing the user cannot get back, so it is
        # first-class here: see what is applied, put it all back, or keep a copy
        # to carry to a new PC.
        t = self._section(self._cats["tweaks"], "Tweaks & backup")
        row = tk.Frame(t, bg=CARD)
        row.pack(fill="x")
        self._btn(row, "Review applied tweaks\u2026", self._review_tweaks).pack(
            anchor="w")
        self._btn(row, "Undo all tweaks", self._undo_all_tweaks).pack(
            anchor="w", pady=(8, 0))
        self._btn(row, "Back up tweaks\u2026", self._backup_tweaks).pack(
            anchor="w", pady=(8, 0))
        self._btn(row, "Restore tweaks\u2026", self._restore_tweaks).pack(
            anchor="w", pady=(8, 0))
        self._btn(row, "Export change log\u2026", self._export_change_log).pack(
            anchor="w", pady=(8, 0))

    def _build_quarantine(self, cfg):
        # A deleted file cannot be brought back, so "undo a clean" gets its own
        # card: keep cleaned files in a bin instead of deleting them, put them
        # back, or reclaim the space by emptying the bin.
        t = self._section(self._cats["quarantine"], "Quarantine")
        self._toggle(t, "quarantine_enabled", "Keep cleaned files",
                     bool(cfg.get("quarantine_enabled", False)),
                     "Move cleaned files to a bin instead of deleting them, so "
                     "you can put them back.")
        self._quar_lbl = self._wrap_lbl(t, "", pady=(6, 0))
        qrow = tk.Frame(t, bg=CARD)
        qrow.pack(fill="x", pady=(8, 0))
        self._btn(qrow, "Put files back", self._restore_quarantine).pack(
            anchor="w")
        self._btn(qrow, "Empty bin", self._empty_quarantine).pack(
            anchor="w", pady=(8, 0))
        self._refresh_quarantine()

    def _section(self, parent, title):
        """M3 filled card: primary-coloured title, content below."""
        card = RoundedCard(parent, fill=CARD, bg=COLORS["bg"], radius=16)
        card.pack(fill="x", pady=(0, 14))
        tk.Label(
            card.body, text=title, font=(F, 11, "bold"),
            bg=CARD, fg=COLORS["primary"], anchor="w"
        ).pack(fill="x", padx=18, pady=(14, 4))
        sec = tk.Frame(card.body, bg=CARD)
        sec.pack(fill="x", padx=18, pady=(0, 12))
        return sec

    def _wrap_lbl(self, parent, text, pady=(0, 0)):
        """A supporting-text Label that wraps to the pane width instead of
        being clipped: registered so `_on_pane_cfg` keeps its wraplength in
        step with the space actually available."""
        lbl = tk.Label(parent, text=text, font=(F, 9), bg=CARD,
                       fg=COLORS["subtext"], anchor="w", justify="left",
                       wraplength=360)
        lbl.pack(fill="x", pady=pady)
        self._wrap_labels.append(lbl)
        return lbl

    def _row(self, card, label, tip=""):
        r = tk.Frame(card, bg=CARD)
        r.pack(fill="x", pady=6)
        left = tk.Frame(r, bg=CARD)
        left.pack(side="left", fill="x", expand=True)
        lb = tk.Label(
            left, text=label, font=(F, 11), bg=CARD,
            fg=COLORS["text"], anchor="w")
        lb.pack(fill="x")
        if tip:
            # M3 list-item supporting text instead of a hover tooltip. It
            # WRAPS to the pane width; a fixed one-line Label here is exactly
            # what clipped the longest tips in the old three-column layout.
            t = tk.Label(left, text=tip, font=(F, 9), bg=CARD,
                         fg=COLORS["subtext"], anchor="w", justify="left",
                         wraplength=360)
            t.pack(fill="x")
            self._wrap_labels.append(t)
        return r

    def _btn(self, parent, text, cmd):
        return AnimatedButton(
            parent, text=text, command=cmd,
            bg=CARD, fg=COLORS["primary"], variant="outlined",
            font=(F, 10, "bold"), padx=18, pady=7)

    # ----------------------------------------------------------- category nav
    def _on_pane_cfg(self, e):
        """Track the pane width so supporting text wraps rather than clips.

        The label sits in a row that also holds a right-packed control (a
        toggle, dropdown or entry), so the room left for text is the pane
        width minus a generous reserve for that control. Over-reserving only
        wraps the text a touch earlier; under-reserving is the clipping bug
        this exists to prevent."""
        try:
            wrap = max(200, int(e.width) - 160)
        except Exception:
            return
        for lbl in list(self._wrap_labels):
            try:
                lbl.config(wraplength=wrap)
            except Exception:
                pass

    def _show_cat(self, key):
        """Show one category's cards and mark its rail item selected."""
        if key not in self._cats:
            return
        for k, frame in self._cats.items():
            try:
                if k == key:
                    frame.pack(fill="x")
                else:
                    frame.pack_forget()
            except Exception:
                pass
        for k, item in self._nav_items.items():
            item.set_selected(k == key)
        self._active_cat = key
        try:
            self._panel.inner.update_idletasks()
            self._panel.refresh_scroll()
        except Exception:
            pass

    # ----------------------------------------------------------- behaviour
    def _mark(self, *_a):
        if not self._dirty:
            self._dirty = True
            self.save_btn.set_enabled(True)
            self._say("")

    def _toggle(self, card, key, label, value, tip):
        var = tk.BooleanVar(value=value)
        self._vars[key] = var
        r = self._row(card, label, tip)
        ToggleSwitch(r, variable=var, command=self._mark).pack(side="right")
        var.trace_add("write", self._mark)

    def _choice(self, card, key, label, options, current):
        """Modern flat dropdown — quiet surface, no system chrome."""
        names = {k: n for n, k in options}
        keys = {n: k for n, k in options}
        var = tk.StringVar(value=names.get(current, options[0][0]))
        self._vars[key] = (var, keys)
        r = self._row(card, label)

        dd = M3Dropdown(r, textvariable=var,
                        values=[n for n, _k in options],
                        width=max(len(n) for n, _ in options) + 1,
                        bg=CARD, font=(F, 10))
        dd.pack(side="right")
        self._menus.append(dd)
        var.trace_add("write", self._mark)

    def _entry(self, card, key, label, value):
        var = tk.StringVar(value=value)
        self._vars[key] = var
        r = self._row(card, label)
        e = tk.Entry(
            r, textvariable=var, width=6, justify="center",
            bg=COLORS["surface"], fg=COLORS["text"],
            insertbackground=COLORS["text"], relief="flat",
            font=(F, 10), highlightthickness=1,
            highlightbackground=COLORS["hairline"],
            highlightcolor=COLORS["primary"])
        e.pack(side="right", ipady=4, ipadx=6)

        def _e_enter(_ev, ent=e):
            try:
                ent.config(bg=COLORS["surface_hover"],
                           highlightbackground=COLORS["primary"])
            except Exception:
                pass

        def _e_leave(_ev, ent=e):
            try:
                ent.config(bg=COLORS["surface"],
                           highlightbackground=COLORS["hairline"])
            except Exception:
                pass

        e.bind("<Enter>", _e_enter, add="+")
        e.bind("<Leave>", _e_leave, add="+")
        var.trace_add("write", self._mark)

    def set_run_enabled(self, enabled: bool):
        return None

    def _say(self, text, color=None):
        self.status.config(text=text, fg=color or COLORS["subtext"])

    def values(self) -> dict:
        out = {}
        for key, v in self._vars.items():
            if isinstance(v, tuple):
                var, keys = v
                out[key] = keys.get(var.get(), var.get())
            elif isinstance(v, tk.BooleanVar):
                out[key] = bool(v.get())
            else:
                out[key] = str(v.get()).strip()
        return out

    def _save(self):
        import re
        vals = self.values()
        if not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", vals.get("time", "")):
            self._say("Time must be HH:MM (24h).", COLORS["accent_red"])
            return
        from app.settings_apply import apply_settings
        self.save_btn.set_enabled(False)
        self._say("Saving…")
        self.update_idletasks()
        results, errors = apply_settings(vals)
        try:
            self.app._refresh_health_badge()
        except Exception:
            pass
        self._dirty = False
        if errors:
            self.save_btn.set_enabled(True)
            self._say(errors[0], COLORS["accent_red"])
            _themed_showinfo(
                self.winfo_toplevel(),
                "Some settings were not applied",
                "\n\n".join(errors), accent=ACCENT)
        else:
            self._say(
                "Saved." if not results else "Saved: " + "; ".join(results[:3]),
                COLORS["accent_green"])

    def _refresh_quarantine(self):
        try:
            from app import quarantine
            from app.utils import format_bytes
            n, total = quarantine.stats()
            self._quar_lbl.config(
                text=("%d file(s), %s held. Emptying reclaims the space."
                      % (n, format_bytes(total))) if n else "The bin is empty.")
        except Exception:
            try:
                self._quar_lbl.config(text="")
            except Exception:
                pass

    def _restore_quarantine(self):
        try:
            from app import quarantine
            n, failed = quarantine.restore_all()
        except Exception as exc:
            _themed_showinfo(self.winfo_toplevel(), "Could not restore",
                             "The files could not be put back: %s" % exc,
                             accent=ACCENT)
            return
        self._refresh_quarantine()
        msg = ("Put %d file(s) back." % n) if n else "Nothing to put back."
        if failed:
            msg += (" %d file(s) could not be put back because something now "
                    "exists at the original location." % failed)
        _themed_showinfo(self.winfo_toplevel(), "Quarantine", msg, accent=ACCENT)

    def _empty_quarantine(self):
        try:
            from app import quarantine
            from app.utils import format_bytes
            n, total = quarantine.stats()
        except Exception:
            n, total = 0, 0
        if not n:
            _themed_showinfo(self.winfo_toplevel(), "Quarantine",
                             "The bin is already empty.", accent=ACCENT)
            return
        if not _themed_askyesno(
                self.winfo_toplevel(), "Empty the bin?",
                "This permanently deletes %d file(s) (%s) from the Quarantine "
                "bin. They cannot be recovered." % (n, format_bytes(total)),
                accent=ACCENT, yes_text="Delete", no_text="Keep"):
            return
        try:
            from app import quarantine
            quarantine.empty()
        except Exception as exc:
            _themed_showinfo(self.winfo_toplevel(), "Could not empty",
                             "The bin could not be emptied: %s" % exc,
                             accent=ACCENT)
        self._refresh_quarantine()

    def _refresh_folder(self):
        try:
            from app.drivers import folder
            shown = str(folder.resolve(self._vars["drivers_folder"].get()))
            import os as _os
            # folder NAME only (a full path wraps and clips); full path on hover
            self._folder_lbl.config(
                text="\U0001F4C1  " + (_os.path.basename(shown.rstrip("\\/")) or shown))
            if not getattr(self, "_folder_tip", None):
                self._folder_tip = Tooltip(self._folder_lbl, shown)
        except Exception:
            pass

    def _pick_folder(self):
        from tkinter import filedialog
        from app.drivers import folder
        path = filedialog.askdirectory(
            parent=self.winfo_toplevel(),
            title="Choose drivers folder")
        if not path:
            return
        if not folder.writable(path):
            self._say("That folder is not writable.", COLORS["accent_red"])
            return
        self._vars["drivers_folder"].set(path)
        self._refresh_folder()
        self._mark()

    def _open_drivers(self):
        try:
            from app.drivers import folder
            os.startfile(folder.resolve(self._vars["drivers_folder"].get()))
        except Exception:
            self._say("Could not open the folder.", COLORS["accent_yellow"])

    def _open_data(self):
        try:
            from app.config_paths import CONFIG_DIR
            os.startfile(str(CONFIG_DIR))
        except Exception:
            self._say("Could not open the folder.", COLORS["accent_yellow"])

    # -------------------------------------------------------- tweaks & backup
    def _review_tweaks(self):
        """Open the per-row applied-tweaks browser (roadmap 2.1). Each row's
        Undo reverts exactly that one tweak through the normal revert runner."""
        try:
            from app import tweak_center
            groups = tweak_center.applied_groups()
        except Exception as exc:
            self._say(f"Could not read tweak state: {exc}", COLORS["accent_red"])
            return
        if not groups:
            _themed_showinfo(
                self.winfo_toplevel(), "Nothing applied",
                "No tweaks are currently applied.", accent=COLORS["accent_yellow"])
            return
        try:
            from app.ui.dialogs.tweak_snapshots_dialog import show_tweak_snapshots
        except Exception as exc:
            self._say(f"Unavailable: {exc}", COLORS["accent_red"])
            return
        show_tweak_snapshots(self.winfo_toplevel(), groups, self._undo_tasks,
                             accent=ACCENT)

    def _undo_tasks(self, tasks):
        """Confirm and revert a specific list of tweak Tasks (shared by the
        per-row browser and Undo-All)."""
        if not tasks:
            return
        if not _themed_askyesno(
                self.winfo_toplevel(), "Undo tweak(s)?",
                f"Put back {len(tasks)} tweak(s) to how they were before?\n\n"
                "This restores the exact values recorded when each tweak ran.",
                accent=COLORS["accent_red"],
                yes_text="Undo Them", no_text="Go Back"):
            return
        self.app.run_tasks("Tweak", tasks, mode="revert")

    def _undo_all_tweaks(self):
        """Put every applied, revertable tweak back to its recorded prior value
        via the normal revert runner (same machinery as the Tweak tab's Undo)."""
        try:
            from app import tweak_center
            tasks = tweak_center.undo_all_tasks()
        except Exception as exc:
            self._say(f"Could not read tweak state: {exc}", COLORS["accent_red"])
            return
        if not tasks:
            _themed_showinfo(
                self.winfo_toplevel(), "Nothing to Undo",
                "No tweaks are currently applied.", accent=COLORS["accent_yellow"])
            return
        self._undo_tasks(tasks)

    def _backup_tweaks(self):
        """Save the undo history (snapshots + applied list) to a JSON file."""
        from tkinter import filedialog
        try:
            from app import tweak_center
        except Exception as exc:
            self._say(f"Unavailable: {exc}", COLORS["accent_red"])
            return
        path = filedialog.asksaveasfilename(
            parent=self.winfo_toplevel(), title="Back up tweaks",
            defaultextension=".json",
            initialfile=f"CleanerTool_Tweaks_{time.strftime('%Y%m%d')}.json",
            filetypes=[("Tweak backup", "*.json"), ("All files", "*.*")])
        if not path:
            return
        try:
            n = tweak_center.backup_to(path)
        except Exception as exc:
            self._say(f"Backup failed: {exc}", COLORS["accent_red"])
            return
        self._say(f"Backed up {n} tweak(s).", COLORS["accent_green"])

    def _restore_tweaks(self):
        """Merge a backup file back in (never overwrites a fresher local
        snapshot — see config_persist.import_tweak_snapshots)."""
        from tkinter import filedialog
        try:
            from app import tweak_center
        except Exception as exc:
            self._say(f"Unavailable: {exc}", COLORS["accent_red"])
            return
        path = filedialog.askopenfilename(
            parent=self.winfo_toplevel(), title="Restore tweaks",
            filetypes=[("Tweak backup", "*.json"), ("All files", "*.*")])
        if not path:
            return
        try:
            added = tweak_center.restore_from(path)
        except Exception as exc:
            self._say(f"Restore failed: {exc}", COLORS["accent_red"])
            _themed_showinfo(self.winfo_toplevel(), "Could not restore",
                             str(exc), accent=COLORS["accent_red"])
            return
        self._say(f"Restored {added} tweak record(s).", COLORS["accent_green"])

    def _export_change_log(self):
        """Write a plain-text 'what each applied tweak changed' report."""
        from tkinter import filedialog
        try:
            from app import tweak_center
            text = tweak_center.change_log_text()
        except Exception as exc:
            self._say(f"Unavailable: {exc}", COLORS["accent_red"])
            return
        path = filedialog.asksaveasfilename(
            parent=self.winfo_toplevel(), title="Export tweak change log",
            defaultextension=".txt",
            initialfile=f"CleanerTool_TweakLog_{time.strftime('%Y%m%d')}.txt",
            filetypes=[("Text", "*.txt"), ("All files", "*.*")])
        if not path:
            return
        try:
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(text)
        except Exception as exc:
            self._say(f"Could not write log: {exc}", COLORS["accent_red"])
            return
        self._say("Change log saved.", COLORS["accent_green"])
