r"""Decide WHICH file an app's icon should come from.

app/icon_extract.py knows how to turn a file into pixels. This module knows
which file that is — and for most of the installed-programs list, nothing
told it, which is why 16 of 41 rows in the Uninstall Programs dialog showed a
grey placeholder.

The registry's `DisplayIcon` is the obvious source and is what Control Panel
itself shows, so it still wins whenever it resolves. But it is absent or
useless often enough to matter, for three separate reasons:

  * **No DisplayIcon at all.** MSI installers and Electron/Squirrel apps
    (Audacity, Discord, FFmpeg via WinGet) simply don't write one.
  * **The real exe is nested.** Audacity installs to
    `Program Files/Audacity 4/` but the binary is
    `Program Files/Audacity 4/bin/Audacity4.exe`. Eclipse Temurin is
    `.../jdk-25.0.4.101-hotspot/bin/java.exe` — two levels down. A
    top-level-only search finds nothing.
  * **DisplayIcon names a binary with no icon resource.**

So: hunt, in an order chosen so each step is strictly better than the next:

  1. `DisplayIcon`, if it resolves to a real file
  2. a `.ico` inside the install folder — plenty of apps ship their actual
     artwork there and nowhere else (Discord's `app.ico`, for one)
  3. the best-matching `.exe` in the install folder, searched a few levels
     deep, preferring a name that matches the program and skipping the
     updater/uninstaller/helper binaries that are never "the app"
  4. the same, relative to wherever the uninstaller actually lives

Everything is bounded (`max_depth`, `max_hits`): this runs for every program
in the list, on a worker thread, and an unbounded walk of `C:\Program Files`
is not an option.
"""
from __future__ import annotations

import os
import re
import sys

IS_WINDOWS = sys.platform.startswith("win")

#: File extensions we accept as an icon source.
_ICON_EXTS = (".exe", ".ico")

#: Exe stems that are never the program the user means to see. Discord's
#: only real icon lives in app.ico because its launcher is Update.exe, so
#: these are deprioritised rather than excluded outright.
_NOT_THE_APP = (
    "update", "updater", "uninstall", "unins", "setup", "install",
    "crashpad", "crashreport", "crashhandler", "vcredist", "vc_redist",
    "dotnetfx", "repair", "helper", "bootstrap", "elevator",
)

_EXE_TOKENS = (".exe", ".com", ".bat", ".cmd")


def _norm(text: str) -> str:
    """'Audacity 4.0' -> 'audacity4'. Used to match a program to a file."""
    return "".join(ch for ch in (text or "").lower() if ch.isalnum())


def _looks_like_helper(stem: str) -> bool:
    low = (stem or "").lower()
    return any(low == bad or low.startswith(bad) for bad in _NOT_THE_APP)


def _rank(name: str, filename: str, depth: int, size: int = 0):
    """Sort key for one candidate; lower is better. None = not a candidate.

    The final tiebreak is file size, DESCENDING, and that is not cosmetic.
    When no candidate matches the program name, several tools in one folder tie
    on name rank and the old `len(filename)` tiebreak picked the SHORTEST
    name — which chose `jar.exe` and `jjs.exe` over `java.exe` for the
    Eclipse Temurin entries. Those helpers have no icon resource at all, so
    the row stayed a grey square even though the real launcher next to them
    has a perfectly good one. The primary binary is reliably the big one.
    """
    if not filename.lower().endswith(_ICON_EXTS):
        return None
    stem, ext = os.path.splitext(filename)
    target = _norm(name)
    this = _norm(stem)
    if ext.lower() == ".ico":
        # a .ico beside the exe is usually the real artwork
        rank = 0 if (target and (this in target or target in this)) else 1
    elif not target:
        rank = 5 if _looks_like_helper(stem) else 6
    elif this == target:
        rank = 0
    elif target.startswith(this) or this.startswith(target):
        rank = 2
    elif this and (this in target or target in this):
        rank = 3
    else:
        rank = 9 if _looks_like_helper(stem) else 4
    # shallower wins: the app binary lives near the top of its folder
    return (rank, depth, -size)


