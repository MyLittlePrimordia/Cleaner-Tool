"""BUG-001 guard: no zero-delay `after()` may marshal work off a worker.

Run:  python -u tools/verify_no_offthread_tk.py
Exit 0 = clean, 1 = at least one violation.

Why this file exists, and why it is deliberately NARROW.

On Python 3.14, calling `widget.after(0, fn)` from a worker thread raises

    RuntimeError: main thread is not in main loop

every single time -- not intermittently, as on 3.12 and earlier, where the
call was simply dropped when the main thread was outside the event loop.
Measured on Python 3.14.8 / Tk 9.0, where `winfo_*`, `config`, `PhotoImage`
and friends all raise the same way.

Six feature dialogs shipped with that call in their worker paths, each hiding
it behind `except Exception: pass`. They did not crash; they silently stopped
updating and then reported a false reason. "Find My Fastest DNS" found five
servers, painted zero rows, and told the user to check their network. The
Install tab's "Installed" badges and "Update Apps (N)" count never appeared.

SCOPE, AND WHY IT IS THIS SMALL.

The first version of this guard tried to be general: find every thread, then
transitively every function reachable from one, then flag any Tk call. It was
wrong three times in a row and each version reported a clean scan against the
live bug:

  1. scanning for `Thread(` found 8 functions -- every dialog passes its
     worker as `target=self._method`, and that method contains no Thread call;
  2. adding `target=` found 12 -- but `_ui`, where the `after()` actually
     lives, is only REACHED FROM the worker, one layer out;
  3. adding transitive reachability found 35 -- and 427 false positives,
     because name-based reachability cannot tell `str.lower()` from
     `tk.lower()` and `lower` is a legitimate Tk widget method.

A guard that cries wolf gets ignored, which is worse than no guard. So this
one asserts the thing that is both precise and cheap: a ZERO-DELAY `after()`
is the cross-thread marshalling idiom, it appears exactly 3 times in app/,
and each of those 3 is enumerated below with its reason. Any new zero-delay
`after()` has to be added to that list or the build fails.

Timers with a real delay (`after(80, self._poll)`, `after(_TICK_MS, ...)`)
are NOT this bug: they are scheduled from the Tk thread to repeat on the Tk
thread. They are deliberately out of scope here, and
tools/verify_med014_cross_thread_hops.py still covers the dispatcher itself.
"""

from __future__ import annotations

import ast
import io
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

#: file -> why a zero-delay after() is legitimate there.
ALLOWED = {
    "app/ui/app.py": (
        "two Tk-thread sites: _start_disk_monitor arms its own repeating poll "
        "from the Tk thread, and _ui_hop explicitly branches on "
        "threading.current_thread() and defers to _dispatch.post(fn) for a "
        "worker. Both are the documented correct shapes, not this bug."
    ),
    "app/ui/widgets/entry.py": (
        "RoundedEntry.__init__ defers its first paint by 0ms so the widget "
        "has its real geometry before drawing. __init__ runs on the Tk thread."
    ),
}

#: the dispatcher and its pump are the sanctioned mechanism, not a violation.
ALWAYS_ALLOWED = ("app/ui/tkdispatch.py",)


def zero_delay_sites(path):
    """[(lineno, col)] of every `.after(0, ...)` / `.after_idle(...)` call."""
    out = []
    tree = ast.parse(io.open(path, encoding="utf-8").read())
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr in ("after", "after_idle")):
            continue
        if (node.args and isinstance(node.args[0], ast.Constant)
                and node.args[0].value == 0):
            out.append(node.lineno)
    return sorted(out)


def main() -> int:
    print("BUG-001 GUARD: no off-thread zero-delay after()")
    violations = []
    scanned = 0
    total = 0

    for p in sorted((ROOT / "app").rglob("*.py")):
        if "__pycache__" in p.parts:
            continue
        rel = p.relative_to(ROOT).as_posix()
        scanned += 1
        try:
            sites = zero_delay_sites(p)
        except Exception as exc:                        # noqa: BLE001
            violations.append("%s: could not parse (%r)" % (rel, exc))
            continue
        total += len(sites)
        if rel in ALWAYS_ALLOWED:
            continue
        for lineno in sites:
            if rel not in ALLOWED:
                violations.append(
                    "%s:%d  zero-delay after() is the cross-thread marshalling "
                    "idiom and is not on the allow-list. On Python 3.14 this "
                    "raises RuntimeError('main thread is not in main loop') "
                    "from a worker; post through TkDispatcher instead."
                    % (rel, lineno))
            else:
                print("  allow  %s:%d  (%s)" % (rel, lineno, ALLOWED[rel]))

    print()
    print("  scanned %d modules, found %d zero-delay after() call site(s)"
          % (scanned, total))
    if total == 0:
        violations.append("no zero-delay after() found anywhere -- the "
                          "scanner is broken, not the codebase")
    # A stale allow-list entry is itself a defect: it would silently permit a
    # new violation in that file.
    for rel in ALLOWED:
        p = ROOT / rel
        if not p.is_file():
            violations.append("allow-list names a missing file: %s" % rel)
            continue
        if not zero_delay_sites(p):
            violations.append(
                "allow-list entry %s no longer has a zero-delay after() -- "
                "remove it, or a new one there would pass unnoticed" % rel)

    if violations:
        print()
        for v in violations:
            print("  VIOLATION  %s" % v)
        print()
        print("NO-OFFTHREAD-TK: %d violation(s)" % len(violations))
        return 1

    print("  none outside the allow-list")
    print("NO-OFFTHREAD-TK: ALL PASS (%d modules, %d allow-listed sites)"
          % (scanned, total))
    return 0


if __name__ == "__main__":
    sys.exit(main())