"""Phase 1 of the 2026-10 Tools audit: the shared shell and the hub.

These are contract tests, not pixel tests. They pin the parts that would
otherwise rot silently:

  * every ToolPage declares its scope, in the same vocabulary, at chip size;
  * the strip really wraps instead of clipping at a narrow dialog;
  * the shared result components build and expose the handles callers use;
  * error copy maps the failures a Windows utility actually meets;
  * the hub covers every card exactly once and search filters for real;
  * recommendations come from measured facts and disappear when stale.
"""
from __future__ import annotations

import importlib
import inspect
import pkgutil
import time

import pytest

tk = pytest.importorskip("tkinter")

from _tkhelpers import make_root

from app import tool_hub
from app.ui import errors
from app.ui.tool_scope import TOOL_SCOPES, ToolScope, scope_for

#: Longest value a chip may carry and still read as one short fact.
#:
#: The strip ellipsizes by measured pixel width, so this is a REVIEW rule, not
#: something the app reads: `app/ui/tool_scope.py` used to export it and no
#: running code ever looked at it. It lives with the guard that enforces it so
#: the number cannot be mistaken for a rendering limit.
CHIP_VALUE_MAX = 30


# --------------------------------------------------------------------------- #
# TV-003 — the scope contract
# --------------------------------------------------------------------------- #
def _tool_pages():
    import app.ui.dialogs as package
    from app.ui.dialogs._tool_page import ToolPage

    found = {}
    for modinfo in pkgutil.iter_modules(package.__path__):
        if modinfo.name.startswith("_"):
            continue
        module = importlib.import_module("app.ui.dialogs." + modinfo.name)
        for name, cls in inspect.getmembers(module, inspect.isclass):
            if (issubclass(cls, ToolPage) and cls is not ToolPage
                    and cls.__module__ == module.__name__):
                found[name] = cls
    return found


def test_every_tool_page_declares_a_scope():
    pages = _tool_pages()
    # A floor, not an exact count: it exists so a scan that silently finds
    # nothing cannot pass. The inventory is 23 ToolPages after the 2026-10
    # removal of the nine unreachable windows (seven peripheral testers plus
    # the Advisor and What Changed); their scope rows went with them.
    assert len(pages) >= 20, "the dialog inventory changed unexpectedly"
    missing = sorted(set(pages) - set(TOOL_SCOPES))
    assert not missing, "no scope declaration for: %s" % ", ".join(missing)
    stale = sorted(set(TOOL_SCOPES) - set(pages))
    assert not stale, "scope entries for dialogs that no longer exist: %s" % (
        ", ".join(stale))


def test_scope_values_are_short_and_tone_is_derived():
    for name, scope in TOOL_SCOPES.items():
        chips = scope.chips()
        assert chips[0][0] == "Reads", name
        assert 2 <= len(chips) <= 3, name
        for verb, value, tone in chips:
            assert str(value).strip(), "%s: empty %s" % (name, verb)
            assert len(str(value)) <= CHIP_VALUE_MAX, (
                "%s: %s is %d chars (max %d)" % (name, verb, len(value),
                                                 CHIP_VALUE_MAX))
            assert tone in ("neutral", "good", "warn", "bad"), name
        read_only = str(scope.changes).strip().lower() == "nothing"
        assert not scope.destructive if read_only else True
        assert not scope.undoable or bool(scope.reversible), (
            "%s: undoable without a reversible slot" % name)
        if not scope.reversible:
            assert not scope.undoable and not scope.destructive, name


def test_read_only_tools_say_nothing_and_have_no_undo_chip():
    for name, scope in TOOL_SCOPES.items():
        if str(scope.changes).strip().lower() == "nothing":
            assert len(scope.chips()) == 2, name
            assert scope.chips()[1][1] == "Nothing", name
            assert scope.chips()[1][2] == "good", name


def test_scope_lookup_works_by_instance_and_name():
    scope = scope_for("SpecsDialog")
    assert isinstance(scope, ToolScope)
    assert scope_for(object()) is None