def find_icon_in_folder(folder: str, name: str, max_depth: int = 3,
                        max_hits: int = 500):
    """Best icon source under `folder`, or None.

    Iterative BFS so the depth cap is explicit and no recursion limit can
    bite. max_depth=3 covers every real layout: exe at the top, in `bin/`,
    or in a versioned `jdk-x.y.z/bin/`.
    """
    if not folder or not os.path.isdir(folder):
        return None
    try:
        root = os.path.normpath(os.path.expandvars(folder))
    except Exception:
        return None
    best = None
    hits = 0
    frontier = [(root, 0)]
    while frontier and hits < max_hits:
        nxt = []
        for current, depth in frontier:
            if depth > max_depth or hits >= max_hits:
                continue
            try:
                entries = os.listdir(current)
            except OSError:
                continue
            for entry in entries:
                hits += 1
                if hits > max_hits:
                    break
                full = os.path.join(current, entry)
                try:
                    size = os.path.getsize(full)
                except OSError:
                    size = 0
                rank = _rank(name, entry, depth, size)
                if rank is not None:
                    if best is None or rank < best[0]:
                        best = (rank, os.path.join(current, entry))
                        if rank[0] == 0 and depth == 0:
                            return best[1]      # cannot be beaten
                elif depth < max_depth:
                    try:
                        if os.path.isdir(os.path.join(current, entry)):
                            nxt.append((os.path.join(current, entry), depth + 1))
                    except OSError:
                        pass
        frontier = nxt
    return best[1] if best else None


def exe_from_command(command) -> str:
    """The first real executable in a command line, or None.

    Run values are command lines, and the awkward case is an UNQUOTED path
    that contains spaces \u2014 which is most of them, because only paths with
    spaces strictly need quoting and installers are inconsistent about it:

        C:\\Program Files (x86)\\Common Files\\Java\\Java Update\\jusched.exe
        "C:\\Program Files (x86)\\Steam\\steam.exe" -silent

    Naively splitting on whitespace turned the first of those into the
    relative `Update\\jusched.exe`, which still ends in .exe so it passed an
    endswith check and then resolved to nothing. So: a quoted first token
    wins, and otherwise take the LONGEST prefix of the string that is an
    existing file. Longest-first is what makes the arguments lose to the
    program path.
    """
    if not command:
        return None
    text = str(command).strip()
    if text.startswith('"'):
        end = text.find('"', 1)
        if end > 1:
            cand = os.path.expandvars(text[1:end].strip())
            return cand or None
    parts = text.split()
    for i in range(len(parts), 0, -1):
        cand = os.path.expandvars(" ".join(parts[:i]).strip().strip('"'))
        if not cand.lower().endswith(_EXE_TOKENS):
            # a real candidate ends in an executable extension; if the longest
            # prefix does not, no shorter one can
            break
        if os.path.isfile(cand):
            return cand
    # nothing on disk matched (already uninstalled, or a relative path)
    for tok in parts:
        if tok.lower().endswith(_EXE_TOKENS):
            return os.path.expandvars(tok)
    return None


# --- .lnk (Startup-folder shortcuts) -------------------------------------- #

#: A Windows path ending in .exe. Non-greedy so it stops at the first .exe
#: rather than running to the end of a run of adjacent strings.
_ABS_EXE_RE = re.compile(r"[A-Za-z]:\\[ -~]{3,260}?\.exe", re.ASCII)
_MAX_LNK_READ = 16384


def lnk_target(lnk_path):
    """Target of a `.lnk`, or None.

    Implemented by extracting candidate path strings and keeping the ones that
    actually exist on disk, rather than by walking the Shell Link LinkInfo
    field offsets. That is a deliberate reversal: the documented offsets were
    tried first and read back nonsense on real Startup shortcuts here
    (LinkInfoSize came back as 1311187 on a 2 KB file), because the two
    interesting strings — the relative path and the absolute one — sit
    adjacent with no NUL between them, so even a correct offset can hand back
    a run that does not end in .exe.

    Validating against the filesystem is the check that cannot be fooled: a
    candidate is only accepted if `os.path.isfile` agrees. Both encodings are
    scanned, since a shortcut's strings may be stored either way.
    """
    if not lnk_path or not os.path.isfile(lnk_path):
        return None
    try:
        with open(lnk_path, "rb") as fh:
            raw = fh.read(_MAX_LNK_READ)
    except OSError:
        return None

    candidates = []
    # ANSI bytes, read as latin-1 so byte offsets survive the regex
    try:
        candidates += _ABS_EXE_RE.findall(raw.decode("latin-1"))
    except Exception:
        pass
    # UTF-16LE strings
    try:
        candidates += _ABS_EXE_RE.findall(raw.decode("utf-16-le", "ignore"))
    except Exception:
        pass

    best = None
    for cand in candidates:
        cand = cand.strip().strip("\x00").strip()
        if not cand or cand.lower().endswith((".dll",)):
            continue
        # a run can glue the next field on ("...FxSound.exe%C:\..."), so cut
        # at any character that cannot be in a path
        cand = re.split(r"[%\x00]", cand)[0]
        if not cand.lower().endswith(".exe"):
            continue
        path = os.path.expandvars(cand)
        if not os.path.isfile(path):
            continue
        # prefer a path with no relative hops, then the shortest
        hops = cand.count("..\\")
        key = (hops, len(path))
        if best is None or key < best[0]:
            best = (key, path)
    return best[1] if best else None


