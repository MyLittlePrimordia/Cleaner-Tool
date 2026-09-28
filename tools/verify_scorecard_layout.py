"""The Clean Complete card must never crush its own buttons.

Reported from a real 8-task run: the three buttons at the bottom of the card
were clipped by the dialog's lower edge — drawn, but only their top half, and
not clickable.

Cause. ThemedModal mirrors the main window's geometry, so the card is exactly
as tall as the app window. The body above the buttons (hero figure, the
before/after free-space bar, the result chips, the 30-day sentence, an
optional reboot banner) already spends most of that. The rows panel was packed
IN VISUAL ORDER and asked for a height proportional to the task count, so with
a long list it claimed space the buttons needed — and `pack`, given less room
than the sum of the requests, starves the LAST widget packed. Measured with
the old layout: at 12+ rows the button frame was 1 pixel tall.

Fix. Pack the button row FIRST with side="bottom" so its space is reserved
before anything else can take it, and let the rows panel be the only widget
that gives ground (fill="both", expand=True), with its own scrollbar covering
the overflow. Visual order is unaffected — side="bottom" draws at the bottom
regardless of when it is packed.

These checks measure the real geometry, and treat a 1-pixel-tall button row as
the failure it is. An earlier version of this probe only checked
`bottom <= height`, which the crushed frame passed trivially (y = -209 <= 600).
"""
from __future__ import annotations

import sys
import time

sys.path.insert(0, r"C:\Users\User\Desktop\Cleaner Tool")

import tkinter as tk

FAILS = []
N = [0]
GB = 1024 ** 3


def ck(label, cond, detail=""):
    N[0] += 1
    if cond:
        print("  PASS  %s" % label)
    else:
        FAILS.append(label)
        print("  FAIL  %s%s" % (label, ("  <- " + repr(detail)) if detail else ""))


def find_button_row(dlg):
    """The frame that holds the card's AnimatedButtons (all-Canvas children)."""
    found = []

    def walk(w):
        for c in w.winfo_children():
            if isinstance(c, tk.Frame):
                kids = c.winfo_children()
                if len(kids) >= 2 and all(isinstance(k, tk.Canvas) for k in kids):
                    found.append(c)
            walk(c)

    try:
        walk(dlg._dlg)
    except Exception:
        pass
    return found[-1] if found else None


import app.gui as gui
from app.elevation import is_admin
from app.ui.dialogs.scorecard_dialog import ScorecardDialog

root = tk.Tk()
root.withdraw()
app = gui.Application(root)
if not is_admin():
    for w in root.winfo_children():
        if isinstance(w, gui.AdminGateFrame):
            w._continue_limited()
            break

# a button row is usable only if it kept a real height AND sits inside the
# dialog. Both halves matter: the old bug showed up as a 1px frame.
MIN_BTN_H = 20


def measure(n_rows, with_again, needs_reboot=False):
    res = [("Task %d" % i, "ok", (i + 1) * 1024 * 1024, None) for i in range(n_rows)]
    card = ScorecardDialog(
        root, tab_name="Clean", mode="run", cancelled=False, results=res,
        total_bytes=4 * GB, before=(654 * GB, 1024 ** 4), after=(658 * GB, 1024 ** 4),
        needs_reboot=needs_reboot,
        on_run_again=(lambda: None) if with_again else None)
    for _ in range(140):
        try:
            root.update()
            card._dlg.update()
        except Exception:
            pass
        time.sleep(0.005)
    try:
        card._dlg.update_idletasks()
    except Exception:
        pass
    brow = find_button_row(card)
    if brow is None:
        return None
    y = brow.winfo_rooty() - card._dlg.winfo_rooty()
    h = brow.winfo_height()
    dh = card._dlg.winfo_height()
    try:
        card._close()
    except Exception:
        pass
    return {"n": n_rows, "y": y, "h": h, "dialog_h": dh, "bottom": y + h}


print("\n  rows | dialog_h | button y | button h | bottom | verdict")
cases = []
for n_rows, again, reboot in ((0, False, False), (1, False, False),
                              (3, True, False), (8, True, False),
                              (8, True, True), (12, True, False),
                              (20, True, False), (40, True, True)):
    m = measure(n_rows, again, reboot)
    cases.append(m)
    if m is None:
        print("   %4d | could not locate the button row" % n_rows)
        ck("n=%d button row located" % n_rows, False)
        continue
    ok = m["h"] >= MIN_BTN_H and 0 <= m["y"] and m["bottom"] <= m["dialog_h"]
    print("   %4d | %8d | %8d | %8d | %6d | %s"
          % (m["n"], m["dialog_h"], m["y"], m["h"], m["bottom"],
             "OK" if ok else "CLIPPED/CRUSHED"))
    ck("n=%-3d button row is usable (h>=%d, inside the dialog)"
       % (m["n"], MIN_BTN_H), ok, m)

# the button row must be steady, not sliding down as the list grows
heights = [m["h"] for m in cases if m]
ys = [m["y"] for m in cases if m]
ck("the button row keeps a constant height as the list grows",
   len(set(heights)) == 1, sorted(set(heights)))
ck("the button row does not drift down as the list grows",
   len(set(ys)) == 1, sorted(set(ys)))

# and the panel must still scroll rather than squash its own content
card = ScorecardDialog(root, tab_name="Clean", mode="run", cancelled=False,
                       results=[("T%d" % i, "ok", 1024, None) for i in range(30)],
                       total_bytes=GB, before=(654 * GB, 1024 ** 4),
                       after=(658 * GB, 1024 ** 4), needs_reboot=False,
                       on_run_again=lambda: None)
for _ in range(140):
    try:
        root.update(); card._dlg.update()
    except Exception:
        pass
    time.sleep(0.005)
try:
    ck("a 30-row list is scrollable, not truncated",
       bool(getattr(card._rows_panel, "scroll_enabled", False)),
       getattr(card._rows_panel, "scroll_enabled", None))
except Exception as exc:
    ck("rows panel scroll state readable", False, repr(exc))
try:
    card._close()
except Exception:
    pass

try:
    root.destroy()
except Exception:
    pass

print()
if FAILS:
    print("SCORECARD LAYOUT: %d/%d FAILED -> %s" % (len(FAILS), N[0], FAILS))
    sys.exit(1)
print("SCORECARD LAYOUT: ALL PASS (%d checks)" % N[0])
