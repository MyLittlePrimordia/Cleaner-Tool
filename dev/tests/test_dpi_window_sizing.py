"""DPI sizing for the shell and the dialogs.

The 2026-10 visual pass rendered every tab at 100% and 200%. At 200% the app
still opened a 1280x900 *physical* window around 200%-sized text: Clean showed
two of its three preset cards, the third option row ran off the right edge, and
the action bar sat below the screen. Dialogs were capped at 1120x880 physical,
so Process Manager showed six of thirty-three rows and Health Report cut its
last row against the footer.

Both were the same mistake: a 96-DPI number compared with a physical work area.
These tests pin the conversion and the scaled caps without needing a display.
"""
from __future__ import annotations

from app.config import (
    WINDOW_MIN_SIZE, dpi_window_size, resolve_window_minimum,
)


class _Root:
    def __init__(self, dpi):
        self._dpi = float(dpi)

    def winfo_fpixels(self, _what):
        return self._dpi


class _Parent(_Root):
    def __init__(self, width, height, dpi):
        super().__init__(dpi)
        self._w = width
        self._h = height

    def winfo_width(self):
        return self._w

    def winfo_height(self):
        return self._h


def test_device_scale_reads_the_display_and_clamps():
    from app.config import device_scale

    assert device_scale(_Root(96)) == 1.0
    assert device_scale(_Root(144)) == 1.5
    assert device_scale(_Root(192)) == 2.0
    # A nonsense answer must not ask for a 0px or a 4000px window.
    assert device_scale(_Root(0)) == 1.0
    assert device_scale(_Root(9600)) == 3.0

    class _Broken:
        def winfo_fpixels(self, _what):
            raise RuntimeError("no display")

    assert device_scale(_Broken()) == 1.0


def test_the_96_dpi_contract_stops_at_the_designed_footprint():
    # No density floor applies here (a headless/96-DPI caller): the window is
    # the logical contract itself, clipped to the widest layout the pages are
    # designed for rather than to the old 1280x900.
    assert dpi_window_size(2560, 1368, 1.0) == (1600, 1000)


def test_the_default_density_raises_100_percent_but_never_overrides_125():
    """1.333 -> 1.667 on a 100% display; 1.667+ is already denser."""
    from dev.tests._tkhelpers import make_root
    from app.dpi import MIN_TK_SCALING, SCALING_TOLERANCE, apply_ui_density

    root = make_root()
    original = float(root.tk.call("tk", "scaling"))
    try:
        root.tk.call("tk", "scaling", 96.0 / 72.0)
        assert apply_ui_density(root) is True
        # Tk quantizes scaling to the display's pixel grid, so 1.667 comes back
        # 1.6647 (Tk 8.6) or 1.669 (Tk 9) rather than exactly 1.6667; the
        # tolerance is the same one the app uses to call a scale "applied".
        floor = MIN_TK_SCALING - SCALING_TOLERANCE
        assert float(root.tk.call("tk", "scaling")) >= floor
        assert float(root.tk.call("tk", "scaling")) < MIN_TK_SCALING + SCALING_TOLERANCE
        # Second call is a no-op, not a second bump.
        assert apply_ui_density(root) is False
        assert floor <= float(root.tk.call("tk", "scaling")) \
            < MIN_TK_SCALING + SCALING_TOLERANCE
        # The raised floor is the 125% device scale that grows the window.
        assert 1.24 <= root.winfo_fpixels("1i") / 96.0 <= 1.26

        root.tk.call("tk", "scaling", 2.0)
        high = float(root.tk.call("tk", "scaling"))
        assert apply_ui_density(root) is False
        assert float(root.tk.call("tk", "scaling")) == high
    finally:
        root.tk.call("tk", "scaling", original)
        root.destroy()


def test_the_default_density_gives_a_2000_window_at_100_percent():
    # Tk's 96-DPI scale is raised to 120 DPI (1.667) by app.dpi. That is a
    # device scale of 1.25, so the 1440p work area resolves a 1600x941 logical
    # window, which becomes 2000x1175 physical (the odd pixel is int()
    # truncation, see dpi_window_size): 78% of the width and 86% of the
    # height, with type 25% larger than Tk's raw 96-DPI default.
    from app.config import device_scale

    assert device_scale(_Root(120)) == 1.25
    assert dpi_window_size(2560, 1368, 120 / 96) == (2000, 1175)


def test_a_1080p_window_at_the_default_density_is_roomier_without_being_full():
    # 1920x1032 work area at 125%: 1536x826 logical, so 1259x710 logical ->
    # 1573x886 physical. That is 82% of the width and 86% of the height:
    # larger than the old 1440x846 default, still a window rather than a
    # maximized app.
    assert dpi_window_size(1920, 1032, 1.25) == (1573, 886)


def test_the_default_window_leaves_a_desktop_margin():
    # A 1366x768 laptop at 125% resolves the 980x700 logical minimum, which
    # is 1225x875 physical -- taller than the 728px work area. The shell used
    # to open flush against the taskbar there; the margin keeps a strip of
    # desktop (and the bottom resize grip) visible.
    assert dpi_window_size(1366, 728, 1.25) == (1225, 680)
    from app.config import WINDOW_WORKAREA_MARGIN
    assert 728 - WINDOW_WORKAREA_MARGIN == 680


def test_a_200_percent_window_is_physical_pixels_not_96_dpi_pixels():
    # 2560x1440 at 200% is 1280x684 logical; the 980x700 floor is taller than
    # the logical work area, so the physical result is capped at the work
    # area less the desktop margin instead of hanging 32px off the screen.
    assert dpi_window_size(2560, 1368, 2.0) == (2098, 1320)


def test_a_200_percent_4k_window_keeps_the_designed_logical_footprint():
    # 3840x2088 work area at 200%: 1574x897 logical becomes 3148x1794
    # physical, still inside the work area.
    assert dpi_window_size(3840, 2088, 2.0) == (3148, 1794)


def test_the_minimum_scales_and_never_exceeds_the_resolved_window():
    assert resolve_window_minimum(_Root(96), "1600x1000") == WINDOW_MIN_SIZE
    # The scaled 700px floor (1400px) is taller than this 1368px work area;
    # the window that resolved is the cap, so it stays resizable.
    assert resolve_window_minimum(_Root(192), "1996x1320") == (1960, 1320)


def test_a_dialog_keeps_its_logical_footprint_at_200_percent():
    from app.ui.base import ThemedModal

    at100 = ThemedModal._proportional_size(_Parent(1280, 900, 96))
    at200 = ThemedModal._proportional_size(_Parent(1996, 1368, 192))
    assert at100 == (998, 738)
    # Before the fix both stopped at the 1120x880 physical cap.
    assert at200 == (1556, 1121)
    assert at200[0] > at100[0] and at200[1] > at100[1]


def test_an_unmapped_parent_still_gets_a_scaled_fallback():
    from app.ui.base import ThemedModal

    assert ThemedModal._proportional_size(_Parent(1, 1, 96)) == (
        ThemedModal.WIDTH, ThemedModal.HEIGHT)
    assert ThemedModal._proportional_size(_Parent(1, 1, 192)) == (
        ThemedModal.WIDTH * 2, ThemedModal.HEIGHT * 2)
