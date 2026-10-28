"""Smoke test: every dialog the app can open actually opens.

Two failures motivated this file, and it catches both:

  * 2026-10: all 19 Tools rows were wired with ``on_click=lambda k=key: ...``,
    so Tk handed the EVENT to ``_mount`` in place of the tool key. Every row
    did nothing in the app while the whole suite stayed green, because the
    tests called ``_mount(key)`` directly. The first test below drives each row
    the way Tk drives it and follows the chain all the way to a registered
    dialog key, so a broken link anywhere in it fails here.
  * a dialog that raises while it builds. ``open_dialog`` logs that and shows
    "Could not open that tool", which is honest but is still a dead tool for
    the user. The second test builds EVERY dialog in the registry off-screen,
    not only the 19 the directory lists.

THE REGISTRY, ACCOUNTED FOR (2026-10). The Tools directory opens 19 of the 21
registered dialogs. The other two are in ``_SECONDARY``, with where a user
reaches them instead of a Tools row:

    app_update      the status row's Update available banner
    update_picker   Health -> the Security card's "Open Updates" action
                    (CARD_ACTION -> _CARE_ROUTE -> routes "updates")

That is the whole registry: the reachability scan this file runs below once
listed nine keys nothing reached -- the Advisor, What Changed and the seven
peripheral testers (pad, keyboard, mouse, display, camera, mic, speakers) --
and those windows, their entry points, route rows and registry keys were
deleted rather than left as dead weight. The third test keeps the count honest
when a dialog is added, and the fourth keeps the nine removed.

Deliberately NOT asserted: what any dialog DOES once it is up. These dialogs
scan disk, shell out to PowerShell and start network probes, and none of them
touches a device from its constructor (capture and playback start on a button).
Cost: ~3 s for all 21, measured here.
"""
from __future__ import annotations

import inspect
import re
import threading

import pytest

tk = pytest.importorskip("tkinter")

from _tkhelpers import make_root  # noqa: E402

#: Registry keys no Tools row opens: (dialog key, where a user still gets to
#: it). The reason is required - a key with nothing behind it is a bug in the
#: app, not a row in a table (that is what the removal below was for).
_SECONDARY = (
    ("app_update", "the status row's Update available banner"),
    ("update_picker", "Health -> the Security card's Open Updates action"),
    ("health_history", "the Health tab's trend row"),
)

#: The windows that came out in 2026-10 with their entry points, route rows
#: and registry keys, because nothing in the app reached any of them.
_REMOVED = (
    "advisor", "timeline", "gamepad_tester", "keyboard_tester", "mic_check",
    "monitor_test", "mouse_tester", "speaker_test", "webcam_test",
)


def _no_wait(self):
    """Stand in for ThemedModal.wait.

    ``wait()`` enters a NESTED event loop that only a user can leave, and the
    house pattern is that it is split out of ``__init__`` so a harness can
    assert on the built dialog without blocking (see ThemedModal.wait and the
    Scorecard / Storage Insight overrides). Two of those classes override it,
    which is why every registered class is patched rather than the base alone.
    """
    return None


class _ClickEvent:
    """What Tk hands a <Button-1> binding: an event object, nothing else."""

    x = 5
    y = 5
    num = 1


class _StandInApp:
    """Just enough Application for a dialog to build against.

    ``_open_*`` records which entry point a row reached; the private hooks a
    dialog may probe for (a toast, a status flash, a run handoff) answer as
    no-ops rather than raising, because failing the smoke test for a hook the
    real Application does have would be a false alarm.

    DATA attributes a dialog READS are declared here explicitly, because
    ``__getattr__`` never runs for a name that exists and never falls back for
    one that does not: ``getattr(app, "_update_info", None)`` would hand the
    dialog a fabricated function, and the dialog would fail inside itself,
    pointing the traceback at the wrong place. They carry a plausible payload
    so the populated state is what gets built.
    """

    #: App Update's banner payload (see Application._show_update_banner).
    _update_info = {"latest_version": "9.9.9", "current_version": "9.0.0",
                    "release_notes": "Smoke-test payload."}

    def __init__(self, root):
        self.root = root
        self._busy = False
        self._busy_lock = threading.Lock()
        self.opened = []
        self.notes = []

    def log(self, message):
        self.notes.append(str(message))

    def set_status(self, message):
        self.notes.append(str(message))

    def run_tasks(self, *_a, **_k):
        return None

    def __getattr__(self, name):
        if name.startswith("_open_"):
            def _recorded(*_a, **_k):
                self.opened.append(name)
            return _recorded
        if name.startswith("_"):
            return lambda *_a, **_k: None
        raise AttributeError(name)

    def complaint(self):
        """The most useful thing open_dialog said about the last attempt."""
        for note in reversed(self.notes):
            if "Could not open" in note:
                return note.strip().splitlines()[0]
        return self.notes[-1].strip().splitlines()[0] if self.notes else ""


