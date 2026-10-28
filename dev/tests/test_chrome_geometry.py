"""The rounded-corner masks are lifted above the title bar.

They paint an opaque fillet inside each RADIUS x RADIUS corner square. Any
window control that reaches into that square is partly covered (the "square
over the exit button" artifact) AND partly unclickable, because the mask
canvas sits above the label. This pins the invariant instead of a screenshot.
"""
from __future__ import annotations

import pytest

from dev.tests._tkhelpers import make_root

tk = pytest.importorskip("tkinter")


class _StubApp:
    def _on_close(self):
        pass


def test_window_controls_do_not_intersect_corner_arcs():
    from app.ui.widgets.chrome import BorderlessChrome

    root = make_root()
    try:
        root.geometry("900x600")
        chrome = BorderlessChrome(root, _StubApp())
        chrome._build_titlebar()
        # Pack/place coordinates only exist once the window is mapped; a
        # withdrawn root reports off-screen garbage for every child.
        root.deiconify()
        root.update()
        if not all(b.winfo_ismapped() for b in chrome._btns.values()):
            pytest.skip("window manager did not map the test window")

        w = root.winfo_width()
        r = chrome.RADIUS

        for name, btn in chrome._btns.items():
            x0 = btn.winfo_rootx() - root.winfo_rootx()
            y0 = btn.winfo_rooty() - root.winfo_rooty()
            x1, y1 = x0 + btn.winfo_width(), y0 + btn.winfo_height()

            # The two top corner squares the masks own.
            overlaps = []
            if x1 > w - r and y0 < r:
                overlaps.append("NE")
            if x0 < r and y0 < r:
                overlaps.append("NW")
            assert not overlaps, (
                "%s button at (%d,%d)-(%d,%d) intersects the %s corner mask "
                "(window width %d, radius %d)"
                % (name, x0, y0, x1, y1, ",".join(overlaps), w, r)
            )
    finally:
        root.destroy()
