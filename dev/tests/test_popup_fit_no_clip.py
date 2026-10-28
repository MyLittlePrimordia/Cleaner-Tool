"""Regression tests for the 2026-10 "no scrolling or clipping in the popups"
sweep.

The user's request, restated as the things these tests pin down:

  * "none of the popups in the Tools tab have any scrolling except for ones
    with intended scrollbars" - a popup may only scroll where the content
    genuinely does not fit;
  * "or any clipping of any sort" - nothing may be cut off at the panel edge.

The audit that produced this file built all 25 Tools popups off-screen at both
the normal size (a maximised 1707x960 window) and the smallest size the app
allows (WINDOW_MIN_SIZE = 980x700) and measured every widget's box against the
box its parent gave it. Three findings, three fixes:

  * the fixed-size dashboards (Hardware Monitor, Pilot, Gamepad) clipped the
    bottom of their content whenever the content needed more height than the
    content zone had - the packer just cut it off;
  * so did five testers once the window was at its minimum (Specs, Keyboard,
    Mouse, Monitor, Speed Test, Drive Toolkit);
  * long StatusLine captions were sheared MID-CHARACTER at the footer column
    edge, and the Uninstall toolbar's "Select all" lost half its label to the
    search pill on a narrow window.

The fix is the ToolPage `fit=True` content mode: an auto-fit scroll panel that
fills the viewport when the content is short and scrolls when it is tall. These
tests are side-effect-free - no DataTable registry scan, no WMI, no audio, no
subprocess; only the shared widgets and a bare ToolPage probe are built. Tk is
skipped gracefully when there is no display.
"""
from __future__ import annotations

import inspect

import pytest

tk = pytest.importorskip("tkinter")

from app.ui.theme import COLORS  # noqa: E402

#: The popups whose content is a fixed-size dashboard/tester that cannot be
#: relied on to fit every window size the app allows. Each must use the
#: auto-fit content zone. If one is added, add it here too.
AUTO_FIT_DIALOGS = (
    "HardwareMonitorDialog", "PilotDialog", "SpecsDialog",
    "SpeedTestDialog", "DriveToolkitDialog",
)

#: Popups whose content owns its own scrolling (a DataTable, or an inner
#: bounded ScrollableRoundedPanel). Routing them through a second scroll panel
#: would give the popup two scrollbars - the exact nesting the 2026-10 Tools
#: redesign removed - so they must stay on the plain content frame.
SELF_SCROLLING_DIALOGS = (
    "ServicesDialog", "NetworkConnectionsDialog",
    "DuplicatesDialog", "RestorePointDialog", "UninstallProgramsDialog",
    "FileLockDialog", "StorageInsightDialog", "ProcessManagerDialog",
    "StartupManagerDialog",
)


def _tk_root():
    try:
        root = tk.Tk()
    except tk.TclError as exc:  # pragma: no cover - headless CI
        pytest.skip("no display: %s" % exc)
    root.withdraw()
    return root


def _mapped_root(w=520, h=240):
    """A real (but off-screen) root: the scroll maths needs true geometry."""
    try:
        root = tk.Tk()
    except tk.TclError as exc:  # pragma: no cover - headless CI
        pytest.skip("no display: %s" % exc)
    root.geometry("%dx%d+3000+3000" % (w, h))
    return root


def _src(cls_name):
    import app.ui.dialogs as D

    cls = getattr(D, cls_name)
    if cls.__module__.startswith("app.ui.dialogs"):
        mod = inspect.getmodule(cls)
    else:  # pragma: no cover - re-exported from elsewhere
        mod = inspect.getmodule(cls)
    return inspect.getsource(mod)


# ----------------------------------------------------------- the ToolPage mode
@pytest.mark.parametrize("name", AUTO_FIT_DIALOGS)
def test_clipping_prone_dialogs_use_the_auto_fit_zone(name):
    assert "fit=True" in _src(name), (
        "%s renders fixed-size content, so its content zone must auto-fit "
        "(fit=True): otherwise the packer cuts the overflow off at the panel "
        "edge on any window smaller than the content" % name)


@pytest.mark.parametrize("name", SELF_SCROLLING_DIALOGS)
def test_self_scrolling_dialogs_do_not_nest_a_second_panel(name):
    assert "fit=True" not in _src(name), (
        "%s already owns its scrolling (a DataTable or a bounded inner "
        "panel); an outer scroll panel would give the popup two scrollbars"
        % name)


def test_no_dialog_asks_for_both_scroll_modes():
    import app.ui.dialogs as D

    from app.ui.dialogs._tool_page import ToolPage

    names = [n for n in dir(D) if n.endswith("Dialog")]
    for n in names:
        cls = getattr(D, n)
        if not (isinstance(cls, type) and issubclass(cls, ToolPage)):
            continue
        src = _src(n)
        assert not ("scroll=True" in src and "fit=True" in src), (
            "%s sets both scroll=True and fit=True; scroll wins and the "
            "auto-fit guarantee is silently lost" % n)


def test_toolpage_fit_routes_content_through_a_fill_inner_panel():
    from app.ui.dialogs._tool_page import ToolPage
    from app.ui.widgets import ScrollableRoundedPanel

    root = _tk_root()
    try:

        class _Probe(ToolPage):
            def __init__(self, parent):
                super().__init__(parent, title="Probe", scope="x", fit=True)
                tk.Label(self.content, text="body").pack()

        dlg = _Probe(root)
        try:
            panel = dlg._content_panel
            assert isinstance(panel, ScrollableRoundedPanel), (
                "fit=True must build the shared scroll panel")
            assert panel._fill_inner is True, (
                "the panel must stretch its inner frame to the viewport, or "
                "centred layouts and expand children collapse to their "
                "natural height")
            assert dlg.content.master is panel.inner
        finally:
            dlg.close()
    finally:
        root.destroy()


