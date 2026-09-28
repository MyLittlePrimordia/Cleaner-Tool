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

from app.utils import resolve_exe

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
#: How long a single process poll may block. `stop()` must wait longer than
#: this or it can return with the watcher thread still inside tasklist
#: (BUG-023), which is what let a stale revert reach the next pilot.
POLL_TIMEOUT_S = 10.0
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


def default_get_processes(timeout: int = 10, wanted=None):
    """Live [(image name, full path)] pairs, via tasklist CSV + a
    per-process QueryFullProcessImageNameW resolution (batched through
    tasklist's PID column — one subprocess, then fast ctypes calls; no
    per-process spawning). THREE-valued, and that is the point (BUG-022):
    it used to return [] on every failure, and [] is also what 'nothing is
    running' looks like - so a tasklist timeout was indistinguishable
    from the game exiting, and two consecutive timeouts mid-match reverted
    the tweaks of a game that was still running.
        []    - polled successfully, nothing matched
        list  - polled successfully, these matched
        None  - we do not know; the poll failed
    tick() treats None as 'do not count this poll', so a transient failure
    can no longer cause a revert. Never raises.

    F5-3: tasklist is invoked as an argv list with shell=False — no cmd.exe
    parsing of the command line at all.
    F1-3: when `wanted` (a set of lowercase invocable basenames) is given,
    candidates are pre-filtered BY BASENAME before any OpenProcess/ctypes
    resolution — when none of the watched exes are even running, this is
    just one tasklist and no ctypes loop at all. `wanted=None` keeps the
    legacy resolve-everything behavior."""
    try:
        import subprocess as _sp
        out = _sp.check_output(
            [resolve_exe("tasklist"), "/fo", "csv", "/nh"], text=True, timeout=timeout,
            errors="replace", stderr=_sp.DEVNULL,
            creationflags=getattr(_sp, "CREATE_NO_WINDOW", 0),
        )
    except Exception:
        # BUG-022: the tasklist call FAILED. That is "we do not know", and it
        # must not be reported as [] (="nothing is running") - the state
        # machine counts misses toward revert, so two consecutive tasklist
        # timeouts mid-match used to pull Game Mode out from under a player who
        # was still playing, plus a "Game Mode OFF" toast. tasklist timing out
        # at 10 s is routine on a loaded machine or during an AV scan.
        return None
    procs = []
    try:
        for line in (out or "").splitlines():
            parts = [p.strip().strip('"') for p in line.split('","')]
            if len(parts) < 2:
                continue
            name = parts[0].lower()
            if not name.endswith(".exe"):
                continue
            if wanted is not None and name not in wanted:
                continue
            pid = parts[1]
            procs.append((name, int(pid) if pid.isdigit() else 0))
    except Exception:
        return None
    if not procs:
        return []   # a SUCCESSFUL poll that matched nothing
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
        self._on_apply = on_apply or (lambda _hits: None)
        self._on_revert = on_revert or (lambda: None)
        self._stable_polls = max(1, stable_polls)
        self._state = "idle"      # idle | active
        self._misses = 0
        self._active_hit = None
        self._stop = False
        self._thread = None
        # BUG-023 generation counter. `_gen` is the generation of the run that
        # is CURRENTLY polling; `_generation` is the high-water mark handed out
        # to each start(). A thread only ever speaks for its own `_gen`, so a
        # consumer can compare and drop anything stale.
        self._generation = 0
        self._gen = 0
        self._gen_lock = threading.Lock()
        # matching state: basenames always; path-verified dirs per basename
        self._watched_names = set()
        self._watched_dirs = {}   # basename -> {normparent, ...}
        if get_processes is None:
            # F1-3: resolve the module-global at CALL time, not construction.
            # The sync tests/harness swap session_pilot.default_get_processes
            # AFTER __init__ (smoke_async does exactly that), and a patched
            # source shaped like `lambda timeout=10:` must keep working —
            # hence the TypeError fallback to the legacy call signature.
            def _default_source(timeout: int = 10):
                fn = globals().get("default_get_processes")
                if fn is None:
                    return []
                try:
                    return fn(timeout=timeout, wanted=set(self._watched_names))
                except TypeError:
                    return fn(timeout=timeout)
            self._get_processes = _default_source
        else:
            self._get_processes = get_processes
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
        is swallowed so one bad tasklist can never kill the watcher.

        BUG-022: a poll that came back UNKNOWN (the process source returned
        None - e.g. tasklist timed out) is NOT counted as a miss. Counting it
        is what made two consecutive tasklist timeouts revert the tweaks of a
        game that was still running, and toast "Game Mode OFF". We simply do
        not know, so we decline to conclude anything and try again next tick.
        A genuine exit is still two REAL polls with no match.
        """
        try:
            procs = self._get_processes()
        except Exception:
            return self._state
        if procs is None:
            # unknown. Deliberately not `or []`, which would fold this into
            # "nothing is running" - the exact bug.
            return self._state
        procs = procs or []
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
            # BUG-023: a STOPPED pilot must never speak again. `stop()` sets
            # _stop and then waits, but `tick()` can already be in flight - past
            # the _stop check in _loop, inside the (slow) process poll - when
            # stop() is called. It would then reach _on_apply/_on_revert AFTER
            # stop() returned, and _pilot_revert restores settings and clears
            # _pilot_applied_keys, which by then belong to whatever session the
            # user has since started. A stale revert, silently undoing a new
            # session. So re-check immediately before every callback, not just
            # in the loop.
            if self._stop:
                return self._state
            if self._state == "idle":
                if hits:
                    self._state = "active"
                    self._misses = 0
                    self._active_hit = hits[0]
                    if not self._stop:
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
                        if not self._stop:
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
        # BUG-023: every start() is a new GENERATION. Anything a previous
        # generation still produces - a late tick, an already-queued message -
        # can be recognised as stale and dropped, instead of the code having to
        # bet that stop() was thorough enough. Cheap, and it makes the
        # "stop() actually stopped" property checkable rather than assumed.
        with self._gen_lock:
            self._generation += 1
            self._gen = self._generation

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

    def stop(self, join_timeout: float = None) -> bool:
        """Ask the watcher to finish, and WAIT for it to actually finish.

        BUG-023. This used to join with a fixed 2-second timeout while `tick()`
        can block for up to the process-poll timeout (10 s for tasklist) plus
        the path-resolution pass. So `stop()` routinely returned with the thread
        STILL ALIVE, and that had two consequences the caller could not see:

          1. The old thread could still reach `_on_revert()` and put a stale
             "revert" message on the queue that the NEW pilot (already
             installed by then) drains - reverting tweaks the new pilot had
             just applied, i.e. a spontaneous "Game Mode OFF" mid-game.
          2. `_set_stay_awake` increments a counter non-atomically against a
             later decrement, so two increments against one decrement left the
             stay-awake hold engaged forever: THE PC NEVER SLEEPS and the
             display never turns off until the app exits.

        The fix is to not lie about termination. `_stop` is set, then we wait
        for the thread to actually die, for as long as a poll can legitimately
        take (the poll timeout, with headroom). Only if it still will not die
        do we say so, by returning False, instead of pretending.

        Returns True if the thread is confirmed dead.
        """
        self._stop = True
        th, self._thread = self._thread, None
        if th is None:
            return True
        try:
            if th.is_alive() and th is not threading.current_thread():
                if join_timeout is None:
                    # cover an in-flight poll: the process source's own
                    # timeout, plus slack for the ctypes path pass
                    join_timeout = POLL_TIMEOUT_S + 5.0
                th.join(timeout=join_timeout)
                if th.is_alive():
                    # Still running. Report it rather than pretending.
                    return False
        except Exception:
            return False
        return True

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
