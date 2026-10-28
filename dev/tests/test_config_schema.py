"""The config schema's own contract, as a test.

`app/config_schema.py` states the rule it exists to enforce: every key
`DEFAULT_CONFIG` declares has a coercer, and every coercer has a default —
otherwise a key is "defaulted but never validated", which is how a hand-edited
value (a truthy `"yes"` in a flag) survives a load. Nothing ran
`validate_schema()`, so the rule was only documentation.

This file is the gate, plus a worked case for the key that was the last
violation: `quarantine_enabled` had a default and no validator.

validate_schema() answers "does this key HAVE a coercer". The round trip
answers the question it cannot: "does that coercer accept the default it is
paired with" — a coercer that rewrites its own default is just as invisible.
The third property is totality, and it is the one with teeth: a coercer that
RAISES does not lose one field, it makes the loader quarantine the whole file.
Every coercer is therefore fuzzed with hostile values, and two of them turned
out to raise on numbers json.load() will happily produce.
"""
from __future__ import annotations

import json

import pytest

from app.config_persist import DEFAULT_CONFIG
from app.config_schema import (
    _MAX_SEEN_TIPS,
    _MAX_SNAPSHOT_DEPTH,
    _MAX_SNAPSHOT_NODES,
    _MAX_SNAPSHOT_SPECS,
    _MAX_SNAPSHOT_TASKS,
    DEFENDER_EXCLUSION_PATH_MAX,
    DEFENDER_EXCLUSIONS_MAX,
    DRIVERS_FOLDER_MAX,
    GAME_NIGHT_APP_NAME_MAX,
    GAME_NIGHT_APPS_MAX,
    GAMEPING_MAX,
    HEALTH_FINDINGS_MAX,
    HEALTH_HISTORY_MAX,
    PROFILE_ITEMS_MAX,
    PROFILE_MAX,
    PROFILE_NAME_MAX,
    PROTECTED_PATHS_MAX,
    RUN_HISTORY_MAX,
    SCHEMA,
    SESSION_KEYS_MAX,
    STARTUP_COMMAND_MAX,
    STARTUP_MAX,
    STARTUP_NAME_MAX,
    apply_schema,
    validate_schema,
)


def test_schema_and_defaults_agree_on_every_key():
    """No key may be defaulted without validation, or validated without a
    default. validate_schema() reports exactly those two kinds of drift."""
    assert validate_schema(DEFAULT_CONFIG) == []


def test_the_two_declarations_cover_the_same_keys():
    assert set(SCHEMA) <= set(DEFAULT_CONFIG)
    assert set(DEFAULT_CONFIG) <= set(SCHEMA)


def test_every_default_survives_a_save_and_load(tmp_path, monkeypatch):
    """The other half of the contract: a coercer must accept the default it
    is paired with.

    validate_schema() only proves that a key HAS a coercer. A coercer can
    still reject its own default — a `choices` tuple missing the default, a
    cap shorter than the default list, a `lower=True` rule against a
    capitalised default — and nothing notices, because the failure is silent:
    apply_schema() returns what it was told to return, the freshly written
    file is read back as something else, and the app simply starts with a
    different value than the one declared in DEFAULT_CONFIG. So write the
    defaults out, read them back from disk, and compare.
    """
    import app.config_persist as cp

    cfg = tmp_path / "config.json"
    monkeypatch.setattr(cp, "CONFIG_DIR", tmp_path)   # temp + lock files too
    monkeypatch.setattr(cp, "CONFIG_FILE", cfg)
    monkeypatch.setattr(cp, "_config_cache", None, raising=False)
    monkeypatch.setattr(cp, "_config_mtime", None, raising=False)

    cp.save_config(dict(DEFAULT_CONFIG))

    # Drop the cache save_config just filled, or the read below answers from
    # memory and the schema never runs.
    monkeypatch.setattr(cp, "_config_cache", None, raising=False)
    monkeypatch.setattr(cp, "_config_mtime", None, raising=False)
    loaded = cp.load_config()

    assert json.loads(cfg.read_text(encoding="utf-8")) == DEFAULT_CONFIG, (
        "save_config must write the defaults verbatim")
    mangled = {k: (DEFAULT_CONFIG[k], loaded.get(k, "<dropped>"))
               for k in DEFAULT_CONFIG if loaded.get(k) != DEFAULT_CONFIG[k]}
    assert mangled == {}, (
        f"these coercers do not accept their own default: {mangled!r}")


