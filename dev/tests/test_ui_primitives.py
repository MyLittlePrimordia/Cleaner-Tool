"""Stage 2 of the 2026-10 makeover: buttons, list rows and empty states.

The audit counted 78 button call sites each deciding its own font, padding and
container (design audit C-001), eight row heights for the same object
(C-003), and six hand-written versions of "nothing here" (D3). These tests pin
the three primitives that replaced them, so a new call site has one shape to
copy and a future edit cannot quietly reintroduce a fourth.

Headless-safe: the widget tests skip without a display, the source sweeps do
not need one.
"""
from __future__ import annotations

import inspect
import pathlib
import re

import pytest

from app.ui import theme
from app.ui.widgets.button import ROLE_SPECS, SIZE_SPECS, AnimatedButton

tk = pytest.importorskip("tkinter")

from dev.tests._tkhelpers import make_root            # noqa: E402


# --------------------------------------------------------------------------
# Buttons
# --------------------------------------------------------------------------
def _button(root, **kw):
    from app.ui.widgets.button import AnimatedButton as B
    return B(root, text="Go", **kw)


def test_secondary_is_one_face_across_the_app():
    # The neutral dialog action used to be "surface" in some dialogs and
    # "outlined primary" in the Tools popups. One role now.
    assert ROLE_SPECS["secondary"] == (theme.COLORS["surface"],
                                       theme.COLORS["text"], None)


def test_filled_takes_the_callers_accent_and_black_label():
    root = make_root()
    try:
        b = _button(root, role="filled", accent=theme.COLORS["accent_pink"])
        assert b._bg == theme.COLORS["accent_pink"]
        assert b._fg == theme.COLORS["black"]
        assert b._variant is None
    finally:
        root.destroy()


def test_outlined_labels_primary_even_without_a_colour_pair():
    # An outlined role resolves its label, not its container: an earlier
    # version fell through to the tonal content colour.
    root = make_root()
    try:
        b = _button(root, role="outlined")
        assert b._variant == "outlined"
        assert b._fg == theme.COLORS["primary"]
    finally:
        root.destroy()


def test_danger_and_warn_keep_their_own_colour_over_an_accent():
    root = make_root()
    try:
        red = _button(root, role="danger", accent=theme.COLORS["accent_green"])
        amber = _button(root, role="warn", accent=theme.COLORS["accent_green"])
        assert red._fg == theme.COLORS["accent_red"]
        assert amber._fg == theme.COLORS["accent_yellow"]
    finally:
        root.destroy()


def test_size_ladder_supplies_font_and_padding_together():
    root = make_root()
    try:
        for name, (padx, pady, font) in SIZE_SPECS.items():
            b = _button(root, role="secondary", size=name)
            assert (b._padx, b._pady, b._font) == (padx, pady, font), name
        # The ladder is ordered, and only `lg` is the page hero.
        assert (SIZE_SPECS["sm"][0] < SIZE_SPECS["md"][0]
                < SIZE_SPECS["lg"][0])
        assert (SIZE_SPECS["sm"][1] < SIZE_SPECS["md"][1]
                < SIZE_SPECS["lg"][1])
    finally:
        root.destroy()


def test_tool_page_footer_uses_the_shared_role_and_size_ladder():
    from app.ui.dialogs import _tool_page
    assert _tool_page.KIND_ROLE == {"primary": "filled",
                                   "secondary": "secondary",
                                   "danger": "danger",
                                   "warn": "warn"}
    # A popup's footer is the same height as every other dialog's footer: it
    # reads the shared button ladder instead of a private size, and the two
    # private padding dicts are gone. (The toolbar half of this check went
    # with the unused toolbar_button helper in the 2026-10 sweep — the live
    # dialogs fill their toolbar with their own controls.)
    assert _tool_page.FOOT_SIZE in SIZE_SPECS
    assert not hasattr(_tool_page, "BTN")


def test_button_source_loads_its_font_from_the_type_scale():
    assert 'TYPE["label_large"]' in inspect.getsource(
        AnimatedButton.__init__)


#: Keypad cells keep an explicit pixel box and a larger type. The Speaker
#: Tester was the only call site allowed to do it; it was removed in 2026-10
#: with the other unreachable peripherals, so the allowlist is EMPTY and the
#: ladder now applies to every AnimatedButton in the app. Anything that needs
#: back in here has to say why, one file at a time - never "any font is fine".
_PIXEL_BOX_ALLOWED: frozenset = frozenset()


