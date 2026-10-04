"""Architecture guard — the invariants the H4-H14 split established.

Run it like any other tool:  python tools/check_architecture.py
Exit code 0 = clean, 1 = at least one violation.

Why this exists: every regression caught during the split was the same
failure wearing a different hat — a name or a path that used to be correct
because it lived in app/gui.py and stopped being correct the moment the code
moved. The split was mechanical, so the mechanical mistakes are the ones worth
pinning. Each check below corresponds to a bug that actually shipped during
the refactor:

  1  app.gui contract        tools/smoke_gui.py imports ~40 names from app.gui
                            through __import__("app.gui", ...). Losing one
                            breaks the suite in a way that reads like a
                            product bug.
  2  layering                nothing below base may import a layer above it,
                            and a widget may never import a dialog. This is
                            what keeps app/ui/app.py the only composition root.
  3  asset resolution        `Path(__file__).with_name("assets")` is only
                            correct for a module sitting directly in app/.
                            Anything under app/ui/ silently loads no icons.
                            This shipped three times (app.py, toolstab.py,
                            chrome.py x2) and the tab-icon test caught it.
  4  facade size             app/gui.py is a re-export shim now. If code is
                            being added back to it, the split has been undone
                            by accident.
  5  import cycles           app/ was made acyclic in H1-H3; the one remaining
                            2-cycle (hardware.mmdev <-> hardware.capture) is
                            deliberate and deferred, so it is allow-listed.
  6  Tk-free hardware/       app/ui/hardware is imported by the headless
                            scheduler, so a stray `import tkinter` there
                            makes `python app/__main__.py --auto-clean` pay
                            for a GUI it never opens.

Stdlib only, no imports of the app itself at module scope (the checks parse
source rather than importing it, so this runs before the app can even load).
"""
from __future__ import annotations

import ast
import io
import os
import pathlib
import re
import symtable
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
APP = ROOT / "app"
UI = APP / "ui"

# --------------------------------------------------------------------------- #
# 1  the app.gui surface the smoke suite depends on
# --------------------------------------------------------------------------- #
GUI_CONTRACT = [
    # theme
    "COLORS", "F", "TAB_ACCENTS", "_lerp", "_hex_lerp",
    "_round_rect_points", "_round_pts", "_measure_font",
    # base
    "ThemedModal", "Tooltip", "_ModalScrim",
    "_themed_askyesno", "_themed_showinfo",
    # widgets
    "AnimatedButton", "AnimatedProgressBar", "GaugeDial", "RoundedEntry",
    "ScrollableRoundedPanel", "SegmentedTabs", "ToggleSwitch",
    "BorderlessChrome", "_enable_dark_titlebar", "_set_window_icon",
    # hardware
    "_drive_gains", "_snapshot_all_drives", "_system_drive_root",
    "_xinput_state_fn", "_xinput_rumble", "_XINPUT_BUTTONS",
    "_XINPUT_RUMBLE_MS", "_XINPUT_STICK_DEADZONE",
    "_XINPUT_TRIGGER_THRESHOLD", "_InputMeter", "_PadReportStats",
    "_mm_guid", "_mm_vfn", "_MM_STATE_NAMES", "_MM_COM_INIT",
    "_capture_inputs", "_default_capture_id",
    # shell
    "Application", "AdminGateFrame", "TaskTab", "InstallTab", "ToolsTab",
    "launch",
]

# --------------------------------------------------------------------------- #
# 2  layering, bottom-up. A module may import its own layer and any LOWER one.
# --------------------------------------------------------------------------- #
LAYERS = ["app.ui.hardware", "app.ui.theme", "app.ui.widgets", "app.ui.base",
          "app.ui.dialogs", "app.ui.tabs", "app.ui.app"]


def layer_of(mod: str) -> int:
    """Index in LAYERS, or -1 for anything outside the app/ui stack."""
    best = -1
    for i, name in enumerate(LAYERS):
        if mod == name or mod.startswith(name + "."):
            if i > best:
                best = i
    return best


