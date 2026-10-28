"""H-004: the default-on set is deliberate, and pinned.

`Task.default` used to be True, so a task appended to a TASKS list without the
flag was CHECKED by default almost everywhere - including the tabs where an
unchecked box is the only thing standing between a user and an irreversible
action. The flag is now opt-in, and this test is the contract: every task that
is checked by default is named here, so adding one shows up as a failing test
rather than as a surprise in a user's first run.
"""
from __future__ import annotations

import pytest

from app.tasks.advanced_tasks import TASKS as ADVANCED
from app.tasks.clean_tasks import TASKS as CLEAN
from app.tasks.game_tasks import TASKS as GAME
from app.tasks.install_tasks import TASKS as INSTALL
from app.tasks.repair_tasks import TASKS as REPAIR
from app.tasks.tweak_tasks import TASKS as TWEAK

#: module -> the ONLY keys allowed to be checked by default.
DEFAULT_ON = {
    "clean": {
        "shader_cache", "launcher_cache", "engine_cache", "user_temp_files",
        "system_temp_files", "win_update_cache", "delivery_optimization",
        "inet_cache", "error_reports", "thumbnail_cache", "chk_fragments",
        "dns_flush",
    },
    "repair": {"restore_point"},
    "tweak": {
        "restore_point_tweak", "ultimate_performance", "classic_context_menu",
        "disable_game_dvr", "game_mode", "windowed_optimize",
    },
    "game": {"gamer_launchers", "game_files"},
    "install": set(),
    "advanced": set(),
}

_LISTS = {
    "clean": CLEAN, "repair": REPAIR, "tweak": TWEAK, "game": GAME,
    "install": INSTALL, "advanced": ADVANCED,
}


def test_the_dataclass_default_is_off():
    from app.tasks import Task

    assert Task.__dataclass_fields__["default"].default is False


@pytest.mark.parametrize("module", sorted(_LISTS))
def test_the_default_on_set_is_unchanged(module):
    live = {task.key for task in _LISTS[module] if task.default}
    assert live == DEFAULT_ON[module]


@pytest.mark.parametrize("module", sorted(_LISTS))
def test_nothing_risky_is_default_on(module):
    for task in _LISTS[module]:
        if not task.default:
            continue
        assert task.risk not in ("DATA LOSS", "ADVANCED"), (
            "%s is checked by default but is marked %s" % (task.key, task.risk))


def test_every_task_choice_is_explicit():
    """No Task may rely on the dataclass default any more."""
    import ast
    import pathlib

    for path in pathlib.Path("app", "tasks").glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            if getattr(node.func, "id", "") != "Task":
                continue
            assert "default" in {k.arg for k in node.keywords}, (
                "%s:%d builds a Task without saying whether it is checked"
                % (path, node.lineno))