def _button_calls():
    """Every AnimatedButton call in the app, except the class's own file."""
    pat = re.compile(r"AnimatedButton\(([^()]*(?:\([^()]*\)[^()]*)*)\)", re.S)
    for path in sorted(pathlib.Path("app").rglob("*.py")):
        if path.name == "button.py":
            continue                      # the primitive + its docstring
        src = path.read_text(encoding="utf-8")
        for m in pat.finditer(src):
            line = src[:m.start()].count("\n") + 1
            yield path.name, line, " ".join(m.group(1).split())


def test_every_button_call_site_uses_the_shared_ladder():
    offenders = []
    for name, line, body in _button_calls():
        if "width=" in body or "height=" in body:
            continue
        if name in _PIXEL_BOX_ALLOWED:
            continue
        if "role=" not in body and "size=" not in body:
            offenders.append("%s:%d %s" % (name, line, body[:90]))
    assert not offenders, (
        "buttons must pass role= and/or size=, not a font/padding pair:\n  "
        + "\n  ".join(offenders))


def test_no_button_call_site_declares_its_own_font():
    offenders = []
    for name, line, body in _button_calls():
        if "width=" in body or "height=" in body:
            continue                      # a pixel-box keypad cell
        if name in _PIXEL_BOX_ALLOWED:
            continue
        if "font=" in body:
            offenders.append("%s:%d %s" % (name, line, body[:90]))
    assert not offenders, (
        "a button may not choose its own type:\n  " + "\n  ".join(offenders))


# --------------------------------------------------------------------------
def test_hex_lerp_never_raises_on_a_malformed_colour():
    """A missing accent must not take a page down while it paints.

    The 2026-10 user log had `ValueError: invalid literal for int() with base
    16: ''` from this helper and a Tools grid that never appeared. The helper
    is now total: a malformed side falls back to the other colour, and two
    malformed sides fall back to the page background.
    """
    assert theme._hex_lerp("", theme.COLORS["accent_pink"], 0.5).lower() == \
        theme.COLORS["accent_pink"].lower()
    assert theme._hex_lerp(theme.COLORS["accent_pink"], None, 0.5).lower() == \
        theme.COLORS["accent_pink"].lower()
    assert theme._hex_lerp("", "", 0.5) == theme.COLORS["bg"]
    # The old normal path still blends.
    assert theme._hex_lerp("#000000", "#ffffff", 0.5) == "#7f7f7f"
    assert theme._hex_lerp("#000", "#fff", 1.0) == "#ffffff"


def test_status_line_centres_short_text_and_pans_long_text():
    """The strip must never silently clip a task line (user report)."""
    from app.ui.widgets.status_line import StatusLine

    root = make_root()
    try:
        root.geometry("420x120")
        line = StatusLine(root, font=theme.TYPE["label_large"],
                          bg=theme.COLORS["bg"],
                          fg=theme.COLORS["subtext"])
        line.pack(fill="x")
        root.update()          # map the strip so it has a real width
        line.set_text("Ready")
        root.update()
        assert line.cget("text") == "Ready"
        assert line.itemcget(line._item_a, "anchor") == "center"
        assert line.itemcget(line._item_b, "state") == "hidden"

        long_text = ("DISM RestoreHealth - repairing Windows component "
                     "store - this line is deliberately far wider than the "
                     "strip and must marquee rather than clip")
        line.set_text(long_text)
        root.update()
        assert line.cget("text") == long_text
        assert line.itemcget(line._item_a, "anchor") == "w"
        assert line.itemcget(line._item_b, "state") == "normal"
        assert line._job is not None, "long text was never scheduled to pan"

        # Label-compatible colour updates still work through config().
        line.config(fg=theme.COLORS["accent_green"])
        assert line.cget("fg") == theme.COLORS["accent_green"]
    finally:
        root.destroy()


# --------------------------------------------------------------------------
# List rows
# --------------------------------------------------------------------------
def test_rows_have_three_sizes_and_one_type_role_each():
    from app.ui.widgets.tool_row import SIZES
    assert set(SIZES) == {"sm", "md", "lg"}
    heights = [SIZES[k][4] for k in ("sm", "md", "lg")]
    assert heights == sorted(heights) == [36, 44, 52]
    # Every rung's title comes from the type scale, never from a literal.
    for name, (title, sub, meta, _pad, _h) in SIZES.items():
        assert title in theme.TYPE.values(), name
        assert sub in theme.TYPE.values(), name
        assert meta in theme.TYPE.values(), name


