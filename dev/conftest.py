"""Make the repo root importable so `import app.*` works from pytest.

The app is a package under the repo root (no install step), and there was no
test suite before Phase 0, so nothing pinned this down. Running `python -m
pytest` from the repo root puts the cwd on sys.path, but being explicit here
also covers plain `pytest` invocations and IDE runners.
"""
import os
import sys
import tempfile

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

# No test may touch the real per-user state. Before the override existed, a
# suite run appended to %LOCALAPPDATA%\CleanerTool\events.log (the elevation
# and quarantine paths log there) and any test that drove a save could rewrite
# the developer's own config.json. `app.config_paths` resolves these once at
# import, so the environment has to be set here, before the first `app.*`
# import - which is why this lives at module scope rather than in a fixture.
_TEST_HOME = os.path.join(tempfile.mkdtemp(prefix="cleanertool_tests_"), "home")
os.environ["CLEANER_TOOL_HOME"] = _TEST_HOME
os.environ.setdefault("CLEANER_TOOL_LOG_DIR", os.path.join(_TEST_HOME, "logs"))


@pytest.fixture(autouse=True)
def _keep_the_elevation_trail_off_the_machine(monkeypatch):
    """No test run may append to the real elevation log.

    app/elevation.py writes its best-effort failures to stderr, a file in
    %TEMP% and the security event log, and several tests drive those paths on
    purpose (a blocked Defender call, a raising ctypes). Tests that assert on
    the trail patch this same name themselves and win.
    """
    import app.elevation as el

    monkeypatch.setattr(el, "_log_or_swallow_fallback",
                        lambda msg, exc=None: None)
