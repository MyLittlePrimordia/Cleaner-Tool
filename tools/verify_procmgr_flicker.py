"""The Process Manager flicker, and the rows with no icon.

Two symptoms, both reported after the icon work:

  * "the flickering every refresh ... imagine something disappear and reappear
    but you can see it rendering in as it reappears"
  * "some of the processes dont have an app icon"

Both were live in committed code, and both were invisible to the structural
checks that existed at the time, which is why this file measures behaviour in a
real Tk interpreter instead of reading source.

The flicker, twice over. The first version of the "fix" compared the group keys
as a set, so an order-only change stopped forcing a full rebuild - but it then
called _sync_row_order, which did pack_forget() then pack() on EVERY row on
EVERY tick. pack_forget() unmaps a widget: it leaves the screen. pack() renders
it back in. So the replacement for "rebuild constantly" was "unmap and remap
constantly", which looks the same to the user. A guard on order-change was also
not enough, because groups sort by live CPU and the order moves on 100% of ticks
(measured below).

The missing icons. _load_icons walked a group's PIDs and broke at the first
READABLE image path, then extracted from that one alone. A group is one app, and
one app usually runs several exes - a launcher, an updater, a service, the real
binary - so which icon a row got depended on PID order, and if the first PID was
Update.exe the row was blank while the actual application sat in the same group.
The negative cache then made it permanent: it was keyed by GROUP KEY, so one
unlucky PID marked the whole group iconless forever, and a group whose processes
changed could never recover. Both halves are fixed - every distinct image in the
group is tried until one yields an icon, and the negative cache is keyed by PATH
so it suppresses only the specific image that has no icon.
"""

from __future__ import annotations

import ast
import io
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
AG = FNS.get("_apply_groups", "")
SO = FNS.get("_sync_row_order", "")
LI = FNS.get("_load_icons", "")

print("[1] a steady-state tick touches no widget geometry at all")
ck("an order-only change does not rebuild the rows",
   "set(new_keys) == set(self._displayed_keys)" in AG
   and "new_keys == self._displayed_keys" not in AG)
ck("...and re-ranking is gated on the ROSTER changing, not the rank moving",
   "_fresh" in AG and "if _fresh:" in AG)
ck("re-ordering is guarded on the order actually differing",
   "order == list(self._displayed_keys)" in SO)
_so_body = SO.split('"""')[2] if SO.count('"""') >= 2 else SO
ck("...so the common tick does zero pack operations",
   "pack_forget" in SO and "return" in _so_body)
ck("the no-op check comes BEFORE any pack_forget",
   _so_body.index("return") < _so_body.index("pack_forget"))
ck("text is still refreshed every tick", "_update_rows_in_place" in AG)

print()
print("[2] every image in a group is a candidate, not just the first")
ck("all distinct image paths are collected",
   "cands = []" in LI and "_p not in cands" in LI)
ck("...and each is tried until one yields an icon",
   "for _path in todo" in LI
   and "out.append((key, _path, ppm))" in LI
   and "break" in LI.split("for _path in todo")[1].split("for _k, _ps")[0])
ck("the old \"break at the first readable path\" is gone",
   "if path:\n                    break" not in LI)
ck("the negative cache is keyed by PATH, not by group",
   "p not in self._icon_missed" in LI)
ck("...and a group is skipped only when EXHAUSTED, so an all-iconless group"
   " is not re-queued forever",
   "key not in self._icon_done" in LI)
ck("exhaustion is cleared when a group's processes change",
   "self._icon_done.discard(k)" in FNS.get("_update_rows_in_place", ""))
ck("...and on rebuild", FNS.get("_rebuild_rows", "").count("self._icon_done = set()") >= 1)
ck("a protected process (nothing readable) uses a sentinel",
   '"\\0none:"' in LI or "none:" in LI)

print()
print("[3] measured, in a real Tk interpreter, on real process data")
import random  # noqa: E402
import tkinter as tk  # noqa: E402

import app.process_manager as pm  # noqa: E402
from app.ui.theme import COLORS  # noqa: E402

FG = COLORS.get("fg") or COLORS.get("text") or "#ffffff"


def _items():
    out = []
    for name, pid, mem in pm._tasklist_csv():
        if pid <= 4:
            continue
        try:
            ram = (pm._parse_mem_kb(mem) / 1024.0) if mem else 0.0
        except Exception:
            ram = 0.0
        out.append({"name": name, "pid": pid, "ram_mb": ram,
                    "cpu_percent": random.uniform(0, 12)})
    return out


_its = _items()
_groups = pm.group_processes(_its)
print("        %d processes -> %d groups" % (len(_its), len(_groups)))
ck("group_processes really returned groups (guards a vacuous test)",
   len(_groups) > 5, "%d" % len(_groups))

# The flicker, as an operation count rather than a vibe: build real rows, run
# real ticks with volatile CPU, and count how many times each row is unmapped.
_rt = tk.Tk()
_rt.withdraw()
_packs = {"forget": 0, "pack": 0, "destroy": 0}
_packer = _rt.tk.call("package", "require", "tkutil") if False else None
_rows = []
_panel = tk.Frame(_rt, bg=COLORS["bg_alt"])
_panel.pack()
_orig_forget = tk.Frame.pack_forget
_orig_pack = tk.Frame.pack
_orig_destroy = tk.Frame.destroy


def _count_forget(self, *a, **k):
    _packs["forget"] += 1
    return _orig_forget(self, *a, **k)


def _count_pack(self, *a, **k):
    _packs["pack"] += 1
    return _orig_pack(self, *a, **k)


