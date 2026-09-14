"""
Game Session Auto-Pilot (user-approved feature 6, 2026-09; Phase-5
overhaul 2026-09: installed-games watchlists + path-verified matching).

Watches the process list while the app is open; when a watched game
appears it applies the user's session preset through the standard run
engine, and when the game has been gone for a grace period it reverts
exactly what the pilot applied. Zero clicks, zero background service —
the watcher is a daemon thread that lives only while the GUI runs.

Phase-5 detection contract:
  * the process source returns (name, full_path) pairs — tasklist CSV
    for names, QueryFullProcessImageNameW (via app.elevation) for the
    full path of every .exe we consider
  * a watchlist entry may be a BASENAME ("eldenring.exe") or a full
    PATH ("C:/.../eldenring.exe"); paths match case-insensitively by
    normalization, basenames match like before
  * "watch installed games" adds every installed game's exe (and, when
    an exact exe can't be identified, the folder's plausible basenames)
    from app.game_catalog — resolved fresh at watcher start
  * path-verified javaw rule: javaw.exe is watched ONLY under a
    Minecraft-ish folder (the old rule fired on every Java app —
    IntelliJ, any Java tool — a false-positive machine)

Architecture: this module is GUI-free and headless-testable. Detection
(tasklist parsing) and the state machine live here; the Application
layer supplies callbacks that call run_tasks() / toast. The pilot
never writes the registry, never kills processes, never touches the
network — it only OBSERVES processes and reports transitions.
"""

from __future__ import annotations

import threading
import time

# Built-in watchlist: stable game-client executable names (lowercase,
# WITH extension — tasklist reports Image Name exactly so). Launchers
# themselves (steam.exe etc.) are deliberately NOT here: an idle
# launcher is not a play session. Anti-cheat SERVICES are excluded too
# (several run at boot and would pin the pilot permanently on).
# Phase-5: javaw.exe REMOVED (generic Java host — every Java tool
# triggered a "game session"). Java-based games are covered by the
# installed-games watchlist with path verification instead.
KNOWN_GAME_EXES = frozenset({
    # FromSoftware / Soulslikes
    "eldenring.exe",
    # Rockstar
    "gta5.exe", "rdr2.exe",
    # CD Projekt
    "witcher3.exe", "cyberpunk2077.exe",
    # Valve / Counter-Strike
    "cs2.exe",
    # Riot (game clients, not the always-on services)
    "valorant-win64-shipping.exe", "league of legends.exe",
    # Epic headline titles
    "fortniteclient-win64-shipping.exe",
    # Respawn / EA
    "r5apex.exe",
    # Activision
    "cod.exe",
    # Bethesda
    "starfield.exe", "fallout4.exe", "skyrimse.exe",
    # Ubisoft headliners
    "acvalhalla.exe", "acmirage.exe", "farcry6.exe",
    # Misc staples with stable exe names
    "minecraft.exe",
    "rocketleague.exe", "overwatch.exe",
    "destiny2.exe", "warframe.x64.exe",
    "pubg_ts3.exe", "tslgame.exe",
    "dota2.exe",
})

POLL_INTERVAL_S = 15.0
EXIT_STABLE_POLLS = 2     # game gone this many consecutive polls = exited
# (alt-tabbing, map loads and lobby-to-match hitches keep the process
# alive, so a single clean miss already means a real exit; 2 is grace)

# javaw path rule: only a javaw under a path that looks like Minecraft
# (the vanilla launcher runtime or a .minecraft folder) counts as a game.
_JAVAW_PATH_HINTS = (".minecraft", "minecraft", "mojang")


def normalize_exe(name: str) -> str:
    """Canonical watchlist form: lowercase basename with extension."""
    import os as _os
    base = _os.path.basename((name or "").strip().lower())
    if base and not base.endswith(".exe"):
        base += ".exe"
    return base


