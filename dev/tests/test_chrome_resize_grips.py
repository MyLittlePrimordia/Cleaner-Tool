"""Audit B11: a resize grip must never sit above page content.

The grips are thin frames laid over the window edges, and the content host
used to fill the window right up to those same edges — so the outermost 6px of
every page was a resize handle: a click on the first pixels of a button began
a resize instead of pressing it. The chrome now owns a PAD-wide shell-coloured
band (BorderlessChrome.PAD) and the application lays its content inside
BorderlessChrome.content_box().

These tests pin the two halves against each other: the pure arithmetic, the
widgets Tk actually places, and the box place_content() hands the app.
"""
from __future__ import annotations

import pytest

from dev.tests._tkhelpers import make_root

tk = pytest.importorskip("tkinter")

from types import SimpleNamespace  # noqa: E402

from app.config import WINDOW_MIN_SIZE  # noqa: E402
from app.ui.theme import COLORS  # noqa: E402
from app.ui.widgets.chrome import BorderlessChrome  # noqa: E402  (needs tk)


def _overlaps(a, b):
    ax, ay, aw, ah = a
    bx, by, bw, bh = b
    return (ax < bx + bw and bx < ax + aw and ay < by + bh and by < ay + ah)


def _grip_box(spec, width, height):
    """Tk's own placement arithmetic for one `layout_specs()` entry.

    Mirrors `place(**spec)`: an absolute x/y plus a relative part, and a
    width/height that is either absolute or relative. Re-derived here rather
    than read from the app so the placed-grip test compares live widget
    geometry against the spec, not against the class that placed it.
    """
    x = float(spec.get("x", 0) or 0) + float(spec.get("relx", 0) or 0) * width
    y = float(spec.get("y", 0) or 0) + float(spec.get("rely", 0) or 0) * height
    w = spec.get("width")
    w = float(w) if w is not None else float(spec.get("relwidth", 0) or 0) * width
    h = spec.get("height")
    h = float(h) if h is not None else float(spec.get("relheight", 0) or 0) * height
    return (x, y, w, h)


SIZES = [(1040, 800), (760, 560), (1920, 1080), (401, 301)]


@pytest.mark.parametrize("width,height", SIZES)
def test_no_grip_rect_reaches_into_the_content_box(width, height):
    content = BorderlessChrome.content_box(width, height)
    # the box itself has to leave the band alone
    assert content[0] == BorderlessChrome.PAD
    assert content[1] == BorderlessChrome.TITLE_H
    assert content[0] + content[2] == width - BorderlessChrome.PAD
    assert content[1] + content[3] == height - BorderlessChrome.PAD

    for name, spec in BorderlessChrome.layout_specs().items():
        box = _grip_box(spec, width, height)
        assert box[2] > 0 and box[3] > 0, f"{name} grip has no area"
        assert not _overlaps(box, content), (
            f"{name} grip {box} covers content {content} in a "
            f"{width}x{height} window")
        # ...and it stays inside the window
        assert box[0] >= 0 and box[1] >= 0
        assert box[0] + box[2] <= width
        assert box[1] + box[3] <= height


def test_the_band_is_fully_covered_by_grips_along_each_edge():
    """No dead pixels: every point of the side/bottom band resizes.

    A gap between the edge strips would be a shell-coloured margin that
    neither resizes nor holds content — the click would land on the root and
    do nothing.
    """
    w, h = 1040, 800
    boxes = {name: _grip_box(spec, w, h)
             for name, spec in BorderlessChrome.layout_specs().items()}
    g = BorderlessChrome.PAD
    top = BorderlessChrome.TITLE_H

    def covered(px, py):
        return any(bx <= px <= bx + bw and by <= py <= by + bh
                   for bx, by, bw, bh in boxes.values())

    # left/right band, below the title bar and above the bottom strip
    for y in range(top + 1, h - g - 1, 17):
        assert covered(g - 1, y), f"left band gap at y={y}"
        assert covered(w - g, y), f"right band gap at y={y}"
    # bottom band, across the full width
    for x in range(1, w - 1, 23):
        assert covered(x, h - g), f"bottom band gap at x={x}"


def test_placed_grips_land_on_the_band_and_miss_the_content():
    root = make_root()
    try:
        root.geometry("900x640+40+40")
        chrome = BorderlessChrome(root, app=object())
        # don't let <Map> engage the borderless chrome inside the test
        try:
            root.unbind("<Map>", chrome._map_bind_id)
        except Exception:
            pass
        chrome._build_grips()
        root.update()
        w, h = root.winfo_width(), root.winfo_height()
        if w < 200 or h < 200:      # pragma: no cover - no real display
            pytest.skip("window geometry unavailable: %sx%s" % (w, h))

        content = BorderlessChrome.content_box(w, h)
        specs = BorderlessChrome.layout_specs()
        for name, widget in chrome._grip_widgets.items():
            box = (widget.winfo_x(), widget.winfo_y(),
                   widget.winfo_width(), widget.winfo_height())
            assert not _overlaps(box, content), (
                f"{name} grip at {box} covers content {content}")
            expected = tuple(int(round(v))
                             for v in _grip_box(specs[name], w, h))
            assert box == expected, f"{name} grip placed at {box}, want {expected}"
    finally:
        root.destroy()


def test_hot_sides_are_the_four_bands_and_nothing_else():
    g = BorderlessChrome.PAD
    w, h = 1000, 700
    hot = BorderlessChrome.hot_sides

    assert hot(0, 300, w, h) == frozenset({"w"})
    assert hot(g - 1, 300, w, h) == frozenset({"w"})
    assert hot(g, 300, w, h) == frozenset()        # first content pixel
    assert hot(w - 1, 300, w, h) == frozenset({"e"})
    assert hot(w - g, 300, w, h) == frozenset({"e"})
    assert hot(w - g - 1, 300, w, h) == frozenset()
    assert hot(500, h - 1, w, h) == frozenset({"s"})
    assert hot(500, h - g - 1, w, h) == frozenset()
    assert hot(500, 300, w, h) == frozenset()      # inside the page
    # the top strip is the window's fourth edge: the title bar's own contents
    # start below it, so the whole 6 px band resizes
    assert hot(500, 0, w, h) == frozenset({"n"})
    assert hot(500, g - 1, w, h) == frozenset({"n"})
    assert hot(500, g, w, h) == frozenset()        # first title-bar pixel
    assert hot(500, BorderlessChrome.TITLE_H, w, h) == frozenset()
    assert hot(0, 0, w, h) == frozenset({"n", "w"})
    assert hot(w - 1, 0, w, h) == frozenset({"n", "e"})
    assert hot(0, BorderlessChrome.TITLE_H - 1, w, h) == frozenset({"w"})
    assert hot(0, h - 1, w, h) == frozenset({"w", "s"})
    assert hot(w - 1, h - 1, w, h) == frozenset({"e", "s"})
    assert hot(5, 5, 0, 0) == frozenset()          # degenerate window


