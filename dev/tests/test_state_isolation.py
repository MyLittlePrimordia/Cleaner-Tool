"""A test run must never touch the developer's real per-user state.

2026-10: a full suite run appended to the real
``%LOCALAPPDATA%\\CleanerTool\\events.log`` (the elevation and quarantine paths
log there) and could rewrite ``config.json`` through any save path the tests
exercised. Both files belong to the person running the tests, not to the
suite.

The fix is one override in ``app.config_paths`` (``CLEANER_TOOL_HOME``) plus
conftest pointing it at a temp directory before the first ``app.*`` import,
because ``CONFIG_DIR`` / ``CONFIG_FILE`` are resolved once at import time.

These tests pin the mechanism, not the conftest line: if the override is ever
dropped from ``get_config_dir``, the first test fails even though conftest
still sets the variable.
"""
from __future__ import annotations

import os
from pathlib import Path


def test_the_suite_is_pointed_at_a_temp_home():
    from app import config_paths

    home = os.environ.get("CLEANER_TOOL_HOME")
    assert home, "conftest.py must set CLEANER_TOOL_HOME before app imports"
    home_path = Path(home)
    assert (config_paths.CONFIG_DIR == home_path
            or home_path in config_paths.CONFIG_DIR.parents), \
        config_paths.CONFIG_DIR


def test_the_resolved_paths_are_not_the_real_user_state():
    from app import config_paths

    real = Path(os.environ.get("LOCALAPPDATA", "")) / "CleanerTool"
    if str(real) in ("", "CleanerTool"):
        real = Path.home() / "AppData" / "Local" / "CleanerTool"
    assert config_paths.CONFIG_DIR != real, config_paths.CONFIG_DIR
    assert config_paths.CONFIG_FILE.parent == config_paths.CONFIG_DIR
    assert config_paths.EVENTS_LOG_FILE.parent == config_paths.CONFIG_DIR


def test_the_override_wins_over_localappdata(tmp_path, monkeypatch):
    from app import config_paths

    monkeypatch.setenv("CLEANER_TOOL_HOME", str(tmp_path / "portable"))
    assert config_paths.get_config_dir() == tmp_path / "portable"


def test_an_empty_override_falls_back_to_localappdata(tmp_path, monkeypatch):
    from app import config_paths

    monkeypatch.setenv("CLEANER_TOOL_HOME", "   ")
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    assert config_paths.get_config_dir() == tmp_path / "CleanerTool"
