"""The 2026-10 makeover's later stages: one grid, one footer, one strip.

Stage 3 closed with three page-level grids still hardcoding their own column
count (C-003), stage 4 found five plain popups each hand-building the same
bottom bar, and stage 5 found the run bar growing the window's last band only
while a run was in flight. Each of those is a shared component now; these
tests pin the properties that made it worth sharing, not the widget classes:

* a grid re-flows the SAME cards when the column count changes, centres a
  lone trailing card, and gives unused columns zero weight;
* a footer reserves its space first, puts the primary at the far right, puts
  the destructive action on the quiet left, and keeps the primary still when
  a secondary is removed;
* the progress strip keeps its height whether or not the bar is showing.

Headless-safe: every widget test skips without a display.
"""
from __future__ import annotations

import pytest

from app.ui.widgets import page

tk = pytest.importorskip("tkinter")

from dev.tests._tkhelpers import make_root  # noqa: E402


# --------------------------------------------------------------------------
# one page grid
# --------------------------------------------------------------------------
def test_flow_columns_reads_the_width_not_the_card_count():
    # The numbers the 980px window floor and the 1280px default really hand
    # the grid: measured inner widths, not guesses.
    assert page.flow_columns(800) == 3
    assert page.flow_columns(1100) == 4
    assert page.flow_columns(760, max_columns=3) == 3
    # Never zero columns, never more than the caller allows.
    assert page.flow_columns(0) == 1
    assert page.flow_columns(10) == 1
    assert page.flow_columns(4000) == page.CARD_MAX_COLS
    assert page.flow_columns(2000, max_columns=2) == 2


def test_grid_cards_centres_a_lone_trailing_card():
    root = make_root()
    try:
        holder = tk.Frame(root)
        cards = [tk.Frame(holder) for _ in range(5)]
        page.grid_cards(holder, cards, 3)
        # 5 cards, 3 columns: the fifth sits in the MIDDLE column, not
        # stranded against the left edge.
        info = cards[4].grid_info()
        assert (int(info["row"]), int(info["column"])) == (1, 1)
        assert all(int(c.grid_info()["row"]) == 0 for c in cards[:3])
    finally:
        root.destroy()


def test_grid_cards_regrids_without_rebuilding_and_clears_stale_weights():
    root = make_root()
    try:
        holder = tk.Frame(root)
        cards = [tk.Frame(holder) for _ in range(8)]
        page.grid_cards(holder, cards, 3)
        assert holder.grid_columnconfigure(2)["weight"] == 1
        # Same eight widgets, wider page: four columns, and the fourth is
        # now a real column.
        page.grid_cards(holder, cards, 4)
        assert int(cards[3].grid_info()["column"]) == 3
        assert holder.grid_columnconfigure(3)["weight"] == 1
        # Narrow again: column three must give its share back, or Tk keeps
        # a blank strip of row where a column used to be.
        page.grid_cards(holder, cards, 2)
        assert int(holder.grid_columnconfigure(3)["weight"]) == 0
        assert int(cards[3].grid_info()["column"]) == 1
    finally:
        root.destroy()


def test_the_status_strip_reserves_the_run_time_control():
    """The resting status row must already be as tall as it is during a run.

    A source-level check rather than a main-window build: constructing
    Application in the suite would touch the user's real state, and the
    property is a single number. The run-time Stop button is 32px including
    its padding; a 26px status line made the whole band grow by 4px on every
    run start, which is exactly the jump ProgressRow exists to prevent.
    """
    import inspect

    from app.ui import app as app_module

    source = inspect.getsource(app_module.Application)
    assert 'height=30' in source.split('self.status_lbl = StatusLine', 1)[1][:400]


def test_the_tools_page_is_a_fixed_four_column_card_grid():
    from app.ui.tabs.toolstab import ToolsTab

    root = make_root()
    try:
        root.withdraw()
        tab = ToolsTab(root, app=None)
        assert len(tab._cards) == 19
        for card in tab._cards.values():
            assert int(card.grid_info()["column"]) in (0, 1, 2, 3)
    finally:
        root.destroy()