def test_scope_strip_wraps_instead_of_clipping():
    from app.ui.widgets.scope_strip import ScopeStrip

    scope = ToolScope(
        reads="Disk usage + caches",
        changes="Deletes the files you pick",
        reversible="Only with Quarantine",
        destructive=True)
    root = make_root()
    try:
        root.withdraw()
        strip = ScopeStrip(root, scope.chips())
        strip.pack(fill="x")
        root.update_idletasks()
        assert strip.rows() == 1
        assert strip.winfo_reqheight() >= 32
        strip._reflow(300)
        root.update_idletasks()
        assert strip.rows() >= 2
        assert strip.winfo_reqheight() >= 64
        shown = strip.texts()
        assert [v for _verb, v in shown] == [c[1] for c in scope.chips()]
        # A very narrow pass ellipsizes; widening must restore the value.
        strip._reflow(120)
        assert strip.texts()[0][1] != scope.chips()[0][1]
        strip._reflow(900)
        assert strip.texts()[0][1] == scope.chips()[0][1]
    finally:
        root.destroy()


def test_a_tool_page_mounts_the_scope_strip_and_keeps_the_prose():
    from app.ui.dialogs._tool_page import ToolPage

    def _init(self, parent):
        ToolPage.__init__(self, parent, title="Test", scope="Long prose.")

    Page = type("SpecsDialog", (ToolPage,), {"__init__": _init})
    root = make_root()
    try:
        root.withdraw()
        page = Page(root)
        assert page.tool_scope is not None
        assert page.scope_text == "Long prose."
        assert page.scope_strip is not None
        assert page.scope_strip.winfo_manager() == "pack"
        page.close()
    finally:
        root.destroy()


# --------------------------------------------------------------------------- #
# TV-004 — the shared result components
# --------------------------------------------------------------------------- #
def test_metric_strip_updates_one_cell_and_keeps_even_columns():
    from app.ui.theme import COLORS
    from app.ui.widgets.metric_strip import _TONE, MetricStrip

    root = make_root()
    try:
        root.withdraw()
        strip = MetricStrip(root, [("down", "Download", "Mbps"),
                                   ("up", "Upload", "Mbps")])
        strip.pack(fill="x")
        strip.set("down", "94.2")
        strip.set("up", "12.1", tone="warn", delta="\u25bc 3%",
                  delta_tone="bad")
        assert strip.value_label("down").cget("text") == "94.2"
        assert strip.value_label("down").cget("fg") == COLORS["text"]
        assert strip.value_label("up").cget("fg") == _TONE["warn"]
        assert strip._cells["up"].delta_lbl.cget("text") == "\u25bc 3%"
        assert strip._cells["down"].label_lbl.cget("text") == "Download"
        assert strip.keys() == ("down", "up")
    finally:
        root.destroy()


def test_finding_row_carries_severity_and_one_action():
    from app.ui.widgets.finding_row import SEVERITY, FindingRow

    root = make_root()
    try:
        root.withdraw()
        row = FindingRow(root, title="Disk space", evidence="C: is 91% full",
                         severity="bad", action_label="Open Storage",
                         on_action=lambda: None)
        row.pack(fill="x")
        root.update_idletasks()
        assert row.title_lbl.cget("text") == "Disk space"
        assert row.evidence_lbl.cget("text") == "C: is 91% full"
        assert row.action() is not None
        # Severity is a construction-time fact: a finding that changes grade is
        # rebuilt, so there is no setter to keep in step with this dot.
        assert row._dot_color == SEVERITY["bad"]
        row.set_action_enabled(False)
        assert row.action()._enabled is False
        row.set_action_enabled(True)
        assert row.action()._enabled is True
    finally:
        root.destroy()