# pilot_dialog -> catalog_picker_dialog and installtab -> install_profiles_dialog
# both predate the split. They are real dialog->dialog opens, not layering bugs.
LAYER_EXCEPTIONS = {
    ("app.ui.dialogs.pilot_dialog", "app.ui.dialogs.catalog_picker_dialog"),
    ("app.ui.tabs.installtab", "app.ui.dialogs.install_profiles_dialog"),
}

# --------------------------------------------------------------------------- #
# 5  the one deliberate cycle, both directions
# --------------------------------------------------------------------------- #
CYCLE_EXCEPTIONS = {frozenset(("app.ui.hardware.mmdev",
                                "app.ui.hardware.capture"))}

# --------------------------------------------------------------------------- #
# 4  app/gui.py is a facade. It was 265 lines right after the split; give it
#     headroom for docstrings but not for new behaviour.
# --------------------------------------------------------------------------- #
GUI_LINE_BUDGET = 400


def py_files(*dirs):
    """Every .py under each dir. A dir may also be a single .py path."""
    for d in dirs:
        d = pathlib.Path(d)
        cands = [d] if d.is_file() else sorted(d.rglob("*.py"))
        for p in cands:
            if p.is_file() and "__pycache__" not in p.parts:
                yield p


def rel(p: pathlib.Path) -> str:
    return str(p.relative_to(ROOT)).replace("\\", "/")


def modname(p: pathlib.Path) -> str:
    r = rel(p)
    if r.endswith("/__init__.py"):
        r = r[: -len("/__init__.py")]
    elif r.endswith(".py"):
        r = r[:-3]
    return r.replace("/", ".")


def imports_of(p: pathlib.Path):
    """(module, lineno) for every `import x` / `from x import y`, absolute only."""
    src = io.open(p, encoding="utf-8").read()
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.Import):
            for a in node.names:
                yield a.name, node.lineno
        elif isinstance(node, ast.ImportFrom):
            if node.level:  # relative import: stays inside the package
                continue
            if node.module:
                yield node.module, node.lineno


def check(cond, ok, bad, failures, label):
    if cond:
        print("  PASS  %s" % label)
    else:
        failures.append(bad)
        print("  FAIL  %s" % label)
        for b in bad:
            print("          %s" % b)


