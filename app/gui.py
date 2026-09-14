"""
GUI layer — Phase 2 redesign for non-technical gamers.

Design goals:
  * One question answered in under 2 seconds: "which big button do I press?"
  * No console output, no command lines, no log window. A colored animated
    progress bar with plain-language status; the full log stays in memory
    and is exportable to .txt via the 📋 corner icon.
  * Dark theme only (navy palette from user reference), accents per tab:
    mint = Clean, amber = Repair, violet = Tweak, red = Undo.
  * The 3 tabs are one pill-shaped switcher: a thumb slides between
    Clean / Repair / Tweak, recoloring to the selected tab.
  * Presets are big cards. Only "Custom" ever shows a toggle grid.
  * Toggles are animated pill switches (green on / red-muting off), with a
    separate "✓ Active" badge sourced from the real applied-tweak registry,
    so "will run" (toggle) never reads as "is active on this PC" (badge).
  * Run replaces itself with an in-place animated progress bar; Undo
    Tweaks is its own red card on the Tweak tab.
  * Thread-safe: the worker never touches widgets directly; every UI
    update hops to the Tk thread via root.after.

Phase 2 punch-list items implemented here: #12 (tab consolidation),
#13 (preset system), #14 (toggles + applied badges), #15 (Undo flow),
#16 (fading status + hidden full log), #17 (single "Run" label).
"""

import os
import pathlib
import subprocess
import sys
import threading
import time
import tkinter as tk
from tkinter import ttk, messagebox
import tkinter.font as tkfont

from app.config import (
    APP_NAME, WINDOW_SIZE, WINDOW_MIN_SIZE, COLORS, FONT_FAMILY, APP_VERSION,
)
from app.elevation import is_admin, relaunch_as_admin
from app.utils import TaskContext, TaskSkipped, TaskCancelled, format_bytes
from app.tab_presets import TABS, PRESETS, TAB_NAMES
from app.toast import notify_clean_complete, notify_low_space, show_toast
from app.warnings import check_dangerous_combos, check_info_notices
from app.config_persist import get_tweak_state, mark_tweak_applied, mark_tweak_reverted


def _themed_askyesno(parent, title: str, message: str, accent: str = None,
                     yes_text: str = "Continue", no_text: str = "Go Back") -> bool:
    """Dark-themed Yes/No confirm dialog (in-app modal, ThemedModal chrome).

    Returns True on Yes, False on No / X / Escape. Falls back to the
    native messagebox only if the themed window itself fails.
    """
    try:
        # ThemedModal is defined below in this module — resolve at call
        # time, not import time (same late-binding pattern as before)
        _TM = globals().get("ThemedModal")
        if _TM is None:
            return bool(messagebox.askyesno(title, message))
        result = {"ok": False}
        modal = _TM(parent, title=title,
                    accent=accent or COLORS["accent_yellow"],
                    size=None, scrim=True)
        body = modal.body
        tk.Label(body, text=message, font=(FONT_FAMILY, 9), bg=COLORS["bg"],
                 fg=COLORS["text"], wraplength=440,
                 justify="left").pack(fill="x", pady=(8, 10))

        def _yes():
            result["ok"] = True
            modal.close()

        def _no():
            result["ok"] = False
            modal.close()

        brow = tk.Frame(body, bg=COLORS["bg"])
        brow.pack(fill="x", pady=(2, 8))
        AnimatedButton(brow, text=no_text, command=_no,
                       bg=COLORS["surface"], fg=COLORS["text"],
                       font=(FONT_FAMILY, 9, "bold"), padx=18,
                       pady=7).pack(side="right", padx=(8, 0))
        AnimatedButton(brow, text=yes_text, command=_yes,
                       bg=accent or COLORS["accent_yellow"],
                       fg=COLORS["black"], font=(FONT_FAMILY, 9, "bold"),
                       padx=18, pady=7).pack(side="right")
        modal._dlg.bind("<Return>", lambda _e: _yes())
        # Escape default: No (already bound to close by the base — the
        # False result stands because nothing overrode result["ok"])
        modal.wait()
        return bool(result["ok"])
    except Exception:
        try:
            return bool(messagebox.askyesno(title, message))
        except Exception:
            return False


def _themed_showinfo(parent, title: str, message: str, accent: str = None,
                     ok_text: str = "OK") -> None:
    """Dark-themed info dialog (in-app modal, ThemedModal chrome)."""
    try:
        _TM = globals().get("ThemedModal")
        if _TM is None:
            messagebox.showinfo(title, message)
            return
        modal = _TM(parent, title=title,
                    accent=accent or COLORS["accent_green"],
                    size=None, scrim=True)
        body = modal.body
        tk.Label(body, text=message, font=(FONT_FAMILY, 9), bg=COLORS["bg"],
                 fg=COLORS["text"], wraplength=440,
                 justify="left").pack(fill="x", pady=(8, 10))
        brow = tk.Frame(body, bg=COLORS["bg"])
        brow.pack(fill="x", pady=(2, 8))
        AnimatedButton(brow, text=ok_text, command=modal.close,
                       bg=accent or COLORS["accent_green"],
                       fg=COLORS["black"], font=(FONT_FAMILY, 9, "bold"),
                       padx=22, pady=7).pack(side="right")
        modal._dlg.bind("<Return>", lambda _e: modal.close())
        modal.wait()
    except Exception:
        try:
            messagebox.showinfo(title, message)
        except Exception:
            pass


class _ModalScrim:
    """Dimmed backdrop behind a modal dialog (the 'in-app popup' look).

    A black Toplevel with ~45% alpha, sized/positioned exactly over the
    main window, that follows it while the modal is open (move, resize,
    minimize, restore). Clicks pass nowhere: the scrim itself grabs, so
    clicking the dimmed background is inert by design (progress dialogs
    and pickers both keep their state this way).

    Re-entrancy proof: every geometry/mapping mutation is wrapped in a
    settling flag, and _follow skips when the parent box did not change
    — lift()/deiconify() fire <Configure> on the parent on some WMs
    (observed on Tk 9.0), which would otherwise ping-pong forever.

    Headless-safe: when the parent window is withdrawn (smoke harness)
    or the platform lacks per-window alpha, the scrim silently skips
    itself — a dialog must never crash because its backdrop can't dim.
    """

    _ALPHA = 0.45

    def __init__(self, root, defer=False):
        """defer=True: build withdrawn but do NOT show yet — the caller
        shows scrim + dialog back-to-back when the dialog is fully built
        (single appearance, no dim-gap flash on first open)."""
        self.root = root
        self.win = None
        self._bindings = []
        self._settling = False
        self._last_box = None
        try:
            if root is None or not root.winfo_exists():
                return
            if not root.winfo_ismapped():
                # withdrawn/headless parent — nothing to dim behind
                return
            win = self.win = tk.Toplevel(root)
            win.withdraw()
            try:
                win.overrideredirect(True)
            except Exception:
                pass
            try:
                win.attributes("-alpha", self._ALPHA)
            except Exception:
                # alpha unsupported: a solid black scrim would be worse
                # than none — degrade to transparent-less (skip scrim)
                try:
                    win.destroy()
                except Exception:
                    pass
                self.win = None
                return
            win.configure(bg="#000000")
            try:
                win.transient(root)
            except Exception:
                pass
            self._settling = True
            try:
                self._geometry()
            finally:
                self._settling = False
            # follow the parent while the modal is up (guarded hops)
            for seq, cb in (("<Configure>", self._follow),
                            ("<Map>", self._on_parent_map),
                            ("<Unmap>", self._hide)):
                try:
                    bid = root.bind(seq, cb, add="+")
                    self._bindings.append((seq, bid))
                except Exception:
                    pass
            if not defer:
                self._show()
        except Exception:
            self.win = None

    def _box(self):
        try:
            return (self.root.winfo_rootx(), self.root.winfo_rooty(),
                    self.root.winfo_width(), self.root.winfo_height())
        except Exception:
            return None

    def _follow(self, _event=None):
        # change-detected follow: identical boxes must not re-mutate the
        # scrim (that re-raises <Configure> on some WMs — the old loop)
        box = self._box()
        if box is None or box == self._last_box:
            return
        self._show()

    def _on_parent_map(self, _event=None):
        # parent restored from taskbar: re-show (parent unmapped hid us)
        self._show()

    def _geometry(self):
        """Mirror the parent window's on-screen box exactly."""
        box = self._box()
        if box is None:
            return
        self._last_box = box
        try:
            self.win.geometry(f"{box[2]}x{box[3]}+{box[0]}+{box[1]}")
        except Exception:
            pass

    def _show(self, *_):
        if self.win is None or self._settling:
            return
        self._settling = True
        try:
            if not self.root.winfo_ismapped():
                self.win.withdraw()
                return
            self._geometry()
            if not self.win.winfo_ismapped():
                self.win.deiconify()
            # stay above the parent, below the (later-created) dialog —
            # lift ONLY when actually lower to avoid a Configure storm
            try:
                self.win.tk.call("raise", self.win._w, self.root._w)
            except Exception:
                pass
        except Exception:
            pass
        finally:
            self._settling = False

    def _hide(self, *_):
        if self.win is None:
            return
        try:
            self.win.withdraw()
        except Exception:
            pass

    def close(self):
        for seq, bid in self._bindings:
            try:
                self.root.unbind(seq, bid)
            except Exception:
                pass
        self._bindings = []
        if self.win is not None:
            try:
                self.win.destroy()
            except Exception:
                pass
            self.win = None


class ThemedModal:
    """Shared chrome base for every in-app popup (user redesign 2026-09).

    One window language, replacing the seven copy-pasted chrome blocks:
      * borderless Toplevel (overrideredirect) — no white OS title strip
      * rounded card drawn on a Canvas with the transparent key color,
        so the corners are actually rounded on screen
      * accent-dot title row + X close button top-right (hover-brighten)
      * optional scrim behind it (parent dimmed, mirroring its geometry)
      * ONE shared fixed client size for all feature dialogs (symmetry:
        every popup in the app is the same footprint, every time)
      * Escape closes; drag the title row to move; modal via wait() —
        split from __init__ so the smoke harness can assert on the
        built dialog without entering the nested event loop

    Subclass contract (all five feature dialogs follow it today):
      * super().__init__(parent, title=..., accent=...) FIRST
      * build content into self.body (a Frame, bg=bg, inside the card)
      * self.wait() at the end of the show site, never in __init__

    The whole chrome is best-effort: on any failure the dialog falls
    back to a plain native Toplevel so content still shows (a popup must
    never fail to open because its decorations couldn't render).
    """

    # ONE shared size for all feature dialogs (user: symmetry request).
    # 800x600 — Storage's two columns and Health's 2x3 card grid need the
    # room (660x520 clipped their rows); DNS/Pilot/Scorecard breathe in
    # the same footprint. Fits inside the 1040x800 main window with
    # margin on all sides. Content taller than the card scrolls inside
    # ScrollableRoundedPanel — the popup footprint itself never changes.
    WIDTH = 800
    HEIGHT = 600
    RADIUS = 16
    _TITLE_H = 44

    # Open-instance registry (flicker fix, user bug report: opening the
    # Auto-Pilot popup and clicking its preset Combobox made the main
    # window flash the Install tab behind the open dialog). Root cause:
    # the app's is-a-modal-open detectors test grab_current(), but a Tk
    # Combobox popdown TAKES the global grab while the dropdown is posted
    # — releasing the dialog's grab — so the startup pre-warm chain and
    # the Install catalog builder read "no modal" and mapped/raised pages
    # behind the open popup. Counting live ThemedModal instances is immune
    # to that grab dance: a dialog is open for as long as its Toplevel
    # exists, dropdown or not. _MODAL_OPEN maps id(self) -> self, kept in
    # sync by __init__ and close() (and by the borderless-fallback shape,
    # which still opens and still closes through close()).
    _MODAL_OPEN: "dict[int, object]" = {}

    def __init__(self, parent, *, title, accent=None, size=None,
                 scrim=True, no_x=False):
        try:
            root = parent.winfo_toplevel()
        except Exception:
            root = parent
        self._modal_root = root
        self._modal_title = title
        self._modal_accent = accent or COLORS["accent_blue"]
        self._modal_scrim_on = bool(scrim)
        self._modal_closed = False
        self._modal_on_close = None
        self._scrim = None
        w, h = size or (self.WIDTH, self.HEIGHT)
        try:
            ThemedModal._MODAL_OPEN[id(self)] = self
        except Exception:
            pass

        dlg = self._dlg = tk.Toplevel(root)
        try:
            dlg.title(title)          # still set: taskbar/alt-tab text
        except Exception:
            pass
        dlg.configure(bg=COLORS["bg"])
        dlg.resizable(False, False)
        # --- borderless + rounded card --------------------------------- #
        # (flicker fix, user bug report 2026-09: the window used to
        # deiconify HERE — an empty unpositioned frame flashed on screen
        # — then content streamed in, then it teleported to center, then
        # the scrim popped in last. Four visible flashes per popup.
        # Now: the window stays WITHDRAWN through the entire build; the
        # scrim dims first; the dialog maps once, already positioned.)
        self._card = None
        self._x_btn = None
        self._made_borderless = False
        try:
            # transparent key color = the dialog's own bg trick: pixels
            # matching it are clicked-through-transparent on Windows
            self._transkey = _next_transparent_color()
            dlg.configure(bg=self._transkey)
            dlg.withdraw()
            dlg.overrideredirect(True)
            dlg.attributes("-transparentcolor", self._transkey)
            dlg.overrideredirect(False)   # re-attach to WM…
            dlg.overrideredirect(True)    # …borderless again (focus fix)
            # NOTE: no deiconify here — mapping happens once, at the end
            self._made_borderless = True
        except Exception:
            self._made_borderless = False

        # scrim behind the card (in-app look) — created BEFORE the card
        # so stacking ends up: parent < scrim < this dialog.
        # (flicker fix: the scrim used to map AFTER the dialog flashed —
        # dim-first ordering is part of the single-appearance sequence.
        # Flicker fix 2, Health first-click report: the scrim mapped the
        # moment it was constructed — while the subclass was still
        # building its content — leaving a dim gap with no dialog on
        # slow first opens. defer=True keeps it withdrawn; the tail
        # shows scrim + dialog back-to-back when everything is ready.)
        if self._modal_scrim_on and self._made_borderless:
            try:
                self._scrim = _ModalScrim(root, defer=True)
            except Exception:
                self._scrim = None

        # rounded card surface (canvas, so corners can be cut by the
        # transparent key — a plain Frame can't shape its own corners)
        card = self._card = tk.Canvas(
            dlg, width=w, height=h, bg=self._transkey,
            highlightthickness=0, bd=0, closeenough=1.0)
        card.pack(fill="both", expand=True)
        try:
            card.create_polygon(
                _round_rect_points(0, 0, w - 1, h - 1, self.RADIUS),
                smooth=True, fill=COLORS["bg"], outline=COLORS["hairline"])
        except Exception:
            pass
        self._modal_w, self._modal_h = w, h

        # title row — drawn DIRECTLY on the card canvas (dot oval +
        # title text + X text). A Frame here would be a rectangle whose
        # square top corners cover the card's rounded top corners (the
        # 'top corners not rounded like the bottom' bug — canvas items
        # have no background rect, so the rounding shows through).
        self._x_btn = None
        try:
            card.create_oval(16, 9, 26, 19, fill=self._modal_accent,
                             outline="")
        except Exception:
            pass
        try:
            card.create_text(36, 8, text=title, font=(F, 11, "bold"),
                             fill=COLORS["text"], anchor="nw")
        except Exception:
            pass
        if not no_x:
            try:
                card.create_text(w - 14, 8, text="✕", font=(F, 11, "bold"),
                                 fill=COLORS["subtext"], anchor="ne",
                                 tags=("dlg_x",))

                def _x_on(_e):
                    try:
                        card.itemconfigure("dlg_x", fill=COLORS["accent_red"])
                    except Exception:
                        pass

                def _x_off(_e):
                    try:
                        card.itemconfigure("dlg_x", fill=COLORS["subtext"])
                    except Exception:
                        pass

                card.tag_bind("dlg_x", "<Enter>", _x_on, add="+")
                card.tag_bind("dlg_x", "<Leave>", _x_off, add="+")
                card.tag_bind("dlg_x", "<Button-1>",
                              lambda _e: self.close(), add="+")
            except Exception:
                pass
        # hairline under the title row
        try:
            card.create_line(self.RADIUS + 4, self._TITLE_H, w - self.RADIUS - 5,
                             self._TITLE_H, fill=COLORS["hairline"])
        except Exception:
            pass
        # drag-to-move by the title strip (custom chrome needs a
        # hand-rolled move; canvas-level with X + region guards)
        self._drag_bind(card)

        # content area — subclasses build here
        body = self.body = tk.Frame(card, bg=COLORS["bg"])
        card.create_window(self.RADIUS + 2, self._TITLE_H + 2, window=body,
                           anchor="nw", width=w - 2 * (self.RADIUS + 2),
                           height=h - self._TITLE_H - 2 - self.RADIUS)

        # uniform close paths: X, Escape, WM_DELETE (native X can appear
        # if the borderless fallback engaged on exotic setups)
        dlg.bind("<Escape>", lambda _e: self.close())
        try:
            dlg.protocol("WM_DELETE_WINDOW", self.close)
        except Exception:
            pass

        # fixed popup footprint + center over the parent (still
        # withdrawn — update_idletasks computes geometry invisibly, so
        # there is no teleport flash)
        try:
            dlg.update_idletasks()
            dlg.geometry(f"{w}x{h}")
            px, py = root.winfo_rootx(), root.winfo_rooty()
            pw, ph = root.winfo_width(), root.winfo_height()
            dlg.geometry(f"{w}x{h}"
                         f"+{px + max(0, (pw - w) // 2)}"
                         f"+{py + max(0, (ph - h) // 2)}")
        except Exception:
            pass

        # single appearance: scrim dims first, then the dialog maps
        # once — already positioned, fully built — then keyboard focus
        # (borderless windows are skipped by the WM's normal focus
        # handoff; without this, Escape goes nowhere — user bug report)
        try:
            if self._scrim is not None:
                self._scrim._show()
        except Exception:
            pass
        try:
            dlg.deiconify()
        except Exception:
            pass
        try:
            dlg.lift()
        except Exception:
            pass
        try:
            dlg.focus_force()
        except Exception:
            pass

    # ---- chrome helpers ------------------------------------------------ #

    def _drag_bind(self, card):
        """Hand-rolled drag-move on the canvas title strip: press anywhere
        with y < TITLE_H except on the X item, track the pointer delta,
        reposition the window. Fails soft."""
        state = {"x": 0, "y": 0, "on": False}

        def press(e):
            # clicks on the X glyph belong to the X binding, never drag
            try:
                cur = card.find_withtag("current")
                if cur and "dlg_x" in (card.gettags(cur[0]) or ()):
                    state["on"] = False
                    return
            except Exception:
                pass
            if getattr(e, "y", 9999) >= self._TITLE_H:
                state["on"] = False
                return
            state["x"], state["y"], state["on"] = e.x_root, e.y_root, True

        def motion(e):
            if not state["on"]:
                return
            try:
                dx = e.x_root - state["x"]
                dy = e.y_root - state["y"]
                state["x"], state["y"] = e.x_root, e.y_root
                self._dlg.geometry(f"+{self._dlg.winfo_x() + dx}"
                                   f"+{self._dlg.winfo_y() + dy}")
            except Exception:
                pass

        def release(_e):
            state["on"] = False

        try:
            card.bind("<Button-1>", press, add="+")
            card.bind("<B1-Motion>", motion, add="+")
            card.bind("<ButtonRelease-1>", release, add="+")
        except Exception:
            pass

    def on_close(self, cb):
        """Extra cleanup to run when the dialog closes (cancel tokens,
        unregisters — subclasses wire their own bookkeeping)."""
        self._modal_on_close = cb

    def close(self):
        """Single close path: run hook, drop the scrim, destroy. Safe to
        call twice (double-click X, Escape after X)."""
        if self._modal_closed:
            return
        self._modal_closed = True
        # flicker fix: deregister FIRST — the prewarm/catalog chains poll
        # the open-modal registry between idle steps, so the "no modal"
        # state must be visible the moment the close starts, not after the
        # destroy below finishes tearing widgets down.
        try:
            ThemedModal._MODAL_OPEN.pop(id(self), None)
        except Exception:
            pass
        if self._modal_on_close is not None:
            try:
                self._modal_on_close()
            except Exception:
                pass
            self._modal_on_close = None
        if self._scrim is not None:
            try:
                self._scrim.close()
            except Exception:
                pass
            self._scrim = None
        try:
            self._dlg.grab_release()
        except Exception:
            pass
        try:
            self._dlg.destroy()
        except Exception:
            pass
        # C-2 audit fix: return the transparent key color to the dispenser.
        # Without this, 5 sequential dialogs permanently exhaust the palette
        # and every later dialog shares the '#060708' fallback — two stacked
        # fallback dialogs can then collide on corner transparency. getattr:
        # the borderless-fallback ctor may never have allocated a key.
        try:
            _release_transparent_color(getattr(self, "_transkey", None))
        except Exception:
            pass

    def wait(self):
        """Modal wait — split from __init__ so the smoke harness can
        assert on the built dialog without blocking (house pattern)."""
        try:
            self._dlg.grab_set()
        except Exception:
            pass
        try:
            self._dlg.wait_window()
        except Exception:
            pass

    def _close(self):
        """Legacy close alias (internal buttons + smoke harness route)."""
        self.close()

    @classmethod
    def any_open(cls) -> bool:
        """True while at least one ThemedModal dialog exists. The
        grab-current detectors this replaces all had the same blind spot:
        a Tk Combobox popdown TAKES the global grab while its dropdown is
        posted (releasing the dialog's own grab), so grab_current()
        returned None with a modal plainly on screen — and the background
        chains (pre-warm, Install catalog slices) churned pages behind
        the open popup. Counting live instances can't be fooled by the
        grab dance. Dead-instance sweep: close() deregisters itself, but
        a dialog destroyed through any exotic path would otherwise linger
        as a stale dict entry forever."""
        try:
            for self in list(cls._MODAL_OPEN.values()):
                dlg = getattr(self, "_dlg", None)
                if dlg is None or not dlg.winfo_exists():
                    cls._MODAL_OPEN.pop(id(self), None)
            return bool(cls._MODAL_OPEN)
        except Exception:
            # registry unusable — fall back to the old grab check rather
            # than claiming "no modal" wrongly
            try:
                import tkinter as _tk
                return bool(_tk._default_root and
                            _tk._default_root.grab_current() is not None)
            except Exception:
                return False


# module-level transparent-color dispenser: two dialogs can't both use the
# same key color if one is ever stacked over the other's corner pixels
_TRANS_USED = set()
_TRANS_PALETTE = ("#010203", "#020304", "#030405", "#040506", "#050607")


def _next_transparent_color():
    try:
        for c in _TRANS_PALETTE:
            if c not in _TRANS_USED:
                _TRANS_USED.add(c)
                return c
        return "#060708"
    except Exception:
        return "#010203"


def _release_transparent_color(c):
    try:
        _TRANS_USED.discard(c)
    except Exception:
        pass


class ScorecardDialog(ThemedModal):
    """Themed end-of-run results card (user-approved feature 2026-09).

    Replaces the old generic 'Done' messagebox after Clean / Repair /
    Tweak / Undo runs. One glance answers "what just happened to my
    PC?":
      * hero number — GB freed, big and green (runs that freed space)
      * before/after free-space bar for C: (three rounded segments:
        still used / freed this run / was already free — Animated-
        ProgressBar-style geometry)
      * status chips — done / skipped / failed (+ stopped early)
      * one plain-language line per task, status-colored
      * amber 'Restart needed' banner when a task flags REBOOT REQUIRED
      * 'Clean Again' re-runs the exact same task list (completed
        Clean-tab runs only — user-approved spec)

    Chrome matches the themed dialogs exactly: dark Toplevel + dark
    titlebar, transient + modal (wait()), Escape/Return to close,
    centered over the parent, AnimatedButton pills, accent dot title
    row. Per-task rows beyond _ROWS_INLINE flow into a rounded scroll
    panel whose floating scrollbar auto-hides when everything fits
    (only deep presets ever overflow).

    The modal wait() is split from __init__ on purpose so the smoke
    harness can assert on the built dialog (refs _dlg/_hero_lbl/
    _bar_canvas/_rows_panel/_reboot_banner/_counts) without entering
    the nested event loop.
    """

    WIDTH = 800                 # fixed shared popup footprint (ThemedModal)
    _ROWS_INLINE = 12    # beyond this the row list scrolls
    _ROW_H = 26          # per-row height budget for the panel cap

    def __init__(self, parent, *, tab_name, mode, cancelled, results,
                 total_bytes, before=None, after=None, needs_reboot=False,
                 on_run_again=None, ok_text="Close"):
        self._run_again_cb = on_run_again
        tab_accent = TAB_ACCENTS.get(tab_name, COLORS["accent_green"])
        if cancelled:
            self._title = ("Undo Stopped" if mode == "revert"
                           else f"{tab_name} Run Stopped")
            self._title_accent = COLORS["accent_yellow"]
        else:
            self._title = ("Undo Complete" if mode == "revert"
                           else f"{tab_name} Complete")
            self._title_accent = (COLORS["accent_green"] if mode == "revert"
                                  else tab_accent)

        # counts straight from the per-task results (never from stale
        # counters — the dialog owns its own truth)
        ok_n = sum(1 for _l, s, _b in results if s == "ok")
        skip_n = sum(1 for _l, s, _b in results if s == "skip")
        fail_n = sum(1 for _l, s, _b in results if s == "fail")
        stop_n = sum(1 for _l, s, _b in results if s == "stop")
        self._counts = (ok_n, skip_n, fail_n, stop_n)

        super().__init__(parent, title=self._title,
                         accent=self._title_accent)
        dlg = self._dlg
        body = self.body

        # hero number — freed space, the emotional payoff (skipped when
        # the run freed nothing: no fake hero, the chips tell the story)
        self._hero_lbl = None
        if total_bytes and total_bytes > 0:
            hero = tk.Frame(body, bg=COLORS["bg"])
            hero.pack(fill="x", pady=(2, 0))
            self._hero_lbl = tk.Label(hero, text=format_bytes(total_bytes),
                                      font=(F, 26, "bold"), bg=COLORS["bg"],
                                      fg=COLORS["accent_green"])
            self._hero_lbl.pack()
            tk.Label(hero, text="disk space freed", font=(F, 10),
                     bg=COLORS["bg"], fg=COLORS["subtext"]).pack()

        # before/after free-space bar — only when the run freed space AND
        # both C: snapshots were captured (honest: no invented numbers)
        self._bar_canvas = None
        if (total_bytes and total_bytes > 0 and before and after
                and before[1] and after[1]):
            b_avail, b_total = (before[0] or 0), before[1]
            a_avail, a_total = (after[0] or 0), after[1]
            if a_avail >= b_avail and a_total > 0:
                bar_wrap = tk.Frame(body, bg=COLORS["bg"])
                bar_wrap.pack(fill="x", padx=18, pady=(8, 0))
                tk.Label(bar_wrap,
                         text=f"C:  {format_bytes(b_avail)}  →  {format_bytes(a_avail)} free",
                         font=(F, 9, "bold"), bg=COLORS["bg"],
                         fg=COLORS["text"]).pack(anchor="w")
                self._bar_canvas = tk.Canvas(bar_wrap, height=16,
                                             highlightthickness=0, bd=0,
                                             bg=COLORS["bg"])
                self._bar_canvas.pack(fill="x", pady=(4, 0))
                self._bar_canvas.bind(
                    "<Configure>",
                    lambda e, b=b_avail, a=a_avail, t=a_total:
                        self._draw_bar(self._bar_canvas, b, a, t))
                # draw once at the known packed width — a dialog whose
                # parent is withdrawn (headless harness) never receives
                # <Configure>, and _draw_bar falls back to a 16px height
                # when the canvas is still unmapped, so this initial pass
                # is safe in both environments. A later real <Configure>
                # just redraws at the true width.
                self._draw_bar(self._bar_canvas, b_avail, a_avail, a_total,
                               width=self.WIDTH - 36)

        # status chips
        chips = tk.Frame(body, bg=COLORS["bg"])
        chips.pack(fill="x", padx=18, pady=(10, 0))
        tk.Label(chips, text=f"✓  {ok_n} done", font=(F, 10, "bold"),
                 bg=COLORS["bg"], fg=COLORS["accent_green"]).pack(side="left")
        if skip_n:
            tk.Label(chips, text=f"⊘  {skip_n} skipped", font=(F, 10, "bold"),
                     bg=COLORS["bg"], fg=COLORS["subtext"]).pack(side="left", padx=(12, 0))
        if fail_n:
            tk.Label(chips, text=f"✕  {fail_n} failed", font=(F, 10, "bold"),
                     bg=COLORS["bg"], fg=COLORS["accent_red"]).pack(side="left", padx=(12, 0))
        if stop_n:
            tk.Label(chips, text="◦  stopped early", font=(F, 10, "bold"),
                     bg=COLORS["bg"], fg=COLORS["accent_yellow"]).pack(side="left", padx=(12, 0))

        # hairline separator
        tk.Frame(body, bg=COLORS["hairline"], height=1).pack(fill="x", padx=18, pady=(10, 4))

        # per-task rows — rounded panel capped at _ROWS_INLINE rows tall;
        # the floating scrollbar only appears when content overflows
        n_rows = len(results)
        panel_h = max(52, min(n_rows, self._ROWS_INLINE) * self._ROW_H + 16)
        self._rows_panel = panel = ScrollableRoundedPanel(body)
        panel.config(height=panel_h)   # after ctor (its Canvas init pins height=10)
        panel.pack(fill="x", padx=18, pady=(2, 0))
        if not results:
            tk.Label(panel.inner, text="No tasks ran.", font=(F, 9),
                     bg=COLORS["bg_alt"], fg=COLORS["subtext"]).pack(pady=12)
        for label, status, nbytes in results:
            self._build_row(panel.inner, label, status, nbytes)
        # settle scroll metrics NOW (ctor-time) so scroll_enabled is
        # correct even before the canvas ever maps — the harness asserts
        # on it without pumping Configure events
        try:
            panel.refresh_scroll()
        except Exception:
            pass

        # reboot banner — amber, same tinted-banner pattern as Undo's
        self._reboot_banner = None
        if needs_reboot:
            banner = self._reboot_banner = tk.Frame(body, bg="#3A2C12")
            tk.Label(banner,
                     text="⚠  Restart needed — some changes only take effect after a reboot.",
                     font=(F, 9, "bold"), bg="#3A2C12",
                     fg=COLORS["accent_yellow"]).pack(anchor="w", padx=10, pady=8)
            banner.pack(fill="x", padx=18, pady=(10, 0))

        # buttons — Close primary (accent fill), Clean Again secondary
        brow = tk.Frame(body, bg=COLORS["bg"])
        brow.pack(fill="x", padx=18, pady=(12, 10))
        if self._run_again_cb is not None:
            AnimatedButton(brow, text="Clean Again", command=self._run_again,
                           bg=COLORS["surface"], fg=COLORS["text"],
                           font=(F, 9, "bold"), padx=18,
                           pady=7).pack(side="right", padx=(8, 0))
        AnimatedButton(brow, text=ok_text, command=self.close,
                       bg=self._title_accent, fg=COLORS["black"],
                       font=(F, 9, "bold"), padx=22,
                       pady=7).pack(side="right")

        # Return mirrors Close (single primary action on this card)
        dlg.bind("<Return>", lambda _e: self.close())

    # ---- rows ---------------------------------------------------------- #

    @staticmethod
    def _glyphs(status):
        return {
            "ok": ("✓", COLORS["accent_green"]),
            "skip": ("⊘", COLORS["subtext"]),
            "fail": ("✕", COLORS["accent_red"]),
            "stop": ("◦", COLORS["accent_yellow"]),
        }.get(status, ("•", COLORS["subtext"]))

    def _build_row(self, parent, label, status, nbytes):
        """One compact one-liner: status glyph | task label (+ plain-
        language suffix for skip/fail/stop) | bytes freed (right). Same
        bg_alt row look as the preset summary cells."""
        glyph, gcolor = self._glyphs(status)
        row = tk.Frame(parent, bg=COLORS["bg_alt"])
        tk.Label(row, text=glyph, font=(F, 9, "bold"), bg=COLORS["bg_alt"],
                 fg=gcolor, width=2, anchor="w").pack(side="left")
        text, fg = label, COLORS["text"]
        if status == "skip":
            text += "  — nothing to do"
            fg = COLORS["subtext"]
        elif status == "fail":
            text += "  — failed (see log)"
        elif status == "stop":
            text += "  — stopped"
            fg = COLORS["subtext"]
        tk.Label(row, text=text, font=(F, 9), bg=COLORS["bg_alt"], fg=fg,
                 anchor="w", justify="left",
                 wraplength=400).pack(side="left", padx=(2, 0))
        if nbytes and nbytes > 0:
            tk.Label(row, text=format_bytes(nbytes), font=(F, 9, "bold"),
                     bg=COLORS["bg_alt"],
                     fg=COLORS["accent_green"]).pack(side="right")
        row.pack(fill="x", pady=1)

    # ---- before/after bar ---------------------------------------------- #

    def _draw_bar(self, canvas, before_avail, after_avail, total, width=None):
        """Three rounded segments proportional to the drive's real
        capacity: still used (muted) / freed this run (bright green) /
        was already free (muted green). Geometry mirrors the segmented
        AnimatedProgressBar so the bar reads as the same design family."""
        try:
            canvas.delete("all")
            w = width if width is not None else canvas.winfo_width()
            if w < 40 or total <= 0:
                return
            gap = 3
            avail = w - gap * 2
            used_after = max(0, total - after_avail)
            freed = max(0, after_avail - before_avail)
            free_before = max(0, min(before_avail, total))
            parts = [
                (used_after / total, COLORS["surface"]),
                (freed / total, COLORS["accent_green"]),
                (free_before / total,
                 _hex_lerp(COLORS["surface"], COLORS["accent_green"], 0.35)),
            ]
            x = 0
            h = canvas.winfo_height()
            if h < 4:                 # not mapped yet (winfo gives 1)
                h = 16
            for i, (frac, color) in enumerate(parts):
                seg_w = int(frac * avail)
                if seg_w <= 0:
                    continue          # a zero-width segment adds no gap
                if i > 0:
                    x += gap
                canvas.create_polygon(
                    _round_rect_points(x, 2, x + seg_w, h - 2, 3),
                    smooth=True, fill=color, outline="")
                x += seg_w
        except Exception:
            pass

    # ---- actions -------------------------------------------------------- #

    def _run_again(self):
        cb = self._run_again_cb
        self.close()
        if cb is not None:
            try:
                cb()
            except Exception:
                pass

    def wait(self):
        """Modal wait (grab + wait_window) — split from __init__ so the
        smoke harness can assert on the built dialog without blocking."""
        try:
            self._dlg.grab_set()
        except Exception:
            pass
        try:
            self._dlg.wait_window()
        except Exception:
            pass


class StorageInsightDialog(ThemedModal):
    """Storage Insight (user-approved feature 3, 2026-09): 'what's eating
    my drives?' — machine-wide reclaimable-junk estimates beside a
    biggest-games list, in one screen with no scrolling.

    Left column — Reclaimable junk: one ToggleSwitch row per scan
    category (5 fixed rows, so the layout never grows). Sizes stream in
    live as the worker measures; 'Clean Selected' runs the checked
    categories' real tasks through the standard run engine (which ends
    on the Scorecard like any other run).

    Right column — Biggest games: read-only rows (drive chip + name +
    size + Open-folder button), top 5 by size plus a '+N more' summary
    line. Names display-truncated to one row (full name + path ride the
    row tooltip) so every row is a constant 39px and the columns area
    never outgrows the dialog — no scrollbar, ever. Deliberately NO
    delete button here: uninstalling a whole game is a human decision
    that belongs in its launcher.

    Honesty rules: the junk scan only covers the audited file-based
    allowlist (see app/storage_scan.py) and the dialog footnotes what it
    does NOT estimate; a cancelled scan labels its numbers partial.

    Chrome: ThemedModal (borderless rounded card, scrim, X, fixed size).
    The scan worker never touches widgets — every paint hops via after().
    """

    WIDTH = 800                 # fixed shared popup footprint (ThemedModal)
    # Five games, not eight (user call 2026-09: no scrollbar — the columns
    # area fits 5 fixed-height rows; the '+N more' line honestly counts
    # the rest, same as before).
    _MAX_GAMES = 5

    def __init__(self, parent, app):
        self.app = app
        self._scan_token = [True]   # replaced per scan; True = stopped
        self._scan_done = False
        self._cat_vars = {}         # title -> BooleanVar
        self._cat_sizes = {}        # title -> measured bytes (None = pending)
        self._cat_size_lbls = {}    # title -> size Label
        self._cat_keys = {}         # title -> member task keys
        self._games = []            # measured game dicts

        super().__init__(parent, title="Storage Insight",
                         accent=TAB_ACCENTS["Clean"])
        dlg = self._dlg
        body = self.body

        # intro line (title row carries the accent dot + name)
        tk.Label(body, text="See what's eating your drives.",
                 font=(F, 9), bg=COLORS["bg"],
                 fg=COLORS["subtext"], wraplength=680,
                 justify="left").pack(anchor="w")

        # hero: live reclaimable total
        hero = tk.Frame(body, bg=COLORS["bg"])
        hero.pack(fill="x", pady=(8, 0))
        self._hero_lbl = tk.Label(hero, text="Scanning…", font=(F, 20, "bold"),
                                  bg=COLORS["bg"], fg=COLORS["subtext"])
        self._hero_lbl.pack()
        tk.Label(hero, text="estimated reclaimable junk", font=(F, 9),
                 bg=COLORS["bg"], fg=COLORS["subtext"]).pack()

        # footer FIRST (packed side=bottom): the action buttons are
        # guaranteed visible no matter how tall the columns grow (user
        # bug report: long game names wrapped rows and squeezed Rescan /
        # Clean Selected out of the fixed 600px dialog). The columns
        # live in a scroll panel taking exactly the remaining space.
        foot = tk.Frame(body, bg=COLORS["bg"])
        foot.pack(side="bottom", fill="x", pady=(10, 6))
        self._status_lbl = tk.Label(foot, text="Starting scan…", font=(F, 9),
                                    bg=COLORS["bg"], fg=COLORS["subtext"],
                                    anchor="w")
        self._status_lbl.pack(side="left")
        self._clean_btn = AnimatedButton(
            foot, text="Clean Selected", command=self._clean_selected,
            bg=TAB_ACCENTS["Clean"], fg=COLORS["black"],
            font=(F, 9, "bold"), padx=18, pady=7)
        self._clean_btn.pack(side="right")
        self._clean_btn.set_enabled(False)
        AnimatedButton(foot, text="Rescan", command=self._start_scan,
                       bg=COLORS["surface"], fg=COLORS["text"],
                       font=(F, 9, "bold"), padx=18,
                       pady=7).pack(side="right", padx=(0, 8))

        # honest footnote: what the estimates do NOT cover (above footer)
        try:
            from app.storage_scan import SCAN_EXCLUDED_NOTE as _note
        except Exception:
            _note = ""
        if _note:
            tk.Label(body, text="Note: " + _note, font=(F, 8),
                     bg=COLORS["bg"], fg=COLORS["subtext"],
                     wraplength=self.WIDTH - 36, justify="left").pack(
                         side="bottom", anchor="w", pady=(8, 0))

        # two symmetric columns in a scroll panel (uniform grid, like the
        # preset cards) — takes the remaining middle space; overflow
        # scrolls inside instead of pushing the footer out
        self._cols_panel = ScrollableRoundedPanel(body)
        self._cols_panel.pack(fill="both", expand=True, pady=(10, 0))
        cols = tk.Frame(self._cols_panel.inner, bg=COLORS["bg"])
        cols.pack(fill="x")
        cols.grid_columnconfigure(0, weight=1, uniform="insight")
        cols.grid_columnconfigure(1, weight=1, uniform="insight")

        left = tk.Frame(cols, bg=COLORS["bg"])
        left.grid(row=0, column=0, sticky="new", padx=(0, 6))
        tk.Label(left, text="Reclaimable junk", font=(F, 10, "bold"),
                 bg=COLORS["bg"], fg=COLORS["text"]).pack(anchor="w", pady=(0, 4))
        self._junk_body = tk.Frame(left, bg=COLORS["bg"])
        self._junk_body.pack(fill="x")

        right = tk.Frame(cols, bg=COLORS["bg"])
        right.grid(row=0, column=1, sticky="new", padx=(6, 0))
        tk.Label(right, text="Biggest games", font=(F, 10, "bold"),
                 bg=COLORS["bg"], fg=COLORS["text"]).pack(anchor="w", pady=(0, 4))
        self._games_body = tk.Frame(right, bg=COLORS["bg"])
        self._games_body.pack(fill="x")

        self._build_junk_rows()
        self._build_games_placeholder()
        self._refresh_cols_scroll()

        # closing cancels the scan (the base close hook runs it)
        self.on_close(self._cancel_scan)
        self._start_scan()

    def _cancel_scan(self):
        try:
            self._scan_token[0] = True
        except Exception:
            pass

    # ---- rows ---------------------------------------------------------- #

    def _build_junk_rows(self):
        """One ToggleSwitch row per scan category (fixed count of 5 —
        the layout never grows, so no scrollbar is ever needed)."""
        try:
            from app.storage_scan import SCAN_CATEGORIES as _cats
        except Exception:
            _cats = ()
        for title, keys, tip in _cats:
            self._cat_keys[title] = list(keys)
            var = tk.BooleanVar(value=True)
            var.trace_add("write", lambda *_: self._refresh_estimate())
            self._cat_vars[title] = var
            self._cat_sizes[title] = None
            outer = tk.Frame(self._junk_body, bg=COLORS["bg_alt"])
            inner = tk.Frame(outer, bg=COLORS["bg_alt"])
            inner.pack(fill="both", expand=True)
            ToggleSwitch(inner, var).pack(side="left", anchor="center",
                                          padx=(8, 2), pady=6)
            lbl = tk.Label(inner, text=title, font=(F, 9, "bold"),
                           bg=COLORS["bg_alt"], fg=COLORS["text"],
                           anchor="w", justify="left")
            lbl.pack(side="left", anchor="center")
            try:
                from app.tab_presets import TABS as _TABS
                _names = [t.label for t in _TABS.get("Clean", [])
                          if t.key in keys]
                Tooltip(lbl, tip + ("\n\nIncludes: " + ", ".join(_names)
                                    if _names else ""))
            except Exception:
                pass
            size = tk.Label(inner, text="…", font=(F, 9, "bold"),
                            bg=COLORS["bg_alt"], fg=COLORS["subtext"])
            size.pack(side="right", anchor="center", padx=(6, 8))
            self._cat_size_lbls[title] = size
            outer.pack(fill="x", pady=2)

    def _build_games_placeholder(self):
        self._games_ph = tk.Label(self._games_body, text="Scanning libraries…",
                                  font=(F, 9), bg=COLORS["bg"],
                                  fg=COLORS["subtext"])
        self._games_ph.pack(anchor="w", pady=6)

    # ---- scan worker ---------------------------------------------------- #

    def _ui(self, fn, *args):
        """Marshal a paint hop to the Tk thread (worker-safe).

        F10: never touch Tk from the worker (even winfo_exists burns
        ~1s off-thread) — schedule unconditionally; a destroyed dialog
        raises inside after() and is swallowed below."""
        try:
            self._dlg.after(0, lambda: fn(*args))
        except Exception:
            pass

    def _start_scan(self):
        # stop any in-flight scan, then reset rows to pending
        try:
            self._scan_token[0] = True
        except Exception:
            pass
        token = self._scan_token = [False]
        self._scan_done = False
        try:
            self._clean_btn.set_enabled(False)
        except Exception:
            pass
        for title in self._cat_sizes:
            self._cat_sizes[title] = None
            try:
                self._cat_size_lbls[title].config(text="…",
                                                  fg=COLORS["subtext"])
            except Exception:
                pass
        try:
            self._hero_lbl.config(text="Scanning…", fg=COLORS["subtext"])
            self._set_scan_status("Starting scan…")
        except Exception:
            pass
        for w in list(self._games_body.winfo_children()):
            try:
                w.destroy()
            except Exception:
                pass
        self._build_games_placeholder()
        try:
            import threading as _th
            _th.Thread(target=self._scan_worker, args=(token,),
                       daemon=True).start()
        except Exception:
            pass

    def _scan_worker(self, token):
        def dead():
            # F10: token-only — close handlers already set token on the Tk
            # thread; winfo_exists here burned ~1s off-thread per call.
            try:
                return bool(token[0])
            except Exception:
                return True

        try:
            from app.storage_scan import (
                SCAN_CATEGORIES, acquire_scan, release_scan, store_estimate,
                find_game_libraries, make_measure_ctx, measure_clean_tasks)
        except Exception:
            self._ui(self._set_scan_status, "Scan unavailable.")
            self._ui(self._scan_finished, True)
            return

        # Phase-6: the junk phase runs under the shared scan slot — a
        # Health dialog or nudge estimate never walks the same folders
        # at the same time. Give-up is honest ("scan busy"); the dialog
        # simply keeps whatever landed. The slot covers phase 1 only;
        # the (fast, cancellable) game sizing runs outside it.
        if not acquire_scan(cancelled=dead):
            self._ui(self._set_scan_status, "Another scan is running — Rescan in a moment.")
            self._ui(self._scan_finished, True)
            return
        try:
            ctx = make_measure_ctx(
                set_status=lambda m: self._ui(self._set_scan_status, m),
                cancelled=dead)
            # phase 1: junk, one category at a time (live row updates)
            grand = 0
            for title, keys, _tip in SCAN_CATEGORIES:
                if dead():
                    return
                self._ui(self._set_scan_status, f"Scanning {title}…")
                try:
                    sizes = measure_clean_tasks(ctx, keys)
                except Exception:
                    sizes = {}
                if dead():
                    return
                grand += sum(sizes.values()) if sizes else 0
                self._ui(self._paint_category,
                         title, sum(sizes.values()) if sizes else 0)
            # a complete, uncancelled junk walk publishes the shared
            # estimate cache (Health + Nudge quote it instead of re-walking)
            if not dead():
                store_estimate(grand)
        finally:
            release_scan()
        # phase 2: game libraries (read-only sizing)
        if not dead():
            self._ui(self._set_scan_status, "Sizing installed games…")
        try:
            games = find_game_libraries(
                cancelled=dead,
                on_game=lambda n: self._ui(self._set_scan_status,
                                           f"Sizing {n}…"))
        except Exception:
            games = []
        if dead():
            return
        cancelled = bool(token[0])
        self._ui(self._paint_games, games)
        self._ui(self._scan_finished, cancelled)

    # ---- paints (Tk thread only) ---------------------------------------- #

    def _set_scan_status(self, text):
        try:
            if self._status_lbl.winfo_exists():
                self._status_lbl.config(text=text)
        except Exception:
            pass

    def _paint_category(self, title, total):
        self._cat_sizes[title] = total
        try:
            lbl = self._cat_size_lbls.get(title)
            if lbl is not None and lbl.winfo_exists():
                if total and total > 0:
                    lbl.config(text=format_bytes(total),
                               fg=COLORS["accent_green"])
                else:
                    lbl.config(text="✓ clean", fg=COLORS["accent_green"])
        except Exception:
            pass
        self._refresh_estimate()

    _game_name_font = None      # cached F,9-bold pixel measurer

    @classmethod
    def _short_game_name(cls, name):
        """Display-truncate a game name to one row (ellipsis + full name
        in the row tooltip). Pixel-exact via font.measure — character
        counts lie with proportional fonts, and an unwrapped long name
        is what grew rows to 2-3 lines and fed the scrollbar. 150px
        fits the name column at 760px dialog width on any DPI (the
        layout uses absolute pixels, so measure in the same units)."""
        try:
            name = name or "?"
            if cls._game_name_font is None:
                cls._game_name_font = tkfont.Font(family=F, size=9,
                                                  weight="bold")
            if cls._game_name_font.measure(name) <= 150:
                return name
            ell = "…"
            while len(name) > 1 and cls._game_name_font.measure(name + ell) > 150:
                name = name[:-1]
            return (name + ell) if name else "…"
        except Exception:
            try:
                return (name or "?")[:22]
            except Exception:
                return "?"

    def _paint_games(self, games):
        self._games = list(games or [])
        for w in list(self._games_body.winfo_children()):
            try:
                w.destroy()
            except Exception:
                pass
        if not self._games:
            tk.Label(self._games_body, text="No game libraries detected on this PC.",
                     font=(F, 9), bg=COLORS["bg"],
                     fg=COLORS["subtext"]).pack(anchor="w", pady=6)
            return
        shown = self._games[:self._MAX_GAMES]
        for g in shown:
            outer = tk.Frame(self._games_body, bg=COLORS["bg_alt"])
            inner = tk.Frame(outer, bg=COLORS["bg_alt"])
            inner.pack(fill="both", expand=True)
            tk.Label(inner, text=g.get("drive", ""), font=(F, 8, "bold"),
                     bg=COLORS["bg_alt"], fg=COLORS["subtext"],
                     width=3, anchor="w").pack(side="left", anchor="center",
                                              padx=(8, 2), pady=6)
            lbl = tk.Label(inner, text=self._short_game_name(g.get("name", "?")),
                           font=(F, 9, "bold"),
                           bg=COLORS["bg_alt"], fg=COLORS["text"],
                           anchor="w", justify="left", wraplength=175)
            lbl.pack(side="left", anchor="center")
            try:
                Tooltip(lbl, f"{g.get('name', '?')}\n{g.get('launcher', '')} — {g.get('path', '')}\n"
                             "Read-only: uninstall from its launcher.")
            except Exception:
                pass
            AnimatedButton(inner, text="Open",
                           command=lambda p=g.get("path", ""): self._open_game_folder(p),
                           bg=COLORS["surface"], fg=COLORS["text"],
                           font=(F, 8), padx=10, pady=3).pack(
                               side="right", anchor="center", padx=(0, 8))
            tk.Label(inner, text=format_bytes(g.get("bytes", 0)),
                     font=(F, 9), bg=COLORS["bg_alt"],
                     fg=COLORS["text"]).pack(side="right", anchor="center",
                                             padx=(0, 6))
            outer.pack(fill="x", pady=2)
        rest = self._games[self._MAX_GAMES:]
        if rest:
            tk.Label(self._games_body,
                     text=f"+{len(rest)} more games ({format_bytes(sum(g.get('bytes', 0) for g in rest))} total)",
                     font=(F, 8), bg=COLORS["bg"],
                     fg=COLORS["subtext"]).pack(anchor="w", pady=(4, 0))
        self._refresh_cols_scroll()

    def _refresh_cols_scroll(self):
        """Settle the columns scroll panel after rows change (the panel
        takes exactly the remaining middle space; overflow scrolls
        inside instead of squeezing the footer)."""
        try:
            panel = getattr(self, "_cols_panel", None)
            if panel is not None:
                panel.refresh_scroll()
        except Exception:
            pass

    def _refresh_estimate(self):
        """Hero + footer estimate from checked categories (Tk thread)."""
        try:
            total = sum(self._cat_sizes.get(t, 0) or 0
                        for t, v in self._cat_vars.items() if v.get())
            if any(s is None for s in self._cat_sizes.values()):
                self._hero_lbl.config(text="Scanning…", fg=COLORS["subtext"])
            elif total > 0:
                self._hero_lbl.config(text=format_bytes(total),
                                      fg=COLORS["accent_green"])
            else:
                self._hero_lbl.config(text="All clean", fg=COLORS["accent_green"])
            try:
                self._clean_btn.config_text(
                    f"Clean Selected ({format_bytes(total)})" if total > 0
                    else "Clean Selected")
            except Exception:
                pass
        except Exception:
            pass

    def _scan_finished(self, cancelled):
        self._scan_done = True
        try:
            if cancelled:
                self._set_scan_status("Scan stopped — sizes shown are partial.")
            else:
                self._set_scan_status("Scan complete — pick what to clean.")
            self._refresh_estimate()
            self._clean_btn.set_enabled(True)
        except Exception:
            pass

    # ---- actions ---------------------------------------------------------- #

    def _open_game_folder(self, path):
        import os as _os
        try:
            if path and os.path.isdir(path):
                _os.startfile(path)
        except Exception:
            pass

    def _clean_selected(self):
        """Close the dialog, then run the checked categories' real tasks
        through the standard engine (progress bar + Stop + Scorecard).
        Tasks resolve live from the Clean table — tolerant, like the
        scheduler: unknown keys are skipped, never crash."""
        if not self._scan_done:
            return
        keys = []
        for title, var in self._cat_vars.items():
            try:
                if var.get():
                    keys.extend(self._cat_keys.get(title, []))
            except Exception:
                pass
        if not keys:
            try:
                self._set_scan_status("Nothing checked — turn on a category first.")
            except Exception:
                pass
            return
        try:
            from app.tab_presets import TABS as _TABS
            by_key = {t.key: t for t in _TABS.get("Clean", [])}
            tasks = [by_key[k] for k in keys if k in by_key]
        except Exception:
            tasks = []
        if not tasks:
            return
        self.close()
        try:
            self.app.run_tasks("Clean", tasks, mode="run")
        except Exception:
            pass

    def wait(self):
        """Modal wait (grab + wait_window) — split from __init__ like the
        Scorecard so the harness can assert on the built dialog."""
        try:
            self._dlg.grab_set()
        except Exception:
            pass
        try:
            self._dlg.wait_window()
        except Exception:
            pass


class DnsTesterDialog(ThemedModal):
    """Find My Fastest DNS (user-approved feature 5, 2026-09): one click
    tests the well-known gaming-friendly resolvers plus the machine's
    current DNS when publicly testable (router IPs are held back with an
    honesty footnote — timing them can only fake-alarm) and shows which
    is fastest FOR THIS connection.

    The user picks ANY answering server — the fastest is crowned ★ and
    pre-selected, but the choice stays theirs (never forced). Apply runs
    the real gaming_dns task with the pick's (primary, secondary) pair
    through the standard run engine (snapshot + undo + scorecard all
    work). Restore runs the same task's revert, putting back the exact
    pre-apply DNS (DHCP included) on every adapter the snapshot covers.

    Rows stream in live as each resolver answers (parallel probe
    threads, ~1-3s typical); unreachable resolvers show 'no reply'
    instead of a fake number. Closing mid-test stops the probes.
    Chrome matches the themed dialogs (dark Toplevel + dark titlebar,
    transient + modal, Escape closes, centered over the parent).
    """

    def __init__(self, parent, app):
        self.app = app
        self._stop_token = [False]
        self._results = {}        # ip -> ms or None (landed)
        self._expected = 0        # total probes this round
        self._done_n = 0          # landed count (Tk thread only)
        self._finished = False
        self._scan_gen = 0        # round generation (stale-hop guard)
        self._selected_ip = None  # the user's pick (defaults to fastest)
        self._row_widgets = {}    # ip -> {"row","ms","badge","pick","name","current"}
        self._watchdog_after = None

        super().__init__(parent, title="Find My Fastest DNS",
                         accent=TAB_ACCENTS["Tweak"])
        dlg = self._dlg
        body = self.body

        # plain-language intro (title row carries the accent dot + name)
        tk.Label(body, text="Tests which DNS server responds fastest for YOUR "
                            "connection — then applies it.",
                 font=(F, 9), bg=COLORS["bg"],
                 fg=COLORS["subtext"], wraplength=680,
                 justify="left").pack(anchor="w")

        # progress (indeterminate — probe count is known but per-probe
        # latency isn't; the honest signal is rows landing below)
        self._bar = AnimatedProgressBar(body, accent=TAB_ACCENTS["Tweak"])
        self._bar.pack(fill="x", pady=(10, 0))
        self._bar.set_indeterminate(True)
        self._status_lbl = tk.Label(body, text="Starting…", font=(F, 9),
                                    bg=COLORS["bg"], fg=COLORS["subtext"],
                                    anchor="w")
        self._status_lbl.pack(fill="x", pady=(2, 0))

        tk.Frame(body, bg=COLORS["hairline"], height=1).pack(
            fill="x", pady=(10, 4))
        self._rows_body = tk.Frame(body, bg=COLORS["bg"])
        self._rows_body.pack(fill="x")
        tk.Frame(body, bg=COLORS["hairline"], height=1).pack(
            fill="x", pady=(4, 0))

        # buttons (Apply stays a bare verb — the status line + the
        # highlighted row say WHAT it will apply; Restore reverts both
        # primary + secondary to the pre-apply snapshot via the real
        # gaming_dns revert, same engine as Tweak-tab Undo)
        brow = tk.Frame(body, bg=COLORS["bg"])
        brow.pack(fill="x", pady=(12, 6))
        self._apply_btn = AnimatedButton(
            brow, text="Apply", command=self._apply_selected,
            bg=TAB_ACCENTS["Tweak"], fg=COLORS["black"],
            font=(F, 9, "bold"), padx=18, pady=7)
        self._apply_btn.pack(side="right")
        self._apply_btn.set_enabled(False)
        AnimatedButton(brow, text="Re-test", command=self._start_test,
                       bg=COLORS["surface"], fg=COLORS["text"],
                       font=(F, 9, "bold"), padx=18,
                       pady=7).pack(side="right", padx=(0, 8))
        self._restore_btn = AnimatedButton(
            brow, text="Restore", command=self._restore_default,
            bg=COLORS["surface"], fg=COLORS["text"],
            font=(F, 9, "bold"), padx=18, pady=7)
        self._restore_btn.pack(side="left")
        Tooltip(self._restore_btn, "Restores your DNS to default")

        # closing cancels probes + the watchdog (base close hook)
        self.on_close(self._cancel_probes)
        self._start_test()

    def _cancel_probes(self):
        try:
            self._stop_token[0] = True
        except Exception:
            pass
        try:
            if self._watchdog_after is not None:
                self._dlg.after_cancel(self._watchdog_after)
        except Exception:
            pass
        self._watchdog_after = None
        try:
            self._bar.set_indeterminate(False)
        except Exception:
            pass

    # ---- test orchestration -------------------------------------------- #

    def _ui(self, fn, *args):
        """Marshal a paint hop to the Tk thread (worker-safe).

        F10: never touch Tk from the worker (even winfo_exists burns
        ~1s off-thread) — schedule unconditionally; a destroyed dialog
        raises inside after() and is swallowed below."""
        try:
            self._dlg.after(0, lambda: fn(*args))
        except Exception:
            pass

    def _start_test(self):
        try:
            self._stop_token[0] = True
        except Exception:
            pass
        token = self._stop_token = [False]
        self._results = {}
        self._done_n = 0
        self._finished = False
        self._selected_ip = None
        # round generation: every hop below carries THIS round's gen so a
        # previous round's late prober can never corrupt the new round's
        # counts (rapid Re-test used to mix results across rounds)
        self._scan_gen = getattr(self, "_scan_gen", 0) + 1
        gen = self._scan_gen
        try:
            if self._watchdog_after is not None:
                self._dlg.after_cancel(self._watchdog_after)
        except Exception:
            pass
        self._watchdog_after = None
        for w in list(self._rows_body.winfo_children()):
            try:
                w.destroy()
            except Exception:
                pass
        self._row_widgets = {}
        try:
            self._apply_btn.set_enabled(False)
            self._apply_btn.config_text("Apply")
            self._bar.set_indeterminate(True)
            self._set_status("Reading your current DNS…")
        except Exception:
            pass
        try:
            import threading as _th
            _th.Thread(target=self._test_worker, args=(token, gen),
                       daemon=True).start()
        except Exception:
            pass
        # watchdog: finish with whatever landed (a hung prober must never
        # wedge the dialog — every probe is socket-timeout bounded, this
        # is belt and suspenders). Carries the round gen so a previous
        # round's watchdog can't finish the new round early.
        try:
            self._watchdog_after = self._dlg.after(
                25000, lambda _g=gen: self._finish(stalled=True, _gen=_g))
        except Exception:
            pass

    def _test_worker(self, token, gen):
        def dead():
            # F10: token-only — close handlers already set token on the Tk
            # thread; winfo_exists here burned ~1s off-thread per call.
            try:
                return bool(token[0])
            except Exception:
                return True

        try:
            from app.dns_test import (DNS_RESOLVERS, build_targets,
                                      get_current_dns, time_resolver)
        except Exception:
            self._ui(self._set_status, "DNS tester unavailable.")
            self._ui(self._finish, False, gen)
            return
        if dead():
            return
        current = []
        try:
            current = get_current_dns()
        except Exception:
            current = []
        if dead():
            return
        # rows: public resolvers always; the machine's current DNS only
        # when publicly testable (build_targets holds router IPs back —
        # timing them can only produce a misleading 'no reply').
        try:
            targets, skipped = build_targets(current)
        except Exception:
            targets, skipped = ([(n, ip, False) for n, ip in DNS_RESOLVERS], [])
        self._expected = len(targets)
        self._ui(self._build_rows, targets, skipped, gen)
        import threading as _th

        def _probe(name, ip):
            if dead():
                return
            try:
                ms = time_resolver(ip, cancelled=lambda: dead())
            except Exception:
                ms = None
            if dead():
                return
            self._ui(self._paint_result, ip, ms, gen)

        for _name, _ip, _cur in targets:
            if dead():
                return
            _th.Thread(target=_probe, args=(_name, _ip),
                       daemon=True).start()

    # ---- paints (Tk thread only) ---------------------------------------- #

    def _set_status(self, text):
        try:
            if self._status_lbl.winfo_exists():
                self._status_lbl.config(text=text)
        except Exception:
            pass

    def _build_rows(self, targets, skipped_current=None, _gen=None):
        # stale-round guard: a previous round's late hop must never
        # rebuild the current round's rows (rapid Re-test races it)
        if _gen is not None and _gen != getattr(self, "_scan_gen", _gen):
            return
        for w in list(self._rows_body.winfo_children()):
            try:
                w.destroy()
            except Exception:
                pass
        self._row_widgets = {}
        self._selected_ip = None
        try:
            self._apply_btn.set_enabled(False)
            self._apply_btn.config_text("Apply")
        except Exception:
            pass
        for name, ip, is_current in targets:
            row = tk.Frame(self._rows_body, bg=COLORS["bg_alt"])
            try:
                from app.dns_test import get_dns_pair as _pair
                _pri, _sec = _pair(ip)
                _pair_txt = f"{_pri} / {_sec}" if _sec != _pri else _pri
            except Exception:
                _pair_txt = ip
            title = f"{name}  {_pair_txt}" + ("  (current)" if is_current else "")
            name_lbl = tk.Label(row, text=title, font=(F, 9, "bold"),
                                bg=COLORS["bg_alt"], fg=COLORS["text"],
                                anchor="w")
            name_lbl.pack(side="left", padx=(8, 0), pady=6)
            badge = tk.Label(row, text="", font=(F, 8, "bold"),
                             bg=COLORS["bg_alt"], fg=COLORS["accent_green"])
            badge.pack(side="left", padx=(6, 0))
            pick = tk.Label(row, text="", font=(F, 10, "bold"),
                            bg=COLORS["bg_alt"], fg=TAB_ACCENTS["Tweak"])
            pick.pack(side="left", padx=(6, 0))
            ms = tk.Label(row, text="…", font=(F, 9, "bold"),
                          bg=COLORS["bg_alt"], fg=COLORS["subtext"])
            ms.pack(side="right", padx=(0, 8))
            self._row_widgets[ip] = {"row": row, "ms": ms, "badge": badge,
                                     "pick": pick, "name": name,
                                     "current": is_current}
            # rows become clickable once they answer (unanswered rows
            # ignore clicks — picking a dead server can only fail Apply)
            for w in (row, name_lbl, badge, pick, ms):
                try:
                    w.bind("<Button-1>",
                           lambda e, _ip=ip: self._select_ip(_ip), add="+")
                except Exception:
                    pass
            row.pack(fill="x", pady=2)
        # (user call 2026-09: the router-DNS footnote is gone — held-back
        # router IPs are simply absent from the list, no explanation owed)
        try:
            self._set_status(f"Testing {len(targets)} servers…")
        except Exception:
            pass

    def _paint_result(self, ip, ms, _gen=None):
        if _gen is not None and _gen != getattr(self, "_scan_gen", _gen):
            return
        self._results[ip] = ms
        self._done_n += 1
        try:
            handles = self._row_widgets.get(ip)
            if handles is not None and handles["ms"].winfo_exists():
                if ms is None:
                    handles["ms"].config(text="no reply",
                                         fg=COLORS["accent_red"])
                else:
                    handles["ms"].config(text=f"{ms:.0f} ms",
                                         fg=COLORS["text"])
                    # answered rows are pickable — afford the click
                    try:
                        handles["row"].config(cursor="hand2")
                        for _w in handles["row"].winfo_children():
                            _w.config(cursor="hand2")
                    except Exception:
                        pass
            remaining = max(0, self._expected - self._done_n)
            if remaining:
                self._set_status(f"{remaining} server(s) left…")
        except Exception:
            pass
        if self._done_n >= self._expected:
            self._finish()

    @staticmethod
    def _row_tint(selected):
        """Selection language (user redesign): a selected row is TINTED,
        not annotated — accent-mixed bg the eye can't miss, same trick
        as hover states. Deselected rows return to plain bg_alt."""
        if selected:
            return _hex_lerp(COLORS["bg_alt"], TAB_ACCENTS["Tweak"], 0.30)
        return COLORS["bg_alt"]

    def _tint_row(self, ip, selected):
        """Repaint one row (frame + every child label) in/out of the
        selection tint. The old '✓ selected' text label is gone —
        highlight IS the signal now."""
        try:
            handles = (self._row_widgets or {}).get(ip)
            if handles is None:
                return
            bg = self._row_tint(selected)
            row = handles["row"]
            if row.winfo_exists():
                row.config(bg=bg)
                for w in row.winfo_children():
                    try:
                        w.config(bg=bg)
                    except Exception:
                        pass
                # the pick dot glyph (radio-style ●) rides the name label
                if selected:
                    handles["pick"].config(text="●")
                else:
                    handles["pick"].config(text="")
        except Exception:
            pass

    def _select_ip(self, ip):
        """Pick a server row (user choice — the fastest is only the
        default). Unanswered rows ignore clicks: picking a dead server
        could only fail Apply. Picking the already-current server shows
        an honest 'nothing to change' state instead of a no-op Apply.
        The status names the full (primary / secondary) pair Apply
        will set, so dual-DNS is visible before the click."""
        try:
            if self._results.get(ip) is None:
                return
            self._selected_ip = ip
            for _ip in (self._row_widgets or {}):
                self._tint_row(_ip, _ip == ip)
            handles = (self._row_widgets or {}).get(ip, {})
            name = handles.get("name", ip)
            try:
                from app.dns_test import get_dns_pair as _pair
                _pri, _sec = _pair(ip)
                _pair_txt = f"{_pri} / {_sec}" if _sec != _pri else _pri
            except Exception:
                _pair_txt = ip
            try:
                wms = self._results.get(ip)
                if handles.get("current"):
                    self._set_status(f"You're already using {name} "
                                     f"({wms:.0f} ms) — nothing to change.")
                    self._apply_btn.set_enabled(False)
                else:
                    self._set_status(f"Selected: {name} ({_pair_txt}, {wms:.0f} ms).")
                    self._apply_btn.set_enabled(True)
            except Exception:
                pass
        except Exception:
            pass

    def _finish(self, stalled=False, _gen=None):
        if self._finished:
            return
        # stale-round guard (see _build_rows): only the current round
        # may finish the dialog
        if _gen is not None and _gen != getattr(self, "_scan_gen", _gen):
            return
        self._finished = True
        try:
            if self._watchdog_after is not None:
                self._dlg.after_cancel(self._watchdog_after)
        except Exception:
            pass
        self._watchdog_after = None
        try:
            self._bar.set_indeterminate(False)
            self._bar.set_fraction(1.0)
        except Exception:
            pass
        try:
            from app.dns_test import pick_winner
            winner = pick_winner(self._results)
        except Exception:
            winner = None
        if not winner:
            self._set_status("Couldn't reach any DNS server — check your connection, then Re-test."
                             if not stalled else
                             "Timed out waiting for replies — check your connection, then Re-test.")
            return
        # crown the winner row (fastest answers first — the default pick)
        try:
            handles = self._row_widgets.get(winner)
            if handles is not None and handles["badge"].winfo_exists():
                handles["badge"].config(text="★ fastest")
        except Exception:
            pass
        # default-select the winner; the user may still pick any other
        # answering server (their choice is never forced)
        try:
            self._select_ip(winner)
        except Exception:
            pass

    # ---- actions ---------------------------------------------------------- #

    def _apply_selected(self):
        """Close, then run the REAL gaming_dns task with the pick's
        (primary, secondary) pair through the standard engine
        (preflight, admin gate, snapshot/undo, scorecard).
        dataclasses.replace keeps the applied key 'gaming_dns' so
        badges, Undo and Tweak Health all work."""
        try:
            from app.dns_test import get_dns_pair
            winner = self._selected_ip
            if not winner or self._results.get(winner) is None:
                return
            primary, secondary = get_dns_pair(winner)
        except Exception:
            return
        try:
            from app.tab_presets import TABS as _TABS
            task = next(t for t in _TABS.get("Tweak", []) if t.key == "gaming_dns")
        except Exception:
            return
        try:
            import dataclasses as _dc

            def _run(ctx, _w=primary, _s=secondary):
                from app.tasks.tweak_tasks import apply_gaming_dns as _apply
                return _apply(ctx, servers=(_w, _s))

            winner_task = _dc.replace(task, run=_run)
        except Exception:
            return
        self.close()
        try:
            self.app.run_tasks("Tweak", [winner_task], mode="run")
        except Exception:
            pass

    def _restore_default(self):
        """Close, then run the REAL gaming_dns revert through the
        standard engine — restores each snapshotted adapter's exact
        prior DNS (static pair or DHCP via -ResetServerAddresses),
        so both primary + secondary go back. Same key/mode as
        Tweak-tab Undo; with no snapshot the revert reports honestly
        instead of guessing."""
        try:
            from app.tab_presets import TABS as _TABS
            task = next(t for t in _TABS.get("Tweak", []) if t.key == "gaming_dns")
        except Exception:
            return
        if getattr(task, "revert", None) is None:
            return
        self.close()
        try:
            self.app.run_tasks("Tweak", [task], mode="revert")
        except Exception:
            pass


class _CatalogPickerDialog(ThemedModal):
    """Searchable installed-games/apps picker (Phase-5 pilot overhaul).

    One shared ThemedModal (fixed footprint, X, Escape, scrim) showing a
    scrollable, filterable list of catalog entries. Live keyboard filter
    (RoundedEntry), launcher chip on each row, click to pick.

    Enumeration NEVER blocks the open: pass scan_fn (zero-arg callable)
    and the dialog maps instantly with an honest "Scanning…" row while a
    daemon thread enumerates; results land via a Tk-thread hop. A big
    Steam library or registry on a cold disk must never freeze the UI
    (user bug report: first click froze, then the picker jerked in).
    Precomputed `entries` still works (fast sources, tests).
    """

    def __init__(self, parent, title, entries=None, accent=None,
                 on_pick=None, empty_text="Nothing found on this PC.",
                 scan_fn=None, scanning_text="Scanning…"):
        # entries: precomputed iterable of {name, launcher, path}, or
        # None when scan_fn supplies them asynchronously
        self._all = list(entries or [])
        self._shown = []
        self._on_pick = on_pick
        self._empty_text = empty_text
        self._scanning_text = scanning_text
        self._scanning = scan_fn is not None
        self._scan_token = [False]   # True = stopped/obsolete
        super().__init__(parent, title=title,
                         accent=accent or TAB_ACCENTS["Tweak"])
        body = self.body

        self._search_var = tk.StringVar()
        self._search_var.trace_add("write", lambda *_: self._apply_filter())
        entry = RoundedEntry(body, textvariable=self._search_var,
                             placeholder="Type to filter…")
        entry.pack(fill="x", pady=(2, 6))

        self._list_panel = panel = ScrollableRoundedPanel(body)
        panel.pack(fill="both", expand=True, pady=(0, 4))
        self._list_body = panel.inner
        self._apply_filter()

        brow = tk.Frame(body, bg=COLORS["bg"])
        brow.pack(fill="x", pady=(4, 2))
        AnimatedButton(brow, text="Cancel", command=self.close,
                       bg=COLORS["surface"], fg=COLORS["text"],
                       font=(F, 9, "bold"), padx=18, pady=6).pack(side="right")

        if scan_fn is not None:
            # closing a dialog with a sweep in flight is safe: the poll
            # chain checks _modal_closed first (see _check_sweep), the
            # worker only ever writes the holder dict, and the thread is
            # a daemon (dies with the process)
            self._sweep_holder = {"done": False, "found": []}
            try:
                import threading as _th
                _th.Thread(target=lambda: self._sweep_worker(scan_fn),
                           daemon=True,
                           name="CatalogPickerScan").start()
            except Exception:
                self._scanning = False
                self._apply_filter()
            else:
                try:
                    self._dlg.after(150, lambda: self._check_sweep())
                except Exception:
                    pass

    def _sweep_worker(self, scan_fn):
        """Worker thread: enumerate into the holder dict. Pure Python —
        NEVER a Tk call (foreign Tk calls burn ~1s and raise unless the
        main thread happens to be inside event dispatch). The Tk-thread
        poller below publishes."""
        try:
            found = scan_fn() or []
        except Exception:
            found = []
        try:
            self._sweep_holder["found"] = list(found)
        except Exception:
            pass
        try:
            self._sweep_holder["done"] = True
        except Exception:
            pass

    def _check_sweep(self):
        """Tk-thread poller: publish swept entries once ready. Stops on
        close; re-arms while the sweep flies."""
        try:
            if self._modal_closed:
                return
            if not self._dlg.winfo_exists():
                return
        except Exception:
            return
        try:
            holder = self._sweep_holder
        except Exception:
            return
        if not holder.get("done"):
            try:
                self._dlg.after(150, lambda: self._check_sweep())
            except Exception:
                pass
            return
        self._all = list(holder.get("found") or [])
        self._scanning = False
        self._apply_filter()

    def _cancel_scan(self):
        # retained for API compat (older callers may invoke it); the
        # holder/closed checks in _check_sweep make it a no-op path
        try:
            self._scan_token[0] = True
        except Exception:
            pass

    def _apply_filter(self):
        q = (self._search_var.get() or "").strip().lower()
        for w in list(self._list_body.winfo_children()):
            try:
                w.destroy()
            except Exception:
                pass
        shown = 0
        for g in self._all:
            if q and q not in g.get("name", "").lower() \
                    and q not in g.get("launcher", "").lower():
                continue
            self._build_row(g)
            shown += 1
            if shown >= 300:      # hard cap: pathological registries
                break
        if not shown:
            # scanning state gets its own honest placeholder (not the
            # empty state — "nothing found" while still searching lies)
            text = (self._scanning_text if getattr(self, "_scanning", False)
                    else self._empty_text)
            tk.Label(self._list_body, text=text,
                     font=(F, 9), bg=COLORS["bg_alt"],
                     fg=COLORS["subtext"]).pack(pady=14)
        try:
            self._list_panel.refresh_scroll()
        except Exception:
            pass

    def _build_row(self, g):
        row = tk.Frame(self._list_body, bg=COLORS["bg_alt"])
        inner = tk.Frame(row, bg=COLORS["bg_alt"])
        inner.pack(fill="both", expand=True)
        chip = tk.Label(inner, text=g.get("launcher", "?"), font=(F, 8, "bold"),
                        bg=_hex_lerp(COLORS["bg_alt"], COLORS["accent_sky"], 0.18),
                        fg=COLORS["accent_sky"], padx=6, pady=2)
        chip.pack(side="left", padx=(8, 6), pady=6)
        name = g.get("name", "?")
        tip = (f"{g.get('launcher', '')} — {g.get('folder', '')}"
               if g.get("folder") else name)
        lbl = tk.Label(inner, text=name, font=(F, 9, "bold"),
                       bg=COLORS["bg_alt"], fg=COLORS["text"],
                       anchor="w", cursor="hand2")
        lbl.pack(side="left", anchor="center")
        try:
            Tooltip(lbl, tip)
        except Exception:
            pass
        for w in (row, inner, chip, lbl):
            try:
                w.bind("<Button-1>",
                       lambda e, _g=g: self._pick(_g), add="+")
            except Exception:
                pass
        row.pack(fill="x", pady=1)

    def _pick(self, g):
        cb = self._on_pick
        self.close()
        if cb is not None:
            try:
                cb(g)
            except Exception:
                pass


def _scan_installed_games():
    """Worker-thread catalog sweep for the games picker (imports live
    here so the click path pays nothing — not even an import)."""
    try:
        from app.game_catalog import installed_games
        return installed_games()
    except Exception:
        return []


def _scan_installed_apps():
    """Worker-thread catalog sweep for the apps picker (same)."""
    try:
        from app.game_catalog import installed_apps
        return installed_apps()
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

        # master switch row
        mrow = tk.Frame(body, bg=COLORS["bg"])
        mrow.pack(fill="x", pady=(10, 0))
        self._enabled_var = tk.BooleanVar(
            value=bool(cfg.get("session_pilot_enabled", False)))
        ToggleSwitch(mrow, self._enabled_var).pack(side="left")
        tk.Label(mrow, text="Watch for games", font=(F, 10, "bold"),
                 bg=COLORS["bg"], fg=COLORS["text"]).pack(
                     side="left", padx=(10, 0))
        self._live_lbl = tk.Label(mrow, text="", font=(F, 9, "bold"),
                                  bg=COLORS["bg"], fg=COLORS["subtext"])
        self._live_lbl.pack(side="right")
        try:
            self._enabled_var.trace_add("write", self._on_toggle)
        except Exception:
            pass

        # preset picker row
        prow = tk.Frame(body, bg=COLORS["bg"])
        prow.pack(fill="x", pady=(10, 0))
        tk.Label(prow, text="When a game starts, run:",
                 font=(F, 9), bg=COLORS["bg"],
                 fg=COLORS["text"]).pack(side="left")
        self._preset_var = tk.StringVar(value=preset)
        combo = self._preset_combo = ttk.Combobox(
            prow, textvariable=self._preset_var,
            values=self._preset_names, state="readonly",
            width=16, font=(F, 9))
        combo.pack(side="left", padx=(8, 0))
        try:
            combo.bind("<<ComboboxSelected>>", self._on_preset)
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
        # installed apps, and the classic drive browse for portable exes
        addrow = tk.Frame(body, bg=COLORS["bg"])
        addrow.pack(fill="x", pady=(6, 0))
        AnimatedButton(addrow, text="+ Installed games…",
                       command=self._pick_from_games,
                       bg=COLORS["surface"], fg=COLORS["text"],
                       font=(F, 9), padx=14, pady=5).pack(side="left")
        AnimatedButton(addrow, text="+ Installed apps…",
                       command=self._pick_from_apps,
                       bg=COLORS["surface"], fg=COLORS["text"],
                       font=(F, 9), padx=14, pady=5).pack(
                           side="left", padx=(8, 0))
        AnimatedButton(addrow, text="+ Browse…",
                       command=self._add_game,
                       bg=COLORS["surface"], fg=COLORS["text"],
                       font=(F, 9), padx=14, pady=5).pack(
                           side="left", padx=(8, 0))
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
                    "Every game found on this PC (Steam, Epic, GOG, Ubisoft,\n"
                    "Riot, Xbox, EA) is watched automatically — newly installed\n"
                    "games are picked up on the next app start. Matches are\n"
                    "path-verified, so a same-named exe elsewhere never fires.\n"
                    "Turn this off to watch only the list below.")
        except Exception:
            pass
        try:
            self._watch_all_var.trace_add("write", self._on_watch_all)
        except Exception:
            pass
        self._rebuild_game_rows()

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

    def set_live_status(self, text, color=None):
        try:
            self._live_lbl.config(text=text,
                                  fg=color or COLORS["subtext"])
        except Exception:
            pass


class HealthReportDialog(ThemedModal):
    """PC Health Report Card (user-approved feature 7, 2026-09): one
    click grades the PC's vitals — SSD, GPU driver, disk space, Windows
    files, reclaimable junk — with plain-language findings and one-click
    fixes. Six uniform cards (2x3, like the preset cards): five sensors
    plus the overall score. Sensors stream in live as each finishes;
    anything unmeasurable grades '?' (unknown), never a fake F."""

    _GRADE_COLORS = {
        "A": COLORS["accent_green"],
        "B": COLORS["accent_sky"],
        "C": COLORS["accent_yellow"],
        "D": COLORS["accent_yellow"],
        "F": COLORS["accent_red"],
        "?": COLORS["subtext"],
    }
    # sensor key -> (card title, grid position)
    _CARDS = (
        ("smart", "SSD & Drives"),
        ("gpu", "GPU Driver"),
        ("disk", "Disk Space"),
        ("windows", "Windows Files"),
        ("junk", "Reclaimable Junk"),
        ("overall", "Overall"),
    )

    def __init__(self, parent, app):
        self.app = app
        self._stop_token = [False]
        self._cards = {}            # key -> card dict (landed)
        self._card_widgets = {}     # key -> {"letter","headline","detail","action"}
        self._card_actions = {}     # key -> callable (wired at paint time)
        self._scan_done = False

        super().__init__(parent, title="PC Health Report",
                         accent=COLORS["accent_green"])
        dlg = self._dlg
        body = self.body

        tk.Label(body, text="Simple grades for a complicated machine — green is good.",
                 font=(F, 9), bg=COLORS["bg"],
                 fg=COLORS["subtext"], wraplength=680,
                 justify="left").pack(anchor="w")

        # 2x3 uniform grid (same trick as the preset cards: uniform
        # columns + rows stay symmetric at any content height).
        # FROZEN LAYOUT (user bug report: live sensor paints resized the
        # rows — action buttons popping in, Stop vanishing, headlines
        # growing — shoving the footer out of the fixed 600px dialog and
        # making the whole card wall visibly churn mid-scan). Rows carry
        # a minsize floor so pending->painted transitions never shrink or
        # grow them for typical content, and every card reserves its
        # action slot (see _build_card) so buttons appearing/vanishing
        # move nothing.
        grid = tk.Frame(body, bg=COLORS["bg"])
        grid.pack(fill="x", pady=(10, 0))
        for c in (0, 1):
            grid.grid_columnconfigure(c, weight=1, uniform="healthcards")
        for r in (0, 1, 2):
            grid.grid_rowconfigure(r, weight=1, uniform="healthrows",
                                   minsize=144)
        for i, (key, title) in enumerate(self._CARDS):
            rr, cc = divmod(i, 2)
            self._build_card(grid, rr, cc, key, title)

        # footer: status left, actions right
        foot = tk.Frame(body, bg=COLORS["bg"])
        foot.pack(fill="x", pady=(10, 6))
        self._status_lbl = tk.Label(foot, text="Starting check…", font=(F, 9),
                                    bg=COLORS["bg"], fg=COLORS["subtext"],
                                    anchor="w")
        self._status_lbl.pack(side="left")
        self._close_btn = AnimatedButton(
            foot, text="Close", command=self.close,
            bg=COLORS["accent_green"], fg=COLORS["black"],
            font=(F, 9, "bold"), padx=18, pady=7)
        self._close_btn.pack(side="right")
        self._rescan_btn = AnimatedButton(
            foot, text="Rescan", command=self._start_scan,
            bg=COLORS["surface"], fg=COLORS["text"],
            font=(F, 9, "bold"), padx=18, pady=7)
        self._rescan_btn.pack(side="right", padx=(0, 8))
        self._stop_btn = AnimatedButton(
            foot, text="Stop", command=self._stop_scan,
            bg=COLORS["surface"], fg=COLORS["accent_red"],
            font=(F, 9, "bold"), padx=18, pady=7)
        self._stop_btn.pack(side="right", padx=(0, 8))

        # closing cancels the sensor sweep (base close hook)
        self.on_close(self._cancel_scan)
        self._start_scan()

    def _cancel_scan(self):
        try:
            self._stop_token[0] = True
        except Exception:
            pass

    # ---- cards ----------------------------------------------------------- #

    def _build_card(self, grid, rr, cc, key, title):
        # Fixed-height card (see grid minsize above): the inner frame
        # never propagates its size, and the action slot below is ALWAYS
        # packed (empty until actionable) — painting grades, headlines,
        # details, or showing/hiding the action button cannot move a
        # single pixel of the surrounding layout.
        outer = tk.Frame(grid, bg=COLORS["hairline"], bd=0)
        outer.grid(row=rr, column=cc, sticky="nsew", padx=5, pady=4)
        # rigid inner (the freeze guarantee): fixed 134px, propagate off,
        # AND no vertical expand — fill+expand would let a grown grid row
        # stretch the card (defeating the fixed height the probe caught).
        # Budget inside 134: title 28 + mid 40 + detail ≤32 (2 lines) +
        # slot 30 = 130. Realistic content (≤2-line details) fits with
        # zero movement; pathological 3+-line sensor verbosity clips its
        # tail inside the card while buttons/footer never move.
        inner = tk.Frame(outer, bg=COLORS["bg_alt"], height=134)
        inner.pack(fill="x", padx=1, pady=1)
        inner.pack_propagate(False)
        tk.Label(inner, text=title, font=(F, 9, "bold"),
                 bg=COLORS["bg_alt"], fg=COLORS["text"],
                 anchor="w").pack(fill="x", padx=10, pady=(8, 0))
        mid = tk.Frame(inner, bg=COLORS["bg_alt"])
        mid.pack(fill="x", padx=10, pady=(2, 0))
        # fixed 2-char width: grade glyphs (…/A/?/C/F) differ in width at
        # 24pt — without this the headline jumps horizontally on every
        # paint (caught by the layout-freeze probe)
        letter = tk.Label(mid, text="…", font=(F, 24, "bold"), width=2,
                          bg=COLORS["bg_alt"], fg=COLORS["subtext"])
        letter.pack(side="left", anchor="n")
        headline = tk.Label(mid, text="Checking…", font=(F, 9),
                            bg=COLORS["bg_alt"], fg=COLORS["subtext"],
                            anchor="w", justify="left", wraplength=190)
        headline.pack(side="left", anchor="center", padx=(8, 0))
        detail = tk.Label(inner, text="", font=(F, 8),
                          bg=COLORS["bg_alt"], fg=COLORS["subtext"],
                          anchor="w", justify="left", wraplength=250)
        detail.pack(fill="x", padx=10)
        # reserved action slot — ONLY on cards that can ever show one
        # (gpu + overall). A slot on every card would steal 34px from
        # cards whose details legitimately run 2-3 lines; conditional
        # reservation keeps every realistic content combo inside the
        # fixed 134px. Constant 30px when present: appearing/vanishing
        # actions move nothing. Anchored to the card BOTTOM so a
        # pathological detail can only clip its own (tooltip-backed)
        # tail, never the button (the actionable UI the user must
        # always reach). The button is parented TO the slot so it fills
        # exactly the reserved space; other cards get an unpacked,
        # invisible button (zero geometry impact, uniform handles).
        slot = None
        if key in ("gpu", "overall"):
            slot = tk.Frame(inner, bg=COLORS["bg_alt"], height=30)
            slot.pack(side="bottom", fill="x", padx=10, pady=(2, 2))
            slot.pack_propagate(False)
        action = AnimatedButton(slot if slot is not None else inner,
                                text="",
                                command=lambda k=key: self._fire_card_action(k),
                                bg=COLORS["surface"], fg=COLORS["text"],
                                font=(F, 8, "bold"), padx=10, pady=4)
        # packed on demand (GPU/overall cards only, when actionable)
        self._card_widgets[key] = {"outer": outer, "letter": letter,
                                   "headline": headline, "detail": detail,
                                   "action": action, "slot": slot}

    def _grade_color(self, grade):
        return self._GRADE_COLORS.get(grade, COLORS["subtext"])

    def _paint_card(self, key, card):
        handles = (self._card_widgets or {}).get(key)
        if handles is None:
            return
        try:
            grade = card.get("grade", "?")
            if handles["letter"].winfo_exists():
                handles["letter"].config(text=grade,
                                         fg=self._grade_color(grade))
            if handles["headline"].winfo_exists():
                handles["headline"].config(text=card.get("headline", ""),
                                           fg=COLORS["text"])
            if handles["detail"].winfo_exists():
                # display cap (layout-freeze guarantee): the fixed 134px
                # card fits title + mid + N detail lines + (on gpu) the
                # 30px action slot. Unbounded sensor verbosity (100+
                # char failure messages = 3-4 lines) would overflow the
                # card and bury the action button — so displayed detail
                # is capped (gpu shares its card with a button: 1 line;
                # button-less cards: 2 lines) with "…" + the FULL text
                # one hover away in a tooltip. Sensors and logs keep the
                # complete message; only the card face is bounded. This
                # makes the worst case deterministic: nothing painted
                # here can ever move the layout or leave the dialog.
                try:
                    full = card.get("detail", "") or ""
                    cap = 41 if key == "gpu" else 82
                    if len(full) > cap:
                        handles["detail"].config(text=full[:cap - 1] + "…")
                        try:
                            Tooltip(handles["detail"], full)
                        except Exception:
                            pass
                    else:
                        handles["detail"].config(text=full)
                except Exception:
                    handles["detail"].config(text=card.get("detail", ""))
            # per-card action: GPU card only, when the driver needs love.
            # The button lives in the reserved slot (constant height) —
            # showing/hiding it never resizes the card.
            try:
                btn = handles["action"]
                show = (key == "gpu" and grade in ("B", "C", "D", "F"))
                if show and btn.winfo_exists():
                    btn.config_text("Update driver →")
                    self._card_actions[key] = self._goto_install
                    btn.pack(fill="both", expand=True)
                elif btn.winfo_exists() and btn.winfo_manager() != "":
                    btn.pack_forget()
                    try:
                        self._card_actions.pop(key, None)
                    except Exception:
                        pass
            except Exception:
                pass
        except Exception:
            pass

    def _fire_card_action(self, key):
        """Run a card's wired action (no-op when none)."""
        try:
            fn = (self._card_actions or {}).get(key)
        except Exception:
            fn = None
        if fn is None:
            return
        try:
            fn()
        except Exception:
            pass

    def _paint_overall(self):
        try:
            from app.health_scan import (count_fixes, fix_plan,
                                         overall_grade)
            grades = {k: (c.get("grade", "?") if isinstance(c, dict) else "?")
                      for k, c in (self._cards or {}).items()}
            overall = overall_grade(grades)
            n = count_fixes(self._cards)
            action, _payload = fix_plan(self._cards)
            handles = (self._card_widgets or {}).get("overall")
            if handles is None:
                return
            if handles["letter"].winfo_exists():
                handles["letter"].config(text=overall,
                                         fg=self._grade_color(overall))
            if handles["headline"].winfo_exists():
                handles["headline"].config(
                    text=("All good ✓" if n == 0 else
                          f"{n} thing{'s' if n != 1 else ''} to fix"),
                    fg=COLORS["text"])
            if handles["detail"].winfo_exists():
                handles["detail"].config(text="")
            try:
                btn = handles["action"]
                if btn.winfo_exists():
                    if action == "none":
                        btn.pack_forget()
                        try:
                            self._card_actions.pop("overall", None)
                        except Exception:
                            pass
                    else:
                        btn.config_text("Fix what's found")
                        btn.pack(fill="both", expand=True)
                        try:
                            self._card_actions["overall"] = (
                                lambda: self._do_fix(action, _payload))
                        except Exception:
                            pass
            except Exception:
                pass
        except Exception:
            pass

    # ---- scan worker ------------------------------------------------------ #

    def _ui(self, fn, *args):
        """Marshal a paint hop to the Tk thread (worker-safe).

        F10: never touch Tk from the worker (even winfo_exists burns
        ~1s off-thread) — schedule unconditionally; a destroyed dialog
        raises inside after() and is swallowed below."""
        try:
            self._dlg.after(0, lambda: fn(*args))
        except Exception:
            pass

    def _set_scan_status(self, text):
        try:
            if self._status_lbl.winfo_exists():
                self._status_lbl.config(text=text)
        except Exception:
            pass

    def _start_scan(self):
        try:
            self._stop_token[0] = True
        except Exception:
            pass
        token = self._stop_token = [False]
        self._scan_done = False
        self._cards = {}
        try:
            self._card_actions = {}
        except Exception:
            pass
        # reset cards to pending (and unpublish any previous round's
        # action buttons — a rescan must not offer a stale fix)
        for key, title in self._CARDS:
            if key == "overall":
                continue
            self._paint_card(key, {"grade": "?", "headline": "Checking…",
                                   "detail": "", "note": ""})
        try:
            self._stop_btn.pack(side="right", padx=(0, 8))
        except Exception:
            pass
        self._paint_overall_pending()
        self._set_scan_status("Starting check…")
        try:
            import threading as _th
            _th.Thread(target=self._scan_worker, args=(token,),
                       daemon=True).start()
        except Exception:
            pass

    def _paint_overall_pending(self):
        try:
            handles = (self._card_widgets or {}).get("overall")
            if handles is None:
                return
            if handles["letter"].winfo_exists():
                handles["letter"].config(text="…", fg=COLORS["subtext"])
            if handles["headline"].winfo_exists():
                handles["headline"].config(text="Scanning…",
                                           fg=COLORS["subtext"])
        except Exception:
            pass

    def _scan_worker(self, token):
        def dead():
            # F10: token-only — close handlers already set token on the Tk
            # thread; winfo_exists here burned ~1s off-thread per call.
            try:
                return bool(token[0])
            except Exception:
                return True

        try:
            from app.health_scan import _collecting_ctx, run_health_scan
        except Exception:
            self._ui(self._set_scan_status, "Health check unavailable.")
            self._ui(self._scan_finished, True)
            return
        ctx = _collecting_ctx(
            [],
            set_status=lambda m: self._ui(self._set_scan_status, m),
            cancelled=dead)
        try:
            cards = run_health_scan(
                ctx,
                on_card=lambda k, c: self._ui(self._paint_card_and_store, k, c))
        except Exception:
            cards = {}
        if dead():
            return
        self._ui(self._scan_finished, bool(token[0]))

    def _paint_card_and_store(self, key, card):
        try:
            self._cards[key] = card
        except Exception:
            pass
        self._paint_card(key, card)

    def _scan_finished(self, cancelled):
        self._scan_done = True
        try:
            self._stop_btn.pack_forget()
        except Exception:
            pass
        self._paint_overall()
        try:
            if cancelled:
                self._set_scan_status("Stopped — partial results shown.")
            else:
                self._set_scan_status("Check complete — pick a fix or close.")
        except Exception:
            pass

    def _stop_scan(self):
        try:
            self._stop_token[0] = True
        except Exception:
            pass

    # ---- fix actions -------------------------------------------------------- #

    def _goto_install(self):
        self.close()
        try:
            self.app._switch_to("Install")
        except Exception:
            pass

    def _do_fix(self, action, payload):
        """Execute the overall Fix plan, then close. Navigation targets
        are tolerant (unknown keys/presets are skipped, never crash)."""
        try:
            if action == "storage":
                self.close()
                try:
                    self.app._open_storage_insight()
                except Exception:
                    pass
                return
            if action == "repair":
                self.close()
                try:
                    page = self.app.tabs.get("Repair")
                    if page is not None:
                        page._enter_custom()
                        for k in payload or []:
                            try:
                                v = page.vars.get(k)
                                if v is not None:
                                    v.set(True)
                            except Exception:
                                pass
                        self.app._switch_to("Repair")
                except Exception:
                    pass
                return
            if action == "clean":
                self.close()
                try:
                    page = self.app.tabs.get("Clean")
                    if page is not None:
                        try:
                            page._select_preset(payload or "Quick Clean")
                        except Exception:
                            pass
                        self.app._switch_to("Clean")
                except Exception:
                    pass
                return
            if action == "install":
                self.close()
                try:
                    self.app._switch_to("Install")
                except Exception:
                    pass
                return
        except Exception:
            pass

    # ---- actions ------------------------------------------------------------ #


def _enable_dark_titlebar(window):
    """Ask Windows to draw this window's title bar dark (DWM immersive mode).

    User feedback: the white title strip clashed with the dark UI. Tkinter
    uses the native window chrome, whose color is owned by Windows — the
    only supported override is DWMWA_USE_IMMERSIVE_DARK_MODE (Win10 1809+;
    attribute 20 on 20H1+, 19 before). Best-effort: any failure just keeps
    the system default. Safe on non-Windows (no-op).
    """
    try:
        import ctypes
        import sys as _sys
        if not _sys.platform.startswith("win"):
            return
        try:
            hwnd = window.winfo_id()
        except Exception:
            return
        # Tk's winfo_id is the inner child window — the DWM attribute wants
        # the real top-level, i.e. its parent. Try both.
        candidates = []
        try:
            candidates.append(ctypes.windll.user32.GetParent(hwnd))
        except Exception:
            pass
        candidates.append(hwnd)
        for attr in (20, 19):
            for h in candidates:
                try:
                    if not h:
                        continue
                    val = ctypes.c_int(1)
                    ctypes.windll.dwmapi.DwmSetWindowAttribute(
                        h, attr, ctypes.byref(val), ctypes.sizeof(val))
                except Exception:
                    continue
    except Exception:
        pass


def _set_window_icon(root: tk.Tk):
    """Set colored 🧼 window icon next to the title — no black square."""
    try:
        candidates = []
        if getattr(sys, "_MEIPASS", None):
            candidates.append(pathlib.Path(sys._MEIPASS) / "app" / "assets" / "icon.ico")
            candidates.append(pathlib.Path(sys._MEIPASS) / "assets" / "icon.ico")
        candidates.append(pathlib.Path(__file__).with_name("assets") / "icon.ico")
        for p in candidates:
            if p.is_file():
                root.iconbitmap(str(p))
                return
    except Exception:
        pass


def _load_title_icon(target=16):
    """Load the real app icon (icon.ico 🧼) as a Tk PhotoImage for
    the in-window title bar (user bug report: the placeholder green dot
    must be the actual icon). Stdlib-only ICO parse: modern .ico files
    embed PNGs — grab the entry that subsamples EXACTLY to `target`
    (no scaling blur), else the smallest entry >= target, else the
    largest available. Returns None on any failure so the caller falls
    back to the drawn dot. Frozen-exe paths included."""
    try:
        import base64 as _b64
        cands = []
        if getattr(sys, "_MEIPASS", None):
            cands.append(pathlib.Path(sys._MEIPASS) / "app" / "assets" / "icon.ico")
            cands.append(pathlib.Path(sys._MEIPASS) / "assets" / "icon.ico")
        cands.append(pathlib.Path(__file__).with_name("assets") / "icon.ico")
        raw = None
        for p in cands:
            try:
                if p.is_file():
                    raw = p.read_bytes()
                    break
            except Exception:
                continue
        if not raw or len(raw) < 22:
            return None
        if raw[0:4] != b"\x00\x00\x01\x00":
            return None
        count = int.from_bytes(raw[4:6], "little")
        at_best, sz_best, factor = None, 0, 1
        big_w, big_at, big_sz = 0, 0, 0
        for i in range(min(count, 32)):
            off = 6 + i * 16
            e = raw[off:off + 16]
            if len(e) < 16:
                break
            w = e[0] or 256
            sz = int.from_bytes(e[8:12], "little")
            at = int.from_bytes(e[12:16], "little")
            if at + 8 > len(raw) or at + sz > len(raw):
                continue
            if raw[at:at + 8] != b"\x89PNG\r\n\x1a\n":
                continue  # BMP entries need a decoder we don't ship
            if w == target:
                at_best, sz_best, factor = at, sz, 1
                break
            if w > target and w % target == 0 and at_best is None:
                at_best, sz_best, factor = at, sz, w // target
            if w > big_w:
                big_w, big_at, big_sz = w, at, sz
        if at_best is None:
            if big_w and big_w >= target:
                # closest larger entry, used as-is when it fits the bar
                at_best, sz_best, factor = big_at, big_sz, 1
            else:
                return None
        img = tk.PhotoImage(
            data=_b64.b64encode(raw[at_best:at_best + sz_best]).decode("ascii"))
        try:
            if factor > 1:
                img = img.subsample(factor, factor)
        except Exception:
            pass
        return img
    except Exception:
        return None


TAB_ACCENTS = {
    "Clean": COLORS["accent_sky"],
    "Repair": COLORS["accent_yellow"],
    "Tweak": COLORS["accent_blue"],
    "Install": COLORS["accent_green"],
    # Tools (user-approved 5th tab): pink was the palette's reserved spare
    # accent — distinct from all four existing tabs at a glance, and warm/
    # energetic for a power-tools page. Mauve was rejected as too close
    # to Tweak's violet.
    "Tools": COLORS["accent_pink"],
}

F = FONT_FAMILY


# --------------------------------------------------------------------------- #
# Tooltip — shared single instance, dark, small
# --------------------------------------------------------------------------- #

class Tooltip:
    # F8: tips appear after a short hover-hold instead of instantly. Sweeping
    # the pointer across Install rows (up to 4 tooltips per row) used to
    # map/unmap the shared window on every row crossing; the delay collapses
    # that churn into one show per deliberate hover.
    SHOW_DELAY_MS = 350

    _shared_tip = None
    _shared_label = None
    _current_widget = None
    # F8: one pending (scheduled, not yet shown) tip at a time. Entering a
    # different tooltip-bearing widget cancels the previous widget's timer.
    _pending_after = None
    _pending_owner = None

    def __init__(self, widget, text: str):
        self.widget = widget
        self.text = text
        widget.bind("<Enter>", self._on_enter)
        widget.bind("<Leave>", self._hide)
        # F8: a destroyed widget must not leave a pending timer behind —
        # Tk after-ids outlive the widget that scheduled them, so the timer
        # is cancelled here (add="+": never wipe another <Destroy> handler)
        widget.bind("<Destroy>", self._on_destroy, add="+")

    @classmethod
    def _ensure_shared(cls, widget):
        """Create the shared tooltip window if needed — parented to the
        ROOT window, never to the hovered widget.

        Bug (user-reported crash spam): the shared Toplevel was parented to
        whichever label was first hovered. Task pages rebuild on every
        preset/mode switch, destroying those labels — and Tk destroys the
        tooltip window WITH its parent. The class kept holding the dead
        reference, so every later hover threw
        'invalid command name ...!toplevel.!label'. Parenting to root
        (which lives for the whole session) plus winfo_exists guards makes
        the tooltip immune to page rebuilds."""
        if cls._shared_tip is not None and not cls._shared_tip.winfo_exists():
            # stale handle (shouldn't happen with root parenting, but be
            # safe) — forget it and rebuild
            cls._shared_tip = None
            cls._shared_label = None
        if cls._shared_tip is None:
            root = widget.winfo_toplevel()
            cls._shared_tip = tw = tk.Toplevel(root)
            tw.wm_overrideredirect(True)
            tw.configure(bg=COLORS["hairline"])
            cls._shared_label = tk.Label(
                tw, text="", bg=COLORS["surface"], fg=COLORS["text"],
                font=(F, 9), justify="left", wraplength=420,
                padx=10, pady=7, bd=0,
            )
            cls._shared_label.pack()
            tw.withdraw()  # start hidden; _show positions + deiconifies

    # ---- F8: delayed show ------------------------------------------- #

    def _on_enter(self, event=None):
        """Hover starts the show-delay clock instead of showing at once."""
        Tooltip._cancel_pending_show()
        if Tooltip._current_widget is not None and Tooltip._current_widget is not self.widget:
            # the pointer reached a NEW tooltip-bearing widget while an old
            # tip is still visible — drop it now (no stale tip lingering
            # through the new widget's delay)
            Tooltip._hide_shared()
        try:
            if not self.widget.winfo_exists():
                return
        except Exception:
            return
        if not self.text:
            return
        Tooltip._pending_owner = self
        try:
            Tooltip._pending_after = self.widget.after(
                self.SHOW_DELAY_MS, self._fire_pending)
        except Exception:
            Tooltip._pending_after = None
            Tooltip._pending_owner = None

    @classmethod
    def _cancel_pending_show(cls):
        """Drop a scheduled-but-not-yet-shown tip (leave / destroy / a
        different widget entered first). after ids are interpreter-wide and
        outlive the widget, so cancelling through the owner is safe even
        when the owner is gone (guarded)."""
        if cls._pending_after is None:
            return
        try:
            if cls._pending_owner is not None:
                cls._pending_owner.widget.after_cancel(cls._pending_after)
        except Exception:
            pass
        cls._pending_after = None
        cls._pending_owner = None

    def _fire_pending(self):
        """F8: the delay elapsed. Show only if the pointer is STILL over
        this widget — a Leave that never arrived (e.g. the page scrolled
        under a stationary pointer) must not pop a stale tip."""
        Tooltip._pending_after = None
        Tooltip._pending_owner = None
        if not self._pointer_over():
            return
        self._show()

    def _pointer_over(self) -> bool:
        """True while the pointer is inside this widget or one of its
        children (root coords, same probe the row hover uses)."""
        try:
            if not self.widget.winfo_exists():
                return False
            x, y = self.widget.winfo_pointerxy()
            w = self.widget.winfo_containing(x, y)
            while w is not None and w is not self.widget:
                w = w.master
            return w is not None
        except Exception:
            return False

    def _on_destroy(self, _e=None):
        """F8: widget died — cancel its pending show and withdraw a tip it
        still owns."""
        if Tooltip._pending_owner is self:
            Tooltip._cancel_pending_show()
        if Tooltip._current_widget == self.widget:
            Tooltip._hide_shared()

    # ---- display ----------------------------------------------------- #

    def _show(self, event=None):
        if not self.text:
            return
        try:
            if not self.widget.winfo_exists():
                return
        except Exception:
            return
        if Tooltip._current_widget is not None and Tooltip._current_widget != self.widget:
            Tooltip._hide_shared()
        Tooltip._current_widget = self.widget
        x = self.widget.winfo_rootx() + 16
        y = self.widget.winfo_rooty() + self.widget.winfo_height() + 6
        # Readable tooltips (user feedback: one-line 80-char cap cut off
        # legit content, e.g. app descriptions + links). Cap at ~170 chars
        # with wraplength so tips flow over 2-3 lines instead of truncating.
        tip = self.text.strip()
        flat = " ".join(tip.split())
        if len(flat) > 170:
            flat = flat[:169].rstrip() + "…"
        Tooltip._ensure_shared(self.widget)
        if Tooltip._shared_tip is None or Tooltip._shared_label is None:
            return
        Tooltip._shared_label.config(text=flat)
        Tooltip._shared_tip.wm_geometry(f"+{x}+{y}")
        Tooltip._shared_tip.deiconify()

    def _hide(self, event=None):
        """Leave (or a programmatic hide): cancel this widget's pending
        show, then withdraw the tip if this widget owns the visible one."""
        if Tooltip._pending_owner is self:
            Tooltip._cancel_pending_show()
        if Tooltip._current_widget == self.widget:
            Tooltip._hide_shared()

    @classmethod
    def _hide_shared(cls):
        if cls._shared_tip is not None and cls._shared_tip.winfo_exists():
            cls._shared_tip.withdraw()
        cls._current_widget = None


# --------------------------------------------------------------------------- #
# Animated primitives
# --------------------------------------------------------------------------- #

def _lerp(a, b, t):
    # clamp t: spring overshoot can push t past 1.0, producing colors
    # outside 00-FF (invalid Tk color names) and off-canvas coords
    t = max(0.0, min(1.0, t)) if isinstance(t, float) else t
    return a + (b - a) * t


def _hex_lerp(c1, c2, t):
    """Blend two #rrggbb colors. t is clamped to [0, 1] — spring overshoot
    can pass 1.0 and would otherwise produce >FF channel values, which Tk
    rejects as invalid color names."""
    t = max(0.0, min(1.0, t))
    r1, g1, b1 = int(c1[1:3], 16), int(c1[3:5], 16), int(c1[5:7], 16)
    r2, g2, b2 = int(c2[1:3], 16), int(c2[3:5], 16), int(c2[5:7], 16)
    return f"#{int(_lerp(r1, r2, t)):02x}{int(_lerp(g1, g2, t)):02x}{int(_lerp(b1, b2, t)):02x}"


def _round_rect_points(x0, y0, x1, y1, r, steps=5):
    """Point list for a rounded rectangle (for Tk smooth polygons, which
    ARE antialiased on Windows — unlike create_rectangle)."""
    import math
    pts = []
    corners = [(x1 - r, y0 + r, -90), (x1 - r, y1 - r, 0),
               (x0 + r, y1 - r, 90), (x0 + r, y0 + r, 180)]
    for cx, cy, start in corners:
        for i in range(steps + 1):
            a = math.radians(start + 90 * i / steps)
            pts += [cx + r * math.cos(a), cy + r * math.sin(a)]
    return pts


class RoundedEntry(tk.Frame):
    """Text entry with rounded corners (user request: filter/search boxes).

    Tk Entries are always rectangular, so a canvas behind draws the rounded
    background + focus ring while a borderless Entry sits on top. Drop-in:
    pack/grid the RoundedEntry itself and use `.entry` exactly like the
    Entry it replaces (textvariable, binds, width all live there).
    Focus ring turns the tab accent on focus, hairline otherwise."""

    RADIUS = 9
    PAD_Y = 5

    def __init__(self, parent, textvariable=None, width=20, font=None,
                 entry_bg=None, accent=None, **kw):
        super().__init__(parent, bg=parent["bg"] if isinstance(parent, tk.Frame) else COLORS["bg"])
        self._accent = accent or COLORS["accent_green"]
        self._fill = entry_bg or COLORS["surface"]
        self._cv = tk.Canvas(self, highlightthickness=0, bd=0, bg=self["bg"])
        self._cv.place(relx=0, rely=0, relwidth=1, relheight=1)
        self.entry = tk.Entry(self, textvariable=textvariable, width=width,
                              bg=self._fill, fg=COLORS["text"],
                              insertbackground=COLORS["text"],
                              font=font or (F, 9), bd=0, relief="flat",
                              highlightthickness=0, **kw)
        self.entry.pack(fill="x", padx=(self.RADIUS + 2, self.RADIUS + 2),
                        pady=self.PAD_Y)
        self.bind("<Configure>", lambda e: self._draw(focused=False))
        # add="+" so call-site placeholder binds (FocusIn/Out) keep working
        self.entry.bind("<FocusIn>", lambda e: self._draw(focused=True), add="+")
        self.entry.bind("<FocusOut>", lambda e: self._draw(focused=False), add="+")
        self.after(0, lambda: self._draw(focused=False))

    def _draw(self, focused):
        try:
            w, h = self.winfo_width(), self.winfo_height()
            if w < 4 or h < 4:
                return
            self._cv.delete("all")
            self._cv.create_polygon(
                _round_rect_points(1, 1, w - 1, h - 1, self.RADIUS),
                smooth=True, fill=self._fill,
                outline=self._accent if focused else COLORS["hairline"], width=2)
        except Exception:
            pass



def _measure_font(root, font):
    """F7: ONE tkfont.Font per (root, font spec), cached on the root.

    _measure used to create + destroy a throwaway tk.Label per call just to
    ask the font how wide/tall the text is — and config_text runs on every
    run-count change, so All On/All Off paid one widget create/destroy +
    redraw per row. Font objects are cheap to keep and die with their root,
    so the cache lives ON the root object (a harness that destroys and
    recreates roots can never serve a stale handle)."""
    cache = getattr(root, "_measure_fonts", None)
    if cache is None:
        cache = root._measure_fonts = {}
    f = cache.get(font)
    if f is None:
        f = cache[font] = tkfont.Font(root=root, font=font)
    return f


class AnimatedButton(tk.Canvas):
    """Rounded button with press/relax animation and hover glow. Renders
    text inside the canvas so the whole pill animates as one piece."""

    def __init__(self, parent, text, command=None, bg=COLORS["surface"],
                 fg=COLORS["text"], hover_bg=None, font=None, padx=26, pady=10,
                 width=None, height=None, **kw):
        super().__init__(parent, highlightthickness=0, bd=0,
                         bg=parent["bg"] if isinstance(parent, (tk.Frame, tk.Canvas)) else COLORS["bg"],
                         **kw)
        self._text = text
        self._command = command
        self._bg = bg
        self._fg = fg
        self._hover_bg = hover_bg or _hex_lerp(bg, "#FFFFFF", 0.08)
        self._font = font or (F, 10, "bold")
        self._padx = padx
        self._pady = pady
        self._press = 0.0          # 0 relaxed .. 1 pressed
        self._hover = 0.0
        # Must exist BEFORE any <Enter>/<Leave> can fire — Tk can deliver
        # a hover event before a click ever happens (user-reported crash).
        self._pressing = False
        self._hovering = False
        self._current_bg = bg
        self._enabled = True
        self._fixed_w = width
        self._fixed_h = height
        self._focus_ring = False

        # keyboard accessibility (audit a11y finding): canvas widgets are
        # invisible to Tab focus by default — accept focus, activate on
        # Return/Space. Focus indication is drawn IN-CANVAS (user feedback:
        # Tk's native highlightthickness box read as a stray 'framework
        # outline' left behind after clicking) — no native focus rectangle.
        self.configure(takefocus=1, highlightthickness=0)
        self.bind("<ButtonPress-1>", self._on_press)
        self.bind("<ButtonRelease-1>", self._on_release)
        self.bind("<Enter>", self._on_enter)
        self.bind("<Leave>", self._on_leave)
        self.bind("<FocusIn>", lambda e: self._draw_focus(True))
        self.bind("<FocusOut>", lambda e: self._draw_focus(False))
        self.bind("<Return>", self._on_key_activate)
        self.bind("<space>", self._on_key_activate)
        self._measure()
        self._draw()

    def _measure(self):
        # F7: ask the cached shared font instead of a throwaway tk.Label.
        # A text-only Label's request size is font.measure(text)+6 wide by
        # font.metrics("linespace")+6 tall (Tk Label border/padding
        # defaults — verified equal to real Label widgets across every
        # in-app font/text pair). Single-line text only, like every button
        # label in this UI; multiline would need per-line measuring.
        fnt = _measure_font(self.winfo_toplevel(), self._font)
        w = fnt.measure(self._text) + 6
        h = fnt.metrics("linespace") + 6
        w += self._padx * 2
        h += self._pady * 2
        if self._fixed_w:
            w = self._fixed_w
        if self._fixed_h:
            h = self._fixed_h
        self.config(width=w, height=h)
        # NOTE: _w/_h are tkinter's reserved widget-path attribute names —
        # store pixel size under _bw/_bh instead (clobbering _w makes the
        # widget's Tcl path an integer and every later canvas call fails
        # with "invalid command name").
        self._bw, self._bh = w, h

    def _draw(self):
        self.delete("all")
        # Press: shrink 3% and darken slightly; hover: lighten slightly.
        t = self._press
        shrink = t * 0.04
        w = self._bw * (1 - shrink)
        h = self._bh * (1 - shrink)
        x0 = (self._bw - w) / 2
        y0 = (self._bh - h) / 2
        color = _hex_lerp(self._current_bg, self._hover_bg, self._hover)
        color = _hex_lerp(color, "#000000", t * 0.12)
        if not self._enabled:
            # disabled: muted flat fill, dimmed text
            color = _hex_lerp(color, COLORS["bg"], 0.45)
        r = h / 2 if h < 44 else 14
        self._round_rect(x0, y0, x0 + w, y0 + h, r, fill=color, outline="")
        if self._focus_ring and self._enabled:
            # in-canvas keyboard focus: 1px brightened outline just inside
            # the pill — visible only while actually Tab-focused
            self._round_rect(x0 + 1.5, y0 + 1.5, x0 + w - 1.5, y0 + h - 1.5,
                             max(1, r - 1.5), outline=_hex_lerp(color, "#FFFFFF", 0.55),
                             width=1)
        fg = self._fg if self._enabled else _hex_lerp(self._fg, COLORS["bg"], 0.45)
        self.create_text(
            self._bw / 2, self._bh / 2, text=self._text,
            font=self._font, fill=fg,
        )

    def _round_rect(self, x0, y0, x1, y1, r, **kw):
        points = [
            x0 + r, y0, x1 - r, y0, x1, y0, x1, y0 + r,
            x1, y1 - r, x1, y1, x1 - r, y1, x0 + r, y1,
            x0, y1, x0, y1 - r, x0, y0 + r, x0, y0,
        ]
        return self.create_polygon(points, smooth=True, **kw)

    def _draw_focus(self, on: bool):
        """Keyboard focus state — rendered in-canvas by _draw (no native
        Tk focus rectangle; user feedback called that a stray outline)."""
        self._focus_ring = on
        self._draw()

    # animation loop
    def _animate(self):
        target_p = 1.0 if self._pressing else 0.0
        target_h = 1.0 if self._hovering else 0.0
        moved = False
        if abs(self._press - target_p) > 0.02:
            self._press = _lerp(self._press, target_p, 0.35)
            moved = True
        if abs(self._hover - target_h) > 0.02:
            self._hover = _lerp(self._hover, target_h, 0.30)
            moved = True
        if moved:
            self._draw()
            self.after(16, self._animate)
        else:
            self._press, self._hover = target_p, target_h
            self._draw()

    def _on_press(self, _):
        if self._enabled:
            self._pressing = True
            self._animate()

    def _on_release(self, _):
        if self._enabled and getattr(self, "_pressing", False):
            self._pressing = False
            self._animate()
            # small haptic-feel delay then fire
            self.after(60, self._fire)

    def _on_key_activate(self, _):
        """Keyboard activation for a Tab-focused button (<Return>/<space>).

        M2 audit fix: this used to call _on_release directly, but that path
        only fires the command when <ButtonPress-1> has first set
        _pressing — so keyboard focus was advertised (takefocus=1) while
        Enter/Space never fired anything. The mouse path is press (set
        _pressing) -> release (fire); a key press has no separate press
        event, so synthesize the press half and run the exact same release
        sequence (same animation, same 60ms-then-fire). The `not
        self._pressing` guard stops held-key auto-repeat from re-firing the
        command."""
        if self._enabled and not self._pressing:
            self._pressing = True
            self._on_release(None)

    def _fire(self):
        if self._enabled and self._command:
            try:
                self._command()
            except Exception as exc:
                # audit fix: bare except made a buggy command a silent no-op
                # — the click appeared to do nothing with zero feedback.
                import traceback
                try:
                    messagebox.showerror(
                        "Unexpected Error",
                        f"Something went wrong running that button:\n\n{exc}\n\n"
                        f"(Full details are in the exported log: the 📋 corner icon)",
                    )
                except Exception:
                    pass
                # surface to stderr too — captured by Export Logs when a
                # console exists (source runs), invisible in windowed builds
                traceback.print_exc()

    def _on_enter(self, _):
        if self._enabled:
            self._hovering = True
            self._animate()

    def _on_leave(self, _):
        self._hovering = False
        self._pressing = False
        self._animate()

    def set_style(self, bg=None, fg=None, hover_bg=None):
        if bg:
            self._bg = self._current_bg = bg
        if fg:
            self._fg = fg
        if hover_bg:
            self._hover_bg = hover_bg
        self._draw()

    def set_enabled(self, enabled: bool):
        self._enabled = enabled
        self._draw()

    def config_text(self, text: str):
        if text == self._text:
            return  # F7: unchanged label — skip re-measure + full redraw
        self._text = text
        self._measure()
        self._draw()


class ToggleSwitch(tk.Canvas):
    """iOS-style toggle, MK3 (user feedback: 'low-res, unpolished, hover
    shows a framework outline').

    What changed vs MK2:
      * SIZE: 44x24 (iOS 'mini' proportions — same shape language, less
        vertical bulk, enables 2-column Custom grids).
      * ANTIALIASING: Tk's create_oval/create_rectangle are NOT antialiased
        on Windows — the capsule caps and knob had visibly jagged edges
        (the 'low-res' look). Everything is now drawn with smooth=True
        polygons (Tk antialiases those), so all edges are crisp.
      * NO HOVER RIM: the old hover affordance drew 3 outline shapes
        slightly OUTSIDE the capsule bounds — at this scale it read as a
        boxy 'framework outline' around the clickable area (user-reported).
        Hover now just brightens the track; keyboard focus brightens it
        slightly more. No outlines anywhere.

    Motion: unchanged critically-damped ease-out (~200ms), no overshoot.
    Track color crossfades muted slate → iOS green (#34C759); knob white
    when ON, gray when OFF so state reads at a glance."""

    # iOS-mini metrics (points ≈ px at 100% scaling)
    W, H = 44, 24
    INSET = 1.5
    KNOB_INSET = 2.5
    GREEN = "#34C759"        # Apple system green
    KNOB_ON = "#FFFFFF"
    KNOB_OFF = "#C3CAD4"     # gray when off — state reads at a glance
    SHADOW = "#000000"
    OFF_TRACK = "#3E4A5A"     # neutral slate, matches dark UI
    EASE = 0.32              # per-tick approach @16ms (settles ~200ms)

    def __init__(self, parent, variable: tk.BooleanVar, command=None,
                 accent=None, **kw):  # accent kept for API compat, ignored
        super().__init__(parent, highlightthickness=0, bd=0,
                         bg=parent["bg"] if isinstance(parent, (tk.Frame, tk.Canvas)) else COLORS["bg"],
                         width=self.W, height=self.H, cursor="hand2", **kw)
        self._var = variable
        self._command = command
        self._pos = 1.0 if variable.get() else 0.0   # 0 off .. 1 on
        self._vel = 0.0                              # kept for API compat (no overshoot now)
        self._target = self._pos
        self._enabled = True
        self._anim_after = None
        self._focused = False
        self.bind("<Button-1>", self._toggle)
        self.bind("<Enter>", lambda e: self._draw(hover=True))
        self.bind("<Leave>", lambda e: self._draw())
        # keyboard accessibility (audit a11y): space/Return toggle, Tab focuses
        self.configure(takefocus=1)
        self.bind("<space>", lambda e: self._toggle(e))
        self.bind("<Return>", lambda e: self._toggle(e))
        self.bind("<FocusIn>", self._on_focus_in)
        self.bind("<FocusOut>", self._on_focus_out)
        variable.trace_add("write", lambda *_: self._sync_from_var())
        self._draw()

    def _on_focus_in(self, _e=None):
        self._focused = True
        self._draw()

    def _on_focus_out(self, _e=None):
        self._focused = False
        self._draw()

    # -- interaction -------------------------------------------------- #

    def _toggle(self, _):
        if not self._enabled:
            return
        self._var.set(not self._var.get())
        if self._command:
            self.after(10, self._command)

    def _sync_from_var(self):
        self._target = 1.0 if self._var.get() else 0.0
        self._start_anim()

    def set_enabled(self, enabled: bool):
        self._enabled = enabled
        self._draw()

    # -- ease-out animation -------------------------------------------- #

    def _start_anim(self):
        if self._anim_after is not None:
            try:
                self.after_cancel(self._anim_after)
            except Exception:
                pass
        self._tick_anim()

    def _tick_anim(self):
        # critically damped: monotonic approach, settles exact, no wobble
        self._pos += (self._target - self._pos) * self.EASE
        if abs(self._target - self._pos) < 0.004:
            self._pos = self._target
            self._vel = 0.0
            self._draw()
            self._anim_after = None
            return
        self._draw()
        self._anim_after = self.after(16, self._tick_anim)

    # -- rendering ------------------------------------------------------ #

    @property
    def _knob_d(self):
        return self.H - self.KNOB_INSET * 2

    def _travel(self):
        return self.W - self.KNOB_INSET * 2 - self._knob_d

    def _draw(self, hover=False):
        self.delete("all")
        w, h = self.W, self.H
        ins = self.INSET
        self._focused_now = self._focused
        # track: ONE smooth capsule polygon (rect middle + semicircle ends
        # in a single antialiased shape) — MK2's rect+2-ovals had unantialiased
        # seams and jagged caps.
        r = (h - ins * 2) / 2
        x0, y0, x1, y1 = ins, ins, w - ins, h - ins
        track = _hex_lerp(self.OFF_TRACK, self.GREEN, self._pos)
        if hover and self._enabled:
            track = _hex_lerp(track, "#FFFFFF", 0.12)
        elif self._focused_now and self._enabled:
            track = _hex_lerp(track, "#FFFFFF", 0.08)
        self._capsule(x0, y0, x1, y1, r, fill=track)
        # knob: smooth-polygon circle (antialiased, unlike create_oval)
        kd = self._knob_d
        cx = self.KNOB_INSET + kd / 2 + self._pos * self._travel()
        knob = _hex_lerp(self.KNOB_OFF, self.KNOB_ON, self._pos)
        if self._enabled:
            self._circle(cx + 0.5, self.KNOB_INSET + kd / 2 + 1.5, kd,
                          fill=self.SHADOW, stipple="gray25")
        self._circle(cx, self.KNOB_INSET + kd / 2, kd, fill=knob)
        # NOTE (user feedback): the old hover rim — 3 outline shapes drawn
        # slightly outside the capsule — is GONE. It read as a 'framework
        # outline' around the clickable area at this scale.

    def _capsule(self, x0, y0, x1, y1, r, **kw):
        """One smooth polygon: rectangle with two TRUE semicircle caps. The
        old version listed only the cap corners and let smooth=True guess
        the curve, which flattened the ends (user feedback: ends weren't
        fully rounded around the knob). Now both ends are sampled along
        real arcs (8 segments each), so the track hugs the circular knob
        with perfect semicircles — still one antialiased shape."""
        import math
        pts = [x0 + r, y0, x1 - r, y0]                      # top edge
        cx, cy = x1 - r, (y0 + y1) / 2                      # right cap
        for i in range(9):
            a = math.radians(-90 + 180 * i / 8)
            pts += [cx + r * math.cos(a), cy + r * math.sin(a)]
        pts += [x1 - r, y1, x0 + r, y1]                      # bottom edge
        cx = x0 + r                                          # left cap
        for i in range(9):
            a = math.radians(90 + 180 * i / 8)
            pts += [cx + r * math.cos(a), cy + r * math.sin(a)]
        return self.create_polygon(pts, smooth=True, **kw)

    def _circle(self, cx, cy, d, **kw):
        """Antialiased circle: 16-point polygon with smooth=True. With few
        points + smooth, Tk renders a clean conic — crisper than
        create_oval's non-antialiased rasterization at small sizes."""
        import math
        pts = []
        for i in range(16):
            a = 2 * math.pi * i / 16
            pts.append(cx + d / 2 * math.cos(a))
            pts.append(cy + d / 2 * math.sin(a))
        return self.create_polygon(pts, smooth=True, **kw)


class AnimatedProgressBar(tk.Canvas):
    """Segmented progress bar with a sliding glow. Real fraction fills green
    segments; while a task is active an indeterminate shimmer runs on top."""

    def __init__(self, parent, accent=COLORS["accent_green"], height=14, **kw):
        super().__init__(parent, height=height, highlightthickness=0, bd=0,
                         bg=COLORS["bg_widget"], **kw)
        self._accent = accent
        self._fraction = 0.0
        self._indeterminate = False
        self._shimmer = 0.0
        self._segs = 20
        self.bind("<Configure>", lambda e: self._draw())

    def set_fraction(self, fraction: float):
        self._fraction = max(0.0, min(1.0, fraction))
        self._draw()

    def set_indeterminate(self, on: bool):
        # M10: a second set_indeterminate(True) without an intervening
        # False used to start a second _shimmer_loop, orphaning the first
        # (double speed + one un-cancellable timer). Re-entry is a no-op;
        # a fresh start cancels any in-flight tick first.
        if on and self._indeterminate and getattr(self, "_shimmer_after", None) is not None:
            return
        self._indeterminate = on
        if on:
            try:
                if getattr(self, "_shimmer_after", None) is not None:
                    self.after_cancel(self._shimmer_after)
                    self._shimmer_after = None
            except Exception:
                pass
            self._shimmer_loop()
        # audit fix (minor): turning it off left the last scheduled
        # _shimmer_loop after() running one more frame (and redrawing the
        # shimmer highlight on top of a finished bar). Cancel any in-flight
        # tick and redraw once from the real fraction.
        else:
            try:
                if getattr(self, "_shimmer_after", None) is not None:
                    self.after_cancel(self._shimmer_after)
                    self._shimmer_after = None
            except Exception:
                pass
            self._draw()

    def _shimmer_loop(self):
        if not self._indeterminate:
            return
        self._shimmer = (self._shimmer + 0.04) % (1.0 + 0.3)
        self._draw()
        self._shimmer_after = self.after(30, self._shimmer_loop)

    def _draw(self):
        self.delete("all")
        w = self.winfo_width()
        h = self.winfo_height()
        if w < 10:
            return
        gap = 3
        seg_w = (w - gap * (self._segs - 1)) / self._segs
        lit = self._fraction * self._segs
        for i in range(self._segs):
            x = i * (seg_w + gap)
            # partial segment alpha via color blend
            seg_t = max(0.0, min(1.0, lit - i))
            if self._indeterminate:
                dist = abs(((i / self._segs) + 0.5) - self._shimmer) % 1.0
                dist = min(dist, 1.0 - dist)
                seg_t = max(seg_t, max(0.0, 0.65 - dist * 2.2))
            color = _hex_lerp(COLORS["surface"], self._accent, seg_t)
            self._round_rect(x, 2, x + seg_w, h - 2, 3, fill=color, outline="")

    def _round_rect(self, x0, y0, x1, y1, r, **kw):
        points = [
            x0 + r, y0, x1 - r, y0, x1, y0, x1, y0 + r,
            x1, y1 - r, x1, y1, x1 - r, y1, x0 + r, y1,
            x0, y1, x0, y1 - r, x0, y0 + r, x0, y0,
        ]
        return self.create_polygon(points, smooth=True, **kw)


# --------------------------------------------------------------------------- #
# Admin gate
# --------------------------------------------------------------------------- #

class AdminGateFrame(tk.Frame):
    def __init__(self, root, on_continue_limited):
        super().__init__(root, bg=COLORS["bg"])
        self.root = root
        self.on_continue_limited = on_continue_limited
        self.pack(fill="both", expand=True)
        wrapper = tk.Frame(self, bg=COLORS["bg"])
        wrapper.place(relx=0.5, rely=0.5, anchor="center")
        tk.Label(wrapper, text="🛡️", font=("Segoe UI Emoji", 40), bg=COLORS["bg"],
                 fg=COLORS["accent_yellow"]).pack(pady=(0, 10))
        tk.Label(wrapper, text="Administrator Privileges Required", font=(F, 15, "bold"),
                 bg=COLORS["bg"], fg=COLORS["text"]).pack()
        tk.Label(
            wrapper,
            text=("Most cleaning works without admin, but repairs and tweaks need it.\n"
                  "Click below to restart as Administrator (UAC will appear)."),
            font=(F, 10), bg=COLORS["bg"], fg=COLORS["subtext"], justify="center",
        ).pack(pady=(8, 18))
        btn_row = tk.Frame(wrapper, bg=COLORS["bg"])
        btn_row.pack()
        AnimatedButton(
            btn_row, text="Restart as Administrator", command=self._restart_elevated,
            bg=COLORS["accent_green"], fg=COLORS["black"], font=(F, 10, "bold"),
        ).pack(side="left", padx=6)
        AnimatedButton(
            btn_row, text="Continue Without Admin", command=self._continue_limited,
            bg=COLORS["surface"], fg=COLORS["text"], font=(F, 10),
        ).pack(side="left", padx=6)

    def _restart_elevated(self):
        if relaunch_as_admin():
            self._set_elevate_buttons_state("disabled")
            self._wait_status = tk.Label(self, text="Waiting for elevation — click Yes on the UAC prompt, then wait...", font=(F, 9),
                                         bg=COLORS["bg"], fg=COLORS["accent_blue"])
            self._wait_status.pack(pady=(10, 0))

            def _wait_thread():
                from app.elevation import wait_for_elevated_process
                success = wait_for_elevated_process(timeout=60.0)

                def _on_done():
                    if success:
                        try:
                            self.root.destroy()
                        except Exception:
                            pass
                        sys.exit(0)
                    else:
                        try:
                            if hasattr(self, "_wait_status") and self._wait_status.winfo_exists():
                                self._wait_status.destroy()
                        except Exception:
                            pass
                        self._set_elevate_buttons_state("normal")
                        # Don't auto-open limited mode here: the elevated
                        # window is often already running (old bug created
                        # two main windows). Let the user decide.
                        try:
                            messagebox.showwarning("Elevation Cancelled",
                                                   "Administrator elevation was cancelled or timed out.\n\n"
                                                   "If an elevated window opened, use that one and close this.\n"
                                                   "Otherwise click 'Continue Without Admin' to proceed in limited mode.")
                        except Exception:
                            pass

                try:
                    self.root.after(0, _on_done)
                except Exception:
                    pass

            threading.Thread(target=_wait_thread, daemon=True).start()
        else:
            messagebox.showerror("Elevation Failed",
                                 "Could not request administrator rights. You can still continue in limited mode.")

    def _set_elevate_buttons_state(self, state: str):
        try:
            for wrapper in self.winfo_children():
                for child in wrapper.winfo_children():
                    if isinstance(child, tk.Frame):
                        for btn in child.winfo_children():
                            if isinstance(btn, AnimatedButton):
                                btn.set_enabled(state == "normal")
        except Exception:
            pass

    def _continue_limited(self):
        self.destroy()
        self.on_continue_limited()


def _round_pts(x0, y0, x1, y1, r):
    return [
        x0 + r, y0, x1 - r, y0, x1, y0, x1, y0 + r,
        x1, y1 - r, x1, y1, x1 - r, y1, x0 + r, y1,
        x0, y1, x0, y1 - r, x0, y0 + r, x0, y0,
    ]


class ScrollableRoundedPanel(tk.Canvas):
    """Rounded-corner content panel with a custom-drawn scrollbar.

    Why custom instead of tk.Scrollbar (user feedback): native scrollbars
    can't be recolored on Windows — they'd stay light-gray next to the
    dark theme. This panel draws its own track + rounded thumb as canvas
    items in theme colors, floating over the content edge (appearing or
    hiding never reflows the grid).

    Other user-feedback fixes baked in:
      * the scrollbar only hides when ALL content fits — it can no longer
        vanish at 'scrolled to bottom' (old bug: auto-hide tested
        last >= 1.0, which is also true at full scroll-down)
      * cells are created ONCE by the caller and only re-gridded — resize
        never rebuilds widgets, so there's no laggy toggle redraw and no
        state loss
      * the panel never demands its content's height — a Canvas asks for
        almost nothing, so the Run button can never be pushed out of view
    """

    RADIUS = 16
    INSET_X = 12
    INSET_Y = 8
    SB_W = 16          # reserved right strip for the floating scrollbar

    def __init__(self, parent, **kw):
        page_bg = parent["bg"] if isinstance(parent, (tk.Frame, tk.Canvas)) else COLORS["bg"]
        super().__init__(parent, bg=page_bg, highlightthickness=0, bd=0, height=10, **kw)
        self._fill = COLORS["bg_alt"]
        self._offset = 0
        self._content_h = 0
        self._view_h = 0
        self._drag = None
        # F4 coalesced-scroll state: bursty input (wheel deltas, drag
        # motions) records intent here and ONE scheduled flush applies it
        # per ~16 ms turn instead of _apply-ing per event.
        self._scroll_flush_id = None
        self._pending_wheel = 0
        self._pending_drag_y = None
        self._drag_snapshot = None   # press-time geometry kept for the release flush
        self.resize_decide_cb = None     # set by TaskTab (column decision)
        self._resize_after = None
        # persistent scrollbar items (smooth-scroll fix): created once in
        # _ensure_sb_items, only moved/recolor per tick — declared here so
        # every method can safely getattr before first paint.
        self._sb_track = None
        self._sb_thumb = None
        self.inner = tk.Frame(self, bg=self._fill)
        self._win = self.create_window(0, 0, window=self.inner, anchor="nw")
        self.bind("<Configure>", self._on_configure)
        self.bind("<ButtonPress-1>", self._on_press)
        self.bind("<B1-Motion>", self._on_motion)
        self.bind("<ButtonRelease-1>", self._on_release)

    # ---- geometry ---------------------------------------------------- #

    def view_height(self):
        return max(1, self.winfo_height() - self.INSET_Y * 2)

    def remap_children(self):
        """Force the canvas-window child (self.inner) to (re)map.

        Tk quirk (reproduced minimal, 2026-09-06): a frame embedded via
        create_window() does NOT remap when an ANCESTOR of the canvas is
        pack_forget()ten and re-pack()ed — the canvas maps again but the
        embedded window stays unmapped forever (winfo_ismapped()==0), so
        the body cache's re-shown grids rendered BLANK and every filter
        check on their rows saw nothing. Re-asserting the item's
        geometry (coords + itemconfigure) forces Tk through the geometry
        pass that remaps the embedded frame. Called by _build_body on
        every cache-hit re-show; cheap (two canvas ops, no rebuild)."""
        try:
            w = self.winfo_width()
            self.itemconfigure(self._win, width=max(10, w - self.INSET_X - self.SB_W - 2))
            self.coords(self._win, self.INSET_X, self.INSET_Y)
        except Exception:
            pass

    @property
    def scroll_enabled(self):
        return self._content_h > self._view_h

    def _on_configure(self, _e):
        self._redraw_bg()
        w = self.winfo_width()
        self.itemconfigure(self._win, width=max(10, w - self.INSET_X - self.SB_W - 2))
        self.coords(self._win, self.INSET_X, self.INSET_Y)
        self.refresh_scroll()
        # debounce the column re-decision on resize
        if self.resize_decide_cb is not None:
            if self._resize_after is not None:
                try:
                    self.after_cancel(self._resize_after)
                except Exception:
                    pass
            self._resize_after = self.after(180, self._fire_resize_cb)

    def _fire_resize_cb(self):
        self._resize_after = None
        if self.resize_decide_cb is not None and self.winfo_exists():
            self.resize_decide_cb()

    def _redraw_bg(self):
        self.delete("bg")
        w, h = self.winfo_width(), self.winfo_height()
        if w > 6 and h > 6:
            self.create_polygon(_round_pts(0, 0, w, h, self.RADIUS), smooth=True,
                                fill=self._fill, outline="", tags="bg")
            self.tag_lower("bg")

    # ---- scroll ------------------------------------------------------ #

    def refresh_scroll(self, flush=True):
        """Re-measure the content and settle the scrollbar.

        flush=False skips the inner.update_idletasks() geometry pass. That
        flush is what makes the full refresh expensive on a MAPPED canvas
        (it forces every pending idle redraw of the just-grown content —
        measured ~90-220 ms per call while catalog slices build into a
        visible page, 2026-09 bench). Requested sizes recompute on demand
        (winfo_reqheight walks the geometry-request chain without
        allocating or painting), so flush=False is the right per-slice
        tick while content streams in; flush=True stays for settle points
        (catalog finish, searches, collapses, _catalog_finish)."""
        if flush:
            self.inner.update_idletasks()
        self._content_h = self.inner.winfo_reqheight()
        self._view_h = self.view_height()
        if self._content_h <= self._view_h:
            self._offset = 0
        self._apply()

    def _max_offset(self):
        return max(0, self._content_h - self._view_h)

    def _apply(self):
        max_off = self._max_offset()
        self._offset = max(0, min(self._offset, max_off))
        self.coords(self._win, self.INSET_X, self.INSET_Y - self._offset)
        # Scrollbar performance fix: items are created ONCE and only
        # moved/recolor via coords/itemconfigure. The old version did
        # delete("sb") + full recreate on EVERY scroll tick — each
        # recreate is a fresh smooth-polygon (conic spline) rasterization,
        # and the delete/recreate gap leaves a blank frame between them.
        # On the Install tab (the heaviest page) that blank-frame churn
        # per wheel event was the visible stutter/"can't draw fast
        # enough" feel. Persistent items let Tk repaint only the small
        # damaged region (same technique the tab-switcher thumb uses).
        self._ensure_sb_items()
        if not self.scroll_enabled:
            self._hide_sb()
            return
        w, h = self.winfo_width(), self.winfo_height()
        y0, y1 = self.INSET_Y, h - self.INSET_Y
        cx = w - 10
        frac = (self._view_h / self._content_h) if self._content_h else 1.0
        thumb_h = max(30, int(frac * (y1 - y0)))
        pos = (self._offset / max_off) if max_off else 0.0
        ty = y0 + pos * ((y1 - y0) - thumb_h)
        track_c = _hex_lerp(self._fill, "#FFFFFF", 0.06)
        self.itemconfigure(self._sb_track, fill=track_c)
        self.coords(self._sb_track, cx, y0, cx, y1)
        self.itemconfigure(self._sb_thumb, state="normal")
        self.coords(self._sb_thumb, *_round_pts(cx - 4, ty, cx + 4, ty + thumb_h, 4))

    def _ensure_sb_items(self):
        """Create the persistent scrollbar items on first use (or recreate
        if the canvas was cleared). find_withtag is the liveness check —
        an item id of a deleted item is falsy-safe here, but an int id
        that was valid at creation can be stale after a canvas-level
        delete, so verify it still resolves."""
        track_ok = False
        thumb_ok = False
        try:
            if self._sb_track is not None:
                track_ok = bool(self.find_withtag(self._sb_track))
            if self._sb_thumb is not None:
                thumb_ok = bool(self.find_withtag(self._sb_thumb))
        except Exception:
            pass
        if not track_ok:
            self._sb_track = self.create_line(0, 0, 0, 0, fill=self._fill, width=4,
                                              capstyle="round", tags="sb")
        if not thumb_ok:
            self._sb_thumb = self.create_polygon(_round_pts(0, 0, 0, 0, 4), smooth=True,
                                                  fill=COLORS["surface_hover"],
                                                  outline="", state="hidden", tags="sb")
        # keep them above the content window & bg at all times
        self.tag_raise(self._sb_track)
        self.tag_raise(self._sb_thumb)

    def _hide_sb(self):
        try:
            self.itemconfigure(self._sb_thumb, state="hidden")
            self.itemconfigure(self._sb_track, state="hidden")
        except Exception:
            pass

    def on_wheel(self, delta):
        if not self.scroll_enabled:
            return
        # Smooth-scroll fix: the old int(delta/120)*60 truncation turned
        # smooth/free-spin wheels and precision touchpads (small ±delta
        # events) into zero-pixel scrolls followed by 60px jumps — the
        # classic "can't keep up" feel. Scale proportionally instead: a
        # classic ±120 notch moves a sane 60px; a high-resolution wheel's
        # small deltas move small precise steps that sum smoothly.
        # (max(1,…) keeps even a tiny ±1 delta scrolling one pixel.)
        step = max(1, int(abs(delta) * 0.5))
        # F4: a wheel burst arriving inside one ~16 ms turn accumulates
        # here and is applied by a single flush — same total distance per
        # notch (60px classic, proportional for smooth wheels), but one
        # _apply per turn instead of one per event (unbounded rate vs
        # paint).
        self._pending_wheel += step if delta > 0 else -step
        self._schedule_scroll_flush()

    def _schedule_scroll_flush(self):
        """F4: coalesce bursty scroll input into at most one _apply per
        ~16 ms event-loop turn. after(16) (not after_idle) is used because
        Tk runs idle callbacks between every pair of queued events — under
        a motion flood after_idle would flush once PER event and coalesce
        nothing. The pending flag guarantees a single scheduled flush no
        matter how many events arrive before it fires."""
        if self._scroll_flush_id is None:
            try:
                self._scroll_flush_id = self.after(16, self._flush_scroll)
            except Exception:
                pass  # panel destroyed mid-event — nothing left to flush

    def _flush_scroll(self):
        """Apply whatever scroll intent accumulated since the last flush
        (F4). Drag semantics are preserved: the stored drag position is
        absolute (drag overrides), wheel deltas add to it (wheel adds),
        and _apply clamps to the content range. Also called from
        ButtonRelease so the final drag position lands immediately rather
        than waiting out the coalescing timer."""
        self._scroll_flush_id = None
        changed = False
        try:
            if self._pending_drag_y is not None:
                y = self._pending_drag_y
                self._pending_drag_y = None
                # during a live drag use its press-time tuple; after
                # ButtonRelease fall back to the snapshot taken there
                drag = self._drag if self._drag is not None else self._drag_snapshot
                if drag is not None:
                    self._drag_snapshot = None
                    self._offset = self._thumb_offset_for(drag, y)
                    changed = True
            if self._pending_wheel:
                self._offset -= self._pending_wheel
                self._pending_wheel = 0
                changed = True
            if changed:
                self._apply()
        except Exception:
            pass  # widget destroyed mid-flush (shutdown) — nothing to paint

    # ---- thumb drag / track click ------------------------------------ #

    def _sb_zone_x(self):
        return self.winfo_width() - self.SB_W - 8

    def _thumb_geom(self):
        h = self.winfo_height()
        y0, y1 = self.INSET_Y, h - self.INSET_Y
        frac = (self._view_h / self._content_h) if self._content_h else 1.0
        thumb_h = max(30, int(frac * (y1 - y0)))
        max_off = self._max_offset()
        pos = (self._offset / max_off) if max_off else 0.0
        ty = y0 + pos * ((y1 - y0) - thumb_h)
        return ty, thumb_h, y0, y1, max_off

    def _thumb_offset_for(self, drag, y):
        """Scroll offset that puts the thumb under pointer y, given a
        press-time drag tuple (grab_dy, thumb_h, y0, y1, max_off). Shared
        by the track-click jump and the coalesced drag flush so both paths
        use identical math."""
        grab_dy, thumb_h, y0, y1, max_off = drag
        ty = max(y0, min(y1 - thumb_h, y - grab_dy))
        span = (y1 - y0) - thumb_h
        return 0 if span <= 0 else ((ty - y0) / span) * max_off

    def _on_press(self, e):
        if not self.scroll_enabled or e.x < self._sb_zone_x():
            return
        self._drag_snapshot = None   # fresh drag: forget any old release snapshot
        ty, thumb_h, y0, y1, max_off = self._thumb_geom()
        if ty - 6 <= e.y <= ty + thumb_h + 6:
            self._drag = (e.y - ty, thumb_h, y0, y1, max_off)
        else:
            # track click: jump so the thumb centers under the pointer
            self._drag = (thumb_h / 2, thumb_h, y0, y1, max_off)
            self._set_thumb_centered(e.y)
        self.config(cursor="hand2")

    def _set_thumb_centered(self, y):
        self._offset = self._thumb_offset_for(self._drag, y)
        self._apply()

    def _on_motion(self, e):
        # F4: record the latest drag position and let the coalescing flush
        # apply it — a 1000 Hz mouse report flood no longer forces one
        # _apply per report (unbounded apply rate vs the display's paint).
        if self._drag is not None:
            self._pending_drag_y = e.y
            self._schedule_scroll_flush()

    def _on_release(self, _e):
        if self._drag is not None and self._pending_drag_y is not None:
            # keep the press-time geometry for one final flush so the last
            # drag position still lands after the button comes up
            self._drag_snapshot = self._drag
        self._drag = None
        self.config(cursor="")
        # F4: land any still-pending position/delta immediately — the
        # mouse is up, don't wait out the coalescing timer
        if self._pending_drag_y is not None or self._pending_wheel:
            self._flush_scroll()


# --------------------------------------------------------------------------- #
# Per-tab page
# --------------------------------------------------------------------------- #

class TaskTab(tk.Frame):
    """One of the three tab pages.

    Modes (Phase 2 #13):
      * preset mode — curated preset cards + Custom card; selecting a
        preset shows a plain-language summary; no checkboxes at all.
      * custom mode — full animated toggle grid (all tasks for the tab).
      * undo mode (Tweak only) — all-off grid of reversible tweaks; toggles
        choose what to revert using real snapshotted prior values.
    """

    PRESET_BLURBS = {
        "Quick Clean": "Everyday junk — fast and safe.",
        "Deep Clean": "Deeper junk, browsers, old files.",
        "Quick Repair": "Fast checks for common problems.",
        "Deep Repair": "Full fix stack — 30+ minutes.",
        "Minimal": "Safe speed basics, no risk.",
        "Recommended": "Best all-round setup + privacy.",
        "Game Session": "Max power for one play session.",
    }

    def __init__(self, parent, app, tab_name):
        super().__init__(parent, bg=COLORS["bg"])
        self.app = app
        self.tab_name = tab_name
        self.tasks = TABS[tab_name]
        self.task_by_key = {t.key: t for t in self.tasks}
        self.presets = PRESETS[tab_name]
        self.mode = "preset"          # preset | custom | undo
        self.vars = {}
        self._selected_preset = None
        self._anim_jobs = []          # active after() ids owned by this page
        # grid engine state: cells built once, only re-gridded afterwards
        self._cells = None            # list[(widget, task)] in stable order
        self._cell_cols = None
        self._regrid_after = None
        # body cache: built-once bodies per mode/preset (no laggy redraws)
        self._body_cache = {}
        self._body_vars = {}
        # F3: undo rows of the last-built undo grid, for in-place badge
        # repaints (each row stores its badge handle on the widget)
        self._undo_rows = []
        # F7: _set_all() sets this while it bulk-writes every row var, so
        # the per-var traces skip the run-count refresh (one refresh after
        # the loop is enough); single toggles are never suppressed.
        self._suspend_count_refresh = False

        self._build()

    # ---------------- structure ---------------- #

    def _build(self):
        # Preset card area (top) — compact padding reclaims vertical space
        # so Clean/Repair Custom grids fit WITHOUT scrolling at default size
        self.preset_area = tk.Frame(self, bg=COLORS["bg"])
        self.preset_area.pack(fill="x", padx=26, pady=(14, 4))

        # Summary / toggle-grid area (middle, swaps by mode)
        self.body_area = tk.Frame(self, bg=COLORS["bg"])

        # Run row (bottom) — packed FIRST (side=bottom) so it can never be
        # squeezed out of view (user-reported clipping: a tall content
        # frame used to push the Run button off-screen).
        self.run_row = tk.Frame(self, bg=COLORS["bg"])
        self.run_row.pack(side="bottom", fill="x", padx=26, pady=(4, 10))
        self.body_area.pack(side="top", fill="both", expand=True, padx=26, pady=4)

        self._build_preset_cards()
        self._build_body()
        self._build_run_row()
        # F2: preset cards and the Run button are built HERE once and only
        # recolored/restyled on later mode switches (_highlight_cards /
        # _update_run_row) — never destroyed and recreated per switch.

    def _build_preset_cards(self):
        for w in self.preset_area.winfo_children():
            w.destroy()
        accent = TAB_ACCENTS[self.tab_name]

        # (Phase 2 #15) Tweak tab gets the red Undo card on the end.
        # Health card (user-approved feature 2, 2026-09): Tweak also gets a
        # 6th card — completing the 3x2 grid (Minimal/Recommended/Game
        # Session over Custom/Undo/Health) so the second row is symmetric
        # with the first at every window width.
        cards = list(self.presets.keys()) + ["Custom"] + (["Undo Tweaks", "Tweak Health"] if self.tab_name == "Tweak" else [])
        self.preset_cards = {}
        # WRAP AT 3 PER ROW (user-reported clipping: a 5th card squeezed the
        # whole row). 3 across keeps every card readable at min window size;
        # extra cards flow to a second row instead of shrinking the rest.
        # Clean/Repair have 3 cards (one row, unchanged); Tweak has 6 (3+2 -> 3+3).
        PER_ROW = 3
        for col, name in enumerate(cards):
            if name == "Custom":
                blurb, icon, color, fg = "Pick exactly what runs.", "⚙", COLORS["surface"], COLORS["text"]
                command = lambda: self._enter_custom()
            elif name == "Undo Tweaks":
                blurb, icon, color, fg = "Put back what the tweaks changed.", "↩", COLORS["surface"], COLORS["text"]
                command = lambda: self._enter_undo()
            elif name == "Tweak Health":
                blurb = "See what's still applied — fix what Windows undid."
                icon = "🩺"
                color, fg = COLORS["surface"], COLORS["text"]
                command = lambda: self._enter_health()
            else:
                blurb = self.PRESET_BLURBS.get(name, "")
                icons = {"Quick Clean": "🧹", "Deep Clean": "🧼", "Quick Repair": "🩺",
                         "Deep Repair": "🔧", "Minimal": "🚀", "Recommended": "⭐",
                         "Game Session": "🎮"}
                icon = icons.get(name, "•")
                color, fg = COLORS["surface"], COLORS["text"]
                command = (lambda n=name: self._select_preset(n))
            card = self._make_card(self.preset_area, name, blurb, icon, color, fg, command, accent)
            row, column = divmod(col, PER_ROW)
            card.grid(row=row, column=column, sticky="nsew", padx=5, pady=4)
            self.preset_cards[name] = card
        for c in range(PER_ROW):
            self.preset_area.grid_columnconfigure(c, weight=1, uniform="cards")

    def _make_card(self, parent, title, blurb, icon, bg, fg, command, accent):
        outer = tk.Frame(parent, bg=COLORS["hairline"], bd=0)
        inner = tk.Frame(outer, bg=bg)
        inner.pack(fill="both", expand=True, padx=1, pady=1)
        head = tk.Frame(inner, bg=bg)
        # Title-only cards (approved layout change): the one-line blurb is no
        # longer drawn on the face — shorter rows leave the Custom/Undo
        # panels below more room. The blurb text still rides the tooltip.
        head.pack(fill="x", padx=12, pady=(6, 6))
        icon_lbl = tk.Label(head, text=icon, font=("Segoe UI Emoji", 16), bg=bg, fg=accent)
        icon_lbl.pack(side="left")
        title_lbl = tk.Label(head, text=title, font=(F, 12, "bold"), bg=bg, fg=fg)
        title_lbl.pack(side="left", padx=8)
        if blurb:
            # one Tooltip per card part, same text: hovering anywhere on the
            # card shows the description (child crossings fire <Leave>, so a
            # single container-bound tip would die at the first label edge)
            for _w in (outer, inner, head, icon_lbl, title_lbl):
                Tooltip(_w, blurb)
        # click + hover (add="+": card parts may carry Tooltip <Enter>/<Leave>
        # hooks by now — plain bind() would wipe them, see _build_toggle_row)
        for w in (outer, inner, head):
            w.bind("<Button-1>", lambda e, c=command: c())
            w.bind("<Enter>", lambda e, i=inner, o=outer, a=accent: self._card_hover(i, o, a, True), add="+")
            w.bind("<Leave>", lambda e, i=inner, o=outer, a=accent: self._card_hover(i, o, a, False), add="+")
        for child in inner.winfo_children():
            for c in child.winfo_children() or [child]:
                try:
                    c.bind("<Button-1>", lambda e, c2=command: c2())
                    c.bind("<Enter>", lambda e, i=inner, o=outer, a=accent: self._card_hover(i, o, a, True), add="+")
                    c.bind("<Leave>", lambda e, i=inner, o=outer, a=accent: self._card_hover(i, o, a, False), add="+")
                except Exception:
                    pass
        return outer

    def _card_hover(self, inner, outer, accent, on):
        # subtle: hairline border brightens toward accent on hover
        outer.config(bg=accent if on else COLORS["hairline"])

    def _build_body(self):
        """Body cache (user feedback: mode switches redrew ~50 toggle
        widgets and looked laggy). Bodies are built once per mode and
        cached; switching modes just shows/hides — instant, and Custom
        toggle choices survive a trip to a preset and back.

        self.vars must follow the SHOWN body: each cached body remembers
        its own var dict (custom and undo bodies have different var sets),
        and showing a cached body restores its dict — otherwise Run would
        read the previous mode's vars (bug caught by the layout test).

        F8 (user bug report 2026-09-06: 'the Custom filter search box does
        nothing'): the cache-hit path restored ONLY vars. _cell_blocks /
        _cells still pointed at whatever grid was built LAST (the preset
        summary, or another mode's grid), so the filter's grid_remove()s
        landed on a HIDDEN grid while the visible one never changed —
        'it still shows all the toggles'. The cache now stores the full
        (vars, cells, cell_blocks) tuple and every cache hit restores all
        three, so _cell_blocks always describes the SHOWN body."""
        # hide all cached bodies
        for w in self.body_area.winfo_children():
            w.pack_forget()
        cache_key = self.mode if self.mode != "preset" else f"preset:{self._selected_preset}"
        if cache_key in self._body_cache:
            self._body_cache[cache_key].pack(fill="both", expand=True)
            # F8b: pack_forget/pack of a body whose panel embeds content
            # via create_window() leaves the embedded frame UNMAPPED (Tk
            # quirk — see ScrollableRoundedPanel.remap_children). The
            # re-assert must run AFTER the repack's geometry settles
            # (itemconfigure issued pre-mapping is ignored by Tk —
            # reproduced minimal, 2026-09-06), so it rides the idle
            # queue, one event-loop turn later.
            _panel_fix = None
            for _w in self._body_cache[cache_key].winfo_children():
                if isinstance(_w, ScrollableRoundedPanel):
                    _panel_fix = _w
                    break
            if _panel_fix is not None:
                def _remap(_p=_panel_fix):
                    try:
                        if _p.winfo_exists():
                            _p.remap_children()
                    except Exception:
                        pass
                try:
                    _panel_fix.after_idle(_remap)
                except Exception:
                    pass
            if cache_key in self._body_vars:
                cached = self._body_vars[cache_key]
                if isinstance(cached, tuple) and len(cached) == 3:
                    self.vars, self._cells, self._cell_blocks = cached
                else:
                    self.vars = cached   # legacy shape — defensive
            return
        wrap = tk.Frame(self.body_area, bg=COLORS["bg"])
        self._body_cache[cache_key] = wrap
        wrap.pack(fill="both", expand=True)
        if self.mode == "preset":
            self._build_summary_body(wrap)
        elif self.mode == "custom":
            self._build_toggle_grid(wrap, mode="run")
        elif self.mode == "health":
            self._build_health_body(wrap)
        else:
            self._build_toggle_grid(wrap, mode="undo")
        # builders refresh self.vars — remember this body's FULL grid
        # state (F8) so a later cache hit can restore all of it
        if self.mode != "preset":
            self._body_vars[cache_key] = (self.vars, self._cells, self._cell_blocks)

    def _clear_body_cache(self):
        """Drop cached bodies (called when tab's tasks/state change — undo
        badges depend on live tweak state). MUST run on the Tk thread: it
        destroys widgets (worker-thread Tcl calls are unsafe) and the old
        dict-drop-only version leaked every cached wrap as a hidden child
        of body_area on each run. Rebuilds the visible mode immediately so
        the panel never shows blank and badges reflect post-run state."""
        for wrap in list(self._body_cache.values()):
            try:
                if wrap is not None and wrap.winfo_exists():
                    wrap.destroy()
            except Exception:
                pass
        self._body_cache = {}
        self._body_vars = {}
        try:
            self._build_body()
        except Exception:
            pass
        # health mode: its run row carries a live applied-count label
        # ('Re-apply All (N)'), so it must rebuild with the body — other
        # modes' run rows are count-agnostic until the next toggle
        if getattr(self, "mode", "") == "health":
            try:
                self._update_run_row()
            except Exception:
                pass

    def _prewarm_body(self, mode: str) -> bool:
        """F6(b): build one body (custom/undo) into the cache WITHOUT
        showing it, so the first real click on Custom/Undo is a cache hit
        (~2 ms) instead of a 100-450 ms first build. Runs once from
        Application's idle pre-warm chain after the window is up; a no-op
        when that body already exists (the user got there first and the
        normal lazy build owns it).

        The grid builder mutates page state as it goes (self.vars,
        _cells, _cell_blocks), so that state is snapshotted and restored —
        only the cache entry (hidden widgets + their var dict) is kept.
        _undo_rows is deliberately NOT restored: an undo pre-warm only
        runs while no undo body exists, so its rows are exactly the rows
        the next _enter_undo shows and badge-refreshes in place (F3)."""
        if mode not in ("custom", "undo"):
            return False
        if mode in self._body_cache:
            return False
        saved = (self.vars,
                 getattr(self, "_cells", None),
                 getattr(self, "_cell_blocks", None))
        try:
            wrap = tk.Frame(self.body_area, bg=COLORS["bg"])
            self._body_cache[mode] = wrap
            self._build_toggle_grid(wrap, mode="run" if mode == "custom" else "undo")
            # F8: store the FULL grid state — vars alone left _cell_blocks
            # stale on the cache-hit path, which is exactly why the Custom
            # filter search box appeared dead (it filtered a hidden grid).
            self._body_vars[mode] = (self.vars, self._cells, self._cell_blocks)
            # the hidden wrap measures ~1px wide, so labels were wrapped to
            # the narrow fallback — the grid's own debounced <Configure>
            # refit (_fit_toggle_labels) re-wraps them to the real column
            # width the moment this body is first shown.
            wrap.pack_forget()  # never packed — stays hidden until shown
            self.vars, self._cells, self._cell_blocks = saved
            return True
        except Exception:
            # a failed pre-warm must never crash the idle chain — drop the
            # half-built cache entry; the lazy first-click build takes over
            self._body_cache.pop(mode, None)
            self._body_vars.pop(mode, None)
            self.vars, self._cells, self._cell_blocks = saved
            return False

    def _build_summary_body(self, wrap):
        """Read-only plain-language summary of the selected preset (no
        checkboxes — punch-list #13 'no checkboxes for curated tiers')."""
        if self._selected_preset is None:
            tk.Label(wrap, text=f"Pick a {self.tab_name} preset above to see what it does.",
                     font=(F, 11), bg=COLORS["bg"], fg=COLORS["subtext"]).pack(expand=True)
            return
        keys = self.presets[self._selected_preset]
        head = tk.Frame(wrap, bg=COLORS["bg"])
        head.pack(fill="x", pady=(2, 8))
        accent = TAB_ACCENTS[self.tab_name]
        tk.Label(head, text=f"{self._selected_preset} — {len(keys)} tasks",
                 font=(F, 13, "bold"), bg=COLORS["bg"], fg=accent).pack(anchor="w")
        tk.Label(head, text=self.PRESET_BLURBS.get(self._selected_preset, ""),
                 font=(F, 10), bg=COLORS["bg"], fg=COLORS["subtext"]).pack(anchor="w")

        panel = ScrollableRoundedPanel(wrap)
        panel.pack(fill="both", expand=True)

        tasks = [self.task_by_key[k] for k in keys]
        cells = []
        for t in tasks:
            cell = tk.Frame(panel.inner, bg=COLORS["bg_alt"])
            dot = tk.Canvas(cell, width=8, height=8, bg=COLORS["bg_alt"], highlightthickness=0)
            dot.create_oval(1, 1, 7, 7, fill=TAB_ACCENTS[self.tab_name], outline="")
            dot.pack(side="left", padx=(0, 6), anchor="n")
            lbl = tk.Label(cell, text=t.label, font=(F, 9), bg=COLORS["bg_alt"],
                           fg=COLORS["text"], anchor="w", justify="left", wraplength=215)
            lbl.pack(side="left", anchor="w")
            Tooltip(lbl, t.description)
            if getattr(t, "risk", "SAFE") == "REBOOT REQUIRED":
                r = tk.Label(cell, text=" 🔄", font=("Segoe UI Emoji", 9), bg=COLORS["bg_alt"],
                             fg=COLORS["text"])
                r.pack(side="left")
                Tooltip(r, "Reboot Required — Windows needs a restart after this one")
            cells.append((cell, t))
        self._cells = cells
        self._cell_blocks = [(None, cells)]   # summary has no group headers
        panel.resize_decide_cb = lambda: self._regrid(panel)
        self._regrid(panel, force=True)

    def _regrid(self, panel, force=False):
        """Re-grid existing cells (built once) into the best column count
        for the current panel size. No widget rebuilds -> no toggle redraw
        flicker, no state loss. Column choice tries to make content fit
        WITHOUT scrolling: it walks column counts from 3 up and picks the
        first whose resulting content height fits the view; only if none
        fit does scrolling engage.

        Layout unit is self._cell_blocks: [(header_or_None, [(cell, task)])].
        Headers span the full width; each block's cells flow below its
        header. (The preset summary sets a single headerless block.)"""
        blocks = getattr(self, "_cell_blocks", None)
        if not blocks:
            return
        # list-mode rows (Custom/Undo grids) are packed full-width, not
        # gridded — there is nothing to re-fit on resize.
        try:
            _probe = blocks[0][0] or (blocks[0][1][0][0] if blocks[0][1] else None)
            if _probe is not None and _probe.winfo_manager() == "pack":
                return
        except Exception:
            pass
        cells = [c for _h, bc in blocks for c in bc]
        if not cells:
            return
        panel.update_idletasks()
        inner_w = max(10, panel.winfo_width() - panel.INSET_X - panel.SB_W - 2)
        view_h = panel.view_height()
        # per-cell height measured from the first cell (stable once mapped)
        try:
            cell_h = max(20, cells[0][0].winfo_reqheight() + 8)  # +pady
        except Exception:
            cell_h = 44
        n_headers = sum(1 for h, _bc in blocks if h is not None)

        best = None
        for cols in (3, 4, 5):
            col_w = inner_w // cols
            if col_w < 170:
                continue  # cells would be unreadably narrow
            data_rows = sum((len(bc) + cols - 1) // cols for _h, bc in blocks)
            content_h = data_rows * cell_h + n_headers * 30 + panel.INSET_Y * 2
            if content_h <= view_h:
                best = cols
                break
        if best is None:
            # nothing fits height-wise: use the widest count that keeps
            # cells >= 170px so scrolling distance is minimized
            best = max(3, min(5, inner_w // 170))
            if best < 3:
                best = 3
        cols = best
        r = 0
        for hdr, bcells in blocks:
            if hdr is not None:
                hdr.grid(row=r, column=0, columnspan=cols, sticky="ew",
                         padx=6, pady=(8, 2))
                r += 1
            for i, (cell, _t) in enumerate(bcells):
                rr, cc = divmod(i, cols)
                cell.grid(row=r + rr, column=cc, sticky="nw", padx=6, pady=2)
            r += (len(bcells) + cols - 1) // cols
        for c in range(cols):
            panel.inner.grid_columnconfigure(c, weight=1, uniform="grid")
        for c in range(cols, 6):
            panel.inner.grid_columnconfigure(c, weight=0)
        self._cell_cols = cols
        panel.refresh_scroll()

    def _build_toggle_grid(self, wrap, mode="run"):
        """The only place toggles appear (Phase 2 #13/#14). Custom mode =
        pick what runs; Undo mode = pick what reverts (all default off)."""
        accent = TAB_ACCENTS[self.tab_name]

        if mode == "undo":
            banner = tk.Frame(wrap, bg="#3A2226")
            tk.Label(banner, text="↩  Undo — switches pick which tweaks to put back to how they were.",
                     font=(F, 10, "bold"), bg="#3A2226", fg=COLORS["accent_red"]).pack(anchor="w", padx=10, pady=8)
            banner.pack(fill="x", pady=(0, 6))

        topbar = tk.Frame(wrap, bg=COLORS["bg"])
        topbar.pack(fill="x", pady=(0, 6))
        AnimatedButton(topbar, "All On", command=lambda: self._set_all(True),
                       bg=COLORS["surface"], fg=COLORS["text"], font=(F, 9), padx=16, pady=6).pack(side="left", padx=3)
        AnimatedButton(topbar, "All Off", command=lambda: self._set_all(False),
                       bg=COLORS["surface"], fg=COLORS["text"], font=(F, 9), padx=16, pady=6).pack(side="left", padx=3)
        if mode == "run":
            hint = "Turn on what you want to run — click a row anywhere"
        else:
            hint = "Turn on what you want to undo"
        tk.Label(topbar, text=hint, font=(F, 9), bg=COLORS["bg"],
                 fg=COLORS["subtext"]).pack(side="left", padx=10)
        # search filter (user request: find one option among dozens fast) —
        # rounded corners, centered in the leftover space (user request)
        #
        # F8 (user bug: the filter 'did nothing'): the trace used to call
        # _apply_toggle_filter which read self._cell_blocks LIVE — shared
        # mutable state pointing at whatever grid was built last. This box's
        # grid is `blocks` (built below in THIS method), so the trace now
        # passes THIS grid's blocks explicitly; the filter can never again
        # operate on a stale/hidden grid while the visible one ignores it.
        _ph = f"Filter {len(self.tasks)} options…"
        _sv = tk.StringVar(value=_ph)
        _mid = tk.Frame(topbar, bg=COLORS["bg"])
        _mid.pack(side="left", expand=True, fill="x")
        _re = RoundedEntry(_mid, textvariable=_sv, width=20, accent=accent)
        _re.pack(anchor="center")
        _se = _re.entry
        _se.bind("<FocusIn>", lambda e: _sv.set("") if _sv.get() == _ph else None)
        # audit fix (UI polish): InstallTab's search restores its placeholder
        # on blur; this one never did — after clicking away, the box stayed
        # permanently empty with no hint of what it was for.
        _se.bind("<FocusOut>", lambda e: _sv.set(_ph) if not _sv.get().strip() else None)

        state = self.app.tweak_state if mode == "undo" else {}

        panel = ScrollableRoundedPanel(wrap)
        panel.pack(fill="both", expand=True)
        self._grid_row = 0   # shared 2-column grid row cursor (grid mode)

        tasks = [t for t in self.tasks if (mode == "run" or t.revert is not None)]
        self.vars = {}
        # Custom grids are grouped by function (tab_presets.CUSTOM_GROUPS).
        # (User request, 2-column layout): each option is a compact single-
        # line settings row in a TWO-COLUMN grid — Install-tab density, so
        # tabs with dozens of options scroll roughly half as far. Details
        # live in the hover tooltip, not inline. The whole row stays
        # clickable — big targets, nothing truncated.
        from app.tab_presets import CUSTOM_GROUPS
        by_key = {t.key: t for t in tasks}
        grouped = CUSTOM_GROUPS.get(self.tab_name, [])
        blocks = []   # (header_or_None, [(row_outer, task), ...])
        cells = []
        # 2-column grid inside the panel; headers span both columns
        inner_grid = panel.inner
        # measure once: with 2 equal columns, each gets ~half the inner
        # width (minus scrollbar strip). Labels wrap to that, not a fixed
        # 560px that would clip in a column.
        inner_w = max(10, panel.winfo_width() - panel.INSET_X - panel.SB_W - 2)
        col_w = max(160, (inner_w - 24) // 2)   # 24 = grid padx total
        for _title, _keys in grouped:
            gtasks = [by_key[k] for k in _keys if k in by_key]
            if not gtasks:
                continue
            hdr = tk.Frame(inner_grid, bg=COLORS["surface"])
            tk.Label(hdr, text=_title, font=(F, 10, "bold"), bg=COLORS["surface"],
                     fg=COLORS["text"]).pack(side="left", padx=(8, 0), pady=4)
            tk.Label(hdr, text=f"{len(gtasks)} options", font=(F, 8),
                     bg=COLORS["surface"], fg=COLORS["subtext"]).pack(side="left", padx=6)
            hdr.grid(row=self._grid_row, column=0, columnspan=2,
                     sticky="ew", padx=6, pady=(10, 2))
            self._grid_row += 1
            grows = []
            for i, t in enumerate(gtasks):
                row, col = divmod(i, 2)
                cell, _task = self._build_toggle_row(inner_grid, t, mode, state, accent, col_w)
                cell.grid(row=self._grid_row + row, column=col,
                          sticky="new", padx=6, pady=2)
                grows.append((cell, t))
            self._grid_row += (len(gtasks) + 1) // 2
            blocks.append((hdr, grows))
            cells.extend([(c, t) for c, t in grows])
        # safety net (should be unreachable — tab_presets validates full
        # coverage): any task missing from the groups still shows up.
        _shown = {t.key for _h, grows in blocks for _c, t in grows}
        _missing = [t for t in tasks if t.key not in _shown]
        if _missing:
            grows = []
            for i, t in enumerate(_missing):
                row, col = divmod(i, 2)
                cell, _task = self._build_toggle_row(inner_grid, t, mode, state, accent, col_w)
                cell.grid(row=self._grid_row + row, column=col,
                          sticky="new", padx=6, pady=2)
                grows.append((cell, t))
            self._grid_row += (len(_missing) + 1) // 2
            blocks.append((None, grows))
            cells.extend([(c, t) for c, t in grows])
        self._cells = cells
        self._cell_blocks = blocks
        # F3: remember the undo grid's rows so later Undo entries can
        # repaint '✓ Active' badges in place without rebuilding the grid
        if mode == "undo":
            self._undo_rows = list(cells)
        for c in (0, 1):
            inner_grid.grid_columnconfigure(c, weight=1, uniform="togglerows")
        # one-line guarantee (user request): col_w at build time is measured
        # before the window is laid out (often 1px), which froze every label
        # at a ~140px wrap and forced 2-line names. Refit label wraps to the
        # REAL column width on every grid resize (debounced) so names stay on
        # one row at any window size/DPI.
        _fit_after = {"id": None}

        def _fit_toggle_labels(*_):
            try:
                if _fit_after["id"] is not None:
                    inner_grid.after_cancel(_fit_after["id"])
            except Exception:
                pass

            def _do():
                _fit_after["id"] = None
                try:
                    w = max(10, panel.winfo_width() - panel.INSET_X - panel.SB_W - 2)
                    cw = max(160, (w - 24) // 2)
                    wrap = max(120, cw - 110)
                    for _h, _grows in blocks:
                        for _cell, _t in _grows:
                            _lbl = getattr(_cell, "_lbl", None)
                            if _lbl is not None and _lbl.winfo_exists():
                                _lbl.config(wraplength=wrap)
                except Exception:
                    pass
                # F6(b): bodies pre-warmed hidden measure ~1px wide, so rows
                # may have wrapped taller than they will at the real column
                # width — after the refit above, settle the scroll metrics
                # on the now-true content height.
                try:
                    panel.refresh_scroll()
                except Exception:
                    pass

            try:
                _fit_after["id"] = inner_grid.after(120, _do)
            except Exception:
                pass

        inner_grid.bind("<Configure>", _fit_toggle_labels)
        _fit_toggle_labels()
        panel.resize_decide_cb = None
        panel.refresh_scroll()

        # F8: wire the search trace LAST, once `blocks` fully describes
        # THIS grid — the closure captures this grid's own rows, so the
        # filter always operates on exactly the grid the user sees,
        # never a stale shared _cell_blocks (the original dead-filter bug).
        _sv.trace_add("write", lambda *_: self._apply_toggle_filter(
            _sv.get().strip(), _ph, mode, blocks=blocks, panel=panel))

    def _build_toggle_row(self, parent, t, mode, state, accent, col_w=380):
        """One compact settings-row cell (2-column grid, Install-tab density):
        borderless box (user request: no outline frame), clickable anywhere (not just the switch), switch +
        single-line label. The full description lives in the hover tooltip
        (user request: inline 3-line descriptions doubled the scrolling).
        Hover never draws an outline or paints the row background (user
        request — a rectangular wash looked square around the pill toggle).
        The highlight lives only on the switch (its own hover brightening)
        and the task name (tinted toward the tab color). Registers its var;
        returns (outer, task)."""
        var = tk.BooleanVar(value=(t.default if mode == "run" else False))
        self.vars[t.key] = var
        try:
            var.trace_add("write", lambda *_: self._refresh_run_count())
        except Exception:
            pass
        outer = tk.Frame(parent, bg=COLORS["bg_alt"], cursor="hand2")
        inner = tk.Frame(outer, bg=COLORS["bg_alt"], cursor="hand2")
        inner.pack(fill="both", expand=True)
        sw = ToggleSwitch(inner, var)
        sw.pack(side="left", anchor="center", padx=(8, 2), pady=6)
        txt = tk.Frame(inner, bg=COLORS["bg_alt"], cursor="hand2")
        txt.pack(side="left", anchor="center", fill="both", expand=True,
                 padx=(0, 8), pady=6)
        # single line: wraplength only guards ultra-long labels — most fit
        # one line at column width (switch 44 + its padx + row padx accounted)
        wrap = max(140, col_w - 44 - 10 - 16 - 12)
        lbl = tk.Label(txt, text=t.label, font=(F, 9, "bold"), bg=COLORS["bg_alt"],
                       fg=COLORS["text"], anchor="w", justify="left",
                       wraplength=wrap, cursor="hand2")
        lbl.pack(side="left", anchor="center")
        Tooltip(lbl, t.description)
        outer._lbl = lbl  # dynamic one-line fit (see _fit_toggle_labels)
        extra = []
        if mode == "run" and getattr(t, "admin_required", False):
            adm = tk.Label(txt, text="🛡️", font=("Segoe UI Emoji", 9), bg=COLORS["bg_alt"],
                           fg=COLORS["text"], cursor="hand2")
            adm.pack(side="left", anchor="center", padx=(6, 0))
            Tooltip(adm, "Admin Required — needs Administrator rights (skipped in limited mode)")
            extra.append(adm)
        if getattr(t, "risk", "SAFE") == "REBOOT REQUIRED":
            r = tk.Label(txt, text="🔄", font=("Segoe UI Emoji", 9), bg=COLORS["bg_alt"],
                         fg=COLORS["text"], cursor="hand2")
            r.pack(side="left", anchor="center", padx=(6, 0))
            Tooltip(r, "Reboot Required — Windows needs a restart after this one")
            extra.append(r)
        # F3: undo rows get a '✓ Active' badge label at build time, PACKED
        # only while the tweak is actually applied (state from the last
        # _build_toggle_grid call). The handle lives on the row so
        # _refresh_undo_badges can pack/unpack in place on later Undo
        # entries — the grid is never rebuilt just to refresh badges.
        if mode == "undo":
            b = tk.Label(txt, text="✓ Active", font=(F, 7, "bold"),
                         bg=COLORS["bg_alt"], fg=COLORS["accent_green"], cursor="hand2")
            if t.key in state:
                b.pack(side="left", anchor="center", padx=(6, 0))
            extra.append(b)
            outer._undo_badge = b
        # Find-my-fastest-DNS entry (user-approved feature 5, 2026-09):
        # a mini "⚡ Test" button on the gaming_dns row only (Custom grid,
        # run mode). Deliberately NOT in `extra`: extra widgets inherit
        # the row's click-to-toggle binding, but this button must open
        # the tester instead of flipping the switch — its own binding is
        # the only one it carries (same structural exemption as the
        # ToggleSwitch itself).
        if mode == "run" and self.tab_name == "Tweak" and t.key == "gaming_dns":
            test_btn = AnimatedButton(
                txt, text="⚡ Test", command=self._open_dns_tester,
                bg=COLORS["surface_hover"], fg=COLORS["text"],
                font=(F, 8), padx=8, pady=3)
            test_btn.pack(side="left", anchor="center", padx=(6, 0))
            # harness marker: the generic synth-events loop must skip this
            # button (a press+release would open a live tester dialog with
            # real scan workers mid-suite); its behavior is covered by the
            # dedicated dns tests instead
            test_btn._harness_skip = True
            Tooltip(test_btn, "Find My Fastest DNS — tests Cloudflare, Google, "
                              "Quad9, OpenDNS and AdGuard on YOUR connection, then offers "
                              "to apply the winner with one click.")

        # subtle hover (user request: NO outline, NO background wash — both
        # read as a square box around the pill toggle). Only the switch
        # brightens and the task name tints toward the tab color.
        _hi_fg = _hex_lerp(COLORS["text"], accent, 0.65)

        def _hover(on, ev=None):
            # Leave fires on every child crossing too — ignore those while
            # the pointer is still anywhere inside this row.
            if not on and ev is not None:
                try:
                    w = outer.winfo_containing(ev.x_root, ev.y_root)
                    p = w
                    while p is not None:
                        if p == outer:
                            return
                        p = p.master
                except Exception:
                    pass
            try:
                lbl.config(fg=_hi_fg if on else COLORS["text"])
                sw._draw(hover=True) if on else sw._draw()
            except Exception:
                pass

        def _click(ev):
            if ev.widget is sw:
                return  # the switch handles its own clicks
            var.set(not var.get())

        # add="+": the labels carry Tooltips (plain bind() would wipe them —
        # same ordering bug that once killed the Install name hover)
        for w in (outer, inner, txt, lbl, *extra):
            w.bind("<Enter>", lambda e: _hover(True, e), add="+")
            w.bind("<Leave>", lambda e: _hover(False, e), add="+")
            w.bind("<Button-1>", _click)
        return (outer, t)

    def _build_run_row(self):
        for w in self.run_row.winfo_children():
            w.destroy()
        accent = TAB_ACCENTS[self.tab_name]
        if self.mode == "undo":
            label, bg, fg = "Undo Selected", COLORS["accent_red"], "#FFFFFF"
            self.run_btn = AnimatedButton(
                self.run_row, text=label, command=self._run_selected,
                bg=bg, fg=fg, font=(F, 12, "bold"), padx=42, pady=11,
            )
            self.run_btn.pack(anchor="center")
        elif self.mode == "health":
            # two centered health buttons (same 12pt pill language as Run):
            # Check Health re-runs the read-only verifies; Re-apply All
            # replays every applied tweak through the standard run engine
            btns = tk.Frame(self.run_row, bg=COLORS["bg"])
            btns.pack(anchor="center")
            self.run_btn = AnimatedButton(
                btns, text="Check Health", command=self._check_health_async,
                bg=accent, fg=COLORS["black"], font=(F, 12, "bold"),
                padx=24, pady=11,
            )
            self.run_btn.pack(side="left", padx=6)
            n = len(getattr(self, "_health", []) or [])
            self._health_reapply_btn = AnimatedButton(
                btns, text=f"Re-apply All ({n})" if n else "Re-apply All",
                command=self._reapply_health_all,
                bg=COLORS["surface"], fg=COLORS["text"],
                font=(F, 12, "bold"), padx=24, pady=11,
            )
            self._health_reapply_btn.pack(side="left", padx=6)
        else:
            label, bg, fg = "Run", accent, COLORS["black"]
            self.run_btn = AnimatedButton(
                self.run_row, text=label, command=self._run_selected,
                bg=bg, fg=fg, font=(F, 12, "bold"), padx=42, pady=11,
            )
            self.run_btn.pack(anchor="center")
        self._refresh_run_count()

    def _run_count(self) -> int:
        """Live selection size for the Run button (user request: show what
        you picked). Preset mode = preset length; custom/undo = toggles on."""
        try:
            return len(self.selected_tasks())
        except Exception:
            return 0

    def _refresh_run_count(self):
        """Repaint 'Run (N)' / 'Undo Selected (N)' — plain label when 0.
        Health mode has its own button labels (no selection count), so it
        skips here; its Re-apply count is refreshed at run-row build."""
        if getattr(self, "_suspend_count_refresh", False):
            return  # F7: _set_all is coalescing — it refreshes once after
        if getattr(self, "mode", "") == "health":
            return
        try:
            btn = getattr(self, "run_btn", None)
            if btn is None or not btn.winfo_exists():
                return
            n = self._run_count()
            if self.mode == "undo":
                btn.config_text(f"Undo Selected ({n})" if n else "Undo Selected")
            else:
                btn.config_text(f"Run ({n})" if n else "Run")
        except Exception:
            pass

    # ---------------- interactions ---------------- #

    def _select_preset(self, name):
        # F2: mode switches update IN PLACE. The preset cards and the Run
        # button are built once (see _build); switching modes only changes
        # mode state, the body (cache hit = instant show/hide), the card
        # highlight, and the Run button's colors/label — no widget
        # destroy/recreate churn per click.
        self.mode = "preset"
        self._selected_preset = name
        self._build_body()
        self._update_run_row()
        self._highlight_cards()

    def _enter_custom(self):
        self.mode = "custom"
        self._selected_preset = None
        self._build_body()
        self._update_run_row()
        self._highlight_cards()

    def _enter_health(self):
        """Tweak Health (user-approved feature 2, 2026-09): the 6th Tweak
        card. Shows every tweak the app believes is applied on this PC
        and checks whether Windows still honors it. Same enter pattern as
        _enter_undo — except the body is ALWAYS rebuilt fresh (rows depend
        on live applied-state; plain labels are cheap, unlike the animated
        toggle grid, so caching buys nothing and only risks stale rows)."""
        self.mode = "health"
        self._selected_preset = None
        old = self._body_cache.pop("health", None)
        try:
            if old is not None and old.winfo_exists():
                old.destroy()
        except Exception:
            pass
        self._body_vars.pop("health", None)
        self._build_body()       # cache miss -> fresh health rows + auto-check
        self._update_run_row()
        self._highlight_cards()

    def _refresh_health_state(self):
        """Rebuild self._health from the current applied-tweak registry:
        [(key, task, state)] for every applied key that still maps to a
        real Tweak task. State starts 'pending' (shown as ✓ Active, the
        registry's word — same as the Undo badges); the async check then
        promotes rows to active / drifted / unknown."""
        try:
            self.app.tweak_state = get_tweak_state()
        except Exception:
            pass
        state = getattr(self.app, "tweak_state", None) or {}
        self._health = []
        for key in state:
            t = self.task_by_key.get(key)
            if t is not None and t.revert is not None:
                self._health.append([key, t, "pending"])
        # rows the health body built (repainted in place like F3)
        self._health_rows = getattr(self, "_health_rows", {})

    # ---------------- Tweak Health body (mode="health") ---------------- #

    def _build_health_body(self, wrap):
        """Status view, not a selection view (no checkboxes — the Phase 2
        rule): amber drift banner (hidden unless the check found drifted
        tweaks) + 2-column grid of applied-tweak status rows matching the
        Custom row geometry, all inside a rounded panel.

        Always starts from the LIVE applied-tweak registry (entry,
        undo-body-style cache clears, and post-run rebuilds all funnel
        here) and kicks the async verify at the end, so rows can never
        describe a machine state from before the last run."""
        accent = TAB_ACCENTS[self.tab_name]
        self._refresh_health_state()
        self._health_results = {}    # the auto-check below repopulates
        self._health_rows = {}
        # health has no toggle vars — explicitly clear, and give the body
        # cache empty cell lists so a later cache-hit restore never
        # resurrects another mode's grid handles (F8-class staleness)
        self.vars = {}
        self._cells = []
        self._cell_blocks = []

        # drift banner (pack_forget keeps it hidden until drift is found)
        banner = tk.Frame(wrap, bg="#3A2C12")
        tk.Label(banner, text="", font=(F, 10, "bold"), bg="#3A2C12",
                 fg=COLORS["accent_yellow"]).pack(side="left", anchor="w",
                                                  padx=10, pady=8)
        self._health_fix_btn = AnimatedButton(
            banner, text="Fix Them", command=self._fix_health_drift,
            bg=accent, fg=COLORS["black"], font=(F, 9, "bold"),
            padx=16, pady=6)
        self._health_fix_btn.pack(side="right", padx=10, pady=6)
        self._health_banner = banner
        self._health_banner_lbl = banner.winfo_children()[0]

        head = tk.Frame(wrap, bg=COLORS["bg"])
        head.pack(fill="x", pady=(6, 4))
        tk.Label(head, text=f"Your active tweaks — {len(self._health)} applied",
                 font=(F, 11, "bold"), bg=COLORS["bg"],
                 fg=accent).pack(side="left")
        self._health_checked_lbl = tk.Label(head, text="",
                                            font=(F, 9), bg=COLORS["bg"],
                                            fg=COLORS["subtext"])
        self._health_checked_lbl.pack(side="left", padx=10)

        panel = ScrollableRoundedPanel(wrap)
        panel.pack(fill="both", expand=True)
        if not self._health:
            tk.Label(panel.inner,
                     text="No tweaks applied yet — run a preset or pick some in Custom first.",
                     font=(F, 10), bg=COLORS["bg_alt"],
                     fg=COLORS["subtext"]).pack(expand=True, pady=24)
        else:
            rows = []
            for key, t, _st in self._health:
                cell = self._build_health_row(panel.inner, t)
                rows.append((cell, t))
            # same 2-across flow as the Custom grid (Install-tab density)
            for i, (cell, _t) in enumerate(rows):
                rr, cc = divmod(i, 2)
                cell.grid(row=rr, column=cc, sticky="new", padx=6, pady=2)
            for c in (0, 1):
                panel.inner.grid_columnconfigure(c, weight=1, uniform="healthrows")
        self._cells = []
        self._cell_blocks = []
        panel.refresh_scroll()
        self._paint_health_rows()
        # auto-check: ~25 fast registry reads in a worker (the one powercfg
        # query aside, every verify is sub-ms). The Check Health button
        # re-runs this same check on demand.
        self._check_health_async()

    def _build_health_row(self, parent, t):
        """One status row: status glyph + label + live-state text on the
        right. Same bg_alt cell geometry as _build_toggle_row (minus the
        switch — this is a display, not a control), so the Health view
        lines up with the Custom/Undo grids pixel-for-pixel."""
        outer = tk.Frame(parent, bg=COLORS["bg_alt"])
        inner = tk.Frame(outer, bg=COLORS["bg_alt"])
        inner.pack(fill="both", expand=True)
        dot = tk.Label(inner, text="·", font=(F, 12, "bold"),
                       bg=COLORS["bg_alt"], fg=COLORS["subtext"])
        dot.pack(side="left", anchor="center", padx=(8, 2), pady=6)
        lbl = tk.Label(inner, text=t.label, font=(F, 9, "bold"),
                       bg=COLORS["bg_alt"], fg=COLORS["text"],
                       anchor="w", justify="left",
                       wraplength=215)
        lbl.pack(side="left", anchor="center")
        Tooltip(lbl, t.description)
        state = tk.Label(inner, text="applied", font=(F, 8, "bold"),
                         bg=COLORS["bg_alt"], fg=COLORS["subtext"],
                         anchor="e")
        state.pack(side="right", anchor="center", padx=(6, 8))
        self._health_rows[t.key] = (outer, dot, state)
        outer._lbl = lbl
        return outer

    def _paint_health_rows(self):
        """Repaint every health row from self._health in place (F3
        pattern): states are pending/active/drifted/unknown. Drifted
        rows get the amber ROW TINT (selection language, same as the
        DNS picker) — 'fix these' targets are obvious at a glance."""
        results = getattr(self, "_health_results", {}) or {}
        amber = _hex_lerp(COLORS["bg_alt"], COLORS["accent_yellow"], 0.22)
        for key, t, _st in getattr(self, "_health", []) or []:
            handles = getattr(self, "_health_rows", {}).get(key)
            if handles is None:
                continue
            _outer, dot, state = handles
            try:
                if not dot.winfo_exists():
                    continue
            except Exception:
                continue
            outcome = results.get(key)
            if outcome is False:
                try:
                    self._tint_health_row(handles, amber)
                    dot.config(text="⚠", fg=COLORS["accent_yellow"])
                    state.config(text="Reset by Windows",
                                 fg=COLORS["accent_yellow"])
                except Exception:
                    pass
            elif outcome is True:
                try:
                    self._tint_health_row(handles, COLORS["bg_alt"])
                    dot.config(text="✓", fg=COLORS["accent_green"])
                    state.config(text="Active",
                                 fg=COLORS["accent_green"])
                except Exception:
                    pass
            else:
                try:
                    self._tint_health_row(handles, COLORS["bg_alt"])
                    dot.config(text="✓" if _st != "unknown" else "·",
                               fg=(COLORS["accent_green"] if _st != "unknown"
                                   else COLORS["subtext"]))
                    state.config(text="Active" if _st != "unknown" else "Applied",
                                 fg=(COLORS["accent_green"] if _st != "unknown"
                                     else COLORS["subtext"]))
                except Exception:
                    pass
        self._paint_health_banner()

    @staticmethod
    def _tint_health_row(handles, bg):
        """Repaint a health row (outer + inner + every child) in a new
        background — the tint IS the state language now."""
        outer = handles[0]
        if outer is None or not outer.winfo_exists():
            return
        outer.config(bg=bg)
        for child in outer.winfo_children():
            try:
                child.config(bg=bg)
                for sub in child.winfo_children():
                    sub.config(bg=bg)
            except Exception:
                pass

    def _paint_health_banner(self):
        """Pack the amber drift banner only when verified-drifted rows
        exist (it stays hidden otherwise — no nagging)."""
        banner = getattr(self, "_health_banner", None)
        if banner is None:
            return
        try:
            if not banner.winfo_exists():
                return
            results = getattr(self, "_health_results", {}) or {}
            health = getattr(self, "_health", []) or []
            drifted = [k for k, _t, _s in health if results.get(k) is False]
            if drifted:
                self._health_banner_lbl.config(
                    text=f"⚠  Windows undid {len(drifted)} of your tweak{'s' if len(drifted) != 1 else ''}")
                self._health_fix_btn.config_text(
                    f"Fix Them ({len(drifted)})" if drifted else "Fix Them")
                if banner.winfo_manager() == "":
                    banner.pack(fill="x", pady=(0, 6))
            else:
                if banner.winfo_manager() != "":
                    banner.pack_forget()
        except Exception:
            pass

    # ---------------- Tweak Health check + re-apply ---------------- #

    def _check_health_async(self):
        """Run every applied tweak's verify() in a worker thread (verify
        fns are read-only, but the one powercfg query takes ~100ms and
        Tk updates must never come from a worker — the worker writes a
        plain holder dict and a Tk-thread poller publishes, the same shape
        as _CatalogPickerDialog._check_sweep).

        C-4 audit fix: the old worker called self.winfo_exists() +
        self.after() directly — Tk calls from a foreign thread, which burn
        ~1s and can raise outside event dispatch (measured rule behind the
        picker's async design). The worker now touches no Tk at all."""
        health = list(getattr(self, "_health", []) or [])
        if not health:
            try:
                self._health_checked_lbl.config(text="")
            except Exception:
                pass
            self._paint_health_banner()
            return
        try:
            self._health_checked_lbl.config(text="Checking…")
        except Exception:
            pass
        # Generation-guarded holder: a fresh check replaces it, so a late
        # landing from a superseded sweep is dropped, never painted.
        holder = {"done": False, "res": {}}
        self._health_holder = holder

        def _worker():
            res = {}
            for key, t, _st in health:
                fn = getattr(t, "verify", None)
                if fn is None:
                    continue            # no verify: registry's word stands
                try:
                    v = fn()
                except Exception:
                    v = None
                res[key] = v
            # pure-Python publish — never a Tk call (see docstring)
            try:
                holder["res"] = res
            except Exception:
                pass
            try:
                holder["done"] = True
            except Exception:
                pass

        try:
            import threading as _th
            _th.Thread(target=_worker, daemon=True).start()
        except Exception:
            # no sweep in flight — publish the empty result now (already on
            # the Tk thread) instead of arming a poller that could never land
            self._finish_health_check({})
            return
        try:
            self.after(150, lambda: self._check_health(holder))
        except Exception:
            pass

    def _check_health(self, holder):
        """Tk-thread poller for one health sweep. Drops superseded results
        (a newer check replaced the holder), re-arms while the sweep flies,
        publishes exactly once via _finish_health_check."""
        try:
            if getattr(self, "_health_holder", None) is not holder:
                return              # superseded — drop silently
        except Exception:
            return
        try:
            if not self.winfo_exists():
                return
        except Exception:
            return
        try:
            done = holder.get("done")
        except Exception:
            return
        if not done:
            try:
                self.after(150, lambda: self._check_health(holder))
            except Exception:
                pass
            return
        try:
            res = holder.get("res") or {}
        except Exception:
            res = {}
        self._finish_health_check(res)

    def _finish_health_check(self, res):
        import time as _t
        try:
            self._health_results = dict(res or {})
        except Exception:
            self._health_results = {}
        verified = sum(1 for v in self._health_results.values() if v is True)
        drifted = sum(1 for v in self._health_results.values() if v is False)
        try:
            self._health_checked_lbl.config(
                text=(f"Checked just now — {verified} active"
                      + (f", {drifted} reset by Windows" if drifted else "")))
        except Exception:
            pass
        self._paint_health_rows()
        if drifted:
            try:
                self.app.set_status(
                    f"Tweak Health: {drifted} tweak(s) were reset — press Fix Them to restore.")
            except Exception:
                pass

    def _fix_health_drift(self):
        """Re-apply only the verified-drifted tweaks through the standard
        run engine (runs land on the scorecard like any other run)."""
        results = getattr(self, "_health_results", {}) or {}
        health = getattr(self, "_health", []) or []
        by_key = {t.key: t for _k, t, _s in health}
        drifted = [by_key[k] for k in results if results.get(k) is False and k in by_key]
        if not drifted:
            try:
                _themed_showinfo(self.app.root, "Nothing to Fix",
                                 "Every checkable tweak is still active. Nothing was re-applied.",
                                 accent=TAB_ACCENTS[self.tab_name])
            except Exception:
                pass
            return
        self.app.run_tasks(self.tab_name, drifted, mode="run")

    def _reapply_health_all(self):
        """Re-apply every applied tweak (the Phase-1 'Reinforce' button —
        one click restores anything a Windows Update quietly reset)."""
        health = getattr(self, "_health", []) or []
        tasks = [t for _k, t, _s in health]
        if not tasks:
            return
        self.app.run_tasks(self.tab_name, tasks, mode="run")

    def _enter_undo(self):
        self.mode = "undo"
        self._selected_preset = None
        # badges must reflect the machine's CURRENT applied state, not the
        # state from when the app was opened — repaint them in place on the
        # cached undo grid (F3); a missing cached body rebuilds fresh below
        self._refresh_undo_badges()
        self._build_body()
        self._update_run_row()
        self._highlight_cards()

    def _update_run_row(self):
        """F2: restyle the persistent Run button for the current mode
        instead of destroying/recreating it per switch (each rebuild also
        paid a temp-label font measure). The command never changes —
        _run_selected dispatches on self.mode — so only the colors and the
        base label differ per mode; _refresh_run_count adds the live count.
        Health mode is the exception: its two-button row (Check Health /
        Re-apply All) is rebuilt here so the applied-count label is always
        fresh — mode switches are rare, the body builds dominate cost."""
        if self.mode == "health":
            try:
                self._build_run_row()
            except Exception:
                pass
            return
        btn = getattr(self, "run_btn", None)
        try:
            if btn is None or not btn.winfo_exists():
                self._build_run_row()
                return
        except Exception:
            self._build_run_row()
            return
        accent = TAB_ACCENTS[self.tab_name]
        if self.mode == "undo":
            btn.set_style(bg=COLORS["accent_red"], fg="#FFFFFF",
                          hover_bg=_hex_lerp(COLORS["accent_red"], "#FFFFFF", 0.08))
        else:
            btn.set_style(bg=accent, fg=COLORS["black"],
                          hover_bg=_hex_lerp(accent, "#FFFFFF", 0.08))
        self._refresh_run_count()

    def _refresh_undo_badges(self):
        """F3: repaint the Undo grid's '✓ Active' badges IN PLACE from the
        machine's CURRENT applied-tweak state. Every undo row owns a badge
        label (created at grid build, packed only while the tweak is
        applied); entering Undo just packs/unpacks those handles — a couple
        of ms vs. rebuilding the ~60-row grid (~300+ ms) every entry. Rows
        are the ones captured when the undo grid was last built; after a
        run the whole body cache is cleared (see _clear_body_cache call in
        the run-completion path), so the next entry rebuilds once fresh."""
        try:
            self.app.tweak_state = get_tweak_state()
        except Exception:
            pass
        state = getattr(self.app, "tweak_state", None) or {}
        for outer, t in getattr(self, "_undo_rows", ()) or ():
            try:
                if not outer.winfo_exists():
                    continue
                badge = getattr(outer, "_undo_badge", None)
                if badge is None or not badge.winfo_exists():
                    continue
                if t.key in state:
                    if badge.winfo_manager() == "":
                        badge.pack(side="left", anchor="center", padx=(6, 0))
                else:
                    if badge.winfo_manager() != "":
                        badge.pack_forget()
            except Exception:
                continue

    def _highlight_cards(self):
        active = {"preset": self._selected_preset, "custom": "Custom", "undo": "Undo Tweaks",
                  "health": "Tweak Health"}.get(self.mode)
        for name, card in getattr(self, "preset_cards", {}).items():
            inner = card.winfo_children()[0]
            if name == active:
                accent = COLORS["accent_red"] if name == "Undo Tweaks" else TAB_ACCENTS[self.tab_name]
                inner.config(bg=COLORS["surface_hover"])
                for child in inner.winfo_children():
                    try:
                        child.config(bg=COLORS["surface_hover"])
                        for sub in child.winfo_children():
                            sub.config(bg=COLORS["surface_hover"])
                    except Exception:
                        pass
                card.config(bg=accent)
            else:
                inner.config(bg=COLORS["surface"])
                for child in inner.winfo_children():
                    try:
                        child.config(bg=COLORS["surface"])
                        for sub in child.winfo_children():
                            sub.config(bg=COLORS["surface"])
                    except Exception:
                        pass
                card.config(bg=COLORS["hairline"])

    def _set_all(self, on: bool):
        """F7: All On / All Off. Every var write fires its row trace, which
        refreshed the run-count button label once PER ROW (N redundant
        label re-measures + full canvas redraws). Suppress the per-row
        count refresh for the loop, then refresh exactly once. Everything
        else each row write does — the ToggleSwitch's own trace plays its
        animation — is untouched."""
        self._suspend_count_refresh = True
        try:
            for v in self.vars.values():
                v.set(on)
        finally:
            self._suspend_count_refresh = False
        self._refresh_run_count()

    def _apply_toggle_filter(self, q, placeholder, mode, blocks=None, panel=None):
        """Filter the Custom/Undo rows as the user types: hide rows whose
        label/description doesn't match; hide empty group headers. 'All On/
        All Off' still respect hidden rows' vars (they act on self.vars
        directly), which is the honest behavior — hidden ≠ deselected.

        F8 (user bug 2026-09-06: typing in the box changed nothing): this
        used to read self._cell_blocks live — shared mutable state that on
        a cache hit pointed at whatever grid was built LAST (often a
        hidden one), so the filter happily grid_remove()d rows the user
        couldn't see. Callers now pass THIS grid's blocks + panel; the
        self._cell_blocks fallback stays only for legacy direct calls
        (the smoke harness), which are single-grid and safe.

        Grid mode (2-column): hidden cells use grid_remove() so their
        grid slot (row/column) is REMEMBERED — re-showing restores the
        exact position, and hidden rows leave no gap because grid_remove
        collapses empty rows."""
        q = (q or "").strip().lower()
        show_all = (not q) or q == placeholder.lower()
        blocks = blocks if blocks is not None else (getattr(self, "_cell_blocks", None) or [])
        for _hdr, rows in blocks:
            if not rows:
                continue
            any_visible = False
            for _row, t in rows:
                managed = _row.winfo_manager() == "grid"
                if show_all or (q in t.label.lower() or q in t.description.lower()):
                    if not managed:
                        _row.grid()
                    any_visible = True
                else:
                    if managed:
                        _row.grid_remove()
            if _hdr is not None:
                hdr_managed = _hdr.winfo_manager() == "grid"
                if show_all or any_visible:
                    if not hdr_managed:
                        _hdr.grid()
                else:
                    if hdr_managed:
                        _hdr.grid_remove()
        # scroll metrics only recompute in refresh_scroll(); hiding rows
        # shrinks the inner frame — without this the thumb/offsets describe
        # the pre-filter content (overshoot scrolling, wrong thumb size).
        if panel is not None:
            try:
                panel.refresh_scroll()
                return
            except Exception:
                pass
        for w in self.body_area.winfo_children():
            if not w.winfo_ismapped():
                continue
            for child in w.winfo_children():
                if isinstance(child, ScrollableRoundedPanel):
                    child.refresh_scroll()

    def set_run_enabled(self, enabled: bool):
        if getattr(self, "run_btn", None):
            try:
                self.run_btn.set_enabled(enabled)
            except Exception:
                pass
        # health mode's second button (Re-apply All / Fix flows) must be
        # gated too, or a run could be launched twice
        hb = getattr(self, "_health_reapply_btn", None)
        if hb is not None:
            try:
                if hb.winfo_exists():
                    hb.set_enabled(enabled)
            except Exception:
                pass
        fb = getattr(self, "_health_fix_btn", None)
        if fb is not None:
            try:
                if fb.winfo_exists():
                    fb.set_enabled(enabled)
            except Exception:
                pass

    def selected_tasks(self):
        if self.mode == "preset" and self._selected_preset:
            keys = self.presets[self._selected_preset]
            return [self.task_by_key[k] for k in keys]
        return [t for t in self.tasks if self.vars.get(t.key) and self.vars[t.key].get()]

    def undo_selected_tasks(self):
        return [t for t in self.tasks if self.vars.get(t.key) and self.vars[t.key].get() and t.revert]

    def _run_selected(self):
        if self.mode == "undo":
            selected = self.undo_selected_tasks()
            if not selected:
                _themed_showinfo(self, "Nothing to Undo",
                                 "Turn on at least one tweak to undo first.",
                                 accent=COLORS["accent_yellow"])
                return
            if not _themed_askyesno(self, "Confirm Undo",
                                   f"Put back {len(selected)} tweak(s) to how "
                                   "they were before?",
                                   accent=COLORS["accent_red"],
                                   yes_text="Undo Them", no_text="Go Back"):
                return
            self.app.run_tasks(self.tab_name, selected, mode="revert")
        else:
            selected = self.selected_tasks()
            if not selected:
                _themed_showinfo(self, "Nothing Selected",
                                 "Pick a preset or turn some switches on first.",
                                 accent=COLORS["accent_yellow"])
                return
            self.app.run_tasks(self.tab_name, selected, mode="run")

    def _open_dns_tester(self):
        """Find My Fastest DNS entry (the ⚡ Test button on the gaming_dns
        row). Modal dialog; applying runs through the standard engine."""
        try:
            self.app._open_dns_tester()
        except Exception:
            pass


# --------------------------------------------------------------------------- #
# Install tab — Master App Catalog browser (Install.txt spec)
# --------------------------------------------------------------------------- #

class InstallTab(tk.Frame):
    """Catalog browser: categories with checkboxes, [FOSS] badges, hover
    tooltips (description + link hint), per-category Select All, and one
    'Install Selected Apps' button that runs everything through the unified
    winget engine (network pre-check, silent, latest, fallback URLs).

    Layout (user request): app rows flow in TWO columns per category —
    big categories stop being a scroll marathon. Names are the links
    (hover-underline); 🔗 is the mirror link when a fallback exists."""

    _CAT_COLUMNS = 2

    def __init__(self, parent, app):
        super().__init__(parent, bg=COLORS["bg"])
        self.app = app
        self.tab_name = "Install"
        self.vars = {}            # winget id -> BooleanVar
        self._cat_frames = {}     # category -> (header_frame, body_frame, open)
        self._app_rows = {}       # app id -> row widget (for installed badges)
        self._cat_sections = []  # (category, header, body, all_row_widgets) for the search filter
        self._bundle_rows = []   # (row_widget, searchable_text) — embedded bundle/task rows
        self._installed_ids = None  # cached set from install_tasks.get_installed_ids
        accent = TAB_ACCENTS["Install"]

        from app.app_catalog import APP_CATALOG, CATEGORY_ORDER, MANUAL_ONLY_APPS

        # top bar: hint + centered search. The action buttons live in a
        # bottom-middle run row (user request: match Clean/Repair/Tweak).
        #
        # SYMMETRY FIX (user bug report 2026-09): this row used to be
        # label-height (~27 px), so Install's content column started
        # ~27 px higher than Clean/Repair/Tweak's preset-card row —
        # switching to/from Install read as the whole page "jumping".
        # The row is now pinned to the same height as a one-row preset
        # card area (56 px, measured from the Clean tab) with the hint
        # + search vertically centered inside, so every tab's first
        # visual row starts and ends at the same Y.
        topbar = tk.Frame(self, bg=COLORS["bg"])
        topbar.pack(fill="x", padx=26, pady=(14, 4))
        topbar.pack_propagate(False)
        topbar.config(height=56)
        hint_wrap = tk.Frame(topbar, bg=COLORS["bg"])
        hint_wrap.pack(side="left", fill="y")
        hint_wrap.rowconfigure(0, weight=1)
        tk.Label(hint_wrap, text="Pick apps to install — latest versions, silent install.",
                 font=(F, 10), bg=COLORS["bg"],
                 fg=COLORS["subtext"]).grid(row=0, sticky="w")
        # search box (user request: big catalog, no way to find one).
        # Placeholder count stays true automatically (was hardcoded '147',
        # drifting every time the catalog changed).
        from app.app_catalog import APP_CATALOG as _AC, MANUAL_ONLY_APPS as _MO
        _search_ph = f"Search {len(_AC) + len(_MO)} apps…"
        # rounded + centered between the hint and the buttons (user request)
        search_frame = tk.Frame(topbar, bg=COLORS["bg"])
        search_frame.pack(side="left", expand=True, fill="both")
        search_frame.rowconfigure(0, weight=1)
        search_center = tk.Frame(search_frame, bg=COLORS["bg"])
        search_center.grid(row=0, sticky="ew")
        self._search_var = tk.StringVar(value="")
        self._search_var.trace_add("write", lambda *_: self._apply_search())
        _sre = RoundedEntry(search_center, textvariable=self._search_var, width=18,
                            accent=accent)
        _sre.pack(anchor="center")
        self._search_entry = _sre.entry
        self._search_entry.insert(0, _search_ph)
        self._search_placeholder = _search_ph
        self._search_focused = False
        self._search_entry.bind("<FocusIn>", self._search_focus_in)
        self._search_entry.bind("<FocusOut>", self._search_focus_out)

        # bottom-middle run row (user request: match the Clean/Repair/Tweak
        # Run buttons). Packed before the panel so the panel fills the
        # middle and the buttons can never be squeezed out of view.
        self.run_row = tk.Frame(self, bg=COLORS["bg"])
        self.run_row.pack(side="bottom", fill="x", padx=26, pady=(4, 10))
        _btns = tk.Frame(self.run_row, bg=COLORS["bg"])
        _btns.pack(anchor="center")
        # Update button: updates are an action, not an install selection —
        # live count from `winget upgrade` ("Update Apps (N)"). Starts in a
        # scanning state; the worker below paints the real count. Yellow so
        # it reads at a glance next to the green Install button.
        self.update_btn = AnimatedButton(
            _btns, text="Update Apps…", command=self._update_apps,
            bg=COLORS["accent_yellow"], fg=COLORS["black"], font=(F, 12, "bold"), padx=24, pady=11,
        )
        self.update_btn.pack(side="left", padx=6)
        self._update_count = None  # None = unknown/scanning, else int
        self.install_btn = AnimatedButton(
            _btns, text="Install", command=self._install_selected,
            bg=accent, fg=COLORS["black"], font=(F, 12, "bold"), padx=24, pady=11,
        )
        self.install_btn.pack(side="left", padx=6)
        self.install_btn.set_enabled(False)  # gray until something is picked

        panel = ScrollableRoundedPanel(self)
        panel.pack(fill="both", expand=True, padx=26, pady=6)
        self._panel = panel

        # --- Essentials: one-click tasks, split in two groups (user request):
        #   * "LTSC Missing Components" — Store, winget+UniGetUI, Xbox stack,
        #     Game Bar, Windows codecs (the bits LTSC strips out)
        #   * "Essentials" — the ALL-VC++/DirectX/.NET/Java/Classic runtime
        #     bundles (what used to be the "Runtimes & Dependencies" catalog
        #     category, merged so nobody guesses versions).
        # Styled as CHECKBOX ROWS to match the catalog below (user feedback:
        # the old button-grid looked different/inconsistent). Checking rows
        # selects them; the big 'Install Selected Apps' button runs the
        # checked Essentials TOGETHER with checked catalog apps. ---
        from app.tab_presets import TABS as ALL_TABS
        ess_tasks = list(ALL_TABS["Install"])
        self.ess_vars = {}
        self._ess_groups = {}       # group title -> {"body": frame, "arrow": label}
        self.bundle_vars = {}       # bundle key -> (BooleanVar, Task); rendered inside catalog categories
        self._cat_count_lbls = {}   # category -> "N apps" label (x/y selected)
        # group order follows TASKS order (Store stays first: brings winget)
        _seen_groups: list = []
        for _t in ess_tasks:
            _g = getattr(_t, "group", "Essentials")
            if _g not in _seen_groups:
                _seen_groups.append(_g)
        _group_subs = {
            "LTSC Missing Components": "— the Store, winget, Xbox, Game Bar & codecs that LTSC strips out",
            "Essentials": "— one-click runtime bundles: check them all, no version guessing",
        }

        # F6(a): the catalog rows (~950 widgets) are NOT built on the
        # synchronous startup path anymore — the constructor above (chrome,
        # buttons, panel) is all the first frame pays for. The sections
        # below are queued and built in idle-time slices by
        # _start_catalog_build() (one Essentials group or one category per
        # slice, each ~5-20 ms), which Application kicks off after its
        # post-startup pre-warm chain. Until the queue drains, a lightweight
        # placeholder marks the content area; features that touch rows
        # (search, badges, counts) are all pre-build-safe by design — see
        # _catalog_finish for what is applied once rows exist.
        self._catalog_queue = []
        for _g in _seen_groups:
            _gtasks = [t for t in ess_tasks if getattr(t, "group", "Essentials") == _g]
            self._catalog_queue.append(
                lambda g=_g, ts=_gtasks: self._build_ess_group(
                    panel.inner, g, _group_subs.get(g, ""), accent, ts))
        for cat in CATEGORY_ORDER:
            apps = [a for a in APP_CATALOG if a["category"] == cat]
            manuals = [a for a in MANUAL_ONLY_APPS if a["category"] == cat]
            if not apps and not manuals:
                continue
            self._catalog_queue.append(
                lambda c=cat, ap=apps, mn=manuals: self._build_category(
                    panel.inner, c, ap, mn, accent))

        self._catalog_ready = False   # queue fully drained?
        self._catalog_after = None    # chained-slice after-id
        self._pending_badges = None   # installed-badge scan that landed mid-build
        # "Preparing catalog…" marker. Since the progressive-show change
        # (2026-09, user-approved) the page CAN be shown half-built: the
        # marker sits above the sections that have already landed and
        # _catalog_finish drops it (plus applies pending badge/search/count
        # state) when the queue drains. It also still guards direct/harness
        # construction before any slice runs.
        self._placeholder = tk.Label(
            panel.inner, text="Preparing catalog…", font=(F, 10),
            bg=COLORS["bg_alt"], fg=COLORS["subtext"])
        self._placeholder.pack(pady=24)
        try:
            panel.refresh_scroll()
        except Exception:
            pass

        # installed badges + update count: kick off the (slow) winget calls
        # in worker threads, then paint on the Tk thread when they land.
        # Both paints are pre-build-safe (badges defer until rows exist).
        self._start_installed_badge_scan()
        self._start_update_count_scan()
        self._refresh_install_count()

    # ---------------- F6(a): chunked catalog build ---------------- #

    _CATALOG_SLICE_GAP_MS = 8   # breathing room between idle slices

    def _start_catalog_build(self):
        """F6(a): drain the queued catalog slices in idle time. Called by
        Application's post-startup pre-warm chain — AFTER the Custom/Undo
        pre-warm steps, so the one-off Tk costs (font loads) are already
        paid and every slice here stays small. No-op when already built or
        when a slice run is in flight."""
        if self._catalog_ready:
            return
        if self._catalog_after is not None:
            return  # a slice run is already in flight
        if not self._catalog_queue:
            # defensive: nothing queued but never finished — settle now
            self._catalog_finish()
            return
        self._catalog_step()

    def _catalog_step(self):
        """Build one queued slice (an Essentials group or a catalog
        category) and chain the rest on a small gap — the UI thread never
        blocks more than the slice takes (~5-20 ms)."""
        self._catalog_after = None
        # flicker fix: registry check, not grab_current() — a Combobox
        # popdown inside an open dialog steals the grab while posted, which
        # made this read "no modal" and build/raise slices behind the
        # popup (the Auto-Pilot dropdown flicker).
        _modal = ThemedModal.any_open()
        if _modal:
            # a popup is up — building/mapping slices now would churn the
            # main window behind it (user bug report: first-open flicker
            # behind Auto-Pilot). Retry shortly; the queue keeps its place
            # and _ensure_catalog_built can still cancel/drain explicitly.
            try:
                self._catalog_after = self.after(250, self._catalog_step)
            except Exception:
                self._catalog_after = None
            return
        if self._catalog_queue:
            try:
                self._catalog_queue.pop(0)()
            except Exception as exc:
                # honest failure: log and keep the chain alive — a widget
                # build error must not take the idle loop down
                try:
                    self.app.log(f"Install catalog: a section failed to build: {exc}")
                except Exception:
                    pass
        if self._catalog_queue:
            # Progressive-show (2026-09): while the user is actually
            # LOOKING at this page mid-build, settle the scroll metrics
            # after each slice so the scrollbar enables the moment the real
            # content outgrows the viewport (and the offset stays clamped).
            # Skipped while the page is hidden/covered — that pass would
            # only churn the visible page; _catalog_finish still does the
            # final full refresh when the queue drains. flush=False is
            # deliberate: the full refresh's update_idletasks forced every
            # pending idle redraw of the mapped content per slice (bench:
            # ~90-220 ms each) — the flush-free metrics pass recomputes
            # requested sizes without painting (~1-2 ms).
            if getattr(getattr(self, "app", None), "active_tab", None) == "Install":
                try:
                    self._panel.refresh_scroll(flush=False)
                except Exception:
                    pass  # panel torn down mid-build (shutdown)
            try:
                self._catalog_after = self.after(self._CATALOG_SLICE_GAP_MS,
                                                 self._catalog_step)
            except Exception:
                self._catalog_after = None  # root torn down mid-build
        else:
            self._catalog_finish()

    def _ensure_catalog_built(self):
        """F6(a): finish the queued catalog slices SYNCHRONOUSLY right now.

        Kept for direct/harness callers that need synchronous completeness
        (the repo smoke driver and measurement harness call it explicitly
        after switching to Install). The UI no longer calls this from
        _show_tab: reaching Install mid-build shows the page progressively
        instead of force-draining the queue on the click (the old call
        stalled the click 700-900 ms cold; measured 2026-09)."""
        if self._catalog_ready:
            return
        if self._catalog_after is not None:
            try:
                self.after_cancel(self._catalog_after)
            except Exception:
                pass
            self._catalog_after = None
        while self._catalog_queue:
            try:
                self._catalog_queue.pop(0)()
            except Exception as exc:
                try:
                    self.app.log(f"Install catalog: a section failed to build: {exc}")
                except Exception:
                    pass
        self._catalog_finish()

    def _catalog_finish(self):
        """F6(a): catalog fully built — drop the placeholder, then apply
        everything that arrived while the rows were still pending:
          * installed-badge scan result held in _pending_badges
          * a search query typed pre-build (fresh rows render filtered)
        and settle counts + scroll metrics on the real content."""
        self._catalog_ready = True
        try:
            if self._placeholder is not None and self._placeholder.winfo_exists():
                self._placeholder.destroy()
        except Exception:
            pass
        self._placeholder = None
        if self._pending_badges is not None:
            ids, self._pending_badges = self._pending_badges, None
            try:
                self._paint_installed_badges(ids)
            except Exception:
                pass
        try:
            q = self._search_var.get().strip()
            if q and not q.lower().startswith("search "):
                self._apply_search()
        except Exception:
            pass
        try:
            self._refresh_install_count()
            self._panel.refresh_scroll()
        except Exception:
            pass

    # ---------------- search box ---------------- #

    def _search_focus_in(self, _e):
        if not self._search_focused:
            self._search_focused = True
            if self._search_var.get().startswith("Search "):
                self._search_var.set("")

    def _search_focus_out(self, _e):
        self._search_focused = False
        if not self._search_var.get().strip():
            self._search_var.set(self._search_placeholder)

    def _apply_search(self):
        """Filter-as-you-type, Custom-tab semantics (user request): typing
        e.g. 'rufus' must CLEAR the page to only matching apps — no empty
        category dividers, no always-visible stragglers.

        Behavior:
          * empty/placeholder query -> full catalog back, Essentials back
          * otherwise: a category shows ONLY its matching rows (re-packed
            top-down per column, no gaps); a category with zero matches
            hides its whole block (divider + body) — an empty divider is
            noise, exactly the pre-fix bug
          * Essentials sections (LTSC components / runtime bundles) hide
            during a query too — they're one-click tasks, not catalog
            apps, so a name search must not leave them stranded mid-page
          * the embedded bundle rows (APO + Peace GUI, APO + FluidEQ,
            RustDesk, FreeFileSync) participate like any app: hidden
            unless the query matches their label/description

        2-column layout: rows live inside per-column frames. Hiding one
        leaves a gap in that column, so visible rows are RE-PACKED per
        column (top-down flow, no gaps) from the filter result."""
        q = self._search_var.get().strip().lower()
        show_all = (not q) or q.startswith("search ")
        from app.app_catalog import APP_CATALOG, MANUAL_ONLY_APPS
        by_id = {a["id"]: a for a in APP_CATALOG}
        by_id.update({m["id"]: m for m in MANUAL_ONLY_APPS})
        for cat, header, body, rows in getattr(self, "_cat_sections", []):
            visible_rows = []
            for app_id, row in rows:
                app = by_id.get(app_id)
                if app is None:
                    continue
                if show_all or (q in app["name"].lower()
                                or q in app["id"].lower()
                                or q in app["description"].lower()
                                or q in app.get("hardware", "").lower()):
                    visible_rows.append((app_id, row))
            any_visible = bool(visible_rows)
            # re-pack visible rows inside their OWN column frames, in original
            # order (pack_forget collapses, so no gaps). NOTE: rows must NEVER
            # move across columns: Tk forbids pack -in to any master that is
            # not the widget's parent or a descendant of it ("can't pack X
            # inside Y" — an uncle column is illegal), so the old round-robin
            # rebalance crashed on the first filtered search.
            col_frames = [c for c in body.winfo_children()
                          if isinstance(c, tk.Frame)]
            col_index = {str(c): i for i, c in enumerate(col_frames)}
            for app_id, row in rows:
                try:
                    row.pack_forget()  # parentage kept; re-pack below is legal
                except Exception:
                    pass
            buckets = [[] for _ in col_frames]
            for app_id, row in visible_rows:
                buckets[col_index.get(str(row.winfo_parent()), 0)].append(row)
            for col, bucket in zip(col_frames, buckets):
                for row in bucket:
                    row.pack(fill="x", padx=4, pady=2)
            # the whole category BLOCK hides when nothing matched — the
            # divider ('outer' frame: header + body + bundle row) packs
            # directly under the panel's inner frame, so pack_forget on it
            # removes divider AND body AND the bundle row in one move.
            outer_block = body.master
            bundle_visible = show_all or any(
                q in text for _row, text in getattr(self, "_bundle_rows", [])
                if _row.master is outer_block
            )
            hide_block = (not show_all) and not any_visible and not bundle_visible
            if hide_block:
                if outer_block.winfo_manager() == "pack":
                    outer_block.pack_forget()
            else:
                if outer_block.winfo_manager() != "pack":
                    outer_block.pack(fill="x", pady=(6, 4))
                # restore the body to the category's RECORDED open/collapsed
                # state (a search must not re-open a category the user
                # collapsed — the ▸/▾ arrow would otherwise desync).
                try:
                    cat_open = self._cat_frames[cat][2]
                except (KeyError, IndexError):
                    cat_open = True
                if cat_open and body.winfo_manager() != "pack":
                    body.pack(fill="x")
                elif not cat_open and body.winfo_manager() == "pack":
                    body.pack_forget()
            # embedded bundle rows (APO bundles etc.): filter by their own
            # searchable text; hide during a non-matching query
            for _row, text in getattr(self, "_bundle_rows", []):
                if _row.master is not outer_block:
                    continue
                try:
                    if show_all or (q in text):
                        _row.pack(fill="x", padx=10, pady=(2, 6))
                    else:
                        _row.pack_forget()
                except Exception:
                    pass
        # Essentials sections: one-click tasks, not catalog apps — hide
        # entirely during a query (Custom-tab semantics: only matches)
        for _title, group in getattr(self, "_ess_groups", {}).items():
            try:
                ess_block = group["body"].master  # the 'ess' frame
                if show_all:
                    if ess_block.winfo_manager() != "pack":
                        ess_block.pack(fill="x", pady=(2, 6))
                else:
                    if ess_block.winfo_manager() == "pack":
                        ess_block.pack_forget()
            except Exception:
                pass
        # keep the panel's scroll region honest after re-flow
        try:
            self._panel.refresh_scroll()
        except Exception:
            pass

    # ---------------- selection count ---------------- #

    def _refresh_install_count(self):
        """Live 'Install (N)' count + per-category 'x/y selected' labels
        (user request: show what you picked). Traced from every checkbox
        var, so All/None/search-independent sets all land. The Install
        button stays grayed out until something is picked (user request)."""
        try:
            sel = self.selected_apps()
            n = len(sel)
            btn = getattr(self, "install_btn", None)
            if btn is not None:
                btn.config_text(f"Install ({n})" if n else "Install")
                busy = bool(getattr(getattr(self, "app", None), "_busy", False))
                btn.set_enabled(bool(n) and not busy)
            from app.app_catalog import APP_CATALOG
            for cat, total_lbl in getattr(self, "_cat_count_lbls", {}).items():
                total = sum(1 for a in APP_CATALOG if a["category"] == cat)
                picked = sum(1 for k, s in sel if k == "app" and s["category"] == cat)
                # Essentials tasks counted on the button only (they have no
                # category header of their own to annotate)
                try:
                    total_lbl.config(text=f"{picked}/{total} selected" if picked else f"{total} apps")
                except Exception:
                    pass
        except Exception:
            pass

    def _trace_install_var(self, var):
        """One shared trace callback for every Install checkbox var."""
        try:
            var.trace_add("write", lambda *_: self._refresh_install_count())
        except Exception:
            pass

    # ---------------- installed badges ---------------- #

    def _post_to_tk(self, fn):
        """Schedule fn on the Tk thread. InstallTab is a tk.Frame and has
        NO .root attribute — the old self.root.after(...) here raised
        AttributeError on every scan (swallowed by the except), so the
        installed-badges and Update-count callbacks NEVER ran: badges never
        appeared and the Update button sat at 'Update Apps…' forever.
        winfo_toplevel() resolves the real root window (the same trick
        Tooltip._ensure_shared uses), which lives for the whole session.
        F10: the toplevel is captured on the Tk thread by
        _start_installed_badge_scan and held here — the worker never
        calls winfo_* itself."""
        try:
            tk_root = getattr(self, "_tk_holder", None)
            if tk_root is None:
                tk_root = self.winfo_toplevel()
                self._tk_holder = tk_root
            tk_root.after(0, fn)
        except Exception:
            pass  # shutdown race — root already destroyed; nothing to paint

    def _start_installed_badge_scan(self):
        """winget list is slow (1-3s) — run it once in a worker thread and
        post the result back; badges paint in one pass on the Tk thread."""
        # F10: capture the Tk holder on THIS (Tk) thread so the worker
        # below never touches winfo_*.
        try:
            self._tk_holder = self.winfo_toplevel()
        except Exception:
            self._tk_holder = None

        def _scan():
            try:
                from app.tasks.install_tasks import get_installed_ids
                ids = get_installed_ids()
            except Exception:
                ids = None
            if ids is not None:
                # F-001 fix: route through the real root (self.root never
                # existed on a Frame — this call silently killed the scan).
                self._post_to_tk(lambda: self._paint_installed_badges(ids))

        threading.Thread(target=_scan, daemon=True).start()

    def _paint_installed_badges(self, installed_ids):
        """Show a green '✓ Installed' badge next to every catalog app that
        winget reports as present. Runs once per session (cached upstream).

        (Placement fix, user feedback 2026-09-06: the badge used to pack at
        the row's FAR-RIGHT edge — visually divorced from the name it
        belongs to. It now packs side="left" AFTER the existing left-side
        widgets, so it joins the name cluster directly (name | FOSS | OEM |
        🔗 | ✓ Installed) instead of floating at the row's end. Color note:
        green stays reserved for this badge alone — the FOSS chip was
        recolored sky blue (accent_sky) so the two never read as one.)"""
        self._installed_ids = installed_ids or set()
        if not self._catalog_ready:
            # F6(a): the badge scan is kicked off at construction, but the
            # catalog rows build later in idle slices — hold the result so
            # _catalog_finish can paint it on the real rows.
            self._pending_badges = installed_ids or set()
            return
        n = 0
        for app_id, row in getattr(self, "_app_rows", {}).items():
            if app_id.lower() not in self._installed_ids:
                continue
            n += 1
            b = tk.Label(row, text="✓ Installed", font=(F, 7, "bold"),
                         bg=COLORS["bg_alt"], fg=COLORS["accent_green"])
            # beside the name cluster, not exiled to the row's right edge —
            # packed left it lands right after the last badge (FOSS / OEM /
            # 🔗), keeping the marker adjacent to the app it belongs to
            b.pack(side="left", padx=(6, 0))
        if n:
            self.app.log(f"Install tab: {n} of your catalog apps are already installed (badges shown).")

    # ---------------- Update Apps button ---------------- #

    def _start_update_count_scan(self, refresh: bool = False):
        """`winget upgrade` is slow (5-30s) — count upgradable apps in a
        worker thread, then paint 'Update Apps (N)' on the Tk thread."""

        def _scan():
            try:
                from app.tasks.install_tasks import get_upgradable_count
                n = get_upgradable_count(refresh=refresh)
            except Exception:
                n = None
            # F-001 fix: same dead self.root reference as the badge scan —
            # the Update-count paint never ran, so the button never showed
            # its real number. Post through the real toplevel instead.
            self._post_to_tk(lambda: self._paint_update_count(n))

        threading.Thread(target=_scan, daemon=True).start()

    def _paint_update_count(self, n):
        """Paint the Update button label. None = unknown (no winget /
        offline / parse failed) — plain 'Update Apps', still clickable.
        Always yellow (user request): it must read at a glance next to the
        Install button."""
        self._update_count = n
        btn = getattr(self, "update_btn", None)
        if btn is None:
            return
        try:
            if n is None:
                btn.config_text("Update Apps")
            else:
                btn.config_text(f"Update Apps ({n})")
            btn.set_style(bg=COLORS["accent_yellow"], fg=COLORS["black"])
        except Exception:
            pass

    def refresh_update_count(self):
        """Re-scan upgrades in the background (called after any install /
        update run completes, so the button never shows a stale number)."""
        btn = getattr(self, "update_btn", None)
        if btn is not None:
            try:
                btn.config_text("Update Apps…")
            except Exception:
                pass
        self._start_update_count_scan(refresh=True)

    def _update_apps(self):
        """Dedicated Update button: runs `winget upgrade --all` through the
        standard worker flow (progress bar + Stop button)."""
        from app.tasks.install_tasks import UPDATE_ALL_TASK
        self.app.install_selected_mixed([], [UPDATE_ALL_TASK])

    def _build_ess_group(self, parent, title, subtitle, accent, tasks):
        """One collapsible Essentials section (LTSC group or runtime group).
        Rows register into the shared self.ess_vars / self.vars pools so
        'Install Selected Apps' includes them naturally, in TASKS order.
        Header is arrow + titles only (user request: no All/None buttons)."""
        ess = tk.Frame(parent, bg=COLORS["bg_alt"])
        ess.pack(fill="x", pady=(2, 6))
        ess_head = tk.Frame(ess, bg=COLORS["surface"])
        ess_head.pack(fill="x")
        _ess_title = tk.Label(ess_head, text=title, font=(F, 10, "bold"),
                              bg=COLORS["surface"], fg=accent)
        _ess_title.pack(side="left", padx=(8, 4), pady=5)
        _ess_sub = tk.Label(ess_head, text=subtitle,
                            font=(F, 8), bg=COLORS["surface"], fg=COLORS["subtext"])
        _ess_sub.pack(side="left")

        body = tk.Frame(ess, bg=COLORS["bg_alt"])
        body.pack(fill="x")
        # arrow on the RIGHT like every other Install divider (user request)
        arrow = tk.Label(ess_head, text="▾", font=(F, 10, "bold"),
                         bg=COLORS["surface"], fg=accent, width=2)
        arrow.pack(side="right", padx=(0, 8), pady=6)

        def toggle_ess(b=body, a=arrow):
            if b.winfo_ismapped():
                b.pack_forget()
                a.config(text="▸")
            else:
                b.pack(fill="x")
                a.config(text="▾")

        arrow.bind("<Button-1>", lambda e: toggle_ess())
        ess_head.bind("<Button-1>", lambda e: toggle_ess())
        # hover recolors the bar AND its labels together (user bug: only the
        # bar changed, leaving boxed-looking text on mismatched backgrounds)
        def _ess_hover(on, _e=None):
            _bg = COLORS["surface_hover"] if on else COLORS["surface"]
            try:
                ess_head.config(bg=_bg)
                for _w in (_ess_title, _ess_sub, arrow):
                    _w.config(bg=_bg)
            except Exception:
                pass
        ess_head.bind("<Enter>", lambda e: _ess_hover(True))
        ess_head.bind("<Leave>", lambda e: _ess_hover(False))
        self._ess_groups[title] = {"body": body, "arrow": arrow}

        for task in tasks:
            var = tk.BooleanVar(value=False)
            self.ess_vars[task.key] = var
            # also register in the shared vars pool so 'Install Selected
            # Apps' includes Essentials naturally
            self.vars[f"task:{task.key}"] = var
            self._trace_install_var(var)
            row = tk.Frame(body, bg=COLORS["bg_alt"])
            row.pack(fill="x", padx=10, pady=2)
            cb = tk.Checkbutton(row, variable=var, bg=COLORS["bg_alt"],
                                fg=COLORS["text"], activebackground=COLORS["bg_alt"],
                                selectcolor=COLORS["surface"], onvalue=True, offvalue=False)
            cb.pack(side="left")
            name_lbl = tk.Label(row, text=task.label, font=(F, 9, "bold"),
                                bg=COLORS["bg_alt"], fg=COLORS["text"])
            name_lbl.pack(side="left", padx=(6, 4))
            # admin shield only (user request: no "one-click" text — the
            # shield matches the Custom toggle rows)
            if getattr(task, "admin_required", False):
                tg = tk.Label(row, text="🛡️", font=("Segoe UI Emoji", 9),
                              bg=COLORS["bg_alt"], fg=COLORS["text"], cursor="hand2")
                tg.pack(side="left", padx=(6, 0))
                Tooltip(tg, "Admin Required — needs Administrator rights (skipped in limited mode)")
            Tooltip(name_lbl, task.description)
            Tooltip(cb, task.description)

    def set_run_enabled(self, enabled: bool):
        if getattr(self, "install_btn", None):
            # re-enabling still respects the empty-selection gray-out
            # (user request) — only a non-empty pick lights Install up
            if enabled:
                try:
                    n = len(self.selected_apps())
                except Exception:
                    n = 1
                self.install_btn.set_enabled(bool(n))
            else:
                self.install_btn.set_enabled(False)
        if getattr(self, "update_btn", None):
            self.update_btn.set_enabled(enabled)

    def selected_apps(self):
        """Checked catalog apps + checked Essentials tasks, in one batch."""
        from app.app_catalog import APP_CATALOG
        from app.tab_presets import TABS as ALL_TABS
        task_by_key = {t.key: t for t in ALL_TABS["Install"]}
        result = []
        # Essentials first (Store before everything: brings winget)
        for key, var in getattr(self, "ess_vars", {}).items():
            if var.get():
                t = task_by_key.get(key)
                if t:
                    result.append(("task", t))
        # catalog-embedded bundles (APO + Peace / APO + FluidEQ live in
        # Media; RustDesk / FreeFileSync in Utilities — any task in
        # EMBEDDED_TASKS_BY_CATEGORY)
        for key, (var, t) in getattr(self, "bundle_vars", {}).items():
            if var.get():
                result.append(("task", t))
        for a in APP_CATALOG:
            v = self.vars.get(a["id"])
            if v is not None and v.get():
                result.append(("app", a))
        return result

    def _install_selected(self):
        selected = self.selected_apps()
        if not selected:
            _themed_showinfo(self, "Nothing Selected",
                             "Check some Essentials or apps to install first.",
                             accent=COLORS["accent_green"])
            return
        apps = [s for kind, s in selected if kind == "app"]
        tasks = [s for kind, s in selected if kind == "task"]
        self.app.install_selected_mixed(apps, tasks)

    def _row_icon(self, row, app_name, size=22):
        """App icon label for a catalog row (user call: logos next to
        names, big enough to read). Loads app/assets/apps/<name>.png
        once and caches the PhotoImage (anti-GC, tab-icon pattern);
        subsamples to a ~22px box so every row stays the same height.
        Missing file -> fixed-width spacer so names never misalign."""
        try:
            cache = self.__dict__.setdefault("_icon_cache", {})
            if app_name not in cache:
                cache[app_name] = None
                try:
                    import math as _math
                    import re as _re
                    base = _re.sub(r"\s*\(.*?\)", "", str(app_name))
                    base = _re.sub(r"[^a-z0-9]", "", base.lower()) + ".png"
                    from app.utils import resolve_asset_path as _rap
                    path = _rap(os.path.join("apps", base))
                    if path:
                        img = tk.PhotoImage(file=path)
                        w, h = img.width(), img.height()
                        if w > 0 and h > 0:
                            f = max(1, int(_math.ceil(max(w, h) / float(size))))
                            if f > 1:
                                img = img.subsample(f, f)
                            cache[app_name] = img
                except Exception:
                    cache[app_name] = None
            img = cache.get(app_name)
            if img is not None:
                lbl = tk.Label(row, image=img, bg=COLORS["bg_alt"],
                               bd=0, highlightthickness=0)
                lbl.pack(side="left", padx=(2, 0))
                return lbl
        except Exception:
            pass
        try:
            sp = tk.Frame(row, bg=COLORS["bg_alt"], width=size, height=size)
            sp.pack(side="left", padx=(2, 0))
            sp.pack_propagate(False)
        except Exception:
            pass
        return None

    def _build_category(self, parent, cat, apps, manuals, accent):
        outer = tk.Frame(parent, bg=COLORS["bg_alt"])
        outer.pack(fill="x", pady=(6, 4))

        header = tk.Frame(outer, bg=COLORS["surface"])
        header.pack(fill="x")
        # arrow on the RIGHT on every Install divider (user request)
        arrow = tk.Label(header, text="▾", font=(F, 10, "bold"), bg=COLORS["surface"],
                         fg=accent, width=2)
        arrow.pack(side="right", padx=(0, 8), pady=6)
        _cat_title = tk.Label(header, text=cat, font=(F, 10, "bold"), bg=COLORS["surface"],
                              fg=COLORS["text"])
        _cat_title.pack(side="left", padx=6)
        n_lbl = tk.Label(header, text=f"{len(apps) + len(manuals)} apps", font=(F, 9),
                         bg=COLORS["surface"], fg=COLORS["subtext"])
        n_lbl.pack(side="left", padx=4)
        self._cat_count_lbls[cat] = n_lbl

        def toggle_cat():
            body = self._cat_frames[cat][1]
            open_ = self._cat_frames[cat][2]
            if open_:
                body.pack_forget()
                arrow.config(text="▸")
                self._cat_frames[cat] = (self._cat_frames[cat][0], body, False)
            else:
                body.pack(fill="x")
                arrow.config(text="▾")
                self._cat_frames[cat] = (self._cat_frames[cat][0], body, True)

        # (user request: no All/None buttons on catalog category dividers —
        # pick apps individually; Essentials groups keep theirs)
        # hover recolors the bar AND its labels together (user bug: only the
        # bar changed, leaving boxed-looking text on mismatched backgrounds)
        def _cat_hover(on, _e=None):
            _bg = COLORS["surface_hover"] if on else COLORS["surface"]
            try:
                header.config(bg=_bg)
                for _w in (_cat_title, n_lbl, arrow):
                    _w.config(bg=_bg)
            except Exception:
                pass
        for w in (header, arrow):
            w.bind("<Button-1>", lambda e: toggle_cat())
            w.bind("<Enter>", lambda e: _cat_hover(True))
            w.bind("<Leave>", lambda e: _cat_hover(False))

        body = tk.Frame(outer, bg=COLORS["bg_alt"])
        body.pack(fill="x")
        cat_rows = []   # (app_id, row) — feeds the search filter
        cols = self._CAT_COLUMNS          # 2-column flow (user request)
        col_frames = []
        for c in range(cols):
            cf = tk.Frame(body, bg=COLORS["bg_alt"])
            cf.grid(row=0, column=c, sticky="nsew", padx=(6, 6))
            body.grid_columnconfigure(c, weight=1, uniform="catcols")
            col_frames.append(cf)

        def _make_name_link(parent, text, url, tip_text, base_fg=None, bold=True):
            """Clickable app name (user design): bold like the Custom task
            names, calm text color; hover turns it hyperlink-blue +
            underlined (click opens the primary link, 🔗 the mirror)."""
            fg = base_fg or COLORS["text"]
            _font = (F, 9, "bold") if bold else (F, 9)
            lbl = tk.Label(parent, text=text, font=_font,
                           bg=COLORS["bg_alt"], fg=fg, cursor="hand2")

            def _enter(_e):
                lbl.config(font=(F, 9, "bold", "underline") if bold else (F, 9, "underline"),
                           fg=COLORS["accent_blue"])

            def _leave(_e):
                lbl.config(font=_font, fg=fg)

            # Tooltip FIRST, hover binds with add="+": plain bind() replaces
            # (this exact ordering bug is why name hover highlighting silently
            # never worked — Tooltip's bind wiped it).
            if tip_text:
                Tooltip(lbl, tip_text)
            lbl.bind("<Enter>", _enter, add="+")
            lbl.bind("<Leave>", _leave, add="+")
            lbl.bind("<Button-1>", lambda e, u=url: self._open_url(u))
            return lbl

        def _checkbox_width_spacer(parent):
            """Width-matched invisible spacer holding the checkbox's slot in
            manual-only rows, so their names align exactly with the names in
            checkbox rows (user-reported misalignment). Sized from a real
            hidden Checkbutton — can't drift from theme/font changes the way
            a hardcoded pixel count would.

            Perf fix (2026-09-05, Install-tab lag report): this used to
            create AND destroy a real Checkbutton for every single
            manual-only row, on every category slice — dozens of throwaway
            widget creations (each a real OS window handle) during the
            idle-time catalog build, adding up to visible stutter switching
            into the tab. The probe measurement is identical every time
            (same theme/font for the whole tab lifetime), so it's now
            computed ONCE per InstallTab and cached on self — every
            subsequent manual row just reuses the cached width."""
            w = getattr(self, "_cb_spacer_width", None)
            if w is None:
                cb_probe = tk.Checkbutton(parent, bg=COLORS["bg_alt"],
                                           selectcolor=COLORS["surface"])
                w = cb_probe.winfo_reqwidth()
                cb_probe.destroy()
                self._cb_spacer_width = w
            sp = tk.Frame(parent, bg=COLORS["bg_alt"], width=w, height=1)
            sp.pack(side="left")
            sp.pack_propagate(False)   # hold the reserved width
            return sp

        for ci, app in enumerate(apps):
            var = tk.BooleanVar(value=False)
            self.vars[app["id"]] = var
            self._trace_install_var(var)
            row = tk.Frame(col_frames[ci % cols], bg=COLORS["bg_alt"])
            row.pack(fill="x", padx=4, pady=2)
            cb = tk.Checkbutton(row, variable=var, bg=COLORS["bg_alt"],
                                fg=COLORS["text"], activebackground=COLORS["bg_alt"],
                                selectcolor=COLORS["surface"], onvalue=True, offvalue=False)
            cb.pack(side="left")
            self._row_icon(row, app["name"])
            # tooltip: short description only (user request: no URLs in tips);
            # OEM-exclusive apps append their hardware tag (user request
            # 2026-09-06) so non-matching users are warned before installing
            tip = app["description"]
            hw = app.get("hardware", "")
            if hw:
                tip += f" ({hw})"
            name_lbl = _make_name_link(row, app["name"], app["url"], tip)
            name_lbl.pack(side="left", padx=(6, 4))
            if app["foss"]:
                # sky blue, NOT the tab accent: the accent IS green here, and
                # green is the "✓ Installed" badge's color — the two chips sat
                # next to each other in the same green (user feedback
                # 2026-09-06). accent_sky is the palette's designated Install
                # alt so FOSS/Installed read as different signals at a glance.
                foss = tk.Label(row, text="FOSS", font=(F, 7, "bold"),
                                bg=COLORS["bg_alt"], fg=COLORS["accent_sky"])
                foss.pack(side="left", padx=(4, 0))
            if hw:
                hw_lbl = tk.Label(row, text="⚠ OEM", font=(F, 7, "bold"),
                                  bg=COLORS["bg_alt"], fg=COLORS["accent_yellow"])
                Tooltip(hw_lbl, f"Hardware restricted — {hw}. Do not install this on other systems.")
                hw_lbl.pack(side="left", padx=(4, 0))
            # mirror link (user design): 🔗 beside the name as the SECONDARY
            # link when a verified fallback_url exists; the name stays the
            # primary. ↗ arrows are gone entirely.
            fallback = app.get("fallback_url", "")
            if fallback:
                self._make_mirror_link(row, fallback)
            Tooltip(cb, tip)
            cat_rows.append((app["id"], row))
            self._app_rows[app["id"]] = row

        # manual-only entries (no winget package exists): same row shape as
        # winget apps (user design) — NO checkbox (a width-matched spacer
        # keeps names aligned with checkbox rows, user-reported fix), normal
        # text color, name IS the link, FOSS badge when flagged, plus 🔗
        # when a mirror exists.
        for ci, app in enumerate(manuals):
            row = tk.Frame(col_frames[(len(apps) + ci) % cols], bg=COLORS["bg_alt"])
            row.pack(fill="x", padx=4, pady=2)
            _checkbox_width_spacer(row)
            self._row_icon(row, app["name"])
            fallback = app.get("fallback_url", "")
            # tooltip: short description only (user request: no URLs in tips;
            # the row has no checkbox, which already says "open the page")
            tip = app["description"]
            if fallback:
                tip += " (mirror available via 🔗)"
            name_lbl = _make_name_link(row, app["name"], app["url"], tip,
                                        base_fg=COLORS["text"])
            name_lbl.pack(side="left", padx=(6, 4))
            if app["foss"]:
                # sky blue to match the winget-row FOSS chips (green belongs
                # to the "✓ Installed" badge alone — see _paint_installed_badges)
                foss = tk.Label(row, text="FOSS", font=(F, 7, "bold"),
                                bg=COLORS["bg_alt"], fg=COLORS["accent_sky"])
                foss.pack(side="left", padx=(4, 0))
            if fallback:
                self._make_mirror_link(row, fallback)
            cat_rows.append((app["id"], row))

        # Embedded bundle/task rows (APO + Peace GUI, APO + FluidEQ, RustDesk,
        # FreeFileSync — anything in install_tasks.EMBEDDED_TASKS_BY_CATEGORY):
        # full-width checkbox rows at the END of the category, below the
        # columns. Search behavior (user request): these rows are NO LONGER
        # exempt from the filter — typing e.g. 'rufus' must clear the page
        # to ONLY matching apps, so they hide too unless the query matches
        # ('peace', 'fluideq', 'rustdesk'...). Each is registered as a
        # pseudo-app row the filter can match and hide.
        from app.tasks.install_tasks import EMBEDDED_TASKS_BY_CATEGORY as _EMBED
        for _bundle in _EMBED.get(cat, []):
            _bvar = tk.BooleanVar(value=False)
            self.bundle_vars[_bundle.key] = (_bvar, _bundle)
            self._trace_install_var(_bvar)
            _brow = tk.Frame(outer, bg=COLORS["bg_alt"])
            _brow.pack(fill="x", padx=10, pady=(2, 6))
            _bcb = tk.Checkbutton(_brow, variable=_bvar, bg=COLORS["bg_alt"],
                                   fg=COLORS["text"], activebackground=COLORS["bg_alt"],
                                   selectcolor=COLORS["surface"], onvalue=True, offvalue=False)
            _bcb.pack(side="left")
            self._row_icon(_brow, _bundle.label)
            _blbl = tk.Label(_brow, text=_bundle.label, font=(F, 9, "bold"),
                             bg=COLORS["bg_alt"], fg=COLORS["text"])
            _blbl.pack(side="left", padx=(6, 4))
            if _bundle.admin_required:
                _bsh = tk.Label(_brow, text="🛡️", font=("Segoe UI Emoji", 9),
                                bg=COLORS["bg_alt"], fg=COLORS["text"], cursor="hand2")
                _bsh.pack(side="left", padx=(6, 0))
                Tooltip(_bsh, "Admin Required — needs Administrator rights (skipped in limited mode)")
            Tooltip(_blbl, _bundle.description)
            Tooltip(_bcb, _bundle.description)
            # filter registration: searchable text from the bundle's own
            # label + description; stored on the OUTER block (its pack
            # master) so hiding one row never orphans the section logic.
            self._bundle_rows.append((_brow, f"{_bundle.label} {_bundle.description}".lower()))

        self._cat_frames[cat] = (header, body, True)
        self._cat_sections.append((cat, header, body, cat_rows))

    def _make_mirror_link(self, row, fallback):
        """🔗 mirror link with a press-flash (user request: some animation /
        effect on click so it feels alive). Press lights it in the tab
        accent; release opens the page and restores it. Same emoji font and
        size as the 🛡️ badge; no underline, ever."""
        _font = ("Segoe UI Emoji", 9)
        mirror = tk.Label(row, text="🔗", font=_font,
                          bg=COLORS["bg_alt"], fg=COLORS["subtext"],
                          cursor="hand2")
        mirror.pack(side="left", padx=(4, 0))

        def _press(_e):
            try:
                mirror.config(fg=TAB_ACCENTS["Install"])
            except Exception:
                pass

        def _release(e, u=fallback):
            try:
                mirror.config(fg=COLORS["subtext"], font=_font)
            except Exception:
                pass
            self._open_url(u)

        mirror.bind("<ButtonPress-1>", _press)
        mirror.bind("<ButtonRelease-1>", _release)
        Tooltip(mirror, "Backup download (official mirror)")
        return mirror

    @staticmethod
    def _resolve_default_browser_cmd() -> "list[str] | None":
        """Resolve the default https browser's launch command from the
        registry (UserChoice ProgId -> shell\\open\\command), or None.

        Why: os.startfile opens the URL via ShellExecute — but when the
        browser is ALREADY RUNNING, Windows' focus-stealing prevention
        leaves it in the background (taskbar flash, no window raise).
        Launching the browser exe directly with the URL as argv raises
        its window reliably, even when it's already open (user-reported:
        'opens the link but doesn't bring up the browser in view').

        Registry reality (verified on a live machine): the ProgId command
        template is registered under HKEY_CLASSES_ROOT (the merged
        HKCU+HKLM view) — e.g. FirefoxURL-<hash>\\shell\\open\\command —
        and often NOT under HKCU\\Software\\Classes. Check both, HKCR
        first as the authoritative merged view."""
        try:
            import winreg
            import shlex
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                                r"Software\Microsoft\Windows\Shell\Associations\UrlAssociations\https\UserChoice") as k:
                prog_id, _ = winreg.QueryValueEx(k, "ProgId")
            cmd = None
            for root, prefix in ((winreg.HKEY_CLASSES_ROOT, prog_id),
                                 (winreg.HKEY_CURRENT_USER, rf"Software\Classes\{prog_id}")):
                try:
                    with winreg.OpenKey(root, rf"{prefix}\shell\open\command") as k:
                        cmd, _ = winreg.QueryValueEx(k, "")
                        break
                except OSError:
                    continue
            if not cmd:
                return None
            # template contains %1 (or empty) — strip it; shlex splits the
            # quoted exe path correctly
            cmd = cmd.replace("%1", "").replace("%*", "").strip()
            parts = shlex.split(cmd, posix=False)
            parts = [p.strip('"') for p in parts if p.strip('"')]
            return parts or None
        except Exception:
            return None

    @classmethod
    def _open_url(cls, url: str):
        """Open a URL in the user's browser AND bring the browser window to
        the foreground.

        Route 1 (new, user-reported fix): launch the default browser's exe
        directly with the URL as an argument — window raises even when the
        browser is already running (ShellExecute/startfile leave a running
        browser in the background behind focus-stealing prevention).
        Route 2: os.startfile / ShellExecuteW (works when no browser is
        running, or the exe launch failed).
        Route 3: cmd start (last resort). All failures are shown, never
        swallowed (the original 'links do nothing' bug on LTSC)."""
        import os
        import subprocess
        # Route 1: direct browser launch (raises window if already running)
        browser_cmd = cls._resolve_default_browser_cmd()
        if browser_cmd:
            try:
                subprocess.Popen([*browser_cmd, url],
                                  creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                return
            except Exception:
                pass  # fall through to the shell routes
        # Route 2: shell default handler
        try:
            os.startfile(url)
            return
        except Exception:
            pass
        # Route 3: explicit shell open
        try:
            subprocess.Popen(["cmd", "/c", "start", "", url], shell=False,
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            return
        except Exception as exc:
            messagebox.showerror("Could Not Open Link",
                                 f"Couldn't open this page:\n{url}\n\n{exc}")

    def _set_category(self, cat, on: bool):
        from app.app_catalog import APP_CATALOG
        for app in APP_CATALOG:
            if app["category"] == cat and app["id"] in self.vars:
                self.vars[app["id"]].set(on)
        try:
            self._refresh_install_count()
        except Exception:
            pass


# --------------------------------------------------------------------------- #
# Main window
# --------------------------------------------------------------------------- #

class BorderlessChrome:
    """Custom chrome for the MAIN window (user redesign 2026-09): no
    native title strip, rounded corners, in-window — ❐ ✕ buttons.

    Technique (same as professional apps on tkinter):
      * overrideredirect(True) — borderless; a WS_EX_APPWINDOW style fix
        keeps the taskbar button + Alt-Tab entry + taskbar icon preview
      * four lifted corner-mask canvases painted with the window's
        transparent key color cut real rounded corners — every existing
        widget stays packed to root (zero reparenting, zero layout churn)
      * hand-rolled drag (title bar area) and 8-direction resize grips
      * maximize fills the work area (not covering the taskbar);
        restore puts the window exactly back

    Headless-safe: chrome engages lazily on the first <Map> event, so a
    withdrawn root (smoke harness, capture tools) never flashes or
    mis-binds. Every operation is best-effort with a hard fallback to
    the native title bar (borderless simply stays off).
    """

    GRIP = 6          # resize edge thickness
    RADIUS = 12       # corner radius (window)
    TITLE_H = 40      # custom title bar height

    def __init__(self, root, app):
        self.root = root
        self.app = app
        self.engaged = False
        self.maximized = False
        self._pre_max = None      # (x, y, w, h) before maximizing
        self._drag = None
        self._resize = None
        self._transkey = None
        self._btns = {}
        self._corners = []
        self._titlebar = None
        self._engaging = False
        self._conf_after = None        # coalesced layout pass (perf fix)
        self._last_root_box = None
        # engage lazily: the harness withdraws the root before mainloop,
        # and <Map> only fires when the window actually shows
        try:
            self._map_bind_id = root.bind("<Map>", self._on_first_map, add="+")
        except Exception:
            self._map_bind_id = None

    def _apply_appwindow_style(self):
        """WS_EX_APPWINDOW: keep the taskbar button + Alt-Tab entry
        alive on a borderless window. Best-effort, Windows only."""
        try:
            import ctypes
            GWL_EXSTYLE = -20
            WS_EX_APPWINDOW = 0x00040000
            hwnd = ctypes.windll.user32.GetParent(self.root.winfo_id()) \
                or self.root.winfo_id()
            style = ctypes.windll.user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
            if not style & WS_EX_APPWINDOW:
                ctypes.windll.user32.SetWindowLongW(
                    hwnd, GWL_EXSTYLE, style | WS_EX_APPWINDOW)
        except Exception:
            pass

    # ---- engagement ---------------------------------------------------- #

    def _on_first_map(self, _event=None):
        if self.engaged or self._engaging:
            return
        try:
            if not self.root.winfo_ismapped():
                return
        except Exception:
            return
        # re-entrancy guard: _engage() itself withdraws/deiconifies,
        # which fires more <Map>/<Unmap> events — the flag must be up
        # BEFORE the first withdraw, or this handler re-fires forever
        self._engaging = True
        try:
            self._engage()
        except Exception:
            self.engaged = False   # native chrome stays — safe fallback
        finally:
            self._engaging = False

    def _engage(self):
        root = self.root
        # stop listening FIRST (the withdraw/deiconify dance below re-fires
        # <Map> — we must not still be subscribed when it does)
        try:
            root.unbind("<Map>", self._map_bind_id)
        except Exception:
            pass
        # 1) borderless + keep taskbar presence (WS_EX_APPWINDOW = 0x40000)
        root.withdraw()
        root.overrideredirect(True)
        self._apply_appwindow_style()
        # transparent key: distinct from modal keys, never equals a real
        # theme color (all theme colors have a channel >= 0x0D)
        self._transkey = "#010409"
        try:
            root.attributes("-transparentcolor", self._transkey)
        except Exception:
            # no transparency support: rounded corners would show black
            # notches — keep the borderless bar but square corners
            self._transkey = COLORS["bg"]
        root.deiconify()

        # 2) rounded corner masks (lifted over ALL content)
        self._corners = []
        r = self.RADIUS if self._transkey != COLORS["bg"] else 0
        if r:
            for corner in ("nw", "ne", "sw", "se"):
                c = tk.Canvas(root, width=r + 2, height=r + 2,
                              bg=self._transkey, highlightthickness=0, bd=0)
                self._corners.append(c)
            self._layout_corners()
            for c in self._corners:
                # Tk 9: canvas 'raise' subcommand takes an ITEM id; the
                # stacking call must go through the raw path (lift() on a
                # Canvas is item-lift on both 8.6 and 9.0)
                try:
                    c.tk.call('raise', c._w)
                except Exception:
                    pass

        # 3) custom title bar (built once; _build_main_ui packs below it)
        self._build_titlebar()

        # 4) resize grips — invisible edge frames, topmost below corners
        self._build_grips()

        # 5) follow size changes: re-mask corners, keep grips glued
        root.bind("<Configure>", self._on_configure, add="+")

        self.engaged = True
        self._on_configure()

    def _layout_corners(self):
        """Corner masks for the rounded window (user bug report fix, v3).

        Each mask is a small canvas lifted over one window corner whose
        OWN background is the window's transparent key color (that bg is
        the cut), with ONE opaque disc of radius r painted on top,
        centered r pixels INWARD (diagonally) from the window corner.
        The arc then passes exactly THROUGH the corner pixel: inside
        the arc = dark bg (visible window), outside = key-transparent
        (the rounded cut). This is the classic corner-mask construction.

        Per-corner geometry (canvas size C = r+2, placed with a 2px
        outward bleed on the window's side(s)):
          NW at (-2,-2):       window corner = canvas (2,2);
              inward dir (+1,+1) -> disc center (2+r, 2+r)
          NE at (w-r, -2):     window corner = canvas (C,2)=(r,2)... the
              canvas spans window x w-r-? -> place x=w-r means canvas
              covers window x from w-r to w+2, so corner w = canvas
              (r, 2); inward dir (-1,+1) -> center (r-r, 2+r) = (0, 2+r)
          SW at (-2, h-r):     corner = canvas (2, r); inward (+1,-1)
              -> center (2+r, r-r) = (2+r, 0)
          SE at (w-r, h-r):    corner = canvas (r, r); inward (-1,-1)
              -> center (0, 0)
        (History: v1 drew inverted transparent wedges — the reported
        'flipped triangles'; v2 centered discs ON the corner pixel,
        which covers the whole corner square = no rounding at all.
        Pixel-sampled GDI verification drove both fixes.)
        """
        try:
            r = self.RADIUS
            w = self.root.winfo_width()
            h = self.root.winfo_height()
            C = r + 2
            nw, ne, sw, se = self._corners
            nw.place(x=-2, y=-2)
            ne.place(x=w - r, y=-2)
            sw.place(x=-2, y=h - r)
            se.place(x=w - r, y=h - r)
            # disc centers: corner pixel + r * inward unit diagonal
            centers = (
                (2 + r, 2 + r),        # NW
                (r - r, 2 + r),        # NE  -> (0, 2+r)
                (2 + r, r - r),        # SW  -> (2+r, 0)
                (r - r, r - r),        # SE  -> (0, 0)
            )
            for c, (cx, cy) in zip(self._corners, centers):
                c.delete("all")
                c.create_oval(cx - r, cy - r, cx + r, cy + r,
                              fill=COLORS["bg"], outline="")
        except Exception:
            pass

    # ---- custom title bar ----------------------------------------------- #

    def _build_titlebar(self):
        root = self.root
        bar = self._titlebar = tk.Frame(root, bg=COLORS["bg"], height=self.TITLE_H)
        # packed LAST in _build_main_ui order? No — place() keeps it
        # independent of pack order, always at the very top:
        bar.place(x=0, y=0, relwidth=1.0, height=self.TITLE_H)
        bar.lift()

        # app icon: the REAL icon.ico (soap bubbles 🧼, user bug report —
        # the placeholder green dot is gone), 24px in the 40px bar. Held
        # on self so Tk never garbage-collects it; any failure falls back
        # to the drawn dot so the bar always has a mark.
        self._title_icon_img = None
        try:
            self._title_icon_img = _load_title_icon(24)
        except Exception:
            self._title_icon_img = None
        if self._title_icon_img is not None:
            try:
                tk.Label(bar, image=self._title_icon_img, bg=COLORS["bg"],
                         bd=0, highlightthickness=0).pack(
                             side="left", padx=(12, 6), pady=12)
            except Exception:
                self._title_icon_img = None
        if self._title_icon_img is None:
            icon = tk.Canvas(bar, width=16, height=16, bg=COLORS["bg"],
                             highlightthickness=0, bd=0)
            try:
                icon.create_oval(2, 2, 14, 14, fill=COLORS["accent_green"],
                                 outline="")
                icon.create_oval(5, 5, 11, 11, fill=COLORS["bg"], outline="")
                icon.create_oval(6.5, 6.5, 9.5, 9.5, fill=COLORS["accent_green"],
                                 outline="")
            except Exception:
                pass
            icon.pack(side="left", padx=(14, 8), pady=12)

        self._title_lbl = tk.Label(bar, text=APP_NAME, font=(F, 10, "bold"),
                                   bg=COLORS["bg"], fg=COLORS["text"])
        self._title_lbl.pack(side="left", pady=(0, 0))

        # window buttons: minimize, maximize, close (X = red on hover)
        def _mkbtn(sym, tip):
            b = tk.Label(bar, text=sym, font=(F, 10, "bold"),
                         bg=COLORS["bg"], fg=COLORS["subtext"],
                         cursor="hand2", padx=10, pady=8)
            b.bind("<Enter>", lambda _e: b.config(fg=COLORS["text"]))
            b.bind("<Leave>", lambda _e: b.config(fg=COLORS["subtext"]))
            try:
                Tooltip(b, tip)
            except Exception:
                pass
            return b

        def _minimize():
            # Tk 9: an override-redirect window can't be iconified —
            # drop the flag for the duration; _on_restore re-arms it
            try:
                root.overrideredirect(False)
            except Exception:
                pass
            try:
                root.iconify()
            except Exception:
                pass

        close = _mkbtn("✕", "Close")
        close.bind("<Enter>", lambda _e: close.config(fg=COLORS["accent_red"]))
        close.bind("<Leave>", lambda _e: close.config(fg=COLORS["subtext"]))
        close.bind("<Button-1>", lambda _e: self.app._on_close())
        close.pack(side="right", padx=(0, 8), pady=6)
        maxb = _mkbtn("❐", "Maximize")
        maxb.bind("<Button-1>", lambda _e: self.toggle_maximize())
        maxb.pack(side="right", padx=4, pady=6)
        minb = _mkbtn("—", "Minimize")
        minb.bind("<Button-1>", lambda _e: _minimize())
        minb.pack(side="right", padx=4, pady=6)
        self._btns = {"min": minb, "max": maxb, "close": close}

        # restore path: when the user clicks the taskbar button, Windows
        # remaps the window — re-arm borderless + restack chrome, then
        # reapply the taskbar style (it survives iconify but be safe).
        # PERF FIX: <Map> fires for every CHILD widget too (a grid build
        # maps hundreds) — filter to the ROOT window's own map or the
        # overrideredirect+ctypes calls run per-widget (measured: 393
        # style calls during one prewarm).
        def _on_restore(e=None):
            try:
                if e is not None and e.widget is not root:
                    return
                if root.state() != "iconic" and self.engaged:
                    root.overrideredirect(True)
                    self._apply_appwindow_style()
                    self._layout_chrome()
            except Exception:
                pass
        try:
            root.bind("<Map>", _on_restore, add="+")
        except Exception:
            pass

        # drag = anywhere on the bar that isn't a button; double-click
        # maximizes (native habit)
        bar.bind("<Button-1>", self._drag_press, add="+")
        bar.bind("<B1-Motion>", self._drag_move, add="+")
        bar.bind("<ButtonRelease-1>", lambda _e: self._drag_set(None), add="+")
        bar.bind("<Double-Button-1>", lambda _e: self.toggle_maximize(), add="+")
        self._title_lbl.bind("<Button-1>", self._drag_press, add="+")
        self._title_lbl.bind("<B1-Motion>", self._drag_move, add="+")
        self._title_lbl.bind("<ButtonRelease-1>", lambda _e: self._drag_set(None), add="+")
        self._title_lbl.bind("<Double-Button-1>", lambda _e: self.toggle_maximize(), add="+")

    # ---- drag / resize --------------------------------------------------- #

    def _drag_set(self, v):
        self._drag = v

    def _drag_press(self, e):
        if self.maximized:
            self._drag = None
            return
        self._drag = {"dx": e.x - 0, "dy": e.y - 0,
                      "wx": self.root.winfo_x(), "wy": self.root.winfo_y(),
                      "px": e.x_root, "py": e.y_root}

    def _drag_move(self, e):
        d = self._drag
        if not d:
            return
        try:
            # move by pointer delta (robust against the 1-frame lag of
            # winfo_x under rapid drags)
            nx = d["wx"] + (e.x_root - d["px"])
            ny = d["wy"] + (e.y_root - d["py"])
            self.root.geometry(f"+{nx}+{ny}")
        except Exception:
            pass

    def toggle_maximize(self):
        try:
            root = self.root
            if not self.maximized:
                # remember, then fill the WORK area (taskbar stays visible)
                self._pre_max = (root.winfo_x(), root.winfo_y(),
                                 root.winfo_width(), root.winfo_height())
                try:
                    import ctypes
                    wa = ctypes.wintypes.RECT()
                    hwnd = ctypes.windll.user32.GetParent(root.winfo_id()) or root.winfo_id()
                    if ctypes.windll.user32.SystemParametersInfoW(
                            48, 0, ctypes.byref(wa), 0):   # SPI_GETWORKAREA
                        root.geometry(f"{wa.right - wa.left}x{wa.bottom - wa.top}"
                                      f"+{wa.left}+{wa.top}")
                    else:
                        raise RuntimeError("no work area")
                except Exception:
                    root.wm_state("zoom")
                self.maximized = True
            else:
                x, y, w, h = self._pre_max or (60, 60, 1040, 800)
                root.geometry(f"{w}x{h}+{x}+{y}")
                self.maximized = False
        except Exception:
            pass

    def _build_grips(self):
        root = self.root
        g = self.GRIP
        # 8 invisible resize grips along the edges/corners
        specs = {
            "n": (0, 0, 1.0, 0, "n", "sb_v_double_arrow", "ns"),
            "s": (0, 1.0 - 0, 1.0, 0, "s", "sb_v_double_arrow", "ns"),
            "e": (1.0 - 0, 0, 0, 1.0, "e", "sb_h_double_arrow", "ew"),
            "w": (0, 0, 0, 1.0, "w", "sb_h_double_arrow", "ew"),
        }
        self._grip_widgets = {}
        for name, (rx, ry, rw, rh, side, cur, axis) in specs.items():
            f = tk.Frame(root, bg=COLORS["bg"], cursor=cur)
            self._grip_widgets[name] = f
            self._wire_grip(f, side, axis)
        # corners get diagonal cursors
        corners = {
            "nw": ("size_nw_se", "nw"), "ne": ("size_ne_sw", "ne"),
            "sw": ("size_sw_ne", "sw"), "se": ("size_se_nw", "se"),
        }
        for name, (cur, side) in corners.items():
            f = tk.Frame(root, bg=COLORS["bg"])
            try:
                f.config(cursor=cur)
            except Exception:
                pass          # exotic cursor names vary by platform
            self._grip_widgets[name] = f
            self._wire_grip(f, side, None, diagonal=name)
        self._layout_grips()

    def _layout_grips(self):
        try:
            g = self.GRIP
            gw = {}
            for name, f in (self._grip_widgets or {}).items():
                gw[name] = f
            # edges
            gw["n"].place(x=0, y=0, relwidth=1.0, height=g)
            gw["s"].place(x=0, rely=1.0, y=-g, relwidth=1.0, height=g)
            gw["w"].place(x=0, y=0, width=g, relheight=1.0)
            gw["e"].place(relx=1.0, x=-g, y=0, width=g, relheight=1.0)
            # corners (over the edge strips)
            gw["nw"].place(x=0, y=0, width=g * 2, height=g * 2)
            gw["ne"].place(relx=1.0, x=-g * 2, y=0, width=g * 2, height=g * 2)
            gw["sw"].place(x=0, rely=1.0, y=-g * 2, width=g * 2, height=g * 2)
            gw["se"].place(relx=1.0, x=-g * 2, rely=1.0, y=-g * 2,
                           width=g * 2, height=g * 2)
            # grips must NOT eat clicks meant for the title bar buttons:
            # n-grip under the title bar but thin; buttons sit at the top
            # RIGHT — carve the n/e/w corner grips around the bar area
            for f in gw.values():
                f.lift()
            if self._titlebar is not None:
                self._titlebar.lift()
            for c in self._corners:
                try:
                    c.tk.call('raise', c._w)   # Tk9-safe widget stacking
                except Exception:
                    pass
        except Exception:
            pass

    def _wire_grip(self, f, side, axis, diagonal=None):
        st = {"x": 0, "y": 0, "w": 0, "h": 0, "sx": 0, "sy": 0}

        def press(e):
            if self.maximized:
                return
            st["sx"], st["sy"] = e.x_root, e.y_root
            st["x"], st["y"] = self.root.winfo_x(), self.root.winfo_y()
            st["w"], st["h"] = self.root.winfo_width(), self.root.winfo_height()

        def move(e):
            if self.maximized:
                return
            dx = e.x_root - st["sx"]
            dy = e.y_root - st["sy"]
            sides = [side]
            if diagonal:
                sides = [side[0], side[1]]          # 'nw' -> ['n','w']
            try:
                minw, minh = WINDOW_MIN_SIZE
                nx, ny = st["x"], st["y"]
                nw, nh = st["w"], st["h"]
                if "e" in sides:
                    nw = max(minw, st["w"] + dx)
                if "s" in sides:
                    nh = max(minh, st["h"] + dy)
                if "w" in sides:
                    nw = max(minw, st["w"] - dx)
                    nx = st["x"] + (st["w"] - nw)
                if "n" in sides:
                    nh = max(minh, st["h"] - dy)
                    ny = st["y"] + (st["h"] - nh)
                self.root.geometry(f"{nw}x{nh}+{nx}+{ny}")
            except Exception:
                pass

        f.bind("<Button-1>", lambda e: press(e), add="+")
        f.bind("<B1-Motion>", lambda e: move(e), add="+")

    # ---- geometry follow -------------------------------------------------- #

    def restack_chrome(self):
        """Re-assert chrome stacking above freshly built content.

        _build_main_ui destroys and rebuilds all content (admin-gate ->
        limited flow, tab rebuilds). New siblings stack ABOVE the chrome
        pieces created earlier (grips, corner masks, title bar), burying
        the bottom corner masks under the content host — the 'bottom
        corners not rounded' bug. Called after every content rebuild so
        z-order always ends: content < titlebar < grips < corners."""
        try:
            if not self.engaged:
                return
            if self._titlebar is not None:
                self._titlebar.lift()
            for f in (self._grip_widgets or {}).values():
                try:
                    f.lift()
                except Exception:
                    pass
            for c in (self._corners or []):
                try:
                    c.tk.call('raise', c._w)   # Tk9-safe widget stacking
                except Exception:
                    pass
        except Exception:
            pass

    def _on_configure(self, event=None):
        """Root-size follower (PERF-CRITICAL FIX, user bug report).

        The old version ran the full corner+grip re-place on EVERY
        <Configure> — but <Configure> fires for every CHILD widget that
        maps, moves or resizes (a grid build = hundreds of them), so a
        Tweak/undo prewarm triggered ~900 layout passes x 13 place/lift
        calls each, measured 3.4 s inside tkraise alone and ~22 s of
        frozen UI across the startup pre-warm chain.

        Correct contract: only the ROOT window's own geometry matters,
        and only when it actually changed; passes coalesce to one per
        idle tick (bursty child-configure storms land as a single
        deferred layout)."""
        try:
            if not self.engaged and self._titlebar is None:
                return
            if event is not None and event.widget is not self.root:
                return          # child Configure — not our window
            box = None
            try:
                box = (self.root.winfo_width(), self.root.winfo_height(),
                       self.root.winfo_x(), self.root.winfo_y())
            except Exception:
                pass
            if box is not None and box == self._last_root_box:
                return          # nothing about the window changed
            self._last_root_box = box
            # coalesce: one layout pass per idle turn, never per event
            if self._conf_after is None:
                try:
                    self._conf_after = self.root.after_idle(self._layout_chrome)
                except Exception:
                    self._conf_after = None
        except Exception:
            pass

    def _layout_chrome(self):
        """Deferred single layout pass (corners + grips)."""
        self._conf_after = None
        try:
            if not self.engaged and self._titlebar is None:
                return
            self._layout_corners()
            self._layout_grips()
        except Exception:
            pass


class QuickToolsDialog(ThemedModal):
    """Quick Tools popup (6th Tools card): the exact shortcut box that
    used to sit inline below the 4 cards — same 2 columns (Windows
    tools / Windows Settings), same bullet rows. Same
    popup dimensions as every other feature dialog (ThemedModal
    800x600 default — no custom size). Read-only launcher, no busy
    guard (mirrors card clicks, never touches the run engine)."""

    def __init__(self, parent, app, open_target):
        self.app = app
        self._open_target = open_target
        # NOTE: no size= passed — ThemedModal WIDTHxHEIGHT default keeps
        # this popup at the exact same dimensions as DNS/Health/etc.
        super().__init__(parent, title="Quick Tools",
                         accent=TAB_ACCENTS["Tools"])
        body = self.body
        try:
            tk.Label(body, text="Windows tools and settings.",
                     font=(F, 9), bg=COLORS["bg"],
                     fg=COLORS["subtext"], wraplength=680,
                     justify="left").pack(anchor="w", pady=(0, 8))
        except Exception:
            pass
        try:
            cols = tk.Frame(body, bg=COLORS["bg"])
            cols.pack(fill="both", expand=True)
            for ci in (0, 1):
                cols.grid_columnconfigure(ci, weight=1, uniform="toolcols")
            for ci, (group, items) in enumerate(ToolsTab._SHORTCUTS):
                self._build_column(cols, ci, group, items)
        except Exception:
            pass

    def _build_column(self, parent, col, title, items):
        cell = tk.Frame(parent, bg=COLORS["bg"])
        cell.grid(row=0, column=col, sticky="new", padx=8)
        tk.Label(cell, text=title, font=(F, 10, "bold"),
                 bg=COLORS["bg"], fg=COLORS["text"],
                 anchor="w").pack(fill="x")
        tk.Frame(cell, bg=COLORS["hairline"], height=1).pack(
            fill="x", pady=(4, 6))
        for label, target in items:
            self._make_bullet(cell, label, target)

    def _make_bullet(self, parent, label, target):
        row = tk.Frame(parent, bg=COLORS["bg"])
        row.pack(fill="x", pady=2)
        dot = tk.Label(row, text="•", font=(F, 9, "bold"),
                       bg=COLORS["bg"], fg=TAB_ACCENTS["Tools"],
                       bd=0, highlightthickness=0)
        dot.pack(side="left", padx=(2, 4))
        lbl = tk.Label(row, text=label, font=(F, 9, "bold"),
                       bg=COLORS["bg"], fg=COLORS["text"],
                       cursor="hand2", anchor="w",
                       bd=0, highlightthickness=0)
        lbl.pack(side="left", fill="x", expand=True)
        for w in (row, dot, lbl):
            w.bind("<Enter>", lambda e, l=lbl: l.config(
                fg=TAB_ACCENTS["Tools"]), add="+")
            w.bind("<Leave>", lambda e, l=lbl: l.config(
                fg=COLORS["text"]), add="+")
            w.bind("<Button-1>",
                   lambda e, t=target: self._launch(t), add="+")

    def _launch(self, target):
        try:
            self._open_target(target)
        except Exception:
            pass


class GaugeDial(tk.Canvas):
    """Semicircular speed gauge — house custom-draw style (smooth
    polylines + _hex_lerp bands, like AnimatedButton/ProgressBar).

    Log scale 0..1000 Mbps so slow links stay readable; the needle
    eases toward set_value() on a 16ms tick. Tk-thread only (the
    dialog hops worker progress through _ui). Best-effort: any draw
    failure leaves the last good frame, never raises."""

    MAX_MBPS = 1000.0
    # Evenly-spaced tick table (Ookla-style): one equal-angle step per
    # tick, so the dial reads symmetrically. fraction_for() lerps
    # piecewise between them — needle, sweep and ticks all share it.
    TICKS = (0, 5, 10, 50, 100, 250, 500, 750, 1000)

    def __init__(self, parent, accent=None, width=360, height=215):
        super().__init__(parent, width=width, height=height,
                         highlightthickness=0, bd=0, bg=COLORS["bg"])
        self._accent = accent or TAB_ACCENTS["Tools"]
        # NOTE: _w/_h are tkinter's reserved widget-path attrs (clobbering
        # _w breaks every later Tk call — same trap as AnimatedButton);
        # pixel size lives under _bw/_bh.
        self._bw, self._bh = width, height
        self._shown = 0.0
        self._target = 0.0
        self._anim_after = None
        try:
            self.bind("<Destroy>", self._on_destroy, add="+")
        except Exception:
            pass
        self._draw()

    @staticmethod
    def fraction_for(mbps, cap=MAX_MBPS):
        """Tick-table 0..1 (pure — unit-tested): equal angle per tick,
        piecewise-linear between tick values."""
        try:
            v = max(0.0, float(mbps))
        except Exception:
            return 0.0
        try:
            ticks = GaugeDial.TICKS
            if v <= ticks[0]:
                return 0.0
            if v >= ticks[-1]:
                return 1.0
            n = len(ticks) - 1
            for i in range(n):
                lo, hi = ticks[i], ticks[i + 1]
                if lo <= v <= hi:
                    t = (v - lo) / (hi - lo) if hi > lo else 0.0
                    return (i + max(0.0, min(1.0, t))) / n
            return 1.0
        except Exception:
            return 0.0

    def set_value(self, mbps):
        try:
            v = max(0.0, float(mbps))
        except Exception:
            v = 0.0
        self._target = v
        self._kick()

    def reset(self):
        self._shown = 0.0
        self._target = 0.0
        try:
            if self._anim_after is not None:
                self.after_cancel(self._anim_after)
                self._anim_after = None
        except Exception:
            pass
        self._draw()

    def _kick(self):
        if self._anim_after is not None:
            return
        self._tick()

    def _tick(self):
        self._anim_after = None
        try:
            if not self.winfo_exists():
                return
        except Exception:
            return
        try:
            diff = self._target - self._shown
            if abs(diff) < 0.15 and abs(diff) < max(0.5, self._target * 0.01):
                self._shown = self._target
                self._draw()
                return
            self._shown = self._shown + diff * 0.22
            self._draw()
        except Exception:
            return
        try:
            self._anim_after = self.after(16, self._tick)
        except Exception:
            self._anim_after = None

    def _on_destroy(self, _e=None):
        try:
            if self._anim_after is not None:
                self.after_cancel(self._anim_after)
        except Exception:
            pass
        self._anim_after = None

    def _point(self, cx, cy, radius, frac):
        import math
        theta = math.pi * (1.0 - max(0.0, min(1.0, frac)))
        return (cx + radius * math.cos(theta), cy - radius * math.sin(theta))

    def _arc_points(self, cx, cy, radius, frac_to, steps=60):
        pts = []
        for i in range(int(steps * max(0.0, min(1.0, frac_to))) + 1):
            f = (i / steps)
            pts.extend(self._point(cx, cy, radius, f))
        return pts

    def _draw(self):
        try:
            self.delete("all")
        except Exception:
            return
        try:
            import math
            w, h = self._bw, self._bh
            cx, cy = w / 2.0, h - 22.0
            radius = min(w / 2.0 - 34.0, h - 58.0)
            # track (full sweep, hairline-bright)
            track = self._arc_points(cx, cy, radius, 1.0)
            if len(track) >= 4:
                self.create_line(*track, fill=COLORS["hairline"], width=10,
                                 capstyle="round", smooth=True)
            # value sweep: solid Tools-pink (user call) with a radiant
            # under-glow (wide dim pass + bright core — Tk has no
            # blur/alpha, so glow is faked the house way with layered
            # smooth polylines, like the Ookla dial).
            # Sweep fix (user bug: grainy bar, unfilled start): the sweep
            # is ONE continuous polyline, never chunked — chunk joints
            # antialias into seams/speckle and caps read as dots. Round
            # caps + a dot on BOTH ends bury the track's own overhang so
            # the fill starts flush at the arc base.
            frac = self.fraction_for(self._shown)
            if frac > 0.005:
                npts = 90
                pts = []
                for i in range(npts + 1):
                    pts.extend(self._point(cx, cy, radius, frac * i / npts))
                if len(pts) >= 4:
                    try:
                        self.create_line(
                            *pts, fill=_hex_lerp(self._accent, COLORS["bg"], 0.62),
                            width=11, capstyle="round", smooth=True)
                    except Exception:
                        pass
                    try:
                        self.create_line(*pts, fill=self._accent, width=6,
                                         capstyle="round", smooth=True)
                    except Exception:
                        pass
                try:
                    sx, sy = self._point(cx, cy, radius, 0.0)
                    self.create_oval(sx - 3, sy - 3, sx + 3, sy + 3,
                                     fill=self._accent, outline="")
                    hx, hy = self._point(cx, cy, radius, frac)
                    hr = 4
                    self.create_oval(hx - hr, hy - hr, hx + hr, hy + hr,
                                     fill=self._accent,
                                     outline="")
                except Exception:
                    pass
            # ticks + labels (evenly spaced — one step per tick; large
            # labels parked well clear, user call). Ticks end a full 10px
            # inside the glow edge and labels sit 22px further in still,
            # so lines never touch numerals at any font scale.
            for t in self.TICKS:
                f = self.fraction_for(float(t))
                x0, y0 = self._point(cx, cy, radius - 20.0, f)
                x1, y1 = self._point(cx, cy, radius - 10.0, f)
                try:
                    self.create_line(x0, y0, x1, y1, fill=COLORS["subtext"], width=2)
                    lx, ly = self._point(cx, cy, radius - 42.0, f)
                    self.create_text(lx, ly, text=f"{t}", font=(F, 9),
                                     fill=COLORS["text"])
                except Exception:
                    pass
            # needle + pink hub (user call: the joint stays Tools-pink
            # while the sweep carries the spectrum)
            try:
                nx, ny = self._point(cx, cy, radius - 16.0, frac)
                self.create_line(cx, cy, nx, ny, fill=COLORS["text"], width=3,
                                 capstyle="round")
                r = 7
                self.create_oval(cx - r, cy - r, cx + r, cy + r,
                                 fill=TAB_ACCENTS["Tools"], outline="")
            except Exception:
                pass
        except Exception:
            pass


class SpeedTestDialog(ThemedModal):
    """Speed Test popup (5th Tools card): animated gauge dial + live
    Download / Upload / Ping rows, like speed-test websites.

    Same popup dimensions as every other feature dialog (ThemedModal
    800x600 default — no custom size) and the same worker/hop/
    watchdog/cancel skeleton as DnsTesterDialog. Read-only: no
    Apply, no snapshot — Re-test only. Legs run sequentially (ping,
    then download, then upload — parallel legs would fight over the
    same pipe). Unreachable legs show '—', never a fake 0. No API
    keys on any path (app/speed_test.py)."""

    LEGS = (("ping", "Ping"), ("download", "Download"), ("upload", "Upload"))

    def __init__(self, parent, app):
        self.app = app
        self._stop_token = [False]
        self._results = {}          # leg -> float | None (landed)
        self._finished = False
        self._scan_gen = 0
        self._watchdog_after = None
        self._row_values = {}
        self._server_name = None
        # NOTE: no size= passed — exact same dimensions as DNS/Health/etc.
        super().__init__(parent, title="Speed Test",
                         accent=TAB_ACCENTS["Tools"])
        body = self.body

        tk.Label(body, text="Tests your connection speed — download, "
                            "upload and ping.",
                 font=(F, 9), bg=COLORS["bg"],
                 fg=COLORS["subtext"], wraplength=680,
                 justify="left").pack(anchor="w")

        # gauge + headline readout (centered, like the websites).
        # Compact by design: the 800x600 card is fixed and system fonts
        # vary, so every pad here is tight — the Re-test row must stay
        # visible without scrolling (user bug: Upload clipped, no
        # Re-test in reach). The gauge IS the progress signal, so no
        # separate progress bar (DNS keeps its own).
        gwrap = tk.Frame(body, bg=COLORS["bg"])
        gwrap.pack(fill="x", pady=(1, 0))
        self._gauge = GaugeDial(gwrap, accent=TAB_ACCENTS["Tools"],
                                width=360, height=225)
        self._gauge.pack(anchor="center")
        # No unit sub-line (user call): the headline already reads
        # "141 Mbps" via format_mbps — the freed row funds a bigger
        # gauge instead.
        self._big_lbl = tk.Label(gwrap, text="—", font=(F, 18, "bold"),
                                 bg=COLORS["bg"], fg=COLORS["text"])
        self._big_lbl.pack(anchor="center")

        # usage suitability (Ookla-style): 4 icons with 5 stars under
        # each — Browsing / Gaming / Streaming / Video call. PNGs from
        # app/assets (bundled into the exe); emoji fallback when a file
        # is missing. Stars repaint from results; tooltips read
        # "Streaming — Great" / "Browsing — Slow".
        self._usage = {}
        self._usage_imgs = {}
        urow = tk.Frame(body, bg=COLORS["bg"])
        urow.pack(fill="x", pady=(2, 0))
        for ci in range(4):
            urow.grid_columnconfigure(ci, weight=1, uniform="usecols")
        try:
            from app.speed_test import USAGE_ORDER as _UO
        except Exception:
            _UO = ("browsing", "gaming", "streaming", "videocall")
        for ci, use in enumerate(_UO):
            cell = tk.Frame(urow, bg=COLORS["bg"])
            cell.grid(row=0, column=ci, sticky="n")
            img = self._load_usage_icon(use)
            if img is not None:
                icon_lbl = tk.Label(cell, image=img, bg=COLORS["bg"],
                                    bd=0, highlightthickness=0)
            else:
                try:
                    from app.speed_test import USAGE_FALLBACK_EMOJI as _UFE
                    _emo = _UFE.get(use, "•")
                except Exception:
                    _emo = "•"
                icon_lbl = tk.Label(cell, text=_emo, font=("Segoe UI Emoji", 20),
                                    bg=COLORS["bg"], fg=COLORS["text"])
            icon_lbl.pack(anchor="center")
            dots = tk.Label(cell, text="☆☆☆☆☆", font=(F, 8, "bold"),
                            bg=COLORS["bg"], fg=COLORS["subtext"])
            dots.pack(anchor="center")
            try:
                from app.speed_test import USAGE_TITLES as _UT
                _title = _UT.get(use, use)
            except Exception:
                _title = use
            tips = []
            for w in (cell, icon_lbl, dots):
                try:
                    tips.append(Tooltip(w, f"{_title} — testing…"))
                except Exception:
                    pass
            self._usage[use] = {"dots": dots, "tips": tips, "title": _title}
        self._reset_usage()

        self._status_lbl = tk.Label(body, text="Starting…", font=(F, 9),
                                    bg=COLORS["bg"], fg=COLORS["subtext"],
                                    anchor="w")
        self._status_lbl.pack(fill="x", pady=(2, 0))

        # single stats bar, no divider lines (user call): plain cells
        # on the dialog bg, even columns only.
        self._rows_body = tk.Frame(body, bg=COLORS["bg"])
        self._rows_body.pack(fill="x", pady=(4, 0))
        self._build_rows()

        brow = tk.Frame(body, bg=COLORS["bg"])
        brow.pack(fill="x", pady=(4, 2))
        self._retest_btn = AnimatedButton(
            brow, text="Re-test", command=self._start_test,
            bg=COLORS["surface"], fg=COLORS["text"],
            font=(F, 9, "bold"), padx=18, pady=7)
        self._retest_btn.pack(anchor="center")
        # NOTE (user call): no Tooltip here on purpose — Tooltip binds
        # <Enter>/<Leave> without add="+", which wipes AnimatedButton's
        # own hover/press animation binds. Bare, the button highlights
        # on hover and depresses on press like every other button.

        self.on_close(self._cancel_test)
        # Return re-runs (Escape still closes via ThemedModal): guarantees
        # a re-run path even on clipped/odd-DPI renders.
        try:
            self._dlg.bind("<Return>", lambda _e: self._start_test())
        except Exception:
            pass
        self._start_test()

    def _load_usage_icon(self, use):
        """~28px PhotoImage for a usage cell (512px asset subsampled;
        power-of-two factors can't hit 28 from 512, so the factor is
        computed directly). None on any failure so the caller falls
        back to emoji. Ref kept by the caller (anti-GC, tab pattern)."""
        try:
            from app.speed_test import USAGE_FILES as _UF
            fname = _UF.get(use)
            if not fname:
                return None
            from app.utils import resolve_asset_path as _rap
            path = _rap(fname)
            if not path:
                return None
            img = tk.PhotoImage(file=path)
            w, h = img.width(), img.height()
            f = max(1, int(round(min(w, h) / 28.0)))
            if f > 1:
                img = img.subsample(f, f)
            self._usage_imgs[use] = img
            return img
        except Exception:
            return None

    def _reset_usage(self):
        """Testing state: hollow stars + 'testing…' tips."""
        try:
            from app.speed_test import USAGE_ORDER as _UO
        except Exception:
            _UO = ("browsing", "gaming", "streaming", "videocall")
        for use in _UO:
            handles = (self._usage or {}).get(use)
            if not handles:
                continue
            try:
                if handles["dots"].winfo_exists():
                    handles["dots"].config(text="☆☆☆☆☆", fg=COLORS["subtext"])
            except Exception:
                pass
            for tip in handles.get("tips", []):
                try:
                    tip.text = f"{handles['title']} — testing…"
                except Exception:
                    pass

    def _paint_usage(self, finished=False):
        """Repaint stars + tooltips from self._results. Missing legs read
        'testing…' mid-run, 'No result' once finished. Tk thread only."""
        try:
            from app.speed_test import (usage_stars as _us, usage_word as _uw,
                                        USAGE_ORDER as _UO)
        except Exception:
            return
        down = self._results.get("download")
        up = self._results.get("upload")
        ms = self._results.get("ping")
        if not finished and down is None and up is None and ms is None:
            return  # nothing landed yet — keep the testing state
        for use in _UO:
            handles = (self._usage or {}).get(use)
            if not handles:
                continue
            try:
                s = _us(use, down, up, ms)
                dots_txt = "★" * max(0, min(5, s)) + "☆" * (5 - max(0, min(5, s)))
                # star scale (user call): 1 red -> 5 blue, like the gauge
                if s >= 5:
                    fg = COLORS["accent_sky"]
                elif s == 4:
                    fg = COLORS["accent_green"]
                elif s == 3:
                    fg = COLORS["accent_yellow"]
                elif s == 2:
                    fg = _hex_lerp(COLORS["accent_yellow"],
                                   COLORS["accent_red"], 0.5)
                elif s >= 1:
                    fg = COLORS["accent_red"]
                else:
                    fg = COLORS["subtext"]
                if handles["dots"].winfo_exists():
                    handles["dots"].config(text=dots_txt, fg=fg)
                for tip in handles.get("tips", []):
                    try:
                        tip.text = f"{handles['title']} — {_uw(s)}"
                    except Exception:
                        pass
            except Exception:
                pass

    # ---- lifecycle (DnsTesterDialog skeleton, read-only) ---------------- #

    def _ui(self, fn, *args):
        # F10: same as the other dialogs — no winfo_exists off-thread.
        try:
            self._dlg.after(0, lambda: fn(*args))
        except Exception:
            pass

    def _cancel_test(self):
        try:
            self._stop_token[0] = True
        except Exception:
            pass
        try:
            if self._watchdog_after is not None:
                self._dlg.after_cancel(self._watchdog_after)
        except Exception:
            pass
        self._watchdog_after = None
        try:
            self._gauge._on_destroy()
        except Exception:
            pass

    def _set_status(self, text):
        try:
            if self._status_lbl.winfo_exists():
                self._status_lbl.config(text=text)
        except Exception:
            pass

    def _build_rows(self):
        """One single stats row (user call): Ping / Download / Upload
        evenly spaced in uniform columns — each cell stacks its title
        over its value. _row_values keeps the leg -> value-label
        contract so paints are untouched."""
        for w in list(self._rows_body.winfo_children()):
            try:
                w.destroy()
            except Exception:
                pass
        self._row_values = {}
        for ci, (leg, title) in enumerate(self.LEGS):
            self._rows_body.grid_columnconfigure(ci, weight=1, uniform="statcols")
            cell = tk.Frame(self._rows_body, bg=COLORS["bg_alt"])
            cell.grid(row=0, column=ci, sticky="nsew", padx=3)
            tk.Label(cell, text=title, font=(F, 8),
                     bg=COLORS["bg_alt"], fg=COLORS["subtext"]).pack(pady=(5, 0))
            val = tk.Label(cell, text="…", font=(F, 10, "bold"),
                           bg=COLORS["bg_alt"], fg=COLORS["subtext"])
            val.pack(pady=(0, 5))
            self._row_values[leg] = val

    def _start_test(self):
        try:
            self._stop_token[0] = True
        except Exception:
            pass
        token = self._stop_token = [False]
        self._results = {}
        self._finished = False
        self._scan_gen = getattr(self, "_scan_gen", 0) + 1
        gen = self._scan_gen
        try:
            if self._watchdog_after is not None:
                self._dlg.after_cancel(self._watchdog_after)
        except Exception:
            pass
        self._watchdog_after = None
        try:
            self._gauge.reset()
            self._big_lbl.config(text="—")
            for leg in self._row_values:
                try:
                    self._row_values[leg].config(text="…", fg=COLORS["subtext"])
                except Exception:
                    pass
            self._reset_usage()
            self._set_status("Testing…")
        except Exception:
            pass
        try:
            import threading as _th
            _th.Thread(target=self._test_worker, args=(token, gen),
                       daemon=True).start()
        except Exception:
            pass
        try:
            self._watchdog_after = self._dlg.after(
                120000, lambda _g=gen: self._finish(stalled=True, _gen=_g))
        except Exception:
            pass

    def _test_worker(self, token, gen):
        def dead():
            # F10: token-only — close handlers already set token on the Tk
            # thread; winfo_exists here burned ~1s off-thread per call.
            try:
                return bool(token[0])
            except Exception:
                return True

        try:
            from app import speed_test as _st
            from app.downloader import has_network
        except Exception:
            self._ui(self._set_status, "Speed test unavailable.")
            self._ui(self._finish, False, gen)
            return
        if dead():
            return
        try:
            online = has_network(timeout=8.0)
        except Exception:
            online = False
        if not online:
            if dead():
                return
            for leg in ("ping", "download", "upload"):
                self._ui(self._paint_result, leg, None, gen)
            self._ui(self._finish, False, gen)
            return
        # — closest server race (Cloudflare anycast is usually the
        # nearest edge already; the race confirms it and names the
        # winner like other speed-test apps) — #
        if dead():
            return
        self._ui(self._set_status, "Finding closest server…")
        try:
            server_name, base_url, _race_ms = _st.race_endpoints(
                cancelled=lambda: dead())
        except Exception:
            server_name, base_url = "Cloudflare edge", _st.DOWNLOAD_URL
        self._server_name = server_name
        if dead():
            return
        # — ping (quick, no progress) — #
        self._ui(self._set_status, f"Server: {server_name} — testing ping…")
        try:
            ms = _st.ping_ms(cancelled=lambda: dead())
        except Exception:
            ms = None
        if dead():
            return
        self._ui(self._paint_result, "ping", ms, gen)
        # — download (parallel streams, adaptive rounds) — #
        if dead():
            return
        self._ui(self._set_status, f"Server: {server_name} — testing download…")
        down = self._run_parallel_rounds(_st, dead, gen, direction="download",
                                         base_url=base_url)
        if dead():
            return
        # — upload — #
        self._ui(self._set_status, f"Server: {server_name} — testing upload…")
        up = self._run_parallel_rounds(_st, dead, gen, direction="upload",
                                       base_url=base_url)
        if dead():
            return
        self._ui(self._finish, False, gen)

    def _run_parallel_rounds(self, st, dead, gen, direction="download",
                             base_url=None):
        """Parallel multi-stream rounds; paint live + final.

        One TCP stream cannot fill a fast pipe and a lone small sample
        is mostly handshake — so each round runs PARALLEL_STREAMS
        concurrent streams and aggregates wall-clock throughput. Slow
        links stop after the small round to save data; fast links run
        bigger rounds for accuracy. Returns last good value or None."""
        import threading as _th
        import time as _time
        is_down = (direction == "download")
        up_url = st.UPLOAD_URL
        if is_down:
            rounds = list(st.DOWNLOAD_ROUNDS)
        else:
            rounds = list(st.UPLOAD_ROUNDS)
        best = None
        for n in rounds:
            if dead():
                return best
            t0 = _time.monotonic()
            lock = _th.Lock()
            last_hop = [0.0]
            agg_got = [0]
            planned = n * st.PARALLEL_STREAMS

            def _prog(got, total, _t0=t0, _leg=direction):
                try:
                    with lock:
                        agg_got[0] += got
                        now = _time.monotonic()
                        if now - last_hop[0] < 0.12:
                            return
                        last_hop[0] = now
                        el = max(0.05, now - _t0)
                        live = (agg_got[0] * 8.0) / el / 1_000_000.0
                        frac = min(1.0, agg_got[0] / max(1, planned))
                    self._ui(self._paint_live, _leg, live, frac, gen)
                except Exception:
                    pass

            try:
                if is_down:
                    url = base_url or st.DOWNLOAD_URL
                    v = st.download_parallel(n, streams=st.PARALLEL_STREAMS,
                                             cancelled=lambda: dead(),
                                             progress_cb=_prog, url=url)
                    if v is None and url == st.DOWNLOAD_URL:
                        v = st.download_parallel(
                            n, streams=st.PARALLEL_STREAMS,
                            cancelled=lambda: dead(), progress_cb=_prog,
                            url=st.FALLBACK_DOWNLOAD_URL)
                else:
                    v = st.upload_parallel(n, streams=st.PARALLEL_STREAMS,
                                           cancelled=lambda: dead(), url=up_url)
            except Exception:
                v = None
            if dead():
                return best
            if v is not None:
                best = v
                self._ui(self._paint_result, direction, v, gen)
            if dead():
                return best
            # adaptive gates (data saver): the FIRST round always runs
            # (a lone small sample is handshake-heavy, never the verdict);
            # bigger rounds only when the pipe proves fast.
            if best is None:
                continue  # blocked round — try the next size once
            if n == rounds[0]:
                if best < st.ROUND_MIN_MBPS:
                    break
            elif len(rounds) > 2 and n == rounds[1]:
                if best < st.ROUND_FAST_MBPS:
                    break
        return best

    # ---- paints (Tk thread only) ---------------------------------------- #

    def _paint_live(self, leg, live_mbps, frac, _gen=None):
        if _gen is not None and _gen != getattr(self, "_scan_gen", _gen):
            return
        try:
            self._gauge.set_value(max(0.0, float(live_mbps or 0.0)))
        except Exception:
            pass
        if leg == "download":
            try:
                from app.speed_test import format_mbps as _fmt
                self._big_lbl.config(text=_fmt(live_mbps))
            except Exception:
                pass

    def _paint_result(self, leg, value, _gen=None):
        if _gen is not None and _gen != getattr(self, "_scan_gen", _gen):
            return
        self._results[leg] = value
        try:
            self._paint_usage(finished=False)
        except Exception:
            pass
        try:
            from app.speed_test import format_mbps as _fmb, format_ping as _fp
            lbl = self._row_values.get(leg)
            if lbl is not None and lbl.winfo_exists():
                if leg == "ping":
                    txt = _fp(value)
                else:
                    txt = _fmb(value)
                if value is None:
                    lbl.config(text=txt, fg=COLORS["accent_red"])
                else:
                    lbl.config(text=txt, fg=COLORS["text"])
        except Exception:
            pass
        if leg == "download":
            try:
                from app.speed_test import format_mbps as _fmb
                self._big_lbl.config(text=_fmb(value))
                self._gauge.set_value(value or 0.0)
            except Exception:
                pass
        elif leg == "upload" and value is not None:
            try:
                self._gauge.set_value(value)
            except Exception:
                pass

    def _finish(self, stalled=False, _gen=None):
        if self._finished:
            return
        if _gen is not None and _gen != getattr(self, "_scan_gen", _gen):
            return
        self._finished = True
        try:
            if self._watchdog_after is not None:
                self._dlg.after_cancel(self._watchdog_after)
        except Exception:
            pass
        self._watchdog_after = None
        try:
            from app.speed_test import format_mbps as _fmb, format_ping as _fp
            down, up, ms = (self._results.get("download"),
                            self._results.get("upload"),
                            self._results.get("ping"))
            try:
                self._paint_usage(finished=True)
            except Exception:
                pass
            if down is None and up is None and ms is None:
                self._set_status("Couldn't reach the speed test servers — "
                                 "check your connection, then Re-test."
                                 if not stalled else
                                 "Timed out — check your connection, then Re-test.")
                try:
                    self._big_lbl.config(text="—")
                except Exception:
                    pass
                return
            try:
                self._gauge.set_value(down if down is not None
                                      else (up or 0.0))
                self._big_lbl.config(text=_fmb(down))
            except Exception:
                pass
            parts = []
            try:
                if getattr(self, "_server_name", None):
                    parts.append(f"{self._server_name}")
            except Exception:
                pass
            if down is not None:
                parts.append(f"↓ {_fmb(down)}")
            if up is not None:
                parts.append(f"↑ {_fmb(up)}")
            if ms is not None:
                parts.append(f"ping {_fp(ms)}")
            missing = [n for n, v in (("download", down), ("upload", up),
                                      ("ping", ms)) if v is None]
            if missing:
                self._set_status("Partial result (" + ", ".join(missing) +
                                 " unavailable) — " + " · ".join(parts) + ".")
            else:
                self._set_status(" · ".join(parts) + ".")
        except Exception:
            pass


class ToolsTab(tk.Frame):
    """Tools tab (user redesign 2026-09): the 5th tab — pink accent.

    Six feature cards in a 3+3 grid — the exact same card size,
    gaps and grid as the Tweak tab's 6 preset cards (PER_ROW=3,
    same pack/grid/pad). The old inline shortcuts box now lives in
    the 6th card's Quick Tools popup, so the tab itself is cards
    only, like Tweak's top."""

    _CARDS = (
        ("storage", "💾", "Storage",
         "Free up disk space", TAB_ACCENTS["Clean"]),
        ("health", "🩺", "PC Health",
         "Grade your PC health", COLORS["accent_green"]),
        ("dns", "⚡", "DNS Test",
         "Find your fastest DNS", TAB_ACCENTS["Tweak"]),
        ("pilot", "🎮", "Auto-Pilot",
         "Auto-tweaks for games", TAB_ACCENTS["Tweak"]),
        ("speed", "🚀", "Speed Test",
         "Check download, upload + ping", TAB_ACCENTS["Clean"]),
        ("quick", "🧰", "Quick Tools",
         "Windows tools + settings", TAB_ACCENTS["Tools"]),
    )

    _SHORTCUTS = (
        ("Windows tools",
         (("Task Manager", "taskmgr"), ("Disk Cleanup", "cleanmgr"),
          ("Reliability Monitor", "perfmon /rel"), ("System Restore", "rstrui"),
          ("Power Options", "powercfg.cpl"), ("Device Manager", "devmgmt.msc"),
          ("System Information", "msinfo32"))),
        ("Windows Settings",
         (("Startup Apps", "ms-settings:startupapps"),
          ("Uninstall Programs", "ms-settings:appsfeatures"),
          ("Storage Sense", "ms-settings:storagesense"),
          ("Windows Update", "ms-settings:windowsupdate"),
          ("Network Settings", "ms-settings:network-status"),
          ("Bluetooth & Devices", "ms-settings:bluetooth"),
          ("Sound Settings", "ms-settings:sound"),
          ("App Volume", "ms-settings:apps-volume"),
           ("Display Settings", "ms-settings:display"),
           ("Graphics Settings", "ms-settings:display-advancedgraphics"))),
    )

    def __init__(self, parent, app):
        super().__init__(parent, bg=COLORS["bg"])
        self.app = app
        self._card_frames = {}

        self._build()

    # ---------------- structure ---------------- #

    def _build(self):
        # Six cards in a 3+3 grid — byte-identical geometry to Tweak's
        # 6 preset cards (TaskTab._build_preset_cards): same parent
        # pack (fill=x, padx=26, pady=(14, 4)), same PER_ROW=3 divmod,
        # same card grid pads (padx=5, pady=4) and uniform columns, so
        # every card lands at the exact same size + position. The old
        # top helper label is gone (Tweak has none above its cards) and
        # the shortcuts box now lives in the Quick Tools popup — the
        # tab itself is cards only.
        # (Why no topbar: keeping it would push every card down vs
        # Tweak; exact position match requires the same top offset.)
        PER_ROW = 3
        card_area = tk.Frame(self, bg=COLORS["bg"])
        card_area.pack(fill="x", padx=26, pady=(14, 4))
        self._card_area_ref = card_area
        for i, (key, icon, title, blurb, card_accent) in enumerate(self._CARDS):
            row, col = divmod(i, PER_ROW)
            self._build_card(card_area, row, col, key, icon, title, blurb,
                             card_accent)
        for c in range(PER_ROW):
            card_area.grid_columnconfigure(c, weight=1, uniform="cards")
        # Kept for the smoke harness (it scans this for shortcut rows);
        # now points at the card grid — the rows themselves live in the
        # Quick Tools popup, which the harness opens separately.
        self._panel_ref = card_area

    def _build_shortcut_column(self, parent, col, title, items):
        """One bullet column: left-aligned header + hairline divider +
        bullet rows (pink dot + bold name, Install-row language)."""
        cell = tk.Frame(parent, bg=COLORS["bg_alt"])
        cell.grid(row=0, column=col, sticky="new", padx=8)
        tk.Label(cell, text=title, font=(F, 10, "bold"),
                 bg=COLORS["bg_alt"], fg=COLORS["text"],
                 anchor="w").pack(fill="x")
        tk.Frame(cell, bg=COLORS["hairline"], height=1).pack(
            fill="x", pady=(4, 6))
        for label, target in items:
            self._make_bullet(cell, label, target)

    def _make_bullet(self, parent, label, target):
        """One bullet row: pink dot + clickable name (hand cursor, pink
        hover). True list rows — no card chrome, no cell padding beyond
        a single pixel separator (the 10-row Settings column must fit
        the page with zero scrolling; 31px rows stay roomy)."""
        row = tk.Frame(parent, bg=COLORS["bg_alt"])
        row.pack(fill="x", pady=0)
        dot = tk.Label(row, text="•", font=(F, 9, "bold"),
                       bg=COLORS["bg_alt"], fg=TAB_ACCENTS["Tools"],
                       bd=0, highlightthickness=0)
        dot.pack(side="left", padx=(2, 4))
        lbl = tk.Label(row, text=label, font=(F, 9, "bold"),
                       bg=COLORS["bg_alt"], fg=COLORS["text"],
                       cursor="hand2", anchor="w",
                       bd=0, highlightthickness=0)
        lbl.pack(side="left", fill="x", expand=True)
        for w in (row, dot, lbl):
            w.bind("<Enter>", lambda e, l=lbl: l.config(
                fg=TAB_ACCENTS["Tools"]), add="+")
            w.bind("<Leave>", lambda e, l=lbl: l.config(
                fg=COLORS["text"]), add="+")
            w.bind("<Button-1>",
                   lambda e, t=target: self._open_target(t), add="+")

    def _build_card(self, parent, rr, cc, key, icon, title, blurb,
                      card_accent):
        """One feature card — mirrors TaskTab._make_card exactly: accent-
        colored emoji icon + bold title on the face, description in a
        hover tooltip (user call 2026-09: no blurb text on cards, like
        the Clean/Repair/Tweak preset cards). Grid pads + uniform
        columns match TaskTab._build_preset_cards exactly (padx=5,
        pady=4, uniform="cards") so Tools cards sit at the same size
        + position as Tweak's 6."""
        outer = tk.Frame(parent, bg=COLORS["hairline"], bd=0)
        outer.grid(row=rr, column=cc, sticky="nsew", padx=5, pady=4)
        inner = tk.Frame(outer, bg=COLORS["bg_alt"])
        inner.pack(fill="both", expand=True, padx=1, pady=1)
        head = tk.Frame(inner, bg=COLORS["bg_alt"])
        head.pack(fill="x", padx=12, pady=(6, 6))
        icon_lbl = tk.Label(head, text=icon, font=("Segoe UI Emoji", 16),
                            bg=COLORS["bg_alt"], fg=card_accent)
        icon_lbl.pack(side="left")
        title_lbl = tk.Label(head, text=title, font=(F, 12, "bold"),
                             bg=COLORS["bg_alt"], fg=COLORS["text"])
        title_lbl.pack(side="left", padx=8)
        self._card_frames[key] = (outer, inner)

        if blurb:
            # one Tooltip per card part, same text: hovering anywhere on
            # the card shows the description (child crossings fire
            # <Leave>, so a single container-bound tip would die at the
            # first label edge) — same pattern as _make_card
            for _w in (outer, inner, head, icon_lbl, title_lbl):
                Tooltip(_w, blurb)

        def hover_on(_e):
            try:
                outer.config(bg=TAB_ACCENTS["Tools"])
            except Exception:
                pass

        def hover_off(_e):
            try:
                outer.config(bg=COLORS["hairline"])
            except Exception:
                pass

        # click + hover on the container frames AND every descendant
        # (add="+": parts carry Tooltip <Enter>/<Leave> hooks by now —
        # plain bind() would wipe them, see _build_toggle_row)
        for w in (outer, inner, head):
            w.bind("<Button-1>", lambda e, k=key: self._mount(k))
            w.bind("<Enter>", hover_on, add="+")
            w.bind("<Leave>", hover_off, add="+")
        for child in inner.winfo_children():
            for c in child.winfo_children() or [child]:
                try:
                    c.bind("<Button-1>", lambda e, k=key: self._mount(k))
                    c.bind("<Enter>", hover_on, add="+")
                    c.bind("<Leave>", hover_off, add="+")
                except Exception:
                    pass

    def _open_target(self, target):
        try:
            if target.startswith("ms-settings:"):
                self.app._launch_settings(target)
            else:
                self.app._launch(target)
        except Exception:
            pass

    # ---------------- feature launch ---------------- #

    def _mount(self, key):
        """A card click opens the feature's X-button popup (user call:
        the in-tab panel has no room at 1040x800 — the 800x600 modals are
        the right surface). This stays the single entry path so cards
        and cross-tab reroutes behave identically."""
        try:
            if key == "storage":
                self.app._open_storage_insight()
            elif key == "health":
                self.app._open_health_report()
            elif key == "dns":
                self.app._open_dns_tester()
            elif key == "pilot":
                self.app._open_pilot_dialog()
            elif key == "speed":
                self.app._open_speed_test()
            elif key == "quick":
                self.app._open_quick_tools()
        except Exception:
            pass

    def set_run_enabled(self, enabled: bool):
        """Run-engine tab contract (A-1 audit fix): Application gates every
        tab through set_run_enabled when a run starts/finishes (run_tasks,
        install_selected_mixed, the install _done hop). TaskTab and
        InstallTab disable their Run/Install buttons; Tools hosts no
        runnable rows, so the correct behavior is to satisfy the contract
        with nothing to gate. The missing method made every tab loop raise
        AttributeError — wedging _busy True forever on any Install/Update
        click and silently hiding the Stop button + progress bar on every
        Clean/Repair/Tweak run."""
        return None


class Application:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title(APP_NAME)
        self.root.geometry(WINDOW_SIZE)
        self.root.minsize(*WINDOW_MIN_SIZE)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self.root.configure(bg=COLORS["bg"])
        _set_window_icon(self.root)
        _enable_dark_titlebar(self.root)
        # custom borderless chrome (engages lazily on first <Map> so the
        # headless harness never flashes; falls back to native on failure)
        self._chrome = BorderlessChrome(root, self)

        self._busy = False
        self._busy_lock = threading.Lock()
        self._cancel_requested = False
        self._worker_thread = None
        self.tweak_state = get_tweak_state()   # Applied-badge source (#14)
        # Auto-Pilot session state (feature 6): watcher object, keys the
        # current session requested (revert reconciles against the live
        # registry), and the open setup dialog for live status (if any)
        self._pilot = None
        self._pilot_applied_keys = []
        self._pilot_fresh_keys = []
        self._pilot_dialog = None

        self._build_style()
        if is_admin():
            self._build_main_ui()
        else:
            AdminGateFrame(self.root, on_continue_limited=self._build_main_ui)

    # ---------------- close guard (Phase 1 M3, kept) ---------------- #

    def _on_close(self):
        if self._busy:
            proceed = _themed_askyesno(
                self.root,
                "Task Still Running",
                "A task is still running. Closing now may leave a Windows "
                "service stopped until you restart it manually.\n\n"
                "Stop the task and close anyway?",
                accent=COLORS["accent_red"],
                yes_text="Stop & Close",
                no_text="Keep Running",
            )
            if not proceed:
                return
            self._request_cancel()
            if self._worker_thread is not None:
                self._worker_thread.join(timeout=8.0)
                if self._worker_thread.is_alive():
                    self._log_full("  ! Task did not stop in time — closing anyway.")
        self._stop_disk_monitor()
        # cancel any in-flight nudge estimate walk (phase-6: the scan
        # coordinator's cancelled() consults this token between tasks)
        try:
            self._nudge_scan_token[0] = True
        except Exception:
            pass
        # pilot watcher must die with the app — and stopping never
        # reverts (close is side-effect free by design)
        try:
            self._pilot_stop()
        except Exception:
            pass
        self.root.destroy()

    def _build_style(self):
        style = ttk.Style()
        style.theme_use("default")
        style.configure(".", background=COLORS["bg"], foreground=COLORS["text"])
        style.configure("TProgressbar", thickness=10, troughcolor=COLORS["surface"],
                        background=COLORS["accent_green"])
        # dark combobox + listbox (Auto Maintenance dialog)
        style.configure("TCombobox",
                        fieldbackground=COLORS["surface"], background=COLORS["surface"],
                        foreground=COLORS["text"], arrowcolor=COLORS["text"],
                        selectbackground=COLORS["surface_hover"], selectforeground=COLORS["text"],
                        bordercolor=COLORS["surface"], lightcolor=COLORS["surface"],
                        darkcolor=COLORS["surface"])
        style.map("TCombobox",
                  fieldbackground=[("readonly", COLORS["surface"]), ("disabled", COLORS["surface"])],
                  background=[("readonly", COLORS["surface"])],
                  foreground=[("readonly", COLORS["text"]), ("disabled", COLORS["subtext"])])
        self.root.option_add("*TCombobox*Listbox.background", COLORS["surface"])
        self.root.option_add("*TCombobox*Listbox.foreground", COLORS["text"])
        self.root.option_add("*TCombobox*Listbox.selectBackground", COLORS["surface_hover"])
        self.root.option_add("*TCombobox*Listbox.selectForeground", COLORS["text"])
        self.root.option_add("*TCombobox*Listbox.font", (F, 9))

    # ---------------- main UI ---------------- #

    def _build_main_ui(self):
        for widget in self.root.winfo_children():
            try:
                # never destroy the chrome's own pieces (title bar, corner
                # masks, grips) — they are chrome, not rebuildable content.
                # Grips added to the skip list after the bottom-corners bug:
                # a rebuild without this killed every resize edge.
                chrome = self._chrome
                if widget in (getattr(chrome, "_titlebar", None),):
                    continue
                if widget in (getattr(chrome, "_corners", []) or []):
                    continue
                if widget in (getattr(chrome, "_grip_widgets", {}) or {}).values():
                    continue
            except Exception:
                pass
            widget.destroy()

        self._build_menu()

        # content host: everything the app packs goes in here, leaving
        # the top TITLE_H strip for the custom title bar
        host = self._content_host = tk.Frame(self.root, bg=COLORS["bg"])
        host.place(x=0, y=BorderlessChrome.TITLE_H, relwidth=1.0,
                   relheight=1.0, height=-BorderlessChrome.TITLE_H)

        # Toolbar row (user redesign 2026-09): the Quick Tools dropdown and
        # the 4 feature pills are GONE — everything they did now lives in
        # the Tools tab (embedded panels + shortcut link-rows). What
        # remains is the privilege mode on the right; the toolbar is a
        # thin, quiet strip.
        toolbar = tk.Frame(host, bg=COLORS["bg"])
        toolbar.pack(fill="x", padx=26, pady=(8, 0))
        mode = "Administrator" if is_admin() else "Limited — cleaning only"
        tk.Label(toolbar, text=mode, font=(F, 9), bg=COLORS["bg"],
                 fg=COLORS["subtext"]).pack(side="right", anchor="se")

        # Per-tab icon, centered above the pill switcher (user request).
        # The image swaps on every tab switch; PhotoImages are kept on self
        # so Tk never garbage-collects the currently shown one.
        #
        # ICON SIZE CONTRACT (switcher-jump fix 2026-09): every icon must
        # DISPLAY at the same size or the icon label changes height per
        # tab and shoves the whole column (switcher included) up/down —
        # the "few px jump" bug report. Assets are normalized to 72x72 on
        # disk, and this loader subsamples any stray oversized file to
        # <=72px with a computed factor (not a magic /4) so one odd PNG
        # can never change the label height again.
        self._tab_icons: dict = {}
        for _tab, _fname in (("Clean", "clean.png"), ("Repair", "repair.png"),
                             ("Tweak", "tweak.png"), ("Install", "install.png"),
                             ("Tools", "tools.png")):
            for _p in self._asset_candidates(_fname):
                if not _p.is_file():
                    continue
                try:
                    _img = tk.PhotoImage(file=str(_p))
                    w, h = _img.width(), _img.height()
                    if w > 72 or h > 72:
                        _f = 2
                        while w // _f > 72 or h // _f > 72:
                            _f *= 2
                        _img = _img.subsample(_f, _f)
                    self._tab_icons[_tab] = _img
                    break
                except Exception:
                    continue
        self._tab_icon_lbl = tk.Label(host, bg=COLORS["bg"])
        self._tab_icon_lbl.pack(pady=(6, 8))

        # Pill switcher (Phase 2 #12: 5 tabs -> one sliding 3-way pill).
        # active_tab must exist before _build_tab_switch draws the thumb.
        self.active_tab = "Clean"
        self._update_tab_icon()
        self._build_tab_switch()

        # Tab pages (Install gets the catalog browser, Tools the embedded
        # power features, others the preset UI)
        self.page_container = tk.Frame(host, bg=COLORS["bg"])
        self.page_container.pack(fill="both", expand=True, padx=4, pady=(0, 4))
        self.tabs = {}
        for name in TAB_NAMES:
            if name == "Install":
                self.tabs[name] = InstallTab(self.page_container, self)
            elif name == "Tools":
                self.tabs[name] = ToolsTab(self.page_container, self)
            else:
                self.tabs[name] = TaskTab(self.page_container, self, name)
        self._show_tab("Clean")

        # Bottom: status line + details disclosure + progress + disk
        bottom = tk.Frame(host, bg=COLORS["bg"])
        bottom.pack(fill="x", padx=26, pady=(2, 10))

        stat_row = tk.Frame(bottom, bg=COLORS["bg"])
        stat_row.pack(fill="x")
        self.status_lbl = tk.Label(stat_row, text="Ready.", font=(F, 10, "bold"),
                                   bg=COLORS["bg"], fg=COLORS["subtext"], anchor="w")
        self.status_lbl.pack(side="left")
        self._status_fade_ids = []
        # drive chips (user request): one clickable chip per local drive /
        # USB stick, color-coded by fill level; click opens it in Explorer.
        # Up to MAX_INLINE_CHIPS inline; the rest live behind a "+N ▾" menu.
        self._drive_chips_frame = tk.Frame(stat_row, bg=COLORS["bg"])
        self._drive_chips_frame.pack(side="right")
        self._drive_chips: list = []      # [(label_widget, drive_root)]
        self._drive_extra: list = []      # drives behind the +N menu
        self._drive_last_key = None       # rebuild only when the set changes
        # low-space nudge chips (user-approved feature 4, 2026-09): one
        # amber chip per nearly-full drive, packed LEFT of the drive
        # chips (later side="right" packs land further left). Created and
        # destroyed on red transitions — never a permanent fixture.
        self._nudge_frame = tk.Frame(stat_row, bg=COLORS["bg"])
        self._nudge_frame.pack(side="right", padx=(8, 0))
        self._nudge_chips: dict = {}      # letter -> {"frame", "label", "tip"}
        self._drive_was_red: dict = {}    # letter -> bool (hysteresis state)
        self._nudge_dismissed: set = set()  # session dismissals (respected)
        self._nudge_estimate = None       # (bytes, epoch) or None
        self._nudge_scan_running = False
        self._nudge_scan_token = [False]  # cancel token (app close / phase-6)
        self._last_drives: list = []      # raw monitor list (tooltip totals)

        # progress overlay (segmented animated bar) — hidden until a run
        # starts (gamers see a clean window, not an empty progress track)
        self.progress_bar = AnimatedProgressBar(bottom, accent=COLORS["accent_green"])
        self._progress_hidden = True

        # Stop button — slots into the status row while a run is active
        self.cancel_btn = AnimatedButton(
            stat_row, text="✕ Stop", command=self._request_cancel,
            bg=COLORS["surface"], fg=COLORS["accent_red"], font=(F, 9, "bold"),
            padx=14, pady=6,
        )
        self.cancel_btn.set_enabled(False)

        # Bottom-corner shortcuts (user request): Auto Maintenance and
        # Export Logs live here as icon buttons instead of inside the
        # Quick Tools popup. Click opens the dialog; hover names it.
        corners = tk.Frame(host, bg=COLORS["bg"])
        corners.pack(fill="x", padx=26, pady=(0, 8))
        self._corner_maint = tk.Label(corners, text="🛠️", font=(F, 13),
                                      bg=COLORS["bg"], fg=COLORS["subtext"],
                                      cursor="hand2", bd=0, highlightthickness=0)
        self._corner_maint.pack(side="left")
        Tooltip(self._corner_maint, "Auto Maintenance")
        self._corner_maint.bind("<Button-1>",
                                lambda e: self._show_schedule_dialog(), add="+")
        self._corner_maint.bind("<Enter>",
                                lambda e: self._corner_maint.config(fg=COLORS["text"]), add="+")
        self._corner_maint.bind("<Leave>",
                                lambda e: self._corner_maint.config(fg=COLORS["subtext"]), add="+")
        self._corner_logs = tk.Label(corners, text="📋", font=(F, 13),
                                     bg=COLORS["bg"], fg=COLORS["subtext"],
                                     cursor="hand2", bd=0, highlightthickness=0)
        self._corner_logs.pack(side="right")
        Tooltip(self._corner_logs, "Export Logs")
        self._corner_logs.bind("<Button-1>",
                               lambda e: self.export_logs(), add="+")
        self._corner_logs.bind("<Enter>",
                               lambda e: self._corner_logs.config(fg=COLORS["text"]), add="+")
        self._corner_logs.bind("<Leave>",
                               lambda e: self._corner_logs.config(fg=COLORS["subtext"]), add="+")

        self._build_log(bottom)
        self._start_disk_monitor()

        # Chrome restack (bottom-corners bug fix): everything above was
        # just (re)built — new siblings stacked above the chrome pieces
        # (title bar / grips / corner masks), burying the BOTTOM corner
        # masks under the content host. Re-assert the chrome z-order.
        try:
            self._chrome.restack_chrome()
        except Exception:
            pass

        self.set_status("Ready. Pick a preset and press Run.")

        # F6: the window is up — pre-warm the first-entry Custom/Undo
        # bodies and the Install catalog in idle time instead of on the
        # user's first click / tab switch.
        self._schedule_idle_prewarm()

        # Auto-Pilot resume (feature 6): the watcher only ever runs while
        # the GUI runs, so "enabled" just means resume watching now
        try:
            from app.config_persist import load_config as _load
            if _load().get("session_pilot_enabled", False):
                self._pilot_start()
        except Exception:
            pass

    # ---------------- F6: idle pre-warm chain ---------------- #

    _PREWARM_START_MS = 900    # let the window settle + first paint finish
    _PREWARM_STEP_GAP_MS = 250 # breathing room between monolithic builds

    def _schedule_idle_prewarm(self):
        """F6(b)+F6(a): after startup, one idle chain (a) pre-warms the
        four first-entry bodies — Clean/Repair/Tweak Custom + Tweak Undo —
        into the per-tab body caches and (b) hands off to the Install tab's
        chunked catalog builder.

        Each monolithic grid build is its own step and steps are spread
        ~250 ms apart, so a step never lands on top of user interaction
        that arrived during the previous one. The grid steps run FIRST on
        purpose: they pay the one-off per-process Tk costs (font loads,
        notably the ~200 ms first Segoe UI Emoji font) so every Install
        catalog slice that follows stays well under the ~30 ms budget.
        Guards: busy runs postpone the chain; a failing step is dropped;
        bodies the user already built are cache hits and skip instantly."""
        if getattr(self, "_idle_chain_scheduled", False):
            return
        self._idle_chain_scheduled = True
        steps = []
        for tab_name, mode in (("Clean", "custom"), ("Repair", "custom"),
                               ("Tweak", "custom"), ("Tweak", "undo")):
            page = self.tabs[tab_name]
            steps.append(lambda p=page, m=mode: p._prewarm_body(m))
        steps.append(lambda: self.tabs["Install"]._start_catalog_build())
        # PERF (startup audit 2026-09): the Install page's FIRST visit
        # still costs ~900 ms because place()ing ~950 widgets maps the
        # whole subtree on the click. Pre-map the page NOW, underneath
        # the others so the first user click is only a tkraise of an
        # already-laid-out page (~50 ms, like warm visits).
        # Flicker fix (user bug report: main window flashed the Install
        # tab while an Auto-Pilot dialog was open): the old sequence
        # place()d the page, RAISED it to the top of the stack (mapping
        # it visibly for the rest of the frame), then re-buried it — and
        # a postponed step could run that raise between the user's popup
        # and the main window. The raise was only ever there to make
        # update_idletasks lay the page out. lower() achieves the same
        # layout pass WITHOUT the page ever stacking above the visible
        # tab: geometry is computed for withdrawn-lowered windows too,
        # and _show_tab's tkraise does the one-and-only visible raise on
        # the user's first real visit.
        def _premap_install():
            try:
                page = self.tabs["Install"]
                if page.winfo_manager() != "place":
                    page.place(relx=0, rely=0, relwidth=1, relheight=1)
                    page.lower()          # lay out UNDERNEATH — never raised
                    self.root.update_idletasks()
                    # (no re-bury loop needed anymore — lower() never
                    # stacked the page above anything in the first place)
            except Exception:
                pass
        steps.append(_premap_install)
        self._idle_steps = steps

        def _step():
            if not getattr(self, "_idle_chain_scheduled", False):
                return
            if self._prewarm_should_wait():
                # a run is active — or a popup is open — postpone rather
                # than map/raise/layout pages behind the user's context
                # (user bug report: background flicker behind first-open
                # Auto-Pilot). The chain resumes on its own afterwards.
                try:
                    self.root.after(500, _step)
                except Exception:
                    pass
                return
            if self._idle_steps:
                step = self._idle_steps.pop(0)
                try:
                    step()
                except Exception:
                    pass  # a failed pre-warm must never crash the app
            if self._idle_steps:
                try:
                    self.root.after(self._PREWARM_STEP_GAP_MS, _step)
                except Exception:
                    pass

        try:
            self.root.after(self._PREWARM_START_MS, _step)
        except Exception:
            pass

    def _asset_candidates(self, fname):
        cands = []
        if getattr(sys, "_MEIPASS", None):
            cands.append(pathlib.Path(sys._MEIPASS) / "app" / "assets" / fname)
            cands.append(pathlib.Path(sys._MEIPASS) / "assets" / fname)
        cands.append(pathlib.Path(__file__).with_name("assets") / fname)
        cands.append(pathlib.Path(__file__).resolve().parents[1] / "app" / "assets" / fname)
        return cands

    # ---------------- pill switcher ---------------- #

    # User-approved faster switch (2026-09): 240 -> 160 ms. Still the same
    # time-based ease-out at ~60 fps ticks; the slide just completes sooner.
    SWITCH_ANIM_MS = 160

    def _build_tab_switch(self):
        """One pill, three labels, one thumb that slides + recolors (#12,
        user idea: 'combine the 3 tab buttons into one animated switch').

        Flicker fix (user feedback): all canvas items are created ONCE and
        the animation only moves/recolors them (coords/itemconfigure). The
        old version did delete("all") + full recreate every frame, which
        leaves a blank-frame window between delete and recreate and
        re-rasterizes the text glyphs each frame — that was the visible
        flicker/micro-stutter. Persistent items let Tk repaint only the
        small damaged region. Motion is also time-based (eased), so a late
        frame never changes the animation's speed — only its smoothness."""
        # Phase-2 fix: the switcher must live in the content host, below
        # the custom title bar — packing to self.root slid it under the
        # toolbar clip (user bug report: switcher clipped at the top)
        switch_wrap = tk.Frame(self._content_host, bg=COLORS["bg"])
        switch_wrap.pack(pady=(10, 2))
        # width scales with the number of tabs (4 tabs now: Install added)
        n_tabs = len(TAB_NAMES)
        self._switch_labels = TAB_NAMES
        self._switch_w = max(444, 148 * n_tabs)
        self.switch = tk.Canvas(switch_wrap, width=self._switch_w, height=44,
                                highlightthickness=0, bd=0, bg=COLORS["bg"])
        self.switch.pack()
        self._seg = self._switch_w / n_tabs
        self._switch_pos = float(TAB_NAMES.index(self.active_tab))
        self._switch_anim_after = None
        self.switch.bind("<Button-1>", self._on_switch_click)
        self.switch.bind("<Motion>", self._on_switch_motion)
        self.switch.bind("<Leave>", lambda e: self.switch.config(cursor="arrow"))
        # keyboard accessibility (audit a11y): the pill accepts Tab focus;
        # Left/Right arrows move between tabs. Focus is indicated by the
        # thumb's own canvas — no native highlightthickness rectangle
        # (user feedback: stray outline).
        self.switch.configure(takefocus=1, highlightthickness=0)
        self.switch.bind("<Left>", lambda e: self._switch_to(
            TAB_NAMES[max(0, TAB_NAMES.index(self.active_tab) - 1)]))
        self.switch.bind("<Right>", lambda e: self._switch_to(
            TAB_NAMES[min(len(TAB_NAMES) - 1, TAB_NAMES.index(self.active_tab) + 1)]))
        self.switch.bind("<Return>", lambda e: None)  # arrows are the activation
        self.switch.bind("<space>", lambda e: None)
        # persistent items — created once, never deleted
        self._switch_track = self._canvas_round_rect(
            self.switch, 0, 0, self._switch_w, 44, 22,
            fill=COLORS["bg_alt"], outline="")
        thumb_w = self._seg - 8
        x = self._thumb_x(self._switch_pos)
        self._switch_thumb = self._canvas_round_rect(
            self.switch, x, 4, x + thumb_w, 40, 18,
            fill=TAB_ACCENTS[self.active_tab], outline="")
        self._switch_texts = []
        for i, name in enumerate(self._switch_labels):
            cx = self._seg * i + self._seg / 2
            self._switch_texts.append(self.switch.create_text(cx, 22, text=name))
        self._style_switch_labels()

    def _thumb_x(self, t):
        thumb_w = self._seg - 8
        return 4 + t * self._seg + (self._seg - thumb_w - 8) / 2

    def _place_switch_thumb(self, t, fill):
        thumb_w = self._seg - 8
        x = self._thumb_x(t)
        points = self._round_points(x, 4, x + thumb_w, 40, 18)
        self.switch.coords(self._switch_thumb, *points)
        self.switch.itemconfigure(self._switch_thumb, fill=fill)

    def _style_switch_labels(self):
        # Same-size labels for every state (user bug report: the active
        # label's 10->11-bold growth read as the pill "jumping"). The
        # active tab is marked by the thumb + black text alone — a
        # color-only switch cannot shift pixels.
        for i, tid in enumerate(self._switch_texts):
            active = self._switch_labels[i] == self.active_tab
            self.switch.itemconfigure(
                tid,
                fill=COLORS["black"] if active else COLORS["subtext"],
                font=(F, 10, "bold") if active else (F, 10),
            )

    def _on_switch_click(self, event):
        seg = min(len(TAB_NAMES) - 1, max(0, int(event.x // self._seg)))
        self._switch_to(TAB_NAMES[seg])

    def _on_switch_motion(self, event):
        seg = min(len(TAB_NAMES) - 1, max(0, int(event.x // self._seg)))
        self.switch.config(cursor="hand2" if seg != TAB_NAMES.index(self.active_tab) else "arrow")

    def _switch_to(self, name):
        if self._busy or name == self.active_tab:
            # audit fix (P2-08): silently swallowing clicks while busy reads
            # as "app is frozen". Show the busy state instead.
            if self._busy:
                self.set_status("Busy — wait for the current task to finish (or press ✕ Stop).")
            return
        from_accent = TAB_ACCENTS[self.active_tab]
        self.active_tab = name
        self._update_tab_icon()
        # cancel any in-flight animation so a fast re-click retargets cleanly
        if self._switch_anim_after is not None:
            try:
                self.root.after_cancel(self._switch_anim_after)
            except Exception:
                pass
        self._switch_anim = {
            "start_t": self._switch_pos,
            "target_t": float(TAB_NAMES.index(name)),
            "start_ms": time.monotonic(),
            "from_accent": from_accent,
            "to_accent": TAB_ACCENTS[name],
        }
        self._style_switch_labels()
        # F5 (perf): swap the content page and flush its geometry BEFORE
        # the slide starts. The old order ticked the thumb first and laid
        # the new page out inside the 240 ms animation window — on heavy
        # pages (Install ~950 widgets) the synchronous layout/paint stalled
        # the slide, which then jumped to the end. update_idletasks() does
        # the page's geometry pass once so the animation's first frame runs
        # on an already-settled page.
        #
        # BENCHMARK A/B (2026-09): moving that flush out of the click
        # handler (letting idle-time layout land inside the animation
        # window) was measured as a net LOSS: click sync dropped ~10 ms,
        # but total click-to-settled grew ~12 ms and one update round
        # inside the slide absorbed the whole layout (~10 ms on the Tweak
        # grids, maxR 0.8 -> 12 ms) — a dropped frame right where the
        # slide should look smoothest. The flush stays here.
        #
        # Under the stacked-pages PROTO (see _show_tab) the warm-switch
        # flush is near-free (the raised page was already laid out, so
        # update_idletasks has ~nothing to do) but still required on a
        # page's FIRST visit, where place() maps the whole subtree: that
        # layout must settle before the slide's first frame, same as it
        # did under pack. Re-checked by measurement (2026-09 bench, see
        # the session report): warm flush cost stayed negligible, so the
        # flush is kept under the PROTO as well.
        self._show_tab(name)
        self.root.update_idletasks()
        self._tick_switch()

    def _tick_switch(self):
        anim = getattr(self, "_switch_anim", None)
        if anim is None:
            return
        # monotonic() is in SECONDS — convert to ms before dividing by the
        # ms-named duration (units bug made the slide take 240 *seconds*).
        p = (time.monotonic() - anim["start_ms"]) * 1000.0 / self.SWITCH_ANIM_MS
        if p >= 1.0:
            self._switch_pos = anim["target_t"]
            self._place_switch_thumb(self._switch_pos, anim["to_accent"])
            self._switch_anim = None
            self._switch_anim_after = None
            return
        # ease-out cubic: fast start, gentle landing — no visible snap
        eased = 1 - (1 - p) ** 3
        t = anim["start_t"] + (anim["target_t"] - anim["start_t"]) * eased
        self._switch_pos = t
        self._place_switch_thumb(t, _hex_lerp(anim["from_accent"], anim["to_accent"], eased))
        # ~60 fps ticks (F5): Tk cannot present faster than the display
        # refresh, and 100 wakeups/s during a 240 ms slide buys nothing —
        # each tick just re-times the eased position, so the pacing is
        # unchanged (time-based math above), only the wakeup rate drops.
        self._switch_anim_after = self.root.after(16, self._tick_switch)

    def _canvas_round_rect(self, c, x0, y0, x1, y1, r, **kw):
        return c.create_polygon(self._round_points(x0, y0, x1, y1, r), smooth=True, **kw)

    @staticmethod
    def _round_points(x0, y0, x1, y1, r):
        return [
            x0 + r, y0, x1 - r, y0, x1, y0, x1, y0 + r,
            x1, y1 - r, x1, y1, x1 - r, y1, x0 + r, y1,
            x0, y1, x0, y1 - r, x0, y0 + r, x0, y0,
        ]

    def _update_tab_icon(self):
        """Swap the icon above the pill to the active tab's asset image."""
        try:
            lbl = getattr(self, "_tab_icon_lbl", None)
            if lbl is None or not lbl.winfo_exists():
                return
            img = getattr(self, "_tab_icons", {}).get(getattr(self, "active_tab", ""))
            if img is not None:
                lbl.config(image=img)
            else:
                lbl.config(image="")
        except Exception:
            pass

    def _show_tab(self, name):
        # -------------------------------------------------------------
        # PROTO (user-approved experiment, 2026-09): always-mapped STACKED
        # tab pages. All pages live in page_container (the "stage" — it is
        # the shared container the pages were already direct children of;
        # nothing else is packed into it). A page is place()d once into the
        # stage on its FIRST show (relx/rely/relwidth/relheight=1 fills the
        # stage exactly, so a page's geometry equals the old pack result),
        # then switching only tkraise()s the target above its siblings —
        # no pack_forget/pack anywhere in the switch path, so switching
        # never unmaps a page and never triggers a full re-layout/re-raster
        # of the shown page ("I can watch the content render" stutter).
        # Every page root frame is opaque (bg=COLORS["bg"]), so a lower
        # page can never show through. Pages are stacked ONLY after their
        # first visit: an unvisited page is not managed at all (same as the
        # old pack_forget state — zero startup cost, and the Install page's
        # idle catalog slices build into an unmanaged frame exactly as
        # before). Rollback: restore the backup at
        #   ~/.openclaw-autoclaw/agents/auto-coder/workspace/.openclaw/tmp/
        #   gui.py.bak-20260905-054920  (sha256 4289ED3C...2132D8)
        # and revert the _switch_to flush note if kept.
        # -------------------------------------------------------------
        # F6(a)+progressive show (2026-09): the Install catalog builds in
        # idle-time chunks after startup. Reaching Install mid-build used to
        # force-drain the remaining queue synchronously HERE (measured
        # 700-900 ms cold on the click). Now the queue is NOT drained: we
        # only make sure the slice chain is running (or restart it if a
        # cancelled chain left the queue non-empty — the page must never
        # sit on its placeholder forever), then show the page as-is with
        # the "Preparing catalog…" placeholder; the queued slices keep
        # building in idle and _catalog_finish drops the placeholder and
        # applies pending badge/search/count state when they drain.
        if name == "Install":
            _start = getattr(self.tabs[name], "_start_catalog_build", None)
            if _start is not None:
                try:
                    _start()   # guarded: no-op when ready / already in flight
                except Exception:
                    pass
        target = self.tabs[name]
        if target.winfo_manager() != "place":
            target.place(relx=0, rely=0, relwidth=1, relheight=1)
        target.tkraise()
        # progress bar may not exist yet during initial build
        if hasattr(self, "progress_bar"):
            accent = TAB_ACCENTS[name]
            self.progress_bar._accent = accent
            self.progress_bar._draw()
        # one global wheel router: scroll whichever rounded panel on the
        # visible page contains the pointer (per-page bind_all would let the
        # LAST-bound page eat every wheel event for the whole app)
        if not getattr(self, "_wheel_bound", False):
            self._wheel_bound = True
            self.root.bind_all("<MouseWheel>", self._on_wheel_routed)

    def _on_wheel_routed(self, e):
        page = self.tabs.get(self.active_tab)
        if page is None or not page.winfo_ismapped():
            return
        target = None
        w = self.root.winfo_containing(e.x_root, e.y_root)
        node = w
        while node is not None:
            if isinstance(node, ScrollableRoundedPanel):
                target = node
                break
            node = node.master
        if target is not None:
            target.on_wheel(e.delta)

    # ---------------- menu ---------------- #

    def _build_menu(self):
        # (user redesign 2026-09): the Quick Tools dropdown is GONE — its
        # items now live in the Tools tab as link-rows. This method keeps
        # ONLY the no-native-menubar guard: a native menu renders as a
        # white strip Tk cannot theme, so one must never be attached.
        self._tools_menu = None
        try:
            self.root.config(menu="")
        except Exception:
            pass

    def _open_storage_insight(self):
        """Storage Insight entry (Tools tab card + drive-chip right-click +
        low-space nudge chip + Health fix-action). Opens the X-button
        popup (user call: in-tab panels have no room at 1040x800 — the
        800x600 modal is the right surface). No-op while a run owns the
        progress UI."""
        try:
            with self._busy_lock:
                busy = self._busy
        except Exception:
            busy = False
        if busy:
            try:
                self.set_status("Busy — wait for the current task to finish (or press ✕ Stop).")
            except Exception:
                pass
            return
        try:
            StorageInsightDialog(self.root, self).wait()
        except Exception:
            pass

    def _open_dns_tester(self):
        """gaming_dns row button entry (delegates here). Opens the DNS
        popup. No busy-guard: opening never touches the run engine —
        only Apply does, and run_tasks guards busy itself."""
        try:
            DnsTesterDialog(self.root, self).wait()
        except Exception:
            pass

    def _open_health_report(self):
        """Check PC entry (Tools tab card). Opens the Health popup.
        Busy-guarded like Storage: a sensor sweep competes for disk/CPU
        with a live run, and its fix actions navigate tabs the run engine
        is using."""
        try:
            with self._busy_lock:
                busy = self._busy
        except Exception:
            busy = False
        if busy:
            try:
                self.set_status("Busy — wait for the current task to finish (or press ✕ Stop).")
            except Exception:
                pass
            return
        try:
            HealthReportDialog(self.root, self).wait()
        except Exception:
            pass

    # ---------------- Game Session Auto-Pilot (feature 6) ---------------- #
    # The watcher thread lives only while the GUI runs (started here on
    # boot when enabled, stopped on close/toggle-off). Detection and
    # state live in app/session_pilot.py (headless-tested); THESE methods
    # are the GUI half: resolve preset tasks, call run_tasks, toast, and
    # repaint the dialog's live line when it's open. run_tasks itself is
    # async + thread-safe by design (worker + Tk hops), so the watcher
    # thread only ever schedules after() hops — never touches widgets.

    def _open_pilot_dialog(self):
        """Pilot entry (Tools tab card). Opens the setup popup (setup
        only, no busy-guard needed)."""
        try:
            PilotDialog(self.root, self).wait()
        except Exception:
            pass

    def _open_speed_test(self):
        """Speed Test entry (Tools tab card). Opens the speed popup
        (read-only measurement, no busy-guard needed — same rationale
        as the DNS tester)."""
        try:
            SpeedTestDialog(self.root, self).wait()
        except Exception:
            pass

    def _open_quick_tools(self):
        """Quick Tools entry (Tools tab card). Opens the shortcuts
        popup (read-only launchers, no busy-guard needed)."""
        try:
            tools_tab = self.tabs.get("Tools")
            open_target = getattr(tools_tab, "_open_target", None)
            if open_target is None:
                return
            QuickToolsDialog(self.root, self, open_target).wait()
        except Exception:
            pass

    def _pilot_preset_tasks(self):
        """Task objects for the configured session preset (tolerant:
        unknown preset names / stale keys degrade to Game Session, then
        to whatever resolves — never raise)."""
        try:
            from app.config_persist import load_config as _load
            want = _load().get("session_pilot_preset", "Game Session")
        except Exception:
            want = "Game Session"
        try:
            presets = PRESETS.get("Tweak", {})
            keys = presets.get(want) or presets.get("Game Session", [])
            by_key = {t.key: t for t in TABS.get("Tweak", [])}
            tasks = [by_key[k] for k in keys if k in by_key]
            if tasks:
                return tasks
            return [by_key[k] for k in presets.get("Game Session", [])
                    if k in by_key]
        except Exception:
            return []

    def _pilot_ensure(self):
        """(Re)build the pilot object from current config (called on
        boot, toggle-on, and watchlist edits — cheap, idempotent).

        Phase-5 watchlist assembly (3 layers):
          1. built-in famous-title basenames (legacy contract)
          2. user picks — full PATHS when known (path-verified matching:
             a same-named exe in another folder never fires), bare names
             for legacy entries
          3. 'watch all installed games' (opt-in): every game found by
             app.game_catalog — resolved FRESH so a newly installed game
             is picked up on the next toggle/start, never a stale cache.
             Unresolvable-exe games contribute their folder's plausible
             exes as bare names (heuristic already filtered
             redists/launchers at catalog level).

        Flicker fix (user bug report): layer 3 is a DISK SWEEP and used
        to run synchronously here — on a big library that froze the Tk
        thread for seconds on toggle/boot. Now the pilot builds + starts
        immediately on layers 1-2 (fast, config-only) and layer 3 merges
        in from a worker thread (generation-guarded, see below)."""
        try:
            from app.config_persist import load_config as _load
            from app.session_pilot import (KNOWN_GAME_EXES, SessionPilot,
                                           normalize_exe)
            cfg = _load()
            watched = self._pilot_watched_fast(cfg, normalize_exe)
            # dedupe: a game both hand-picked AND watch-all must not
            # double-register (set() at the pilot level handles it)
            old = getattr(self, "_pilot", None)
            try:
                if old is not None:
                    old.stop()
            except Exception:
                pass
            pilot = SessionPilot(
                watched=watched,
                on_apply=self._pilot_on_detect,
                on_revert=self._pilot_on_exit)
            self._pilot = pilot
            if cfg.get("session_pilot_watch_all"):
                self._pilot_merge_catalog(pilot)
        except Exception:
            pass

    @staticmethod
    def _pilot_watched_fast(cfg, normalize_exe):
        """Layers 1-2: built-ins + user picks. Pure config, no disk —
        safe on the Tk thread."""
        try:
            from app.session_pilot import KNOWN_GAME_EXES
        except Exception:
            return []
        watched = list(KNOWN_GAME_EXES)
        # user picks: paths stay paths (dir-pinned), names stay names
        for g in cfg.get("session_pilot_games", []) or []:
            try:
                n = normalize_exe(g)
                if n:
                    watched.append(n)
            except Exception:
                pass
        for pth in cfg.get("session_pilot_paths", []) or []:
            if isinstance(pth, str) and pth.strip():
                watched.append(pth)
        return watched

    def _pilot_merge_catalog(self, pilot):
        """Worker-thread layer-3 sweep: enumerate installed games off the
        Tk thread, then merge into the LIVE pilot via a Tk-thread poll.
        Generation-guarded: if the pilot was replaced/stopped meanwhile
        (rapid toggles, app closing), the stale result is dropped instead
        of resurrecting dead state. A game already running when the merge
        lands is caught by the next 15s poll.

        Threading contract (measured 2026-09): worker threads NEVER call
        Tk here — not even winfo_exists/after (a refused foreign Tk call
        burns ~1s then raises). The worker writes a plain holder dict;
        the poller below (Tk thread, scheduled by the caller on the Tk
        thread) publishes."""
        try:
            import threading as _th
        except Exception:
            return
        try:
            seq = getattr(self, "_merge_seq", 0) + 1
        except Exception:
            seq = 1
        self._merge_seq = seq
        holder = {"done": False, "extra": []}

        def _run():
            extra = []
            try:
                from app.game_catalog import installed_games
                for g in installed_games() or []:
                    gp = (g.get("path") or "").strip()
                    if gp:
                        extra.append(gp)
                        continue
                    folder = (g.get("folder") or "").strip()
                    try:
                        import glob as _glob
                        lvl1 = _glob.glob(os.path.join(folder, "*.exe"))
                        lvl2 = _glob.glob(
                            os.path.join(folder, "*", "*.exe"))
                        for cand in (lvl1 + lvl2)[:6]:
                            extra.append(os.path.basename(cand))
                    except Exception:
                        pass
            except Exception:
                pass
            try:
                holder["extra"] = extra
            except Exception:
                pass
            try:
                holder["done"] = True
            except Exception:
                pass

        try:
            _th.Thread(target=_run, daemon=True,
                       name="PilotCatalogMerge").start()
        except Exception:
            return
        try:
            self.root.after(
                150, lambda: self._check_merge(pilot, holder, seq))
        except Exception:
            pass

    def _check_merge(self, pilot, holder, seq):
        """Tk-thread poller for one catalog sweep. Drops superseded
        results (newer ensure bumped the sequence), re-arms while the
        sweep flies, lands exactly once."""
        try:
            if getattr(self, "_merge_seq", 0) != seq:
                return              # superseded — drop silently
        except Exception:
            return
        try:
            if not self.root.winfo_exists():
                return
        except Exception:
            return
        try:
            done = holder.get("done")
        except Exception:
            return
        if not done:
            try:
                self.root.after(
                    150, lambda: self._check_merge(pilot, holder, seq))
            except Exception:
                pass
            return
        self._land_catalog(pilot, holder.get("extra") or [])

    def _land_catalog(self, pilot, extra):
        """Tk thread: merge a swept catalog into the pilot IFF it is
        still the current one (else drop — superseded or stopped)."""
        try:
            if not extra:
                return
            if getattr(self, "_pilot", None) is not pilot:
                return
            try:
                from app.config_persist import load_config as _load
                from app.session_pilot import normalize_exe
                cfg = _load()
            except Exception:
                return
            # rebuild from CURRENT config (a pick made mid-sweep must
            # survive) + the swept layer, then swap atomically-ish
            # (single set_watched call; a concurrent poll may use mixed
            # tables for one tick at most — self-heals next poll)
            watched = self._pilot_watched_fast(cfg, normalize_exe)
            watched.extend(extra)
            try:
                pilot.set_watched(watched)
            except Exception:
                pass
            try:
                self.log("Game Session Auto-Pilot: installed-games "
                         "watchlist merged.")
            except Exception:
                pass
        except Exception:
            pass

    def _pilot_start(self):
        try:
            self._pilot_ensure()
            pilot = getattr(self, "_pilot", None)
            if pilot is not None:
                pilot.start()
                self.log("Game Session Auto-Pilot: watching for game launches.")
                # one-time notice when watch-all is active (the new-config
                # default): the auto-detection sweep is disclosed, never a
                # surprise — and points at the exact switch that turns it off.
                try:
                    from app.config_persist import load_config as _lc
                    if _lc().get("session_pilot_watch_all"):
                        if not getattr(self, "_pilot_watchall_notice_shown", False):
                            self._pilot_watchall_notice_shown = True
                            self.set_status(
                                "Auto-Pilot is watching your installed games — "
                                "open the Auto-Pilot dialog to narrow the list.")
                except Exception:
                    pass
        except Exception:
            pass

    def _pilot_stop(self):
        # Stopping NEVER reverts: the user may still be playing, and app
        # close must be side-effect free. Applied tweaks stay badged
        # until Undo reverts them (said plainly in the dialog).
        try:
            pilot = getattr(self, "_pilot", None)
            if pilot is not None:
                pilot.stop()
            self._pilot = None
            self.log("Game Session Auto-Pilot: stopped watching.")
        except Exception:
            pass

    def _pilot_refresh_watchlist(self):
        """User added/removed an .exe: rebuild the watchlist live."""
        try:
            pilot = getattr(self, "_pilot", None)
            running = bool(pilot is not None and pilot.running())
            self._pilot_ensure()
            if running:
                try:
                    self._pilot.start()
                except Exception:
                    pass
        except Exception:
            pass

    def _pilot_running(self) -> bool:
        try:
            pilot = getattr(self, "_pilot", None)
            return bool(pilot is not None and pilot.running())
        except Exception:
            return False

    def _pilot_sink(self, text, color=None):
        """Live line repaint for the open pilot dialog (no-op closed)."""
        try:
            dlg = getattr(self, "_pilot_dialog", None)
            if dlg is not None:
                dlg.set_live_status(text, color)
        except Exception:
            pass

    def _pilot_on_detect(self, hits):
        """Watcher thread → hop to Tk. Records the session keys FIRST so
        a later exit always has something truthful to reconcile."""
        # F10: no winfo_exists off-thread — after() on a dead root raises
        # and is swallowed below.
        try:
            self.root.after(0, lambda: self._pilot_apply(list(hits or [])))
        except Exception:
            pass

    def _modal_open(self):
        """True while any themed popup is up (a dialog is open).

        User bug report: opening Auto-Pilot right after launch flickered
        the main window behind the popup — the startup pre-warm chain and
        the Install catalog builder kept mapping/raising/laying-out pages
        underneath the modal. Both chains now pause while this is true
        (same postpone-and-retry pattern as the busy-run guard).

        Flicker fix (dropdown variant of the same bug): the grab_current()
        check this used to make reads FALSE the moment a Combobox popdown
        inside the dialog takes the global grab — clicking the preset
        dropdown re-armed the background chains behind the open popup.
        The open-instance registry (ThemedModal.any_open) is the detector
        now; the grab check remains only as the registry's own fallback."""
        return ThemedModal.any_open()

    def _prewarm_should_wait(self):
        """Background chains run only when the UI is truly idle: no run
        in flight AND no popup open (a run owns the progress UI; a modal
        owns the user's eyes — building pages behind either is churn)."""
        try:
            if self._busy:
                return True
        except Exception:
            pass
        return self._modal_open()

    def _pilot_ui_free(self):
        """True when a pilot-triggered run may safely choreograph the
        shared run UI (progress bar, tab buttons, status stream): no
        modal dialog open AND no run in flight.

        User bug report: a game launching/exiting while the Auto-Pilot
        setup dialog (or any modal) was open made the whole app churn
        behind the popup. Flicker fix: the detector is the open-instance
        registry (ThemedModal.any_open), not grab_current() — a Combobox
        popdown inside an open dialog releases the grab and would read
        'free' at the exact moment the user is mid-dropdown."""
        try:
            with self._busy_lock:
                if self._busy:
                    return False
        except Exception:
            pass
        if ThemedModal.any_open():
            return False
        return True

    def _pilot_apply(self, hits):
        """Tk thread: apply the session preset (or defer quietly if the
        user is mid-run — the pilot retries nothing; the session simply
        runs untweaked and the exit path no-ops via registry check)."""
        try:
            tasks = self._pilot_preset_tasks()
        except Exception:
            tasks = []
        if not tasks:
            return
        try:
            self._pilot_applied_keys = [t.key for t in tasks]
        except Exception:
            self._pilot_applied_keys = []
        try:
            # Fresh-keys only: exclude tweaks that were ALREADY applied
            # before this session (the user may want those persistent —
            # reverting them at session end would undo the user's own
            # deliberate setup, not the pilot's work).
            before = set(get_tweak_state() or {})
        except Exception:
            before = set()
        try:
            self._pilot_fresh_keys = [k for k in self._pilot_applied_keys
                                      if k not in before]
        except Exception:
            self._pilot_fresh_keys = list(self._pilot_applied_keys)
        busy = False
        try:
            with self._busy_lock:
                busy = self._busy
        except Exception:
            pass
        # flicker fix: registry check (see _pilot_ui_free) — the old
        # grab_current() read False during any open dialog's dropdown
        modal = ThemedModal.any_open()
        runnable = list(tasks)
        if not is_admin():
            # limited mode: only what needs no admin, SILENTLY (a
            # background trigger must never pop "Administrator Required"
            # over someone's game — run_tasks would do exactly that)
            runnable = [t for t in tasks if not t.admin_required]
        if busy or modal or not runnable:
            try:
                self.log("Game Session Auto-Pilot: game detected "
                         f"({(hits or ['?'])[0]}) but "
                         + ("a run is active — session runs untweaked."
                            if busy else
                            ("a dialog is open — session runs untweaked."
                             if modal else
                             "no runnable tweaks in limited mode — session runs untweaked.")))
            except Exception:
                pass
            return
        try:
            self.log(f"Game Session Auto-Pilot: applying session preset for {(hits or ['?'])[0]}.")
            self._pilot_sink(f"🎮 {(hits or ['?'])[0]} — session active",
                             COLORS["accent_green"])
            try:
                import threading as _th
                _th.Thread(target=show_toast,
                           args=("🎮 Game Mode ON",
                                 "Session tweaks applied — enjoy the game. "
                                 "They restore automatically when you quit."),
                           daemon=True).start()
            except Exception:
                pass
            self.run_tasks("Tweak", runnable, mode="run", quiet=True)
        except Exception:
            pass

    def _pilot_on_exit(self):
        """Watcher thread → hop to Tk."""
        # F10: no winfo_exists off-thread (see _pilot_on_detect).
        try:
            self.root.after(0, self._pilot_revert)
        except Exception:
            pass

    def _pilot_revert(self):
        """Tk thread: revert ONLY fresh session keys still marked applied
        (registry intersection). Excluded, and therefore never touched:
        keys the run never managed to apply (busy-skip, failure,
        user-undone mid-session) AND keys that were already applied
        before the session (the user's persistent setup — reverting those
        would undo deliberate choices, not pilot work). Silent when
        there's nothing to do."""
        try:
            fresh = list(getattr(self, "_pilot_fresh_keys", []) or [])
        except Exception:
            fresh = []
        if not fresh:
            return
        # UI-freedom guard (user bug report + audit find): a revert that
        # fires while a run owns the progress UI — or while ANY modal
        # dialog is open — must not choreograph shared UI behind the
        # user's context. Worse, run_tasks would pop a "Busy" dialog
        # (possibly over a game) AND the revert would be lost. Skip with
        # an honest log line instead; Undo Tweaks restores manually and
        # the applied-badges stay truthful about what is still on.
        # F01: intent is preserved (keys NOT cleared) so a later exit
        # event can still revert; keys clear only on the runnable path.
        _busy = False
        try:
            with self._busy_lock:
                _busy = self._busy
        except Exception:
            pass
        # flicker fix: registry check (see _pilot_ui_free) — the old
        # grab_current() read False during any open dialog's dropdown
        _modal = ThemedModal.any_open()
        if _busy or _modal:
            try:
                self.log("Game Session Auto-Pilot: game closed but the UI is "
                         "occupied (" + ("a run is active" if _busy else "a dialog is open") +
                         ") — session tweaks stay applied; Undo Tweaks restores them.")
            except Exception:
                pass
            try:
                self._pilot_sink("● Watching…", TAB_ACCENTS["Tweak"])
            except Exception:
                pass
            return
        try:
            current = set(get_tweak_state() or {})
        except Exception:
            current = set()
        keys = [k for k in fresh if k in current]
        if not keys:
            try:
                self._pilot_applied_keys = []
                self._pilot_fresh_keys = []
            except Exception:
                pass
            try:
                self.log("Game Session Auto-Pilot: session ended — settings already restored.")
            except Exception:
                pass
            try:
                self._pilot_sink("● Watching…", TAB_ACCENTS["Tweak"])
            except Exception:
                pass
            return
        try:
            by_key = {t.key: t for t in TABS.get("Tweak", [])}
            tasks = [by_key[k] for k in keys
                     if k in by_key and by_key[k].revert is not None]
        except Exception:
            tasks = []
        if not tasks:
            try:
                self._pilot_applied_keys = []
                self._pilot_fresh_keys = []
            except Exception:
                pass
            return
        try:
            self.log("Game Session Auto-Pilot: game closed — restoring session tweaks.")
            self._pilot_sink("Restoring settings…", COLORS["accent_yellow"])
            try:
                import threading as _th
                _th.Thread(target=show_toast,
                           args=("Game Mode OFF",
                                 "Session tweaks restored to your normal settings."),
                           daemon=True).start()
            except Exception:
                pass
            self.run_tasks("Tweak", tasks, mode="revert", quiet=True)
        except Exception:
            pass
        try:
            self._pilot_applied_keys = []
            self._pilot_fresh_keys = []
        except Exception:
            pass
        try:
            self._pilot_sink("● Watching…", TAB_ACCENTS["Tweak"])
        except Exception:
            pass

    # F02: exact allowlist — the old shell=True Popen ran every target
    # through cmd.exe (latent injection if any caller ever passed
    # non-constant input). Exes go via shell=False argv; documents
    # (.cpl/.msc) via os.startfile; anything else is refused.
    _LAUNCH_EXES = {
        "taskmgr": ("taskmgr",),
        "cleanmgr": ("cleanmgr",),
        "perfmon /rel": ("perfmon", "/rel"),
        "rstrui": ("rstrui",),
        "msinfo32": ("msinfo32",),
    }
    _LAUNCH_DOCS = ("powercfg.cpl", "devmgmt.msc")

    def _launch(self, command):
        try:
            if command in self._LAUNCH_EXES:
                subprocess.Popen(list(self._LAUNCH_EXES[command]), shell=False)
            elif command in self._LAUNCH_DOCS:
                os.startfile(command)  # noqa: S606 — allowlisted document only
            else:
                messagebox.showerror("Could Not Launch", f"Refusing to launch unknown target: {command!r}")
        except Exception as exc:
            messagebox.showerror("Could Not Launch", str(exc))

    def _launch_settings(self, uri: str):
        """Open a Windows Settings page via its ms-settings: URI."""
        try:
            os.startfile(uri)  # noqa: S606 — shell-resolved URI, not an exe
        except Exception as exc:
            messagebox.showerror("Could Not Open", f"Couldn't open Windows Settings:\n{exc}")

    # ---------------- Auto Maintenance dialog ---------------- #

    def _show_schedule_dialog(self):
        from app.config_persist import load_config
        from app.scheduler import (enable_schedule, disable_schedule,
                                   get_schedule_status, run_auto_update)

        config = load_config()
        enabled = config.get("schedule_enabled", False)
        update_enabled = config.get("schedule_update_enabled", False)
        freq = config.get("schedule_frequency", "weekly")
        time_str = config.get("schedule_time", "03:00")

        # ThemedModal chrome (was the lone fixed-420x420 native-titled
        # dialog): shared 800x600 card, X close, Escape, modal wait —
        # content layout below is unchanged
        modal = ThemedModal(self.root, title="Auto Maintenance",
                            accent=COLORS["accent_green"])
        dlg = modal._dlg

        enabled_var = tk.BooleanVar(value=enabled)
        # audit fix: held reference instead of dlg.winfo_children()[0] —
        # the positional lookup silently retargets if a widget is ever
        # added before the button (the label below was packed AFTER, but
        # the pattern was one reorder away from corrupting the wrong widget)
        main_toggle_btn = AnimatedButton(
            modal.body, text="On" if enabled else "Off",
            command=lambda: enabled_var.set(not enabled_var.get()),
            bg=COLORS["accent_green"] if enabled else COLORS["surface"],
            fg=COLORS["black"] if enabled else COLORS["text"], font=(F, 10, "bold"),
        )
        main_toggle_btn.pack(pady=(4, 4))
        # keep the button label in sync without recursion
        def _sync_toggle(*_):
            on = enabled_var.get()
            main_toggle_btn.config_text("Maintenance: On" if on else "Maintenance: Off")
            main_toggle_btn.set_style(bg=COLORS["accent_green"] if on else COLORS["surface"],
                                      fg=COLORS["black"] if on else COLORS["text"])
        enabled_var.trace_add("write", _sync_toggle)
        _sync_toggle()

        # --- Update Everything schedule (user request: scheduler expansion):
        # a second scheduled task that runs winget upgrade --all headless. ---
        upd_frame = tk.Frame(modal.body, bg=COLORS["bg"])
        upd_frame.pack(fill="x", padx=16, pady=(6, 2))
        upd_var = tk.BooleanVar(value=update_enabled)
        def _sync_upd(*_):
            on = upd_var.get()
            upd_btn.config_text("Update Everything: On" if on else "Update Everything: Off")
            upd_btn.set_style(bg=COLORS["accent_green"] if on else COLORS["surface"],
                              fg=COLORS["black"] if on else COLORS["text"])
        upd_btn = AnimatedButton(
            upd_frame, text="Update Everything: On" if update_enabled else "Update Everything: Off",
            command=lambda: upd_var.set(not upd_var.get()),
            bg=COLORS["accent_green"] if update_enabled else COLORS["surface"],
            fg=COLORS["black"] if update_enabled else COLORS["text"], font=(F, 9, "bold"),
        )
        upd_btn.pack(anchor="w")
        upd_var.trace_add("write", _sync_upd)
        _sync_upd()
        tk.Label(upd_frame, text="also runs 'winget upgrade --all' on the schedule below — apps stay current automatically",
                 font=(F, 8), bg=COLORS["bg"], fg=COLORS["subtext"],
                 wraplength=380, justify="left").pack(anchor="w")
        # 'update now' shortcut: headless update without scheduling anything
        # C-1 audit fix: close through the modal (scrim + bindings + closed
        # flag), not by destroying its Toplevel — the old _run_update_now(dlg)
        # leaked a mapped scrim + 3 root bindings behind the run.
        AnimatedButton(upd_frame, text="Update Everything Now", command=lambda: self._run_update_now(modal),
                       bg=COLORS["surface"], fg=COLORS["text"], font=(F, 9),
                       padx=12, pady=5).pack(anchor="w", pady=(4, 0))

        freq_frame = tk.Frame(modal.body, bg=COLORS["bg"])
        freq_frame.pack(fill="x", padx=16, pady=6)
        tk.Label(freq_frame, text="How often:", bg=COLORS["bg"], fg=COLORS["text"],
                 font=(F, 9)).pack(side="left")
        freq_var = tk.StringVar(value=freq)
        freq_combo = tk.OptionMenu(freq_frame, freq_var, "daily", "weekly", "monthly")
        freq_combo.config(bg=COLORS["surface"], fg=COLORS["text"],
                          activebackground=COLORS["surface_hover"], activeforeground=COLORS["text"],
                          highlightthickness=0, bd=0, relief="flat",
                          font=(F, 9), width=12, indicatoron=True)
        try:
            menu = freq_combo["menu"]
            menu.config(bg=COLORS["surface"], fg=COLORS["text"],
                        activebackground=COLORS["surface_hover"], activeforeground=COLORS["text"],
                        bd=0, relief="flat", font=(F, 9))
        except Exception:
            pass
        freq_combo.pack(side="right")

        time_frame = tk.Frame(modal.body, bg=COLORS["bg"])
        time_frame.pack(fill="x", padx=16, pady=6)
        tk.Label(time_frame, text="Time (24h):", bg=COLORS["bg"], fg=COLORS["text"],
                 font=(F, 9)).pack(side="left")
        time_var = tk.StringVar(value=time_str)
        tk.Entry(time_frame, textvariable=time_var, width=10,
                 bg=COLORS["surface"], fg=COLORS["text"], insertbackground=COLORS["text"],
                 font=(F, 9), bd=0, highlightthickness=1,
                 highlightbackground=COLORS["hairline"], highlightcolor=COLORS["accent_green"]).pack(side="right")

        task_frame = tk.Frame(modal.body, bg=COLORS["bg"])
        task_frame.pack(fill="x", padx=16, pady=8)
        total_selected = sum(len(v) for v in config.get("selected_tasks", {}).values())
        tk.Label(task_frame, text=f"Tasks to run: {total_selected}",
                 bg=COLORS["bg"], fg=COLORS["subtext"], font=(F, 9)).pack(anchor="w")
        tk.Label(task_frame, text="(Tasks come from the last thing you ran)",
                 bg=COLORS["bg"], fg=COLORS["subtext"], font=(F, 9)).pack(anchor="w")

        status_var = tk.StringVar(value="")
        tk.Label(modal.body, textvariable=status_var, bg=COLORS["bg"],
                 fg=COLORS["accent_blue"], font=(F, 9), wraplength=340, justify="left").pack(anchor="w", padx=16)

        def refresh_status():
            # F14: reconcile live schtasks state into config (external
            # /Delete no longer leaves the toggle stale-True).
            try:
                from app.scheduler import resync_schedule_flag as _resync
                _resync()
                _resync(["--auto-update"])
            except Exception:
                pass
            ok, out = get_schedule_status()
            ok_upd, _ = get_schedule_status(["--auto-update"])
            parts = []
            parts.append("Auto maintenance: scheduled." if ok else "Auto maintenance: not scheduled.")
            parts.append("Update Everything: scheduled." if ok_upd else "Update Everything: not scheduled.")
            status_var.set("  ".join(parts))

        def apply():
            results = []
            # NOTE (audit HIGH fix): this dialog captured its config dict at
            # open time. Saving that WHOLE stale copy rolled back everything
            # a concurrent run wrote meanwhile (applied_tweaks, snapshots) —
            # and the four save_config() calls below were redundant anyway:
            # enable_schedule/disable_schedule already load a FRESH config
            # and persist exactly these schedule fields (scheduler.py).
            # So this function now only calls the scheduler helpers and
            # never persists the stale dict.
            # main maintenance schedule
            if enabled_var.get():
                ok, msg = enable_schedule(freq_var.get(), time_var.get())
                if ok:
                    results.append("Maintenance enabled")
                else:
                    messagebox.showerror("Failed", f"Could not create maintenance task:\n{msg}", parent=dlg)
            else:
                ok, msg = disable_schedule()
                if ok:
                    results.append("Maintenance disabled")
                else:
                    messagebox.showerror("Failed", f"Could not remove maintenance task:\n{msg}", parent=dlg)
            # update-everything schedule (separate schtasks entry)
            if upd_var.get():
                ok, msg = enable_schedule(freq_var.get(), time_var.get(), extra_args=["--auto-update"])
                if ok:
                    results.append("Update Everything enabled")
                else:
                    messagebox.showerror("Failed", f"Could not create update task:\n{msg}", parent=dlg)
            else:
                ok, msg = disable_schedule(["--auto-update"])
                if ok:
                    results.append("Update Everything disabled")
            if results:
                status_var.set("; ".join(results))
            refresh_status()

        btn_frame = tk.Frame(modal.body, bg=COLORS["bg"])
        btn_frame.pack(fill="x", padx=16, pady=(10, 16))
        AnimatedButton(btn_frame, text="Apply", command=apply,
                       bg=COLORS["accent_green"], fg=COLORS["black"], font=(F, 10, "bold"),
                       ).pack(side="right", padx=4)
        AnimatedButton(btn_frame, text="Close", command=modal.close,
                      bg=COLORS["surface"], fg=COLORS["text"], font=(F, 10),
                      ).pack(side="right")
        refresh_status()
        # C-5 audit fix: enter the modal wait like every other dialog (grab +
        # wait_window). Without this the schedule dialog floated grab-less:
        # keyboard focus stayed on the dimmed main window, and the modal
        # detectors (_modal_open/_prewarm_should_wait) could not see it, so
        # background chains kept building pages behind the open dialog.
        modal.wait()

    def _run_update_now(self, parent_modal=None):
        """'Update Everything Now' — runs the Install tab's Update button task
        through the standard worker flow (progress bar + Stop button),
        without touching any schedules.

        C-1 audit fix: close the schedule dialog through the modal's close()
        path (scrim + bindings + closed flag). The old parent_dlg.destroy()
        leaked a mapped scrim that dimmed the app for the rest of the
        session, leaked the scrim's root event bindings, and never set the
        modal's closed flag."""
        from app.tasks.install_tasks import UPDATE_ALL_TASK
        if parent_modal is not None:
            try:
                parent_modal.close()
            except Exception:
                pass
        self.install_selected_mixed([], [UPDATE_ALL_TASK])

    # ---------------- disk monitor (drive chips) ---------------- #
    # User request: show EVERY drive (internal, USB, SD — anything with a
    # drive letter), color-coded by fill level, click opens it in Explorer.
    # Rebuilt each tick so a freshly plugged USB stick shows up within one
    # poll (30s) without a restart. MTP phone storage is deliberately out
    # of scope: no drive letter, slow/flaky free-space queries.

    _MAX_INLINE_CHIPS = 3

    @staticmethod
    def _query_drive_free_total(root_path: str):
        """(free_bytes, total_bytes) for one drive root, or None if the
        query fails. Same Win32 call the pre-flight check and the drive
        chips use; wrapped here so the scorecard's before/after capture
        and any later caller share one honest reader."""
        try:
            import ctypes as _ct
            free = _ct.c_ulonglong(0)
            total = _ct.c_ulonglong(0)
            avail = _ct.c_ulonglong(0)
            if _ct.windll.kernel32.GetDiskFreeSpaceExW(
                    _ct.c_wchar_p(root_path), _ct.byref(avail),
                    _ct.byref(total), _ct.byref(free)):
                return free.value, total.value
        except Exception:
            pass
        return None

    @staticmethod
    def _query_drives():
        """All ready drives with free/total bytes. Uses GetLogicalDrives +
        GetDiskFreeSpaceExW (both instant, no WMI) — returns
        [(letter, root_path, free_gb, total_gb)], C: first, rest sorted."""
        import ctypes
        drives = []
        bitmask = ctypes.windll.kernel32.GetLogicalDrives()
        if not bitmask:
            return drives
        for i in range(26):
            if not (bitmask >> i) & 1:
                continue
            letter = chr(ord("A") + i)
            root = f"{letter}:\\"
            free = ctypes.c_ulonglong(0)
            total = ctypes.c_ulonglong(0)
            avail = ctypes.c_ulonglong(0)
            # drives with 0 total = no media (empty card reader slots) — skip
            res = ctypes.windll.kernel32.GetDiskFreeSpaceExW(
                ctypes.c_wchar_p(root),
                ctypes.byref(avail), ctypes.byref(total), ctypes.byref(free),
            )
            if res and total.value > 0:
                gb = 1024 ** 3
                drives.append((letter, root, free.value / gb, total.value / gb))
        # C: first (always the one people glance for), then alphabetical
        drives.sort(key=lambda d: (d[0] != "C", d[0]))
        return drives

    @staticmethod
    def _drive_fill_color(free_gb, total_gb):
        """Green normally, amber under ~15% free, red under ~5% — the
        'will this game fit?' glance check."""
        if total_gb <= 0:
            return COLORS["subtext"]
        ratio = free_gb / total_gb
        if ratio <= 0.05:
            return COLORS["accent_red"]
        if ratio <= 0.15:
            return COLORS["accent_yellow"]
        return COLORS["accent_green"]

    def _open_drive(self, root):
        """Open a drive in Explorer via os.startfile (ShellExecute) — the
        right route for folders; the browser-raising _open_url path is for
        web URLs only. Works on stripped Windows where webbrowser-based
        opens fail silently."""
        import os as _os
        try:
            _os.startfile(root)
        except Exception as exc:
            messagebox.showerror("Could Not Open Drive",
                                 f"Couldn't open {root}\n\n{exc}")

    def _show_extra_drives_menu(self):
        """The '+N ▾' overflow menu: every drive that didn't fit inline,
        with free space and the same color rules; click opens in Explorer."""
        if not self._drive_extra:
            return
        menu = tk.Menu(self.root, tearoff=0, bg=COLORS["surface"],
                       fg=COLORS["text"], activebackground=COLORS["surface_hover"],
                       activeforeground=COLORS["text"], bd=0,
                       font=(F, 9))
        for i, (letter, root, free_gb, total_gb) in enumerate(self._drive_extra):
            col = self._drive_fill_color(free_gb, total_gb)
            menu.add_command(
                label=f"{letter}:  {free_gb:.0f} GB free of {total_gb:.0f} GB",
                command=lambda r=root: self._open_drive(r),
            )
            try:
                menu.entryconfig(i, foreground=col)
            except Exception:
                pass
        try:
            menu.tk.call("tk_popup", menu, self.root.winfo_pointerx(),
                          self.root.winfo_pointery() + 12)
        except Exception:
            pass

    # ---------------- low-space nudge (feature 4) -------------------- #
    # A per-drive hysteresis state machine driven by the disk-monitor
    # tick: fire at <=5% free, clear only above 7% (a drive hovering at
    # the boundary must not re-toast every 30s). Firing = native toast
    # (in a thread — subprocess must never block the Tk thread) + an
    # amber chip in the status row that opens Storage Insight. A
    # background dry-run scan enriches the chip tooltip with a real
    # reclaimable number when it lands; the dialog itself always
    # re-scans live, so a stale estimate can never cause a wrong clean.

    _NUDGE_FIRE_RATIO = 0.05
    _NUDGE_CLEAR_RATIO = 0.07

    def _check_low_space(self, drives):
        """Tick entry — call with the monitor's drive list (Tk thread).
        Pure state transitions + widget updates; the only subprocess
        (the toast) is handed to a daemon thread."""
        try:
            by_letter = {d[0]: d for d in drives}
        except Exception:
            return
        prev = getattr(self, "_drive_was_red", {}) or {}
        now = {}
        try:
            for letter, (_l2, _root, free_gb, total_gb) in by_letter.items():
                ratio = (free_gb / total_gb) if total_gb and total_gb > 0 else 1.0
                was = prev.get(letter)
                if was is True:
                    is_red = ratio <= self._NUDGE_CLEAR_RATIO
                    if not is_red:
                        self._hide_nudge_chip(letter)   # recovered
                else:
                    is_red = ratio <= self._NUDGE_FIRE_RATIO
                    if is_red and letter not in getattr(self, "_nudge_dismissed", set()):
                        self._fire_low_space_nudge(letter, free_gb)
                now[letter] = is_red
            # drives that vanished (unplugged USB) take their chips along
            for letter in list(getattr(self, "_nudge_chips", {}) or {}):
                if letter not in by_letter:
                    self._hide_nudge_chip(letter)
            # in-place free-GB refresh for visible chips (no widget churn)
            for letter, (_l2, _root, free_gb, _t) in by_letter.items():
                chip = (getattr(self, "_nudge_chips", {}) or {}).get(letter)
                if chip is not None:
                    try:
                        lbl = chip.get("label")
                        if lbl is not None and lbl.winfo_exists():
                            lbl.config(text=f" ⚠ {letter}: {free_gb:.1f} GB free ")
                    except Exception:
                        pass
            self._drive_was_red = now
        except Exception:
            pass

    def _fire_low_space_nudge(self, letter, free_gb):
        """New red episode: toast (threaded) + chip + estimate scan."""
        try:
            import threading as _th
            _th.Thread(target=notify_low_space,
                       args=(letter, float(free_gb)),
                       daemon=True).start()
        except Exception:
            pass
        self._show_nudge_chip(letter, free_gb)
        self._ensure_nudge_estimate()

    def _nudge_chip_text(self, letter, free_gb):
        return f" ⚠ {letter}: {free_gb:.1f} GB free "

    def _nudge_tip_text(self, letter, free_gb, total_gb):
        base = (f"{letter}:\\ — {free_gb:.0f} GB free of {total_gb:.0f} GB "
                f"({(free_gb / total_gb * 100 if total_gb else 0):.0f}% free). "
                "Click for Storage Insight.")
        est = getattr(self, "_nudge_estimate", None)
        if est is not None:
            try:
                from app.utils import format_bytes as _fb
                import time as _ti
                base += (f"\n~{_fb(est[0])} of safe junk found "
                         f"(scanned {_ti.strftime('%H:%M', _ti.localtime(est[1]))}) — "
                         "click to review and clean.")
            except Exception:
                pass
        return base

    def _show_nudge_chip(self, letter, free_gb):
        chips = getattr(self, "_nudge_chips", None)
        frame_holder = getattr(self, "_nudge_frame", None)
        if chips is None or frame_holder is None:
            return
        if letter in chips:
            return
        try:
            if not frame_holder.winfo_exists():
                return
            # drives list for the tooltip totals (best-effort snapshot)
            total_gb = 0.0
            try:
                for _letter, _r, _f, _t in self._last_drives or []:
                    if _letter == letter:
                        total_gb = _t
                        break
            except Exception:
                pass
            box = tk.Frame(frame_holder, bg="#3A2C12")
            lbl = tk.Label(box, text=self._nudge_chip_text(letter, free_gb),
                           font=(F, 9, "bold"), bg="#3A2C12",
                           fg=COLORS["accent_yellow"], cursor="hand2")
            lbl.pack(side="left", padx=(8, 2), pady=3)
            lbl.bind("<Button-1>", lambda e: self._open_storage_insight())
            tip = Tooltip(lbl, self._nudge_tip_text(letter, free_gb, total_gb))
            x = tk.Label(box, text="✕", font=(F, 8), bg="#3A2C12",
                         fg=COLORS["subtext"], cursor="hand2")
            x.pack(side="left", padx=(0, 8))
            x.bind("<Button-1>", lambda e, l=letter: self._dismiss_nudge(l))
            Tooltip(x, "Dismiss for this session")
            box.pack(side="left", padx=(0, 6))
            chips[letter] = {"frame": box, "label": lbl, "tip": tip}
        except Exception:
            pass

    def _hide_nudge_chip(self, letter):
        try:
            chips = getattr(self, "_nudge_chips", None) or {}
            chip = chips.pop(letter, None)
            if chip is not None:
                try:
                    chip["frame"].destroy()
                except Exception:
                    pass
        except Exception:
            pass

    def _dismiss_nudge(self, letter):
        """Session dismissal: respected even if the drive re-reddens."""
        try:
            self._nudge_dismissed.add(letter)
        except Exception:
            pass
        self._hide_nudge_chip(letter)

    def _ensure_nudge_estimate(self):
        """One background dry-run scan per session (the Storage Insight
        engine): enriches chip tooltips with a real reclaimable number.
        Skipped while a run owns the disk, while one is already flying,
        or when an estimate already landed. Phase-6: consults the
        shared cache first (a Storage dialog walk often just filled it),
        and the walk itself is cancelable via the session token."""
        try:
            if getattr(self, "_nudge_estimate", None) is not None:
                return
            if getattr(self, "_nudge_scan_running", False):
                return
            with self._busy_lock:
                if self._busy:
                    return
            self._nudge_scan_running = True
        except Exception:
            return

        def _worker():
            try:
                import time as _ti
                from app.storage_scan import cached_estimate, measure_all_cached
                # cache hit (Storage/Health walked recently): instant
                hit = cached_estimate()
                if hit is not None:
                    total, stamp = hit
                else:
                    token = getattr(self, "_nudge_scan_token", [False])
                    total = measure_all_cached(cancelled=lambda: token[0])
                    stamp = _ti.time()
                try:
                    # F10: no winfo_exists off-thread — landing on a dead
                    # root raises inside after() and is swallowed.
                    self.root.after(0, lambda: self._land_nudge_estimate(total, stamp))
                except Exception:
                    pass
            except Exception:
                pass
            finally:
                self._nudge_scan_running = False

        try:
            import threading as _th
            _th.Thread(target=_worker, daemon=True).start()
        except Exception:
            self._nudge_scan_running = False

    def _land_nudge_estimate(self, total, stamp):
        """Tk-thread landing for the estimate: store + refresh tooltips."""
        # F11: None = scan busy/cancelled — keep prior estimate, don't
        # store a None that _fb() would choke on.
        if total is None:
            return
        try:
            self._nudge_estimate = (total, stamp)
            for letter, chip in (getattr(self, "_nudge_chips", {}) or {}).items():
                try:
                    tip = chip.get("tip")
                    if tip is not None:
                        tip.text = self._refresh_chip_tip(letter)
                except Exception:
                    pass
        except Exception:
            pass

    def _refresh_chip_tip(self, letter):
        """Rebuild one chip's tooltip from live drive data + estimate."""
        try:
            for _letter, _r, _f, _t in self._last_drives or []:
                if _letter == letter:
                    return self._nudge_tip_text(letter, _f, _t)
        except Exception:
            pass
        return "Click for Storage Insight."

    def _rebuild_drive_chips(self, drives):
        """Recreate the inline chips (only when the drive set changed —
        labels update in place otherwise). C: always inline; overflow
        collapses into the '+N' menu chip."""
        for w in self._drive_chips_frame.winfo_children():
            w.destroy()
        self._drive_chips = []
        self._drive_extra = []
        inline, extra = drives[:self._MAX_INLINE_CHIPS], drives[self._MAX_INLINE_CHIPS:]
        for letter, root, free_gb, total_gb in inline:
            col = self._drive_fill_color(free_gb, total_gb)
            # fixed width stops the status row from shifting every poll as
            # the free-GB number changes digit count (audit minor CLS)
            lbl = tk.Label(self._drive_chips_frame,
                            text=f" {letter}: {free_gb:5.0f} GB free ",
                            font=(F, 9, "bold"), bg=COLORS["bg"], fg=col,
                            cursor="hand2")
            lbl.pack(side="left", padx=(6, 0))
            lbl.bind("<Button-1>", lambda e, r=root: self._open_drive(r))
            # Storage Insight (feature 3): right-click opens the insight
            # dialog; left-click keeps the long-standing Explorer behavior
            lbl.bind("<Button-3>", lambda e: self._open_storage_insight())
            Tooltip(lbl, f"{root} — {free_gb:.0f} GB free of {total_gb:.0f} GB. "
                         "Click to open in Explorer, right-click for Storage Insight.")
            self._drive_chips.append((lbl, root))
        if extra:
            self._drive_extra = extra
            more = tk.Label(self._drive_chips_frame,
                            text=f" +{len(extra)} drives ▾ ", font=(F, 9, "bold"),
                            bg=COLORS["bg"], fg=COLORS["subtext"], cursor="hand2")
            more.pack(side="left", padx=(6, 0))
            more.bind("<Button-1>", lambda e: self._show_extra_drives_menu())
            Tooltip(more, "More connected drives:\n" +
                    "\n".join(f"{d[0]}: {d[2]:.0f} GB free" for d in extra))

    def _start_disk_monitor(self):
        if hasattr(self, "_disk_monitor_after_id") and self._disk_monitor_after_id:
            try:
                self.root.after_cancel(self._disk_monitor_after_id)
            except Exception:
                pass
        self._disk_monitor_running = True

        # audit fix (M9): _query_drives() called GetDiskFreeSpaceExW for up
        # to 26 drive letters synchronously on the Tk thread every 30s (plus
        # once at startup). An unreachable mapped network drive can make
        # that Win32 call stall for its OS-level timeout, freezing the
        # entire UI (no repaint, no button clicks) until it returns. Query
        # in a daemon thread and marshal only the paint back to Tk via
        # root.after(0, ...).
        import threading as _threading

        def _apply_drives(drives):
            if not getattr(self, "_disk_monitor_running", False):
                return
            try:
                # remember the raw list for nudge tooltips (free/total)
                self._last_drives = list(drives or [])
                # rebuild widgets only when the drive SET changes (plug /
                # unplug); color+text refresh in place on every tick
                key = tuple(d[0] for d in drives)
                if key != self._drive_last_key:
                    self._drive_last_key = key
                    self._rebuild_drive_chips(drives)
                for lbl, root in self._drive_chips:
                    for letter, r2, free_gb, total_gb in drives:
                        if r2 == root:
                            lbl.config(
                                text=f" {letter}: {free_gb:5.0f} GB free ",
                                fg=self._drive_fill_color(free_gb, total_gb),
                            )
                            break
                # low-space nudge state machine (own try: a nudge bug must
                # never break the chip refresh above)
                try:
                    self._check_low_space(drives)
                except Exception:
                    pass
            except Exception:
                pass
            try:
                if getattr(self, "_disk_monitor_running", False) and self.root.winfo_exists():
                    self._disk_monitor_after_id = self.root.after(30000, update_disk)
            except Exception:
                pass

        def _query_then_apply():
            try:
                drives = self._query_drives()
            except Exception:
                drives = []
            # F10: flag-check only off-thread; dead root raises in after().
            try:
                if getattr(self, "_disk_monitor_running", False):
                    self.root.after(0, lambda: _apply_drives(drives))
            except Exception:
                pass

        def update_disk():
            if not getattr(self, "_disk_monitor_running", False):
                return
            _threading.Thread(target=_query_then_apply, daemon=True).start()

        try:
            self._disk_monitor_after_id = self.root.after(0, update_disk)
        except Exception:
            pass

    def _stop_disk_monitor(self):
        self._disk_monitor_running = False
        if hasattr(self, "_disk_monitor_after_id") and self._disk_monitor_after_id:
            try:
                self.root.after_cancel(self._disk_monitor_after_id)
            except Exception:
                pass
            self._disk_monitor_after_id = None

    # ---------------- log (in-memory only, #16 simplified per user) ------- #
    # No log window in the UI anymore — the log accumulates in memory and
    # "Export Logs" (Quick Tools menu) saves it to a .txt for debugging.

    def _build_log(self, parent):
        self._log_lines = []
        self._log_lines.append("Welcome to Cleaner Tool.")

    def log(self, text):
        def _do():
            self._log_lines.append(f"[{time.strftime('%H:%M:%S')}] {text}")
            if len(self._log_lines) > 8000:
                del self._log_lines[:2000]
        try:
            if threading.current_thread() is threading.main_thread():
                _do()
            else:
                self.root.after(0, _do)
        except Exception:
            pass

    # keep old internal name working for close-guard message
    def _log_full(self, text):
        self.log(text)

    def export_logs(self):
        """Save the full in-memory log to a .txt the user picks (user
        request: replaces the old View Details window — keeps debugging
        possible without console clutter in the UI)."""
        from tkinter import filedialog
        try:
            default_name = f"CleanerTool_Log_{time.strftime('%Y%m%d_%H%M%S')}.txt"
            path = filedialog.asksaveasfilename(
                parent=self.root,
                title="Export Logs",
                defaultextension=".txt",
                initialfile=default_name,
                filetypes=[("Text files", "*.txt"), ("All files", "*.*")],
            )
            if not path:
                return
            header = (
                f"Cleaner Tool log export\n"
                f"Exported: {time.strftime('%Y-%m-%d %H:%M:%S')}\n"
                f"Edition: {self._windows_edition_str()}\n"
                f"{'=' * 60}\n"
            )
            with open(path, "w", encoding="utf-8") as f:
                f.write(header + "\n".join(self._log_lines) + "\n")
            # user bug: exporting mid-run overwrote the live "Running X…"
            # status with "Log saved to …" and it never came back. When a
            # run is active the status belongs to the run — log only.
            if not getattr(self, "_busy", False):
                self.set_status(f"Log saved to {path}")
            self.log(f"Log exported to {path}")
        except Exception as exc:
            messagebox.showerror("Export Failed", f"Could not save the log:\n{exc}")

    def _windows_edition_str(self) -> str:
        try:
            import winreg
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                                r"SOFTWARE\Microsoft\Windows NT\CurrentVersion") as k:
                product = winreg.QueryValueEx(k, "ProductName")[0]
                display = winreg.QueryValueEx(k, "DisplayVersion")[0]
            return f"{product} ({display})"
        except Exception:
            return "Windows"

    # ---------------- status line (fading, #16) ---------------- #

    def set_status(self, text):
        def _do():
            try:
                if hasattr(self, "status_lbl") and self.status_lbl.winfo_exists():
                    # cancel pending fades
                    for aid in getattr(self, "_status_fade_ids", []):
                        try:
                            self.root.after_cancel(aid)
                        except Exception:
                            pass
                    self._status_fade_ids = []
                    self.status_lbl.config(text=text, fg=TAB_ACCENTS.get(self.active_tab, COLORS["accent_green"]))
                    # fade back to muted after 4s (unless running)
                    if not self._busy:
                        aid = self.root.after(4000, lambda: self._fade_status())
                        self._status_fade_ids.append(aid)
            except Exception:
                pass
        try:
            if threading.current_thread() is threading.main_thread():
                _do()
            else:
                self.root.after(0, _do)
        except Exception:
            pass

    def _fade_status(self):
        try:
            self.status_lbl.config(fg=COLORS["subtext"])
        except Exception:
            pass

    # ---------------- progress ---------------- #

    def _set_progress(self, value, maximum=None):
        def _do():
            try:
                # M10: `if maximum:` misrouted maximum=0 into the fraction
                # branch (and a real value/0 would raise); be explicit.
                if maximum is not None:
                    self.progress_bar.set_fraction(value / maximum if maximum else 0.0)
                else:
                    self.progress_bar.set_fraction(value)
            except Exception:
                pass
        try:
            self.root.after(0, _do)
        except Exception:
            pass

    def _stop_indeterminate(self):
        """Turn off the shimmer and paint the full bar — ON THE TK THREAD.

        F-003 fix: _run_tasks_worker used to call
        progress_bar.set_indeterminate(False) directly from the worker
        thread. set_indeterminate(False) does after_cancel + Canvas _draw
        (delete/all + item creation) — real Tcl calls. Tkinter is not
        thread-safe; the identical Install-tab path (install_selected_mixed
        _done) was already fixed to marshal via root.after for exactly this
        reason, but this call site never got the fix. Same shape as
        _set_progress above: try/except only guards the shutdown race."""
        def _do():
            try:
                self.progress_bar.set_indeterminate(False)
            except Exception:
                pass
        try:
            self.root.after(0, _do)
        except Exception:
            pass

    # ---------------- run flow ---------------- #

    @staticmethod
    def _preflight_check(tasks):
        """Plain-language notices before a long run: pending reboot, low
        disk, admin rights, network (only when a task looks like it needs
        the internet — install/repair-download tasks). Returns a list of
        notice strings; empty = all clear. Non-blocking (ask-to-continue
        in the caller, never a hard stop)."""
        notices = []

        # pending reboot (CBS RebootPending) — repair tasks work better after
        try:
            import winreg as _wr
            with _wr.OpenKey(_wr.HKEY_LOCAL_MACHINE,
                             r"SOFTWARE\Microsoft\Windows\CurrentVersion\Component Based Servicing\RebootPending"):
                notices.append("Windows is waiting for a restart — restarting first makes repairs more reliable.")
        except OSError:
            pass
        except Exception:
            pass

        # free disk on C:
        try:
            import ctypes as _ct
            free = _ct.c_ulonglong(0)
            total = _ct.c_ulonglong(0)
            avail = _ct.c_ulonglong(0)
            if _ct.windll.kernel32.GetDiskFreeSpaceExW(_ct.c_wchar_p("C:\\"),
                                                       _ct.byref(avail),
                                                       _ct.byref(total),
                                                       _ct.byref(free)):
                gb = free.value / (1024 ** 3)
                if gb < 5:
                    notices.append(f"Only {gb:.1f} GB free on C: — cleaning needs breathing room; free up space first.")
        except Exception:
            pass

        # admin rights when selected tasks need them (limited mode would
        # silently skip them — better said up front)
        if not is_admin():
            admin_tasks = [t for t in tasks if t.admin_required]
            if admin_tasks:
                notices.append(f"{len(admin_tasks)} task(s) need Administrator — they'll be skipped in limited mode "
                               "(restart the app as Administrator for the full run).")

        # network when a task is an internet task (Install tab always is;
        # repair DISM RestoreHealth downloads too)
        _net_keys = {t.key for t in tasks}
        _needs_net = any(k.startswith("install_") for k in _net_keys) \
                     or "dism_restorehealth" in _net_keys
        if _needs_net:
            try:
                from app.downloader import has_network
                if not has_network():
                    notices.append("No internet connection detected — some tasks need to download files.")
            except Exception:
                pass

        return notices

    def _set_buttons_enabled(self, enabled: bool):
        def _do():
            try:
                for tab in self.tabs.values():
                    tab.set_run_enabled(enabled)
                if getattr(self, "cancel_btn", None) is not None:
                    self.cancel_btn.set_enabled(not enabled)
                if not enabled:
                    self._progress_show()
                else:
                    self._progress_hide()
            except Exception:
                pass
        try:
            self.root.after(0, _do)
        except Exception:
            pass

    def _progress_show(self):
        try:
            # bottom frame = the progress bar's own parent; pack at its top
            self.progress_bar.pack(fill="x", pady=(6, 2))
            self.cancel_btn.pack(side="right")
            self._progress_hidden = False
        except Exception:
            pass

    def _progress_hide(self):
        try:
            self.progress_bar.pack_forget()
            self.cancel_btn.pack_forget()
            self._progress_hidden = True
        except Exception:
            pass

    def install_selected_mixed(self, apps, tasks):
        """Install-tab runner: checked Essentials tasks FIRST (Store brings
        winget, bundles before individual apps), then catalog apps — one
        worker, one progress bar, fully cancelable via the Stop button
        (install commands are registered with the cancel registry now)."""
        with self._busy_lock:
            if self._busy:
                _themed_showinfo(self.root, "Busy",
                                 "Please wait for the current operation to finish.",
                                 accent=COLORS["accent_yellow"])
                return
            self._busy = True

        # Admin gating for Essentials that need it (same rule as other tabs)
        if not is_admin():
            admin_blocked = [t for t in tasks if t.admin_required]
            if admin_blocked:
                names = ", ".join(t.label for t in admin_blocked)
                self.log(f"Limited mode: skipping {len(admin_blocked)} admin-only installer(s): {names}")
                tasks = [t for t in tasks if not t.admin_required]
                if tasks or apps:
                    _themed_showinfo(
                        self.root, "Administrator Required",
                        f"Skipped {len(admin_blocked)} installer(s) that need "
                        f"admin:\n{names}\n\nContinuing with the rest.",
                        accent=COLORS["accent_yellow"])
            if not tasks and not apps:
                _themed_showinfo(self.root, "Nothing to Run",
                                 "All selected installers need Administrator rights.",
                                 accent=COLORS["accent_yellow"])
                with self._busy_lock:
                    self._busy = False
                return

        self._cancel_requested = False
        for tab in self.tabs.values():
            tab.set_run_enabled(False)
        # user bug: the bar + Stop button never appeared during install /
        # update runs (only the tab buttons disabled) — a 10-minute
        # `winget upgrade --all` looked dead. Show them like run_tasks does.
        # Determinate % is impossible for winget batch runs, so indeterminate
        # + live per-package status lines (see _live_status_ctx) is the
        # honest signal.
        self._progress_show()
        self.cancel_btn.set_enabled(True)
        self.progress_bar.set_fraction(0.0)
        self.progress_bar.set_indeterminate(True)
        total = len(apps) + len(tasks)
        self.set_status(f"Installing {total} item(s)...")
        self.log(f"===== Installing {total} selected item(s) =====")

        ctx = TaskContext(
            log=self.log, set_status=self.set_status,
            cancelled=lambda: self._cancel_requested,
        )

        def _done(ok, summary, ok_n=0, fail_n=0):
            # audit fix: this ran on the WORKER thread — set_indeterminate/
            # set_run_enabled touch Tk widgets/Canvases, and Tkinter is not
            # thread-safe (rare non-deterministic crashes at install end).
            # Hop to the Tk thread first, like _set_buttons_enabled does.
            def _ui():
                self.progress_bar.set_indeterminate(False)
                self._set_progress(1, 1)
                self._progress_hide()
                self.cancel_btn.set_enabled(False)
                with self._busy_lock:
                    self._busy = False
                self._worker_thread = None
                self._cancel_requested = False
                for tab in self.tabs.values():
                    tab.set_run_enabled(True)
                # installs/updates change what `winget upgrade` reports —
                # re-scan so "Update Apps (N)" never shows a stale number.
                try:
                    install_tab = self.tabs.get("Install")
                    if install_tab is not None and hasattr(install_tab, "refresh_update_count"):
                        install_tab.refresh_update_count()
                except Exception:
                    pass
                self.set_status(summary)
                # Install results now use the same themed card language
                # as everything else (was the last white OS box in the app)
                _themed_showinfo(self.root, "Install Done", summary,
                                 accent=COLORS["accent_green"])
            try:
                self.root.after(0, _ui)
            except Exception:
                pass

        def _worker():
            ok_n, fail_n, stopped = 0, 0, False
            # 1) Essentials tasks (sequential, honest per-task errors)
            for task in tasks:
                if ctx.cancelled():
                    stopped = True
                    break
                self.set_status(f"Running {task.label}...")
                self.log(f"--- {task.label} ---")
                try:
                    task.run(ctx)
                    ok_n += 1
                except TaskCancelled as exc:
                    # audit minor 2: a cancelled update is stopped, not a
                    # failure — don't count it in the error box
                    stopped = True
                    self.log(f"  {task.label} was cancelled: {exc}")
                    break
                except Exception as exc:
                    fail_n += 1
                    self.log(f"  ! {task.label} failed: {exc}")
            # 2) Catalog apps — per-app honesty (audit fix: the old code did
            # ok_n += len(apps) whenever install_selected_apps didn't raise,
            # counting failed apps as installed; the runner now reports the
            # real per-app outcome)
            if not stopped and apps:
                from app.tasks.install_tasks import install_selected_apps as _runner
                try:
                    app_ok, app_fail = _runner(ctx, apps)
                except TaskCancelled as exc:
                    stopped = True
                    self.log(f"  app installs were cancelled: {exc}")
                    app_ok, app_fail = [], []
                except Exception as exc:
                    # _runner raises only when EVERY app failed — count them
                    # all as failed instead of dying with _done never called
                    # (which wedged the UI busy with a frozen progress bar).
                    self.log(f"  ! app installs failed: {exc}")
                    app_ok, app_fail = [], [a["name"] for a in apps]
                else:
                    # A mid-batch Stop breaks the runner without raising —
                    # report Stopped, not Complete.
                    if ctx.cancelled():
                        stopped = True
                ok_n += len(app_ok)
                fail_n += len(app_fail)
            if stopped:
                self.log("Stopped — remaining items were skipped.")
                _done(False, f"Stopped: {ok_n} finished before stopping.", ok_n, fail_n)
            else:
                summary = f"Install complete: {ok_n} succeeded" + (f", {fail_n} failed" if fail_n else "") + "."
                _done(True, summary, ok_n, fail_n)

        thread = threading.Thread(target=_worker, daemon=True)
        self._worker_thread = thread
        thread.start()

    def run_tasks(self, tab_name, tasks, mode="run", quiet=False):
        """quiet=True: pilot/headless mode — no interactive popups of any
        kind (preflight askyesno, warnings, admin gates) and no modal
        Scorecard at completion; the run reports via status line + toast
        only. A modal grab_set window must never appear over a running
        fullscreen game."""
        all_selected = list(tasks)  # H1: capture real selection before filtering
        if not is_admin():
            admin_tasks = [t for t in tasks if t.admin_required]
            if admin_tasks:
                names = ", ".join(t.label for t in admin_tasks)
                self.log(f"Limited mode: skipping {len(admin_tasks)} admin-only task(s): {names}")
                tasks = [t for t in tasks if not t.admin_required]
                if not quiet:
                    _themed_showinfo(
                        self.root,
                        "Administrator Required",
                        f"Skipped {len(admin_tasks)} admin-only task(s):\n{names}\n\n"
                        "Running the rest in limited mode.",
                        accent=COLORS["accent_yellow"],
                    )
            if not tasks:
                if not quiet:
                    _themed_showinfo(
                        self.root,
                        "Nothing to Run",
                        "All selected tasks require Administrator rights. Restart as Administrator to run them.",
                        accent=COLORS["accent_yellow"],
                    )
                return

        with self._busy_lock:
            if self._busy:
                if not quiet:
                    _themed_showinfo(self.root, "Busy", "Please wait for the current operation to finish.",
                                     accent=COLORS["accent_yellow"])
                return
            self._busy = True

        # ---- Pre-flight checks (user request): fail fast with plain
        # language instead of dying mid-DISM. Long-run = 5+ tasks or any
        # task flagged long-running by the Repair tab's nature. ----
        try:
            preflight = self._preflight_check(tasks)
        except Exception:
            preflight = []
        if preflight:
            if quiet:
                self.log("Pre-flight notices (quiet run): " + " | ".join(preflight))
            else:
                msg = "Before we start:\n\n" + "\n".join(f"  • {p}" for p in preflight)
                if not _themed_askyesno(self.root, "Pre-flight Check", msg + "\n\nStart anyway?",
                                        accent=COLORS["accent_yellow"]):
                    with self._busy_lock:
                        self._busy = False
                    return
                self.log("Pre-flight notices: " + " | ".join(preflight))

        # Blocking hazards only — FYI notes are logged, never a popup
        # (curated presets like Deep Clean / Recommended are safe by
        # design, so they run silent; Custom combos that truly conflict
        # still ask here — except quiet runs, which log and continue:
        # the pilot only ever runs its own curated preset).
        try:
            info_notes = check_info_notices(tasks)
        except Exception:
            info_notes = []
        if info_notes:
            for _note in dict.fromkeys(info_notes):
                self.log(_note)
        warnings = check_dangerous_combos(tasks)
        if warnings:
            warnings = list(dict.fromkeys(warnings))
            if quiet:
                self.log("Hazard combo (quiet run, continuing): " + " | ".join(warnings))
            else:
                msg = "Potentially problematic task combinations detected:\n\n" + "\n\n".join(warnings)
                msg += "\n\nDo you want to continue?"
                if not _themed_askyesno(self.root, "Confirm Task Combination", msg,
                                        accent=COLORS["accent_red"]):
                    with self._busy_lock:
                        self._busy = False
                    return

        # Save selection for scheduler (H1: run mode only, full pre-filter list)
        # C-3 audit fix: quiet (background pilot) runs must NOT overwrite the
        # saved selection — a game launch would otherwise silently replace the
        # user's last manual Tweak pick with the Game Session preset, and the
        # next scheduled --auto-clean would apply session tweaks unattended.
        if mode == "run" and not quiet:
            # F08: single-lock RMW.
            try:
                from app.config_persist import update_config as _update
                def _mut(cfg, tab_name=tab_name, all_selected=all_selected):
                    cfg.setdefault("selected_tasks", {})[tab_name] = [t.key for t in all_selected]
                _update(_mut)
            except Exception:
                pass

        self._cancel_requested = False
        self._set_buttons_enabled(False)
        self.progress_bar.set_fraction(0.0)
        self.progress_bar.set_indeterminate(True)
        self.set_status(f"Starting {len(tasks)} task(s)...")

        # Scorecard 'before' snapshot: C: free/total bytes captured on the
        # Tk thread right before the worker starts (one instant
        # GetDiskFreeSpaceExW call, same source as _preflight_check /
        # _query_drives). The worker reads it via self._run_before_bytes;
        # a failed read (None) simply renders the scorecard without the
        # before/after bar — never invented numbers.
        self._run_before_bytes = self._query_drive_free_total("C:\\")
        self._run_results = []       # per-task (label, status, bytes) tuples

        thread = threading.Thread(target=self._run_tasks_worker,
                                  args=(tab_name, tasks, mode, quiet), daemon=True)
        self._worker_thread = thread
        thread.start()

    def _request_cancel(self):
        if not self._busy:
            return
        self._cancel_requested = True
        try:
            from app.utils import cancel_current_command
            cancel_current_command()
        except Exception:
            pass
        self.log("Stop requested — finishing the current task, then stopping.")

    def _run_tasks_worker(self, tab_name, tasks, mode, quiet=False):
        verb = "Undoing" if mode == "revert" else "Running"
        self.log(f"===== {verb} {len(tasks)} {tab_name} task(s) =====")

        ctx = TaskContext(log=self.log, set_status=self.set_status,
                          cancelled=lambda: self._cancel_requested)

        total_bytes = 0
        completed, failed = 0, 0
        skipped_n = 0
        cancelled = False
        self._set_progress(0, len(tasks))

        # Scorecard collectors (worker-thread writes only; the dialog
        # builder runs on the Tk thread AFTER the worker joins its UI
        # hop, so no lock is needed — same single-writer pattern as the
        # existing counters above).
        results = getattr(self, "_run_results", None)
        if results is None:
            results = self._run_results = []

        for idx, task in enumerate(tasks):
            if ctx.cancelled():
                self.log("Cancelled by user — remaining tasks were skipped.")
                cancelled = True
                break
            self.set_status(f"{verb}: {task.label}...")
            func = task.revert if mode == "revert" else task.run

            if func is None:
                self.log(f"  ! No {'revert' if mode=='revert' else 'run'} for '{task.label}'")
                failed += 1
                results.append((task.label, "fail", 0))
                self._set_progress(idx + 1, len(tasks))
                continue
            task_bytes = 0
            try:
                result = func(ctx)
                if isinstance(result, int) and not isinstance(result, bool):
                    total_bytes += result
                    task_bytes = result
                elif isinstance(result, bool) and not result:
                    raise RuntimeError("Task returned False")
                completed += 1
                results.append((task.label, "ok", task_bytes))
                # Phase 2 (#14): keep the applied-tweak registry truthful
                if mode == "revert":
                    mark_tweak_reverted(task.key)
                else:
                    if task.revert is not None:  # it's a tweak
                        mark_tweak_applied(task.key)
            except TaskSkipped as exc:
                # B5 audit fix: a skip means nothing changed on this
                # machine — count it as completed-with-skip, log the
                # reason, but do NOT record the tweak as applied (the
                # '✓ Active' badge must show what is actually active).
                skipped_n += 1
                results.append((task.label, "skip", 0))
                self.log(f"  (skipped) {task.label}: {exc}")
            except TaskCancelled as exc:
                # F-2 audit fix: utils.run_cmd_checked deliberately raises
                # TaskCancelled for a user Stop (its H6 contract), and
                # install_selected_mixed already treats it as 'stopped' —
                # but this loop's generic `except Exception` below counted
                # the interrupted task as FAILED ("Running stopped early:
                # 0 succeeded, 1 failed" with an 'ERROR' line saying
                # 'cancelled by user'). Catch it first: the run was
                # stopped, not failed; no tweak is marked applied/reverted,
                # remaining tasks are skipped by the next iteration's
                # cancelled() check, and the summary reports 'stopped'.
                cancelled = True
                results.append((task.label, "stop", 0))
                self.log(f"  (stopped) {task.label}: {exc}")
            except Exception as exc:
                failed += 1
                results.append((task.label, "fail", 0))
                self.log(f"  ! ERROR in '{task.label}': {exc}")
            self._set_progress(idx + 1, len(tasks))

        # tasks never attempted (user Stop mid-run) show as 'stopped' rows
        # so the scorecard tells the whole story. Every attempted task
        # appended exactly one result, so len(results) == attempted count;
        # the remainder are the never-started ones. (Only after a real
        # stop — a completed loop leaves nothing remaining.)
        if cancelled and len(results) < len(tasks):
            for t in tasks[len(results):]:
                results.append((t.label, "stop", 0))

        # refresh applied state for badges
        try:
            self.tweak_state = get_tweak_state()
        except Exception:
            pass
        # audit fix: cached Undo bodies hold stale "✓ Active" badges built
        # from the PRE-run tweak_state. _clear_body_cache existed for exactly
        # this but had zero call sites — _enter_undo only popped the 'undo'
        # key, leaving preset/custom bodies stale after a revert. Drop all
        # cached bodies now that the machine state has actually changed.
        # H11: _clear_body_cache destroys widgets — marshal to the Tk
        # thread like every other worker→UI hop (direct call mutated the
        # cache dicts from the worker and leaked the widget handles).
        try:
            tweak_tab = self.tabs.get("Tweak")
            if tweak_tab is not None:
                self.root.after(0, tweak_tab._clear_body_cache)
        except Exception:
            pass

        needs_reboot = any(getattr(t, "risk", "") == "REBOOT REQUIRED" for t in tasks)
        # B5: skipped tasks (nothing to do on this machine) get their own
        # honest count instead of being lumped into 'succeeded'.
        summary_core = f"{verb} {'stopped early' if cancelled else 'complete'}: {completed} succeeded"
        if skipped_n:
            summary_core += f", {skipped_n} skipped (nothing to change)"
        summary_core += f", {failed} failed."
        summary_lines = [summary_core]
        if cancelled:
            summary_lines.append("Remaining tasks were skipped (cancelled by user).")
        if total_bytes > 0:
            summary_lines.append(f"Disk space freed: {format_bytes(total_bytes)}")
        if needs_reboot:
            summary_lines.append("Reboot system for changes to take effect.")
        summary = "\n".join(summary_lines)
        self.log("=" * 48)
        self.log(summary)
        if needs_reboot:
            self.log("⚠️ Reboot system for changes to take effect.")
        self.log("=" * 48)

        # F-003 fix: marshal through the Tk thread — was a direct
        # cross-thread Canvas call (see _stop_indeterminate docstring).
        self._stop_indeterminate()
        self._set_progress(1, 1)
        self.set_status(summary_lines[0] + (" — Reboot needed" if needs_reboot else ""))

        with self._busy_lock:
            self._busy = False
        self._worker_thread = None
        self._cancel_requested = False
        self._set_buttons_enabled(True)

        # Native completion toast — scheduled BEFORE the scorecard hop:
        # _done_popup blocks in the scorecard's modal wait_window(), so
        # any hop queued after it would only run once the user closes the
        # dialog — useless when the app is minimized (the exact case the
        # toast exists for: transient signal while the scorecard waits
        # unseen behind the taskbar). Tk fires same-delay after()s in
        # schedule order, so the toast decision always runs first. Both
        # the state() read and the toast fire on the Tk thread; the
        # worker only schedules. Headless scheduled runs never reach this
        # code at all (scheduler.py runs its own path, gui not imported).
        if not cancelled and tab_name == "Clean" and mode == "run":
            def _toast_if_minimized():
                try:
                    if self.root.state() != "iconic":
                        return
                    # audit minor 3: pass the failure count so the toast
                    # copy is truthful (only claims a clean completion
                    # when nothing failed)
                    threading.Thread(target=notify_clean_complete,
                                     args=(total_bytes, completed, failed, skipped_n),
                                     daemon=True).start()
                except Exception:
                    pass
            try:
                self.root.after(0, _toast_if_minimized)
            except Exception:
                pass

        def _done_popup():
            # Scorecard (user-approved 2026-09): the themed results card
            # replaces the generic Done/Stopped messagebox. Built on the
            # Tk thread with the worker's collected per-task rows; the
            # freed-space 'after' snapshot is read HERE (the run's
            # deletions have settled by the time this hop executes).
            # quiet (pilot) runs skip the modal entirely — a grab_set
            # window over a fullscreen game is hostile; toast + status
            # line carry the outcome instead.
            if quiet:
                try:
                    verb = "restored" if mode == "revert" else "applied"
                    self.set_status(f"Auto-Pilot: session tweaks {verb}.")
                except Exception:
                    pass
                return
            try:
                after = self._query_drive_free_total("C:\\")
            except Exception:
                after = None
            run_again = None
            if (not cancelled and tab_name == "Clean" and mode == "run"):
                # 'Clean Again' — re-run the same selection. The task list
                # is captured by closure; in preset mode the page still
                # holds the same preset, in custom the same toggles.
                same_tasks = list(tasks)
                def run_again():
                    self.run_tasks(tab_name, same_tasks, mode="run")
            try:
                card = ScorecardDialog(
                    self.root,
                    tab_name=tab_name, mode=mode, cancelled=cancelled,
                    results=list(getattr(self, "_run_results", []) or []),
                    total_bytes=total_bytes,
                    before=getattr(self, "_run_before_bytes", None),
                    after=after,
                    needs_reboot=needs_reboot,
                    on_run_again=run_again,
                )
                card.wait()
            except Exception:
                # the scorecard must never mask a run's outcome — fall
                # back to the old messagebox if building it fails
                try:
                    if cancelled:
                        _themed_showinfo(self.root, "Stopped", summary,
                                         accent=COLORS["accent_yellow"])
                    else:
                        _themed_showinfo(self.root, "Done", summary,
                                         accent=COLORS["accent_green"])
                except Exception:
                    pass
        # H6-shutdown race: after() on a destroyed root (window closed
        # mid-run) raises TclError out of the worker — guard like the
        # sibling hops (_set_progress/_set_buttons_enabled).
        try:
            self.root.after(0, _done_popup)
        except Exception:
            pass


def launch():
    root = tk.Tk()
    Application(root)
    root.mainloop()