# --------------------------------------------------------------------------- #
# TV-010 — one permission notice and one failure vocabulary
# --------------------------------------------------------------------------- #
def test_permission_notice_has_the_fixed_title_and_one_action():
    from app.ui.widgets.permission_notice import PermissionNotice

    calls = []
    root = make_root()
    try:
        root.withdraw()
        notice = PermissionNotice(root, reason="Some holders are hidden.",
                                  action_text="Scan as admin",
                                  on_action=lambda: calls.append(True))
        notice.pack(fill="x")
        root.update_idletasks()
        assert notice.title_lbl.cget("text") == "Needs administrator"
        assert notice.reason_lbl.cget("text") == "Some holders are hidden."
        assert notice.action() is not None
        notice.set_enabled(False)
        assert notice.action()._enabled is False
        # A-008: a refusal re-states the notice rather than repeating the
        # invitation, so the reason line has to follow the state.
        notice.set_refused(r"takeown /f C:\Windows\System32\drivers\etc\hosts")
        assert notice.reason_lbl.cget("text") != "Some holders are hidden."
        assert notice.action()._enabled is True
    finally:
        root.destroy()


@pytest.mark.parametrize("exc,words", [
    (PermissionError("nope"), ("administrator",)),
    (FileNotFoundError("gone"), ("no longer there",)),
    (TimeoutError("took too long"), ("too long",)),
    (OSError(11, "disk full"), ("disk space",)),
    (ConnectionRefusedError("refused"), ("network",)),
    (RuntimeError("odd failure"), ("something went wrong", "odd failure")),
])
def test_error_copy_names_the_situation(exc, words):
    text = errors.explain(exc)
    assert text.endswith(".")
    for word in words:
        assert word in text.lower()


def test_error_copy_truncates_an_unrecognised_wall_of_text():
    text = errors.explain(RuntimeError("x" * 900))
    assert len(text) <= errors._TEXT_MAX + 40
    assert "\u2026" in text


# --------------------------------------------------------------------------- #
# TV-001 — the hub
# --------------------------------------------------------------------------- #
class _StubApp:
    pass


def test_every_card_lives_in_exactly_one_group():
    from app.ui.tabs.toolstab import ToolsTab

    keys = [c[0] for c in ToolsTab._CARDS]
    grouped = [k for _t, members in ToolsTab._GROUPS for k in members]
    assert len(grouped) == len(set(grouped)), "a card is listed twice"
    assert set(grouped) == set(keys), "a card has no group (or a stale key)"
    titles = [t for t, _m in ToolsTab._GROUPS]
    assert len(set(titles)) == 4


def test_the_hub_is_a_compact_directory_with_no_search_or_advisor():
    from app.ui.tabs.toolstab import ToolsTab

    root = make_root()
    try:
        root.withdraw()
        tab = ToolsTab(root, _StubApp())
        for gone in ("_advisor_btn", "_search_var", "_pinned",
                     "_trend_card", "_card_status"):
            assert not hasattr(tab, gone), gone
        assert len(tab._cards) == len(ToolsTab._CARDS) == 19
    finally:
        root.destroy()


def test_every_section_uses_the_same_four_column_grid():
    """Every tile the same width AND the same height, in four columns.

    A three-card group must not stretch its cards wider than the eight-card
    group above it: `equal=True` keeps the requested column count even when a
    row is short, which is what makes the page read as one grid rather than
    as four card sizes.
    """
    from app.ui.tabs.toolstab import ToolsTab

    root = make_root()
    try:
        root.geometry("1240x860")
        root.update()
        tab = ToolsTab(root, _StubApp())
        tab.pack(fill="both", expand=True)
        for _ in range(6):
            root.update()
        widths = {card.winfo_width() for card in tab._cards.values()}
        heights = {card.winfo_height() for card in tab._cards.values()}
        assert widths
        assert len(widths) == 1, sorted(widths)
        assert len(heights) == 1, sorted(heights)
        for card in tab._cards.values():
            assert int(card.grid_info()["column"]) in (0, 1, 2, 3)
    finally:
        root.destroy()


def _card_bottom(tab):
    return max(card.winfo_rooty() + card.winfo_height()
               for card in tab._cards.values()) - tab.winfo_rooty()