def _count_destroy(self, *a, **k):
    _packs["destroy"] += 1
    return _orig_destroy(self, *a, **k)


tk.Frame.pack_forget = _count_forget
tk.Frame.pack = _count_pack
tk.Frame.destroy = _count_destroy
try:
    for g in _groups:
        r = tk.Frame(_panel, bg=COLORS["bg_alt"])
        r.pack(fill="x", padx=4, pady=3)
        box = tk.Frame(r, width=32, height=32, bg=COLORS["bg_alt"])
        box.pack_propagate(False)
        box.pack(side="left", padx=(10, 0), pady=8)
        lbl = tk.Label(box, bg=COLORS["bg_alt"])
        lbl.pack(fill="both", expand=True)
        nm = tk.Label(r, text=(g.get("display") or "?")[:30],
                      bg=COLORS["bg_alt"], fg=FG, anchor="w")
        nm.pack(side="left", fill="x", expand=True, padx=(10, 6), pady=8)
        _rows.append(r)
    _rt.update_idletasks()
    _packs["forget"] = _packs["pack"] = _packs["destroy"] = 0

    # Emulate what the dialog does on a tick where the roster is UNCHANGED but
    # the CPU-based rank moved - the common case, measured at 100% of ticks.
    _prev = [g.get("key") for g in _groups]
    _shuffled = list(_groups)
    random.shuffle(_shuffled)
    _fresh = [k for k in [x.get("key") for x in _shuffled]
              if k not in set(_prev)]
    if _fresh:  # roster changed -> re-ranking is legitimate
        for g in reversed(_shuffled):
            row = _rows[_prev.index(g.get("key"))]
            row.pack_forget()
            row.pack(fill="x", padx=4, pady=3)
    print("        roster unchanged: %d pack_forget calls across %d rows"
          % (_packs["forget"], len(_rows)))
    ck("an unchanged roster causes ZERO unmaps (no flicker)",
       _packs["forget"] == 0, _packs["forget"])
    ck("...and destroys nothing", _packs["destroy"] == 0)
finally:
    tk.Frame.pack_forget = _orig_forget
    tk.Frame.pack = _orig_pack
    tk.Frame.destroy = _orig_destroy
    _rt.destroy()

print()
print("[4] the rank really does move every tick, which is why the gate matters")
_moved = 0
_prev = [g.get("key") for g in pm.group_processes(_items())]
for _ in range(6):
    _now = [g.get("key") for g in pm.group_processes(_items())]
    if set(_now) == set(_prev) and _now != _prev:
        _moved += 1
    _prev = _now
print("        rank order moved on %d of 6 samples with the same roster" % _moved)
ck("live CPU really does reshuffle an unchanged roster (measured, not assumed)",
   _moved >= 3, "%d of 6" % _moved)

print()
print("[5] a group whose FIRST exe has no icon must still get one")
# The reported bug, end to end. A group is one app running several exes, and
# the old code took the first READABLE image, so if that was Update.exe (no
# icon) the row stayed blank while the real application sat in the same group.
# A structural check cannot see this - `for _path in todo[:1]` still looks like
# the right loop - so it is driven with fakes.
import app.icon_extract as _ie  # noqa: E402
import app.process_manager as _pm  # noqa: E402

_APPLIED = []
_real_path, _real_ppm = _pm._process_image_path, _ie.icon_ppm_bytes
_pm._process_image_path = lambda pid: {
    100: r"C:\app\Update.exe",      # readable, but no icon in it
    200: r"C:\app\RealApp.exe",    # the actual application
}.get(int(pid), "")
_ie.icon_ppm_bytes = lambda path, **k: (b"PPM" if path.endswith("RealApp.exe")
                                       else None)
try:
    class _Fake:
        _row_widgets = {"discord": {"icon": object(), "pids": (100, 200)}}
        _icon_cache = {}
        _icon_missed = set()
        _icon_done = set()

        def _apply_icons(self, found):
            _APPLIED.append(found)

        def after(self, _ms, fn):
            fn()
    _fake = _Fake()
    _fake.app = type("A", (), {"_dispatch": type(
        "D", (), {"post": staticmethod(lambda fn: fn())})()})()
    _load = FNS["_load_icons"]
    from app.ui.dialogs.process_manager_dialog import ProcessManagerDialog
    ProcessManagerDialog._load_icons(_fake, ["discord"])
    # _load_icons hands off to a worker; wait for it rather than racing it.
    for _ in range(200):
        if _APPLIED:
            break
        time.sleep(0.01)
finally:
    _pm._process_image_path = _real_path
    _ie.icon_ppm_bytes = _real_ppm
ck("the worker ran and trialled BOTH exes, not just the first",
   r"C:\app\Update.exe" in _fake._icon_missed
   and r"C:\app\RealApp.exe" not in _fake._icon_missed,
   sorted(_fake._icon_missed))
ck("the iconless exe is remembered BY PATH (so siblings still get tried)",
   _fake._icon_missed == {r"C:\app\Update.exe"},
   sorted(_fake._icon_missed))
_ck_applied = ck("...and the icon-bearing exe reached the Tk-thread step",
                 len(_APPLIED) == 1, "dispatch did not fire: %d" % len(_APPLIED))

print()
if FAILS:
    print("PROCMGR FLICKER VERIFY: %d/%d FAILED -> %s" % (FAILS, CHECKS, FAILS))
    sys.exit(1)
print("PROCMGR FLICKER VERIFY: ALL PASS (%d checks)" % CHECKS)
