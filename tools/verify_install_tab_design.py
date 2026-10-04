"""Pin the two Install-tab design fixes: the 2-column Essentials layout and
the split of the Everyday Apps Pack into four individual installs.

Run:  python -u tools/verify_install_tab_design.py
Exit 0 = clean, 1 = at least one failure.

Why this exists:

1. Essentials was the ONLY divider in the Install tab still flowing its rows
   down a single column. Every catalog divider builds per-column frames on a
   uniform grid (installtab._CAT_COLUMNS == 2) and deals rows round-robin;
   _build_ess_group packed every row straight into one frame instead, so the
   runtime groups rendered about twice as tall as the categories around them
   and the tab read as ragged. Purely a visual asymmetry between two builders
   that look interchangeable, so nothing caught it.

2. The Everyday Apps Pack installed Calculator, Clock, Sticky Notes and Sound
   Recorder from ONE checkbox. That cannot express "I only want the Clock", and
   ticking it handed the user three further apps they had not asked for. It is
   now four independent tasks, each with its own Store id, capability check and
   icon.

The icons are part of the contract, not decoration: _row_icon() derives the
filename from the task LABEL (lowercased, non-alphanumerics stripped), so a
renamed row silently loses its logo and falls back to the generic swatch. This
check pins label -> filename -> file on disk for all four.
"""

from __future__ import annotations

import io
import os
import pathlib
import re
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# Isolate the user's real config before anything imports it.
os.environ["LOCALAPPDATA"] = tempfile.mkdtemp(prefix="install_design_")

FAILS: list[str] = []


def check(cond: bool, label: str, detail: str = "") -> bool:
    print("  %s  %s%s" % ("PASS" if cond else "FAIL", label,
                           ("  -- " + detail) if detail else ""))
    if not cond:
        FAILS.append(label)
    return cond


# --------------------------------------------------------------------------- #
# 1. Essentials is 2-column
# --------------------------------------------------------------------------- #
def verify_essentials_two_columns() -> None:
    print("\n[1] Essentials divider lays out in 2 columns")

    from app.dpi import enable_dpi_awareness
    enable_dpi_awareness()
    import tkinter as tk
    from app.ui.tabs import installtab as IT
    from app.tasks import Task

    check(IT.InstallTab._CAT_COLUMNS == 2,
          "_CAT_COLUMNS is 2",
          "got %r" % (IT.InstallTab._CAT_COLUMNS,))

    names = ["Visual C++ 2015-2022 Redistributable (x86/x64)",
             ".NET Desktop Runtime 8.0",
             ".NET Desktop Runtime 6.0",
             "DirectX End-User Runtime",
             "Java Runtime (AdoptOpenJDK 21)",
             "Java Runtime (AdoptOpenJDK 17)"]

    class _Stub:
        _CAT_COLUMNS = 2

        def __init__(self):
            self.ess_vars = {}
            self.vars = {}
            self._ess_groups = {}
            self.bundle_vars = {}
            self._cat_count_lbls = {}

        def _trace_install_var(self, var):
            pass

        def _row_icon(self, parent, label):
            pass

    root = tk.Tk()
    root.geometry("1000x600")
    parent = tk.Frame(root, bg=IT.COLORS["bg_alt"])
    parent.pack(fill="both", expand=True)

    tasks = [Task("ess_check_%d" % i, n, "d", lambda ctx: None,
                  default=False, admin_required=False)
             for i, n in enumerate(names)]
    host = _Stub()
    IT.InstallTab._build_ess_group(host, parent, "EssentialsCheck",
                                   "- probe", IT.COLORS["accent_blue"], tasks)
    root.update_idletasks()
    root.update()

    body = host._ess_groups["EssentialsCheck"]["body"]
    cols = [w for w in body.winfo_children() if w.winfo_manager() == "grid"]
    check(len(cols) == 2, "body holds 2 column frames", "got %d" % len(cols))

    xs, placed = set(), 0
    for cf in cols:
        rows = [w for w in cf.winfo_children() if w.winfo_manager() == "pack"]
        xs.add(cf.winfo_rootx())
        placed += len(rows)
    check(len(xs) == 2, "the 2 columns are at DIFFERENT x positions",
          "xs=%s" % sorted(xs))
    check(placed == len(names),
          "every task row is placed inside a column",
          "%d/%d" % (placed, len(names)))

    # rows must NOT be direct children of body any more (that was the bug)
    direct = [w for w in body.winfo_children() if w.winfo_manager() == "pack"]
    check(not direct,
          "no row is packed straight into the single-column body",
          "%d direct pack children" % len(direct))
    root.destroy()