def test_the_whole_directory_fits_the_page_band_at_100_percent():
    """The grid fits the height the shell actually gives a page.

    980x700 is the window minimum, but the page only sees the band under the
    app bar and above the status row. Measured through the real shell (a
    hidden Application at the minimum window) that band is 852x477 at 100%
    density, not 980x700. Testing against the WINDOW height is how a card
    grid that overflows the page can still pass.
    """
    from app.ui.tabs.toolstab import ToolsTab

    root = make_root()
    try:
        root.geometry("852x477")
        tab = ToolsTab(root, _StubApp())
        tab.pack(fill="both", expand=True)
        for _ in range(4):
            root.update()
        bottom = _card_bottom(tab)
        assert bottom <= tab.winfo_height(), (bottom, tab.winfo_height())
    finally:
        root.destroy()


def test_the_whole_directory_fits_the_page_band_at_the_125_floor():
    """The density the app actually opens at: a 1076x626 page band.

    Every font and every card mark grows with `tk scaling`, so the floor is a
    different layout, not a zoomed copy of the 100% one. 1076x626 is the band
    the shell gives a page at the scaled minimum window (1226x864) on the
    125% floor; measuring it is what keeps the default launch unclipped.
    """
    from app.ui.tabs.toolstab import ToolsTab

    root = make_root()
    # The point scale is a property of the DISPLAY in Tk, not of this interp:
    # leaving it at 1.667 makes every later test render at 125% inside a
    # 100%-sized window, and the next layout assertion fails for a reason
    # that has nothing to do with the code it is testing.
    original = root.tk.call("tk", "scaling")
    try:
        root.tk.call("tk", "scaling", 1.6667)
        root.geometry("1076x626")
        tab = ToolsTab(root, _StubApp())
        tab.pack(fill="both", expand=True)
        for _ in range(4):
            root.update()
        bottom = _card_bottom(tab)
        assert bottom <= tab.winfo_height(), (bottom, tab.winfo_height())
    finally:
        try:
            root.tk.call("tk", "scaling", original)
        except Exception:
            pass
        root.destroy()


def test_the_page_has_no_scroll_panel():
    from app.ui.tabs.toolstab import ToolsTab
    from app.ui.widgets import ScrollableRoundedPanel

    root = make_root()
    try:
        root.withdraw()
        tab = ToolsTab(root, _StubApp())

        def walk(widget):
            stack = [widget]
            while stack:
                cur = stack.pop()
                if isinstance(cur, ScrollableRoundedPanel):
                    return True
                stack.extend(cur.winfo_children())
            return False

        assert not walk(tab)
    finally:
        root.destroy()


def test_every_hub_card_reaches_an_app_entry_point():
    """_mount swallows exceptions; pin the wiring so a rename is not silent."""
    import re

    from app.ui.app import Application
    from app.ui.tabs.toolstab import ToolsTab

    names = re.findall(r"self\.app\.(_open_[A-Za-z0-9_]+)",
                       inspect.getsource(ToolsTab._mount))
    keys = [key for key, *_rest in ToolsTab._CARDS]
    assert len(names) == len(keys) == 19
    assert len(set(names)) == 19, "two cards share one destination"
    for name in names:
        assert hasattr(Application, name), name


class _ClickEvent:
    """What Tk hands a <Button-1> binding: an event object, nothing else."""

    x = 5
    y = 5
    num = 1


class _RecordingApp:
    """An Application stand-in that records which tool a row asked for."""

    def __init__(self):
        self.opened = []

    def log(self, *_a, **_k):
        return None

    def set_status(self, *_a, **_k):
        return None

    def __getattr__(self, name):
        if name.startswith("_open_"):
            def _hit(*_a, **_k):
                self.opened.append(name)
            return _hit
        raise AttributeError(name)


def _entry_point_of(name):
    """The registry key `Application.<name>` hands to open_dialog, or None."""
    import re

    from app.ui.app import Application

    src = inspect.getsource(Application)
    body = re.search(
        r"\n    def %s\(self\):(.*?)(?=\n    def )" % re.escape(name),
        src, re.S)
    if body is None:
        return None
    hit = re.search(r"open_dialog\(self, \"([a-z_0-9]+)\"\)", body.group(1))
    return hit.group(1) if hit else None


