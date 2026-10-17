"""Regression tests for Game Mode's "Add app" feature (user request 2026-10).

The report was "it doesn't have the option to add custom apps/programs to
the game mode": the close-while-playing list could only ever hold the six
apps from the reviewed CLOSEABLE_APPS table.

The feature is deliberately narrow, and that is what these tests pin:

  * the user picks an .exe in a file dialog, but only the process NAME is
    ever stored — never a path, never a command line;
  * a `custom:<name>` key resolves to a process name ONLY when the name is
    a plain exe basename, so a hand-edited config cannot smuggle a path or
    extra arguments into `taskkill` (which receives it as one argv element
    with shell=False);
  * the config schema agrees with that rule, because it also vets the file
    on load.

Side-effect free: pure string contracts plus source contracts. No taskkill
runs, no config is written, no GUI is built.
"""
from __future__ import annotations

import inspect

import pytest


def _src(module_name):
    import importlib

    return inspect.getsource(importlib.import_module(module_name))


def _func_src(module_name, func_name):
    src = _src(module_name)
    body = src.split("def %s(" % func_name, 1)
    assert len(body) == 2, "%s must exist in %s" % (func_name, module_name)
    return body[1].split("\ndef ", 1)[0]


# ------------------------------------------------------------- validation
@pytest.mark.parametrize("raw, want", [
    ("steam.exe", "steam.exe"),
    ("Steam.exe", "Steam.exe"),
    ("creative cloud.exe", "creative cloud.exe"),   # real Adobe helper name
    ("custom:steam.exe", "steam.exe"),              # stored key form
    ("  steam.exe  ", "steam.exe"),
    ("", None),
    (None, None),
    (123, None),
    ("noext", None),
    (".exe", None),
    ("../../evil.exe", None),
    ("..\\up.exe", None),
    ("C:\\Windows\\System32\\cmd.exe", None),       # drive colon + separators
    ("\\\\server\\share\\x.exe", None),
    ("evil.exe -rf /", None),                       # arguments/switch tail
    ("steam.exe\nother.exe", None),                 # control character
    ('"steam.exe"', None),
    ("steam|.exe", None),
    ("a" * 120 + ".exe", None),                     # length cap
])
def test_custom_exe_name_accepts_only_plain_process_names(raw, want):
    from app.game_night import custom_exe_name

    assert custom_exe_name(raw) == want


def test_custom_key_is_stable_and_fail_closed():
    from app.game_night import custom_key

    assert custom_key("Steam.exe") == "custom:steam.exe"
    assert custom_key("Creative Cloud.exe") == "custom:creative cloud.exe"
    assert custom_key("../evil.exe") is None
    assert custom_key(None) is None


def test_custom_label_reads_as_a_name():
    from app.game_night import custom_label

    assert custom_label("steam.exe") == "Steam"
    assert custom_label("obs64.exe") == "Obs64"
    assert custom_label("creative cloud.exe") == "Creative cloud"


def test_clean_custom_apps_dedupes_and_drops_junk():
    from app.game_night import clean_custom_apps

    assert clean_custom_apps(
        ["A.EXE", "a.exe", "..\\x.exe", "b.exe", 5, None]
    ) == ["a.exe", "b.exe"]
    assert clean_custom_apps(None) == []
    assert len(clean_custom_apps(
        ["p%d.exe" % i for i in range(80)])) == 25


# --------------------------------------------------------- close plumbing
def test_exe_names_for_resolves_custom_keys_to_process_names():
    from app.game_night import custom_key, exe_names_for

    assert exe_names_for([custom_key("Steam.exe")]) == ("steam.exe",)
    assert exe_names_for(["custom:obs64.exe"]) == ("obs64.exe",)


def test_exe_names_for_ignores_malformed_custom_keys():
    from app.game_night import exe_names_for

    assert exe_names_for(["custom:..\\evil.exe"]) == ()
    assert exe_names_for(["custom:C:\\Windows\\cmd.exe"]) == ()
    assert exe_names_for(["nonsense"]) == ()


def test_exe_names_for_never_double_lists_a_process():
    from app.game_night import exe_names_for

    names = exe_names_for(["browsers", "custom:chrome.exe"])
    assert names.count("chrome.exe") == 1
    assert "msedge.exe" in names


def test_close_apps_acts_on_custom_keys_too():
    """Taskkill is not run here: the contract is that the close path routes
    custom keys through the same validator, not through a second parser."""
    body = _func_src("app.game_night", "close_apps")
    assert "custom_exe_name(key)" in body
    assert "custom_label(" in body


# --------------------------------------------------------------- config
def test_schema_coercer_drops_anything_but_a_plain_exe_name():
    from app.config_schema import SCHEMA

    coerce = SCHEMA["game_night_custom_apps"][0]
    assert coerce(["Steam.exe", "..\\x.exe", "ok.exe", 5], []) == \
        ["steam.exe", "ok.exe"]
    # a non-list value falls back to the default, the way the other list
    # coercers do, and still comes back sanitised
    assert coerce("notalist", []) == []
    assert coerce(["C:\\evil\\x.exe"], []) == []


def test_default_config_has_the_custom_apps_key():
    from app.config_persist import DEFAULT_CONFIG

    assert DEFAULT_CONFIG["game_night_custom_apps"] == []


def test_config_setter_sanitises_before_writing():
    body = _func_src("app.config_persist", "set_game_night_custom_apps")
    assert "clean_custom_apps" in body, (
        "the setter must vet names, so what lands in the config is exactly "
        "what the close path may act on")


def test_get_game_night_reports_custom_apps():
    body = _func_src("app.config_persist", "get_game_night")
    assert "custom_apps" in body


# ------------------------------------------------------------------ UI
def test_pilot_dialog_wires_add_app_to_the_close_list():
    src = _src("app.ui.dialogs.pilot_dialog")
    assert 'text="Add app"' in src, "the Add app button went missing"
    assert "_add_custom_app" in src and "_remove_custom_app" in src
    assert "_rebuild_close_rows" in src, (
        "adding or removing an app must repaint the whole list")
    assert "set_game_night_custom_apps" in src
    assert "custom_key" in src and "custom_exe_name" in src


def test_pilot_add_app_keeps_the_name_not_the_path():
    body = _func_src("app.ui.dialogs.pilot_dialog", "_add_custom_app")
    assert "os.path.basename(path)" in body, (
        "only the file's name may travel on; the picked path must not be "
        "stored anywhere")
    assert "session_pilot_paths" not in body, (
        "custom close apps are not watchlist paths")