@pytest.mark.parametrize("stored,want", [
    (True, True),
    (False, False),
    ("yes", False),        # a truthy string is not a bool, so the default wins
    ("true", False),
    (1, False),            # isinstance(1, bool) is False, on purpose
    (0, False),
    (None, False),
])
def test_quarantine_enabled_must_be_a_real_bool(stored, want):
    data = apply_schema({"quarantine_enabled": stored}, DEFAULT_CONFIG)
    assert data["quarantine_enabled"] is want


def test_a_hand_edited_flag_reaches_the_quarantine_check_as_off(tmp_path,
                                                              monkeypatch):
    """The consequence, end to end through config.json.

    `quarantine.enabled()` trusted `bool(config["quarantine_enabled"])`, so a
    hand-edited "yes" turned the bin ON while the Settings toggle — which
    compares against `False` — showed it off. The coercer now makes the value
    a real bool before anything reads it.
    """
    import app.config_persist as cp
    from app import quarantine

    cfg = tmp_path / "config.json"
    cfg.write_text('{"quarantine_enabled": "yes"}', encoding="utf-8")
    monkeypatch.setattr(cp, "CONFIG_FILE", cfg)
    monkeypatch.setattr(cp, "_config_cache", None, raising=False)
    monkeypatch.setattr(cp, "_config_mtime", None, raising=False)

    assert cp.load_config()["quarantine_enabled"] is False
    assert quarantine.enabled() is False

    # and a real True still survives the round trip
    cfg.write_text('{"quarantine_enabled": true}', encoding="utf-8")
    monkeypatch.setattr(cp, "_config_cache", None, raising=False)
    monkeypatch.setattr(cp, "_config_mtime", None, raising=False)
    assert quarantine.enabled() is True


# --------------------------------------------------------------------------- #
# Bounds. Every cap is declared next to a comment saying why it exists, and a
# cap that is declared but not enforced reads as protection that is not there.
# A hand-edited config.json is the input these exist for, so each one is fed a
# value well past its limit and checked to land exactly on it — while a value
# already inside the limit must pass through untouched.
# --------------------------------------------------------------------------- #

def _coerced(key, value):
    """The value this key ends up with, through the schema."""
    return apply_schema({key: value}, DEFAULT_CONFIG)[key]


@pytest.mark.parametrize("key,cap", [
    ("gameping_hosts", GAMEPING_MAX),
    ("game_night_keys", SESSION_KEYS_MAX),
    ("game_night_close_apps", SESSION_KEYS_MAX),
    ("duplicate_protected_paths", PROTECTED_PATHS_MAX),
    ("defender_exclusions_applied", DEFENDER_EXCLUSIONS_MAX),
])
def test_a_plain_list_is_cut_to_its_cap_and_keeps_the_head(key, cap):
    values = [f"item {i}" for i in range(cap * 3)]
    got = _coerced(key, values)
    assert len(got) == cap
    assert got == values[:cap], "the head is what the user added first"


@pytest.mark.parametrize("key,cap", [
    ("gameping_hosts", GAMEPING_MAX),
    ("game_night_keys", SESSION_KEYS_MAX),
    ("game_night_close_apps", SESSION_KEYS_MAX),
    ("duplicate_protected_paths", PROTECTED_PATHS_MAX),
    ("defender_exclusions_applied", DEFENDER_EXCLUSIONS_MAX),
])
def test_a_list_at_its_cap_is_left_alone(key, cap):
    """A cap is a ceiling, not a target: nothing inside it may be dropped."""
    values = [f"item {i}" for i in range(cap)]
    assert _coerced(key, values) == values


