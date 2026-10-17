"""Make the repo root importable so `import app.*` works from pytest.

The app is a package under the repo root (no install step), and there was no
test suite before Phase 0, so nothing pinned this down. Running `python -m
pytest` from the repo root puts the cwd on sys.path, but being explicit here
also covers plain `pytest` invocations and IDE runners.
"""
import os
import sys

_ROOT = os.path.dirname(os.path.abspath(__file__))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)
