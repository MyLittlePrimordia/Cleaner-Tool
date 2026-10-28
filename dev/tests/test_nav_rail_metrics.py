"""The navigation rail's metrics follow the display, not a pixel literal.

At 150% the 24px rail icon renders at 36px. The rail used to keep a fixed
56x32 pill and place the label by its CENTRE 12px under the pill's edge, so
the mark overflowed its own indicator and only ~3px of clear space was left
above its label: the icon and the text read as one crowded block (user
report). These tests pin the two rules that fix it — the pill is built around
the CURRENT icon, and the row is tall enough for the pill, the label gap and
a line of label text.
"""
from __future__ import annotations

import pytest

from dev.tests._tkhelpers import make_root

tk = pytest.importorskip("tkinter")


def _rail_only_app(root):
    """An Application instance with a root but no shell built.

    `_rail_metrics` only reads `self.root` and the class-level design values,
    so constructing the whole window would be wasted work (and would start the
    health/update workers).
    """
    from app.ui.app import Application

    app = Application.__new__(Application)
    app.root = root
    return app


@pytest.mark.parametrize("dpi", [96, 120, 144, 192])
def test_the_pill_is_built_around_the_scaled_icon(dpi):
    from app.ui.theme import ICON_CONTROL, SPACE_1, SPACE_2, SPACE_4
    from app.ui.theme import icon_target_px

    root = make_root()
    # Tk's scaling is per DISPLAY, not per interpreter: leaving it raised
    # would change every Tk root a later test creates in this process.
    original = float(root.tk.call("tk", "scaling"))
    try:
        root.tk.call("tk", "scaling", dpi / 72.0)
        _rail_w, item_h, ind_w, ind_h, _top, gap = \
            _rail_only_app(root)._rail_metrics()
        icon = icon_target_px(ICON_CONTROL, root)
        assert (ind_w, ind_h) == (icon + 2 * SPACE_4, icon + 2 * SPACE_1)
        assert gap == SPACE_2
        # The row has to hold the pill, the gap and the label's line box, and
        # still leave breathing room under it. A row equal to the pill alone
        # is the fixed-72 mistake this test exists to stop.
        assert item_h > ind_h + gap
    finally:
        root.tk.call("tk", "scaling", original)
        root.destroy()


def test_the_rail_grows_with_the_display_instead_of_only_its_icons():
    root = make_root()
    original = float(root.tk.call("tk", "scaling"))
    try:
        root.tk.call("tk", "scaling", 96 / 72.0)
        small = _rail_only_app(root)._rail_metrics()
        root.tk.call("tk", "scaling", 192 / 72.0)
        large = _rail_only_app(root)._rail_metrics()
        # rail_w, item_h, ind_w, ind_h all grow; the gap stays a design token.
        assert large[0] > small[0]
        assert large[1] > small[1]
        assert large[2] > small[2] and large[3] > small[3]
        assert large[5] == small[5]
    finally:
        root.tk.call("tk", "scaling", original)
        root.destroy()
