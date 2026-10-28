"""Phase 0 of the 2026-10 Tools audit: the cheap, visible fixes.

Each test pins one approved item (IDs from AUDIT-2026-10-TOOLS.md):

    TL-001  Game Readiness reads off the Tk thread
    TL-002  Game Saves lists off the Tk thread
    TV-006  the shared status line can show a determinate fraction
    TL-013  Tools/Drivers are built on first visit, not at launch
    TV-009  an idle gamepad does not repaint every frame
    TV-008  a card's accent encodes its job, not its origin tab
    TL-007  protected leftovers are reported as admin-gated BEFORE deleting
"""
from __future__ import annotations

import threading
import time

import pytest

tk = pytest.importorskip("tkinter")

from dev.tests._tkhelpers import make_root  # noqa: E402

# --------------------------------------------------------------------------- #
# TV-006 — determinate progress
# --------------------------------------------------------------------------- #

def test_the_progress_caption_carries_count_and_percentage():
    from app.ui.dialogs._tool_page import progress_caption

    assert progress_caption(1, 4, "Scanning", "files") == \
        "Scanning · 1 of 4 files (25%)"
    # Thousands are grouped, not a wall of digits.
    assert progress_caption(1284, 4910, "Hashing", "files") == \
        "Hashing · 1,284 of 4,910 files (26%)"
    # No label, no dangling separator.
    assert progress_caption(2, 2) == "2 of 2 (100%)"
    # A 99/100 step rounds UP: a bar pinned at 0 is what the fix is for.
    assert "(99%)" in progress_caption(99, 100)


def test_the_status_line_fills_instead_of_marching():
    from app.ui.dialogs._tool_page import StatusLine

    root = make_root()
    try:
        root.withdraw()
        line = StatusLine(root)
        line.pack(fill="x")
        root.update_idletasks()
        line.progress(1, 4, "Scanning", "files")
        assert line._frac == pytest.approx(0.25)
        assert "1 of 4" in line.caption.cget("text")
        assert "25%" in line.caption.cget("text")
        # stop() must return the rail to idle, or a finished run leaves a
        # 25% bar on screen forever.
        line.stop("Done.")
        assert line._frac is None
        # An unknown total is honest: back to the indeterminate march.
        line.progress(0, 0, "Working")
        assert line._frac is None
        assert line._busy is True
        line.stop("")
    finally:
        root.destroy()


# --------------------------------------------------------------------------- #
# TL-001 / TL-002 — the two blocking reads
# --------------------------------------------------------------------------- #

class _StubApp:
    def __init__(self):
        self.calls = []

    def log(self, _msg):
        pass

    def _show_tab(self, name):
        self.calls.append(("show", name))

    def _open_storage_insight(self):
        self.calls.append(("storage", None))

    def run_tasks(self, *a, **k):
        self.calls.append(("run", a))


def _pump(root, seconds=3.0):
    end = time.time() + seconds
    while time.time() < end:
        root.update()
        time.sleep(0.02)


def test_game_readiness_reads_off_the_tk_thread(monkeypatch):
    from app import game_readiness
    from app.ui.dialogs.game_readiness_dialog import GameReadinessDialog

    seen = {}

    def fake_check():
        seen["thread"] = threading.current_thread().name
        seen["main"] = threading.current_thread() is threading.main_thread()
        time.sleep(0.5)
        return [{"key": "game_mode", "label": "Game Mode", "state": "ok",
                 "detail": "on", "fix": None, "fix_label": ""}]

    monkeypatch.setattr(game_readiness, "check", fake_check)
    root = make_root()
    try:
        root.withdraw()
        started = time.time()
        dlg = GameReadinessDialog(root, _StubApp())
        elapsed = time.time() - started
        # The constructor returned while check() was still sleeping: the old
        # code blocked here for up to 45 s.
        assert elapsed < 0.35, "constructor blocked for %.2fs" % elapsed
        assert dlg._busy is True
        _pump(root)
        assert seen.get("main") is False, "check() ran on the Tk thread"
        assert dlg._busy is False
        assert dlg._rows and dlg._rows[0]["key"] == "game_mode"
        assert "Checked 1 settings" in dlg.status._raw
        dlg.close()
    finally:
        root.destroy()


