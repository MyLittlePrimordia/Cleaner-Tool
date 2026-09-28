"""PilotDialog — extracted verbatim from app/gui.py (H12, batch 1).

One dialog per module, mirroring app/ui/widgets. Depends only on
app.ui below it, plus the app.utils helpers it already used; it
knows nothing
about any other dialog except where noted.
"""
from __future__ import annotations

import os

import tkinter as tk
from tkinter import ttk, messagebox
from app.ui.base import ThemedModal, Tooltip
from app.ui.theme import COLORS, F, TAB_ACCENTS, _hex_lerp
from app.ui.widgets import AnimatedButton, ScrollableRoundedPanel, ToggleSwitch
from app.ui.dialogs.catalog_picker_dialog import _CatalogPickerDialog


def _scan_installed_apps():
    """Worker-thread catalog sweep for the apps picker (same)."""
    try:
        from app.game_catalog import installed_apps
        return installed_apps()
    except Exception:
        return []


def _scan_installed_games():
    """Worker-thread catalog sweep for the games picker (imports live
    here so the click path pays nothing — not even an import)."""
    try:
        from app.game_catalog import installed_games
        return installed_games()
    except Exception:
        return []


class PilotDialog(ThemedModal):
    """Game Session Auto-Pilot setup (user-approved feature 6, 2026-09):
    master switch, session-preset picker, and the user's own .exe
    watchlist. Plain language throughout — this dialog is for gamers,
    not tweakers. Turning it off (or closing the app) never reverts
    anything; applied tweaks stay badged until Undo reverts them.

    Phase-5: the watchlist has three ways to grow — Pick from installed
    games (launcher-aware catalog), Pick from installed apps (registry),
    and the classic Browse for portable exes. 'Watch all my installed
    games' folds the whole catalog in automatically (opt-in)."""

    _MAX_SHOWN_GAMES = 6

    def __init__(self, parent, app):
        self.app = app
        try:
            from app.config_persist import load_config as _load
            cfg = _load()
        except Exception:
            cfg = {}
        try:
            from app.tab_presets import PRESETS as _PRESETS
            self._preset_names = list(_PRESETS.get("Tweak", {}).keys())
        except Exception:
            self._preset_names = ["Game Session"]
        if not self._preset_names:
            self._preset_names = ["Game Session"]
        preset = cfg.get("session_pilot_preset", "Game Session")
        if preset not in self._preset_names:
            preset = "Game Session"

        try:
            root = parent.winfo_toplevel()
        except Exception:
            root = parent
        super().__init__(parent, title="Game Session Auto-Pilot",
                         accent=TAB_ACCENTS["Tweak"])
        dlg = self._dlg
        body = self.body

        tk.Label(body, text="Session tweaks apply when a game starts — removed when you quit.",
                 font=(F, 9), bg=COLORS["bg"],
                 fg=COLORS["subtext"], wraplength=680,
                 justify="left").pack(anchor="w")

        # UI fix (less busy): master toggle + preset picker used to be two
        # separate rows — merged onto one, since together they're really
        # one sentence ("Watch for games, and run [preset] when one starts").
        mrow = tk.Frame(body, bg=COLORS["bg"])
        mrow.pack(fill="x", pady=(10, 0))
        self._enabled_var = tk.BooleanVar(
            value=bool(cfg.get("session_pilot_enabled", False)))
        ToggleSwitch(mrow, self._enabled_var).pack(side="left")
        tk.Label(mrow, text="Watch for games", font=(F, 10, "bold"),
                 bg=COLORS["bg"], fg=COLORS["text"]).pack(
                     side="left", padx=(10, 0))
        tk.Label(mrow, text="Run:", font=(F, 9), bg=COLORS["bg"],
                 fg=COLORS["text"]).pack(side="left", padx=(18, 0))
        self._preset_var = tk.StringVar(value=preset)
        combo = self._preset_combo = ttk.Combobox(
            mrow, textvariable=self._preset_var,
            values=self._preset_names, state="readonly",
            width=14, font=(F, 9))
        combo.pack(side="left", padx=(8, 0))
        try:
            combo.bind("<<ComboboxSelected>>", self._on_preset)
        except Exception:
            pass
        self._live_lbl = tk.Label(mrow, text="", font=(F, 9, "bold"),
                                  bg=COLORS["bg"], fg=COLORS["subtext"])
        self._live_lbl.pack(side="right")
        try:
            self._enabled_var.trace_add("write", self._on_toggle)
        except Exception:
            pass

        # watchlist
        tk.Frame(body, bg=COLORS["hairline"], height=1).pack(
            fill="x", pady=(12, 6))
        try:
            from app.session_pilot import KNOWN_GAME_EXES as _KNOWN
            n_known = len(_KNOWN)
        except Exception:
            n_known = 0
        try:
            from app.config_persist import load_config as _cload
            watching_all = bool(_cload().get("session_pilot_watch_all"))
        except Exception:
            watching_all = False
        _hdr = ("Watching every installed game plus the built-in list:" if watching_all
                else f"Watching {n_known} known games, plus yours below:")
        tk.Label(body, text=_hdr,
                 font=(F, 9, "bold"), bg=COLORS["bg"],
                 fg=COLORS["text"]).pack(anchor="w")
        self._games_body = tk.Frame(body, bg=COLORS["bg"])
        self._games_body.pack(fill="x", pady=(4, 0))
        # three ways to grow the watchlist (Phase-5): installed games,
        # installed apps, and the classic drive browse for portable exes.
        # UI fix (less clutter): collapsed the three separate buttons that
        # used to sit here into one "+ Add games…" button with a small
        # picker menu — same three ways in, one thing to look at.
        addrow = tk.Frame(body, bg=COLORS["bg"])
        addrow.pack(fill="x", pady=(6, 0))
        self._add_btn = AnimatedButton(addrow, text="+ Add games…",
                       command=self._open_add_menu,
                       bg=COLORS["surface"], fg=COLORS["text"],
                       font=(F, 9, "bold"), padx=16, pady=6)
        self._add_btn.pack(side="left")
        # watch-all toggle (default ON for new users — user ruling
        # 2026-09-12; anyone whose config already carries a choice keeps
        # it. Detection is path-verified, never guessed.)
        wrow = tk.Frame(body, bg=COLORS["bg"])
        wrow.pack(fill="x", pady=(8, 0))
        self._watch_all_var = tk.BooleanVar(
            value=bool(cfg.get("session_pilot_watch_all", False)))
        ToggleSwitch(wrow, self._watch_all_var).pack(side="left")
        _wa_lbl = tk.Label(wrow, text="Watch all my installed games",
                           font=(F, 9, "bold"), bg=COLORS["bg"],
                           fg=COLORS["text"])
        _wa_lbl.pack(side="left", padx=(10, 0))
        try:
            Tooltip(_wa_lbl,
                    "Watch games on this PC automatically. Turn off to use the list below.")
        except Exception:
            pass
        try:
            self._watch_all_var.trace_add("write", self._on_watch_all)
        except Exception:
            pass
        self._rebuild_game_rows()

        # --- Manual session now (merged from the former Game Night card,
        # audit fix: that card was a thin, easily-missed duplicate of this
        # exact preset with no way to tell it apart from Auto-Pilot. Same
        # tweaks, same apply/revert engine, same fresh-keys-only rule —
        # just started by hand instead of by a detected game.) ---
        # UI fix (less busy): header + description condensed to one line.
        tk.Frame(body, bg=COLORS["hairline"], height=1).pack(
            fill="x", pady=(12, 8))
        tk.Label(body,
                 text="Game Mode — boosts FPS now; turning it off "
                      "puts everything back.",
                 font=(F, 9, "bold"), bg=COLORS["bg"], fg=COLORS["text"],
                 wraplength=680, justify="left", anchor="w").pack(
                     fill="x", pady=(0, 6))
        try:
            from app.game_night import CLOSEABLE_APPS as _CLOSEABLE_APPS
            from app.config_persist import get_game_night as _get_gn
            gn = _get_gn() or {}
        except Exception:
            _CLOSEABLE_APPS, gn = (), {}
        chosen = set(gn.get("close_apps") or [])
        self._close_vars = {}
        if _CLOSEABLE_APPS:
            tk.Label(body, text="Also close while I play (optional)",
                     font=(F, 9, "bold"), bg=COLORS["bg"],
                     fg=COLORS["subtext"], anchor="w").pack(
                         fill="x", pady=(0, 2))
            # UI fix: this box was a cramped 110px showing barely 2 rows
            # of a 6-row list, forcing a scrollbar for almost nothing.
            # Space reclaimed above (merged toggle/preset row, condensed
            # "Start a session now" text) goes straight here instead.
            close_panel = ScrollableRoundedPanel(body)
            close_panel.config(height=165)
            close_panel.pack(fill="x", pady=(0, 8))
            holder = close_panel.inner
            for key, label, note, _exes in _CLOSEABLE_APPS:
                row = tk.Frame(holder, bg=COLORS["bg_alt"])
                var = tk.BooleanVar(value=(key in chosen))
                self._close_vars[key] = var
                ToggleSwitch(row, variable=var,
                             command=self._save_close_choice).pack(
                                 side="left", padx=(10, 10), pady=6)
                text = tk.Frame(row, bg=COLORS["bg_alt"])
                text.pack(side="left", fill="x", expand=True)
                tk.Label(text, text=label, font=(F, 9, "bold"),
                         bg=COLORS["bg_alt"], fg=COLORS["text"],
                         anchor="w").pack(fill="x")
                tk.Label(text, text=note, font=(F, 8), bg=COLORS["bg_alt"],
                         fg=COLORS["subtext"], anchor="w").pack(fill="x")
                row.pack(fill="x", pady=1)
            try:
                close_panel.refresh_scroll()
            except Exception:
                pass
        mbrow = tk.Frame(body, bg=COLORS["bg"])
        mbrow.pack(fill="x", pady=(2, 0))
        self._manual_btn = AnimatedButton(
            mbrow, text="", command=self._toggle_manual_session,
            bg=COLORS["accent_green"], fg=COLORS["black"],
            font=(F, 10, "bold"), padx=20, pady=8)
        self._manual_btn.pack(side="left")
        self._refresh_manual_btn()

        # honesty footer + Done
        tk.Label(body, text="Only watches while this app is open. Turning it off "
                            "never undoes anything — use Undo Tweaks for that.",
                 font=(F, 8), bg=COLORS["bg"], fg=COLORS["subtext"],
                 wraplength=440, justify="left").pack(
                     anchor="w", pady=(10, 0))
        brow = tk.Frame(body, bg=COLORS["bg"])
        brow.pack(fill="x", pady=(10, 6))
        AnimatedButton(brow, text="Done", command=self.close,
                       bg=TAB_ACCENTS["Tweak"], fg=COLORS["black"],
                       font=(F, 9, "bold"), padx=22,
                       pady=7).pack(side="right")

        # register for live watcher status, then paint current state;
        # closing unregisters (base close hook)
        self.on_close(self._unregister)
        try:
            app._pilot_dialog = self
        except Exception:
            pass
        self.refresh_live_status()

    def _unregister(self):
        try:
            if getattr(self.app, "_pilot_dialog", None) is self:
                self.app._pilot_dialog = None
        except Exception:
            pass

    # ---- behavior ------------------------------------------------------ #

    def _save(self, **kv):
        # F08: single-lock RMW (was load; mutate; save with lock released).
        try:
            from app.config_persist import update_config as _update
            def _mut(cfg, kv=kv):
                for k, v in kv.items():
                    cfg[k] = v
            _update(_mut)
        except Exception:
            pass

    def _user_games(self):
        try:
            from app.config_persist import load_config as _load
            games = _load().get("session_pilot_games", [])
            return [g for g in games if isinstance(g, str) and g.strip()]
        except Exception:
            return []

    def _on_toggle(self, *_a):
        on = bool(self._enabled_var.get())
        self._save(session_pilot_enabled=on)
        try:
            if on:
                self.app._pilot_start()
            else:
                self.app._pilot_stop()
        except Exception:
            pass
        self.refresh_live_status()

    def _on_preset(self, _ev=None):
        try:
            name = self._preset_var.get()
        except Exception:
            return
        if name in (self._preset_names or []):
            self._save(session_pilot_preset=name)

    def _rebuild_game_rows(self):
        for w in list(self._games_body.winfo_children()):
            try:
                w.destroy()
            except Exception:
                pass
        # merged watchlist view: user paths (display names) + legacy names
        names_map = {}
        try:
            from app.config_persist import load_config as _load
            m = _load().get("session_pilot_names")
            if isinstance(m, dict):
                names_map = {str(k): str(v) for k, v in m.items()}
        except Exception:
            pass
        paths = self._user_paths()
        games = self._user_games()
        items = [(names_map.get(p, os.path.basename(p)), p,
                  True) for p in paths]
        items += [(g, g, False) for g in games]
        if not items:
            tk.Label(self._games_body,
                     text="(none yet — the built-in list still watches)",
                     font=(F, 9), bg=COLORS["bg"],
                     fg=COLORS["subtext"]).pack(anchor="w", pady=2)
            return
        for disp, ident, is_path in items[:self._MAX_SHOWN_GAMES]:
            row = tk.Frame(self._games_body, bg=COLORS["bg_alt"])
            tag = "📁" if is_path else "·"
            tk.Label(row, text=tag, font=(F, 8),
                     bg=COLORS["bg_alt"], fg=COLORS["subtext"]).pack(
                         side="left", padx=(8, 2))
            lbl = tk.Label(row, text=disp, font=(F, 9, "bold"),
                           bg=COLORS["bg_alt"], fg=COLORS["text"],
                           anchor="w")
            lbl.pack(side="left", padx=(0, 4), pady=4)
            try:
                Tooltip(lbl, ident)      # full path on hover
            except Exception:
                pass
            x = tk.Label(row, text="✕", font=(F, 8),
                         bg=COLORS["bg_alt"], fg=COLORS["subtext"],
                         cursor="hand2")
            x.pack(side="right", padx=(0, 8))
            x.bind("<Button-1>", lambda e, n=ident: self._remove_game(n))
            Tooltip(x, "Stop watching this game")
            row.pack(fill="x", pady=1)
        if len(items) > self._MAX_SHOWN_GAMES:
            tk.Label(self._games_body,
                     text=f"+{len(items) - self._MAX_SHOWN_GAMES} more watched",
                     font=(F, 8), bg=COLORS["bg"],
                     fg=COLORS["subtext"]).pack(anchor="w", pady=(2, 0))

    def _user_paths(self):
        try:
            from app.config_persist import load_config as _load
            paths = _load().get("session_pilot_paths", [])
            return [p for p in paths if isinstance(p, str) and p.strip()]
        except Exception:
            return []

    def _on_watch_all(self, *_a):
        self._save(session_pilot_watch_all=bool(self._watch_all_var.get()))
        try:
            self.app._pilot_refresh_watchlist()
        except Exception:
            pass
        self.refresh_live_status()

    def _open_add_menu(self):
        """Small popup menu for the three ways to grow the watchlist
        (Phase-5) — collapsed from three separate buttons into one, per
        user feedback that the row read as cluttered."""
        try:
            menu = tk.Menu(self._dlg, tearoff=0, bg=COLORS["surface"],
                           fg=COLORS["text"], activebackground=TAB_ACCENTS["Tweak"],
                           activeforeground=COLORS["black"], bd=0)
            menu.add_command(label="Installed games…", command=self._pick_from_games)
            menu.add_command(label="Installed apps…", command=self._pick_from_apps)
            menu.add_command(label="Browse for a file…", command=self._add_game)
            x = self._add_btn.winfo_rootx()
            y = self._add_btn.winfo_rooty() + self._add_btn.winfo_height()
            menu.tk_popup(x, y)
        except Exception:
            pass

    def _pick_from_games(self):
        """Searchable installed-games picker (Steam/Epic/GOG/Ubisoft/
        Riot/Xbox/EA via app.game_catalog). The enumeration runs in the
        picker's worker thread — the dialog opens instantly with a
        Scanning placeholder; a big library on a cold disk never blocks
        the click (user bug report: first-click freeze-then-jerk)."""
        try:
            _CatalogPickerDialog(
                self._dlg, "Pick your installed games",
                accent=TAB_ACCENTS["Tweak"],
                scan_fn=_scan_installed_games,
                on_pick=self._add_catalog_game,
                empty_text="No installed games found — use Browse for "
                           "portable exes.").wait()
        except Exception:
            pass

    def _pick_from_apps(self):
        """Searchable installed-apps picker (registry uninstall hives —
        every program with a runnable exe, redists filtered). Async like
        games above, same reason."""
        try:
            _CatalogPickerDialog(
                self._dlg, "Pick an installed app",
                accent=TAB_ACCENTS["Tweak"],
                scan_fn=_scan_installed_apps,
                on_pick=self._add_catalog_game,
                empty_text="No installed apps found.").wait()
        except Exception:
            pass

    def _add_catalog_game(self, g):
        """Add a picked catalog entry: full PATH when the catalog
        resolved a real exe (dir-pinned matching — same-named exes
        elsewhere never fire), else fall back to the bare name."""
        path = (g.get("path") or "").strip()
        name = g.get("name") or "?"
        if path:
            paths = self._user_paths()
            if path not in paths:
                paths.append(path)
                self._save(session_pilot_paths=paths)
                self._remember_display_name(path, name)
        else:
            # no confident exe (rare — folder-only entry): watch a name
            # hint so the user's pick still does something visible
            from app.session_pilot import normalize_exe as _norm
            exe = _norm(name)
            games = self._user_games()
            if exe and exe not in games:
                games.append(exe)
                self._save(session_pilot_games=games)
        try:
            self.app._pilot_refresh_watchlist()
        except Exception:
            pass
        self._rebuild_game_rows()

    def _remember_display_name(self, path, name):
        """Display-name map for path picks (config: dict path->name) so
        the watchlist shows 'Baldur's Gate 3', not a 90-char path."""
        # F08: single-lock RMW.
        try:
            from app.config_persist import update_config as _update
            def _mut(cfg, path=path, name=name):
                m = cfg.get("session_pilot_names")
                if not isinstance(m, dict):
                    m = {}
                m[path] = name
                cfg["session_pilot_names"] = m
            _update(_mut)
        except Exception:
            pass

    def _add_game(self):
        try:
            from tkinter import filedialog as _fd
            path = _fd.askopenfilename(
                parent=self._dlg, title="Pick the game's .exe file",
                filetypes=[("Executables", "*.exe"), ("All files", "*.*")])
        except Exception:
            return
        if not path:
            return
        # Browse keeps the FULL path now (path-verified matching); the
        # legacy basename storage remains for hand-typed old configs
        paths = self._user_paths()
        if path not in paths:
            paths.append(path)
            self._save(session_pilot_paths=paths)
            self._remember_display_name(path, os.path.basename(path))
        try:
            self.app._pilot_refresh_watchlist()
        except Exception:
            pass
        self._rebuild_game_rows()

    def _remove_game(self, name):
        # name here is the raw row identity (a path or a bare exe)
        if "\\" in name or "/" in name:
            paths = [p for p in self._user_paths() if p != name]
            self._save(session_pilot_paths=paths)
        else:
            games = [g for g in self._user_games() if g != name]
            self._save(session_pilot_games=games)
        try:
            self.app._pilot_refresh_watchlist()
        except Exception:
            pass
        self._rebuild_game_rows()

    def refresh_live_status(self):
        """Paint ● Watching / 🎮 active / ○ Off from live pilot state."""
        try:
            pilot = getattr(self.app, "_pilot", None)
            running = bool(pilot is not None and pilot.running())
        except Exception:
            running = False
        try:
            hit = getattr(pilot, "active_hit", None) if running else None
        except Exception:
            hit = None
        try:
            if not running:
                self._live_lbl.config(text="○ Off", fg=COLORS["subtext"])
            elif hit:
                self._live_lbl.config(text=f"🎮 {hit} — session active",
                                      fg=COLORS["accent_green"])
            else:
                self._live_lbl.config(text="● Watching…",
                                      fg=TAB_ACCENTS["Tweak"])
        except Exception:
            pass
        self._refresh_manual_btn()

    def _save_close_choice(self, *_a):
        try:
            from app.config_persist import set_game_night_close_apps
            set_game_night_close_apps(
                [k for k, v in self._close_vars.items() if v.get()])
        except Exception:
            pass

    def _active_manual(self):
        try:
            from app.config_persist import get_game_night
            return bool(get_game_night().get("active"))
        except Exception:
            return False

    def _refresh_manual_btn(self):
        """Reflect the actual on-disk session state — kept in sync from
        refresh_live_status so this stays correct even if the session was
        started/ended from somewhere other than this dialog's own button."""
        try:
            on = self._active_manual()
            # plain On/Off switch look: green when on, neutral when off
            if on:
                self._manual_btn.config_text("Game Mode: On")
                self._manual_btn.set_style(
                    bg=COLORS["accent_green"], fg=COLORS["black"],
                    hover_bg=_hex_lerp(COLORS["accent_green"], "#FFFFFF", 0.08))
            else:
                self._manual_btn.config_text("Game Mode: Off")
                self._manual_btn.set_style(
                    bg=COLORS["surface"], fg=COLORS["text"],
                    hover_bg=_hex_lerp(COLORS["surface"], "#FFFFFF", 0.08))
        except Exception:
            pass

    def _toggle_manual_session(self):
        try:
            if self._active_manual():
                self.app.end_game_night()
            else:
                self.app.start_game_night()
        except Exception:
            pass
        self._refresh_manual_btn()

    def set_live_status(self, text, color=None):
        try:
            self._live_lbl.config(text=text,
                                  fg=color or COLORS["subtext"])
        except Exception:
            pass
