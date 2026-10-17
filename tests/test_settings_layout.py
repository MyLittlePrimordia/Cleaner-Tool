"""Regression tests for Settings' two-pane layout (2026-10 redesign).

Why these build a REAL Tk widget instead of inspecting the source: the bug they
guard against was a geometry bug. The old three-column page overflowed its
viewport at every supported window size (content 832px vs a 610-795px view) and
clipped long supporting text (the Quarantine tip wanted 425px and was handed
244-373px). Neither is visible in the source, and both are one number away from
silently coming back on a different font/DPI, so the test measures the built
page the same way the audit did.

Side-effect free: the config loader, the scheduler resync and the drivers /
quarantine lookups are all stubbed, so nothing here reads or writes the real
machine (no registry, no scheduled tasks, no config file). Tk itself is skipped
gracefully when there is no display.
"""
from __future__ import annotations

import pytest

tk = pytest.importorskip("tkinter")

from app.ui.tabs import settingstab as S
from app.ui.widgets.scroll import ScrollableRoundedPanel


class _StubApp:
    """Just the three methods SettingsTab calls back into."""

    def export_logs(self):
        pass

    def run_tasks(self, *a, **k):
        pass

    def _refresh_health_badge(self):
        pass


@pytest.fixture
def quiet(monkeypatch):
    """Neutralise every side effect SettingsTab.__init__ would otherwise have.

    SettingsTab re-imports each of these at call time (`from x import y`
    inside the method), so patching the module attribute is enough."""
    monkeypatch.setattr("app.settings_apply.resync_flags", lambda: None)
    monkeypatch.setattr("app.config_persist.load_config", lambda: {})
    monkeypatch.setattr("app.drivers.folder.resolve",
                        lambda custom=None: r"C:\Temp\drivers")
    monkeypatch.setattr("app.drivers.folder.writable", lambda path: True)
    monkeypatch.setattr("app.quarantine.stats", lambda root=None: (0, 0))
    return monkeypatch


def _build(size=(980, 700), scaling=None):
    try:
        root = tk.Tk()
    except tk.TclError as exc:              # pragma: no cover - no display
        pytest.skip("no display for Tk: %s" % exc)
    if scaling is not None:
        # Set BEFORE building: widget sizes are computed at construction, and
        # this is what reproduces the 200%-display clip the tests below guard.
        root.tk.call("tk", "scaling", scaling)
    root.geometry("%dx%d" % size)
    page = tk.Frame(root, bg="#0d0d0d")
    page.pack(fill="both", expand=True)
    tab = S.SettingsTab(page, _StubApp())
    tab.pack(fill="both", expand=True)
    _settle(root)
    return root, tab


def _settle(root):
    for _ in range(4):
        root.update_idletasks()
        root.update()


def _descendants(widget):
    stack = [widget]
    while stack:
        w = stack.pop()
        yield w
        try:
            stack.extend(w.winfo_children())
        except Exception:
            pass


# --------------------------------------------------------------------------- #
# structure
# --------------------------------------------------------------------------- #

def test_rail_has_one_item_per_category(quiet):
    root, tab = _build()
    try:
        assert list(tab._nav_items) == [k for k, _ in S.CATEGORIES]
        # every category has a content frame to show
        assert list(tab._cats) == [k for k, _ in S.CATEGORIES]
    finally:
        root.destroy()


def test_content_pane_uses_shared_scroll_panel(quiet):
    """Regression for the private `_VScroll`: the app's global wheel router
    (Application._on_wheel_routed) finds a ScrollableRoundedPanel by walking up
    from the widget under the pointer, so a private scroller was invisible to
    the wheel. The pane must be the shared panel."""
    root, tab = _build()
    try:
        assert isinstance(tab._panel, ScrollableRoundedPanel)
    finally:
        root.destroy()


def test_only_the_selected_category_is_packed(quiet):
    root, tab = _build()
    try:
        tab._show_cat("tweaks")
        _settle(root)
        assert [k for k, it in tab._nav_items.items() if it._selected] == ["tweaks"]
        packed = [k for k, f in tab._cats.items()
                  if f.winfo_manager() == "pack"]
        assert packed == ["tweaks"]
        # an unknown key is a no-op, not a crash
        tab._show_cat("does-not-exist")
        assert [k for k, it in tab._nav_items.items() if it._selected] == ["tweaks"]
    finally:
        root.destroy()


# --------------------------------------------------------------------------- #
# the two bugs: overflow and clipping
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("size", [(1280, 885), (980, 700), (640, 600)])
def test_no_category_needs_to_scroll(quiet, size):
    """Every category fits its pane — nothing to scroll — at the default size,
    the 980x700 floor, and a size well BELOW the floor (where text has to wrap
    more, which is exactly when a fixed layout would overflow)."""
    root, tab = _build(size)
    try:
        panel = tab._panel
        for key, _label in S.CATEGORIES:
            tab._show_cat(key)
            _settle(root)
            assert panel.inner.winfo_reqheight() <= panel.view_height() + 1, (
                "category %r overflows its pane at %s" % (key, size))
    finally:
        root.destroy()