def test_every_grip_has_a_cursor_that_tk_accepts():
    """The reveal and the pointer must agree: a lit edge you cannot see a
    resize cursor on is an edge you will not try to drag.

    `size_sw_ne` / `size_se_nw` do not exist on Windows (Tk: "bad cursor
    spec"), which is how the two bottom corners ended up with no cursor.
    """
    root = make_root()
    try:
        for name, cur in BorderlessChrome.GRIP_CURSORS.items():
            f = tk.Frame(root)
            f.config(cursor=cur)          # raises TclError if Tk rejects it
            assert f.cget("cursor") == cur

        for name in ("w", "e"):
            assert BorderlessChrome.GRIP_CURSORS[name] == "sb_h_double_arrow"
        assert BorderlessChrome.GRIP_CURSORS["s"] == "sb_v_double_arrow"
        # NW/SE share the "north-west to south-east" cursor, NE/SW the other
        assert BorderlessChrome.GRIP_CURSORS["nw"] == "size_nw_se"
        assert BorderlessChrome.GRIP_CURSORS["se"] == "size_nw_se"
        assert BorderlessChrome.GRIP_CURSORS["ne"] == "size_ne_sw"
        assert BorderlessChrome.GRIP_CURSORS["sw"] == "size_ne_sw"
    finally:
        root.destroy()


def test_placed_grips_carry_their_cursor():
    root = make_root()
    try:
        chrome = BorderlessChrome(root, app=object())
        try:
            root.unbind("<Map>", chrome._map_bind_id)
        except Exception:
            pass
        chrome._build_grips()
        for name, f in chrome._grip_widgets.items():
            assert f.cget("cursor") == BorderlessChrome.GRIP_CURSORS[name], name
    finally:
        root.destroy()


def test_corner_masks_resize_and_show_the_diagonal_cursor():
    """The masks are lifted above the grips, so they own the outermost
    corner pixels — the diagonal cursor there has to be backed by the action."""
    root = make_root()
    try:
        chrome = BorderlessChrome(root, app=object())
        try:
            root.unbind("<Map>", chrome._map_bind_id)
        except Exception:
            pass
        chrome._transkey = "#010409"            # what _engage picks when supported
        chrome._build_corner_masks()
        assert len(chrome._corners) == 4
        for corner, c in zip(("nw", "ne", "sw", "se"), chrome._corners):
            assert c.cget("cursor") == BorderlessChrome.GRIP_CURSORS[corner]
            assert c.bind("<Button-1>"), f"{corner} mask is a dead corner"
            assert c.bind("<B1-Motion>"), f"{corner} mask cannot drag"
    finally:
        root.destroy()


WA = (0, 0, 1920, 1040)          # 1080p screen with a 40 px taskbar
MIN = (300, 200)                 # small minimums keep the arithmetic readable


def test_clamp_resize_keeps_the_dragged_edge_inside_the_work_area():
    c = BorderlessChrome.clamp_resize
    # inside: the drag is applied and the opposite edge does not move
    assert c(100, 100, 800, 600, "e", 100, 0, WA, MIN) == (100, 100, 900, 600)
    assert c(100, 100, 800, 600, "s", 0, 50, WA, MIN) == (100, 100, 800, 650)
    # past the right edge / the taskbar: the near edge stops there
    assert c(100, 100, 800, 600, "e", 5000, 0, WA, MIN) == (100, 100, 1820, 600)
    assert c(100, 100, 800, 600, "s", 0, 5000, WA, MIN) == (100, 100, 800, 940)
    # past the left / top: the opposite edge is the anchor, so it stays put
    assert c(100, 100, 800, 600, "w", -5000, 0, WA, MIN) == (0, 100, 900, 600)
    assert c(100, 100, 800, 600, "n", 0, -5000, WA, MIN) == (100, 0, 800, 700)
    # corners clamp both axes at once
    assert c(100, 100, 800, 600, "se", 5000, 5000, WA, MIN) == (100, 100, 1820, 940)
    assert c(100, 400, 800, 600, "nw", -5000, -5000, WA, MIN) == (0, 0, 900, 1000)


def test_clamp_resize_without_a_work_area_only_enforces_the_minimum():
    c = BorderlessChrome.clamp_resize
    assert c(100, 100, 800, 600, "e", 5000, 0, None, MIN) == (100, 100, 5800, 600)
    assert c(100, 100, 800, 600, "e", -5000, 0, None, MIN) == (100, 100, 300, 600)
    # a work area smaller than the minimum keeps the minimum: a window that
    # cannot be used is worse than one that overhangs
    tiny = (0, 0, 200, 150)
    assert c(0, 0, 800, 600, "e", -5000, 0, tiny, MIN)[2] == 300


def test_clamp_drag_keeps_the_window_on_the_desktop():
    c = BorderlessChrome.clamp_drag
    # inside: the pointer delta, and nothing else, moves the window
    assert c(100, 100, 800, 600, 50, -20, WA) == (150, 80)
    # past the right side / the taskbar: pinned to the far edges
    assert c(100, 100, 800, 600, 5000, 0, WA) == (1920 - 800, 100)
    assert c(100, 100, 800, 600, 0, 5000, WA) == (100, 1040 - 600)
    # past the left / top: the title bar may not leave through it
    assert c(100, 100, 800, 600, -5000, 0, WA) == (0, 100)
    assert c(100, 100, 800, 600, 0, -5000, WA) == (100, 0)
    # a diagonal drag clamps both axes
    assert c(100, 100, 800, 600, 5000, -5000, WA) == (1920 - 800, 0)
    # nothing readable: the drag is not clamped at all
    assert c(100, 100, 800, 600, 5000, 5000, None) == (5100, 5100)
    # a window bigger than the desktop pins to the top-left, where the
    # title bar still is — not to the bottom-right, where it is not
    assert c(10, 10, 4000, 3000, 500, 500, WA) == (0, 0)


def test_the_drag_box_contains_the_monitor_work_area():
    root = make_root()
    try:
        root.geometry("900x640+40+40")
        chrome = BorderlessChrome(root, app=object())
        root.update()
        w, h = root.winfo_width(), root.winfo_height()
        if w < 200 or h < 200:      # pragma: no cover - no real display
            pytest.skip("window geometry unavailable: %sx%s" % (w, h))
        # WINFUNCTYPE's first argument is the RESULT type: with it missing,
        # HMONITOR is eaten as the restype and every callback raises inside
        # ctypes (output only to stderr), so the box silently fell back to
        # the primary screen instead of covering all monitors.
        from app.ui.widgets import chrome as chrome_mod
        assert len(chrome_mod._MONITORENUMPROC._argtypes_) == 4

        area = chrome.work_area_bounds()
        if not area:                # pragma: no cover - non-Windows
            pytest.skip("no work area on this platform")
        x, y = root.winfo_x(), root.winfo_y()
        wa = chrome.work_area(x, y, w, h)
        assert wa
        # the desktop box is the union of monitors, so it can be wider than
        # the monitor under the window — never narrower, and never offset
        assert area[0] <= wa[0] and area[1] <= wa[1]
        assert area[2] >= wa[2] and area[3] >= wa[3]
    finally:
        root.destroy()