def main() -> int:
    failures = []
    files = list(py_files(APP))

    # --- 1 ---------------------------------------------------------------
    print("\n[1] app.gui public contract (%d names)" % len(GUI_CONTRACT))
    gui_ns = set()
    for p in py_files(APP / "gui.py"):
        tree = ast.parse(io.open(p, encoding="utf-8").read())
        for node in ast.walk(tree):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                for a in node.names:
                    gui_ns.add((a.asname or a.name).split(".")[0])
            elif isinstance(node, (ast.FunctionDef, ast.ClassDef)):
                gui_ns.add(node.name)
            elif isinstance(node, ast.Assign):
                for t in node.targets:
                    if isinstance(t, ast.Name):
                        gui_ns.add(t.id)
    missing = [n for n in GUI_CONTRACT if n not in gui_ns]
    # `from app.ui.dialogs import *` is re-exported via __all__ there
    if not missing:
        dlg_init = ast.parse(io.open(UI / "dialogs" / "__init__.py",
                                     encoding="utf-8").read())
        for node in ast.walk(dlg_init):
            if isinstance(node, ast.Assign) and any(
                    isinstance(t, ast.Name) and t.id == "__all__" for t in node.targets):
                gui_ns |= {e.value for e in node.value.elts}
                missing = [n for n in GUI_CONTRACT if n not in gui_ns]
    check(not missing, GUI_CONTRACT, ["app/gui.py no longer exposes: %s" % m
                                      for m in missing], failures,
          "every name tools/smoke_gui.py imports still resolves")

    # --- 2 ---------------------------------------------------------------
    print("\n[2] layering (nothing imports upward)")
    bad = []
    for p in list(py_files(UI)):
        me = layer_of(modname(p))
        if me < 0:
            continue
        for mod, ln in imports_of(p):
            other = layer_of(mod)
            if other < 0 or other <= me:
                continue
            if (modname(p), mod) in LAYER_EXCEPTIONS:
                continue
            bad.append("%s:%d imports %s (upward)" % (rel(p), ln, mod))
    # a widget importing a dialog at all is always wrong
    for p in list(py_files(UI / "widgets")):
        for mod, ln in imports_of(p):
            if mod.startswith("app.ui.dialogs") or mod.startswith("app.ui.tabs") \
                    or mod == "app.ui.app":
                bad.append("%s:%d widget imports %s" % (rel(p), ln, mod))
    check(not bad, [], bad, failures, "no upward imports; no widget->dialog")

    # --- 3 ---------------------------------------------------------------
    print("\n[3] asset resolution goes through utils.resolve_asset_path")
    bad = []
    for p in py_files(UI):
        tree = ast.parse(io.open(p, encoding="utf-8").read())
        for node in ast.walk(tree):
            # Match the AST, not the text: a docstring that *talks about*
            # __file__ is documentation, not a broken path lookup.
            if not (isinstance(node, ast.Name) and node.id == "__file__"
                    and isinstance(node.ctx, ast.Load)):
                continue
            line = io.open(p, encoding="utf-8").read().splitlines()[node.lineno - 1]
            if "assets" in line or "icon" in line.lower():
                bad.append("%s:%d  %s" % (rel(p), node.lineno, line.strip()[:78]))
    check(not bad, [], bad, failures,
          "no __file__-relative asset path outside app/ (silently loads nothing)")

    # --- 4 ---------------------------------------------------------------
    print("\n[4] app/gui.py stays a facade")
    n = len(io.open(APP / "gui.py", encoding="utf-8").read().splitlines())
    check(n <= GUI_LINE_BUDGET, GUI_LINE_BUDGET,
          ["app/gui.py is %d lines (budget %d) — new behaviour belongs in "
           "app/ui/, not back in the facade" % (n, GUI_LINE_BUDGET)],
          failures, "facade is %d lines (budget %d)" % (n, GUI_LINE_BUDGET))

    # --- 5 ---------------------------------------------------------------
    print("\n[5] no import cycles in app/")
    graph = {}
    for p in files:
        src = modname(p)
        for mod, _ln in imports_of(p):
            target = mod
            # `from app.utils import x` -> app.utils
            if not any(rel(q)[:-3].replace("/", ".") == mod
                       for q in py_files(APP)):
                continue
            graph.setdefault(src, set()).add(target)

    def sccs(nodes):
        index, low, on, stack, out = {}, {}, set(), [], []
        counter = [0]

        def strong(v):
            work = [(v, iter(graph.get(v, ())))]
            index[v] = low[v] = counter[0]
            counter[0] += 1
            on.add(v)
            stack.append(v)
            while work:
                node, it = work[-1]
                advanced = False
                for w in it:
                    if w not in graph:
                        continue
                    if w not in index:
                        index[w] = low[w] = counter[0]
                        counter[0] += 1
                        on.add(w)
                        stack.append(w)
                        work.append((w, iter(graph.get(w, ()))))
                        advanced = True
                        break
                    if w in on:
                        low[node] = min(low[node], index[w])
                if not advanced:
                    work.pop()
                    if work:
                        parent = work[-1][0]
                        low[parent] = min(low[parent], low[node])
                    if low[node] == index[node]:
                        comp = []
                        while True:
                            w = stack.pop()
                            on.discard(w)
                            comp.append(w)
                            if w == node:
                                break
                        if len(comp) > 1:
                            out.append(comp)

        for v in nodes:
            if v not in index:
                strong(v)
        return out

    bad = []
    for comp in sccs(sorted(graph)):
        for i, a in enumerate(comp):
            for b in comp[i + 1:]:
                if frozenset((a, b)) in CYCLE_EXCEPTIONS:
                    continue
                bad.append("cycle: %s <-> %s" % (a, b))
    check(not bad, [], bad, failures, "app/ is acyclic (deferred 2-cycle allow-listed)")

    # --- 6 ---------------------------------------------------------------
    print("\n[6] app/ui/hardware stays Tk-free (headless runs import it)")
    bad = []
    for p in py_files(UI / "hardware"):
        for node in ast.walk(ast.parse(io.open(p, encoding="utf-8").read())):
            names = []
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module]
            for n2 in names:
                if n2.split(".")[0] in ("tkinter", "tk"):
                    bad.append("%s:%d imports %s" % (rel(p), node.lineno, n2))
    check(not bad, [], bad, failures, "no tkinter import under app/ui/hardware/")

    # --- 7 ---------------------------------------------------------------
    print("\n[7] the run-lifecycle state machine stays a state machine")
    bad = []
    app_py = UI / "app.py"
    tree = ast.parse(io.open(app_py, encoding="utf-8").read())
    cls = next((n for n in tree.body
                if isinstance(n, ast.ClassDef) and n.name == "Application"), None)
    if cls is None:
        bad.append("app/ui/app.py no longer defines Application")
    else:
        def deco_names(fn):
            out = []
            for d in fn.decorator_list:
                if isinstance(d, ast.Name):
                    out.append(d.id)
                elif isinstance(d, ast.Attribute):
                    out.append(d.attr)
            return out

        defs = {}
        for m in cls.body:
            if isinstance(m, ast.FunctionDef):
                defs.setdefault(m.name, []).append(m)

        # _busy MUST stay a property. If someone "simplifies" it back to a
        # plain bool attribute, _state silently stops being the source of
        # truth and the whole H9 migration quietly reverts — with no test
        # failure, because every read still returns a bool.
        # In the AST a property is a FunctionDef carrying @property (and a
        # second one carrying @<name>.setter), NOT a property object.
        busy_defs = defs.get("_busy", [])
        names = [deco_names(f) for f in busy_defs]
        has_getter = any("property" in n for n in names)
        has_setter = any("setter" in n for n in names)
        if not busy_defs:
            bad.append("Application._busy is gone")
        elif not has_getter or not has_setter:
            bad.append("Application._busy is not a read/write property over "
                       "RunState (getter=%s setter=%s) — the migration has "
                       "reverted to a plain bool" % (has_getter, has_setter))
        for helper in ("_claim_run", "_mark_running", "_release_run"):
            if helper not in defs:
                bad.append("Application.%s is gone" % helper)
        # Only these may WRITE the state. A bare `self._state = ...` anywhere
        # else re-introduces the interleaving the helpers exist to prevent.
        # _request_cancel is on the list because moving RUNNING -> CANCELLING
        # is precisely its job; the other three own the claim/execute/finish
        # edges, which is where the state and _worker_thread must move
        # together.
        writers = set()
        for node in ast.walk(cls):
            if (isinstance(node, ast.Assign)
                    and any(isinstance(t, ast.Attribute) and t.attr == "_state"
                            for t in node.targets)):
                fn = next((f for f in ast.walk(cls)
                           if isinstance(f, ast.FunctionDef)
                           and node in ast.walk(f)), None)
                writers.add(fn.name if fn else "<class body>")
        allowed = {"_busy", "_claim_run", "_mark_running", "_release_run",
                   "last_run_outcome", "__init__", "_request_cancel"}
        for w in sorted(writers - allowed):
            bad.append("Application.%s assigns self._state directly; go "
                       "through _claim_run/_mark_running/_release_run" % w)
    check(not bad, [], bad, failures,
          "_busy is a property over RunState; only the helpers move the state")

    # --- 8 ---------------------------------------------------------------
    print("\n[8] deferred run completions stay generation-gated (H10)")
    bad = []
    if cls is not None:
        src = io.open(app_py, encoding="utf-8").read()
        # Every function that is handed to root.after(0) from a run path and
        # then touches shared state must consult _is_current first. The hops
        # are named explicitly because detecting "is this an after() callback"
        # reliably needs dataflow, not AST matching.
        gated = ("_done_popup", "_toast_if_minimized", "_ui")
        found = 0
        for node in ast.walk(cls):
            if not isinstance(node, ast.FunctionDef) or node.name not in gated:
                continue
            found += 1
            body = ast.get_source_segment(src, node) or ""
            head = body.split("\n\n")[0]
            # a docstring/comment-only preamble is fine; the guard must appear
            # before the first statement that touches the app
            first_code = [ln for ln in head.splitlines()
                          if ln.strip() and not ln.strip().startswith("#")]
            if not any("_is_current" in ln for ln in first_code[:14]):
                bad.append("Application.%s is an after(0) completion with no "
                           "_is_current(gen) guard — a superseded run will "
                           "paint over the current one" % node.name)
        if found < 3:
            bad.append("expected the 3 after(0) run completions (%s), found %d"
                       % (", ".join(gated), found))
        # the worker must receive the generation, not rediscover it
        for fn in defs.get("_run_tasks_worker", []):
            params = [a.arg for a in fn.args.args]
            if "gen" not in params:
                bad.append("_run_tasks_worker does not take a generation; it "
                           "cannot tell whether it is stale")
            if "run_results" not in params:
                bad.append("_run_tasks_worker does not take its own results "
                           "list; it will read whatever run is current")
    check(not bad, [], bad, failures,
          "after(0) completions are gated; the worker is told its generation")

    # --- 9 ---------------------------------------------------------------
    print("\n[9] tweak_state stays a read-only live view (H11)")
    bad = []
    for p in list(py_files(UI)):
        tree = ast.parse(io.open(p, encoding="utf-8").read())
        for node in ast.walk(tree):
            # Nothing may store a snapshot of the applied-tweak registry.
            if isinstance(node, ast.Assign):
                for t in node.targets:
                    if isinstance(t, ast.Attribute) and t.attr == "tweak_state":
                        bad.append("%s:%d assigns .tweak_state — the applied-badge "
                                   "source must stay a read-only live view"
                                   % (rel(p), node.lineno))
            # ...and nothing may add a setter that would permit it.
            if (isinstance(node, ast.FunctionDef)
                    and node.name == "tweak_state"):
                if any(isinstance(d, ast.Attribute) and d.attr == "setter"
                       for d in node.decorator_list):
                    bad.append("%s:%d gives tweak_state a setter" % (rel(p), node.lineno))
                # The view must read through, not memoise under another name.
                # `self._tweak_state_cache = ...; return self._tweak_state_cache`
                # is the same staleness bug wearing a different identifier, and
                # matching only on the name "tweak_state" would miss it.
                for n2 in ast.walk(node):
                    if (isinstance(n2, ast.Assign)
                            and any(isinstance(t, ast.Attribute)
                                    and isinstance(t.value, ast.Name)
                                    and t.value.id == "self"
                                    for t in n2.targets)):
                        tgt = n2.targets[0]
                        if getattr(tgt, "attr", "") != "tweak_state":
                            bad.append("%s:%d tweak_state stores %r on self — "
                                       "the view must re-read, not memoise"
                                       % (rel(p), n2.lineno, tgt.attr))
    # a tasktab that reads the registry itself is re-introducing the cache
    tt = UI / "tabs" / "tasktab.py"
    if tt.is_file():
        tsrc = io.open(tt, encoding="utf-8").read()
        if "get_tweak_state" in tsrc:
            bad.append("app/ui/tabs/tasktab.py reads get_tweak_state directly; "
                       "it should go through Application.tweak_state so there "
                       "is only one path to the applied-badge state")
    check(not bad, [], bad, failures,
          "tweak_state is a read-only live view with a single reader path")

    # --- 10 --------------------------------------------------------------
    print("\n[10] config.json round-trips through the real loader (H7)")
    bad = []
    try:
        import json as _json
        import tempfile
        from pathlib import Path as _P
        # The rest of this file parses source rather than importing it, so
        # that it can run before the app loads at all. This one check has to
        # import, to exercise the real loader, so put the repo on the path
        # for the duration.
        if str(ROOT) not in sys.path:
            sys.path.insert(0, str(ROOT))
        from app import config_persist as _cp
        from app.config_schema import validate_schema
        real_file = _cp.CONFIG_FILE
        probe = _P(tempfile.gettempdir()) / "arch_probe_config.json"
        stored = {"schedule_frequency": "monthly", "schedule_time": "04:30",
                  "close_to_tray_enabled": False,
                  "selected_tasks": {"Clean": ["driver_junk"]},
                  "applied_tweaks": ["probe_key"],
                  "a_key_from_a_newer_build": {"keep": True}}
        try:
            probe.write_text(_json.dumps(stored), encoding="utf-8")
            _cp.CONFIG_FILE = probe
            _cp._config_cache = None
            got = _cp.load_config()
            # The failure this pins: _load_config_from_disk losing its
            # `return data` and falling through to DEFAULT_CONFIG. Every read
            # then returns defaults and the user's config is ignored — with no
            # exception anywhere, so only a real round trip sees it.
            for key, want in (("schedule_frequency", "monthly"),
                              ("schedule_time", "04:30"),
                              ("close_to_tray_enabled", False),
                              ("applied_tweaks", ["probe_key"])):
                if got.get(key) != want:
                    bad.append("load_config lost %r: got %r, want %r — is "
                               "_load_config_from_disk still returning `data`?"
                               % (key, got.get(key), want))
            if got.get("selected_tasks", {}).get("Clean") != ["driver_junk"]:
                bad.append("load_config lost selected_tasks: %r"
                           % got.get("selected_tasks"))
            if got.get("a_key_from_a_newer_build") != {"keep": True}:
                bad.append("load_config dropped an unknown key written by a "
                           "newer build (forward compatibility)")
        finally:
            _cp.CONFIG_FILE = real_file
            _cp._config_cache = None
            try:
                probe.unlink()
            except Exception:
                pass
    except Exception as exc:
        bad.append("config round-trip probe could not run: %r" % (exc,))
    # and the two declarations must not drift apart
    try:
        from app.config_persist import DEFAULT_CONFIG as _dc
        from app.config_schema import validate_schema as _vs
        for problem in _vs(_dc):
            bad.append("schema drift: " + problem)
    except Exception as exc:
        bad.append("schema drift check could not run: %r" % (exc,))
    check(not bad, [], bad, failures,
          "load_config returns the file's contents; SCHEMA matches DEFAULT_CONFIG")

    # --- 11 --------------------------------------------------------------
    print("\n[11] the Auto-Pilot lifecycle stays in SessionOrchestrator (H15)")
    bad = []
    sess = UI / "session.py"
    if not sess.is_file():
        bad.append("app/ui/session.py is gone — the Auto-Pilot orchestration "
                   "has no home again")
    else:
        ssrc = io.open(sess, encoding="utf-8").read()
        stree = ast.parse(ssrc)
        scls = next((n for n in stree.body
                     if isinstance(n, ast.ClassDef)
                     and n.name == "SessionOrchestrator"), None)
        if scls is None:
            bad.append("SessionOrchestrator is missing")
        else:
            sdefs = {m.name for m in scls.body
                     if isinstance(m, ast.FunctionDef)}
            for needed in ("ensure_pump", "cancel_pump", "pump", "post_apply",
                           "post_revert", "start", "stop", "shutdown",
                           "is_running", "note_session_active"):
                if needed not in sdefs:
                    bad.append("SessionOrchestrator.%s is gone" % needed)
            # the watcher thread must not be able to reach Tk. post_apply /
            # post_revert are the only two methods the watcher holds, so if
            # either grows a Tk call the cross-thread notifier race comes back
            # and detects get dropped silently.
            for name in ("post_apply", "post_revert"):
                fn = next((m for m in scls.body
                           if isinstance(m, ast.FunctionDef)
                           and m.name == name), None)
                if fn is None:
                    continue
                body = ast.get_source_segment(ssrc, fn) or ""
                for forbidden in ("after(", "after_cancel", "winfo_",
                                  "messagebox", "PhotoImage"):
                    if forbidden in body:
                        bad.append("SessionOrchestrator.%s calls %r — it runs "
                                   "on the WATCHER thread and must only "
                                   "queue.put" % (name, forbidden))
        # and Application must delegate rather than re-implement
        if cls is not None:
            adefs = {m.name for m in cls.body
                     if isinstance(m, ast.FunctionDef)}
            # _pilot_pump used to be listed here. It was never called by
            # anything -- the H15 split wired SessionPilot's callbacks
            # straight to SessionOrchestrator.post_apply/post_revert and the
            # drain loop to _pilot_sink -- so all this assertion did was pin a
            # dead method in place. A guard that keeps dead code alive is a
            # guard that stops being read; DEAD-001 removed the method and this
            # entry with it.
            for delegating in ("_pilot_running", "_pilot_ensure"):
                if delegating not in adefs:
                    bad.append("Application.%s is gone" % delegating)
            # _pilot_pump_started must not reappear anywhere in Application.
            # It was the pre-H15 one-way flag: once set, the 50ms drain loop
            # rescheduled itself for the rest of the app's life with no way to
            # stop it. Match the AST rather than the text, so the comment that
            # documents the removal does not trip the check, while a real
            # reference — as a Name, an attribute, or a getattr() string —
            # still does.
            stale = []
            for node in ast.walk(cls):
                if isinstance(node, ast.Name) and node.id == "_pilot_pump_started":
                    stale.append("L%d (name)" % node.lineno)
                elif (isinstance(node, ast.Attribute)
                      and node.attr == "_pilot_pump_started"):
                    stale.append("L%d (attribute)" % node.lineno)
                elif (isinstance(node, ast.Constant)
                      and node.value == "_pilot_pump_started"):
                    stale.append("L%d (getattr string)" % node.lineno)
            for where in stale:
                bad.append("Application references _pilot_pump_started at %s — "
                           "the un-stoppable pre-H15 pump flag is back" % where)
    check(not bad, [], bad, failures,
          "orchestration lives in SessionOrchestrator; producers stay queue-only")

    # --- 12 --------------------------------------------------------------
    print("\n[12] the smoke suite stays split, and every area is reachable (H16)")
    bad = []
    smoke_pkg = ROOT / "tools" / "smoke"
    driver = ROOT / "tools" / "smoke_gui.py"
    try:
        if str(ROOT) not in sys.path:
            sys.path.insert(0, str(ROOT))
        from tools import smoke as _sm
    except Exception as exc:
        bad.append("tools.smoke could not be imported: %r" % (exc,))
        _sm = None
    if _sm is not None:
        for name, _purpose in _sm.AREAS:
            p = smoke_pkg / (name + ".py")
            if not p.is_file():
                bad.append("tools/smoke/%s.py is missing (AREAS lists it)" % name)
                continue
            t = ast.parse(io.open(p, encoding="utf-8").read())
            fns = [n.name for n in t.body if isinstance(n, ast.FunctionDef)]
            if "run" not in fns:
                bad.append("tools/smoke/%s.py has no run(ctx)" % name)