def test_plain_content_frame_is_unchanged_when_fit_is_off():
    from app.ui.dialogs._tool_page import ToolPage

    root = _tk_root()
    try:

        class _Probe(ToolPage):
            def __init__(self, parent):
                super().__init__(parent, title="Probe", scope="x")

        dlg = _Probe(root)
        try:
            assert dlg._content_panel is None
        finally:
            dlg.close()
    finally:
        root.destroy()


# ------------------------------------------------- the actual clipping maths
def test_fill_inner_panel_scrolls_instead_of_clipping_tall_content():
    """The core fix: never clamp the embedded frame below the content height.

    Before the fix a `fill_inner` panel forced the inner frame to the bare
    viewport height, so anything taller was cut off at the panel edge and the
    scrollbar had nothing to scroll to.
    """
    from app.ui.widgets.scroll import ScrollableRoundedPanel

    root = _mapped_root(420, 200)
    try:
        p = ScrollableRoundedPanel(root, fill_inner=True)
        p.pack(fill="both", expand=True)
        for _ in range(20):
            tk.Frame(p.inner, bg=COLORS["bg"], height=40, width=180).pack(
                fill="x")
        root.update()
        p.refresh_scroll()

        want = p.inner.winfo_reqheight()
        got = int(p.itemcget(p._win, "height"))
        assert got >= want, (
            "embedded frame clamped to %dpx for %dpx of content: the overflow "
            "is clipped and the scrollbar cannot reach it" % (got, want))
        assert want > p.view_height(), "test needs content taller than the view"
        assert p._max_offset() > 0, "tall content must actually be scrollable"
        assert p.scroll_enabled is True
    finally:
        root.destroy()


def test_fill_inner_panel_still_stretches_short_content_to_the_viewport():
    from app.ui.widgets.scroll import ScrollableRoundedPanel

    root = _mapped_root(420, 260)
    try:
        p = ScrollableRoundedPanel(root, fill_inner=True)
        p.pack(fill="both", expand=True)
        tk.Frame(p.inner, bg=COLORS["bg"], height=30, width=180).pack(fill="x")
        root.update()
        p.refresh_scroll()

        assert int(p.itemcget(p._win, "height")) == p.view_height(), (
            "short content must still be stretched to the viewport, so "
            "`expand=True` children keep filling the page")
        assert p.scroll_enabled is False, (
            "a popup whose content fits must not offer a scrollbar")
        assert p._max_offset() == 0
    finally:
        root.destroy()


# ---------------------------------------------------------- the status caption
def test_statusline_ellipsizes_a_long_caption_and_can_restore_it():
    """A sheared caption reads as a rendering fault; an ellipsis reads as
    truncation. The full string is kept so a wider column shows it again."""
    from app.ui.dialogs._tool_page import StatusLine

    root = _mapped_root(1240, 160)
    long = ("Scanned 47 folders, compared 12,904 files and reclaimed 12.4 GB "
            "of duplicated data")
    try:
        holder = tk.Frame(root, bg=COLORS["bg"], width=220, height=60)
        holder.pack_propagate(False)
        holder.pack()
        sl = StatusLine(holder)
        sl.pack(fill="x")
        root.update()

        sl.set_text(long)
        root.update()
        shown = sl.caption.cget("text")
        assert shown.endswith("\u2026"), (
            "a caption wider than its column must end on an ellipsis, never "
            "be sheared mid-character (got %r)" % shown)
        assert len(shown) < len(long)
        assert sl._raw == long, "the full text must be kept for re-fitting"

        holder.config(width=1200)
        root.update()
        assert sl.caption.cget("text") == long, (
            "widening the column must show the whole caption again")
    finally:
        root.destroy()


def test_statusline_short_caption_is_left_alone():
    from app.ui.dialogs._tool_page import StatusLine

    root = _mapped_root(640, 160)
    try:
        holder = tk.Frame(root, bg=COLORS["bg"], width=600, height=60)
        holder.pack_propagate(False)
        holder.pack()
        sl = StatusLine(holder)
        sl.pack(fill="x")
        root.update()
        sl.stop("Ready.", COLORS["accent_green"])
        root.update()
        assert sl.caption.cget("text") == "Ready."
        assert sl._raw == "Ready."
    finally:
        root.destroy()


# ------------------------------------------------------------- Uninstall toolbar
def test_uninstall_toolbar_gives_the_checkbox_its_width_first():
    """pack() hands the toolbar's width out in PACK ORDER. The search pill
    asks for 50 characters, so packing it first left "Select all" 44px and cut
    the label mid-character on the smallest window the app allows."""
    from app.ui.dialogs import uninstall_programs_dialog as mod

    src = inspect.getsource(mod)
    cb = src.index("self._select_all_var = tk.BooleanVar")
    pill = src.index("entry = _PillEntry(")
    assert cb < pill, (
        "the fixed-width Select-all checkbox must be created - and therefore "
        "packed - before the elastic search pill")
    assert (src.index(').pack(side="right"', cb)
            < src.index('entry.pack(side="left"', pill)), (
        "Select all must be packed before the search pill, or the pill eats "
        "the row on a narrow window")
