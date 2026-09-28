"""Task-key constants shared by the preset tables and the config migrator.

H2 (cycle break). These two sets used to live in `app.tab_presets`, and
`app.config_persist` imported them from there — which closed the repository's
only import cycle:

    utils -> config_persist -> tab_presets -> tasks -> utils

They are pure data with no dependencies, so they belong in their own leaf
module. `app.tab_presets` still re-exports both names, so existing importers
(`app.scheduler`) and the F3-4 "import the PUBLIC constants, not a private
copy" rule are unaffected — there is still exactly ONE definition, and the
drift the original comment warned about remains impossible.
"""
from __future__ import annotations

# Tasks deliberately cut from the app (punch-list #13 "Remove from app").
# The config migrator folds any legacy selection containing them away, and
# tab_presets._validate() asserts none of them reappear in a task table.
CUT_TASK_KEYS = {"adv_memory_integrity", "adv_vmp", "wpbt_disable"}

# Legacy Games-tab task key -> the task that replaced it. Applied by the
# migrator so an old saved selection still points at something that exists.
GAMES_TO_CLEAN_DEDUPE = {
    "gamer_launchers": "launcher_cache",
    "gpu_shader_caches": "shader_cache",
}
