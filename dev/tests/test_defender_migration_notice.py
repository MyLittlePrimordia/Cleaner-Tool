"""SEC-004 migration notice: "ask once" is a promise about one config flag.

The Admin Gate offers to remove the whole-folder Defender exclusion an older
build applied, and the code states the deal for itself: "Ask once only, either
way. Re-asking on every launch for something the user declined would be
nagging, and this is a security setting they may legitimately want to keep."

So two things have to hold, and they fail differently:

* clicking Remove must mark the notice shown even when the removal FAILS (a
  machine with Defender off or a policy block must not turn into a nag);
* ignoring the notice -- clicking past it with Skip -- is the decline, and must
  not bring it back on the next launch.

The gate is built for real here because the notice is created in __init__ and
the flag is written by the button handlers; neither is visible in the source.
Defender, the removal call and the elevation relaunch are all stubbed, so
nothing here touches the real machine.
"""
from __future__ import annotations

import pytest

tk = pytest.importorskip("tkinter")

from dev.tests._tkhelpers import make_root


@pytest.fixture
def config(tmp_path, monkeypatch):
    """config.json in a temp dir, so the flag is read and written for real."""
    import app.config_persist as cp

    cfg = tmp_path / "config.json"
    monkeypatch.setattr(cp, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(cp, "CONFIG_FILE", cfg)
    monkeypatch.setattr(cp, "_config_cache", None, raising=False)
    monkeypatch.setattr(cp, "_config_mtime", None, raising=False)
    return cfg


def _config_value(key):
    from app.config_persist import load_config
    return load_config().get(key)


def _set(key, value):
    from app.config_persist import update_config

    def _mut(c):
        c[key] = value

    update_config(_mut)


@pytest.fixture
def stale(monkeypatch, tmp_path):
    """Defender still lists the exe's whole folder from an older build."""
    import app.elevation as el

    folder = str(tmp_path / "Apps")
    monkeypatch.setattr(el, "find_stale_folder_exclusion", lambda: folder)
    return folder


@pytest.fixture
def nothing_stale(monkeypatch):
    import app.elevation as el

    monkeypatch.setattr(el, "find_stale_folder_exclusion", lambda: "")


@pytest.fixture
def dialogs(monkeypatch):
    """Captured dialogs, so a message is asserted instead of clicked away.

    Both kinds, because the point of the pair is which one fires: a removal has
    one outcome, and the user must not have to infer it from silence. Without
    these stubs the real themed modal blocks the suite forever.
    """
    import app.ui.tabs.admingateframe as g

    calls = []

    def _capture(kind):
        return lambda parent, title, message, *a, **k: \
            calls.append((kind, title, message))

    monkeypatch.setattr(g, "_themed_showwarning", _capture("warning"))
    monkeypatch.setattr(g, "_themed_showinfo", _capture("info"))
    return calls


@pytest.fixture
def logged(monkeypatch):
    """Captured log_or_swallow calls (the import happens inside the handler)."""
    import app.utils as u

    calls = []
    monkeypatch.setattr(u, "log_or_swallow",
                        lambda ctx, msg, exc=None, **k:
                        calls.append((msg, exc)))
    return calls


@pytest.fixture
def removal(monkeypatch):
    """The removal call, stubbed. Returns (calls, state)."""
    import app.elevation as el

    calls = []
    state = {"result": (True, "Removed the exclusion")}

    def fake(path):
        calls.append(path)
        return state["result"]

    monkeypatch.setattr(el, "remove_path_exclusion", fake)
    return calls, state


def _build():
    """A real gate, packed into a real root."""
    from app.ui.tabs.admingateframe import AdminGateFrame

    root = make_root()
    gate = AdminGateFrame(root, lambda: None)
    for _ in range(4):
        root.update_idletasks()
        root.update()
    return root, gate


def _descendants(widget):
    stack = [widget]
    while stack:
        cur = stack.pop()
        yield cur
        stack.extend(cur.winfo_children())


def _notice(gate):
    """The migration notice's label, or None."""
    for w in _descendants(gate):
        if isinstance(w, tk.Label) and "An older version" in str(w.cget("text")):
            return w
    return None


def _button(gate, text):
    from app.ui.widgets import AnimatedButton

    for w in _descendants(gate):
        if isinstance(w, AnimatedButton) and w._text == text:
            return w
    return None


def _remove_button(gate):
    return _button(gate, "Remove the folder exclusion")


def test_the_gate_offers_the_folder_an_older_build_excluded(config, stale):
    root, gate = _build()
    try:
        assert _notice(gate) is not None
        assert stale in str(_notice(gate).cget("text")), "the notice names the folder"
        assert _remove_button(gate) is not None
    finally:
        root.destroy()


def test_clicking_remove_marks_it_shown_and_asks_for_that_exact_folder(
        config, stale, removal, dialogs):
    calls, _state = removal
    root, gate = _build()
    try:
        _remove_button(gate)._command()
        assert calls == [stale], "the folder it named is the folder it removes"
        assert [k for k, _t, _m in dialogs] == ["info"], \
            "a removal that worked says so"
        assert _config_value("defender_migration_notice_shown") is True
    finally:
        root.destroy()

    root, gate = _build()
    try:
        assert _notice(gate) is None, "asked once, never again"
    finally:
        root.destroy()


def test_a_failed_removal_still_marks_it_shown(config, stale, removal,
                                               dialogs):
    """Defender refused, the machine isn't elevated, or a policy blocks it.
    The notice has had its answer either way; repeating it every launch is the
    nag the code promises not to be. (`dialogs` stubs the modal this failure
    raises — without it this test blocks on a real one.)"""
    calls, state = removal
    state["result"] = (False, "Remove-MpPreference failed: access denied")
    root, gate = _build()
    try:
        _remove_button(gate)._command()
        assert calls == [stale]
        assert _config_value("defender_migration_notice_shown") is True
    finally:
        root.destroy()

    root, gate = _build()
    try:
        assert _notice(gate) is None
    finally:
        root.destroy()


def test_a_failed_removal_tells_the_user_the_folder_is_still_excluded(
        config, stale, removal, dialogs):
    """The result of remove_path_exclusion used to be dropped on the floor: no
    dialog, no log line, no visible change. A blocked removal therefore looked
    exactly like a successful one while the folder stayed unscanned."""
    _calls, state = removal
    state["result"] = (False, "Remove-MpPreference failed: access denied")
    root, gate = _build()
    try:
        _remove_button(gate)._command()
    finally:
        root.destroy()

    assert len(dialogs) == 1, "one click, one answer"
    kind, title, message = dialogs[0]
    assert (kind, title) == ("warning", "Exclusion Still In Place")
    assert stale in message, "it names the folder that is still excluded"
    assert "access denied" in message, "and the reason Windows gave"


def test_a_removal_that_needs_admin_says_how_to_get_it(config, stale, removal,
                                                       dialogs):
    """"not running as administrator" is the expected answer on an unelevated
    launch. Printing it raw would read as a bug; it is a next step."""
    _calls, state = removal
    state["result"] = (False, "not running as administrator")
    root, gate = _build()
    try:
        _remove_button(gate)._command()
    finally:
        root.destroy()

    _kind, _title, message = dialogs[0]
    assert "Restart as Admin" in message, "the next step, not just the failure"
    assert stale in message
    assert message.rstrip().endswith("Windows Security.")
    assert "Windows did not remove" not in message, \
        "the raw-failure branch does not also fire"


def test_a_failure_with_no_reason_still_says_something(config, stale, removal,
                                                       dialogs):
    """An empty message must not become a dialog ending in a blank line."""
    _calls, state = removal
    state["result"] = (False, "")
    root, gate = _build()
    try:
        _remove_button(gate)._command()
    finally:
        root.destroy()

    _kind, _title, message = dialogs[0]
    assert message.rstrip().endswith("failed without saying why.")


def test_a_successful_removal_confirms_it(config, stale, removal, dialogs):
    """Success used to be as silent as failure: the notice just stayed on
    screen, so the user could not tell a removal that worked from one that was
    blocked. It gets its own confirmation, of its own kind — not a warning."""
    _calls, state = removal
    state["result"] = (True, "Removed the exclusion for %s" % stale)
    root, gate = _build()
    try:
        _remove_button(gate)._command()
    finally:
        root.destroy()

    assert [k for k, _t, _m in dialogs] == ["info"], \
        "a removal that worked is not reported as a problem"
    _kind, title, message = dialogs[0]
    assert title == "Exclusion Removed"
    assert stale in message, "it names the folder that is scanned again"
    assert "scanned again" in message, "and says what that means"
    assert _config_value("defender_migration_notice_shown") is True


def test_the_confirmation_does_not_depend_on_the_returned_wording(
        config, stale, removal, dialogs):
    """(True, "") is still success. The message is built from the outcome, not
    from text that may change upstream."""
    _calls, state = removal
    state["result"] = (True, "")
    root, gate = _build()
    try:
        _remove_button(gate)._command()
    finally:
        root.destroy()

    assert [k for k, _t, _m in dialogs] == ["info"]
    assert stale in dialogs[0][2]


def test_a_successful_removal_takes_the_offer_down(config, stale, removal,
                                                   dialogs):
    """The confirmation said the exclusion was gone while the label under it
    still said nothing in that folder was scanned, and the button still invited
    a click that would run Remove-MpPreference again. Both belong to a question
    that has been answered, so both go."""
    calls, state = removal
    state["result"] = (True, "Removed the exclusion")
    root, gate = _build()
    try:
        assert _notice(gate) is not None
        _remove_button(gate)._command()
        assert _notice(gate) is None, "the answered notice is gone"
        assert _remove_button(gate) is None, "and so is its button"
        assert calls == [stale], "one click, one removal"
    finally:
        root.destroy()


def test_taking_the_offer_down_leaves_the_rest_of_the_gate_alone(
        config, stale, removal, dialogs):
    """Clearing the offer destroys two widgets, not the page: the way out of
    the gate (and the checkboxes) must survive it."""
    _calls, state = removal
    state["result"] = (True, "Removed the exclusion")
    root, gate = _build()
    try:
        _remove_button(gate)._command()
        assert _button(gate, "Restart as Admin") is not None
        assert _button(gate, "Skip") is not None
        assert gate._auto_elevate_var is not None, "the prefs are still there"
    finally:
        root.destroy()


def test_a_failed_removal_keeps_the_offer_for_another_try(config, stale,
                                                          removal, dialogs):
    """Nothing changed for the folder, so nothing changes on the gate either:
    the offer is still true and the user still needs it."""
    _calls, state = removal
    state["result"] = (False, "Remove-MpPreference failed: access denied")
    root, gate = _build()
    try:
        _remove_button(gate)._command()
        assert _notice(gate) is not None, "the folder is still excluded"
        assert _remove_button(gate) is not None, "so the offer still applies"
    finally:
        root.destroy()


def test_a_double_click_removes_the_exclusion_once(config, stale, monkeypatch,
                                                   dialogs):
    """AnimatedButton defers its command by 60 ms, so two releases inside that
    window queue two fires. The second must find the button disabled, or one
    decision runs Remove-MpPreference twice and confirms twice."""
    import app.elevation as el

    calls = []
    monkeypatch.setattr(el, "remove_path_exclusion",
                        lambda p: calls.append(p) or (True, "Removed"))
    root, gate = _build()
    try:
        button = _remove_button(gate)
        button._pressing = True
        button._fire()
        button._fire()          # the second queued release
        assert calls == [stale], "one click, one removal"
        assert [k for k, _t, _m in dialogs] == ["info"], "and one answer"
    finally:
        root.destroy()


def test_the_button_is_disabled_while_the_removal_runs(config, stale,
                                                       monkeypatch, dialogs):
    """The removal blocks the UI for a PowerShell call. The button has to be
    out of action for that whole stretch, not after it."""
    import app.elevation as el

    root, gate = _build()
    seen = []
    try:
        def slow(path):
            seen.append(gate._stale_remove_button._enabled)
            return True, "Removed"

        monkeypatch.setattr(el, "remove_path_exclusion", slow)
        _remove_button(gate)._fire()
        assert seen == [False], "already disabled when the work starts"
    finally:
        root.destroy()


def test_a_failed_removal_puts_the_button_back(config, stale, removal,
                                               dialogs):
    """The offer survives a failure, so its button has to as well: disabled
    and left disabled would strand the retry."""
    calls, state = removal
    state["result"] = (False, "Remove-MpPreference failed: access denied")
    root, gate = _build()
    try:
        button = _remove_button(gate)
        button._fire()
        assert button._enabled is True, "the retry is possible"
        button._fire()
        assert calls == [stale, stale], "and it really asks again"
    finally:
        root.destroy()


def test_an_offer_that_will_not_destroy_is_logged_not_raised(
        config, stale, removal, dialogs, logged):
    """A label that refuses to go is not worth losing the confirmation (or the
    gate) over, but it must not vanish silently either."""
    _calls, state = removal
    state["result"] = (True, "Removed the exclusion")
    root, gate = _build()
    label = gate._stale_notice_label
    real_destroy = label.destroy

    def boom(*_a, **_k):
        raise RuntimeError("widget is gone")

    label.destroy = boom
    try:
        _remove_button(gate)._command()          # must not raise
        assert gate._stale_notice_label is None, "the reference is dropped"
        assert _notice(gate) is label, "the stuck label is still on screen"
        assert _remove_button(gate) is None, "its button still went"
        assert [k for k, _t, _m in dialogs] == ["info"], "still confirmed"
    finally:
        label.destroy = real_destroy        # so the root can be torn down
        root.destroy()

    assert any("cleared" in m.lower() or "clear" in m.lower()
               for m, _exc in logged), "the failed destroy is recorded"


def test_a_removal_that_raises_is_reported_and_logged(config, stale, monkeypatch,
                                                      dialogs, logged):
    """This except used to swallow: the click did nothing at all, and nothing
    was written anywhere to explain it."""
    import app.elevation as el

    def boom(path):
        raise RuntimeError("powershell went away")

    monkeypatch.setattr(el, "remove_path_exclusion", boom)
    root, gate = _build()
    try:
        _remove_button(gate)._command()
    finally:
        root.destroy()

    assert [k for k, _t, _m in dialogs] == ["warning"], "a crash is not success"
    _kind, _title, message = dialogs[0]
    assert "RuntimeError" in message and "powershell went away" in message
    assert any("exclusion" in m.lower() for m, _exc in logged), \
        "the crash also leaves a trail for support"
    assert _config_value("defender_migration_notice_shown") is True, \
        "a crash is still an answer: no nagging on the next launch"


def test_a_dialog_that_cannot_open_still_leaves_a_trail(config, stale, removal,
                                                       monkeypatch, logged):
    """No display, a destroyed root: the dialog is the only channel left for
    this answer, so its failure must go to the log rather than nowhere."""
    import app.ui.tabs.admingateframe as g

    _calls, state = removal
    state["result"] = (False, "access denied")

    def no_display(*a, **k):
        raise RuntimeError("no display")

    monkeypatch.setattr(g, "_themed_showwarning", no_display)
    root, gate = _build()
    try:
        _remove_button(gate)._command()          # must not raise
    finally:
        root.destroy()

    assert any("notice" in m.lower() and isinstance(exc, RuntimeError)
               for m, exc in logged), "the failed dialog is recorded"


def test_a_confirmation_that_cannot_open_still_leaves_a_trail(
        config, stale, removal, monkeypatch, logged):
    """The success dialog travels the same guarded path as the failure one."""
    import app.ui.tabs.admingateframe as g

    _calls, state = removal
    state["result"] = (True, "Removed the exclusion")

    def no_display(*a, **k):
        raise RuntimeError("no display")

    monkeypatch.setattr(g, "_themed_showinfo", no_display)
    root, gate = _build()
    try:
        _remove_button(gate)._command()          # must not raise
    finally:
        root.destroy()

    assert any("notice" in m.lower() and isinstance(exc, RuntimeError)
               for m, exc in logged), "the failed dialog is recorded"


def test_skipping_past_the_notice_counts_as_declining_it(config, stale):
    """No "keep it" button exists, so ignoring the notice IS the decline: the
    user clicked Skip with the offer on screen. Bringing it back next launch is
    exactly the nagging the code says it avoids."""
    root, gate = _build()
    try:
        assert _notice(gate) is not None, "the offer was on screen"
        gate._continue_limited()
    finally:
        root.destroy()

    assert _config_value("defender_migration_notice_shown") is True
    root, gate = _build()
    try:
        assert _notice(gate) is None, "a declined notice is not shown again"
    finally:
        root.destroy()


def test_restarting_as_admin_also_counts_as_an_answer(config, stale,
                                                     monkeypatch):
    """The other button. Either way the user has moved past the gate, so the
    offer must not follow them into the next launch."""
    import app.ui.tabs.admingateframe as g

    monkeypatch.setattr(g, "relaunch_as_admin", lambda: False)
    monkeypatch.setattr(g, "_themed_showerror", lambda *a, **k: None)

    root, gate = _build()
    try:
        assert _notice(gate) is not None
        gate._restart_elevated()
    finally:
        root.destroy()

    assert _config_value("defender_migration_notice_shown") is True


def test_a_gate_with_nothing_to_offer_does_not_burn_the_notice(config,
                                                              nothing_stale):
    """The flag records that the notice was SEEN. A gate that never showed one
    must not set it, or a stale folder appearing later (the app copied into an
    old excluded folder) would never be mentioned."""
    root, gate = _build()
    try:
        assert _notice(gate) is None
        gate._continue_limited()
    finally:
        root.destroy()

    assert _config_value("defender_migration_notice_shown") is False


def test_with_the_preference_on_the_gate_does_not_even_look(config, monkeypatch,
                                                            tmp_path):
    """Nothing to migrate when the exclusion is opted into: no Defender read,
    no notice, and no flag written."""
    import app.elevation as el

    lookups = []
    monkeypatch.setattr(el, "find_stale_folder_exclusion",
                        lambda: lookups.append(1) or str(tmp_path / "Apps"))
    _set("add_defender_exclusion", True)

    root, gate = _build()
    try:
        assert _notice(gate) is None
        gate._continue_limited()
    finally:
        root.destroy()

    assert lookups == []
    assert _config_value("defender_migration_notice_shown") is False


def test_a_flag_already_set_skips_the_defender_read(config, monkeypatch,
                                                    tmp_path):
    import app.elevation as el

    lookups = []
    monkeypatch.setattr(el, "find_stale_folder_exclusion",
                        lambda: lookups.append(1) or str(tmp_path / "Apps"))
    _set("defender_migration_notice_shown", True)

    root, gate = _build()
    try:
        assert _notice(gate) is None
    finally:
        root.destroy()

    assert lookups == [], "asked once means never asking Defender again"
