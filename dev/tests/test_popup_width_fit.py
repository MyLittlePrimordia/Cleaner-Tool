"""Regression tests for the 2026-10 width-fit pass (the third Tools sweep).

The user's report was about the keyboard tester and Auto-Pilot clipping, but
the same failure shape turned up in a dialog nobody had complained about yet:
at the *minimum* window (980x700, and at 150% DPI where popups shrink to
764x574) a Storage Insight row packed a long title before the size pill on
its right, so pack handed the pill 4px of the ~46px it needed and Tk sheared
"12.4 GB" down to nothing. A Tk Label is CENTRE-anchored: given less width
than its text it cuts glyphs off BOTH ends instead of truncating them.

These tests pin down the shared fix (`_tool_page.fit_label`) and the two
dialogs that use it:

  * the fit really does ellipsize to the container, and really does give the
    full text back when the window grows again;
  * the reserve a row asks for is honoured — the row's own widgets keep their
    pixels, the flexible text is what shortens;
  * the dialogs keep using the shared helper, and the game row's reserve is
    computed from FONT METRICS rather than read back from widget requests
    (a widget request is only recomputed on a layout pass, and a reserve
    taken from a half-updated one is how the pill got clipped in the first
    place — measured: tkfont says 89px for "114.98 GB", the Label renders 93).

All side-effect-free: no engine, registry, subprocess or config code runs, and
no feature dialog is built. Tk is skipped gracefully when there is no display.
"""
from __future__ import annotations

import pytest

tk = pytest.importorskip("tkinter")

from app.ui.dialogs._tool_page import fit_label  # noqa: E402

ELL = "\u2026"
LONG = "A Really Quite Long Game Name Here"


def _mapped_root(w=320, h=120):
    """A real (but off-screen) root, because these widgets need geometry."""
    try:
        root = tk.Tk()
    except tk.TclError as exc:  # pragma: no cover - headless CI
        pytest.skip("no display: %s" % exc)
    root.geometry("%dx%d+3000+3000" % (w, h))
    return root


def _row(root):
    """A container with one long-text Label in it, already laid out."""
    outer = tk.Frame(root, bg="#101010")
    outer.pack(fill="x")
    lbl = tk.Label(outer, text=LONG, bg="#101010", fg="#EEEEEE",
                   font=("Segoe UI", 10))
    lbl.pack(side="left")
    root.update()
    return outer, lbl


# ------------------------------------------------------------------ the fit
def test_fit_label_ellipsizes_to_the_container_width():
    root = _mapped_root(160, 120)
    try:
        outer, lbl = _row(root)
        assert outer.winfo_width() > 100, "container must have real geometry"

        fit_label(outer, lbl, LONG)
        root.update()

        shown = str(lbl.cget("text"))
        assert shown != LONG, "a 160px container cannot show a 178px name"
        assert shown.endswith(ELL), "the cut must be marked, not a shear: %r" % shown
        assert lbl.winfo_reqwidth() <= outer.winfo_width(), (
            "the fitted text must fit the container it was fitted to")

        # ...and the full text comes back when there is room again
        root.geometry("900x120+3000+3000")
        root.update()
        root.update()
        assert str(lbl.cget("text")) == LONG, (
            "the fit must re-expand when the window grows")
    finally:
        root.destroy()


def test_fit_label_keeps_the_reserve_for_a_rows_other_widgets():
    """A row's chips (switch, pill, buttons) keep their pixels: the reserve
    is subtracted before the text is cut, so the chip beside the text is
    never the thing that shrinks."""
    root = _mapped_root(300, 120)
    try:
        outer, lbl = _row(root)
        reserve = 160

        fit_label(outer, lbl, LONG, reserve=reserve)
        root.update()

        room = outer.winfo_width() - reserve
        assert lbl.winfo_width() <= room + 4, (
            "the label must stay inside the room left by the reserve "
            "(label=%d room=%d)" % (lbl.winfo_width(), room))
        assert str(lbl.cget("text")).endswith(ELL)
    finally:
        root.destroy()


def test_fit_label_tolerates_a_callable_reserve():
    """The reserve is allowed to be a callable (Storage Insight's pill changes
    its text after the scan, so its width cannot be a constant)."""
    root = _mapped_root(300, 120)
    try:
        outer, lbl = _row(root)
        calls = []

        def reserve():
            calls.append(1)
            return 120

        fit_label(outer, lbl, LONG, reserve=reserve)
        root.update()
        assert calls, "a callable reserve must be evaluated"
        assert lbl.winfo_width() <= outer.winfo_width() - 120 + 4
    finally:
        root.destroy()


def test_fit_label_returns_the_text_untouched_when_it_fits():
    root = _mapped_root(700, 120)
    try:
        outer, lbl = _row(root)
        fit_label(outer, lbl, "Hades")
        root.update()
        assert str(lbl.cget("text")) == "Hades"
    finally:
        root.destroy()


# --------------------------------------------------- the dialogs that use it
def _src(tmp_path, name):
    from pathlib import Path
    p = Path(__file__).resolve().parents[2] / "app" / "ui" / "dialogs" / name
    return p.read_text(encoding="utf-8")


def test_pilot_delegates_to_the_shared_fit():
    src = _src(None, "pilot_dialog.py")
    assert "from app.ui.dialogs._tool_page import ToolPage, fit_label" in src
    assert "return fit_label(container, label, full, reserve, min_room)" in src, (
        "Auto-Pilot's _fit_label must delegate, not carry a second copy of "
        "the fitting logic")


def test_storage_insight_fits_its_rows_with_the_shared_helper():
    src = _src(None, "storage_insight_dialog.py")
    assert "fit_label" in src, "Storage Insight must use the shared width fit"

    # the junk rows: the title is fitted, and the pill re-runs the fit when
    # the scan gives it a real number (a config() that keeps a widget's
    # geometry identical sends no <Configure>, so waiting for one is not
    # enough — this is what left the pill 4px wide)
    assert "refit = fit_label(inner, lbl, title, reserve=_pill_room)" in src
    assert "self._cat_refits[title] = refit" in src
    assert "lbl.after_idle(refit)" in src

    # the pill's room is text-derived, not read back from the widget
    assert "_fm.measure(str(_lbl.cget(\"text\")))" in src

    # the game rows: every chip's width is font-derived, and the name is the
    # one flexible thing in the row
    assert "_w_drive = _fm9.measure(" in src
    assert "_w_open = _fm9.measure(\"Open\")" in src
    assert "_w_size = _fm10.measure(format_bytes(" in src
    assert "_fit_name = fit_label(inner, lbl, _gname, reserve=_fixed_room)" in src
    assert "wraplength=175" not in src, (
        "the game name must be ellipsized to the room it has, not wrapped at "
        "a fixed 175px that ignores the row's chips")


def test_tool_page_exports_the_fit_helper():
    import app.ui.dialogs._tool_page as tp
    assert callable(tp.fit_label)
    doc = (tp.fit_label.__doc__ or "")
    assert "ellipsis" in doc or "Ellipsize" in doc