def test_a_title_bar_drag_cannot_leave_the_desktop():
    root = make_root()
    try:
        root.geometry("1000x760+40+40")
        chrome = BorderlessChrome(root, app=object())
        try:
            root.unbind("<Map>", chrome._map_bind_id)
        except Exception:
            pass
        chrome._build_titlebar()
        chrome.engaged = True
        root.update()
        w, h = root.winfo_width(), root.winfo_height()
        if w < 200 or h < 200:      # pragma: no cover - no real display
            pytest.skip("window geometry unavailable: %sx%s" % (w, h))
        area = chrome.work_area_bounds()
        if not area:                # pragma: no cover - non-Windows
            pytest.skip("no work area on this platform")
        rx, ry = root.winfo_rootx(), root.winfo_rooty()
        bar = chrome._titlebar

        def drag(dx, dy, release=False):
            # no release here: releasing AT the edge is a snap, which is the
            # next test's business — this one is about the move clamp alone
            bar.event_generate("<Button-1>", x=400, y=30,
                               rootx=rx + 400, rooty=ry + 30)
            bar.event_generate("<B1-Motion>", x=400, y=30,
                               rootx=rx + 400 + dx, rooty=ry + 30 + dy)
            if release:
                bar.event_generate("<ButtonRelease-1>", x=400, y=30,
                                   rootx=rx + 400 + dx, rooty=ry + 30 + dy)
            root.update()

        # 4000 px down-right: the window stops at the far edges, full size
        drag(4000, 4000)
        assert (root.winfo_width(), root.winfo_height()) == (w, h)
        assert root.winfo_x() == max(area[0], area[2] - w)
        assert root.winfo_y() == max(area[1], area[3] - h)

        # ...and 4000 px back up-left: it cannot go above or left of it
        rx, ry = root.winfo_rootx(), root.winfo_rooty()
        drag(-4000, -4000)
        assert (root.winfo_width(), root.winfo_height()) == (w, h)
        assert root.winfo_x() == area[0]
        assert root.winfo_y() == area[1]
    finally:
        root.destroy()


def test_snap_rect_takes_a_half_the_whole_or_a_quarter():
    s = BorderlessChrome.snap_rect
    full = (WA[2] - WA[0], WA[3] - WA[1])

    # left / right edges: half the screen at full height, pinned to the top.
    #
    # 1920 is narrower than twice the window minimum (980), so both halves are
    # 980 wide and meet in the middle — which is what the window manager does
    # with this app's declared minimum.
    assert s(0, 300, 800, 600, WA) == (0, 0, 980, 1040)
    right = s(1900 - 800, 300, 800, 600, WA)
    assert right == (940, 0, 980, 1040)
    assert right[0] + right[2] == WA[2]        # flush with the right edge
    # a screen wide enough for two real halves splits exactly, and an odd
    # width gives the spare pixel to the right side instead of a seam
    wide = (0, 0, 2560, 1040)
    assert s(0, 300, 800, 600, wide) == (0, 0, 1280, 1040)
    assert s(2560 - 800, 300, 800, 600, wide) == (1280, 0, 1280, 1040)
    odd = (0, 0, 2561, 1040)
    left = s(0, 300, 800, 600, odd)
    r = s(2561 - 800, 300, 800, 600, odd)
    assert (left, r) == ((0, 0, 1280, 1040), (1280, 0, 1281, 1040))
    assert left[0] + left[2] == r[0] and r[0] + r[2] == 2561
    # ...but only inside the release zone, which is measured from the edge
    assert s(BorderlessChrome.SNAP_PX + 1, 300, 800, 600, WA) is None
    assert s(1099, 300, 800, 600, WA) is None
    # the top edge, away from a corner, fills the work area (maximize)
    assert s(500, 0, 800, 600, WA) == (0, 0) + full
    # ...but a corner is a quarter, not a maximize: it is the narrower target
    assert s(0, 0, 800, 600, WA) == (0, 0, 980, 700)
    # the bottom edge on its own snaps nothing (no bottom half), only the
    # corners on it
    assert s(500, 1040 - 600, 800, 600, WA) is None
    # the middle of the screen snaps nothing
    assert s(500, 300, 800, 600, WA) is None
    # no work area, or a degenerate one: no snap
    assert s(0, 0, 800, 600, None) is None
    assert s(0, 0, 800, 600, (0, 0, 0, 0)) is None
    # a work area smaller than the minimum still snaps INSIDE itself: Tk
    # would refuse the width and leave the right-hand snap hanging off-screen
    tiny = (0, 0, 200, 150)
    for rect in (s(0, 300, 800, 600, tiny), s(-600, 300, 800, 600, tiny)):
        assert rect[0] >= 0 and rect[0] + rect[2] <= 200, rect
        assert rect[2] == 200, rect