def test_health_history_keeps_the_newest_points_and_drops_the_oldest():
    """A trend ledger is a rolling log too: trim the OLD end."""
    values = [{"t": float(i + 1), "s": 50, "g": "C"}
              for i in range(HEALTH_HISTORY_MAX * 2)]
    got = _coerced("health_history", values)
    assert len(got) == HEALTH_HISTORY_MAX
    assert got[-1] == values[-1]
    assert got[0] == values[HEALTH_HISTORY_MAX]


def test_health_history_keeps_a_clean_bounded_findings_map():
    """`f` is an annotation: a bad map loses the map, never the score."""
    values = [{"t": 1.0, "s": 80, "g": "B",
               "f": {"smart": "b", "disk": "?", "": "A", "boot": 5}},
              {"t": 2.0, "s": 70, "g": "C", "f": "not a map"},
              {"t": 3.0, "s": 60, "g": "D",
               "f": {"k%d" % i: "C"
                     for i in range(HEALTH_FINDINGS_MAX + 5)}}]
    got = _coerced("health_history", values)
    assert len(got) == 3                       # all three scores survive
    assert got[0]["f"] == {"smart": "B"}       # junk keys/grades dropped
    assert "f" not in got[1]                    # a non-map loses only itself
    assert len(got[2]["f"]) == HEALTH_FINDINGS_MAX


def test_run_history_keeps_the_newest_and_drops_the_oldest():
    """This is a rolling log, so the cap trims the OLD end — what a user
    freed last week is less interesting than what they freed yesterday."""
    entries = [{"t": i + 1, "b": i} for i in range(RUN_HISTORY_MAX + 50)]
    got = _coerced("run_history", entries)
    assert len(got) == RUN_HISTORY_MAX
    assert got == entries[-RUN_HISTORY_MAX:]
    assert got[-1] == {"t": RUN_HISTORY_MAX + 50, "b": RUN_HISTORY_MAX + 49}


def test_install_profiles_are_bounded_in_count_items_and_name():
    many = {f"profile {i}": ["pkg"] for i in range(PROFILE_MAX * 2)}
    assert len(_coerced("install_profiles", many)) == PROFILE_MAX

    wide = {"P": [f"pkg{i}" for i in range(PROFILE_ITEMS_MAX * 2)]}
    assert len(_coerced("install_profiles", wide)["P"]) == PROFILE_ITEMS_MAX

    # the name is the dict key, so it is the one field a hand edit can make
    # arbitrarily long without changing the shape
    long_name = "n" * (PROFILE_NAME_MAX * 3)
    assert [len(k) for k in _coerced("install_profiles", {long_name: ["p"]})] \
        == [PROFILE_NAME_MAX]


def test_startup_disabled_records_are_bounded():
    rec = {"source": "HKCU Run", "name": "n", "command": "c"}
    assert len(_coerced("startup_disabled", [rec] * (STARTUP_MAX * 2))) \
        == STARTUP_MAX

    long = _coerced("startup_disabled", [{
        "source": "HKCU Run", "name": "n" * (STARTUP_NAME_MAX * 3),
        "command": "c" * (STARTUP_COMMAND_MAX * 3)}])[0]
    assert len(long["name"]) == STARTUP_NAME_MAX
    assert len(long["command"]) == STARTUP_COMMAND_MAX


def test_seen_tips_is_bounded_and_keeps_only_real_flags():
    many = {f"tip {i}": True for i in range(_MAX_SEEN_TIPS * 4)}
    got = _coerced("seen_tips", many)
    assert len(got) == _MAX_SEEN_TIPS
    assert all(v is True for v in got.values())
    assert _coerced("seen_tips", {"a": "yes", "b": True, 7: True}) \
        == {"b": True}


