"""VERIFY: the Install catalog's rows actually line up.

Run from repo root:  python tools/verify_install_row_alignment.py

Why this exists. The catalog's name column was ragged and nothing caught it.
The cause is in _row_icon: it subsamples a logo down to fit ICON_ROW_PX on its
LONG side and then packs the Label at whatever width that leaves, so the icon
slot is only ICON_ROW_PX wide for a square logo and narrower for a small or
non-square one. Measured on the live tree before this guard existed: icon slots
ranged 16-32 px and the app names started at TEN different x positions
(55-71 px) across 203 rows. _row_icon's own docstring claimed it "subsamples
to a ~size px box so every row stays the same height" - that was false for any
logo whose short side is under the size, because a 16x16 logo is never scaled
UP at all.

The fix pads each logo into a fixed ICON_ROW_PX square (aspect preserved,
centred, transparent padding) so the Label is always exactly that wide, still
ONE widget per row. Padding lives in the image, NOT in Label's width= option,
which is measured in characters - sizing a Label in text units is the mistake
that blanked the Process Manager popup (5aba301).

These are GEOMETRY checks against a real built tree, not source greps: the
bug is a layout fact and only a laid-out widget can answer it. run_all_checks
executes it as a subprocess, so its exit code is the verdict.
"""

from __future__ import annotations

import collections
import os
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

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


import tkinter as tk  # noqa: E402

from app import gui  # noqa: E402
from app.elevation import is_admin  # noqa: E402
from app.ui.theme import ICON_ROW_PX  # noqa: E402


def walk(w):
    out = []
    stack = [w]
    while stack:
        cur = stack.pop()
        try:
            if not cur.winfo_exists():
                continue
        except Exception:
            continue
        out.append(cur)
        try:
            stack.extend(cur.winfo_children())
        except Exception:
            pass
    return out


def catalog_rows(panel):
    """Every catalog row: a Frame holding a Checkbutton and few children.

    Same shape test the bench uses, so the two agree on what a row is."""
    hits = []
    for c in walk(panel.inner):
        try:
            kids = list(c.winfo_children())
        except Exception:
            continue
        if not kids or len(kids) > 8:
            continue
        if any(type(k).__name__ == "Checkbutton" for k in kids):
            hits.append(c)
    return hits


def build_install_tab(root):
    app = gui.Application(root)
    if not is_admin():
        for w in root.winfo_children():
            if isinstance(w, gui.AdminGateFrame):
                w._continue_limited()
                break
    root.deiconify()
    root.geometry("1040x800+60+40")
    root.update()
    root.after(300, lambda: None)
    root.update()
    app._switch_to("Install")
    root.update()
    tab = app.tabs["Install"]
    tab._ensure_catalog_built()
    root.update()
    tab._panel.refresh_scroll(flush=True)
    root.update()
    return app, tab


