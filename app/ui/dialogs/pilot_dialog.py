"""PilotDialog — extracted verbatim from app/gui.py (H12, batch 1).

One dialog per module, mirroring app/ui/widgets. Depends only on
app.ui below it, plus the app.utils helpers it already used; it
knows nothing
about any other dialog except where noted.
"""
from __future__ import annotations

import os

import tkinter as tk

from app.ui.widgets.rounded_card import RoundedCard

from app.ui.widgets.dropdown import M3Dropdown
from tkinter import ttk, messagebox
from app.ui.base import Tooltip, _themed_askyesno, _themed_showinfo
from app.ui.dialogs._tool_page import ToolPage, fit_label
from app.ui.theme import (COLORS, F, TAB_ACCENTS, ICON_ROW_PX, _hex_lerp,
                          ellipsize, icon_bg_rgb, _measure_font)
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


# The Game Mode close list knows each app only by PROCESS NAME — that is all
# Game Mode ever stores (see game_night.CLOSEABLE_APPS / custom_exe_name). To
# show an icon the name has to be resolved back to a real file, so this uses
# four read-only sources, best first:
#   1. the registry's App Paths entry for the exe (chrome.exe -> the real one);
#   2. the RUNNING image path — the exact source Process Manager trusts, so
#      the same app shows the same picture on both surfaces;
#   3. a bounded sweep of the standard install roots for a file of that name
#      (an installed app that simply is not running right now);
#   4. the installed-apps registry, matched on the exe's basename.
# All of it runs on a worker thread; a name none of them can locate simply
# leaves its reserved slot empty, exactly as Process Manager does for an app
# with no artwork.
_APP_PATH_HIVES = (
    r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths",
    r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\App Paths",
)


def _app_path_lookup(exe):
    """Full path for an exe name from the registry App Paths keys, or ""."""
    name = str(exe or "").strip().lower()
    if not name:
        return ""
    try:
        import winreg
    except Exception:
        return ""
    for base in _APP_PATH_HIVES:
        for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
            try:
                with winreg.OpenKey(hive, base + "\\" + name) as key:
                    path, _t = winreg.QueryValueEx(key, "")
            except OSError:
                continue
            except Exception:
                continue
            try:
                path = os.path.expandvars(str(path or "").strip().strip('"'))
            except Exception:
                continue
            if path and os.path.isfile(path):
                return path
    return ""


def _resolve_close_icon_specs(exe_names):
    """{exe name (lower): icon source 'path[,index]'} for the names here that
    can be located on this PC. Never raises; an unresolvable name is simply
    absent from the result."""
    wanted = []
    for n in exe_names:
        low = str(n or "").strip().lower()
        if low and low not in wanted:
            wanted.append(low)
    if not wanted:
        return {}
    found = {}
    for name in wanted:
        path = _app_path_lookup(name)
        if path:
            found[name] = path
    remaining = [n for n in wanted if n not in found]
    if remaining:
        try:
            from app.process_manager import _process_image_path, _tasklist_csv
            pids = {}
            for img, pid, _mem in _tasklist_csv():
                low = str(img or "").strip().lower()
                if low in remaining and low not in pids:
                    try:
                        pids[low] = int(pid)
                    except Exception:
                        pass
            for low, pid in pids.items():
                path = _process_image_path(pid)
                if path:
                    found[low] = path
        except Exception:
            pass
    remaining = [n for n in wanted if n not in found]
    if remaining:
        try:
            found.update(_sweep_roots_for(remaining))
        except Exception:
            pass
    remaining = [n for n in wanted if n not in found]
    if remaining:
        try:
            from app.game_catalog import installed_apps
            for g in installed_apps() or []:
                base = os.path.basename(
                    str(g.get("path") or "")).strip().lower()
                if base in remaining and base not in found:
                    found[base] = g.get("path")
        except Exception:
            pass
    return found


#: Standard per-user / machine install roots, most-likely first. Almost every
#: modern desktop app on this list lives under one of them (Chrome under
#: LocalAppData\Google\..., Discord under LocalAppData\Discord\..., Slack under
#: LocalAppData\slack\..., Spotify under AppData\Spotify\...).
_ICON_SEARCH_ROOTS = ("LocalAppData", "AppData", "ProgramData",
                      "ProgramFiles", "ProgramFiles(x86)")
_ICON_SEARCH_DEPTH = 3      # exe at <root>\Vendor\App\bin\app.exe
_ICON_SEARCH_CAP = 6000     # entries visited before giving up


def _sweep_roots_for(names):
    """{name: path} for any of `names` found under the standard install roots.

    One bounded BFS for ALL the still-unresolved names, so several missing
    apps cost one walk rather than one walk each. Depth- and hit-capped
    because this runs on a worker thread and an exhaustive walk of
    Program Files is not an option — an app whose exe is nested deeper than
    the cap (or not under these roots at all) is simply left without an
    icon, which its reserved slot already handles.
    """
    want = {str(n).strip().lower() for n in names if n}
    if not want:
        return {}
    found = {}
    roots = []
    for var in _ICON_SEARCH_ROOTS:
        p = os.environ.get(var)
        if p:
            roots.append(os.path.expandvars(p))
    hits = 0
    for root in roots:
        if len(found) >= len(want):
            break
        if not os.path.isdir(root):
            continue
        frontier = [(root, 0)]
        while frontier and hits < _ICON_SEARCH_CAP and len(found) < len(want):
            nxt = []
            for current, depth in frontier:
                if hits >= _ICON_SEARCH_CAP:
                    break
                try:
                    it = os.scandir(current)
                except OSError:
                    continue
                with it:
                    for entry in it:
                        hits += 1
                        if hits > _ICON_SEARCH_CAP:
                            break
                        try:
                            if entry.is_dir(follow_symlinks=False):
                                if depth < _ICON_SEARCH_DEPTH:
                                    nxt.append((entry.path, depth + 1))
                                continue
                            low = entry.name.lower()
                            if low in want and low not in found and \
                                    entry.is_file(follow_symlinks=False):
                                found[low] = entry.path
                        except OSError:
                            continue
            frontier = nxt
    return found




