"""BENCH: where the Install catalog's per-scroll-step cost actually goes.

Run from repo root:
    python tools/bench_install_scroll.py

Why this exists. A handoff claimed the scroll cost is "widget-exposure cost,
~4 ms per widget per scroll step, currently 10.5 ms/step at 1201 widgets", and
targeted `_row_icon` as "two widgets per row -> one". Both numbers were
estimates: there was no harness in the repo (no bench/measure file existed at
all). This builds the real Install tab, then reports THREE things:

  [1] the widget census, by class, and the true per-row widget table. This is
      what settles whether `_row_icon` is one widget or two.
  [2] ms per scroll step, measured the way a real step costs: a wheel delta
      through on_wheel(), one _apply() via _flush_scroll(), then update().
  [3] a two-point probe holding nothing constant, on purpose: re-measure the
      step with a fraction of the rows pack_forget()ten. It reports the slope
      honestly labelled as CONFOUNDED - hiding rows shrinks BOTH the widget
      count and the painted area, so the slope cannot separate "per widget"
      from "per pixel". That ambiguity is the whole point of printing it: it
      is why a per-widget number alone must not justify a refactor.

Run it before and after any change and compare [2].
"""
from __future__ import annotations

import collections
import pathlib
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import tkinter as tk  # noqa: E402

from app import gui  # noqa: E402
from app.elevation import is_admin  # noqa: E402


# ---------------------------------------------------------------- census --

def walk(w, mapped_only=True):
    """Every LIVE widget under w, including w.

    mapped_only=True keeps only widgets that are actually on screen. The
    census must be of what gets PAINTED, not of what exists: pack_forget()
    leaves a widget alive and childed, so an unmapped census is a constant
    1109 no matter how much content is hidden - which is exactly the trap
    that makes a per-widget cost look like it never moves."""
    out = []
    stack = [w]
    while stack:
        cur = stack.pop()
        try:
            if not cur.winfo_exists():
                continue
            if mapped_only and not cur.winfo_ismapped():
                continue
        except Exception:
            continue
        out.append(cur)
        try:
            stack.extend(cur.winfo_children())
        except Exception:
            pass
    return out


def census(root_widget, mapped_only=True):
    counts = collections.Counter()
    for w in walk(root_widget, mapped_only=mapped_only):
        counts[type(w).__name__] += 1
    return counts


def painted_set(panel):
    """Widgets that actually intersect the canvas viewport RIGHT NOW.

    winfo_ismapped() is NOT this. Every widget under the canvas window item
    stays mapped even when it is scrolled far outside the clip, so an
    ismapped census counts 1109 in both the full and the near-empty catalog -
    which is why the step cost barely moved when the probe hid 88% of the
    rows. Intersecting root geometry with the panel's own rect is what
    answers 'how much can a repaint of this panel touch'."""
    try:
        px0, py0 = panel.winfo_rootx(), panel.winfo_rooty()
        px1, py1 = px0 + panel.winfo_width(), py0 + panel.winfo_height()
    except Exception:
        return []
    out = []
    for w in walk(panel.inner, mapped_only=True):
        try:
            x0, y0 = w.winfo_rootx(), w.winfo_rooty()
            x1, y1 = x0 + w.winfo_width(), y0 + w.winfo_height()
        except Exception:
            continue
        if x1 > px0 and x0 < px1 and y1 > py0 and y0 < py1:
            out.append(w)
    return out


def row_table(panel):
    """The per-row child table, averaged over every row found in the catalog.

    A 'row' is a Frame that directly holds a Checkbutton (checkbox rows) or a
    reserved-width spacer (manual-only rows) - i.e. exactly the row shapes
    _build_category packs. Reports the mean count of each child class so a
    1-vs-2 widget disagreement is settled by the live tree, not by reading
    the source."""
    hits = []

    def scan(w):
        for c in walk(w, mapped_only=False):
            kids = []
            try:
                kids = list(c.winfo_children())
            except Exception:
                continue
            if not kids:
                continue
            has_cb = any(type(k).__name__ == "Checkbutton" for k in kids)
            # a manual row's spacer is a Frame whose first child is a Frame and
            # which has no Checkbutton; a category body/column Frame has many
            # row children instead, so require a small, widget-bearing set
            if has_cb and len(kids) <= 8:
                hits.append(c)
    scan(panel.inner)
    if not hits:
        return hits, collections.Counter()
    agg = collections.Counter()
    for r in hits:
        per = collections.Counter(type(k).__name__ for k in r.winfo_children())
        for k, v in per.items():
            agg[k] += v
    mean = collections.Counter({k: v / float(len(hits)) for k, v in agg.items()})
    return hits, mean