def test_tweak_snapshots_are_bounded_in_tasks_and_specs():
    snap = {"specs": [["HKCU", "P", "V"]]}
    many = {f"task {i}": dict(snap) for i in range(_MAX_SNAPSHOT_TASKS * 2)}
    assert len(_coerced("tweak_snapshots", many)) == _MAX_SNAPSHOT_TASKS

    one = {"task": {"specs": [
        ["HKCU", f"P{i}", "V"] for i in range(_MAX_SNAPSHOT_SPECS * 2)]}}
    assert len(_coerced("tweak_snapshots", one)["task"]["specs"]) \
        == _MAX_SNAPSHOT_SPECS


def test_a_freeform_snapshot_is_bounded_in_depth_and_nodes():
    """The powercfg/NVIDIA/telemetry shapes have no `specs`, so they go through
    _jsonish with a depth limit and a shared node budget instead."""
    deep = {"leaf": 1}
    for i in range(50):
        deep = {f"k{i}": deep}
    kept = _coerced("tweak_snapshots", {"task": deep})["task"]
    levels, node = 0, kept
    while isinstance(node, dict) and node:
        levels += 1
        node = next(iter(node.values()))
    # the snapshot itself counts as depth 1, so one fewer key survives than
    # the constant, and the level it refuses collapses to {} rather than
    # recursing or dropping the whole snapshot
    assert levels == _MAX_SNAPSHOT_DEPTH - 1
    assert node == {}

    wide = {"task": {f"k{i}": i for i in range(_MAX_SNAPSHOT_NODES * 10)}}
    kept = _coerced("tweak_snapshots", wide)["task"]
    assert len(kept) == _MAX_SNAPSHOT_NODES - 1     # one node for the snapshot


def test_defender_exclusions_drop_long_entries_instead_of_cutting_them():
    """SEC-004's record is joined into ONE Remove-MpPreference command, so an
    entry's length matters as much as how many there are. Cutting a path to fit
    would invent a path nobody chose; dropping it only leaves an exclusion
    behind, which is the failure the section already tolerates."""
    long_path = "C:\\" + "a" * DEFENDER_EXCLUSION_PATH_MAX
    kept = _coerced("defender_exclusions_applied", [
        r"C:\Apps\CleanerTool.exe",
        long_path,
        "   ",
        "C:\\bad\x00.exe",
        r"C:\Apps\CleanerTool.exe",      # same path again: one entry
    ])
    assert kept == [r"C:\Apps\CleanerTool.exe"]

    # exactly at the limit is still a path; one character more is not
    at_limit = "C:\\" + "a" * (DEFENDER_EXCLUSION_PATH_MAX - 3)
    assert len(at_limit) == DEFENDER_EXCLUSION_PATH_MAX
    assert _coerced("defender_exclusions_applied", [at_limit]) == [at_limit]


def test_the_close_list_name_limit_matches_the_taskkill_gate():
    """An entry is checked twice: here on load, and again in
    `game_night.custom_exe_name` immediately before the taskkill. Those copies
    are independent by design (that module imports nothing from the app), so
    nothing but a test keeps them from drifting — a more permissive copy here
    would let a name into the list that the close path then refuses."""
    from app.game_night import CUSTOM_APP_NAME_MAX, custom_exe_name

    # the two copies are declared separately on purpose; this is the only
    # thing that notices one of them changing
    assert CUSTOM_APP_NAME_MAX == GAME_NIGHT_APP_NAME_MAX

    at_limit = "a" * (GAME_NIGHT_APP_NAME_MAX - 4) + ".exe"
    over = "a" * (GAME_NIGHT_APP_NAME_MAX - 3) + ".exe"
    assert len(at_limit) == GAME_NIGHT_APP_NAME_MAX
    assert len(over) == GAME_NIGHT_APP_NAME_MAX + 1

    assert _coerced("game_night_custom_apps", [at_limit]) == [at_limit]
    assert custom_exe_name(at_limit) == at_limit
    assert _coerced("game_night_custom_apps", [over]) == []
    assert custom_exe_name(over) is None


def test_the_tools_hub_recency_is_bounded():
    from app.config_schema import TOOLS_RECENT_MAX

    kept = _coerced("tools_recent",
                    ["tool%d" % i for i in range(TOOLS_RECENT_MAX * 4)])
    assert len(kept) == TOOLS_RECENT_MAX
    assert _coerced("tools_recent", ["storage", 3, ""]) == ["storage"]