# --------------------------------------------------------------------------- #
# 2. Everyday Apps split into four, each with a real icon
# --------------------------------------------------------------------------- #
EXPECT = {
    "install_calculator":     ("9WZDNCRFHVN5", "Windows Calculator",
                               "has_calculator_app", "calculator.png"),
    "install_clock":          ("9WZDNCRFJ3PR", "Windows Clock",
                               "has_clock_app", "clock.png"),
    "install_sticky_notes":   ("9NBLGGH4QGHW", "Microsoft Sticky Notes",
                               "has_sticky_notes", "stickynotes.png"),
    "install_sound_recorder": ("9WZDNCRFHWKN", "Windows Sound Recorder",
                               "has_sound_recorder", "soundrecorder.png"),
}


def verify_everyday_split() -> None:
    print("\n[2] Everyday Apps are 4 independent installs")

    from app.tasks.install_tasks import TASKS, _EVERYDAY_APPS

    by_key = {t.key: t for t in TASKS}
    check("install_everyday" not in by_key,
          "the old combined pack row is gone")

    for key, (store_id, store_label, capfn, icon) in EXPECT.items():
        t = by_key.get(key)
        if not check(t is not None, "%s is registered" % key):
            continue

        entry = None
        for _name, (sid, lbl, chk) in _EVERYDAY_APPS.items():
            if sid == store_id:
                entry = (sid, lbl, chk)
        if not check(entry is not None,
                     "%s has an _EVERYDAY_APPS entry" % key):
            continue

        check(entry[0] == store_id and entry[1] == store_label,
              "%s -> Store id/title correct" % key,
              "%s / %r" % (entry[0], entry[1]))
        check(entry[2].__name__ == capfn,
              "%s -> capability check correct" % key, entry[2].__name__)
        check(t.admin_required is False,
              "%s -> not admin-gated" % key)

        # the filename _row_icon will derive from this label
        base = re.sub(r"\s*\(.*?\)", "", t.label)
        base = re.sub(r"[^a-z0-9]", "", base.lower()) + ".png"
        check(base == icon,
              "%s label derives its icon filename" % key,
              "%r -> %s" % (t.label, base))

        p = pathlib.Path(ROOT) / "app" / "assets" / "apps" / icon
        check(p.is_file(), "icon file present: %s" % icon)


def verify_icons_load() -> None:
    print("\n[3] the four icons actually load in Tk")

    from app.dpi import enable_dpi_awareness
    enable_dpi_awareness()
    import tkinter as tk
    from app.tasks.install_tasks import TASKS
    from app.ui.tabs.installtab import ICON_ROW_PX
    from app.utils import resolve_asset_path
    import math

    by_key = {t.key: t for t in TASKS}
    root = tk.Tk()
    root.withdraw()
    for key, (_sid, _lbl, _cap, icon) in EXPECT.items():
        t = by_key.get(key)
        if t is None:
            continue
        base = re.sub(r"\s*\(.*?\)", "", t.label)
        base = re.sub(r"[^a-z0-9]", "", base.lower()) + ".png"
        path = resolve_asset_path(os.path.join("apps", base))
        if not check(bool(path), "resolve_asset_path found %s" % base):
            continue
        try:
            img = tk.PhotoImage(file=path)
            f = max(1, int(math.ceil(max(img.width(), img.height())
                                     / float(ICON_ROW_PX))))
            if f > 1:
                img = img.subsample(f, f)
            check(img.width() > 0 and img.height() > 0,
                  "%s renders non-empty" % t.label,
                  "%dx%d" % (img.width(), img.height()))
        except Exception as exc:
            check(False, "%s renders non-empty" % t.label, str(exc))
    root.destroy()


def main() -> int:
    verify_essentials_two_columns()
    verify_everyday_split()
    verify_icons_load()
    print("\n%s" % ("ALL PASS" if not FAILS
                    else "FAILURES: %d\n  - %s"
                    % (len(FAILS), "\n  - ".join(FAILS))))
    return 1 if FAILS else 0


if __name__ == "__main__":
    raise SystemExit(main())