def test_row_sizes_measure_in_order_on_a_real_root():
    from app.ui.widgets.tool_row import ToolRow
    root = make_root()
    try:
        root.geometry("700x400")
        rows = {}
        for name in ("sm", "md", "lg"):
            r = ToolRow(root, title="Disk cleanup", sub="Temporary files",
                        size=name)
            r.pack(fill="x")
            rows[name] = r
        for _ in range(4):
            root.update_idletasks()
        # Requested height: what the row asks its container for. A window
        # that is never shown reports winfo_height()==1 for everything.
        heights = {n: rows[n].winfo_reqheight() for n in rows}
        assert heights["sm"] < heights["md"] < heights["lg"], heights
    finally:
        root.destroy()


def test_row_leading_slot_and_flat_fill():
    from app.ui.widgets.tool_row import ToolRow
    root = make_root()
    try:
        flat = ToolRow(root, title="12 Sep", meta="2.1 GB", size="sm",
                       show_dot=False, fill=theme.COLORS["surface"])
        assert flat.fill() == theme.COLORS["surface"]
        assert not flat.dot.winfo_manager(), "a dotless row still packed a dot"
        led = ToolRow(root, title="Chrome", size="md", leading_px=32,
                      show_dot=False)
        assert int(led.leading.cget("width")) == 32
        # The text column is packed AFTER the leading slot, so it starts to
        # its right rather than at the row's left edge.
        kids = led.body.winfo_children()
        assert kids.index(led.leading) < kids.index(led._col)
    finally:
        root.destroy()


def test_row_keyboard_activation_goes_through_one_path():
    from app.ui.widgets.tool_row import ToolRow
    root = make_root()
    hit = []
    try:
        row = ToolRow(root, title="Service", size="sm",
                      on_click=lambda _e=None: hit.append(1))
        row.pack(fill="x")
        root.update_idletasks()
        row.activate()
        assert hit == [1]
        assert row.cget("takefocus") in (1, "1")
    finally:
        root.destroy()


def test_bind_content_covers_widgets_added_later():
    from app.ui.widgets.tool_row import ToolRow
    root = make_root()
    try:
        row = ToolRow(root, title="Profile", size="md", show_dot=False)
        extra = tk.Label(row.actions, text="Load", bg=theme.COLORS["bg_alt"])
        extra.pack()
        row.bind_content()
        assert extra.bind("<Button-1>") == ""      # not clickable: no handler
        assert extra.bind("<Enter>"), "late content never got the hover tint"
    finally:
        root.destroy()


# --------------------------------------------------------------------------
# Empty states
# --------------------------------------------------------------------------
def test_empty_state_metrics_are_one_block():
    from app.ui.widgets.empty_state import METRICS
    assert METRICS["pad_top"] < METRICS["glyph_gap"]
    assert METRICS["glyph_font"][1] > METRICS["text_font"][1]
    assert METRICS["text_font"][1] > METRICS["detail_font"][1]
    assert METRICS["pad_x"] == theme.SPACE_5


def test_empty_state_button_belongs_to_the_empty_state():
    """The reason add_button exists: pack() fills the widget's own master."""
    from app.ui.widgets.empty_state import EmptyState
    root = make_root()
    try:
        es = EmptyState(root, glyph="☆", text="No saved setups yet.",
                        wrap=300)
        es.pack(fill="x")
        btn = es.add_button("Reload", lambda: None)
        assert btn is not None
        assert btn.master is es
        assert btn.role == "filled"
    finally:
        root.destroy()


def test_empty_state_honours_the_callers_wrap_width():
    from app.ui.widgets.empty_state import EmptyState
    root = make_root()
    try:
        es = EmptyState(root, text="Nothing matches.", wrap=260)
        labels = [w for w in es.winfo_children() if w.winfo_class() == "Label"]
        assert all(int(w.cget("wraplength")) == 260 for w in labels)
        # A not-yet-laid-out caller (0 or 1px) must not disable wrapping.
        tiny = EmptyState(root, text="Nothing matches.", wrap=1)
        lab = [w for w in tiny.winfo_children()
               if w.winfo_class() == "Label"][0]
        assert int(lab.cget("wraplength")) == EmptyState.DEFAULT_WRAP
    finally:
        root.destroy()