def test_game_saves_lists_off_the_tk_thread(monkeypatch):
    from app import game_saves
    from app.ui.dialogs.game_saves_dialog import GameSavesDialog

    seen = {}

    def fake_list():
        seen["main"] = threading.current_thread() is threading.main_thread()
        time.sleep(0.5)
        return []

    monkeypatch.setattr(game_saves, "list_backups", fake_list)
    root = make_root()
    try:
        root.withdraw()
        started = time.time()
        dlg = GameSavesDialog(root, _StubApp())
        elapsed = time.time() - started
        assert elapsed < 0.35, "constructor blocked for %.2fs" % elapsed
        _pump(root)
        assert seen.get("main") is False, "list_backups() ran on the Tk thread"
        assert dlg._busy is False
        assert dlg._rows == []
        dlg.close()
    finally:
        root.destroy()


# --------------------------------------------------------------------------- #
# TL-013 — Tools and Drivers are lazy pages
# --------------------------------------------------------------------------- #

def test_a_lazy_page_is_built_once_then_cached():
    from app.ui.app import Application

    app = object.__new__(Application)
    built = []
    app.tabs = {}
    app._tab_factories = {"Tools": lambda: built.append(1) or "page"}
    app.log = lambda _m: None

    assert app._ensure_tab("Tools") == "page"
    assert app._ensure_tab("Tools") == "page"
    assert built == [1], "the factory ran more than once"
    assert app.tabs["Tools"] == "page"


def test_tools_and_drivers_are_registered_as_lazy_factories():
    """A source guard, not a runtime one.

    Building a real Application in the suite is heavy (it starts background
    probes), so this pins the construction table instead: a card page that
    goes back to `self.tabs[name] = ...` here is exactly the regression that
    made launch pay for a page nobody opened.
    """
    import inspect

    from app.ui.app import Application

    src = inspect.getsource(Application._build_main_ui)
    assert "self._tab_factories[name] = (" in src
    for name in ("Tools", "Drivers", "Settings"):
        assert ('elif name == "%s":' % name) in src
    # No direct construction of the three lazy pages in the table.
    assert "self.tabs[name] = ToolsTab(" not in src
    assert "self.tabs[name] = DriversTab(" not in src
    assert "self.tabs[name] = SettingsTab(" not in src


# 2026-10: the TV-009 tests here drove the gamepad tester's idle tick. The
# pad, keyboard, mouse, display, camera, mic and speaker testers were removed
# with the Advisor/What Changed pair once nothing in the app reached them (see
# dev/tests/test_tool_open_smoke.py for the reachability report).


# --------------------------------------------------------------------------- #
# TV-008 — accents by job
# --------------------------------------------------------------------------- #

def test_every_card_has_a_key_icon_title_and_blurb():
    from app.ui.tabs.toolstab import ToolsTab

    keys = [c[0] for c in ToolsTab._CARDS]
    assert len(keys) == 19
    assert len(set(keys)) == 19, "duplicate card key"
    for key, icon, title, blurb in ToolsTab._CARDS:
        assert str(icon or "").strip(), "%s has no icon" % key
        assert str(title or "").strip(), "%s has no title" % key
        assert str(blurb or "").strip(), "%s has no explanation" % key


# --------------------------------------------------------------------------- #
# TV-002 — every card has an icon source, and orphan art stays gone
# --------------------------------------------------------------------------- #

def test_every_card_declares_an_icon_and_orphan_art_is_gone():
    """Three cards still draw the emoji glyph (their painted art needs drawn
    assets, not a font render), so the contract is "an icon source exists",
    not "a PNG exists": a blank glyph is the one thing that would render an
    empty square, and the two orphans were artwork for cards that no longer
    exist.
    """
    import pathlib

    from app.ui.tabs.toolstab import ToolsTab

    art = pathlib.Path(__file__).resolve().parents[2] / "app" / "assets" \
        / "tools_icons"
    # health/quick_tools were art for two cards that stopped existing, and the
    # seven below belonged to the peripheral testers removed in 2026-10. A PNG
    # with no row is invisible weight: nothing loads it, nothing reports it.
    for gone in ("health", "quick_tools", "gamepad", "keyboard", "mouse",
                 "monitor", "webcam", "miccheck", "speaker"):
        assert not (art / (gone + ".png")).exists(), (
            "orphan artwork came back: %s.png" % gone)
    for key, glyph, _title, _blurb in ToolsTab._CARDS:
        assert str(glyph or "").strip(), "%s has no icon at all" % key


