"""LOW-001, LOW-011, LOW-012, LOW-013, LOW-014, INFO-005.

LOW-001  `GameServerPingDialog._poll` was DEFINED TWICE, functionally
         identical bodies, only the docstring differing - the second silently
         won, so the first was dead code. `_cancel` contained the identical
         cancel block twice, the second unreachable by construction. Benign
         rather than a bug, but a trap: a fix applied to the wrong copy does
         nothing with no error. Removed, and this test now fails if ANY class
         under app/ defines the same method twice.

LOW-011  `steam_games` passed a manifest-scraped `installdir` straight to
         os.path.join. ntpath.join discards everything before an ABSOLUTE
         component, so an installdir naming an absolute path yielded a folder
         equal to that absolute path, which then passed the isdir() check - a
         bogus picker entry and a spurious watched path.

LOW-012  One site did `icon.split(",")[0]`, truncating any install path
         containing a comma, so the app silently vanished from the picker. The
         correct version already existed further down the SAME file; both now
         share `_split_icon_index`.

LOW-013  Three counter collectors assign `self._hquery` and then run loops
         that can raise, returning with the PDH query open and no path to close
         it - one leaked handle per failed construction.

LOW-014  `storage_scan._is_link` was a third copy of the reparse-point rule
         that failed OPEN, had a dead INVALID_FILE_ATTRIBUTES check (no
         restype, so 0xFFFFFFFF arrives as -1), and used os.path.islink, which
         is False for a real junction. Now delegates to the one correct
         implementation.

INFO-005 The two tray busy flags were never initialised; their first read was
         a `getattr(..., False)`. That matters more after MED-014, where a
         dropped delivery left a flag True and killed the feature for the
         session.
"""

from __future__ import annotations

import ast
import inspect
import io
import ntpath
import os
import subprocess
import sys
import tempfile
import shutil

sys.path.insert(0, ".")

from app import game_catalog as gc           # noqa: E402
from app import hw_monitor                   # noqa: E402
from app import storage_scan                 # noqa: E402
import app.ui.app as appmod                  # noqa: E402

FAILS = 0
CHECKS = 0


def ck(label, cond, detail=""):
    global FAILS, CHECKS
    CHECKS += 1
    if cond:
        print("  PASS  %s" % label)
    else:
        FAILS += 1
        print("  FAIL  %s%s" % (label, ("  <- " + str(detail)) if detail else ""))


print("[1] LOW-001: no class under app/ defines a method twice")


def dname(d):
    return d.id if isinstance(d, ast.Name) else getattr(d, "attr", "")