def icon_source_for_command(command):
    """Icon source for a startup item's command line, or None.

    Handles both shapes a startup entry takes: a Run value that IS a command
    line pointing at an exe, and a Startup-folder `.lnk` shortcut."""
    if not command:
        return None
    text = str(command).strip()
    if text.lower().endswith(".lnk"):
        target = lnk_target(os.path.expandvars(text.strip('"')))
        return target
    return exe_from_command(text)


# --- installed programs --------------------------------------------------- #

def resolve_program_icon(name, display_icon="", install_location="",
                         uninstall_string="", max_depth=3):
    """Best available icon source for one installed program, or None.

    `display_icon` is the raw registry value. Kept as the first choice
    because it is what Control Panel shows. The install-location hunt is the
    fallback, and it is bounded — see the module docstring.
    """
    if not IS_WINDOWS:
        return None
    if display_icon:
        from app.icon_extract import _split_icon_spec
        try:
            path, index = _split_icon_spec(display_icon)
        except Exception:
            path, index = "", 0
        if path:
            real = os.path.expandvars(path)
            if os.path.isfile(real):
                return real + ("," + str(index) if index else "")
    folder = (install_location or "").strip().strip('"')
    if folder:
        found = find_icon_in_folder(os.path.expandvars(folder), name,
                                    max_depth=max_depth)
        if found:
            return found
    uninst = exe_from_command(uninstall_string)
    if uninst and os.path.isfile(uninst):
        found = find_icon_in_folder(os.path.dirname(uninst), name,
                                    max_depth=max_depth)
        if found:
            return found
    return None


# --- a real generic icon instead of a grey square -------------------------- #

def _as_rgb(value, fallback):
    """Accept '#rrggbb' or an (r, g, b) tuple.

    The dialogs pass COLORS['bg_alt'], which is a hex STRING, and
    int() on '#151B24' raises — which made every icon silently vanish
    instead of appearing. One coercion, used everywhere."""
    if isinstance(value, str):
        h = value.strip().lstrip('#')
        if len(h) == 6:
            try:
                return (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16))
            except ValueError:
                pass
        return fallback
    try:
        vals = tuple(int(c) for c in value)[:3]
        return vals if len(vals) == 3 else fallback
    except Exception:
        return fallback


def generic_app_icon_ppm(size=18, bg_rgb=(30, 32, 36),
                         fg_rgb=(126, 133, 143), accent_rgb=(86, 96, 110)):
    """A neutral "this program ships no icon" glyph, as PPM bytes.

    Some binaries genuinely have no icon resource: console programs
    (ffmpeg.exe) and the small JVM tools (jar.exe, jjs.exe, jfr.exe) that
    several installed-programs entries resolve to. A flat grey swatch reads
    as "this row is broken", which is a different claim from "this program has
    no artwork" — and the user was reading it as the former.

    Drawn in pure Python rather than fetched from the shell. Loading
    IDI_APPLICATION needs a MAKEINTRESOURCE pointer that behaved differently
    across the three ways it was tried here (LoadImageW, LoadIconW and
    SHGetFileInfoW all returned NULL on this machine), and a hand-drawn glyph
    has the advantage of being deterministic, dependency-free and testable —
    which matters more here than matching Explorer's exact pixels.

    Draws a small application window: a bordered frame, a title bar, and two
    content lines. Reads as a UI element, not as an error.
    """
    size = max(8, int(size))
    bg = _as_rgb(bg_rgb, (30, 32, 36))
    fg = _as_rgb(fg_rgb, (126, 133, 143))
    ac = _as_rgb(accent_rgb, (86, 96, 110))
    n = size
    px = [[bg for _ in range(n)] for _ in range(n)]

    left, top, right, bot = 1, 2, n - 2, n - 3
    if right - left < 4 or bot - top < 4:
        right, bot = n - 2, n - 2
    # title bar
    bar_bottom = top + max(2, (bot - top) // 3)
    for y in range(top, bar_bottom):
        for x in range(left, right + 1):
            px[y][x] = ac
    # two content lines
    l1 = bar_bottom + max(1, (bot - bar_bottom) // 3)
    l2 = l1 + max(1, (bot - bar_bottom) // 3)
    for y, x0, x1 in ((l1, left + 2, right - 2), (l2, left + 2, right - 3)):
        if y <= bot - 1 and x1 > x0:
            for x in range(x0, x1 + 1):
                px[y][x] = fg
    # frame last so it sits on top of everything
    for x in range(left, right + 1):
        px[top][x] = fg
        px[bot][x] = fg
    for y in range(top, bot + 1):
        px[y][left] = fg
        px[y][right] = fg

    out = bytearray()
    for row in px:
        for r, g, b in row:
            out += bytes((r, g, b))
    header = ("P6\n%d %d\n255\n" % (n, n)).encode("ascii")
    return header + bytes(out)