@pytest.fixture
def off_screen(monkeypatch):
    """A hidden-but-mapped root and the stand-in Application on it.

    Off-screen rather than withdrawn (the house pattern for popup tests): a
    withdrawn root parks every dialog at 0,0, which is 30 windows flashing on
    the developer's desktop one after another.
    """
    from app.ui.dialogs.registry import DIALOGS

    for spec in DIALOGS.values():
        monkeypatch.setattr(spec.cls, "wait", _no_wait)
    root = make_root()
    root.geometry("520x240+3000+3000")
    root.update()
    try:
        yield root, _StandInApp(root)
    finally:
        root.destroy()


def _assert_opens(root, app, dialog_key, where):
    """Build `dialog_key` off-screen. Returns a complaint, or None if it came up.

    Three things have to hold, and each has bitten this app before: the dialog
    builds and its window exists, closing it does not raise, and it leaves the
    open-modal registry (a phantom entry there stalls every background chain
    that polls it).
    """
    from app.ui.base import ThemedModal
    from app.ui.dialogs.registry import open_dialog

    app.notes.clear()
    dialog = open_dialog(app, dialog_key)
    root.update_idletasks()
    window = getattr(dialog, "_dlg", None)
    if dialog is None or window is None or not window.winfo_exists():
        return "%s: %s did not open -- %s" % (where, dialog_key,
                                              app.complaint() or "no message")
    try:
        dialog.close()
    except Exception as exc:
        return "%s: %s raised while closing (%r)" % (where, dialog_key, exc)
    if id(dialog) in ThemedModal._MODAL_OPEN:
        return ("%s: %s stayed in the open-modal registry after close"
                % (where, dialog_key))
    root.update()
    return None


# --------------------------------------------------------------- source maps
def _entry_point_to_dialog_key():
    """Application entry point -> the dialog-registry key it opens."""
    from app.ui.app import Application

    src = inspect.getsource(Application)
    out = {}
    for m in re.finditer(r"\n    def (_open_[A-Za-z0-9_]+)\(self\):(.*?)"
                         r"(?=\n    def )", src, re.S):
        hit = re.search(r'open_dialog\(self, "([a-z_0-9]+)"\)', m.group(2))
        if hit:
            out[m.group(1)] = hit.group(1)
    return out


def _directory_rows():
    """Tools row key -> Application entry point, read from _mount's chain."""
    from app.ui.tabs.toolstab import ToolsTab

    return dict(re.findall(
        r'if key == "([a-z_]+)":\s*\n\s*self\.app\.(_open_[A-Za-z0-9_]+)\(\)',
        inspect.getsource(ToolsTab._mount)))


def _directory_dialog_keys():
    mapping = _entry_point_to_dialog_key()
    return {mapping[m] for m in _directory_rows().values() if m in mapping}


