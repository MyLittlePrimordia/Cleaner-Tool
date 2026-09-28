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
import sys

from app.config import APP_NAME, WINDOW_MIN_SIZE
from app.ui.widgets.tooltip import Tooltip
from app.ui.theme import COLORS, F, TAB_ACCENTS, _round_rect_points
from app.utils import resolve_asset_path


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