def test_the_game_night_close_list_and_the_drivers_folder_are_bounded():
    """These two caps were numbers inside their coercers, so nothing here could
    import them and nothing would have noticed them changing. They are named
    constants now, and the test reads the same number the code uses."""
    apps = [f"app{i}.exe" for i in range(GAME_NIGHT_APPS_MAX * 8)]
    assert len(_coerced("game_night_custom_apps", apps)) == GAME_NIGHT_APPS_MAX
    kept = _coerced("drivers_folder", "x" * (DRIVERS_FOLDER_MAX * 10))
    assert len(kept) == DRIVERS_FOLDER_MAX


#: Collection-valued keys with NO cap, and what bounds them instead. This is a
#: record of decisions, not an assurance: the test below makes a NEW list or
#: map key state its decision here instead of inheriting an unbounded field by
#: accident — which is exactly how `seen_tips` grew without limit before
#: LOW-010.
UNBOUNDED_TODAY = {
    "selected_tasks": "per-tab task keys; bounded by the tasks that exist",
    "applied_tweaks": "one id per applied tweak (~80 exist)",
    "session_pilot_games": "exe names the user typed; matched per poll",
    "session_pilot_paths": "picked exe paths; one per picked game",
    "session_pilot_names": "path -> label, for display only",
}

#: Keys whose cap is exercised above.
CAPPED = {
    "gameping_hosts", "game_night_keys", "game_night_close_apps",
    "duplicate_protected_paths", "run_history", "install_profiles",
    "startup_disabled", "seen_tips", "tweak_snapshots",
    "game_night_custom_apps", "defender_exclusions_applied",
    "health_history", "tools_recent", "insights",
}


def test_no_collection_key_is_silently_unbounded():
    collections = {k for k, v in DEFAULT_CONFIG.items()
                   if isinstance(v, (list, dict))}
    undecided = collections - CAPPED - set(UNBOUNDED_TODAY)
    assert not undecided, (
        f"collection keys with no cap and no recorded decision: "
        f"{sorted(undecided)}")
    # and the record must not drift the other way: a key that gained a cap
    # while still listed as unbounded would go untested
    assert not (set(UNBOUNDED_TODAY) & CAPPED)
    assert set(UNBOUNDED_TODAY) <= collections


# --------------------------------------------------------------------------- #
# Totality. config_schema states it in a comment — "The contract is total:
# none of these raise, whatever they are handed" — and it matters more than it
# sounds: _load_config_from_disk wraps apply_schema in a broad except, and an
# exception there is treated as a CORRUPT FILE. A coercer that raises on a
# hand-edited value does not lose that value, it quarantines the whole config:
# every saved profile, every tweak snapshot, every protection list.
# --------------------------------------------------------------------------- #

#: 10**400 comes out of json.load() perfectly well (ints are unbounded) but
#: float() refuses it with OverflowError. It is the one way a plain text file
#: can hand a coercer a number too big for the obvious conversion.
_BIG_INT = 10 ** 400


def _nest(depth, leaf="x"):
    value = leaf
    for _ in range(depth):
        value = [value]
    return value


def _deep_dict(depth):
    value = {"leaf": 1}
    for i in range(depth):
        value = {f"k{i}": value}
    return value


def _cyclic():
    looped = {}
    looped["self"] = looped
    looped["list"] = [looped]
    return looped


