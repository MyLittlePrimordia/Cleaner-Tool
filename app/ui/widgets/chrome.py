"""Borderless window chrome: custom titlebar, min/max/close, resize grips.

H4. Extracted verbatim from app/gui.py (blueprint B1.2).

The window is created with the appwindow style so the OS frame is hidden,
then this class paints a titlebar and corner grips. It engages lazily on the
first <Map> so the headless test harness never flashes a window, and falls
back to the native frame if any of it fails.

Takes the owning Application as `app` and reaches into it for a few
callbacks; that coupling is pre-existing and unchanged.
"""
from __future__ import annotations

import tkinter as tk

import pathlib

from app.config import APP_NAME, WINDOW_MIN_SIZE
from app.ui.widgets.tooltip import Tooltip
from app.ui.theme import (
    COLORS, FRAME_MS, ICON_CONTROL, ICON_INLINE, RADIUS_CHROME, SPACE_1, SPACE_2,
    SPACE_3, TYPE, _hex_lerp,
)
from app.ui.metrics import CARD_BODY_INSET, CONTROL_GAP, GROUP_GAP
from app.utils import resolve_asset_path

try:                              # Windows-only: the app is Windows-first
    import ctypes as _ct
    from ctypes import wintypes as _wt

    class _MONITORINFO(_ct.Structure):
        """Win32 MONITORINFO: rcWork is the monitor minus its taskbar."""
        _fields_ = [("cbSize", _wt.DWORD), ("rcMonitor", _wt.RECT),
                    ("rcWork", _wt.RECT), ("dwFlags", _wt.DWORD)]

    # EnumDisplayMonitors callback: (HMONITOR, HDC, LPRECT, LPARAM) -> BOOL.
    # The first WINFUNCTYPE argument is the RESULT type, not another arg —
    # leave it out and HMONITOR is swallowed as the restype, which calls the
    # callback with four args it was never told about.
    _MONITORENUMPROC = _ct.WINFUNCTYPE(_wt.BOOL, _ct.c_void_p, _ct.c_void_p,
                                       _ct.POINTER(_wt.RECT), _wt.LPARAM)

    _HAVE_WIN32 = True

    # Extended window styles for the snap preview. LAYERED carries the
    # alpha; TRANSPARENT makes mouse input fall through to whatever is
    # underneath, NOACTIVATE keeps it out of focus/Alt-Tab, TOOLWINDOW keeps
    # it out of the taskbar.
    _GWL_EXSTYLE = -20
    _WS_EX_LAYERED = 0x00080000
    _WS_EX_TRANSPARENT = 0x00000020
    _WS_EX_NOACTIVATE = 0x08000000
    _WS_EX_TOOLWINDOW = 0x00000080