def test_every_tools_row_opens_when_clicked_the_way_tk_clicks():
    """A row click must reach its tool, on the mouse AND the keyboard path.

    The 2026-10 rewrite wired the rows with `on_click=lambda k=key: ...`, so
    Tk passed the EVENT as `k`, every branch in _mount missed, and all 19 rows
    did nothing at all. The static wiring test above cannot see that - it
    reads _mount's source and calls it with a key. These rows are driven the
    way the widget is really driven: one event object for the mouse, None for
    keyboard activation, with the tool key bound by keyword so neither can
    overwrite it.
    """
    from app.ui.tabs.toolstab import ToolsTab

    app = _RecordingApp()
    root = make_root()
    try:
        root.withdraw()
        tab = ToolsTab(root, app)
        for key, card in tab._cards.items():
            app.opened.clear()
            card._on_click(_ClickEvent())
            assert app.opened, "%s: a real click opened nothing" % key
            assert len(app.opened) == 1, (key, app.opened)
            mouse = app.opened[0]
            app.opened.clear()
            card.activate()
            assert app.opened == [mouse], (
                "%s: keyboard and mouse disagree (%s vs %s)"
                % (key, app.opened, mouse))
    finally:
        root.destroy()


def test_every_tools_row_entry_point_opens_a_real_dialog():
    """`_mount` swallowing exceptions is only safe if the chain exists.

    Two links can break without a sound: the entry point could have been
    renamed (covered above) or it could pass a key the dialog registry does
    not know, in which case open_dialog returns None and the click still does
    nothing. This pins the second link for the tools directory.
    """
    import re

    from app.ui.dialogs.registry import DIALOGS
    from app.ui.tabs.toolstab import ToolsTab

    names = re.findall(r"self\.app\.(_open_[A-Za-z0-9_]+)",
                       inspect.getsource(ToolsTab._mount))
    for name in names:
        key = _entry_point_of(name)
        assert key, "%s opens no registered dialog" % name
        assert key in DIALOGS, "%s asks for unknown dialog %r" % (name, key)


def test_recent_ledger_dedupes_and_is_capped(monkeypatch):
    state = {"tools_recent": ["dns", "storage"]}

    def fake_update(mutate):
        cfg = {"tools_recent": list(state["tools_recent"])}
        mutate(cfg)
        state["tools_recent"] = cfg["tools_recent"]
        return cfg

    monkeypatch.setattr("app.config_persist.update_config", fake_update)
    tool_hub.remember("storage")
    assert state["tools_recent"][0] == "storage"
    assert state["tools_recent"].count("storage") == 1
    for key in "abcdefghijk":
        tool_hub.remember(key, limit=4)
    assert len(state["tools_recent"]) <= 4


def test_recommendations_come_from_fresh_measured_findings(monkeypatch):
    now = time.time()
    monkeypatch.setattr(tool_hub, "_drive_recommendation", lambda: None)
    point = {"t": now - 3600, "s": 61, "g": "D",
             "f": {"disk": "D", "junk": "F", "startup": "C",
                   "security": "F", "windows": "F"}}
    monkeypatch.setattr("app.health_history.entries", lambda: [point])
    recs = tool_hub.recommendations(now=now)
    keys = [r[0] for r in recs]
    assert keys[0] == "storage", recs
    assert len(recs) <= tool_hub.RECOMMEND_MAX
    assert all("scored" in r[1].lower() for r in recs)
    # The worst storage finding (junk F) is the one named.
    assert "Reclaimable Junk" in recs[0][1]
    assert "F" in recs[0][1]
    # A/B findings are not advice.
    point["f"] = {"disk": "A", "junk": "B"}
    assert tool_hub.recommendations(now=now) == []


def test_a_stale_checkup_is_not_a_recommendation(monkeypatch):
    now = time.time()
    monkeypatch.setattr(tool_hub, "_drive_recommendation", lambda: None)
    monkeypatch.setattr("app.health_history.entries", lambda: [{
        "t": now - (tool_hub.FRESH_DAYS + 1) * 86400, "s": 40, "g": "F",
        "f": {"junk": "F"}}])
    assert tool_hub.recommendations(now=now) == []