def normalize_path(path: str) -> str:
    """Canonical path form for comparison: lowercase, forward slashes,
    no trailing slash. Returns '' for anything that isn't pathlike.
    L03: UNC (\\\\server\\share\\game.exe) has no drive colon but is a
    valid game path — accept it."""
    try:
        p = (path or "").strip().lower().replace("/", "\\")
        while p.endswith("\\"):
            p = p[:-1]
        if p.startswith("\\\\"):
            return p if "\\" in p[2:] else ""
        if "\\" not in p or ":" not in p:
            return ""
        return p
    except Exception:
        return ""


def default_get_processes(timeout: int = 10):
    """Live [(image name, full path)] pairs, via tasklist CSV + a
    per-process QueryFullProcessImageNameW resolution (batched through
    tasklist's PID column — one subprocess, then fast ctypes calls; no
    per-process spawning). Never raises: [] on any failure (a failed
    poll is a missed poll, never a crash)."""
    try:
        import subprocess as _sp
        out = _sp.check_output(
            "tasklist /fo csv /nh", shell=True, text=True, timeout=timeout,
            errors="replace", stderr=_sp.DEVNULL,
            creationflags=getattr(_sp, "CREATE_NO_WINDOW", 0),
        )
    except Exception:
        return []
    procs = []
    try:
        for line in (out or "").splitlines():
            parts = [p.strip().strip('"') for p in line.split('","')]
            if len(parts) < 2:
                continue
            name = parts[0].lower()
            if not name.endswith(".exe"):
                continue
            pid = parts[1]
            procs.append((name, int(pid) if pid.isdigit() else 0))
    except Exception:
        return []
    # resolve full paths in one pass (OpenProcess+QueryFullProcessImageName
    # are cheap; 0-PID entries just keep name-only matching)
    try:
        from app.elevation import process_exe_path
        return [(name, process_exe_path(pid) or "")
                for name, pid in procs]
    except Exception:
        return [(name, "") for name, _pid in procs]


def _is_javaw_game_path(path: str) -> bool:
    """javaw.exe only counts under a Minecraft-ish install path."""
    p = (path or "").lower()
    return any(h in p for h in _JAVAW_PATH_HINTS)


