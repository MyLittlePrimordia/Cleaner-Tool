"""MED-012 and MED-013: a server-controlled URL, and a read error mistaken
for corruption.

MED-012  `browser_download_url` is 100% server-controlled - whoever can publish
         a release chooses the string - and it went straight to
         `webbrowser.open` with no scheme check and no host check. The app IS
         the system-tuning tool, so "Update Available" carries very high trust,
         and a silent downgrade to http:// is the whole risk. The report
         correctly notes there is NO auto-download-and-execute and the size cap
         is good, so the blast radius is the link. Now validated at the parse
         boundary AND again at the open() call site.

MED-013  `load_config` treated EVERY read failure as corruption. Only
         FileNotFoundError meant "missing", so a PermissionError, an AV
         sharing violation or a locked file all reached
         _quarantine_corrupt_config: a false "config_quarantine" entry in the
         app's own SECURITY EVENT LOG, every tweak badge gone, and then the
         next update_config OVERWRITING the real config.json with defaults -
         destroying the tweak_snapshots revert registry. `save_config` has
         written a .bak all along and nothing ever read it, despite the
         comment promising exactly that.

No real config is touched: MED-012 is pure string validation, and MED-013 runs
against a temporary config directory with the module's CONFIG_FILE redirected.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile

sys.path.insert(0, ".")

from app.update_checker import _trusted_release_url  # noqa: E402

FAILS = 0
CHECKS = 0
REPO = "MyLittlePrimordia/Cleaner-Tool"
GOOD = ("https://github.com/MyLittlePrimordia/Cleaner-Tool/releases/"
        "download/v2.1/Cleaner-Tool.exe")


def ck(label, cond, detail=""):
    global FAILS, CHECKS
    CHECKS += 1
    if cond:
        print("  PASS  %s" % label)
    else:
        FAILS += 1
        print("  FAIL  %s%s" % (label, ("  <- " + str(detail)) if detail else ""))


# ===================================================================== #
print("[1] MED-012: what must be refused")
MUST_REFUSE = [
    ("http downgrade of the real link",
     GOOD.replace("https://", "http://")),
    ("a lookalike host",
     "https://github.com.evil.example/MyLittlePrimordia/Cleaner-Tool/x.exe"),
    ("an unrelated host",
     "https://evil.example/MyLittlePrimordia/Cleaner-Tool/x.exe"),
    ("a different repo's release",
     "https://github.com/attacker/evil/releases/download/v1/Cleaner-Tool.exe"),
    ("file:// local payload",
     "file:///C:/Windows/System32/calc.exe"),
    ("javascript:", "javascript:alert(document.cookie)"),
    ("data:", "data:text/html,<script>alert(1)</script>"),
    ("ms-msdt: handler", "ms-msdt:/id PCWDiagnostic"),
    ("a bare host with no scheme", "github.com/x/y.exe"),
    ("protocol-relative", "//github.com/MyLittlePrimordia/Cleaner-Tool/x.exe"),
    ("empty", ""),
    ("None", None),
    ("uppercase scheme must still be rejected (scheme is lowercased by the "
     "parser, so this is a positive control, listed here to be explicit)",
     "HTTPS://github.com/MyLittlePrimordia/Cleaner-Tool/x.exe"),
]
for label, url in MUST_REFUSE:
    got = _trusted_release_url(url, REPO)
    # urlparse lowercases the scheme, so an uppercase-scheme https URL is
    # legitimately ACCEPTED. Treat that one separately below.
    if label.startswith("uppercase scheme"):
        ck("MED-012: %s -> accepted (https is still https)" % label,
           bool(got) is True, got)
    else:
        ck("MED-012: refused %s" % label, got == "", got)

print()
print("[2] MED-012: the real link still works")
ck("the genuine GitHub release asset is accepted",
   _trusted_release_url(GOOD, REPO) == GOOD)
ck("objects.githubusercontent.com asset host is accepted",
   bool(_trusted_release_url(
       "https://objects.githubusercontent.com/x/MyLittlePrimordia/Cleaner-Tool/a.exe",
       REPO)))
ck("a mismatched repo argument rejects the link",
   _trusted_release_url(GOOD, "someone/other") == "")
ck("a non-str input is refused rather than raising",
   _trusted_release_url(12345, REPO) == "")

print()
print("[3] MED-012: validated at BOTH ends, not just one")
import ast  # noqa: E402
import inspect  # noqa: E402

import app.update_checker as uc  # noqa: E402
import app.ui.app as appmod  # noqa: E402

ck("check_for_updates filters the URL it stores",
   "_trusted_release_url(" in inspect.getsource(uc.check_for_updates))
ck("...and says so in the result when it refuses",
   "refusing to link it" in inspect.getsource(uc.check_for_updates))
app_src = inspect.getsource(appmod.Application._show_update_dialog)
ck("the webbrowser.open call site re-validates", "safe = _trusted_release_url" in app_src)
ck("...and opens `safe`, never the raw value",
   "webbrowser.open(safe)" in app_src and "webbrowser.open(download_url)" not in app_src)
ck("a refused link tells the user instead of silently doing nothing",
   "Refusing to open the update link" in app_src)
ck("the repo used to query and to validate is ONE constant",
   "UPDATE_REPO" in inspect.getsource(appmod.Application._check_for_updates)
   and "UPDATE_REPO" in app_src
   and 'check_for_updates("MyLittlePrimordia' not in
       inspect.getsource(appmod.Application._check_for_updates))

# ===================================================================== #
print()
print("[4] MED-013: an unreadable file is NOT corrupt")
import app.config_persist as cp  # noqa: E402

tmpdir = tempfile.mkdtemp(prefix="opencode_m13_")
real_cfg = cp.CONFIG_FILE
real_dir = cp.get_config_dir


def fresh_config(contents, *, name="config.json"):
    """Point cp at a temp dir and WRITE `contents` (str or dict) there.

    The first version of this helper only computed the path and never wrote
    the file, so CONFIG_FILE did not exist, _load_config_from_disk returned
    DEFAULT_CONFIG immediately, and every .bak / quarantine / PermissionError
    check below was passing or failing for the wrong reason.
    """
    cp._config_cache = None
    cp._config_mtime = None
    d = os.path.join(tmpdir, name.replace(".json", ""))
    shutil.rmtree(d, ignore_errors=True)
    os.makedirs(d, exist_ok=True)
    cp.CONFIG_FILE = type(real_cfg)(os.path.join(d, name))
    with open(cp.CONFIG_FILE, "w", encoding="utf-8") as h:
        h.write(contents if isinstance(contents, str) else json.dumps(contents))
    assert cp.CONFIG_FILE.exists(), "fresh_config did not create the file"
    return cp.CONFIG_FILE


PRIOR = {"applied_tweaks": ["some_tweak"],
         "tweak_snapshots": {"some_tweak": {"specs": [["HKCU", "X", "Y"]]}},
         "selected_tasks": {"Clean": ["shader_cache"]}}


def reset():
    cp._config_cache = None
    cp._config_mtime = None


try:
    # --- a genuinely corrupt body: recoverable from the .bak -------------
    reset()
    f = fresh_config("{ this is not json")
    with open(cp.CONFIG_FILE.with_suffix(".json.bak"), "w", encoding="utf-8") as h:
        json.dump(PRIOR, h)
    cfg = cp._load_config_from_disk()
    ck("a corrupt config recovers from the .bak instead of resetting",
       cfg.get("applied_tweaks") == PRIOR["applied_tweaks"],
       cfg.get("applied_tweaks"))
    ck("...the tweak_snapshots undo registry survives",
       "some_tweak" in (cfg.get("tweak_snapshots") or {}),
       sorted(cfg.get("tweak_snapshots") or {}))
    ck("...and the corrupt file is NOT quarantined away",
       cp.CONFIG_FILE.exists(), "the primary was renamed aside")

    # --- a corrupt body with NO usable backup: quarantine, as before -----
    reset()
    fresh_config("{ not json either")
    cfg = cp._load_config_from_disk()
    ck("with no usable backup it falls back to defaults (old behaviour)",
       isinstance(cfg, dict) and cfg.get("applied_tweaks") == [],
       cfg.get("applied_tweaks"))

    # --- a PERMISSION error: must NOT quarantine -------------------------
    reset()
    f = fresh_config(json.dumps(PRIOR))
    quarantined = []
    real_q = cp._quarantine_corrupt_config
    cp._quarantine_corrupt_config = lambda exc: quarantined.append(exc)
    try:
        # Simulate "cannot read, right now" the way Windows presents it.
        real_open = open

        def deny(*a, **k):
            if a and str(a[0]).endswith("config.json"):
                raise PermissionError(13, "used by another process")
            return real_open(*a, **k)

        import builtins
        cp.__dict__["open"] = deny
        builtins.open = deny
        try:
            cfg = cp._load_config_from_disk()
        finally:
            builtins.open = real_open
            del cp.__dict__["open"]
    finally:
        cp._quarantine_corrupt_config = real_q
    ck("a PermissionError does NOT quarantine the file",
       quarantined == [], quarantined)
    ck("...the file is left in place", cp.CONFIG_FILE.exists())
    ck("...and the cached copy is served when there is one",
       isinstance(cfg, dict), type(cfg).__name__)

    print()
    print("[5] MED-013: a cached copy is served during a read failure")
    reset()
    f = fresh_config(json.dumps(PRIOR))
    # Must use load_config(), not _load_config_from_disk(): only load_config
    # populates the cache, so priming it the internal way left _config_cache
    # as None and the "read failure" then correctly fell through to defaults -
    # a test that was measuring the wrong thing.
    good = cp.load_config()
    ck("baseline load keeps the tweak registry",
       good.get("applied_tweaks") == PRIOR["applied_tweaks"],
       good.get("applied_tweaks"))
    ck("...and the cache is primed", cp._config_cache is not None)
    ck("...and this is genuinely NOT the defaults",
       cp.DEFAULT_CONFIG.get("applied_tweaks") == []
       and good.get("applied_tweaks") != [])

    import builtins
    real_open = builtins.open

    def deny(*a, **k):
        if a and str(a[0]).endswith("config.json"):
            raise PermissionError(13, "locked by AV")
        return real_open(*a, **k)

    builtins.open = deny
    try:
        cp._config_mtime = None          # force the re-stat path
        served = cp.load_config()
    finally:
        builtins.open = real_open
    ck("during a read failure the CACHED config is served, not defaults",
       served.get("applied_tweaks") == PRIOR["applied_tweaks"],
       served.get("applied_tweaks"))
    ck("...so the tweak_snapshots undo registry is still intact",
       "some_tweak" in (served.get("tweak_snapshots") or {}),
       sorted(served.get("tweak_snapshots") or {}))

    print()
    print("[6] structural: the exception clauses are ordered and distinct")
    src = inspect.getsource(cp._load_config_from_disk)
    # AST, not a text scan. Collecting every line starting with "except" also
    # picks up handlers of NESTED try blocks - LOW-007 added a `try/except
    # OSError` around the size stat() - and then reports the top-level
    # ordering as wrong when it is not.
    import ast  # noqa: E402
    fn = next(n for n in ast.walk(ast.parse(src))
              if isinstance(n, ast.FunctionDef)
              and n.name == "_load_config_from_disk")
    top = fn.body[-1] if isinstance(fn.body[-1], ast.Try) else None
    while top is not None and not isinstance(top, ast.Try):
        top = None
    # find the top-level try by walking only the function's own statements
    def _find_try(node):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.Try):
                # a top-level handler for this function is one whose `body`
                # is not nested inside another Try within the same function
                return child
            if isinstance(child, (ast.If, ast.For, ast.While, ast.With)):
                got = _find_try(child)
                if got is not None:
                    return got
        return None
    outer = _find_try(fn)
    order = []
    if outer is not None:
        for h in outer.handlers:
            exc = h.type
            if exc is None:
                order.append("except")
            elif isinstance(exc, ast.Name):
                order.append("except " + exc.id)
            elif isinstance(exc, ast.Tuple):
                order.append("except " + ",".join(
                    e.id for e in exc.elts if isinstance(e, ast.Name)))
    ck("a top-level try was found to inspect", outer is not None, order)
    ck("FileNotFoundError is handled FIRST (it is an OSError subclass, so "
       "order is load-bearing)",
       bool(order) and order[0].startswith("except FileNotFoundError"), order)
    ck("there is a dedicated OSError clause for 'unreadable, not corrupt'",
       any("OSError" in o for o in order), order)
    ck("corruption is judged by a parse/shape error, not by 'any exception'",
       any("JSONDecodeError" in o or "ValueError" in o for o in order), order)
    ck("MED-013 is documented", "MED-013" in src)
finally:
    cp.CONFIG_FILE = real_cfg
    cp._config_cache = None
    cp._config_mtime = None
    shutil.rmtree(tmpdir, ignore_errors=True)

print()
if FAILS:
    print("MED-012/013 VERIFY: %d/%d FAILED" % (FAILS, CHECKS))
    sys.exit(1)
print("MED-012/013 VERIFY: ALL PASS (%d checks)" % CHECKS)
