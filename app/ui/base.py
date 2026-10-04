"""Modal foundation: the scrim, the themed modal base class, and Tooltip.

H4. Extracted verbatim from app/gui.py (blueprint B1.2).

Every one of the 22 feature dialogs inherits ThemedModal, so this sits
directly beneath them and above the widgets. It is also the only place that
owns the shared transparent-colour dispenser, which is why the palette
helpers came along in the same move.

Nothing here changed behaviourally.
"""
from __future__ import annotations

import tkinter as tk
from tkinter import messagebox

from app.config import FONT_FAMILY
from app.ui.theme import (
    COLORS, F, _next_transparent_color, _release_transparent_color,
    _round_rect_points,
)
# The themed message boxes moved in from app/gui.py build their buttons with
# AnimatedButton. This is base -> widgets, which is the allowed direction
# (widgets know nothing about modals), so it introduces no cycle.
from app.ui.widgets.button import AnimatedButton
# Tooltip is a widget (it moved to app/ui/widgets/tooltip.py so that the
# window chrome, which is also a widget, does not have to import upward).
# Re-exported here because most of the app has always imported it from base.
from app.ui.widgets.tooltip import Tooltip

# BUG-001: every feature dialog needs a safe way to hand work from a worker
# thread to the Tk thread, and the only correct way is TkDispatcher -- calling
# `widget.after(0, fn)` from a worker is itself a Tk call from a foreign
# thread. On Python 3.14 that raises RuntimeError("main thread is not in main
# loop") outright; on 3.12 and earlier it is an intermittent dropped callback.
# Six dialogs had their own `_ui()` helper doing the wrong thing, which left
# them permanently wedged (DNS tester found 5 servers and painted zero rows).
#
# One dispatcher per modal, owned and stopped by the base class, so no dialog
# can reinvent the hop and no pump can outlive its dialog.
from app.ui.tkdispatch import TkDispatcher


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

    # Fallback size for a feature dialog when the parent's size cannot be
    # read (parent not mapped yet, no display, headless harness).
    #
    # Dialogs are otherwise sized PROPORTIONALLY to the window they open over,
    # via _proportional_size(). These were absolute: every dialog was 800x600
    # no matter how big the main window was, so after the main window grew the
    # popups looked like small letterboxes floating in it -- the user read that
    # as "not proportional". A dialog is subordinate to its parent, so it now
    # scales with it and keeps the same ratio on every screen size.
    WIDTH = 800
    HEIGHT = 600

    # Fraction of the PARENT window a dialog occupies. Deliberately under
    # 1.0 on both axes so the parent stays visible around the dialog -- that is
    # what makes it read as a popup rather than a second window.
    SIZE_FRACTION = (0.78, 0.82)

    # Hard limits, so a dialog is never uselessly small on a laptop and never
    # fills a 4K screen (at which point it stops looking like a popup at all).
    SIZE_MIN = (720, 520)
    SIZE_MAX = (1120, 880)

    RADIUS = 16
    _TITLE_H = 44

    @classmethod
    def _proportional_size(cls, parent):
        """(w, h) for a dialog over `parent`: a clamped fraction of its box.

        Returns the WIDTH/HEIGHT fallback whenever the parent's real size is
        unavailable -- an unmapped window reports 1x1, which would otherwise
        collapse every dialog to the minimum.
        """
        fallback = (cls.WIDTH, cls.HEIGHT)
        try:
            pw = int(parent.winfo_width())
            ph = int(parent.winfo_height())
        except Exception:
            return fallback
        # 1x1 is Tk's answer for "not laid out yet".
        if pw <= 1 or ph <= 1:
            return fallback
        frac_w, frac_h = cls.SIZE_FRACTION
        min_w, min_h = cls.SIZE_MIN
        max_w, max_h = cls.SIZE_MAX
        w = max(min_w, min(max_w, int(pw * frac_w)))
        h = max(min_h, min(max_h, int(ph * frac_h)))
        return w, h

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
        w, h = size or self._proportional_size(root)
        try:
            ThemedModal._MODAL_OPEN[id(self)] = self
        except Exception:
            pass

        dlg = self._dlg = tk.Toplevel(root)
        # BUG-001: the cross-thread bridge, owned here so every dialog has one
        # and close() below stops it. Created immediately after the Toplevel
        # exists (the dispatcher captures it for after/after_cancel) and before
        # any subclass body runs, so a subclass __init__ can already post.
        # on_error routes into the dialog's own log where there is one,
        # otherwise it is dropped -- a posted callback that raises must never
        # break the pump or the other queued work, which TkDispatcher already
        # guarantees.
        self._dispatch = TkDispatcher(self._dlg, on_error=self._dispatch_error)
        self._dispatch.start()
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

    def _dispatch_error(self, text):
        """Where a posted callback's exception goes. Overridden by dialogs
        that keep a log widget; the default is to drop it, because TkDispatcher
        has already deduplicated and a dialog must not die in its close path."""
        return None

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
        # BUG-001: stop the cross-thread pump FIRST. After this the dispatcher
        # still accepts posts (it never raises) but nothing drains them, which
        # is exactly right for a dialog that is going away -- and it means a
        # worker that finishes late cannot paint into a destroyed Toplevel.
        try:
            self._dispatch.stop()
        except Exception:
            pass
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
        # BUG-010: a nested themed confirm/notice (every _themed_askyesno and
        # _themed_showinfo opens one) takes the grab and the keyboard focus.
        # On its close we released the grab and let Tk hand focus back to the
        # ROOT, not to the still-open parent dialog -- so after answering any
        # in-dialog question, Escape stopped closing that dialog and
        # keystrokes went to the main window behind it. Measured on the real
        # ProcessManagerDialog: focus_get() returned the root `.` and Escape
        # after the confirm did nothing; only the X glyph still dismissed it.
        #
        # Give the focus back to the window that is actually still up.
        try:
            if self._modal_root is not None and self._modal_root.winfo_exists():
                self._modal_root.focus_force()
        except Exception:
            pass
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




# --------------------------------------------------------------------------- #
# Themed message boxes (moved here from app/gui.py in H12)
# --------------------------------------------------------------------------- #
# Siblings of ThemedModal: the app's confirm/info prompts. They live
# with the modal base class because that is what they are built from -
# and five separate surfaces (TaskTab, InstallTab and three dialogs)
# call them, so they cannot live inside any one of those.

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