# --------------------------------------------------------------------------
# one popup footer
# --------------------------------------------------------------------------
def test_modal_footer_reserves_the_bottom_and_pins_the_primary_right():
    from app.ui import theme
    from app.ui.widgets import ModalFooter

    root = make_root()
    try:
        body = tk.Frame(root)
        foot = ModalFooter(body, accent="#00ff00")
        foot.pack_footer()
        info = foot.pack_info()
        assert info["side"] == "bottom"
        # The body owns the side inset now, so the footer must not add a
        # second one (the double inset was what put the buttons 16px inside
        # the banner above them).
        assert int(info["padx"]) == 0
        assert str(info["pady"]).replace(" ", "") == (
            "(%d,0)" % theme.DIALOG_PAD_TOP)
        # Packed before the dialog's content, so a long list has to give
        # ground - never the buttons.
        assert body.pack_slaves()[-1] is foot
        # Pack order inside the bar: quiet (left) -> hint -> primary ->
        # secondary. The primary is packed BEFORE secondaries so adding or
        # removing one cannot move it.
        slots = [str(w) for w in foot.pack_slaves()]
        names = [str(foot.quiet_slot), str(foot.hint_slot),
                 str(foot.primary_slot), str(foot.secondary_slot)]
        assert slots == names

        close = foot.button("Close", lambda: None, kind="primary")
        clear = foot.button("Clear", lambda: None, kind="quiet")
        again = foot.button("Again", lambda: None)
        assert close.master is foot.primary_slot
        assert clear.master is foot.quiet_slot
        assert again.master is foot.secondary_slot
        # A secondary disappearing leaves the primary's master untouched.
        again.destroy()
        assert close.master is foot.primary_slot
        assert foot._hint is None
        assert foot.set_hint("Ready.").cget("text") == "Ready."
    finally:
        root.destroy()


def test_themed_modal_owns_one_body_inset_for_every_popup():
    """The body starts/ends on the shared scale, not on 18px per dialog."""
    from app.ui import theme
    from app.ui.base import ThemedModal

    root = make_root()
    try:
        dlg = ThemedModal(root, title="Probe", size=(800, 600))
        root.update()
        assert dlg.body.winfo_x() == dlg.RADIUS + 2 + theme.DIALOG_PAD_X
        assert dlg.body.winfo_y() == dlg._TITLE_H + 2 + theme.DIALOG_PAD_TOP
        # body_width is the real content column now, not the window width
        # minus a caller's guess.
        assert abs(int(dlg.body_width()) - dlg.body.winfo_width()) <= 2
        dlg.close()
    finally:
        root.destroy()


def test_the_plain_dialogs_share_the_footer():
    import inspect

    from app.ui.dialogs import (
        catalog_picker_dialog,
        install_profiles_dialog,
        past_runs_dialog,
        scorecard_dialog,
    )

    for module in (catalog_picker_dialog, install_profiles_dialog,
                   past_runs_dialog, scorecard_dialog):
        source = inspect.getsource(module)
        assert "ModalFooter" in source, module.__name__
        # The hand-built strip this replaced: a bottom frame with side
        # padding and the old (12, 10) height, written a fifth time.
        assert 'pady=(12, 10)' not in source, module.__name__
        # And no body-level padding left over now that ThemedModal owns it:
        # a dialog that adds its own 18 would sit 34px inside the card.
        assert "padx=18" not in source, module.__name__


def test_install_profiles_keeps_its_status_inside_the_shared_footer():
    from app.ui.dialogs.install_profiles_dialog import InstallProfilesDialog

    class _Tab:
        def profile_ids(self):
            return []

    root = make_root()
    try:
        dlg = InstallProfilesDialog(root, _Tab())
        assert dlg._status.master is dlg._footer.hint_slot
        assert dlg._save_btn.master is not dlg._footer
        dlg.close()
    finally:
        root.destroy()