def test_every_card_is_built_as_the_shared_tool_card():
    """One card component, one anatomy, for all 19 entries.

    The page is a grid of tiles, not a list of rows: a row here would be a
    regression to the directory revision, where the whole catalogue had to
    put every description in a tooltip to stay one line tall.
    """
    from app.ui.tabs.toolstab import ToolsTab
    from app.ui.widgets import TOOL_CARD_HEIGHT, ToolCard, ToolRow

    root = make_root()
    try:
        root.withdraw()
        tab = ToolsTab(root, _StubApp())
        assert set(tab._cards) == {c[0] for c in ToolsTab._CARDS}
        for key, card in tab._cards.items():
            assert isinstance(card, ToolCard), key
            assert not isinstance(card, ToolRow), key
            assert card.winfo_manager() == "grid", key
        for _ in range(4):
            root.update()
        # Symmetry is a property of the page, not of each card: every tile
        # asks for the same height, so a row of them shares one baseline.
        heights = {card.winfo_reqheight() for card in tab._cards.values()}
        assert len(heights) == 1, heights
        assert heights.pop() >= TOOL_CARD_HEIGHT
    finally:
        root.destroy()


def test_every_tool_glyph_stays_inside_its_icon_chip():
    """An art-less tool draws its emoji inside the chip, not over it.

    Tk measures an emoji VARIATION SELECTOR as a second glyph: most cards
    carry a PNG, three fall back to an emoji, and "\U0001F6E1\uFE0F" measured
    82px at 18pt instead of 41 - a mark twice as wide as its chip. The glyph
    is drawn without the selector; this pins that it fits at 100% and 125%.
    """
    from app.ui.tabs.toolstab import ToolsTab

    for scaling in (1.3333, 1.6667):
        root = make_root()
        # Tk's point scale is a property of the DISPLAY, so it has to be put
        # back: a test that leaves 1.667 behind renders every later test at
        # 125% inside a 100%-sized window and they fail for the wrong reason.
        original = root.tk.call("tk", "scaling")
        try:
            root.tk.call("tk", "scaling", scaling)
            root.withdraw()
            tab = ToolsTab(root, _StubApp())
            for _ in range(4):
                root.update()
            checked = 0
            for key, card in tab._cards.items():
                canvas = card.icon_lbl
                box = canvas.winfo_reqwidth()
                # Only the GLYPH is measured: the chip oval fills the box by
                # design, and a PNG may land a pixel or two over its target
                # inside its own transparent margin.
                texts = [i for i in canvas.find_all()
                         if canvas.type(i) == "text"]
                if not texts:
                    continue
                checked += 1
                left, top, right, bottom = canvas.bbox(texts[0])
                assert left >= 0 and top >= 0, (key, scaling, (
                    left, top, right, bottom))
                assert right <= box and bottom <= box, (key, scaling, (
                    left, top, right, bottom))
            assert checked >= 3, "the art-less cards stopped drawing a glyph"
        finally:
            try:
                root.tk.call("tk", "scaling", original)
            except Exception:
                pass
            root.destroy()


def test_a_tool_card_hovers_and_restores_its_face():
    """Hover paints the whole face, and un-hover puts it back.

    A card grid is nineteen hover surfaces at once: a tint that sticks (or
    that repaints only the outer canvas and leaves the labels on the resting
    colour) leaves a pale rectangle inside every card the mouse crossed.
    """
    from app.ui import theme
    from app.ui.tabs.toolstab import ToolsTab

    root = make_root()
    try:
        root.withdraw()
        tab = ToolsTab(root, _StubApp())
        card = tab._cards["storage"]
        card._hover_at(True)
        hot = theme.COLORS["surface_hover"].lower()
        assert card.fill().lower() == hot
        assert card.title_lbl.cget("bg").lower() == hot
        assert card.blurb_lbl.cget("bg").lower() == hot
        card._hover_at(False)
        face = theme.SURFACE_CARD.lower()
        assert card.fill().lower() == face
        assert card.title_lbl.cget("bg").lower() == face
        assert card.blurb_lbl.cget("bg").lower() == face
    finally:
        root.destroy()


