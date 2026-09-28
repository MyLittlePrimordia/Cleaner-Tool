"""Process Manager rows had no app icons.

User request: "process manager doesnt have app icons for the processes this
would be an easy win and should be easy to add."

The pieces already existed - app/icon_extract, app/icon_resolve and the shared
ICON_ROW_PX added when the Install catalog and the two popups were brought to
the same icon size - so the work was in wiring, not in new machinery. Four
things had to be right:

  * The icon source is the RUNNING IMAGE, not the registry's DisplayIcon. For
    an Electron or store app the registry points at a launcher while the real
    icon lives in the .exe actually executing. app/icon_resolve documents the
    same reasoning for the Install tab, and the user's screenshot shows exactly
    those apps - Firefox, Discord, Steam, Opencode, a game.

  * The plate is the ROW's own colour. An extracted icon carries an opaque
    background rectangle, and if that does not match the widget it is drawn on
    it shows as a square border - the bug already found and fixed in the
    uninstall popup. Rows here are COLORS["bg_alt"].

  * Extraction is GDI work, so it happens on a worker and only the finished
    PhotoImage is handed to the Tk thread. A 3-second refresh must never pay
    for it twice: results are cached per group, and a rebuild - which destroys
    the widgets the images were attached to - drops the cache with them so dead
    PhotoImages are not pinned for the session.

  * The placeholder Label is created EMPTY so a row's width does not change
    when its picture lands. A row that jumps is more distracting than a row with
    no icon.
"""

from __future__ import annotations

import ast
import io
import re
import os
import sys
import time

sys.path.insert(0, ".")

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


DLG = r"app\ui\dialogs\process_manager_dialog.py"
PM = r"app\process_manager.py"
S = io.open(DLG, encoding="utf-8", newline="").read()
P = io.open(PM, encoding="utf-8", newline="").read()
FNS = {n.name: ast.get_source_segment(S, n) for n in ast.walk(ast.parse(S))
       if isinstance(n, ast.FunctionDef)}

print("[1] a row has somewhere to put an icon")
ck("the row widget dict records an icon", '"icon": icon_lbl' in S)
ck("an icon Label is created for every row", "icon_lbl = tk.Label(" in S)
_ctor = S.split("icon_lbl = tk.Label(", 1)[1].split(")", 1)[0]
ck("it is created with no image (rows must not reflow when one lands)",
   "image=" not in _ctor)
ck("...it inherits the row background", 'bg=COLORS["bg_alt"]' in _ctor)
ck("icons are requested for the rows on screen",
   "_load_icons(new_keys)" in FNS.get("_apply_groups", ""))

print()
print("[2] extraction is off the Tk thread, and only the image crosses back")
li = FNS.get("_load_icons", "")
ck("_load_icons exists", bool(li))
ck("it starts a named worker", "Thread(" in li and "ProcMgrIcons" in li)
ck("...and it creates no widget and no PhotoImage itself",
   "PhotoImage(" not in li and ".config(" not in li)
ck("the PhotoImage is built in the Tk-thread half", "_apply_icons" in S
   and "tk.PhotoImage(data=ppm)" in S)
# The GDI extraction itself must live in the WORKER half. A previous version
# of this test only checked that _load_icons created no PhotoImage, which a
# sabotage that merely added a no-op to _apply_icons satisfied without moving
# any work - so assert where icon_ppm_bytes is CALLED.
ck("the GDI extraction happens in the worker, not the Tk half",
   "icon_ppm_bytes(" in li and "icon_ppm_bytes(" not in FNS.get("_apply_icons", ""))
ck("the result is posted through the dispatcher", "_dispatch" in li)
ck("it reads self.app with an _app fallback",
   'getattr(self, "app", None)' in li and 'getattr(self, "_app", None)' in li)
ck("the PhotoImage is kept referenced, or Tk garbage-collects it",
   "lbl.image = img" in S)

print()
print("[3] the icon source is the running image, and the plate matches the row")
ck("_process_image_path exists", "_process_image_path" in P)
ck("it uses QueryFullProcessImageNameW", "QueryFullProcessImageNameW" in P)
ck("it binds the signatures (a 32-bit restype truncates the HANDLE)",
   "OpenProcess.restype" in P and "QueryFullProcessImageNameW.restype" in P)
ck("the plate is the ROW colour, not a hardcoded literal",
   'icon_bg_rgb(COLORS["bg_alt"])' in li and "(30, 32, 36)" not in li)