def test_datatable_paints_the_shared_empty_state():
    """A canvas empty state must use the same fonts as a packed one."""
    import tkinter.font as tkfont
    from app.ui.widgets.datatable import DataTable
    from app.ui.widgets.empty_state import METRICS

    root = make_root()
    try:
        root.geometry("640x420")
        t = DataTable(root, columns=[{"key": "n", "title": "Name",
                                      "w": 300}])
        t.pack(fill="both", expand=True)
        t.set_empty("Nothing here.", "No matches.", glyph="\u2713")
        for _ in range(6):
            root.update()
        seen = {}
        for item in t._cv.find_all():
            txt = t._cv.itemcget(item, "text")
            if txt:
                seen[txt] = tkfont.Font(
                    font=t._cv.itemcget(item, "font")).actual()
        assert "Nothing here." in seen, seen
        assert seen["Nothing here."]["size"] == METRICS["text_font"][1]
        assert seen["Nothing here."]["weight"] == "bold"
        assert seen["\u2713"]["size"] == METRICS["glyph_font"][1]
        assert seen["No matches."]["size"] == METRICS["detail_font"][1]
    finally:
        root.destroy()


# --------------------------------------------------------------------------
# The picker: one widget, the whole surface the dialogs were written against
# --------------------------------------------------------------------------
def test_m3_dropdown_covers_the_combobox_surface_dialogs_use():
    """C-005: every picker is M3Dropdown; the native widget is gone.

    The dialogs were written against ttk.Combobox's small surface, so the
    replacement has to keep it: textvariable, get/set, current(), values via
    item access and configure, and the `<<ComboboxSelected>>` event. This is
    the contract that let the migration happen without rewriting dialog
    logic, and the reason a native control cannot creep back in.
    """
    from app.ui.widgets.dropdown import M3Dropdown

    root = make_root()
    try:
        var = tk.StringVar(value="B")
        dd = M3Dropdown(root, textvariable=var, values=["A", "B", "C"])
        # A virtual <<ComboboxSelected>> event needs a MAPPED widget to be
        # delivered; a dialog's dropdown always is by the time a pick can
        # happen, so map it here rather than testing an unreachable state.
        dd.pack(fill="x")
        root.update()
        assert dd.get() == "B"
        assert dd.current() == 1
        dd.current(2)
        assert var.get() == "C"
        dd.set("A")
        assert dd.get() == "A"

        dd["values"] = ["X", "Y"]
        assert dd["values"] == ("X", "Y")
        dd.configure(values=["P", "Q"])
        assert dd["values"] == ("P", "Q")

        picked = []
        dd.bind("<<ComboboxSelected>>", lambda _e: picked.append(dd.get()))
        dd._choose("Q")
        root.update()
        assert picked == ["Q"]

        dd.configure(state="disabled")
        assert dd._enabled is False
        dd._on_click()
        assert dd._open is False          # disabled fields never post a menu
        dd.configure(state="readonly")
        assert dd._enabled is True
    finally:
        root.destroy()


def test_no_native_ttk_widget_survives_in_the_shell():
    """The shell must not configure ttk styles it has no widgets for.

    A dead `ttk.Style()` block is not harmless: it is the path of least
    resistance for the next native control to ship looking "close enough".
    test_design_guard rejects ttk source-wide; this pins the shell's own
    style setup being gone, which is where the last Combobox styling lived.
    """
    import ast
    import inspect

    from app.ui import app as app_module

    # AST, not source text: the explanatory comment above the removed block
    # names `_build_style` and TCombobox, and a string search would fail on
    # its own documentation (the same trap the PowerShell guard records).
    tree = ast.parse(inspect.getsource(app_module))
    functions = {n.name for n in ast.walk(tree)
                 if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
    assert "_build_style" not in functions
    imports = {alias.name for n in ast.walk(tree)
               if isinstance(n, ast.Import) for alias in n.names}
    assert "ttk" not in imports
    from_imports = {alias.name for n in ast.walk(tree)
                    if isinstance(n, ast.ImportFrom) for alias in n.names}
    assert "ttk" not in from_imports
    ttk_attrs = [n.attr for n in ast.walk(tree)
                 if isinstance(n, ast.Attribute)
                 and isinstance(n.value, ast.Name) and n.value.id == "ttk"]
    assert ttk_attrs == []