class PilotDialog(ToolPage):
    """Game Session Auto-Pilot setup (user-approved feature 6, 2026-09):
    master switch, session-preset picker, and the user's own .exe
    watchlist. Plain language throughout — this dialog is for gamers,
    not tweakers. Turning it off (or closing the app) never reverts
    anything; applied tweaks stay badged until Undo reverts them.

    Phase-5: the watchlist has three ways to grow — Pick from installed
    games (launcher-aware catalog), Pick from installed apps (registry),
    and the classic Browse for portable exes. 'Watch all my installed
    games' folds the whole catalog in automatically (opt-in).

    REDESIGN 2026-10: on the shared ToolPage shell — a Header banner and the
    live pilot readout on the shared StatusLine (set_live_status keeps its
    name). The preset cards, watchlist and Game Mode logic are unchanged.
    fit=True: the content zone auto-fits (fills the window when it fits,
    scrolls only if a window too small for the page is somehow reached).

    LAYOUT 2026-10 (popup-fit pass): the three cards now sit in two columns
    (watcher + watchlist left, Game Mode right) and the close-list is a
    content-sized box rather than a fixed 150px scroller, so the popup needs
    no scrollbar of its own at any window size it is opened at. The page is
    still fit=True for the case where a window is smaller than any layout can
    cover: it then scrolls rather than cutting the Game Mode card off."""

    _MAX_SHOWN_GAMES = 6

    #: One-line hints for the close-list rows. Rows are icon + name only now
    #: (per user request the description text is gone), so these are no longer
    #: painted; they are kept because the full sentence is what CLOSEABLE_APPS
    #: carries and remains the row tooltip, and these short forms stay the
    #: reviewed wording for those facts.
    _CLOSE_HINTS = {
        "browsers": "top memory user",
        "discord": "unless you use voice chat",
        "spotify": "music app + helper",
        "onedrive": "pauses sync until restart",
        "chat": "work chat apps",
        "creative": "updater + sync",
    }

    #: Shorter display names for rows whose CLOSEABLE_APPS label is longer
    #: than the row reads well with (e.g. "Adobe Creative Cloud"). The tooltip
    #: carries the full sentence and the close report/logs still use the
    #: table's own label.
    _CLOSE_LABELS = {
        "creative": "Creative Cloud",
        "overlay": "Overwolf",
        "clouddrives": "Dropbox / Drive",
    }

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
                         accent=TAB_ACCENTS["Tweak"],
                         scope="Applied tweaks stay until Undo reverts them.",
                         scroll=False, fit=True)
        self.banner.set_state("\U0001F3AE", TAB_ACCENTS["Tweak"])
        self.banner.set_title("Ready to play in one click")
        self.banner.set_sub("Turn on the right settings automatically when a "
                            "game starts.")
        dlg = self._dlg
        body = self.content

        CARD = COLORS["bg_alt"]

        # Two columns since the 2026-10 popup-fit pass. Everything this page
        # holds stacked in ONE column came to 616px against a 517px content
        # zone at 150% DPI, i.e. a scrollbar in the popup. Grid + uniform, not
        # pack(side="left"): an unconfigured Canvas reports Tk's default 10cm
        # width (567px at 150% DPI) and several widgets here are canvases, so
        # each column would "request" 567px, pack would hand all of it to the
        # first column and squeeze the second to whatever was left. Uniform
        # columns split the width that actually exists, evenly, and each
        # column packs its cards top-down independently - so a tall card on
        # the right can't push the left column's second card down with it.
        cols = tk.Frame(body, bg=COLORS["bg"])
        cols.pack(fill="both", expand=True)
        cols.grid_columnconfigure(0, weight=1, uniform="pilcol")
        cols.grid_columnconfigure(1, weight=1, uniform="pilcol")
        col_l = tk.Frame(cols, bg=COLORS["bg"])
        col_l.grid(row=0, column=0, sticky="new", padx=(0, 5))
        col_r = tk.Frame(cols, bg=COLORS["bg"])
        col_r.grid(row=0, column=1, sticky="new", padx=(5, 0))

        def _card(col, last=False):
            c = RoundedCard(col, fill=CARD, bg=COLORS["bg"], radius=16)
            c.pack(fill="x", pady=(0, 0 if last else 10))
            inner = tk.Frame(c.body, bg=CARD)
            inner.pack(fill="x", padx=18, pady=14)
            # A RoundedCard sizes itself from its body on the body's
            # <Configure>, but content that grows LATER (the close list
            # arriving from its background scan) changes the body's REQUESTED
            # size without resizing the body widget itself, so no <Configure>
            # fires and the card would stay at its first-layout height. Hand
            # the card to the content frame so such a caller can re-measure
            # it (see _refit_close_card).
            inner.card = c
            return inner

        p1 = _card(col_l)
        mrow = tk.Frame(p1, bg=CARD)
        mrow.pack(fill="x")
        self._enabled_var = tk.BooleanVar(
            value=bool(cfg.get("session_pilot_enabled", False)))
        ToggleSwitch(mrow, self._enabled_var).pack(side="left")
        tk.Label(mrow, text="Watch for games", font=(F, 12, "bold"),
                 bg=CARD, fg=COLORS["text"]).pack(
                     side="left", padx=(10, 0))
        # USER FIX 2026-10 (profile picker clipped): the picker used to share
        # the switch's LEFTOVER space and got 55px of the 218px it asked for,
        # so the profile name rendered as "Gam…" (user screenshot). It has
        # its own line now and stretches across the card; M3Dropdown draws its
        # own text through ellipsize(), so no preset name can be cut again.
        prow = tk.Frame(p1, bg=CARD)
        prow.pack(fill="x", pady=(8, 0))
        _prof = tk.Label(prow, text="Profile", font=(F, 10), bg=CARD,
                         fg=COLORS["subtext"])
        _prof.pack(side="left")
        try:
            Tooltip(_prof, "Which set of tweaks Game Mode applies.")
        except Exception:
            pass
        self._preset_var = tk.StringVar(value=preset)
        combo = self._preset_combo = M3Dropdown(
            prow, textvariable=self._preset_var,
            values=self._preset_names, state="readonly",
            width=14, font=(F, 10))
        combo.pack(side="left", fill="x", expand=True, padx=(10, 0))
        try:
            combo.bind("<<ComboboxSelected>>", self._on_preset)
        except Exception:
            pass
        try:
            self._enabled_var.trace_add("write", self._on_toggle)
        except Exception:
            pass

        # watchlist
        p2 = _card(col_l, last=True)
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
        _sub = ("Every installed game is watched" if watching_all
                else f"{n_known} built-in games, plus any you add")
        _title = tk.Label(p2, text="Your watchlist",
                          font=(F, 12, "bold"), bg=CARD,
                          fg=COLORS["text"], anchor="w")
        _title.pack(fill="x")
        _sublbl = tk.Label(p2, text=_sub, font=(F, 10), bg=CARD,
                           fg=COLORS["subtext"], anchor="w")
        _sublbl.pack(fill="x", pady=(1, 0))
        # USER FIX 2026-10 (headline sheared): the old single line read
        # "Watching 25 built-in games plus yours" -- wider than the
        # two-column card, and a Label that is handed less width than its
        # text clips at BOTH ends because its anchor is centre, so the user
        # saw "/atching ... your". Headline and supporting line are separate
        # now (M3: title + supporting text) and both run through _fit_label,
        # which ellipsizes to the real width instead of shearing.
        self._fit_label(p2, _title, "Your watchlist")
        self._fit_label(p2, _sublbl, _sub)
        self._games_body = tk.Frame(p2, bg=CARD)
        self._games_body.pack(fill="x", pady=(4, 0))
        # three ways to grow the watchlist (Phase-5): installed games,
        # installed apps, and the classic drive browse for portable exes.
        # UI fix (less clutter): collapsed the three separate buttons that
        # used to sit here into one "+ Add games…" button with a small
        # picker menu — same three ways in, one thing to look at.
        addrow = tk.Frame(p2, bg=CARD)
        addrow.pack(fill="x", pady=(6, 0))
        self._add_btn = AnimatedButton(addrow, text="Add games",
                       command=self._open_add_menu,
                       bg=CARD, fg=COLORS["primary"], variant="outlined",
                       font=(F, 10, "bold"), padx=20, pady=8)
        self._add_btn.pack(side="left")
        # watch-all toggle (default ON for new users — user ruling
        # 2026-09-12; anyone whose config already carries a choice keeps
        # it. Detection is path-verified, never guessed.)
        wrow = tk.Frame(p2, bg=CARD)
        wrow.pack(fill="x", pady=(8, 0))
        self._watch_all_var = tk.BooleanVar(
            value=bool(cfg.get("session_pilot_watch_all", False)))
        ToggleSwitch(wrow, self._watch_all_var).pack(side="left")
        _wa_lbl = tk.Label(wrow, text="Watch all my installed games",
                           font=(F, 11), bg=CARD,
                           fg=COLORS["text"], anchor="w")
        _wa_lbl.pack(side="left", padx=(10, 0))
        self._fit_label(wrow, _wa_lbl, "Watch all my installed games",
                        reserve=62)
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
        p3 = _card(col_r, last=True)
        self._close_card = getattr(p3, "card", None)
        tk.Label(p3,
                 text="Game Mode",
                 font=(F, 12, "bold"), bg=CARD, fg=COLORS["text"],
                 justify="left", anchor="w").pack(
                     fill="x", pady=(0, 6))
        try:
            from app.game_night import CLOSEABLE_APPS as _CLOSEABLE_APPS
            from app.config_persist import get_game_night as _get_gn
            gn = _get_gn() or {}
        except Exception:
            _CLOSEABLE_APPS, gn = (), {}
        self._custom_apps = [c for c in (gn.get("custom_apps") or [])
                             if isinstance(c, str) and c.strip()]
        self._close_vars = {}
        # Icon state for the close rows: a per-row PhotoImage cache (so
        # repainting the list never re-extracts) and the async plumbing.
        self._close_icon_cache = {}      # row key -> PhotoImage
        self._close_icon_done = set()    # row keys already attempted
        self._close_icon_slots = {}      # row key -> (icon Label, exe names)
        # exe name (lower) -> on-disk icon source for the apps found on this
        # PC, plus whether the background scan that fills it has finished.
        # The list shows a built-in option only once its exe is in here, so
        # the box only ever offers apps the user actually has.
        self._present_specs = {}
        self._specs_ready = False
        self._scan_inflight = False
        if _CLOSEABLE_APPS:
            _c3_hdr = tk.Label(p3, text="Close these while I play (optional)",
                               font=(F, 10), bg=CARD,
                               fg=COLORS["subtext"], anchor="w")
            _c3_hdr.pack(fill="x", pady=(0, 2))
            self._fit_label(p3, _c3_hdr, "Close these while I play (optional)")
            # Popup-fit pass 2026-10: this used to be a box pinned to 150px
            # holding two-line rows — 6 x 64px = 384px of list, so barely two
            # rows were visible and the rest scrolled. The rows are one line
            # now (switch, app icon, name; the full sentence lives in the
            # name's tooltip) and the box is sized to exactly what it holds,
            # so the whole list is visible and the panel has nothing to
            # scroll.
            # UI REQUEST 2026-10: the rows carry NO fill (they sit on the
            # panel, exactly like Process Manager's list) and show each app's
            # icon beside its name — no description text on the row itself.
            # The rows are built by _rebuild_close_rows, because adding or
            # removing one of YOUR apps has to repaint the whole list.
            self._close_panel = ScrollableRoundedPanel(p3)
            self._close_panel.pack(fill="x", pady=(0, 6))
            self._close_holder = self._close_panel.inner
            # USER FEATURE 2026-10 (user request: "no option to add custom
            # apps"): your own programs in the close list. Any .exe on the
            # disk may be picked, but only its NAME is ever stored (see
            # game_night.custom_exe_name) — nothing here can be launched and
            # nothing reaches a shell; Game Mode only asks that process to
            # quit, by name.
            self._add_app_btn = AnimatedButton(
                p3, text="Add app", command=self._add_custom_app,
                bg=CARD, fg=COLORS["primary"], variant="outlined",
                font=(F, 10, "bold"), padx=20, pady=8)
            self._add_app_btn.pack(anchor="w", pady=(0, 8))
            try:
                Tooltip(self._add_app_btn,
                        "Pick any program (.exe) to close while Game Mode is "
                        "on. It is asked to quit politely — never force-killed.")
            except Exception:
                pass
            self._begin_close_scan()
        mbrow = tk.Frame(p3, bg=CARD)
        mbrow.pack(fill="x", pady=(2, 0))
        self._manual_btn = AnimatedButton(
            mbrow, text="", command=self._toggle_manual_session,
            bg=COLORS["accent_green"], fg=COLORS["black"],
            font=(F, 10, "bold"), padx=20, pady=8)
        self._manual_btn.pack(side="left")
        self._refresh_manual_btn()

        # honesty footer + Done
        #
        # AUDIT B3: this read "Only watches while this app is open. Turning it
        # off never undoes anything - use Undo Tweaks for that." The first
        # sentence is about the WATCHER, and reads as though it covered the
        # whole dialog - which made the Game Mode button below look like it
        # reverts when you close the app. It does not: nothing reverts on
        # shutdown, so applied tweaks persist until End is pressed. Now stated
        # as two separate facts, because they are two separate facts.
        #
        # REDESIGN 2026-10: the footer is the shared ToolPage one — Done is
        # its one primary action and the live pilot readout is the StatusLine
        # on the left.
        self.add_button("Done", self.close, kind="primary")

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
                     text="No custom games added yet",
                     font=(F, 10), bg=COLORS["bg_alt"],
                     fg=COLORS["subtext"]).pack(anchor="w", pady=2)
            return
        for disp, ident, is_path in items[:self._MAX_SHOWN_GAMES]:
            row = tk.Frame(self._games_body, bg=COLORS["bg_alt"])
            tag = "📁" if is_path else "·"
            tk.Label(row, text=tag, font=(F, 9),
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
            x = tk.Label(row, text="✕", font=(F, 9),
                         bg=COLORS["bg_alt"], fg=COLORS["subtext"],
                         cursor="hand2")
            x.pack(side="right", padx=(0, 8))
            x.bind("<Button-1>", lambda e, n=ident: self._remove_game(n))
            Tooltip(x, "Stop watching this game")
            row.pack(fill="x", pady=1)
        if len(items) > self._MAX_SHOWN_GAMES:
            tk.Label(self._games_body,
                     text=f"+{len(items) - self._MAX_SHOWN_GAMES} more watched",
                     font=(F, 9), bg=COLORS["bg"],
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

    # ---- close-list (Game Mode) ---------------------------------------- #

    @staticmethod
    def _clip_row_label(text, n=26):
        """Hard cap for one row label: a custom exe name can be 96 chars."""
        t = str(text or "")
        return t if len(t) <= n else t[:n - 1].rstrip() + "…"

    def _rebuild_close_rows(self):
        """Paint the close list: installed built-ins plus your own picks.

        Every row is one line — a switch, the app's icon and its name — and
        the box is resized to what it holds, so the list never scrolls inside
        its own border. The row itself carries no fill: it sits on the panel
        exactly like a Process Manager row, so the list reads as a list rather
        than as a stack of grey boxes. The longer note lives in the row's
        tooltip. Custom rows carry a ✕ that forgets the app again.

        A built-in is listed only when _begin_close_scan has found its app on
        this PC; until the scan lands the box shows a short "checking" line
        rather than six options for programs the user may not have."""
        holder = getattr(self, "_close_holder", None)
        if holder is None:
            return
        for w in list(holder.winfo_children()):
            try:
                w.destroy()
            except Exception:
                pass
        chosen = set()
        try:
            from app.config_persist import get_game_night as _get_gn
            chosen = set((_get_gn() or {}).get("close_apps") or [])
        except Exception:
            pass
        try:
            from app.game_night import (CLOSEABLE_APPS as _CLOSEABLE_APPS,
                                        custom_key as _ckey,
                                        custom_label as _clbl)
        except Exception:
            _CLOSEABLE_APPS, _ckey, _clbl = (), None, None
        present = getattr(self, "_present_specs", None) or {}
        ready = bool(getattr(self, "_specs_ready", False))
        # Built-in options are shown ONLY once the background scan has found
        # their app on this PC — an option for a program that is not installed
        # is clutter, and this is also what guarantees every built-in row can
        # carry a real icon. Custom picks are always shown: the user chose
        # them explicitly, so they stay even if the scan has not (or cannot)
        # resolve them.
        # (key, display label, tooltip note, remove identity, exe names)
        rows = []
        for key, label, note, exes in _CLOSEABLE_APPS:
            if ready and any(str(e).strip().lower() in present for e in exes):
                rows.append((key, self._CLOSE_LABELS.get(key, label), note,
                             None, exes))
        for name in list(getattr(self, "_custom_apps", []) or []):
            key = _ckey(name) if _ckey else None
            if not key:
                continue
            rows.append((key, _clbl(name) if _clbl else str(name),
                         f"{name} — asked to quit (never force-killed) while "
                         f"Game Mode is on.", str(name), (str(name),)))
        self._close_vars = {}
        self._close_icon_slots = {}
        # Retry icon lookups on every rebuild, so an app that started (or was
        # installed) since the dialog opened can pick up its picture; the
        # PhotoImage cache still stops any successful icon being re-extracted.
        self._close_icon_done = set()
        if not rows:
            self._render_close_empty(ready)
            self._resize_close_panel()
            return
        row_bg = COLORS["bg"]
        for key, label, note, remove_ident, exes in rows:
            row = tk.Frame(holder, bg=row_bg)
            var = tk.BooleanVar(value=(key in chosen))
            self._close_vars[key] = var
            ToggleSwitch(row, variable=var,
                         command=self._save_close_choice).pack(
                             side="left", padx=(8, 6), pady=2)
            # Reserved icon slot, sized in PIXELS (a Label's width is in
            # characters, so a Label "32" would be 32 characters across). It
            # is created EMPTY and filled in by _load_close_icons, so the row
            # never reflows when a picture lands — the same fixed slot
            # Process Manager reserves for its rows.
            icon_box = tk.Frame(row, width=ICON_ROW_PX, height=ICON_ROW_PX,
                                bg=row_bg)
            icon_box.pack_propagate(False)
            icon_box.pack(side="left", padx=(6, 10), pady=4)
            icon_lbl = tk.Label(icon_box, bg=row_bg)
            icon_lbl.pack(fill="both", expand=True)
            self._close_icon_slots[key] = (icon_lbl, tuple(exes or ()))
            name_lbl = tk.Label(row, text=self._clip_row_label(label),
                                font=(F, 10, "bold"), bg=row_bg,
                                fg=COLORS["text"], anchor="w")
            name_lbl.pack(side="left")
            if remove_ident is not None:
                x = tk.Label(row, text="✕", font=(F, 9),
                             bg=row_bg, fg=COLORS["subtext"],
                             cursor="hand2")
                # packed before the name on purpose: the ✕ keeps its space
                # when the row is too narrow for everything.
                x.pack(side="right", padx=(6, 10))
                x.bind("<Button-1>", lambda e, n=remove_ident:
                       self._remove_custom_app(n))
                try:
                    Tooltip(x, "Remove this app from the list")
                except Exception:
                    pass
            try:
                Tooltip(name_lbl, note)
            except Exception:
                pass
            row.pack(fill="x", pady=0)
            self._fit_close_row(row, name_lbl)
        self._resize_close_panel()
        self._load_close_icons()

    # ---- which built-ins exist on this PC ------------------------------- #

    def _candidate_exe_names(self):
        """Every exe name the close list could show: the built-in pool plus
        the user's own picks. Used to decide which built-ins exist here."""
        names = []
        try:
            from app.game_night import CLOSEABLE_APPS as _CA
        except Exception:
            _CA = ()
        for _key, _label, _note, exes in _CA:
            names.extend(exes)
        names.extend(getattr(self, "_custom_apps", []) or [])
        return names

    def _begin_close_scan(self):
        """Resolve which built-in close apps exist on this PC, off the Tk
        thread, then paint the list.

        The same resolution the icons use, so it is one pass: the names it
        locates double as (a) the filter for which built-in rows to show and
        (b) the icon source for those rows. The dialog opens instantly; the
        box shows a short "checking" line until the worker posts back.
        """
        if getattr(self, "_scan_inflight", False):
            return
        self._scan_inflight = True
        self._rebuild_close_rows()          # paints the "checking" state
        import threading as _th
        names = self._candidate_exe_names()

        def _work():
            try:
                specs = _resolve_close_icon_specs(names)
            except Exception:
                specs = {}
            try:
                self._dispatch.post(lambda: self._on_close_scan(specs))
            except Exception:
                pass

        try:
            _th.Thread(target=_work, daemon=True,
                       name="PilotCloseScan").start()
        except Exception:
            self._scan_inflight = False

    def _on_close_scan(self, specs):
        self._scan_inflight = False
        try:
            self._present_specs.update(specs or {})
        except Exception:
            self._present_specs = dict(specs or {})
        self._specs_ready = True
        try:
            if self._dlg is not None and self._dlg.winfo_exists():
                self._rebuild_close_rows()
        except Exception:
            pass

    def _render_close_empty(self, ready):
        """The list body when no closeable app is on screen yet.

        Two honest states: still checking, or genuinely nothing to offer.
        Both keep the Add app button below it as the way forward."""
        try:
            text = ("Checking installed apps…" if not ready else
                    "None of the usual background apps were found on this PC. "
                    "Use Add app for your own.")
            tk.Label(self._close_holder, text=text, font=(F, 9),
                     bg=COLORS["bg"], fg=COLORS["subtext"], anchor="w",
                     justify="left", wraplength=max(120, self.body_width(70)),
                     bd=0, highlightthickness=0).pack(
                         anchor="w", padx=10, pady=8)
        except Exception:
            pass

    def _refit_close_card(self):
        """Re-grow the Game Mode card after rows were added out-of-band.

        RoundedCard tracks its body on the body's <Configure>. The close list
        arrives from a background scan, which changes the body's REQUESTED
        height without resizing the body widget itself — so no <Configure>
        fires and the card stays at the height it had when the box still held
        the "checking" line. The body is then too short for its children, pack
        squeezes the close panel and drops the Add app / Game Mode buttons off
        the card entirely. Re-running the card's own sizing after a rebuild
        makes it grow to fit. (Same direct _sync() call tasktab.py already
        makes on its cells.)

        The page's content zone is itself a ScrollableRoundedPanel (this
        dialog opens with fit=True) and it re-measures on ITS OWN <Configure>
        only, so growing a card inside it needs an explicit refresh too — or
        the panel keeps its stale height, shows no scrollbar, and clips the
        bottom of a taller list."""
        try:
            card = getattr(self, "_close_card", None)
            if card is not None and card.winfo_exists():
                card._sync()
        except Exception:
            pass
        try:
            panel = getattr(self, "_content_panel", None)
            if panel is not None and panel.winfo_exists():
                panel.refresh_scroll()
        except Exception:
            pass

    def _resize_close_panel(self):
        """Size the close box to the rows it holds. It only scrolls at all
        when the whole page is taller than a very small window."""
        try:
            panel = self._close_panel
            panel.inner.update_idletasks()
            panel.config(height=panel.inner.winfo_reqheight()
                         + 2 * panel.INSET_Y)
            panel.refresh_scroll()
        except Exception:
            pass
        self._refit_close_card()

    def _fit_close_row(self, row, name_lbl, hint_lbl=None, hint_full=""):
        """Keep a close row on one line at any width.

        With the description text gone the app name is the only thing that
        can be squeezed, so it is ellipsized to the room the row actually
        has. A Tk Label handed less width than its text shears its glyphs,
        which is what this avoids. (The `hint_lbl`/`hint_full` arguments are
        kept for compatibility with the earlier two-line rows; nothing
        passes them any more.)"""
        state = {"w": -1}
        full = str(name_lbl.cget("text"))

        def _apply(event=None):
            try:
                w = int(getattr(event, "width", 0) or 0) or \
                    int(row.winfo_width())
            except Exception:
                return
            if w <= 40 or w == state["w"]:
                return
            state["w"] = w
            # switch (52) + its gaps (14) + icon (32) + its gaps (16) + the
            # ✕'s gutter (34): what is left is the name's room
            room = w - (52 + 14 + ICON_ROW_PX + 16 + 34)
            if room < 40:
                return
            try:
                fm = _measure_font(name_lbl.winfo_toplevel(),
                                   name_lbl.cget("font"))
                name_lbl.config(text=ellipsize(full, fm, room))
            except Exception:
                pass

        try:
            row.bind("<Configure>", _apply, add="+")
        except Exception:
            pass

    # ---- close-list icons ---------------------------------------------- #

    def _load_close_icons(self):
        """Fill each close row's icon slot on a worker thread.

        The spec for each row already came back from the presence scan (stored
        in _present_specs), so this only EXTRACTS pixels — pure GDI, no Tk —
        and comes back with finished PhotoImages. A row whose app nothing
        could locate is simply absent from _present_specs and keeps its empty
        reserved slot. Results are cached per row key, so repainting the list
        (Add app / remove) never re-extracts."""
        slots = getattr(self, "_close_icon_slots", None) or {}
        if not slots:
            return
        specs = getattr(self, "_present_specs", None) or {}
        cache = getattr(self, "_close_icon_cache", None)
        if cache is None:
            self._close_icon_cache = cache = {}
        done = getattr(self, "_close_icon_done", None)
        if done is None:
            self._close_icon_done = done = set()
        pending = []
        for key, (lbl, exes) in slots.items():
            img = cache.get(key)
            if img is not None:
                try:
                    lbl.config(image=img)
                    lbl.image = img
                except Exception:
                    pass
                continue
            if key in done:
                continue
            spec = ""
            for e in exes:
                spec = specs.get(str(e).strip().lower()) or ""
                if spec:
                    break
            pending.append((key, spec))
        if not pending:
            return
        row_bg = COLORS["bg"]
        import threading as _th

        def _work():
            out = []
            for key, spec in pending:
                ppm = None
                if spec:
                    try:
                        from app.icon_extract import icon_ppm_bytes
                        ppm = icon_ppm_bytes(spec, size=ICON_ROW_PX,
                                             bg_rgb=icon_bg_rgb(row_bg))
                    except Exception:
                        ppm = None
                out.append((key, ppm))
            try:
                self._dispatch.post(lambda: self._apply_close_icons(out))
            except Exception:
                pass

        try:
            _th.Thread(target=_work, daemon=True,
                       name="PilotAppIcons").start()
        except Exception:
            pass

    def _apply_close_icons(self, found):
        """Tk-thread half: turn finished PPM bytes into PhotoImages."""
        cache = getattr(self, "_close_icon_cache", None)
        if cache is None:
            self._close_icon_cache = cache = {}
        done = getattr(self, "_close_icon_done", None)
        if done is None:
            self._close_icon_done = done = set()
        slots = getattr(self, "_close_icon_slots", None) or {}
        for key, ppm in found:
            done.add(key)
            if not ppm:
                continue                     # no artwork: slot stays empty
            try:
                img = tk.PhotoImage(data=ppm)
            except Exception:
                continue
            cache[key] = img
            entry = slots.get(key)
            lbl = entry[0] if entry else None
            if lbl is None:
                continue
            try:
                if lbl.winfo_exists():
                    lbl.config(image=img)
                    lbl.image = img          # keep a reference or Tk drops it
            except Exception:
                pass

    def _fit_label(self, container, label, full, reserve=0, min_room=56):
        """Ellipsize `label` to the width `container` really has.

        Thin wrapper over the shared helper in the ToolPage shell (see
        _tool_page.fit_label for the why: a centred Label handed less width
        than its text shears glyphs off both ends). It lives there because
        Storage Insight needs the same treatment for its rows."""
        return fit_label(container, label, full, reserve, min_room)

    def _add_custom_app(self):
        """Add one of YOUR programs to the close-while-playing list.

        Only the exe NAME is kept (game_night.custom_exe_name validates it),
        never the path: Game Mode matches a running process by name and
        hands that name to taskkill as one argv element, so nothing picked
        here can ever be launched or reach a shell."""
        try:
            from tkinter import filedialog as _fd
            path = _fd.askopenfilename(
                parent=self._dlg,
                title="Pick the app to close while you play",
                filetypes=[("Programs", "*.exe"), ("All files", "*.*")])
        except Exception:
            return
        if not path:
            return
        try:
            from app.game_night import custom_exe_name, custom_key
            name = custom_exe_name(os.path.basename(path))
            key = custom_key(name)
        except Exception:
            name, key = None, None
        if not name or not key:
            try:
                _themed_showinfo(
                    self._dlg, "Pick a program file",
                    "Game Mode can only close Windows programs (.exe). "
                    "Nothing was added.")
            except Exception:
                pass
            return
        apps = [a for a in (getattr(self, "_custom_apps", []) or [])
                if str(a).lower() != name.lower()]
        apps.append(name.lower())
        self._custom_apps = apps
        # Remember the picked file as this app's icon source. Only the NAME is
        # ever stored (see the docstring and game_night.custom_exe_name); the
        # path is used here, transiently, purely to draw the row's icon — the
        # same thing Process Manager does with a running image.
        try:
            if getattr(self, "_present_specs", None) is None:
                self._present_specs = {}
            self._present_specs[name.lower()] = path
        except Exception:
            pass
        try:
            from app.config_persist import set_game_night_custom_apps
            set_game_night_custom_apps(apps)
        except Exception:
            pass
        # a freshly added app starts ticked: adding it IS the choice
        chosen = [k for k, v in self._close_vars.items() if v.get()]
        if key not in chosen:
            chosen.append(key)
        try:
            from app.config_persist import set_game_night_close_apps
            set_game_night_close_apps(chosen)
        except Exception:
            pass
        self._rebuild_close_rows()

    def _remove_custom_app(self, name):
        """Forget a user-added app: the row, its tick and the config entry."""
        apps = [a for a in (getattr(self, "_custom_apps", []) or [])
                if str(a).lower() != str(name).lower()]
        self._custom_apps = apps
        try:
            if getattr(self, "_present_specs", None):
                self._present_specs.pop(str(name).strip().lower(), None)
        except Exception:
            pass
        try:
            from app.config_persist import set_game_night_custom_apps
            set_game_night_custom_apps(apps)
        except Exception:
            pass
        try:
            from app.game_night import custom_key as _ckey
            key = _ckey(name)
        except Exception:
            key = None
        if key:
            keep = [k for k, v in self._close_vars.items()
                    if v.get() and k != key]
            try:
                from app.config_persist import set_game_night_close_apps
                set_game_night_close_apps(keep)
            except Exception:
                pass
        self._rebuild_close_rows()

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
                self.status.stop("○ Off", COLORS["subtext"])
            elif hit:
                self.status.stop(f"🎮 {hit} — session active",
                                 COLORS["accent_green"])
            else:
                self.status.stop("● Watching…", TAB_ACCENTS["Tweak"])
        except Exception:
            pass
        self._refresh_manual_btn()

    def _save_close_choice(self, *_a):
        try:
            from app.config_persist import (get_game_night as _get_gn,
                                            set_game_night_close_apps)
            # Preserve any ticked key whose row is NOT on screen right now — a
            # built-in whose app is not installed, or one the scan could not
            # locate. Only the visible rows' state is known here, so anything
            # the user cannot see must not be silently dropped.
            try:
                kept = list((_get_gn() or {}).get("close_apps") or [])
            except Exception:
                kept = []
            visible = set(self._close_vars.keys())
            chosen = [k for k, v in self._close_vars.items() if v.get()]
            chosen += [k for k in kept if k not in visible and k not in chosen]
            set_game_night_close_apps(chosen)
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
                self.set_live_status("Game Mode off", COLORS["accent_green"])
            else:
                if not self._confirm_game_mode():
                    return
                self.app.start_game_night()
                # AUDIT B2: the outcome was going to self.log() - the HIDDEN
                # app log - so an unelevated user was told nothing while the
                # button went green and read "Game Mode: On". Say what
                # actually happened, here, where they can see it.
                self._report_game_mode()
        except Exception as exc:
            self.set_live_status("Could not change Game Mode: %s" % exc,
                                 COLORS["accent_red"])
        self._refresh_manual_btn()

    def _confirm_game_mode(self):
        """Name the tweaks before they are applied. Returns False if declined.

        AUDIT B2: this applied 12 system tweaks - power plan, Game Mode,
        GPU preference, CPU boost, background apps, DVR, crash popups,
        network throttling and more - from ONE unconfirmed click, with no
        preview and no per-tweak result. Every other destructive action in the
        app confirms; this was the exception, and the most consequential one.

        The list is read from the same preset the run uses, so it cannot drift
        from what actually happens. Tweaks already applied are excluded, for
        the same reason the run excludes them: End only reverts what this
        session turned on.
        """
        keys = []
        err = None
        try:
            from app.game_night import preset_task_keys
            preset = list(preset_task_keys())
            # tab_presets.TABS, NOT self.app.tabs - that is the same source
            # Application._game_night_tasks() reads, so the preview cannot
            # describe a different set of tweaks than the run will apply.
            # (self.app.tabs does not even exist until the main UI is built.)
            from app.tab_presets import TABS as _TABS
            by_key = {t.key: t for t in _TABS.get("Tweak", [])}
            already = set()
            try:
                from app.run_state import get_tweak_state
                already = set(get_tweak_state() or {})
            except Exception:
                already = set()
            keys = [k for k in preset if k in by_key and k not in already]
        except Exception as exc:
            # Previously swallowed into `keys = []`, which then hit the
            # "Everything is already on" branch and told the user Game Mode
            # was fully applied when nothing had been inspected at all. A
            # failure to READ the list must be visible, not reassuring.
            err = exc
        if err is not None:
            self.set_live_status("Could not read the Game Mode settings: %s"
                                 % err, COLORS["accent_red"])
            return False
        if not keys:
            # Nothing left to apply: Game Mode is effectively already on for
            # this preset. Say that instead of asking to "apply 0 settings".
            self.set_live_status("Every Game Mode setting is already on.",
                                 COLORS["accent_yellow"])
            return False
        names = []
        for k in keys:
            try:
                names.append("- %s" % by_key[k].label)
            except Exception:
                names.append("- %s" % k)
        body = ("Game Mode changes these %d system settings:\n\n%s\n\n"
                "They stay until you press End, even if you close this app."
                % (len(keys), "\n".join(names[:14])))
        if len(names) > 14:
            body += "\n- and %d more" % (len(names) - 14)
        try:
            # Module-level import, so the harness can patch it. With a late
            # `from app.ui.base import _themed_askyesno` INSIDE this method
            # the patch had nothing to bind to, the real modal opened, and the
            # verifier blocked on it until it was killed. Every other dialog
            # that confirms imports this at module scope for the same reason.
            return _themed_askyesno(
                self._dlg, "Turn Game Mode on?", body,
                accent=TAB_ACCENTS["Tweak"],
                yes_text="Turn it on", no_text="Go Back")
        except Exception:
            # A prompt that cannot build must not silently apply 12 tweaks.
            return False

    def _report_game_mode(self):
        """What actually happened, in the dialog rather than the hidden log."""
        try:
            from app.run_state import get_tweak_state
            on = set(get_tweak_state() or {})
        except Exception:
            on = set()
        try:
            from app.game_night import preset_task_keys
            keys = [k for k in preset_task_keys() if k in on]
        except Exception:
            keys = []
        if not keys:
            self.set_live_status("Game Mode did not change anything.",
                                 COLORS["accent_yellow"])
            return
        try:
            from app.game_night import preset_task_keys
            wanted = len(preset_task_keys())
        except Exception:
            wanted = len(keys)
        if len(keys) >= wanted:
            self.set_live_status("Game Mode on - %d settings applied."
                                 % len(keys), COLORS["accent_green"])
        else:
            self.set_live_status("Game Mode on - %d of %d settings applied. "
                                 "Some need Administrator."
                                 % (len(keys), wanted), COLORS["accent_yellow"])

    def set_live_status(self, text, color=None):
        # REDESIGN 2026-10: the live pilot readout is the shared StatusLine
        # now; the public method keeps its name for outside callers.
        try:
            self.status.stop(text, color or COLORS["subtext"])
        except Exception:
            pass