class SessionPilot:
    """Edge-triggered watcher: on_apply(hit_exes) on launch detection,
    on_revert() after the grace period. tick() advances one poll — the
    sync harness drives it directly (deterministic, no threads); start()
    runs the same tick on a daemon timer for production.

    Watchlist: a set of basenames (name matching, backward compatible)
    PLUS a dict {basename: set(normalized parent dirs)} for
    path-verified entries — a basename hit from a directory that is not
    one of its registered dirs is IGNORED (kills same-name-exe-anywhere
    false positives). Entries with no registered dirs match on basename
    alone (the legacy contract for hand-picked famous titles).

    Guards: apply fires once per session (no re-apply spam while the
    game runs); revert fires once per exit; a re-launch starts a fresh
    session. Callbacks must be quick and non-blocking (they run on the
    watcher thread) — the GUI marshals to Tk itself.
    """

    def __init__(self, get_processes=None, watched=None,
                 on_apply=None, on_revert=None,
                 stable_polls: int = EXIT_STABLE_POLLS):
        self._get_processes = get_processes or default_get_processes
        self._on_apply = on_apply or (lambda _hits: None)
        self._on_revert = on_revert or (lambda: None)
        self._stable_polls = max(1, stable_polls)
        self._state = "idle"      # idle | active
        self._misses = 0
        self._active_hit = None
        self._stop = False
        self._thread = None
        # matching state: basenames always; path-verified dirs per basename
        self._watched_names = set()
        self._watched_dirs = {}   # basename -> {normparent, ...}
        self.set_watched(watched or [])

    # -- introspection (dialog status line + tests) -------------------- #

    @property
    def state(self) -> str:
        return self._state

    @property
    def active_hit(self):
        """Exe name that triggered the current session (None when idle)."""
        return self._active_hit

    @property
    def watched(self) -> set:
        """Legacy view: basenames only (dialog/tests)."""
        return set(self._watched_names)

    def set_watched(self, names) -> None:
        """Rebuild the match tables from mixed basenames/paths. A value
        with a drive letter registers basename + parent-dir pinning;
        a bare name registers basename-only (legacy behavior)."""
        names_tab, dirs_tab = set(), {}
        for w in (names or []):
            if not w:
                continue
            norm_path = normalize_path(w)
            if norm_path:
                base = normalize_exe(w)
                parent = normalize_path(_parent_of(norm_path))
                names_tab.add(base)
                if parent:
                    dirs_tab.setdefault(base, set()).add(parent)
            else:
                names_tab.add(normalize_exe(w))
        self._watched_names = names_tab
        self._watched_dirs = dirs_tab

    # -- core ----------------------------------------------------------- #

    def _hit(self, name, path) -> "str | None":
        """Watched-exe check for one process. Returns the watched
        basename on a hit, else None. Path pinning: when the basename
        has registered dirs, the process's dir must be one of them."""
        if name not in self._watched_names:
            return None
        if name == "javaw.exe" and not _is_javaw_game_path(path):
            return None
        dirs = self._watched_dirs.get(name)
        if dirs:
            pdir = normalize_path(_parent_of(path))
            if pdir and pdir in dirs:
                return name
            return None
        return name

    def tick(self) -> str:
        """One poll: detect launches/exits, fire at most one callback.
        Returns the (possibly new) state. Never raises — a failed poll
        is swallowed so one bad tasklist can never kill the watcher."""
        try:
            procs = self._get_processes() or []
        except Exception:
            return self._state
        try:
            hits = set()
            for item in procs:
                try:
                    name, path = item
                except Exception:
                    continue
                # normalize HERE (not just in the default source): a
                # custom source returning 'CS2.EXE' or a mixed-slash
                # path must match like the canonical forms
                name = normalize_exe(name)
                path = normalize_path(path) or (path or "")
                hit = self._hit(name, path)
                if hit:
                    hits.add(hit)
            hits = sorted(hits)
        except Exception:
            return self._state
        try:
            if self._state == "idle":
                if hits:
                    self._state = "active"
                    self._misses = 0
                    self._active_hit = hits[0]
                    self._on_apply(list(hits))
            else:  # active
                if hits:
                    self._misses = 0
                    self._active_hit = hits[0]
                else:
                    self._misses += 1
                    if self._misses >= self._stable_polls:
                        self._state = "idle"
                        self._misses = 0
                        self._active_hit = None
                        self._on_revert()
        except Exception:
            pass
        return self._state

    # -- thread driver ---------------------------------------------------- #

    def start(self, interval_s: float = POLL_INTERVAL_S) -> bool:
        """Begin background polling. Returns False if already running."""
        if self._thread is not None and self._thread.is_alive():
            return False
        self._stop = False

        def _loop():
            while not self._stop:
                self.tick()
                for _ in range(int(max(1.0, interval_s) * 10)):
                    if self._stop:
                        break
                    time.sleep(0.1)

        try:
            self._thread = threading.Thread(target=_loop, daemon=True,
                                            name="SessionPilot")
            self._thread.start()
            return True
        except Exception:
            self._thread = None
            return False

    def stop(self, join_timeout: float = 2.0) -> None:
        # L06: best-effort join under a small timeout (loop sleeps 0.1s
        # slices, so it exits promptly); never block the Tk thread long.
        # Lock-free: _stop is a plain bool flipped here, read by the loop
        # (GIL-atomic); _thread is only ever started/stopped from the app
        # thread, so no lock is needed for this handoff.
        self._stop = True
        th, self._thread = self._thread, None
        try:
            if th is not None and th.is_alive() and th is not threading.current_thread():
                th.join(timeout=join_timeout)
        except Exception:
            pass

    def running(self) -> bool:
        try:
            return self._thread is not None and self._thread.is_alive()
        except Exception:
            return False


def _parent_of(path: str) -> str:
    """Parent directory of a normalized path ('' at the root)."""
    try:
        i = path.rstrip("\\").rfind("\\")
        return path[:i] if i > 0 else ""
    except Exception:
        return ""