dups = []
for root, dirs, files in os.walk("app"):
    dirs[:] = [d for d in dirs if d != "__pycache__"]
    for f in sorted(files):
        if not f.endswith(".py"):
            continue
        p = os.path.join(root, f)
        try:
            tree = ast.parse(io.open(p, encoding="utf-8").read())
        except SyntaxError:
            continue
        for node in tree.body:
            if not isinstance(node, ast.ClassDef):
                continue
            seen = {}
            for m in node.body:
                if isinstance(m, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    seen.setdefault(m.name, []).append(
                        (m.lineno, {dname(d) for d in m.decorator_list}))
            for name, info in seen.items():
                if len(info) < 2:
                    continue
                decs = set().union(*[s for _ln, s in info])
                # a @property / @x.setter pair is legitimate, not a duplicate
                if decs & {"property", "setter", "deleter", "getter"} and len(decs) > 1:
                    continue
                dups.append("%s %s.%s at %s" % (p, node.name, name,
                                                [i[0] for i in info]))
ck("no duplicate method definitions anywhere in app/", not dups, dups)

DIALOG = r"app\ui\dialogs\game_server_ping_dialog.py"
dsrc = io.open(DIALOG, encoding="utf-8", newline="").read()
dtree = ast.parse(dsrc)
dcls = next(n for n in dtree.body
            if isinstance(n, ast.ClassDef) and n.name == "GameServerPingDialog")
polls = [m for m in dcls.body
         if isinstance(m, ast.FunctionDef) and m.name == "_poll"]
ck("_poll is defined exactly once", len(polls) == 1, len(polls))
cancel = next(m for m in dcls.body
              if isinstance(m, ast.FunctionDef) and m.name == "_cancel")
cbody = dsrc.splitlines()[cancel.lineno - 1:cancel.end_lineno]
ck("_cancel has exactly one after_cancel (the unreachable copy is gone)",
   sum(1 for l in cbody if "after_cancel" in l) == 1,
   [l.strip() for l in cbody if "after_cancel" in l])
ck("the surviving _poll still reschedules itself",
   "self._dlg.after(80, self._poll)" in inspect.getsource(appmod) or
   "self._dlg.after(80, self._poll)" in dsrc)
ck("LOW-001 is documented where the duplicate was", "LOW-001" in dsrc)

print()
print("[2] LOW-011: an absolute or traversing installdir is refused")
ck("the premise holds: ntpath.join discards the prefix",
   ntpath.join(r"C:\Steam\steamapps", "common", r"C:\Windows") == r"C:\Windows",
   ntpath.join(r"C:\Steam\steamapps", "common", r"C:\Windows"))
ssrc = inspect.getsource(gc.steam_games)
ck("steam_games rejects an absolute installdir", "os.path.isabs(installdir)" in ssrc)
ck("...and a separator", '"\\\\" in installdir' in ssrc or '"\\" in installdir' in ssrc)
ck("...and a drive colon", '":" in installdir' in ssrc)
ck("...and dot segments", '"."' in ssrc and '".."' in ssrc)

# behavioural: feed it a hostile manifest in a temp Steam tree
tmp = tempfile.mkdtemp(prefix="low011_")
try:
    steamapps = os.path.join(tmp, "steamapps")
    common = os.path.join(steamapps, "common")
    os.makedirs(common, exist_ok=True)
    # a legit game
    real_game = os.path.join(common, "RealGame")
    os.makedirs(real_game, exist_ok=True)
    with open(os.path.join(steamapps, "appmanifest_good.acf"), "w",
              encoding="utf-8") as h:
        h.write('"AppState"\n{\n\t"name"\t\t"Good Game"\n'
                '\t"installdir"\t\t"RealGame"\n}\n')
    # a hostile one pointing at an absolute path
    with open(os.path.join(steamapps, "appmanifest_bad.acf"), "w",
              encoding="utf-8") as h:
        h.write('"AppState"\n{\n\t"name"\t\t"Evil"\n'
                '\t"installdir"\t\t"' + tmp + r'"\n}\n')
    real_libs = gc._steam_library_roots
    try:
        gc._steam_library_roots = lambda: [tmp]   # the LIBRARY ROOT:
        # steam_games appends "steamapps" itself
        games = gc.steam_games()
    finally:
        gc._steam_library_roots = real_libs
    names = [g["name"] for g in games]
    ck("the legitimate game is still found", "Good Game" in names, names)
    ck("the absolute-installdir entry is refused", "Evil" not in names, names)
    for g in games:
        ck("every folder is under steamapps\\common",
           os.path.normpath(g["folder"]).startswith(os.path.normpath(common)),
           g["folder"])
finally:
    shutil.rmtree(tmp, ignore_errors=True)

print()
print("[3] LOW-012: a comma in the install path no longer truncates it")
ck("the premise holds: a naive split truncates",
   r"C:\Program Files\Foo, Inc\app.exe".split(",")[0]
   == r"C:\Program Files\Foo",
   r"C:\Program Files\Foo, Inc\app.exe".split(",")[0])
CASES = [
    (r"C:\Program Files\Foo, Inc\app.exe", (r"C:\Program Files\Foo, Inc\app.exe", "")),
    (r"C:\Program Files\Foo\app.exe,0", (r"C:\Program Files\Foo\app.exe", "0")),
    (r"C:\a\b.exe,3", (r"C:\a\b.exe", "3")),
    (r"C:\Games\Bar, Baz\game.exe,2", (r"C:\Games\Bar, Baz\game.exe", "2")),
    ("", ("", "")),
]
bad = 0
for raw, want in CASES:
    got = gc._split_icon_index(raw)
    if got != want:
        bad += 1
        print("     got %r want %r" % (got, want))
ck("every comma/index case splits correctly", bad == 0, "%d wrong" % bad)
gsrc = inspect.getsource(gc)
def _naive_comma_splits(src):
    """Lines containing `X.split(",")[0]` in EXECUTABLE code.

    The first version of this check looked for a Subscript whose `.slice` was
    Constant(","), which is simply the wrong node: for `x.split(",")[0]` the
    Subscript's slice is Constant(0) and the "," is an argument of the inner
    Call. So the detector returned [] on a perfect example and the check was
    vacuous - it could never fail, and a sabotage confirmed it.
    """
    hits = []
    for node in ast.walk(ast.parse(src)):
        if not (isinstance(node, ast.Subscript)
                and isinstance(node.value, ast.Call)
                and isinstance(node.value.func, ast.Attribute)
                and node.value.func.attr == "split"):
            continue
        if any(isinstance(a, ast.Constant) and a.value == ","
               for a in node.value.args):
            hits.append(node.lineno)
    return hits


ck("the detector itself works (a positive control)",
   _naive_comma_splits('x = icon.split(",")[0]') != [],
   "the detector cannot see a naive split at all")
_naive = _naive_comma_splits(gsrc)
ck("no naive .split(\",\")[0] remains in executable code", not _naive, _naive)
ck("LOW-012 is documented", "LOW-012" in gsrc)

print()
print("[4] LOW-013: a failed setup closes the PDH query")
hsrc = inspect.getsource(hw_monitor)
ck("_close_pdh_query exists", hasattr(hw_monitor, "_close_pdh_query"))
ck("it is called on all three setup failure paths",
   hsrc.count("_close_pdh_query(self)") == 3, hsrc.count("_close_pdh_query(self)"))
ck("LOW-013 is documented", "LOW-013" in hsrc)
ck("the helper clears the field, not just the handle",
   "owner._hquery = None" in inspect.getsource(hw_monitor._close_pdh_query))


class _O:
    pass


o = _O()
o._hquery = None
ck("a None handle is a no-op and does not raise",
   hw_monitor._close_pdh_query(o) is False)
o2 = _O()
o2._hquery = "not-a-real-handle"
try:
    hw_monitor._close_pdh_query(o2)
    raised = None
except Exception as exc:
    raised = exc
ck("a bogus handle never raises", raised is None, raised)
ck("...and the field is cleared regardless", o2._hquery is None, o2._hquery)

def _body_without_docstring(fn_node, src):
    """Source of a function's statements, EXCLUDING its docstring.

    Needed because these docstrings deliberately describe the old broken code
    ("except Exception: return False", "0xFFFFFFFF", "ctypes") in order to
    explain the fix. A text search therefore "finds" the very patterns the fix
    removed. Checking the body is what actually tests the code.
    """
    lines = src.splitlines()
    st = fn_node.body[0]
    first, last = fn_node.lineno, fn_node.end_lineno
    if (isinstance(st, ast.Expr) and isinstance(st.value, ast.Constant)
            and isinstance(st.value.value, str)):
        first = st.end_lineno + 1
    return "\n".join(lines[first - 1:last])


print()
print("[5] LOW-014: the reparse guard is the correct one, and fails closed")
tmp2 = tempfile.mkdtemp(prefix="low014_")
try:
    real = os.path.join(tmp2, "Real")
    os.makedirs(real, exist_ok=True)
    open(os.path.join(real, "f.bin"), "w").close()
    j = os.path.join(tmp2, "Junction")
    made = subprocess.run(["cmd", "/c", "mklink", "/J", j, real],
                           capture_output=True).returncode == 0
    if made:
        ck("a real directory is not a link", storage_scan._is_link(real) is False)
        ck("a real JUNCTION is detected (os.path.islink alone would say False)",
           storage_scan._is_link(j) is True)
        ck("the premise: os.path.islink(junction) is False",
           os.path.islink(j) is False)
    ck("a missing path fails CLOSED (treated as a link, so the walk refuses)",
       storage_scan._is_link(os.path.join(tmp2, "nope")) is True)
    ssrc = io.open(r"app\storage_scan.py", encoding="utf-8", newline="").read()
    node = next(n for n in ast.walk(ast.parse(ssrc))
                if isinstance(n, ast.FunctionDef) and n.name == "_is_link")
    body = _body_without_docstring(node, ssrc)
    ck("_is_link delegates to the single correct helper",
       "_is_reparse_point" in body and "ctypes" not in body, body)
    ck("the fail-OPEN `except Exception: return False` is gone from the BODY",
       "return False" not in body, body)
    ck("the dead 0xFFFFFFFF check is gone from the BODY",
       "0xFFFFFFFF" not in body, body)
    ck("LOW-014 is documented", "LOW-014" in ast.get_docstring(node))
finally:
    shutil.rmtree(tmp2, ignore_errors=True)

print()
print("[6] INFO-005: the tray flags really exist on a fresh Application")
import tkinter as tk  # noqa: E402
from app.elevation import is_admin  # noqa: E402

root = tk.Tk()
root.withdraw()
app = appmod.Application(root)
try:
    ck("_tray_ping_busy is a real attribute (no getattr needed)",
       "_tray_ping_busy" in vars(app))
    ck("_tray_speedtest_busy is a real attribute",
       "_tray_speedtest_busy" in vars(app))
    ck("both start False", app._tray_ping_busy is False
       and app._tray_speedtest_busy is False)
    ck("_tray_action_lock exists exactly once per instance",
       hasattr(app, "_tray_action_lock"))
    asrc = inspect.getsource(appmod.Application.__init__)
    ck("...and is created in __init__",
       asrc.count("_tray_action_lock = threading.Lock()") == 1)
    ck("the whole module does not create a second, different lock",
       inspect.getsource(appmod).count("_tray_action_lock = threading.Lock()") == 1)
    ck("INFO-005 is documented", "INFO-005" in asrc)
finally:
    try:
        app._dispatch.stop()
        root.destroy()
    except Exception:
        pass

print()
if FAILS:
    print("LOW-001/011/012/013/014 + INFO-005 VERIFY: %d/%d FAILED" % (FAILS, CHECKS))
    sys.exit(1)
print("LOW-001/011/012/013/014 + INFO-005 VERIFY: ALL PASS (%d checks)" % CHECKS)