ck("...and it uses the shared ICON_ROW_PX for size parity with the catalog",
   "ICON_ROW_PX" in li)

print()
print("[4] the 3-second refresh never re-extracts")
ck("there is a per-group icon cache", "self._icon_cache" in S)
ck("a key already cached is not queued again", "key not in self._icon_cache" in li)
ck("a rebuild purges the cache, since it destroyed the widgets",
   FNS.get("_rebuild_rows", "").count("self._icon_cache = {}") == 1)

print()
print("[5] it works on this machine, on real processes")
import app.process_manager as pm  # noqa: E402
from app.icon_extract import icon_ppm_bytes  # noqa: E402
from app.ui.theme import ICON_ROW_PX, icon_bg_rgb, COLORS  # noqa: E402

ck("_process_image_path finds this very process",
   pm._process_image_path(os.getpid()).lower().endswith(".exe"),
   pm._process_image_path(os.getpid()))
ck("...and refuses nonsense without raising",
   pm._process_image_path(0) == "" and pm._process_image_path(-1) == "")

_rows = [r for r in pm._tasklist_csv() if r[1] > 1000]
_paths = []
for _n, _p, _m in _rows:
    _ip = pm._process_image_path(_p)
    if _ip:
        _paths.append(_ip)
    if len(_paths) >= 40:
        break
_bg = icon_bg_rgb(COLORS["bg_alt"])
_t0 = time.perf_counter()
_hits = 0
_sizes = set()
for _p in _paths:
    _ppm = icon_ppm_bytes(_p, size=ICON_ROW_PX, bg_rgb=_bg)
    if _ppm:
        _hits += 1
        _sizes.add(len(_ppm))
_dt = (time.perf_counter() - _t0) * 1000
print("        %d readable image paths, %d icons (%.0f%%), %.1f ms each"
      % (len(_paths), _hits, 100.0 * _hits / max(1, len(_paths)),
         _dt / max(1, len(_paths))))
ck("most user processes DO produce an icon", _hits >= len(_paths) * 0.4,
   "%d of %d" % (_hits, len(_paths)))
ck("every icon is the same rendered size (parity with the catalog)",
   len(_sizes) == 1, _sizes)
ck("a well-known binary always has one",
   bool(icon_ppm_bytes(r"C:\Windows\System32\notepad.exe",
                       size=ICON_ROW_PX, bg_rgb=_bg)))
ck("extraction is cheap enough to be invisible on a worker",
   _dt / max(1, len(_paths)) < 15, "%.1f ms" % (_dt / max(1, len(_paths))))

print()
print()
print("[6] the 3s refresh does not destroy and rebuild the rows")
ag = FNS.get("_apply_groups", "")
ck("membership is compared as a SET, not an ordered list",
   "set(new_keys) == set(self._displayed_keys)" in ag)
ck("...and the old ordered-list comparison is gone",
   "new_keys == self._displayed_keys" not in ag)
ck("order is re-synced instead of rebuilt",
   "_sync_row_order" in ag and "_rebuild_rows" in ag)
ck("a steady-state tick still updates text in place",
   "_update_rows_in_place" in ag)
so = FNS.get("_sync_row_order", "")
# Strip the docstring first: the method's own docstring names _rebuild_rows to
# explain what it does NOT do, so a substring check saw the word and failed on
# correct code. Text checks must not read the prose.
_so_body = so.split('"""')[2] if so.count('"""') >= 2 else so
ck("re-ordering re-packs widgets WITHOUT destroying them",
   "pack_forget" in so and "_rebuild_rows" not in _so_body)
ck("the icon slot is a FIXED size, so a late icon cannot reflow the row",
   "pack_propagate(False)" in S and "icon_box" in S)
# Tk measures a Label's width in CHARACTERS and its height in LINES. A fixed
# slot therefore CANNOT be held by the Label itself - `width=32, height=32`
# means 32 characters across and 32 LINES down, ~500 px per row, which with a
# few dozen apps made the panel tens of thousands of pixels tall and the popup
# render completely blank (found the hard way, reported as "popup is empty").
# A Frame's width/height are pixels, so the slot must be held by one.
ck("the fixed size is held by a Frame, whose units are pixels",
   re.search(r"tk\.Frame\(row,\s*width=ICON_ROW_PX,\s*height=ICON_ROW_PX", S)
   is not None)