def main():
    root = tk.Tk()
    app, tab = build_install_tab(root)
    panel = tab._panel
    rows = catalog_rows(panel)
    ck("the catalog built a realistic number of rows", len(rows) > 50, len(rows))

    # [1] the end-to-end symptom: do the names share one left edge? ------
    print()
    print("[1] the names in each column share ONE left edge")
    by_col = collections.defaultdict(list)
    for r in rows:
        # the name link is the first text Label that carries no image
        for k in r.winfo_children():
            if type(k).__name__ == "Label" and k.cget("image") in ("", None, " "):
                # group by the COLUMN (the row's parent), not the row
                by_col[str(r.master)].append(k.winfo_x())
                break
    spreads = []
    for col, xs in by_col.items():
        spreads.append((max(xs) - min(xs), len(xs), col))
    for spread, n, col in sorted(spreads, reverse=True):
        ck("column with %3d rows is aligned" % n, spread == 0,
           "%d px of horizontal drift" % spread)
    ck("every catalog row is in some column", len(by_col) > 0, len(by_col))

    # [2] every icon slot is exactly ICON_ROW_PX wide ---------------------
    print()
    print("[2] every icon slot is exactly ICON_ROW_PX (%d px)" % ICON_ROW_PX)
    widths = collections.Counter()
    heights = collections.Counter()
    for r in rows:
        kids = list(r.winfo_children())
        icon = None
        for k in kids:
            if type(k).__name__ == "Label" and k.cget("image") not in ("", None, " "):
                icon = k
                break
        if icon is None:
            # the no-logo fallback: a fixed-width spacer Frame
            for k in kids:
                if type(k).__name__ == "Frame" and not k.winfo_children():
                    icon = k
                    break
        if icon is not None:
            widths[icon.winfo_reqwidth()] += 1
            heights[icon.winfo_reqheight()] += 1
    print("        icon slot widths:  %s" % dict(sorted(widths.items())))
    print("        icon slot heights: %s" % dict(sorted(heights.items())))
    ck("all icon slots are the same width", len(widths) == 1, dict(widths))
    ck("...and that width is ICON_ROW_PX", list(widths) == [ICON_ROW_PX], dict(widths))
    ck("all icon slots are the same height", len(heights) == 1, dict(heights))
    ck("...so every row is the same height, as the docstring claims",
       list(heights) == [ICON_ROW_PX], dict(heights))

    # [3] _row_icon is ONE widget per row, on every path ----------------
    print()
    print("[3] _row_icon builds exactly ONE widget, on every code path")
    # ask it directly on a scratch parent, for a spread of catalog names plus
    # a name with no asset at all (the fixed-width fallback branch)
    catalog_names = []
    try:
        from app.catalog_data import iter_catalog_entries as _entries  # type: ignore
        catalog_names = [e.get("name", "") for e in _entries()]
    except Exception:
        pass
    if not catalog_names:
        # fall back to the names already on the built rows
        for r in rows:
            for k in r.winfo_children():
                if type(k).__name__ == "Label" and k.cget("image") in ("", None, " "):
                    catalog_names.append(k.cget("text"))
                    break
    probe_names = [n for n in catalog_names if n][:40] + ["__no_such_app_for_probe__"]
    bad_w = []
    bad_px = []
    for nm in probe_names:
        scratch = tk.Frame(panel.inner, bg=panel._fill)
        scratch.pack()
        try:
            made = tab._row_icon(scratch, nm)
        except Exception as exc:
            bad_w.append((nm, "raised %s" % exc))
            scratch.destroy()
            continue
        root.update()
        added = [w for w in scratch.winfo_children()]
        if len(added) != 1:
            bad_w.append((nm, "%d widgets" % len(added)))
        else:
            if added[0].winfo_reqwidth() != ICON_ROW_PX:
                bad_px.append((nm, added[0].winfo_reqwidth()))
        scratch.destroy()
    ck("every probe name produced exactly one widget", not bad_w, bad_w[:4])
    ck("...and that one widget is ICON_ROW_PX wide", not bad_px, bad_px[:6])
    ck("the no-asset fallback still reserves ICON_ROW_PX (names can't shift)",
       not any(n.startswith("__no_such") and w != ICON_ROW_PX for n, w in bad_px),
       [b for b in bad_px if b[0].startswith("__no_such")])

    # [4] the padding is TRANSPARENT, not an opaque box -----------------
    # This is the check that keeps the fix honest: a padded PhotoImage whose
    # empty area came out opaque would draw a black square around every logo.
    # It tests the mechanism on a SYNTHETIC image, not on catalog content -
    # the corner of a square logo is its own opaque pixel, so "corner is
    # transparent" is the wrong assertion. What has to hold is that the band
    # AROUND a known-size inset is transparent while the inset itself is not.
    print()
    print("[4] the padded icon box is transparent, not a black square")
    probe = tk.PhotoImage(width=12, height=8)
    probe.put("#ff0000", to=(0, 0, 12, 8))       # fully opaque inset
    root.update()
    padded = None
    try:
        padded = tab._pad_icon_box(probe, ICON_ROW_PX)
    except Exception as exc:
        ck("_pad_icon_box exists and runs", False, repr(exc))
    if padded is not None:
        ck("_pad_icon_box returns a %dx%d box" % (ICON_ROW_PX, ICON_ROW_PX),
           (padded.width(), padded.height()) == (ICON_ROW_PX, ICON_ROW_PX),
           (padded.width(), padded.height()))
        ox, oy = (ICON_ROW_PX - 12) // 2, (ICON_ROW_PX - 8) // 2
        ck("the inset pixels are still opaque (the logo survived)",
           not bool(padded.transparency_get(ox + 1, oy + 1)))
        pad = [p for p in ((0, 0), (ICON_ROW_PX - 1, 0), (0, ICON_ROW_PX - 1),
                           (ICON_ROW_PX - 1, ICON_ROW_PX - 1))
               if bool(padded.transparency_get(*p))]
        ck("all four box corners are transparent (no black square)", len(pad) == 4,
           "only %d of 4" % len(pad))
    # an already-exact image must pass through untouched, not be re-boxed
    exact = tk.PhotoImage(width=ICON_ROW_PX, height=ICON_ROW_PX)
    exact.put("#00ff00", to=(0, 0, ICON_ROW_PX, ICON_ROW_PX))
    root.update()
    through = tab._pad_icon_box(exact, ICON_ROW_PX)
    ck("an already-%dpx image passes through unchanged" % ICON_ROW_PX,
       through is exact, "was re-boxed")

    print()
    root.destroy()
    if FAILS:
        print("INSTALL ROW ALIGNMENT: %d/%d FAILED" % (FAILS, CHECKS))
        sys.exit(1)
    print("INSTALL ROW ALIGNMENT: ALL PASS (%d checks)" % CHECKS)


if __name__ == "__main__":
    main()