#: Values a coercer can be handed. Kept as data, not as a generator, so a
#: failure can name the value that broke it.
HOSTILE_VALUES = [
    # scalars
    None, True, False, 0, -1, 1, _BIG_INT, -_BIG_INT, 1.5, -0.0,
    float("nan"), float("inf"), float("-inf"),
    # strings
    "", "   ", "off", "03:00", "\x00", "\ud800", "x" * 200_000,
    "\\" * 1000, "e\u0301" * 1000,
    # sequences and maps
    [], [None], [1, "x", None], list(range(1000)),
    _nest(200), _cyclic(),
    {}, {"": ""}, {1: 2}, {("tuple",): "key"}, {"__bytes_hex__": 5},
    _deep_dict(200),
    # not JSON at all
    object(), b"bytes", bytearray(b"x"), {1, 2}, 3j, memoryview(b"x"),
    # shapes the structured coercers recognise, with the wrong contents
    [{"t": _BIG_INT, "b": _BIG_INT}],     # run_history: unbounded JSON ints
    [{"t": "inf", "b": 0}],
    [{"t": {}, "b": []}],
    [{"source": "s", "name": "n", "command": "c"}],   # startup_disabled
    [{"source": 1, "name": None, "command": ["x"]}],
    {"t": _BIG_INT, "b": _BIG_INT},
    {"t": "inf", "b": 0},
    {"specs": [["HKCU", "P", "V"]]},
    {"specs": "not a list"},
    {"task": {"specs": [[None, None, None]]}},
]


def _short(value, limit=140):
    try:
        text = repr(value)
    except Exception:            # noqa: BLE001 - a repr that raises is a datum too
        text = "<unreprable>"
    return text if len(text) <= limit else text[:limit] + "..."


@pytest.mark.parametrize("key", sorted(SCHEMA))
def test_no_coercer_raises_on_anything_it_is_handed(key):
    """The coercion contract, checked on every declared key rather than on the
    handful with a test of their own.

    A new coercer only has to pass this: whatever it decides, it decides
    without raising, so a hostile config file is loaded with that value
    discarded instead of the whole file being quarantined.
    """
    coerce, intent = SCHEMA[key]
    default = DEFAULT_CONFIG[key]
    failures = []
    for value in HOSTILE_VALUES:
        try:
            coerce(value, default)
        except Exception as exc:     # noqa: BLE001 - raising is the finding
            failures.append(f"{type(exc).__name__} on {_short(value)}")
    assert not failures, (
        f"SCHEMA[{key!r}] ({intent}) must never raise; it raised: "
        + "; ".join(failures))


@pytest.mark.parametrize("index", range(len(HOSTILE_VALUES)))
def test_a_whole_file_of_one_hostile_value_still_loads(index):
    """The same guarantee through apply_schema(), which is the path a real file
    takes: every key carrying the same junk value at once."""
    junk = HOSTILE_VALUES[index]
    data = {key: junk for key in DEFAULT_CONFIG}
    out = apply_schema(data, DEFAULT_CONFIG)
    assert set(out) == set(DEFAULT_CONFIG), (
        f"apply_schema dropped keys on {_short(junk)}")


def test_a_huge_number_does_not_take_the_whole_file_with_it(tmp_path,
                                                          monkeypatch):
    """Why the two OverflowError paths were worth fixing.

    A raising coercer is not a lost field. `_load_config_from_disk` treats any
    exception as corruption: it restores the backup or quarantines the file and
    hands back defaults, so one 400-digit number costs the user every saved
    profile, tweak snapshot and protection list. Both numbers below are legal
    JSON (`json.load` has no int size limit) and both used to raise.
    """
    import app.config_persist as cp

    digits = "9" * 400
    cfg = tmp_path / "config.json"
    cfg.write_text(
        f'{{"health_last_check": {digits}, "run_history": [{{'
        f'"t": {digits}, "b": 0}}], "close_to_tray_enabled": true}}',
        encoding="utf-8")
    monkeypatch.setattr(cp, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(cp, "CONFIG_FILE", cfg)
    monkeypatch.setattr(cp, "_config_cache", None, raising=False)
    monkeypatch.setattr(cp, "_config_mtime", None, raising=False)

    loaded = cp.load_config()

    assert loaded["health_last_check"] == DEFAULT_CONFIG["health_last_check"]
    assert loaded["run_history"] == []        # the one bad entry, dropped
    assert loaded["close_to_tray_enabled"] is True   # everything else survived
    assert cfg.exists()                       # not quarantined or moved
    assert not [p for p in tmp_path.iterdir() if "corrupt" in p.name.lower()]