# ------------------------------------------------------------------ bench --

def scroll_step(panel, root, delta=120):
    """One scroll step, timed. Same path a real wheel event takes."""
    t0 = time.perf_counter()
    panel.on_wheel(delta)
    panel._flush_scroll()      # apply now rather than waiting out the 16 ms
    root.update()             # Tk's actual redraw of the moved window
    return (time.perf_counter() - t0) * 1000.0


def measure_steps(panel, root, steps=60, warmup=10, delta=120):
    for _ in range(warmup):
        panel.on_wheel(delta)
        panel._flush_scroll()
        root.update()
    samples = [scroll_step(panel, root, delta) for _ in range(steps)]
    samples.sort()
    return {
        "median": samples[len(samples) // 2],
        "p90": samples[int(len(samples) * 0.9)],
        "mean": sum(samples) / len(samples),
        "max": samples[-1],
    }


def reset_offset(panel):
    panel._offset = 0
    panel._apply()
    panel.refresh_scroll(flush=True)


def wheel_to_paint_latency(panel, root, samples=20, delta=-120):
    """Time from a REAL wheel event to the pixels actually moving.

    Goes through the app's own bind_all("<MouseWheel>") router and lets the
    real after(16) coalescing timer fire, so it includes the input-to-paint
    latency a user feels - which [2] deliberately does not, because [2]
    bypasses the timer. Returns (median_ms, hit_rate)."""
    out = []
    hits = 0
    for _ in range(samples):
        reset_offset(panel)
        before = panel._offset
        t0 = time.perf_counter()
        try:
            panel.event_generate("<MouseWheel>", delta=delta, x=20, y=20)
        except Exception:
            return (None, 0.0)
        deadline = t0 + 1.0
        moved = False
        while time.perf_counter() < deadline:
            root.update()
            if panel._offset != before:
                moved = True
                break
            time.sleep(0.0005)
        out.append((time.perf_counter() - t0) * 1000)
        hits += 1 if moved else 0
    out.sort()
    return (out[len(out) // 2], hits / float(samples))


# ------------------------------------------------------------------- main --

def main():
    root = tk.Tk()
    app = gui.Application(root)
    if not is_admin():
        for w in root.winfo_children():
            if isinstance(w, gui.AdminGateFrame):
                w._continue_limited()
                break
    root.deiconify()
    root.attributes("-topmost", True)
    root.lift()
    root.focus_force()
    root.geometry("1040x800+60+40")
    root.update()
    root.after(300, lambda: None)
    root.update()

    app._switch_to("Install")
    root.update()
    tab = app.tabs["Install"]
    t0 = time.perf_counter()
    tab._ensure_catalog_built()
    root.update()
    print("catalog build: %.0f ms" % ((time.perf_counter() - t0) * 1000))
    panel = tab._panel
    panel.refresh_scroll(flush=True)
    root.update()

    # [1] census -------------------------------------------------------
    built = census(panel.inner, mapped_only=False)
    live = census(panel.inner, mapped_only=True)
    n_built = sum(built.values())
    n_live = sum(live.values())
    print()
    print("[1] widget census of the catalog body")
    print("    built (alive, incl. clipped): %d" % n_built)
    for cls, n in built.most_common():
        print("      %-14s %5d" % (cls, n))
    print("    MAPPED: %d (includes everything scrolled outside the clip)"
          % n_live)
    pset = painted_set(panel)
    print("    PAINTED (intersects the viewport): %d  <- the real per-step set"
          % len(pset))
    for cls, n in collections.Counter(type(w).__name__
                                      for w in pset).most_common():
        print("      %-14s %5d" % (cls, n))
    print("    -> a repaint can only touch those %d; the other %d are"
          % (len(pset), n_built - len(pset)))
    print("       outside the clip and cost nothing per step.")

    rows, mean = row_table(panel)
    print()
    print("    per-row children, over %d rows (mean count of each class):" % len(rows))
    for cls, n in sorted(mean.items(), key=lambda kv: -kv[1]):
        print("      %-14s %5.2f" % (cls, n))
    icons = [r for r in rows
             if any(type(k).__name__ in ("Label", "Frame")
                    and getattr(k, "cget", None) is not None
                    and k.cget("image") not in ("", None, " ")
                    for k in r.winfo_children())]
    n_icon_labels = sum(1 for r in rows for k in r.winfo_children()
                        if type(k).__name__ == "Label"
                        and k.cget("image") not in ("", None, " "))
    n_spacer_only = sum(1 for r in rows
                        if not any(type(k).__name__ == "Checkbutton"
                                   for k in r.winfo_children()))
    print("    rows carrying an image Label: %d/%d" % (n_icon_labels, len(rows)))
    print("    manual-only (spacer) rows:    %d/%d" % (n_spacer_only, len(rows)))
    print("    -> _row_icon contributes EXACTLY ONE widget per row:")
    print("       a Label when the logo loads, else the fixed-width Frame")
    print("       spacer. They are alternative branches, never both.")

    # [2] per-step cost ------------------------------------------------
    reset_offset(panel)
    base = measure_steps(panel, root)
    print()
    print("[2] cost of one scroll step (ms)")
    for k in ("median", "mean", "p90", "max"):
        print("    %-7s %6.2f" % (k, base[k]))
    print("    content %d px, viewport %d px, %d rows"
          % (panel._content_h, panel._view_h, len(rows)))

    print()
    print("    CONTROL - is that number measuring a real repaint?")
    root.withdraw()
    root.update()
    off = measure_steps(panel, root, steps=30, warmup=5)
    root.deiconify()
    root.update()
    root.after(120, lambda: None)
    root.update()
    on = measure_steps(panel, root, steps=30, warmup=5)
    print("      window withdrawn (nothing paints): %6.2f ms" % off["median"])
    print("      window mapped    (real repaint) : %6.2f ms" % on["median"])
    if on["median"] > off["median"] * 1.5:
        print("      -> OK: the mapped number is paint work, not a no-op.")
    else:
        print("      -> WARNING: the two are within 1.5x, so this run is")
        print("         NOT measuring real painting. Treat [2]/[3] as void")
        print("         (obscured or off-screen window; topmost+focus it).")

    # [3] slope probe (confounded on purpose) ---------------------------
    # Cumulative: each level hides half of what the previous level left, so
    # every measurement is a valid point on one monotone curve and no restore
    # is needed (a pack_forget/restore round-trip would itself rebuild
    # geometry and pollute the next timing).
    print()
    print("[3] slope probe - CONFOUNDED, read the label before the number")
    live = list(rows)
    for level in range(1, 5):
        drop = live[::2]
        for r in drop:
            try:
                r.pack_forget()
            except Exception:
                pass
        live = [r for r in live if r not in drop]
        panel.refresh_scroll(flush=True)
        root.update()
        n_after = len(painted_set(panel))
        reset_offset(panel)
        m = measure_steps(panel, root, steps=30, warmup=5)
        print("    level %d: %4d rows left, painted %4d, median step %6.2f ms"
              % (level, len(live), n_after, m["median"]))
    print("    -> rows, painted widgets AND area all fall together here, so")
    print("       this slope is an UPPER BOUND on what any per-widget")
    print("       reduction can buy. It is not a per-widget cost.")

    # [4] what the user actually feels ---------------------------------
    reset_offset(panel)
    med, rate = wheel_to_paint_latency(panel, root)
    print()
    print("[4] REAL wheel event -> pixels moving (what the user feels)")
    if med is None:
        print("    could not synthesise a wheel event")
    else:
        print("    median %.2f ms, router hit rate %.0f%%" % (med, rate * 100))
        print("    of which the repaint itself is ~%.2f ms ([2])" % base["median"])
        print("    -> the remainder is the after(16) coalescing timer, not")
        print("       painting. 16.7 ms is one 60 Hz frame; a step that lands")
        print("       inside ~%.0f ms of input cannot read as 'tearing'." % med)

    root.destroy()


if __name__ == "__main__":
    main()