# --------------------------------------------------------------------- tests
def test_every_tools_row_reaches_the_dialog_it_names(off_screen):
    """The click contract, end to end, the way Tk actually drives a row.

    The 2026-10 rewrite wired the rows with ``on_click=lambda k=key: ...``, so
    Tk passed the EVENT as ``k``, every branch in ``_mount`` missed, and all 19
    rows did nothing at all. A static test cannot see that: it reads _mount's
    source and calls it with a key. This clicks instead - one event object for
    the mouse, None for keyboard activation, with the tool key bound by keyword
    so neither can overwrite it - and then follows the chain to a real dialog.
    """
    from app.ui.dialogs.registry import DIALOGS
    from app.ui.tabs.toolstab import ToolsTab

    root, app = off_screen
    mapping = _entry_point_to_dialog_key()
    failures = []
    reached = []
    tab = ToolsTab(root, app)
    for key, card in tab._cards.items():
        app.opened.clear()
        card._on_click(_ClickEvent())
        if len(app.opened) != 1:
            failures.append("%s: a click reached %d entry points, expected 1"
                            % (key, len(app.opened)))
            continue
        mouse = app.opened[0]
        dialog_key = mapping.get(mouse)
        if dialog_key is None or dialog_key not in DIALOGS:
            failures.append("%s: %s opens no registered dialog (%r)"
                            % (key, mouse, dialog_key))
            continue
        reached.append(dialog_key)
        app.opened.clear()
        card.activate()
        if app.opened != [mouse]:
            failures.append("%s: keyboard and mouse disagree (%s vs %s)"
                            % (key, app.opened, mouse))
    assert not failures, "\n".join(failures)
    # Coverage, so this cannot silently shrink to nothing.
    assert len(reached) == len(ToolsTab._CARDS) == 19, reached
    assert len(set(reached)) == 19, reached


def test_every_registered_dialog_builds_off_screen(off_screen):
    """Every key in the registry, not just the 19 the directory lists."""
    from app.ui.dialogs.registry import DIALOGS

    root, app = off_screen
    failures = [c for c in (_assert_opens(root, app, key, "registry")
                            for key in sorted(DIALOGS)) if c]
    assert not failures, "\n".join(failures)
    assert len(DIALOGS) == 22, sorted(DIALOGS)


def test_every_registry_key_is_accounted_for():
    """A new dialog must be in the directory or explained in the table above.

    Without this, a dialog can be registered and forgotten: unreachable, and
    nothing says whether that was deliberate.
    """
    from app.ui.dialogs.registry import DIALOGS

    in_directory = _directory_dialog_keys()
    classified = {key for key, _where in _SECONDARY}
    assert len(in_directory) == 19, sorted(in_directory)
    assert not (in_directory & classified), sorted(in_directory & classified)
    missing = sorted(set(DIALOGS) - in_directory - classified)
    assert not missing, (
        "no Tools row opens these and _SECONDARY does not classify them: %s"
        % missing)
    stale = sorted(classified - set(DIALOGS))
    assert not stale, "_SECONDARY names dialogs that no longer exist: %s" % stale
    # Every key that is NOT in the directory must say where a user reaches it.
    # The 2026-10 sweep deleted every dialog that had no answer here.
    unexplained = sorted(key for key, where in _SECONDARY if not where)
    assert not unexplained, (
        "these still have no way in; either wire one up or delete the dialog "
        "(entry point, route row, registry key and module): %s" % unexplained)


def test_the_removed_windows_stay_removed():
    """The nine dead dialogs were deleted; this keeps them deleted.

    A guard rather than a memory, because piecing a removal back together
    piecemeal is a real failure mode here: a previous cleanup shipped the edits
    without the file deletions, and the stale reference was the only thing that
    broke. A resurrected registry key, entry point, route row or module has to
    be deliberate, and this is what asks for the reason.
    """
    import importlib.util

    from app.routes import ROUTES
    from app.ui.app import Application
    from app.ui.dialogs.registry import DIALOGS

    residue = []
    for key in _REMOVED:
        if key in DIALOGS:
            residue.append("registry key %r" % key)
    for name in ("advisor_dialog", "timeline_dialog", "gamepad_dialog",
                 "keyboard_tester_dialog", "mouse_tester_dialog",
                 "monitor_test_dialog", "webcam_dialog", "mic_check_dialog",
                 "speaker_test_dialog"):
        if importlib.util.find_spec("app.ui.dialogs." + name) is not None:
            residue.append("module app.ui.dialogs.%s" % name)
    for method in ("_open_advisor", "_open_timeline", "_open_gamepad_tester",
                   "_open_keyboard_tester", "_open_mouse_tester",
                   "_open_monitor_test", "_open_webcam_test",
                   "_open_mic_check", "_open_speaker_test"):
        if hasattr(Application, method):
            residue.append("entry point Application.%s" % method)
    for target in ("advisor", "timeline", "gamepad", "keyboard", "mouse",
                   "monitor", "webcam", "miccheck", "speaker"):
        if ROUTES.get(target) is not None:
            residue.append("route %r" % target)
    assert not residue, (
        "these came back without a documented reason -- either finish the "
        "removal or say here why the window is back: %s" % residue)