# --------------------------------------------------------------------------
# one reserved progress strip
# --------------------------------------------------------------------------
def test_progress_row_keeps_its_height_with_the_bar_hidden():
    from app.ui.widgets import ProgressRow

    root = make_root()
    try:
        host = tk.Frame(root)
        host.pack(fill="both", expand=True)
        row = ProgressRow(host)
        row.pack(fill="x")
        root.update()
        assert row.bar.winfo_manager() == ""
        hidden_h = row.winfo_height()
        assert hidden_h == ProgressRow.HEIGHT

        row.show()
        root.update()
        assert row.bar.winfo_manager() == "pack"
        assert row.winfo_height() == hidden_h

        row.hide()
        root.update()
        assert row.bar.winfo_manager() == ""
        assert row.winfo_height() == hidden_h
    finally:
        root.destroy()


def test_the_app_keeps_progress_bar_pointing_at_the_reserved_row():
    import inspect

    from app.ui import app as app_module

    source = inspect.getsource(app_module.Application)
    assert "self.progress_row = ProgressRow(" in source
    assert "self.progress_bar = self.progress_row.bar" in source
    # The show/hide path must act on the row, not re-pack the window band.
    from app.ui import runflow
    flow = inspect.getsource(runflow.RunFlow)
    assert "self.progress_row.show()" in flow
    assert "self.progress_row.hide()" in flow


# --------------------------------------------------------------------------
# DPI-aware icons
# --------------------------------------------------------------------------
def test_icon_target_px_follows_the_device_scale():
    from app.ui.theme import icon_target_px

    class _At100:
        def winfo_fpixels(self, _what):
            return 96.0

    class _At200:
        def winfo_fpixels(self, _what):
            return 192.0

    class _Broken:
        def winfo_fpixels(self, _what):
            raise RuntimeError("no display")

    assert icon_target_px(36, _At100()) == 36
    assert icon_target_px(36, _At200()) == 72
    # A widget that cannot answer falls back to 1x instead of raising: the
    # icon loader must never fail a page build over a DPI query.
    assert icon_target_px(36, _Broken()) == 36


def test_scaled_photo_reaches_a_target_integer_subsample_cannot():
    """72 -> 48 needs zoom(2)/subsample(3); rounding a bare factor gave 36.

    That 36px state is what the 200% pass caught: icons stayed 100%-sized
    beside text that had doubled. The helper is the shared fix for every
    icon loader, so it is pinned at the awkward ratios here.
    """
    from app.ui.theme import scaled_photo
    from dev.tests._tkhelpers import make_root

    root = make_root()
    try:
        photo = tk.PhotoImage(width=72, height=72)
        assert scaled_photo(photo, 72).width() == 72
        assert scaled_photo(photo, 36).width() == 36
        assert scaled_photo(photo, 48).width() == 48
        assert scaled_photo(photo, 24).width() == 24
        # Targets are clamped to a positive size; a bad number must not ask
        # Tk for a zero-width image.
        assert scaled_photo(photo, 0).width() >= 1
    finally:
        root.destroy()


# --------------------------------------------------------------------------
# the empty states that moved onto the shared primitive
# --------------------------------------------------------------------------
def _has_empty_state(widget, cls):
    for child in widget.winfo_children():
        if isinstance(child, cls):
            return True
        if _has_empty_state(child, cls):
            return True
    return False


def test_history_viewers_use_the_shared_empty_state():
    from app.ui.dialogs.past_runs_dialog import PastRunsDialog
    from app.ui.widgets import EmptyState

    root = make_root()
    try:
        dlg = PastRunsDialog(root, entries=[])
        assert _has_empty_state(dlg._panel.inner, EmptyState)
        dlg.close()
    finally:
        root.destroy()