# each area must bind the shared names off the Ctx
        body = io.open(p, encoding="utf-8").read()
        if "def run(ctx):" in body and "ctx.root" not in body:
            bad.append("tools/smoke/%s.py never reads ctx — the shared "
                       "names are not coming from the Ctx" % name)
        # driver must stay a driver, not grow areas back
        if driver.is_file():
            n = len(io.open(driver, encoding="utf-8").read().splitlines())
            if n > 1400:
                bad.append("tools/smoke_gui.py is %d lines — areas are creeping "
                           "back into the driver (budget 1400)" % n)
        # An area may import shared helpers from the tools.smoke PACKAGE --
        # that is what the package is for, and assert_dialog_proportional is
        # one. symtable reports an imported binding as is_imported() rather
        # than is_assigned(), so counting only assignments made those look
        # like undefined globals.
        for node in ast.walk(ast.parse(body)):
            if isinstance(node, ast.ImportFrom) and node.module \
                    and node.module.startswith("tools.smoke."):
                bad.append("tools/smoke/%s.py imports from the sibling area "
                           "%r — areas must not depend on each other; move a "
                           "shared helper into tools/smoke/__init__.py"
                           % (name, node.module))
        # a name referenced by one area and defined in another is the one
        # coupling H16 had to break; catch it coming back.
        for name2, _purpose in _sm.AREAS:
            p = smoke_pkg / (name2 + ".py")
            if not p.is_file():
                continue
            st = symtable.symtable(io.open(p, encoding="utf-8").read(), str(p), "exec")
            modnames = {s.get_name() for s in st.get_symbols()
                        if s.is_assigned() or s.is_imported()}
            rt = next((c for c in st.get_children() if c.get_name() == "run"), None)
            if rt is None:
                continue
            for sym in rt.get_symbols():
                nm = sym.get_name()
                if (sym.is_global() and not sym.is_assigned()
                        and nm not in modnames
                        and not hasattr(__builtins__, nm)
                        and nm not in dir(__builtins__)):
                    bad.append("tools/smoke/%s.py references undefined global %r"
                               % (name2, nm))
    check(not bad, [], bad, failures,
           "smoke suite is a driver + %d areas; no cross-area free variables"
           % (len(_sm.AREAS) if _sm else 0))

    # --- 13 --------------------------------------------------------------
    print("\n[13] the harness cannot silently disable a check")
    bad = []
    # (a) no hardcoded developer checkout path. Thirteen tools carried
    # r"C:\Users\User\Desktop\Cleaner Tool", which does not exist on any
    # other machine, so `import app` failed and the check aborted before a
    # single assertion ran. ~326 assertions had never validated anything.
    stale = re.compile(r"[A-Za-z]:\\Users\\[^\\\r\n\"']+")
    for p in list(py_files(ROOT / "tools")):
        for i, line in enumerate(io.open(p, encoding="utf-8").read().splitlines(), 1):
            m = stale.search(line)
            if not m:
                continue
            # Fixture strings for the tests are fine -- they are data, not
            # paths this process will open. Only a real open/read counts.
            if ("open(" in line or "Path(" in line or "sys.path" in line
                    or "io.open" in line):
                bad.append("%s:%d hardcodes a checkout path: %s"
                           % (rel(p), i, m.group(0)[:48]))
    # (b) run_all_checks must inject the repo root, so a check cannot be
    # disabled by its own import bootstrap.
    runner = ROOT / "tools" / "run_all_checks.py"
    if not runner.is_file():
        bad.append("tools/run_all_checks.py is missing")
    else:
        rsrc = io.open(runner, encoding="utf-8").read()
        if "PYTHONPATH" not in rsrc:
            bad.append("tools/run_all_checks.py does not set PYTHONPATH for its "
                       "children -- `python tools/X.py` puts tools/ on sys.path, "
                       "not the repo root, so `import app` dies before any "
                       "assertion runs")
        if "cwd=ROOT" not in rsrc:
            bad.append("tools/run_all_checks.py does not pin cwd=ROOT for its "
                       "children, so the suite only passes from the repo root")
    # (c) the suite must list every check file that exists, or a new check
    # is written and never wired in.
    listed = set()
    if runner.is_file():
        rtree = ast.parse(io.open(runner, encoding="utf-8").read())
        for node in ast.walk(rtree):
            if (isinstance(node, ast.Constant) and isinstance(node.value, str)
                    and node.value.endswith(".py") and "/" in node.value):
                listed.add(node.value)
    for p in sorted((ROOT / "tools").glob("verify_*.py")):
        relp = rel(p)
        if relp not in listed:
            bad.append("%s exists but is not in the run_all_checks SUITE -- a "
                       "check nobody runs" % relp)
    check(not bad, [], bad, failures,
           "no hardcoded checkout paths; runner injects the root; every "
           "verify_*.py is in the SUITE")

    print()
    if failures:
        print("ARCHITECTURE: %d violation(s)" % len(failures))
        return 1
    print("ARCHITECTURE: ALL PASS (%d modules scanned)" % len(files))
    return 0


if __name__ == "__main__":
    sys.exit(main())