def test_snap_rect_takes_a_quarter_at_each_corner():
    s = BorderlessChrome.snap_rect
    width, height = WA[2] - WA[0], WA[3] - WA[1]
    # per axis, floored at the window minimum (Tk refuses less) and never
    # wider than the work area itself
    qw = min(width, max(WINDOW_MIN_SIZE[0], width // 2))
    qh = min(height, max(WINDOW_MIN_SIZE[1], height // 2))
    w, h = 800, 600

    tl = s(WA[0], WA[1], w, h, WA)
    tr = s(WA[2] - w, WA[1], w, h, WA)
    bl = s(WA[0], WA[3] - h, w, h, WA)
    br = s(WA[2] - w, WA[3] - h, w, h, WA)
    assert tl == (WA[0], WA[1], qw, qh)
    assert tr == (WA[2] - qw, WA[1], qw, qh)
    assert bl == (WA[0], WA[3] - qh, qw, qh)
    assert br == (WA[2] - qw, WA[3] - qh, qw, qh)
    # every quarter hangs off the corner it belongs to, never past it
    for rect in (tl, tr, bl, br):
        assert rect[0] >= WA[0] and rect[0] + rect[2] <= WA[2], rect
        assert rect[1] >= WA[1] and rect[1] + rect[3] <= WA[3], rect

    # a screen big enough for real quarters tiles exactly: no seam, no gap
    big = (0, 0, 3000, 1600)
    q = s(0, 0, 800, 600, big), s(3000 - 800, 1600 - 600, 800, 600, big)
    assert q == ((0, 0, 1500, 800), (1500, 800, 1500, 800))

    # the release zone is two-dimensional: one axis at the edge is the half
    # or the maximize, both axes is the corner
    assert s(BorderlessChrome.SNAP_PX + 1, WA[1], w, h, WA) \
        == (WA[0], WA[1], width, height)
    assert s(WA[0], BorderlessChrome.SNAP_PX + 1, w, h, WA) \
        == (WA[0], WA[1], qw, height)
    # a corner quarter wins over the bottom/right edge it shares
    assert s(WA[2] - w, WA[3] - h, w, h, WA) == br

    # the vertical axis follows the POINTER. A window already flush on the
    # bottom edge — taller than the screen, or clamped there — must not read
    # as a bottom corner, or every sideways drag of a tall window would tile
    assert s(WA[2] - w, WA[3] - h, w, h, WA, pointer=(1800, 400)) \
        == (WA[2] - qw, WA[1], qw, height)
    assert s(WA[2] - w, WA[3] - h, w, h, WA, pointer=(1800, 1035)) == br
    assert s(WA[0], WA[1], w, h, WA, pointer=(500, 900)) \
        == (WA[0], WA[1], qw, height)
    # ...and a pointer in a corner is not a corner on its own: the side of the
    # window has to be there too
    assert s(400, WA[3] - h, w, h, WA, pointer=(900, 1035)) is None

    # no work area, or a degenerate one: no snap
    assert s(0, 0, w, h, None) is None
    assert s(0, 0, w, h, (0, 0, 0, 0)) is None
    # a work area smaller than the minimum still snaps INSIDE itself
    tiny = (0, 0, 200, 150)
    for rect in (s(0, 0, w, h, tiny), s(-600, -600, w, h, tiny)):
        assert rect[0] >= 0 and rect[0] + rect[2] <= 200, rect
        assert rect[1] >= 0 and rect[1] + rect[3] <= 150, rect


def test_a_release_near_an_edge_snaps_the_window_there():
    root = make_root()
    try:
        root.geometry("1000x760+60+60")
        chrome = BorderlessChrome(root, app=object())
        try:
            root.unbind("<Map>", chrome._map_bind_id)
        except Exception:
            pass
        chrome._build_titlebar()
        chrome.engaged = True
        root.update()
        w, h = root.winfo_width(), root.winfo_height()
        if w < 200 or h < 200:      # pragma: no cover - no real display
            pytest.skip("window geometry unavailable: %sx%s" % (w, h))
        wa = chrome.work_area(root.winfo_x(), root.winfo_y(), w, h)
        if not wa:                  # pragma: no cover - non-Windows
            pytest.skip("no work area on this platform")
        width = wa[2] - wa[0]
        # the window declares WINDOW_MIN_SIZE to Tk, which refuses a narrower
        # size — so on most screens the "half" is the minimum, not width // 2
        half = max(WINDOW_MIN_SIZE[0], width // 2)
        right_w = width - half if half * 2 <= width else half
        height = wa[3] - wa[1]
        bar = chrome._titlebar

        def drag_and_release(dx, dy):
            rx, ry = root.winfo_rootx(), root.winfo_rooty()
            bar.event_generate("<Button-1>", x=400, y=30,
                               rootx=rx + 400, rooty=ry + 30)
            bar.event_generate("<B1-Motion>", x=400, y=30,
                               rootx=rx + 400 + dx, rooty=ry + 30 + dy)
            bar.event_generate("<ButtonRelease-1>", x=400, y=30,
                               rootx=rx + 400 + dx, rooty=ry + 30 + dy)
            root.update()

        def box():
            return (root.winfo_x(), root.winfo_y(),
                    root.winfo_width(), root.winfo_height())

        # off the left edge -> left half of this screen
        drag_and_release(-4000, 0)
        assert box() == (wa[0], wa[1], half, height)

        # back to a normal window, then off the right edge -> right half
        root.geometry("1000x760+200+200")
        root.update()
        drag_and_release(4000, 0)
        assert box() == (wa[2] - right_w, wa[1], right_w, height)

        # off the top edge -> maximized, through the same path as the button
        root.geometry("1000x760+200+200")
        root.update()
        drag_and_release(0, -4000)
        assert chrome.maximized is True
        assert box() == (wa[0], wa[1], wa[2] - wa[0], height)

        # a press-and-release that never moved is a click, not a snap — even
        # with the bar already parked against the top edge, where a snap
        # would maximize the window out from under the click
        chrome.toggle_maximize()                # restore
        root.geometry("1000x760+200+%d" % wa[1])
        root.update()
        before = box()
        bar.event_generate("<Button-1>", x=400, y=30, rootx=1, rooty=1)
        bar.event_generate("<ButtonRelease-1>", x=400, y=30, rootx=2, rooty=1)
        root.update()
        assert chrome.maximized is False
        assert box() == before
    finally:
        root.destroy()


def test_a_release_in_a_corner_tiles_the_quarter():
    """A fling at a corner pins both edges there, so the release lands on it.

    Native Aero-snap corners: the quarter fills the corner it was dragged to
    and is flush against both of that corner's edges.
    """
    root = make_root()
    try:
        root.geometry("1000x760+60+60")
        chrome = BorderlessChrome(root, app=object())
        try:
            root.unbind("<Map>", chrome._map_bind_id)
        except Exception:
            pass
        chrome._build_titlebar()
        chrome.engaged = True
        root.update()
        w, h = root.winfo_width(), root.winfo_height()
        if w < 200 or h < 200:      # pragma: no cover - no real display
            pytest.skip("window geometry unavailable: %sx%s" % (w, h))
        wa = chrome.work_area(root.winfo_x(), root.winfo_y(), w, h)
        if not wa:                  # pragma: no cover - non-Windows
            pytest.skip("no work area on this platform")
        width, height = wa[2] - wa[0], wa[3] - wa[1]
        qw = min(width, max(WINDOW_MIN_SIZE[0], width // 2))
        qh = min(height, max(WINDOW_MIN_SIZE[1], height // 2))
        # on a display this small a quarter IS the work area, and maximize is
        # then the honest outcome
        full_area = (qw, qh) == (width, height)
        bar = chrome._titlebar

        def drag_and_release(dx, dy, at):
            # `at` differs per drag: the window returns to the same place, and
            # two Button-1s at the same spot microseconds apart are delivered
            # as a DOUBLE click, which maximizes instead of starting a drag
            rx, ry = root.winfo_rootx(), root.winfo_rooty()
            bar.event_generate("<Button-1>", x=at, y=30,
                               rootx=rx + at, rooty=ry + 30)
            bar.event_generate("<B1-Motion>", x=at, y=30,
                               rootx=rx + at + dx, rooty=ry + 30 + dy)
            bar.event_generate("<ButtonRelease-1>", x=at, y=30,
                               rootx=rx + at + dx, rooty=ry + 30 + dy)
            root.update()

        def box():
            return (root.winfo_x(), root.winfo_y(),
                    root.winfo_width(), root.winfo_height())

        for dx, dy, at, want in (
                (-4000, -4000, 240, (wa[0], wa[1], qw, qh)),
                (4000, -4000, 320, (wa[2] - qw, wa[1], qw, qh)),
                (-4000, 4000, 400, (wa[0], wa[3] - qh, qw, qh)),
                (4000, 4000, 480, (wa[2] - qw, wa[3] - qh, qw, qh))):
            if chrome.maximized:
                chrome.toggle_maximize()
            root.geometry("1000x760+%d+%d" % (wa[0] + 200, wa[1] + 200))
            root.update()
            drag_and_release(dx, dy, at)
            assert box() == want
            if not full_area:
                assert chrome.maximized is False
    finally:
        root.destroy()


def _preview_fade_step(chrome):
    """Advance the preview fade by exactly one frame.

    The pending `after` callback is cancelled first, so stepping by hand here
    does not land twice on the next step when the event loop runs.
    """
    chrome._cancel_preview_fade()
    chrome._preview_tick()


def _settle_preview_fade(chrome, limit=60):
    for _ in range(limit):
        if chrome._preview_fade_after is None:
            return
        _preview_fade_step(chrome)


def test_the_snap_preview_shows_the_tile_a_release_would_take():
    """The rectangle under the drag is the rectangle the release applies.

    Native Windows previews the tile as the pointer reaches an edge, so the
    user decides before letting go. The preview and the release both come
    from snap_rect, so they cannot disagree arithmetically — this pins the
    visible side: it appears, it follows from half to corner, it goes away
    when the drag leaves the zone, and what it showed is where the window
    lands.
    """
    root = make_root()
    try:
        root.geometry("1000x760+80+80")
        chrome = BorderlessChrome(root, app=object())
        try:
            root.unbind("<Map>", chrome._map_bind_id)
        except Exception:
            pass
        chrome._build_titlebar()
        chrome.engaged = True
        root.update()
        w, h = root.winfo_width(), root.winfo_height()
        if w < 200 or h < 200:      # pragma: no cover - no real display
            pytest.skip("window geometry unavailable: %sx%s" % (w, h))
        wa = chrome.work_area(root.winfo_x(), root.winfo_y(), w, h)
        if not wa:                  # pragma: no cover - non-Windows
            pytest.skip("no work area on this platform")
        width, height = wa[2] - wa[0], wa[3] - wa[1]
        half = min(width, max(WINDOW_MIN_SIZE[0], width // 2))
        half_h = min(height, max(WINDOW_MIN_SIZE[1], height // 2))
        right_w = width - half if half * 2 <= width else half
        bar = chrome._titlebar

        def preview_box():
            if not chrome._preview_shown or chrome._preview is None:
                return None
            p = chrome._preview
            return (p.winfo_x(), p.winfo_y(),
                    p.winfo_width(), p.winfo_height())

        def send(event, ax, ay, x):
            bar.event_generate("<%s>" % event, x=x, y=30, rootx=ax, rooty=ay)
            root.update()

        # a press on the bar alone shows nothing: there is no tile to show yet
        ax, ay = root.winfo_rootx() + 240, root.winfo_rooty() + 30
        send("Button-1", ax, ay, 240)
        assert preview_box() is None

        # flung past the left edge: the left half is previewed...
        send("B1-Motion", ax - 4000, ay, 240)
        assert preview_box() == (wa[0], wa[1], half, height)

        # ...and up into the corner, the SAME window moves to the quarter
        win = chrome._preview
        send("B1-Motion", ax - 4000, ay - 4000, 240)
        assert preview_box() == (wa[0], wa[1], half, half_h)
        assert chrome._preview is win

        # dragged back out of the zone: the preview fades away again
        send("B1-Motion", ax, ay + 300, 240)
        assert chrome._preview_target is False
        _settle_preview_fade(chrome)
        assert preview_box() is None
        send("ButtonRelease-1", ax, ay + 300, 240)
        assert preview_box() is None

        # ...and a fresh drag into the right edge ends exactly where the
        # preview was. `x` differs per press: two Button-1s at the same point
        # microseconds apart are delivered as a double click, which maximizes
        chrome._hide_snap_preview()
        root.geometry("1000x760+%d+%d" % (wa[0] + 200, wa[1] + 200))
        root.update()
        ax, ay = root.winfo_rootx() + 420, root.winfo_rooty() + 30
        send("Button-1", ax, ay, 420)
        send("B1-Motion", ax + 4000, ay, 420)
        assert preview_box() == (wa[2] - right_w, wa[1], right_w, height)
        send("ButtonRelease-1", ax + 4000, ay, 420)
        _settle_preview_fade(chrome)
        assert preview_box() is None
        assert (root.winfo_x(), root.winfo_y(),
                root.winfo_width(), root.winfo_height()) == \
            (wa[2] - right_w, wa[1], right_w, height)
    finally:
        root.destroy()


def test_a_click_previews_no_snap():
    """A press that jitters by a pixel is a click on the bar, not a drag.

    The preview follows the same DRAG_SLOP rule as the snap: without it, a
    click on a title bar parked against the top edge would flash the
    full-screen maximize preview out from under the pointer.
    """
    root = make_root()
    try:
        chrome = BorderlessChrome(root, app=object())
        try:
            root.unbind("<Map>", chrome._map_bind_id)
        except Exception:
            pass
        chrome._build_titlebar()
        chrome.engaged = True
        root.update()
        w, h = root.winfo_width(), root.winfo_height()
        if w < 200 or h < 200:      # pragma: no cover - no real display
            pytest.skip("window geometry unavailable: %sx%s" % (w, h))
        wa = chrome.work_area(root.winfo_x(), root.winfo_y(), w, h)
        if not wa:                  # pragma: no cover - non-Windows
            pytest.skip("no work area on this platform")
        root.geometry("1000x760+200+%d" % wa[1])     # flush with the top edge
        root.update()
        bar = chrome._titlebar
        ax, ay = root.winfo_rootx() + 300, root.winfo_rooty() + 30
        bar.event_generate("<Button-1>", x=300, y=30, rootx=ax, rooty=ay)
        bar.event_generate("<B1-Motion>", x=300, y=30, rootx=ax, rooty=ay + 1)
        root.update()
        assert chrome._preview_shown is False
        bar.event_generate("<ButtonRelease-1>", x=300, y=30, rootx=ax, rooty=ay + 1)
        root.update()
        assert chrome._preview_shown is False
        assert chrome.maximized is False
    finally:
        root.destroy()


def test_the_snap_preview_is_translucent_click_through_and_unfocusable():
    """What makes it a preview rather than a competing window.

    TRANSPARENT matters most: the preview covers the very corner the pointer
    is dragging into, so without it the button-up lands on the preview and
    the drag ends there. NOACTIVATE keeps the gesture's focus on the app, and
    the layer alpha is what lets the desktop show through at all.
    """
    from app.ui.widgets import chrome as chrome_mod

    if not chrome_mod._HAVE_WIN32:  # pragma: no cover - non-Windows
        pytest.skip("Win32 window styles unavailable")
    import ctypes

    root = make_root()
    try:
        chrome = BorderlessChrome(root, app=object())
        try:
            root.unbind("<Map>", chrome._map_bind_id)
        except Exception:
            pass
        # over the root, so the preview is drawn above the window it targets
        chrome._show_snap_preview((root.winfo_rootx() + 20,
                                   root.winfo_rooty() + 20, 300, 200))
        root.update()       # the style is re-applied on idle, after the map
        _settle_preview_fade(chrome)     # the alpha it settles on is the point
        win = chrome._preview
        if win is None:             # pragma: no cover - Tk refused
            pytest.skip("no preview window on this desktop")
        assert win.winfo_ismapped()

        user32 = ctypes.windll.user32
        user32.GetWindowLongPtrW.restype = ctypes.c_ssize_t
        user32.GetWindowLongPtrW.argtypes = [ctypes.c_void_p, ctypes.c_int]
        user32.GetLayeredWindowAttributes.argtypes = [
            ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32),
            ctypes.POINTER(ctypes.c_byte), ctypes.POINTER(ctypes.c_uint32)]
        hwnd = int(win.wm_frame(), 16)

        ex = user32.GetWindowLongPtrW(hwnd, -20)
        assert ex & 0x00000020, "missing WS_EX_TRANSPARENT: the drag breaks"
        assert ex & 0x08000000, "missing WS_EX_NOACTIVATE: it takes focus"
        assert ex & 0x00080000, "missing WS_EX_LAYERED: no translucency"
        assert user32.GetForegroundWindow() != hwnd

        # the alpha is the difference between a preview and a mask
        key, alpha, flags = ctypes.c_uint32(), ctypes.c_byte(), ctypes.c_uint32()
        assert user32.GetLayeredWindowAttributes(
            hwnd, ctypes.byref(key), ctypes.byref(alpha), ctypes.byref(flags))
        assert flags.value & 0x2                        # LWA_ALPHA
        want = 255 * chrome.SNAP_PREVIEW_ALPHA
        assert abs(alpha.value - want) <= 2, (alpha.value, want)

        # topmost: a tile behind the window it is aimed at is not a preview
        assert win.attributes("-topmost")
        # and it is not a second frame around the app: no title bar, and the
        # fill is a child that covers it edge to edge
        assert win.overrideredirect()
        assert chrome._preview_fill.cget("bg") == chrome.SNAP_PREVIEW_BG
    finally:
        root.destroy()


def test_the_snap_preview_eases_in_and_out():
    """120 ms at 16 ms a step, like the band reveal — not a pop.

    The preview is the one piece of chrome that appears outside the window,
    on whatever desktop is showing, so a rectangle that switches on reads as
    a glitch. It climbs alpha instead of jumping it, and the OUT is the same
    curve: it stays mapped and thins until it is transparent, only then going
    away, so a drag that grazes an edge never flashes.
    """
    root = make_root()
    try:
        chrome = BorderlessChrome(root, app=object())
        try:
            root.unbind("<Map>", chrome._map_bind_id)
        except Exception:
            pass
        chrome._show_snap_preview((60, 60, 300, 200))
        assert chrome._preview_shown
        assert chrome._preview_alpha == 0.0     # mapped transparent, then up

        up = []
        for _ in range(20):
            if chrome._preview_fade_after is None:
                break
            _preview_fade_step(chrome)
            up.append(chrome._preview_alpha)
        assert chrome._preview_alpha == chrome.SNAP_PREVIEW_ALPHA
        assert 5 <= len(up) <= 10, up               # several frames, not one
        assert up == sorted(up) and len(set(up)) == len(up)
        assert up[0] < chrome.SNAP_PREVIEW_ALPHA / 2
        assert float(chrome._preview.attributes("-alpha")) == \
            chrome.SNAP_PREVIEW_ALPHA

        chrome._hide_snap_preview()
        assert chrome._preview_shown                 # still on screen, thinning
        assert chrome._preview.winfo_ismapped()
        down = []
        for _ in range(20):
            if chrome._preview_fade_after is None:
                break
            _preview_fade_step(chrome)
            down.append(chrome._preview_alpha)
        assert chrome._preview_alpha == 0.0
        assert chrome._preview_shown is False
        assert down == sorted(down, reverse=True) and len(set(down)) == len(down)
        assert down[0] < chrome.SNAP_PREVIEW_ALPHA
        root.update()
        assert not chrome._preview.winfo_ismapped()
    finally:
        root.destroy()


def test_a_preview_target_change_mid_fade_does_not_restart_it():
    """Reversing mid-fade carries on from the alpha on screen.

    A drag that leaves an edge and comes back does that several times a
    second; restarting the fade from either end would blink.
    """
    root = make_root()
    try:
        chrome = BorderlessChrome(root, app=object())
        try:
            root.unbind("<Map>", chrome._map_bind_id)
        except Exception:
            pass
        chrome._show_snap_preview((60, 60, 300, 200))
        _preview_fade_step(chrome)
        _preview_fade_step(chrome)
        rising = chrome._preview_alpha
        assert 0.0 < rising < chrome.SNAP_PREVIEW_ALPHA

        chrome._hide_snap_preview()                 # out of the zone
        _preview_fade_step(chrome)
        assert chrome._preview_alpha < rising       # continues from here...
        assert chrome._preview_alpha > 0.0          # ...and does not jump to 0
        assert chrome._preview_shown

        # back in again: the same window climbs from where it got to
        chrome._show_snap_preview((60, 60, 300, 200))
        low = chrome._preview_alpha
        _preview_fade_step(chrome)
        assert chrome._preview_alpha > low
        assert chrome._preview_target is True
    finally:
        root.destroy()


def test_the_band_and_the_preview_switch_instantly_without_animations(
        monkeypatch):
    """Windows > Accessibility > Visual effects > Animation effects = off.

    A 120 ms ease is a courtesy, not a requirement: someone who turned
    animations off asked for exactly this. The band must reach its colour and
    the preview its alpha in the same call, with no ticker left running — a
    fade that still starts and is cut short is the bug this guards.
    """
    root = make_root()
    try:
        chrome = BorderlessChrome(root, app=object())
        try:
            root.unbind("<Map>", chrome._map_bind_id)
        except Exception:
            pass
        chrome._build_grips()
        chrome.engaged = True
        monkeypatch.setattr(type(chrome), "_os_animations",
                            staticmethod(lambda: False))

        band = chrome._grip_widgets["w"]
        assert band.cget("bg") == COLORS["shell"]
        chrome._set_hot(frozenset({"w"}))          # would normally ease
        assert chrome._fade["w"] == 1.0            # already there
        assert chrome._fade_after is None          # and no ticker running
        assert band.cget("bg") == chrome.HOVER_BG

        chrome._set_hot(frozenset({"e"}))
        assert band.cget("bg") == COLORS["shell"]  # left the moment it should
        assert chrome._fade["e"] == 1.0
        assert chrome._fade_after is None

        # the preview shows up at full strength and leaves at once
        chrome._show_snap_preview((40, 40, 300, 200))
        assert chrome._preview_shown
        assert chrome._preview_alpha == chrome.SNAP_PREVIEW_ALPHA
        assert chrome._preview_fade_after is None
        assert float(chrome._preview.attributes("-alpha")) == \
            chrome.SNAP_PREVIEW_ALPHA

        chrome._hide_snap_preview()
        assert chrome._preview_shown is False
        assert chrome._preview_alpha == 0.0
        root.update()
        assert not chrome._preview.winfo_ismapped()
    finally:
        root.destroy()


def test_the_animation_setting_comes_from_windows_and_defaults_to_on(
        monkeypatch):
    """SPI_GETCLIENTAREAANIMATION (0x1042), and animate when it can't be read."""
    from app.ui.widgets import chrome as chrome_mod

    if not chrome_mod._HAVE_WIN32:      # pragma: no cover - non-Windows
        pytest.skip("Win32 settings unavailable")
    import ctypes

    flag = ctypes.c_int(-1)
    if not ctypes.windll.user32.SystemParametersInfoW(
            0x1042, 0, ctypes.byref(flag), 0):  # pragma: no cover
        pytest.skip("SPI_GETCLIENTAREAANIMATION not supported here")
    assert chrome_mod.BorderlessChrome._os_animations() == bool(flag.value)

    # the switch being OFF has to come back as False. This machine animates,
    # so the only honest way to pin that leg is a user32 that says 0 — which
    # is also what catches the reader quietly returning True regardless.
    class _Off:
        def SystemParametersInfoW(self, action, ui, param, flags):
            assert action == 0x1042          # SPI_GETCLIENTAREAANIMATION
            ctypes.cast(param, ctypes.POINTER(ctypes.c_int))[0] = 0
            return 1

    monkeypatch.setattr(chrome_mod, "_ct",
                        SimpleNamespace(c_int=ctypes.c_int,
                                        byref=ctypes.byref,
                                        windll=SimpleNamespace(user32=_Off())))
    assert chrome_mod.BorderlessChrome._os_animations() is False

    # a user32 that refuses to answer must not silence the app's animations
    class _Boom:
        def __getattr__(self, name):
            raise RuntimeError("no user32 here")

    monkeypatch.setattr(chrome_mod, "_ct",
                        SimpleNamespace(c_int=ctypes.c_int,
                                        byref=ctypes.byref, windll=_Boom()))
    assert chrome_mod.BorderlessChrome._os_animations() is True


def test_the_in_app_preference_beats_the_windows_setting(monkeypatch):
    """config.json `window_animations`: system | on | off.

    "system" is the default because the Accessibility switch is the user's
    standing decision; "on"/"off" exist for the person who wants Windows' own
    effects but a calm window edge, or the other way round.
    """
    root = make_root()
    try:
        chrome = BorderlessChrome(root, app=object())
        try:
            root.unbind("<Map>", chrome._map_bind_id)
        except Exception:
            pass
        os_says = {"v": True}
        monkeypatch.setattr(type(chrome), "_os_animations",
                            staticmethod(lambda: os_says["v"]))

        chrome._anim_pref = "off"
        assert chrome._window_animations() is False
        chrome._anim_pref = "on"
        assert chrome._window_animations() is True
        chrome._anim_pref = "system"
        assert chrome._window_animations() is True

        # ...and the fades obey the DECISION, not the OS call
        os_says["v"] = False
        chrome._anim_pref = "on"
        chrome._build_grips()
        chrome.engaged = True
        chrome._set_hot(frozenset({"w"}))
        assert chrome._fade_after is not None     # still eases
        chrome._set_hot(frozenset())
    finally:
        root.destroy()


def test_the_preference_is_read_once_and_refreshed_on_save(monkeypatch):
    """Cached (a config load on a hover path is a disk stat plus a deep copy)
    and refreshed by `reload_animations()`, which the Settings save calls."""
    from app.ui.widgets import chrome as chrome_mod

    stored = {"v": "off"}
    monkeypatch.setattr("app.config_persist.load_config",
                        lambda: {"window_animations": stored["v"]})
    root = make_root()
    try:
        chrome = chrome_mod.BorderlessChrome(root, app=object())
        os_says = {"v": True}
        monkeypatch.setattr(type(chrome), "_os_animations",
                            staticmethod(lambda: os_says["v"]))

        assert chrome._window_animations() is False     # read on first use
        stored["v"] = "on"
        assert chrome._window_animations() is False     # ...and then cached
        chrome.reload_animations()
        os_says["v"] = False
        assert chrome._window_animations() is True      # "on" beats Windows

        # a value this version does not know falls back to following Windows
        stored["v"] = "sometimes"
        chrome.reload_animations()
        assert chrome._window_animations() is False
        # ...and so does an unreadable config
        def _boom():
            raise OSError("no config")
        monkeypatch.setattr("app.config_persist.load_config", _boom)
        chrome.reload_animations()
        os_says["v"] = True
        assert chrome._window_animations() is True
    finally:
        root.destroy()


def test_a_live_drag_cannot_push_the_window_past_the_work_area():
    root = make_root()
    try:
        root.geometry("1000x760+40+40")
        chrome = BorderlessChrome(root, app=object())
        try:
            root.unbind("<Map>", chrome._map_bind_id)
        except Exception:
            pass
        chrome._build_grips()
        chrome.engaged = True
        root.update()
        w, h = root.winfo_width(), root.winfo_height()
        if w < 200 or h < 200:      # pragma: no cover - no real display
            pytest.skip("window geometry unavailable: %sx%s" % (w, h))
        wa = chrome.work_area(40, 40, w, h)
        if not wa:                  # pragma: no cover - non-Windows
            pytest.skip("no work area on this platform")
        x0, y0 = root.winfo_x(), root.winfo_y()
        rx, ry = root.winfo_rootx(), root.winfo_rooty()

        east = chrome._grip_widgets["e"]
        east.event_generate("<Button-1>", x=2, y=h // 2, rootx=rx + w - 2,
                            rooty=ry + h // 2)
        east.event_generate("<B1-Motion>", x=2, y=h // 2, rootx=rx + w + 4000,
                            rooty=ry + h // 2)
        root.update()
        assert root.winfo_x() == x0
        assert root.winfo_y() == y0
        assert root.winfo_height() == h
        assert root.winfo_width() == wa[2] - x0        # right edge ON the limit
    finally:
        root.destroy()


def test_the_north_grip_is_lifted_above_the_title_bar(monkeypatch):
    """The bar covers the full width at y 0..TITLE_H, so a north grip below
    it would never see a click — the top edge would stay dead."""
    root = make_root()
    try:
        class _App:
            def _on_close(self):
                pass

        chrome = BorderlessChrome(root, _App())
        try:
            root.unbind("<Map>", chrome._map_bind_id)
        except Exception:
            pass
        chrome._build_titlebar()
        chrome._build_grips()

        order = []
        real = tk.Misc.lift

        def _rec(self, aboveThis=None):
            order.append(self)
            return real(self, aboveThis)

        monkeypatch.setattr(tk.Misc, "lift", _rec)
        chrome._layout_grips()

        bar = chrome._titlebar
        north = chrome._grip_widgets["n"]
        assert bar in order and north in order
        assert order.index(north) > order.index(bar)
        # and the bar still sits above the page
        east = chrome._grip_widgets["e"]
        assert order.index(east) > order.index(bar)
    finally:
        root.destroy()


def test_the_north_grip_resizes_the_top_edge():
    root = make_root()
    try:
        # above the resize clamp (WINDOW_MIN_SIZE), or the drag is swallowed
        root.geometry("1000x760+40+40")
        chrome = BorderlessChrome(root, app=object())
        try:
            root.unbind("<Map>", chrome._map_bind_id)
        except Exception:
            pass
        chrome._build_grips()
        chrome.engaged = True
        root.update()
        w, h = root.winfo_width(), root.winfo_height()
        if w < 200 or h < 200:      # pragma: no cover - no real display
            pytest.skip("window geometry unavailable: %sx%s" % (w, h))
        rx, ry = root.winfo_rootx(), root.winfo_rooty()
        y0 = root.winfo_y()

        north = chrome._grip_widgets["n"]
        north.event_generate("<Button-1>", x=10, y=2, rootx=rx + 10,
                             rooty=ry + 2)
        north.event_generate("<B1-Motion>", x=10, y=-18, rootx=rx + 10,
                             rooty=ry - 18)
        root.update()
        assert root.winfo_height() == h + 20
        assert root.winfo_y() == y0 - 20
        assert root.winfo_width() == w        # dragging the top edge is vertical
    finally:
        root.destroy()


def test_the_band_is_shell_at_rest_and_one_tone_up_on_hover():
    root = make_root()
    try:
        chrome = BorderlessChrome(root, app=object())
        try:
            root.unbind("<Map>", chrome._map_bind_id)
        except Exception:
            pass
        chrome._build_grips()
        grips = chrome._grip_widgets
        assert all(f.cget("bg") == COLORS["shell"] for f in grips.values())

        def lit():
            return {n for n, f in grips.items() if f.cget("bg") == chrome.HOVER_BG}

        chrome._set_hot(frozenset({"w"}), animate=False)
        assert lit() == {"w", "nw", "sw"}
        chrome._set_hot(frozenset({"e", "s"}), animate=False)
        assert lit() == {"e", "s", "ne", "sw", "se"}
        chrome._set_hot(frozenset(), animate=False)
        assert lit() == set()
        assert all(f.cget("bg") == COLORS["shell"] for f in grips.values())
    finally:
        root.destroy()


def test_the_reveal_eases_in_and_back_out():
    root = make_root()
    try:
        chrome = BorderlessChrome(root, app=object())
        try:
            root.unbind("<Map>", chrome._map_bind_id)
        except Exception:
            pass
        chrome._build_grips()
        chrome.engaged = True
        band = chrome._grip_widgets["s"]
        assert band.cget("bg") == COLORS["shell"]

        chrome._set_hot(frozenset({"s"}))           # animated
        assert band.cget("bg") == COLORS["shell"]   # nothing before the first tick
        chrome._fade_tick()
        mid = band.cget("bg")
        assert mid not in (COLORS["shell"], chrome.HOVER_BG), mid

        ticks = 1
        while ticks < 60 and chrome._fade["s"] != 1.0:
            chrome._fade_tick()
            ticks += 1
        assert chrome._fade["s"] == 1.0
        assert band.cget("bg") == chrome.HOVER_BG
        # 120 ms at 16 ms per step: it must take several frames, not one
        assert 5 <= ticks <= 10, ticks

        chrome._set_hot(frozenset())
        for _ in range(60):
            if chrome._fade["s"] == 0.0:
                break
            chrome._fade_tick()
        assert chrome._fade["s"] == 0.0
        assert band.cget("bg") == COLORS["shell"]
    finally:
        root.destroy()


def test_a_target_change_mid_fade_does_not_restart_it():
    root = make_root()
    try:
        chrome = BorderlessChrome(root, app=object())
        try:
            root.unbind("<Map>", chrome._map_bind_id)
        except Exception:
            pass
        chrome._build_grips()
        chrome.engaged = True

        chrome._set_hot(frozenset({"w"}))
        chrome._fade_tick()
        chrome._fade_tick()
        rising = chrome._fade["w"]
        assert 0.0 < rising < 1.0

        chrome._set_hot(frozenset({"e"}))           # pointer jumped to the right
        chrome._fade_tick()
        assert chrome._fade["w"] < rising             # keeps fading from where it was
        assert 0.0 < chrome._fade["e"] < 1.0
    finally:
        root.destroy()


def test_pointer_motion_reveals_only_the_nearby_band():
    root = make_root()
    try:
        root.geometry("900x640+40+40")
        chrome = BorderlessChrome(root, app=object())
        try:
            root.unbind("<Map>", chrome._map_bind_id)
        except Exception:
            pass
        chrome._build_grips()
        chrome.engaged = True
        root.update()
        w, h = root.winfo_width(), root.winfo_height()
        if w < 200 or h < 200:      # pragma: no cover - no real display
            pytest.skip("window geometry unavailable: %sx%s" % (w, h))
        rx, ry = root.winfo_rootx(), root.winfo_rooty()

        chrome._on_pointer_move(SimpleNamespace(x_root=rx + 2, y_root=ry + h // 2))
        assert chrome._hot == frozenset({"w"})
        chrome._on_pointer_move(SimpleNamespace(x_root=rx + w // 2,
                                                y_root=ry + h // 2))
        assert chrome._hot == frozenset()
        chrome._on_pointer_move(SimpleNamespace(x_root=rx + w - 2,
                                                y_root=ry + h - 2))
        assert chrome._hot == frozenset({"e", "s"})
        # disengaged (native frame): motion must not touch anything
        chrome.engaged = False
        chrome._on_pointer_move(SimpleNamespace(x_root=rx + 2, y_root=ry + h // 2))
        assert chrome._hot == frozenset({"e", "s"})
    finally:
        root.destroy()


def test_place_content_hands_the_app_exactly_the_content_box():
    root = make_root()
    try:
        root.geometry("820x600+30+30")
        host = tk.Frame(root, bg="black")
        BorderlessChrome.place_content(host)
        root.update()
        w, h = root.winfo_width(), root.winfo_height()
        if w < 200 or h < 200:      # pragma: no cover - no real display
            pytest.skip("window geometry unavailable: %sx%s" % (w, h))
        got = (host.winfo_x(), host.winfo_y(),
               host.winfo_width(), host.winfo_height())
        assert got == BorderlessChrome.content_box(w, h)
    finally:
        root.destroy()