except Exception:                 # pragma: no cover - non-Windows
    _ct = _wt = _MONITORINFO = _MONITORENUMPROC = None
    _HAVE_WIN32 = False
    _GWL_EXSTYLE = _WS_EX_LAYERED = _WS_EX_TRANSPARENT = 0
    _WS_EX_NOACTIVATE = _WS_EX_TOOLWINDOW = 0


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
    # Edge band: the shell-coloured strip the resize grips live in. Content is
    # laid INSIDE it (see content_box) so a grip can never sit on top of a
    # button the user is trying to click. Equal to GRIP because a band exactly
    # one grip wide is what the grips need and no more.
    PAD = GRIP
    # At rest the band is the shell colour, so it is not a border you see: a
    # page still looks edge-to-edge. It is revealed — one M3 tonal step up —
    # only while the pointer is in the band, which is also how the user learns
    # the edge resizes. `None` disables the reveal (the grips keep working).
    HOVER_BG = COLORS["surface"]
    FADE_MS = 120     # the reveal is an ease, not a switch
    # One step per frame: the band's cadence IS the app's tempo, so the ease
    # cannot drift to a second clock (that is what FRAME_MS names).
    FADE_STEP_MS = FRAME_MS
    # The pointer cursor is the other half of the reveal: a lit edge that
    # leaves an arrow cursor is an edge the user will not try to drag. NOTE
    # the two names that do NOT exist on Windows: "size_sw_ne" and
    # "size_se_nw" are rejected with "bad cursor spec", which is why the two
    # BOTTOM corners used to show no cursor at all — SE takes the NW_SE one
    # and SW takes the NE_SW one.
    GRIP_CURSORS = {
        "n": "sb_v_double_arrow", "s": "sb_v_double_arrow",
        "e": "sb_h_double_arrow", "w": "sb_h_double_arrow",
        "nw": "size_nw_se", "se": "size_nw_se",
        "ne": "size_ne_sw", "sw": "size_ne_sw",
    }
    RADIUS = RADIUS_CHROME   # the window's own corner, a distinct shape role
    TITLE_H = 40      # custom title bar height
    # How close to a work-area edge a dragged window must be released for the
    # move to snap there (the native Windows zone is about this wide).
    SNAP_PX = 20
    # A press that never really moved is a click, not a drag: snapping on it
    # would maximize the window every time the bar near the top is clicked.
    DRAG_SLOP = 3
    # The snap preview: a borderless, translucent, click-through window laid
    # over the tile a release would choose. The accent reads as "this is where
    # it goes" over both the desktop and the app's own dark surface.
    SNAP_PREVIEW_BG = COLORS["primary"]
    SNAP_PREVIEW_ALPHA = 0.28
    # It eases in and out at the band's cadence (FADE_MS / FADE_STEP_MS): the
    # preview is the only chrome that appears OUTSIDE the window, on someone
    # else's desktop, and a rectangle that pops there reads as a glitch.

    def __init__(self, root, app):
        self.root = root
        self.app = app
        self.engaged = False
        self.maximized = False
        self._pre_max = None      # (x, y, w, h) before maximizing
        self._drag = None
        self._preview = None      # the preview Toplevel, created on first use
        self._preview_fill = None  # its translucent fill frame
        self._preview_failed = False
        self._preview_rect = None  # (x, y, w, h) it is showing, or None
        self._preview_shown = False  # mapped (it may still be fading out)
        self._preview_alpha = 0.0    # opacity on screen, 0..SNAP_PREVIEW_ALPHA
        self._preview_alpha_applied = None   # last value handed to Tk
        self._preview_target = False         # fading towards opaque?
        self._preview_fade_after = None      # pending tick, or None
        # "system" | "on" | "off" from config.json, read on first use
        self._anim_pref = None
        self._transkey = None
        self._btns = {}
        self._corners = []
        self._titlebar = None
        self._engaging = False
        self._conf_after = None        # coalesced layout pass (perf fix)
        self._last_root_box = None
        self._hot = frozenset()        # bands aimed at (the fade follows)
        # per-band reveal progress 0..1; a corner follows the larger of its
        # two sides, so w -> e never snaps through either
        self._fade = {side: 0.0 for side in ("n", "w", "e", "s")}
        self._fade_after = None        # pending tick, or None
        self._band_bg = {}             # grip name -> colour already applied
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
            self._transkey = COLORS["shell"]
        root.deiconify()

        # 2) rounded corner masks (lifted over ALL content) — also the
        #    corner resize handles, see _build_corner_masks
        self._build_corner_masks()

        # 3) custom title bar (built once; _build_main_ui packs below it)
        self._build_titlebar()

        # 4) resize grips — edge frames in the band, shell-coloured until the
        #    pointer comes near (topmost below the corner masks)
        self._build_grips()

        # 4b) the band is invisible until the pointer comes near it
        self._bind_pointer()

        # 5) follow size changes: re-mask corners, keep grips glued
        root.bind("<Configure>", self._on_configure, add="+")

        self.engaged = True
        self._on_configure()

    def _build_corner_masks(self):
        """Four corner canvases: the rounded fillet, and the corner handles.

        They are lifted above the grips, so they are what receives a pointer
        (and a click) in the outermost corner pixels. That makes them the
        corner resize handle — a diagonal cursor there has to be backed by
        the action, or the chrome promises a resize it does not deliver.
        """
        root = self.root
        self._corners = []
        r = self.RADIUS if self._transkey != COLORS["shell"] else 0
        if not r:
            return              # no transparency: square corners, no masks
        for corner in ("nw", "ne", "sw", "se"):
            c = tk.Canvas(root, width=r + 2, height=r + 2,
                          bg=self._transkey, highlightthickness=0, bd=0)
            try:
                c.config(cursor=self.GRIP_CURSORS[corner])
            except Exception:
                pass            # cursor names vary by platform
            self._wire_grip(c, corner)
            self._corners.append(c)
        self._layout_corners()
        for c in self._corners:
            try:
                c.tk.call('raise', c._w)
            except Exception:
                pass

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
                              fill=COLORS["shell"], outline="")
        except Exception:
            pass

    # ---- custom title bar ----------------------------------------------- #

    def _build_titlebar(self):
        root = self.root
        bar = self._titlebar = tk.Frame(root, bg=COLORS["shell"], height=self.TITLE_H)
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
            self._title_icon_img = _load_title_icon(ICON_CONTROL)
        except Exception:
            self._title_icon_img = None
        if self._title_icon_img is not None:
            try:
                tk.Label(bar, image=self._title_icon_img, bg=COLORS["shell"],
                         bd=0, highlightthickness=0).pack(
                             side="left", padx=(SPACE_3, CONTROL_GAP), pady=SPACE_3)
            except Exception:
                self._title_icon_img = None
        if self._title_icon_img is None:
            icon = tk.Canvas(bar, width=ICON_INLINE, height=ICON_INLINE,
                             bg=COLORS["shell"],
                             highlightthickness=0, bd=0)
            try:
                icon.create_oval(2, 2, 14, 14, fill=COLORS["accent_green"],
                                 outline="")
                icon.create_oval(5, 5, 11, 11, fill=COLORS["shell"], outline="")
                icon.create_oval(6.5, 6.5, 9.5, 9.5, fill=COLORS["accent_green"],
                                 outline="")
            except Exception:
                pass
            icon.pack(side="left", padx=(CARD_BODY_INSET, SPACE_2), pady=SPACE_3)

        self._title_lbl = tk.Label(bar, text=APP_NAME, font=TYPE["title_medium"],
                                   bg=COLORS["shell"], fg=COLORS["text"])
        self._title_lbl.pack(side="left", pady=(0, 0))

        # window buttons: minimize, maximize, close (X = red on hover)
        def _mkbtn(sym, tip):
            b = tk.Label(bar, text=sym, font=TYPE["label_large"],
                         bg=COLORS["shell"], fg=COLORS["subtext"],
                         cursor="hand2", padx=GROUP_GAP, pady=SPACE_2)
            # M3 state layer: hover tints the container with on-surface @ 8%
            b.bind("<Enter>", lambda _e: b.config(
                fg=COLORS["text"], bg=COLORS["surface"]))
            b.bind("<Leave>", lambda _e: b.config(
                fg=COLORS["subtext"], bg=COLORS["shell"]))
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
        close.bind("<Enter>", lambda _e: close.config(
            fg=COLORS["accent_red"], bg=COLORS["alert_red_bg"]))
        close.bind("<Leave>", lambda _e: close.config(
            fg=COLORS["subtext"], bg=COLORS["shell"]))
        close.bind("<Button-1>", lambda _e: self.app._on_close())
        # CORNER-SAFE INSET (2026-10): the rounded-corner masks are lifted
        # ABOVE this bar and paint a shell-coloured fillet inside the top-right
        # RADIUS x RADIUS square. The close button used to sit 8 px from the
        # edge, so its top-right 8x10 px was painted over by that fillet (the
        # 'square over the exit button' artifact) and clicks there hit the
        # mask instead of the button. Keeping every window control outside the
        # corner arc fixes both, and stays correct if RADIUS grows.
        close.pack(side="right", padx=(0, self.RADIUS + SPACE_1),
                   pady=CONTROL_GAP)
        maxb = _mkbtn("❐", "Maximize")
        maxb.bind("<Button-1>", lambda _e: self.toggle_maximize())
        maxb.pack(side="right", padx=SPACE_1, pady=CONTROL_GAP)
        minb = _mkbtn("—", "Minimize")
        minb.bind("<Button-1>", lambda _e: _minimize())
        minb.pack(side="right", padx=SPACE_1, pady=CONTROL_GAP)
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
        bar.bind("<ButtonRelease-1>", self._drag_release, add="+")
        bar.bind("<Double-Button-1>", lambda _e: self.toggle_maximize(), add="+")
        self._title_lbl.bind("<Button-1>", self._drag_press, add="+")
        self._title_lbl.bind("<B1-Motion>", self._drag_move, add="+")
        self._title_lbl.bind("<ButtonRelease-1>", self._drag_release, add="+")
        self._title_lbl.bind("<Double-Button-1>", lambda _e: self.toggle_maximize(), add="+")

    # ---- drag / resize --------------------------------------------------- #


    def _drag_press(self, e):
        # a previous drag whose release never arrived could have left the
        # preview up; a new press is the start of a fresh decision either way
        self._hide_snap_preview()
        if self.maximized:
            self._drag = None
            return
        self._drag = {"wx": self.root.winfo_x(), "wy": self.root.winfo_y(),
                      "w": self.root.winfo_width(),
                      "h": self.root.winfo_height(),
                      "px": e.x_root, "py": e.y_root,
                      # read once per drag, not per motion event: the desktop
                      # cannot change mid-drag, and a press is a cheap moment
                      "area": self.work_area_bounds()}

    def _drag_move(self, e):
        d = self._drag
        if not d:
            return
        try:
            # move by pointer delta (robust against the 1-frame lag of
            # winfo_x under rapid drags), clamped so the window stays on the
            # desktop — a title bar dragged above the top edge takes the only
            # grab handle with it
            dx, dy = e.x_root - d["px"], e.y_root - d["py"]
            nx, ny = self.clamp_drag(d["wx"], d["wy"], d["w"], d["h"],
                                     dx, dy, d.get("area"))
            self.root.geometry(f"+{nx}+{ny}")
            self._preview_snap(nx, ny, d["w"], d["h"], e, dx, dy)
        except Exception:
            pass

    def _drag_release(self, e):
        """End a title-bar drag, snapping when it was released at an edge.

        Edges tile halves, the top maximizes, and the four corners tile
        quarters (see snap_rect).

        The snap is decided from where the window ended up, not from the
        pointer: the move clamp already stops the window at the work-area
        edge, so the two agree by construction even when the pointer ran on
        past it. A press-and-release with no real motion is a click (double
        click still maximizes) and snaps nothing.
        """
        d = self._drag
        self._drag = None
        self._hide_snap_preview()
        if not d or self.maximized:
            return
        try:
            dx = e.x_root - d["px"]
            dy = e.y_root - d["py"]
            if abs(dx) < self.DRAG_SLOP and abs(dy) < self.DRAG_SLOP:
                return
            # recomputed, not read back: Tk applies a geometry() change on
            # the next event-loop turn, so at release winfo_* can still be the
            # pre-motion rect (the same 1-frame lag _drag_move works around)
            x, y = self.clamp_drag(d["wx"], d["wy"], d["w"], d["h"],
                                   dx, dy, d.get("area"))
            w, h = d["w"], d["h"]
            # the screen under the window, not the whole desktop: a half or
            # quarter belongs to ONE screen, and the seam between two screens
            # is not one of its edges
            wa = self.work_area(x, y, w, h)
            rect = self.snap_rect(x, y, w, h, wa,
                                  pointer=(e.x_root, e.y_root))
            if not rect:
                return
            nx, ny, nw, nh = rect
            if wa and (nw, nh) == (wa[2] - wa[0], wa[3] - wa[1]):
                # a full work area IS maximize: go through the same door so
                # _pre_max is captured and the restore button agrees
                self.toggle_maximize()
            else:
                self.root.geometry(f"{nw}x{nh}+{nx}+{ny}")
        except Exception:
            pass

    # ---- snap preview ---------------------------------------------------- #

    def _preview_snap(self, x, y, w, h, e, dx, dy):
        """Show the tile a release right now would pick, or hide the preview.

        The inputs are exactly the ones `_drag_release` uses — the clamped
        rect, the screen under the window and the pointer — so the rectangle
        on screen is the rectangle the release will apply, not an estimate of
        it. A press that has not cleared DRAG_SLOP is still a click, and a
        click snaps nothing, so it previews nothing.
        """
        if abs(dx) < self.DRAG_SLOP and abs(dy) < self.DRAG_SLOP:
            self._hide_snap_preview()
            return
        wa = self.work_area(x, y, w, h)
        rect = self.snap_rect(x, y, w, h, wa, pointer=(e.x_root, e.y_root))
        if rect:
            self._show_snap_preview(rect)
        else:
            self._hide_snap_preview()

    def _snap_overlay(self):
        """The preview window, created on first use; None if Tk refuses.

        One window for the whole session rather than one per drag: creating a
        Toplevel mid-drag costs a mapping round-trip, and a window that is
        already mapped only needs a geometry change to jump to the next tile.
        """
        if self._preview is not None or self._preview_failed:
            return self._preview
        try:
            win = tk.Toplevel(self.root)
            win.withdraw()                  # configure while unmapped
            win.overrideredirect(True)      # no frame, no taskbar entry
            win.configure(bg=self.SNAP_PREVIEW_BG, bd=0, highlightthickness=0)
            # the fill is a Frame, not just the toplevel's own background: a
            # widget is what Tk definitely paints across the whole client
            # area, whatever the window manager does with the toplevel
            fill = tk.Frame(win, bg=self.SNAP_PREVIEW_BG, bd=0,
                            highlightthickness=0)
            fill.place(x=0, y=0, relwidth=1.0, relheight=1.0)
            self._preview_fill = fill
            try:
                win.attributes("-topmost", True)    # over the app it targets
            except Exception:
                pass
            try:
                win.attributes("-alpha", self.SNAP_PREVIEW_ALPHA)
            except Exception:
                pass            # opaque, but still a usable preview
            self._preview_click_through(win)
            self._preview = win
        except Exception:
            self._preview_failed = True     # never retry: a drag is no place
            self._preview = None            # to raise Tcl errors every pixel
        return self._preview

    @staticmethod
    def _preview_click_through(win):
        """Make the overlay a picture: input falls through it, no focus.

        The preview covers the very corner the user is dragging into, so it
        sits UNDER the pointer that is choosing the tile. Without
        WS_EX_TRANSPARENT it would take the button-up and the drag would end
        on the wrong window; WS_EX_NOACTIVATE keeps it from stealing focus
        from the app mid-gesture. Windows only, and best-effort.
        """
        if not _HAVE_WIN32:
            return
        try:
            hwnd = int(win.wm_frame(), 16)
            u = _ct.windll.user32
            # LONG_PTR is pointer-sized: the default c_int restype would
            # truncate the style bits on 64-bit Windows
            u.GetWindowLongPtrW.restype = _ct.c_ssize_t
            u.GetWindowLongPtrW.argtypes = [_wt.HWND, _ct.c_int]
            u.SetWindowLongPtrW.restype = _ct.c_ssize_t
            u.SetWindowLongPtrW.argtypes = [_wt.HWND, _ct.c_int, _ct.c_ssize_t]
            ex = u.GetWindowLongPtrW(hwnd, _GWL_EXSTYLE)
            u.SetWindowLongPtrW(hwnd, _GWL_EXSTYLE,
                                ex | _WS_EX_LAYERED | _WS_EX_TRANSPARENT
                                | _WS_EX_NOACTIVATE | _WS_EX_TOOLWINDOW)
        except Exception:
            pass

    def _show_snap_preview(self, rect):
        """Move the preview onto `rect`, fading it up the first time.

        Only a CHANGED rect talks to Tk: a drag emits a motion event per
        pixel, and the same tile must not be re-placed (or re-lifted) on
        every one of them. The fade is AIMED at rather than restarted, so a
        preview still fading out only has to change where it points.
        """
        win = self._snap_overlay()
        if win is None or not rect:
            return
        try:
            x, y, w, h = (int(round(v)) for v in rect)
            key = (x, y, max(1, w), max(1, h))
            if key == self._preview_rect and self._preview_shown:
                self._preview_target = True
                self._start_preview_fade()
                return
            win.geometry(f"{key[2]}x{key[3]}+{key[0]}+{key[1]}")
            if not self._preview_shown:
                # map it transparent and let the fade carry it up: mapping
                # at the target alpha IS the pop this fade exists to avoid.
                # With animation effects off, appearing at once is the point.
                self._preview_alpha = (0.0 if self._window_animations()
                                       else self.SNAP_PREVIEW_ALPHA)
                self._apply_preview_alpha(self._preview_alpha)
                win.deiconify()
                win.lift()
                self._preview_shown = True
                # Tk rewrites the window's extended style when it MAPS the
                # window (it manages topmost/tool-window itself), which drops
                # the bits set while unmapped — measured: 0x080800a4 before
                # the map, 0x00080088 after. Re-apply on idle, which still
                # runs before the next motion event reaches the bar.
                try:
                    win.after_idle(lambda: self._preview_click_through(win))
                except Exception:
                    pass
            self._preview_rect = key
            self._preview_target = True
            self._start_preview_fade()
        except Exception:
            self._preview_shown = False
            self._preview_rect = None

    def _hide_snap_preview(self):
        """Fade the preview out; safe to call when there is none.

        It is withdrawn only once it is fully transparent, so a drag that
        grazes an edge and leaves again never flashes a rectangle, and the
        tile dissolves instead of vanishing.
        """
        self._preview_rect = None
        self._preview_target = False
        if not self._preview_shown:
            return
        if self._preview_alpha <= 0.0:
            self._preview_withdraw()
            return
        self._start_preview_fade()

    def _preview_step(self):
        """Alpha covered by one frame, at the band reveal's cadence."""
        return self.SNAP_PREVIEW_ALPHA * (self.FADE_STEP_MS / float(self.FADE_MS))

    def _window_animations(self):
        """Should the chrome animate? The config override, else Windows.

        config.json's `window_animations` is "system" (follow Windows), "on"
        or "off"; the app's own chrome is the only thing it governs, because
        that is all this app draws. Read once and cached: this is consulted
        where a fade is about to begin, and a config load there would be a
        disk stat plus a deep copy on a hover path. The Settings tab calls
        `reload_animations()` after saving, so a toggle takes effect on the
        next hover instead of a restart later.
        """
        if self._anim_pref is None:
            self._anim_pref = self._read_anim_pref()
        if self._anim_pref == "on":
            return True
        if self._anim_pref == "off":
            return False
        return self._os_animations()

    @staticmethod
    def _read_anim_pref():
        """config.json's `window_animations`, or "system" if it is not there.

        Imported here rather than at module level: this module is imported by
        the app shell, and config_persist is the heavier side of that edge.
        """
        try:
            from app.config_persist import load_config
            pref = str(load_config().get("window_animations", "system")).lower()
        except Exception:
            return "system"
        return pref if pref in ("system", "on", "off") else "system"

    def reload_animations(self):
        """Re-read the `window_animations` preference (Settings saved one)."""
        self._anim_pref = None

    @staticmethod
    def _os_animations():
        """Does Windows want animations? True when it cannot be asked.

        SPI_GETCLIENTAREAANIMATION is the switch behind Settings >
        Accessibility > Visual effects > Animation effects. Reading it is
        cheap but not free, so it is consulted only where a fade is about to
        start — never on a motion path — and a failure to read it animates,
        which is what the app did before this existed.
        """
        if not _HAVE_WIN32:
            return True
        try:
            enabled = _ct.c_int(1)
            if _ct.windll.user32.SystemParametersInfoW(
                    0x1042, 0, _ct.byref(enabled), 0):
                return bool(enabled.value)
        except Exception:
            pass
        return True

    def _start_preview_fade(self):
        """Start the preview ticker unless one is already running.

        Same ticker trick as the band reveal: a running one reads its target
        from `_preview_target` on every step, so reversing mid-fade carries on
        from the alpha that is on screen instead of jumping.

        With animation effects off there is no step at all: the target alpha is
        applied in one go, and a fade-out withdraws immediately.
        """
        if self._preview_fade_after is not None:
            return
        settled = self.SNAP_PREVIEW_ALPHA if self._preview_target else 0.0
        if self._preview_alpha == settled:
            return                      # already there
        if not self._window_animations():
            self._preview_alpha = settled
            self._apply_preview_alpha(settled)
            if settled <= 0.0:
                self._preview_withdraw()
            return
        try:
            self._preview_fade_after = self.root.after(0, self._preview_tick)
        except Exception:
            self._preview_fade_after = None

    def _cancel_preview_fade(self):
        if self._preview_fade_after is None:
            return
        try:
            self.root.after_cancel(self._preview_fade_after)
        except Exception:
            pass
        self._preview_fade_after = None

    def _preview_tick(self):
        """One fade step, then reschedule until the alpha has arrived."""
        self._preview_fade_after = None
        step = self._preview_step()
        tgt = self.SNAP_PREVIEW_ALPHA if self._preview_target else 0.0
        cur = self._preview_alpha
        if cur < tgt:
            cur = min(tgt, cur + step)
        elif cur > tgt:
            cur = max(tgt, cur - step)
        self._preview_alpha = cur
        self._apply_preview_alpha(cur)
        if cur != tgt:
            try:
                self._preview_fade_after = self.root.after(
                    self.FADE_STEP_MS, self._preview_tick)
            except Exception:
                self._preview_fade_after = None
        elif cur <= 0.0:
            self._preview_withdraw()        # invisible: off the desktop

    def _apply_preview_alpha(self, value):
        """Push an alpha to the window, when it actually changes."""
        win = self._preview
        if win is None or value == self._preview_alpha_applied:
            return
        try:
            win.attributes("-alpha", value)
            self._preview_alpha_applied = value
        except Exception:
            pass

    def _preview_withdraw(self):
        """Take the preview off the desktop; it is fully transparent by now."""
        self._cancel_preview_fade()
        self._preview_shown = False
        self._preview_alpha = 0.0
        try:
            self._preview.withdraw()
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

    # ---- band geometry (one source of truth) ----------------------------- #
    #
    # Before 2026-10 the grips were 6px frames placed flush over the window
    # edges while the content host filled the window right up to those same
    # edges: every click in the outermost 6px of a page hit a grip instead of
    # the control under it (the fail the audit logged as B11). The fix is the
    # band this class now owns: the grips occupy a PAD-wide strip, and the
    # application lays its content inside content_box(). Both come from here
    # so the two can never drift apart again.

    @classmethod
    def content_box(cls, width, height):
        """Root-relative (x, y, w, h) the app may lay content into.

        Left/right/bottom lose the edge band; the top loses the title bar
        strip instead (the title bar IS the top band).
        """
        g = cls.PAD
        w = max(1, int(width) - 2 * g)
        h = max(1, int(height) - cls.TITLE_H - g)
        return (g, cls.TITLE_H, w, h)

    @classmethod
    def layout_specs(cls, g=None):
        """name -> place() kwargs for every grip.

        Each rect lies entirely inside the edge band: the corner grips are
        twice as WIDE as the band is tall (and vice versa), so their diagonal
        grab area never reaches past the band into content.
        """
        g = cls.GRIP if g is None else int(g)
        return {
            # edges
            "n": dict(x=0, y=0, relwidth=1.0, height=g),
            "s": dict(x=0, rely=1.0, y=-g, relwidth=1.0, height=g),
            "w": dict(x=0, y=0, width=g, relheight=1.0),
            "e": dict(relx=1.0, x=-g, y=0, width=g, relheight=1.0),
            # corners: 2g along the edge, g across it (inside the band)
            "nw": dict(x=0, y=0, width=g * 2, height=g),
            "ne": dict(relx=1.0, x=-g * 2, y=0, width=g * 2, height=g),
            "sw": dict(x=0, rely=1.0, y=-g, width=g * 2, height=g),
            "se": dict(relx=1.0, x=-g * 2, rely=1.0, y=-g,
                       width=g * 2, height=g),
        }

    @classmethod
    def hot_sides(cls, x, y, width, height):
        """Which edge bands hold the pointer at window (x, y).

        The four bands the chrome paints: left, right, bottom, and the 6 px
        top strip that sits above the title bar. That strip is the window's
        top edge, not part of the bar — the bar's own contents start below
        it, so nothing is lost to the resize.
        surface and drags the window, so it never lights up.
        """
        m = cls.PAD
        try:
            width, height = int(width), int(height)
        except (TypeError, ValueError):
            return frozenset()
        if width <= 0 or height <= 0:
            return frozenset()
        hot = set()
        if 0 <= x < m:
            hot.add("w")
        elif width - m <= x < width:
            hot.add("e")
        if 0 <= y < m:
            hot.add("n")
        if height - m <= y < height:
            hot.add("s")
        return frozenset(hot)

    @classmethod
    def place_content(cls, widget):
        """Lay `widget` into the window's content box (see content_box).

        The application calls this instead of placing its host itself, so
        the band the grips own and the box content gets are one decision.
        """
        g = cls.PAD
        widget.place(x=g, y=cls.TITLE_H, relwidth=1.0, width=-2 * g,
                     relheight=1.0, height=-(cls.TITLE_H + g))

    def _build_grips(self):
        root = self.root
        # 8 resize grips: 4 edges + 4 corners, all inside the band. Name IS
        # the side ("nw" resizes north and west), and the cursor comes from
        # one table for grips and corner masks alike.
        self._grip_widgets = {}
        self._band_bg = {}             # new widgets: nothing applied yet
        for name in ("n", "s", "e", "w", "nw", "ne", "sw", "se"):
            f = tk.Frame(root, bg=COLORS["shell"])
            try:
                f.config(cursor=self.GRIP_CURSORS[name])
            except Exception:
                pass          # cursor names vary by platform
            self._grip_widgets[name] = f
            self._wire_grip(f, name)
        self._layout_grips()

    def _layout_grips(self):
        try:
            gw = dict(self._grip_widgets or {})
            for name, spec in self.layout_specs().items():
                w = gw.get(name)
                if w is not None:
                    w.place(**spec)
            # Grips sit above the title bar: the outermost 6 px of the window
            # is the resize band on all four sides, exactly as a native frame
            # behaves. The bar's own contents (icon, title, buttons) start
            # inside the band, so nothing clickable is covered — the north
            # grip spans y 0..6 and the buttons are packed with pady=6.
            if self._titlebar is not None:
                self._titlebar.lift()
            for f in gw.values():
                f.lift()
            for c in self._corners:
                try:
                    c.tk.call('raise', c._w)   # Tk9-safe widget stacking
                except Exception:
                    pass
        except Exception:
            pass

    # ---- band reveal ------------------------------------------------------ #

    def _bind_pointer(self):
        """Watch the pointer so the band appears and disappears with it.

        Bound on the WINDOW, not per grip: a binding on the toplevel fires
        for events in any child (the toplevel is in every child's bindtag
        chain), so a pointer 3px inside the page still lights the band beside
        it. Bound once, at engage — never re-bound.
        """
        try:
            self.root.bind("<Motion>", self._on_pointer_move, add="+")
            self.root.bind("<Leave>", self._on_pointer_leave, add="+")
        except Exception:
            pass

    def _on_pointer_move(self, event=None):
        if not self.engaged or event is None:
            return
        try:
            x = int(event.x_root) - self.root.winfo_rootx()
            y = int(event.y_root) - self.root.winfo_rooty()
            w, h = self.root.winfo_width(), self.root.winfo_height()
        except Exception:
            return
        hot = self.hot_sides(x, y, w, h)
        if hot != self._hot:            # only on a change: no churn per event
            self._set_hot(hot)

    def _on_pointer_leave(self, _event=None):
        """Clear the reveal, but only when the pointer really left.

        Tk also sends <Leave> to the toplevel when the pointer crosses into a
        CHILD widget, so the event alone does not mean "gone" — asking where
        the pointer is avoids a flicker on every child crossing.
        """
        if not self.engaged or not self._hot:
            return
        here = False
        try:
            px, py = self.root.winfo_pointerxy()
            x0, y0 = self.root.winfo_rootx(), self.root.winfo_rooty()
            here = (x0 <= px < x0 + self.root.winfo_width()
                    and y0 <= py < y0 + self.root.winfo_height())
        except Exception:
            here = False
        if not here:
            self._set_hot(frozenset())

    def _set_hot(self, sides, animate=True):
        """Aim the band at `sides`; the fade follows, or snaps if not animated."""
        sides = frozenset(sides)
        changed = sides != self._hot
        self._hot = sides
        if self.HOVER_BG is None:      # reveal switched off: grips only
            return
        # the OS's animation switch is only consulted when the target actually
        # changes: this runs on every pointer move, and a user32 query there
        # would cost more than the fade it guards
        if animate and changed and not self._window_animations():
            animate = False            # the user asked for no animation
        if not animate:
            self._cancel_fade()
            for side in self._fade:
                self._fade[side] = 1.0 if side in self._hot else 0.0
            self._apply_band_colours()
            return
        self._start_fade()

    def _start_fade(self):
        """Start the ticker unless one is already running.

        A running ticker reads the targets from `_hot` on every step, so a
        pointer that leaves and re-enters mid-fade only changes where it is
        heading — it never restarts from a jump.
        """
        if self._fade_after is not None:
            return
        if all(self._fade[s] == (1.0 if s in self._hot else 0.0)
               for s in self._fade):
            return                      # already settled
        try:
            self._fade_after = self.root.after(0, self._fade_tick)
        except Exception:
            self._fade_after = None
            self._apply_band_colours()

    def _cancel_fade(self):
        if self._fade_after is None:
            return
        try:
            self.root.after_cancel(self._fade_after)
        except Exception:
            pass
        self._fade_after = None

    def _fade_tick(self):
        """One fade step, then reschedule until every band has arrived."""
        self._fade_after = None
        step = self.FADE_STEP_MS / float(self.FADE_MS)
        settled = True
        for side, cur in list(self._fade.items()):
            tgt = 1.0 if side in self._hot else 0.0
            if cur < tgt:
                cur = min(tgt, cur + step)
            elif cur > tgt:
                cur = max(tgt, cur - step)
            self._fade[side] = cur
            if cur != tgt:
                settled = False
        self._apply_band_colours()
        if not settled:
            try:
                self._fade_after = self.root.after(self.FADE_STEP_MS,
                                                   self._fade_tick)
            except Exception:
                self._fade_after = None

    def _apply_band_colours(self):
        """Paint the band from the current fade progress (no layout work)."""
        reveal = self.HOVER_BG
        if reveal is None:
            return
        shell = COLORS["shell"]
        for name, f in (self._grip_widgets or {}).items():
            # a corner follows the larger of its two sides; "n" is not a
            # band (the title bar owns the top edge), so it reads 0
            frac = max((self._fade.get(c, 0.0) for c in name), default=0.0)
            # the two ends use the tokens themselves: a lerp would return the
            # same RGB in lowercase, and exact tokens keep comparisons honest
            if frac <= 0:
                col = shell
            elif frac >= 1.0:
                col = reveal
            else:
                col = _hex_lerp(shell, reveal, frac)
            if self._band_bg.get(name) == col:
                continue                # already this colour: no Tcl traffic
            try:
                f.config(bg=col)
            except Exception:
                continue
            self._band_bg[name] = col

    @staticmethod
    def clamp_resize(x, y, w, h, side, dx, dy, wa, minimum=WINDOW_MIN_SIZE):
        """New (x, y, w, h) for a drag of (dx, dy) on `side`, inside `wa`.

        Pure: no Tk, no globals. The edge being dragged may not leave the
        work area (`wa` = left, top, right, bottom, taskbar excluded) and the
        window may not grow past it, because a window whose bottom sits under
        the taskbar cannot be grabbed back. The opposite edge is fixed — that
        is what makes a resize feel like a resize.

        `wa=None` (nothing readable) means no clamp at all. A work area
        smaller than `minimum` leaves the minimum winning, since a window the
        user cannot use is worse than one that overhangs.
        """
        minw, minh = minimum
        sides = list(side)                     # 'nw' -> ['n','w']
        nx, ny, nw, nh = x, y, w, h
        if "e" in sides:
            nw = max(minw, w + dx)
        if "s" in sides:
            nh = max(minh, h + dy)
        if "w" in sides:
            nw = max(minw, w - dx)
        if "n" in sides:
            nh = max(minh, h - dy)
        if wa:
            wa_l, wa_t, wa_r, wa_b = wa
            if "e" in sides:
                nw = min(nw, max(minw, wa_r - x))
            if "w" in sides:
                nw = max(minw, min(nw, x + w - wa_l))
            if "s" in sides:
                nh = min(nh, max(minh, wa_b - y))
            if "n" in sides:
                nh = max(minh, min(nh, y + h - wa_t))
        if "w" in sides:
            nx = x + w - nw
        if "n" in sides:
            ny = y + h - nh
        return nx, ny, nw, nh

    @staticmethod
    def clamp_drag(x, y, w, h, dx, dy, area):
        """New (x, y) for a title-bar drag of (dx, dy), inside `area`.

        Pure: no Tk, no globals. The window may not be moved off the desktop;
        the size is untouched, because that is what makes it a move rather
        than a resize. Without this, a fast drag parks the title bar above the
        work area's top edge (or past its side) and the only way back is
        Alt+Tab, the taskbar thumbnail or the tray.

        `area` is the desktop box from work_area_bounds(); `None` (nothing
        readable) leaves the drag alone. A window larger than `area` pins to
        its top-left corner rather than to the far edges, since the title bar
        is the end the user needs to reach.
        """
        nx, ny = x + dx, y + dy
        if area:
            l, t, r, b = area
            nx = min(max(l, nx), max(l, r - w))
            ny = min(max(t, ny), max(t, b - h))
        return nx, ny

    @classmethod
    def snap_rect(cls, x, y, w, h, wa, dist=None, minimum=WINDOW_MIN_SIZE,
                  pointer=None):
        """(x, y, w, h) a drag released at this rect should snap to, or None.

        Pure: no Tk, no globals. The edges that snap are the ones a user aims
        at, matching Windows Aero Snap: left and right take that half of the
        work area at full height, the top takes all of it (i.e. maximize), and
        each of the four corners takes that quarter. A quarter wins over the
        edge it shares, so the top-left is a quarter and not a maximize — the
        corner is the more specific target.

        Left/right are read from the window rect, because clamp_drag() stops
        the window at the edge and the pointer can then never follow it there
        (a title bar grabbed 400 px from the right edge leaves the cursor 400
        px short). Top/bottom are read from the POINTER, which keeps moving
        after the window has been clamped — a window taller than the screen
        sits flush on the bottom edge whenever it is dragged at all, so the
        rect there would turn every sideways drag into a bottom corner.
        Each rule reads the signal the clamp cannot fake. `pointer=None`
        falls back to the rect on both axes; `dist` is a 20 px zone on each
        axis, so a corner is a 20x20 target.

        A half is never narrower than `minimum`: the window declares
        `WINDOW_MIN_SIZE` to Tk (`root.minsize`), so a smaller request is
        refused — and refused by SIZE ALONE, which would leave the right-hand
        snap hanging past the right edge of the screen. Asking for at least
        the minimum here is the geometry the window manager would settle on
        anyway. On a screen at least twice as wide it is an exact half. The
        same floor applies per axis to a quarter, which can therefore be
        wider than half the screen; the right/bottom quarters stay pinned to
        their own edge so the minimum never pushes them off the work area.

        `wa=None` (nothing readable) means no snap.
        """
        if not wa:
            return None
        dist = cls.SNAP_PX if dist is None else int(dist)
        l, t, r, b = wa
        width, height = r - l, b - t
        if width <= 0 or height <= 0:
            return None
        half_w = min(width, max(max(1, minimum[0]), width // 2))
        half_h = min(height, max(max(1, minimum[1]), height // 2))
        left = x <= l + dist
        right = x + w >= r - dist
        if pointer:
            py = pointer[1]
            top, bottom = py <= t + dist, py >= b - dist
        else:
            top, bottom = y <= t + dist, y + h >= b - dist
        if (left or right) and (top or bottom):
            # a corner: the quarter anchored there. right/bottom hang off
            # their own edge, which is also what keeps an over-minimum
            # quarter inside the work area
            return (l if left else r - half_w,
                    t if top else b - half_h, half_w, half_h)
        if top:
            return (l, t, width, height)
        if left:
            return (l, t, half_w, height)
        if right:
            # when the half IS half, the odd pixel goes to the right side, so
            # an odd-width screen tiles edge to edge instead of leaving a
            # one-pixel seam down the middle
            right_w = width - half_w if half_w * 2 <= width else half_w
            return (r - right_w, t, right_w, height)
        return None

    def work_area_bounds(self):
        """Bounding box of every monitor's work area: the draggable desktop.

        A DRAG has to be clamped against something wider than one monitor,
        or a window could never be moved onto a second screen — it would stop
        dead at the first screen's edge. The union of all `rcWork` rects is
        that box: on a single screen it IS the work area (taskbar excluded),
        and on two screens side by side the pair reads as one region.

        `work_area()` (the resize clamp) deliberately stays per-monitor; a
        resize can only ever be about the edge under the pointer. Returns None
        when nothing can be read, and falls back to the primary work area.
        """
        if not _HAVE_WIN32:
            return None
        areas = []

        def _collect(hmon, _hdc, _lprc, _data):
            mi = _MONITORINFO()
            mi.cbSize = _ct.sizeof(_MONITORINFO)
            try:
                if _ct.windll.user32.GetMonitorInfoW(hmon, _ct.byref(mi)):
                    r = mi.rcWork
                    areas.append((int(r.left), int(r.top),
                                  int(r.right), int(r.bottom)))
            except Exception:
                pass
            return True

        try:
            proc = _MONITORENUMPROC(_collect)   # keep alive for the call
            _ct.windll.user32.EnumDisplayMonitors(None, None, proc, 0)
        except Exception:
            areas = []
        if areas:
            return (min(a[0] for a in areas), min(a[1] for a in areas),
                    max(a[2] for a in areas), max(a[3] for a in areas))
        try:
            wa = _wt.RECT()
            if _ct.windll.user32.SystemParametersInfoW(48, 0, _ct.byref(wa), 0):
                return (int(wa.left), int(wa.top), int(wa.right), int(wa.bottom))
        except Exception:
            pass
        return None

    def work_area(self, x, y, w, h):
        """(left, top, right, bottom) usable on the monitor holding the rect.

        `rcWork` rather than the full screen: the taskbar is part of the
        desktop the app may not cover. `MonitorFromRect` with
        MONITOR_DEFAULTTONEAREST picks the monitor the drag is happening on,
        so a window on a second screen clamps to that screen instead of to the
        primary. Returns None when nothing can be read (the drag then does not
        clamp), and falls back to the primary work area on any Win32 hiccup.
        """
        if not _HAVE_WIN32:
            return None
        try:
            rect = _wt.RECT(int(x), int(y), int(x) + int(w), int(y) + int(h))
            hmon = _ct.windll.user32.MonitorFromRect(_ct.byref(rect), 2)
            if hmon:
                mi = _MONITORINFO()
                mi.cbSize = _ct.sizeof(_MONITORINFO)
                if _ct.windll.user32.GetMonitorInfoW(hmon, _ct.byref(mi)):
                    r = mi.rcWork
                    return (int(r.left), int(r.top),
                            int(r.right), int(r.bottom))
        except Exception:
            pass
        try:
            wa = _wt.RECT()
            if _ct.windll.user32.SystemParametersInfoW(48, 0, _ct.byref(wa), 0):
                return (int(wa.left), int(wa.top), int(wa.right), int(wa.bottom))
        except Exception:
            pass
        return None

    def _wire_grip(self, f, side):
        """Make `f` resize the window from `side` ("s", or "nw" for corners)."""
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
            try:
                dx = e.x_root - st["sx"]
                dy = e.y_root - st["sy"]
                # work area of the monitor the window is being dragged on;
                # the clamp is pure, so it is unit-tested off the drag path
                wa = self.work_area(st["x"], st["y"], st["w"], st["h"])
                nx, ny, nw, nh = self.clamp_resize(
                    st["x"], st["y"], st["w"], st["h"], side, dx, dy, wa)
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


# --------------------------------------------------------------------------- #
# Window-level helpers (moved here from app/gui.py in H4)
# --------------------------------------------------------------------------- #
# ONE intentional deviation from a byte-identical move: _load_title_icon
# resolved its asset with `Path(__file__).with_name("assets") / "icon.ico"`,
# which is correct while the function lives in app/gui.py but would point at
# app/ui/widgets/assets/ from here. It now uses the existing
# utils.resolve_asset_path, which already probes the frozen _MEIPASS layouts
# and parents[1]/app/assets, so it is correct at any module depth. Verified
# to resolve to the same file.

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
    # Asset resolution delegates to utils.resolve_asset_path rather than
    # `Path(__file__).with_name("assets")`. That expression is only correct
    # while the caller sits directly in app/; from app/ui/... it points at a
    # directory that does not exist, so the icon silently fails to load. The
    # helper anchors on the app package and also probes the frozen _MEIPASS
    # layouts, so it is correct at any depth.
    try:
        _ico = resolve_asset_path("icon.ico")
        if _ico:
            root.iconbitmap(_ico)
    except Exception:
        pass


def _load_title_icon(target=ICON_INLINE):
    """Load the real app icon (icon.ico 🧼) as a Tk PhotoImage for
    the in-window title bar (user bug report: the placeholder green dot
    must be the actual icon). Stdlib-only ICO parse: modern .ico files
    embed PNGs — grab the entry that subsamples EXACTLY to `target`
    (no scaling blur), else the smallest entry >= target, else the
    largest available. Returns None on any failure so the caller falls
    back to the drawn dot. Frozen-exe paths included."""
    try:
        import base64 as _b64
        raw = None
        _ico = resolve_asset_path("icon.ico")
        if _ico:
            try:
                raw = pathlib.Path(_ico).read_bytes()
            except Exception:
                raw = None
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