def test_a_tool_card_opens_from_the_keyboard():
    """Return or Space on a focused card opens it: a card is not mouse-only."""
    from app.ui.tabs.toolstab import ToolsTab

    class _App:
        def __init__(self):
            self.opened = []

        def log(self, *_a, **_k):
            return None

        def __getattr__(self, name):
            if name.startswith("_open_"):
                return lambda: self.opened.append(name)
            raise AttributeError(name)

    app = _App()
    root = make_root()
    try:
        root.geometry("1240x860")
        root.deiconify()
        tab = ToolsTab(root, app)
        tab.pack(fill="both", expand=True)
        for _ in range(4):
            root.update()
        card = tab._cards["storage"]
        assert int(card.cget("takefocus")) == 1
        # focus_force, not focus_set: without an OS-focused toplevel the
        # window manager may refuse the focus request, and the synthesized
        # key then goes nowhere. This is a contract test, not a WM test.
        card.focus_force()
        root.update()
        card.event_generate("<Return>")
        root.update()
        assert app.opened == ["_open_storage_insight"]
        card.event_generate("<space>")
        root.update()
        assert app.opened == ["_open_storage_insight"] * 2
    finally:
        root.destroy()


# --------------------------------------------------------------------------- #
# TV-006 (wiring) — a category walk reports a step, not a loop
# --------------------------------------------------------------------------- #

def test_storage_category_walk_feeds_the_status_line():
    from types import SimpleNamespace

    from app.ui.dialogs.storage_insight_dialog import StorageInsightDialog

    dlg = object.__new__(StorageInsightDialog)
    dlg._scan_step = None
    seen = []
    dlg.status = SimpleNamespace(
        progress=lambda *a: seen.append(("progress",) + a),
        stop=lambda *a: seen.append(("stop",) + a))

    dlg._set_scan_status("Starting scan…")
    dlg._set_scan_step(2, 5)
    dlg._set_scan_status("Scanning browsers…")
    # Per-task messages keep the fill: 2 of 5 is still true mid-category.
    assert seen[-1] == ("progress", 2, 5, "Scanning browsers…")
    assert seen[0] == ("stop", "Starting scan…")
    dlg._set_scan_step(0, 0)
    dlg._set_scan_status("Sizing installed games…")
    assert seen[-1] == ("stop", "Sizing installed games…")


# --------------------------------------------------------------------------- #
# TL-007 — admin-gated leftovers are named before deleting
# --------------------------------------------------------------------------- #

def test_protected_leftovers_are_counted_before_delete(monkeypatch):
    import os

    from app import elevation
    from app.ui.dialogs.uninstall_programs_dialog import _admin_gated

    monkeypatch.setattr(elevation, "is_admin", lambda: False)
    monkeypatch.setenv("ProgramFiles", r"C:\Program Files")
    monkeypatch.setenv("ProgramFiles(x86)", r"C:\Program Files (x86)")
    monkeypatch.setenv("SystemRoot", r"C:\Windows")
    monkeypatch.setenv("ProgramData", r"C:\ProgramData")
    user = os.path.join(os.environ.get("LOCALAPPDATA", r"C:\Users\x\AppData\Local"),
                        "SomeApp")

    paths = [
        r"C:\Program Files\Foo",                 # gated
        r"C:\Program Files (x86)\Bar\cache",     # gated (sub-path)
        r"C:\Windows\Temp\Baz",                  # gated
        r"C:\ProgramData\Qux",                   # gated
        user,                                    # not gated
        "REGKEY::HKLM::Software\\Foo",           # gated
        "REGKEY::HKCU::Software\\Foo",           # not gated
        "STARTUP::Foo",                          # not gated
        "SHORTCUT::C:\\Users\\x\\Desktop\\a.lnk",  # not gated
    ]
    assert _admin_gated(paths) == 5


def test_elevated_or_unknown_runs_claim_nothing(monkeypatch):
    from app import elevation
    from app.ui.dialogs.uninstall_programs_dialog import _admin_gated

    monkeypatch.setattr(elevation, "is_admin", lambda: True)
    assert _admin_gated([r"C:\Program Files\Foo"]) == 0

    def _boom():
        raise RuntimeError("no shell")

    monkeypatch.setattr(elevation, "is_admin", _boom)
    assert _admin_gated([r"C:\Program Files\Foo"]) == 0
