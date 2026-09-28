"""tools.smoke: the GUI smoke suite, split by area (H16).

This used to be one 3,300-line tools/smoke_gui.py with a 2,900-line main().
It is now a driver plus twelve area modules. Nothing was rewritten — every
section body was moved byte for byte, and the section banners came with it so
the original structure stays greppable.

WHY SPLIT IT. The monolith had grown past the point where a failure told you
anything: an assert in the DNS section surfaced as "SMOKE TEST: FAIL" with no
indication of which of ~400 checks ran first. The area modules make the suite
readable and make it possible to run a subset while iterating.

ORDER IS PART OF THE CONTRACT. The areas are NOT independent. They share one
live Application: `shell` builds the tabs, `tabs` walks them, later areas
enter pages those same pages left in a particular state. `AREAS` below is the
order they must run in, and `run_all` is the only supported entry point.

Each area's `run(ctx)` binds the names `main()` used to provide as locals —
root, app, gui, is_admin, config_persist, _save_config_verified — from the
Ctx, then runs the original code. The Ctx exists so the dependency is written
down instead of being four closure variables nobody could see.
"""
from __future__ import annotations

import sys

#: (module name, one-line purpose) in the order they must run.
AREAS = [
    ("shell", "window init flow, admin gate, branding, per-tab icons"),
    ("tabs", "every tab/preset/custom/undo, install catalog, menu, status"),
    ("scorecard", "ScorecardDialog, drive awareness, run history"),
    ("features", "install profiles, Game Night, Drive Toolkit, Startup Manager"),
    ("tweaks", "Tweak Health Center, run-row pills"),
    ("storage", "Storage Insight dry-run guard, Low-Space Nudge"),
    ("dns", "Find My Fastest DNS"),
    ("hardware", "Hardware Monitor"),
    ("pilot", "Game Session Auto-Pilot"),
    ("dialogs", "modal registry vs Combobox popdown, PC Health Report Card"),
    ("tools", "Tools tab, Quick Tools popup"),
    ("config", "run-engine tab contract, legacy config migration"),
]

#: (name, passed) for every check the suite has run.
results = []


def check(name, fn):
    """Run one check, recording the outcome instead of raising.

    A bare assert in the middle of main() would abort the whole suite, so
    later areas would never run and one failure would hide the rest. This is
    why the monolith reported so little from a failure.
    """
    try:
        fn()
        results.append((name, True, ""))
    except Exception as e:
        results.append((name, False, f"{type(e).__name__}: {e}"))


class Ctx:
    """What every area is allowed to reach for.

    Deliberately small. The 4,300-line Application and the Tk root are here
    because the areas genuinely drive the real UI; nothing else should be
    added without a reason, because each addition is a shared dependency that
    has to be sequenced correctly.
    """

    __slots__ = ("root", "app", "gui", "is_admin", "config_persist",
                 "save_config_verified", "run_checks", "gate_sel_saved")

    def __init__(self, root, app, gui, is_admin, config_persist,
                 save_config_verified, run_checks, gate_sel_saved=None):
        self.root = root
        self.app = app
        self.gui = gui
        self.is_admin = is_admin
        self.config_persist = config_persist
        self.save_config_verified = save_config_verified
        self.run_checks = run_checks
        # The Admin Gate precondition has to be pinned BEFORE Application is
        # built (it reads config), but the matching restore has to happen
        # after the gate is passed, which is inside the `shell` area. That
        # value therefore crosses the driver/area boundary, and this is where
        # it crosses. `shell` consumes and clears it.
        self.gate_sel_saved = gate_sel_saved

    def __repr__(self):
        return "<Ctx root=%r app=%r>" % (self.root, self.app)


#: Areas that must run even when --only selects a subset. `shell` passes the
#: admin gate, which is what actually builds the tabs, and it is also where the
#: user's selected_tasks is restored. Skipping it does not just fail — it
#: leaves the config probe in the user's config file.
MANDATORY = ("shell",)


def run_all(ctx, only=None, verbose=False):
    """Run the areas in order. `only` restricts to named areas (for a quick
    iteration loop); it does NOT make them independent, so anything after the
    skipped ones still sees whatever state they left behind, and the suite can
    still fail for that reason rather than because of the area you asked for.
    """
    from importlib import import_module
    if only:
        missing = [m for m in MANDATORY if m not in only]
        if missing:
            print("  note: also running %s (mandatory)" % ", ".join(missing))
            only = set(only) | set(MANDATORY)
    for name, purpose in AREAS:
        if only and name not in only:
            continue
        if verbose:
            print("  -- %-10s %s" % (name, purpose))
        mod = import_module("tools.smoke." + name)
        before = len(results)
        mod.run(ctx)
        ran = len(results) - before
        if verbose:
            print("     %d check(s)" % ran)
    return results


def failures():
    return [(n, e) for n, ok, e in results if not ok]


def settle(ctx, dlg, seconds=0.5):
    """Pump the Tk loop for a moment, then clear a dialog's collected results.

    Was a local `_settle` inside smoke_gui's DNS section, which the Hardware
    Monitor section then reached for — the one genuine cross-area coupling in
    the monolith, and the reason the split needed this helper. A dialog
    constructor may still have a worker inside its first probe; the stop token
    makes it exit at the next check WITHOUT queueing paints, and the
    synchronous reset below overwrites anything it did queue.
    """
    import time as _t
    _dl = _t.monotonic() + seconds
    while _t.monotonic() < _dl:
        ctx.root.update()
        _t.sleep(0.01)
    dlg._results = {}