ck("...and the Label inside it is given no text-unit width/height",
   "width=_ICON" not in S and "height=_ICON" not in S)
ck("a lookup that finds nothing is remembered (negative cache)",
   "self._icon_missed" in li)
# The negative cache is keyed by PATH now (see verify_procmgr_flicker.py - a
# group-keyed cache made one iconless exe permanently blank its whole group),
# so the queue gate is the EXHAUSTION set, not the path cache.
ck("...and an exhausted group is not queued again",
   "key not in self._icon_done" in li)
ck("a miss is recorded per PATH, and a sentinel for nothing-readable",
   "missed.append(_path)" in li and "none:" in li)
ck("...and exhaustion is recorded when a group yields no icon",
   "self._icon_done.update(done)" in li)
ck("rebuilding clears the negative cache too",
   FNS.get("_rebuild_rows", "").count("self._icon_missed = set()") == 1)

# The bug, stated as pure logic: an order-only change must NOT read as
# structural. This is the difference the user actually saw.
_older = ["a", "b", "c"]
_ranked = ["c", "a", "b"]
ck("an order-only change reads as different when compared as a list",
   _older != _ranked)
ck("...and as identical when compared as a set", set(_older) == set(_ranked))

# A live Tk measurement, because the bug that blanked the popup was invisible
# to every other kind of check: the code parsed, imported, and every structural
# assertion passed, while the panel was 18,000 px tall and showed nothing.
# Only the geometry says otherwise. Synthetic rows are used deliberately -
# an earlier version of this test asked group_processes() for real groups,
# got 0 back, and passed vacuously.
print()
print("[7] a real row is a sane height (the check that was missing)")
import tkinter as _tk  # noqa: E402
from app.ui.theme import ICON_ROW_PX as _PXR  # noqa: E402
_FG = COLORS.get("fg") or COLORS.get("text") or "#ffffff"
_NM = ["Discord", "steam", "firefox", "Code", "Opencode", "explorer",
       "nvcontainer", "SearchHost", "ShellExperienceHost", "Spotify"]
_rt = _tk.Tk(); _rt.withdraw()
def _build(reserve, w=0, h=0):
    _p = _tk.Frame(_rt, bg=COLORS["bg_alt"]); _p.pack(); _rs = []
    for _n in _NM:
        _row = _tk.Frame(_p, bg=COLORS["bg_alt"]); _row.pack(fill="x")
        if reserve:
            _b = _tk.Frame(_row, width=_PXR, height=_PXR, bg=COLORS["bg_alt"])
            _b.pack_propagate(False); _b.pack(side="left", padx=(10, 0), pady=8)
            _l = _tk.Label(_b, bg=COLORS["bg_alt"]); _l.pack(fill="both", expand=True)
        else:
            _l = _tk.Label(_row, bg=COLORS["bg_alt"], width=w, height=h)
            _l.pack_propagate(False); _l.pack(side="left", padx=(10, 0), pady=8)
        _t = _tk.Label(_row, text=_n, bg=COLORS["bg_alt"], fg=_FG, anchor="w")
        _t.pack(side="left", fill="x", expand=True, padx=(10, 6), pady=8); _rs.append(_row)
    _rt.update_idletasks(); _h = _p.winfo_reqheight()
    for _r in _rs: _r.destroy()
    _p.destroy(); _rt.update_idletasks(); return _h
_good, _bad = _build(True), _build(False, 32, 32)
_rt.destroy()
print("        %d rows: shipped layout %.0f px/row, Label width/height=32 -> %.0f px/row"
      % (len(_NM), _good/len(_NM), _bad/len(_NM)))
ck("a real row is a sane height", _good / len(_NM) < 60,
   "%.0f px/row" % (_good/len(_NM)))
ck("the old Label-units form really was the blank popup",
   _bad / len(_NM) > 200, "%.0f px/row" % (_bad/len(_NM)))
ck("36 apps would fit the popup at the shipped height",
   (_good/len(_NM)) * 36 < 2000, "%.0f px" % ((_good/len(_NM))*36))

if FAILS:
    print("PROCMGR ICONS VERIFY: %d/%d FAILED -> %s" % (FAILS, CHECKS, FAILS))
    sys.exit(1)
print("PROCMGR ICONS VERIFY: ALL PASS (%d checks)" % CHECKS)