@pytest.mark.parametrize("size", [(1280, 885), (980, 700), (640, 600)])
def test_no_supporting_text_is_clipped(quiet, size):
    """A Label whose requested width exceeds what it was given renders with its
    glyphs cut off — the visible 'clipping' the redesign fixes. At every size,
    in every category, no text Label may be narrower than it asked for."""
    root, tab = _build(size)
    try:
        for key, _label in S.CATEGORIES:
            tab._show_cat(key)
            _settle(root)
            for w in _descendants(tab._cats[key]):
                if not isinstance(w, tk.Label):
                    continue
                if not str(w.cget("text")).strip():
                    continue
                req, act = w.winfo_reqwidth(), w.winfo_width()
                assert req <= act + 2, (
                    "clipped label %r in %r at %s: wants %dpx, has %dpx"
                    % (str(w.cget("text"))[:40], key, size, req, act))
    finally:
        root.destroy()


def test_supporting_text_registers_a_wraplength(quiet):
    """The mechanism behind 'no clipping': supporting labels are registered so
    their wraplength tracks the pane width. If a future edit builds a tip with
    no wraplength, this drops to zero and the clip comes back."""
    root, tab = _build((980, 700))
    try:
        assert tab._wrap_labels, "no supporting labels were registered"
        for lbl in tab._wrap_labels:
            assert int(lbl.cget("wraplength")) > 0
            # and it tracks the pane, not a hardcoded 240 that clips
            assert int(lbl.cget("wraplength")) <= tab._panel.winfo_width()
    finally:
        root.destroy()


# --------------------------------------------------------------------------- #
# DPI / text-fit clipping (2026-10 follow-up)
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("scaling", [1.332282448377581, 1.75, 2.0])
def test_rail_never_clips_its_widest_label(quiet, scaling):
    """The rail was a hardcoded 196px. On a DPI-aware 200% display (tk scaling
    2.0) "Tweaks & backup" needs 210px, so its last glyph was cut off — the
    'tabs clipping' bug. The rail now measures its widest label in the bold
    (selected) face, so it is wide enough at every scaling."""
    import tkinter.font as tkfont

    root, tab = _build((980, 700), scaling=scaling)
    try:
        reg = tkfont.Font(root=root, font=(S.F, 11))
        bold = tkfont.Font(root=root, font=(S.F, 11, "bold"))
        for key, item in tab._nav_items.items():
            need = 16 + max(reg.measure(item._text), bold.measure(item._text))
            assert need <= item.winfo_width(), (
                "rail item %r clipped at scaling %s: needs %dpx, has %dpx"
                % (key, scaling, need, item.winfo_width()))
    finally:
        root.destroy()


@pytest.mark.parametrize("scaling", [1.332282448377581, 1.75, 2.0])
def test_dropdown_fits_its_longest_value(quiet, scaling):
    """M3Dropdown sized itself from a fixed '7.4px per character' estimate,
    which is right for 10pt at 100% but half the real character width at tk
    scaling 2.0 — so the PC Health picker drew "At startup" as "At start…".
    The field is now measured against the real font and widened to fit its
    longest value, so its own text is never ellipsized."""
    from app.ui.widgets.dropdown import M3Dropdown
    from app.ui.theme import ellipsize

    root, _tab = _build((980, 700), scaling=scaling)
    try:
        host = tk.Frame(root, bg=S.COLORS["bg"])
        host.pack()
        values = ["Off", "At startup", "Daily", "Weekly"]
        dd = M3Dropdown(host, values=values, font=(S.F, 10))
        room = dd.winfo_reqwidth() - 64      # matches M3Dropdown._FIELD_RESERVE
        for value in values:
            assert ellipsize(value, (S.F, 10), room) == value, (
                "dropdown ellipsizes %r at scaling %s" % (value, scaling))
    finally:
        root.destroy()


def test_drivers_folder_buttons_are_explicit(quiet):
    """"Change…" beside "Open" named neither the folder nor the action. The
    buttons now say what they do; this guards the wording from drifting back."""
    from app.ui.widgets import AnimatedButton

    root, tab = _build((980, 700))
    try:
        labels = [getattr(w, "_text", "")
                  for w in _descendants(tab._cats["drivers"])
                  if isinstance(w, AnimatedButton)]
        assert "Change folder…" in labels
        assert "Open folder" in labels
    finally:
        root.destroy()


# --------------------------------------------------------------------------- #
# per-row applied-tweaks browser (roadmap 2.1's second half)
# --------------------------------------------------------------------------- #

def test_tweaks_category_has_a_review_button(quiet):
    from app.ui.widgets import AnimatedButton

    root, tab = _build((980, 700))
    try:
        btns = [w for w in _descendants(tab._cats["tweaks"])
                if isinstance(w, AnimatedButton)]
        labels = [getattr(b, "_text", "") for b in btns]
        assert "Review applied tweaks…" in labels
        assert callable(tab._review_tweaks)
        assert callable(tab._undo_tasks)
    finally:
        root.destroy()


def test_tweak_snapshots_dialog_imports():
    import app.ui.dialogs.tweak_snapshots_dialog as d

    assert callable(d.show_tweak_snapshots)
