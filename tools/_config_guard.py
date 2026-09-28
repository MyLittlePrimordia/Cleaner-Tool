"""Byte-exact snapshot/restore of the user's real ``config.json``.

Use this in ANY test that drives the real ``run_tasks`` or the real config
writers. It exists because two of them did not, and their probe keys leaked
into the user's actual settings:

  * ``tools/verify_run_generation.py`` left ``h10_probe`` in ``applied_tweaks``
  * ``tools/verify_run_state_e2e.py`` could leave
    ``user_temp_files_probe`` in ``selected_tasks``

Enumerating the keys a test might write is a losing game: every new probe key
is a new leak. This snapshots the file's bytes and puts them back verbatim, so
anything written during the test disappears regardless of what wrote it.

Byte-exact rather than ``load_config()`` / ``save_config()`` on purpose, since
``save_config`` runs the schema normaliser and can add or reorder keys - which
would make a passing test still leave the user's file subtly different.
"""

from __future__ import annotations

import atexit
import io
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.config_persist import get_config_dir  # noqa: E402


def config_path() -> str:
    return os.path.join(get_config_dir(), "config.json")


class ConfigGuard:
    """Context manager: restore the real config file byte-for-byte on exit.

        with ConfigGuard():
            ...drive real writers...

    Restores even if the body raises, and deletes the file if it did not exist
    beforehand. Never raises itself - a cleanup failure must not mask the real
    test result, so problems are reported on stderr and swallowed.
    """

    def __init__(self, label: str = "config") -> None:
        self.label = label
        self.path = config_path()
        self.existed = os.path.exists(self.path)
        self.raw: bytes | None = None
        self.restored = False
        self._done = False
        # Safety net: these tests drive Tk and real writers, so a crash, a
        # sys.exit, or an exception raised before the explicit __exit__ would
        # otherwise leave the probe keys behind. atexit covers all of those.
        atexit.register(self.__exit__, None, None, None)

    def __enter__(self) -> "ConfigGuard":
        if self.existed:
            with io.open(self.path, "rb") as handle:
                self.raw = handle.read()
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        if self._done:
            return False
        self._done = True
        try:
            if self.existed:
                assert self.raw is not None
                with io.open(self.path, "wb") as handle:
                    handle.write(self.raw)
            elif os.path.exists(self.path):
                os.remove(self.path)
            self.restored = True
        except Exception as restore_exc:  # pragma: no cover
            sys.stderr.write(
                "ConfigGuard(%s): FAILED to restore %s: %r\n"
                % (self.label, self.path, restore_exc)
            )
        return False

    def verify(self) -> bool:
        """True if the file currently matches the snapshot. For assertions."""
        if not self.existed:
            return not os.path.exists(self.path)
        if not os.path.exists(self.path):
            return False
        with io.open(self.path, "rb") as handle:
            return handle.read() == self.raw
