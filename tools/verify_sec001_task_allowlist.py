"""prior SEC-001 (report fix-order item 25, first half): elevated Tweak Undo
trusted user-controlled registry targets.

`startup_disabled` and `tweak_snapshots` both live in the user's config.json.
_prior SEC-001_ is the snapshot half: `get_tweak_snapshot()` reads persisted
data, and `_restore_reg_values()` used to validate the (hive, path, name)
triples only against a DENYLIST of persistence locations. So a same-user
process edits `tweak_snapshots[task]["specs"]`, the user runs Undo while
Cleaner Tool is ELEVATED, and an elevated process writes attacker-selected
values - or deletes values - at any HKCU/HKLM/HKCR location that is not on the
denylist.

The report's words: "Derive restore targets exclusively from immutable
code-owned task metadata. Match the persisted snapshot to the exact expected
task and reject legacy/tampered snapshots that do not match exactly."

`_TASK_REG_ALLOWLIST` in app/tasks/tweak_tasks.py is that code-owned metadata:
a snapshot spec is honoured only if the task itself is defined to touch that
location.

The report's verification plan is followed literally, including the part that
is easy to miss: ALTERNATE PATH SPELLINGS. A denylist-style substring test, or
an allowlist compared with `==` on raw strings, can be walked straight past by
writing "SOFTWARE\\Microsoft\\Windows NT\\CurrentVersion\\Run" with different
case, a doubled separator, or a trailing backslash. So matching normalises
first, and that is asserted here.

SCOPE, and this test does not pretend otherwise: the allowlist covers 40 of
~65 registry-backed tweaks. The remainder (dynamic-path tasks like
disable_nagle, and tasks that write without a snapshot list) are still governed
by the denylist alone and are tracked in references/FINDINGS_LEDGER.txt. This
check asserts the implemented scope and the drift guard; it does not assert
the finding is closed.
"""

from __future__ import annotations

import ast
import io
import sys

sys.path.insert(0, ".")

from app.tasks import tweak_tasks as tt  # noqa: E402

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


A = tt._spec_allowed_for_task
TABLE = tt._TASK_REG_ALLOWLIST

print("[1] the allowlist exists and is code-owned")
ck("_TASK_REG_ALLOWLIST is populated", isinstance(TABLE, dict) and len(TABLE) >= 30,
   len(TABLE) if isinstance(TABLE, dict) else TABLE)
ck("it is a module-level literal in tweak_tasks.py (not loaded from config)",
   "_TASK_REG_ALLOWLIST = {" in io.open(r"app\tasks\tweak_tasks.py",
                                          encoding="utf-8", newline="").read())
ck("the scope gap is documented at the table, not hidden",
   "THIS FINDING IS NOT CLOSED" in io.open(r"app\tasks\tweak_tasks.py",
                                           encoding="utf-8", newline="").read())

print()
print("[2] a location the task DOES own is allowed")
ck("hags -> GraphicsDrivers\\HwSchMode",
   A("hags", "HKLM", "SYSTEM\\CurrentControlSet\\Control\\GraphicsDrivers", "HwSchMode") is True)
ck("game_mode -> its own Software\\Microsoft\\GameBar values",
   A("game_mode", "HKCU", "Software\\Microsoft\\GameBar", "AutoGameModeEnabled") is True)
ck("privacy_baseline -> one of its 12",
   A("privacy_baseline", "HKCU", "Software\\Microsoft\\Windows\\CurrentVersion\\AdvertisingInfo",
     "Enabled") is True)

print()
print("[3] THE BUG: a location the task does NOT own is refused, even when benign")
# This is the case the DENYLIST let through: not a persistence location, so
# the old guard waved it through and an elevated process wrote it.
ck("hags cannot be made to write a Run key",
   A("hags", "HKLM", "SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\Run", "Evil") is False)
ck("a Run value under a policy key is refused too",
   A("game_mode", "HKLM", "SOFTWARE\\Policies\\Microsoft\\Windows\\Run", "x") is False)
ck("cross-task confusion is refused (a task may not borrow another's key)",
   A("hags", "HKCU", "Control Panel\\Mouse", "MouseSpeed") is False)
ck("...in either direction",
   A("mouse_accel", "HKLM", "SYSTEM\\CurrentControlSet\\Control\\GraphicsDrivers",
     "HwSchMode") is False)

print()
print("[4] ALTERNATE PATH SPELLINGS cannot walk past the allowlist")
OWNED = ("hags", "HKLM", "SYSTEM\\CurrentControlSet\\Control\\GraphicsDrivers", "HwSchMode")
for label, (h, p, n) in {
    "lowercase key": ("hklm", "system\\currentcontrolset\\control\\graphicsdrivers", "hwschmode"),
    "trailing backslash": ("HKLM", "SYSTEM\\CurrentControlSet\\Control\\GraphicsDrivers\\", "HwSchMode"),
    "doubled separators": ("HKLM", "SYSTEM\\\\CurrentControlSet\\\\Control\\\\GraphicsDrivers", "HwSchMode"),
    "forward slashes": ("HKLM", "SYSTEM/CurrentControlSet/Control/GraphicsDrivers", "HwSchMode"),
    "leading separator": ("HKLM", "\\SYSTEM\\CurrentControlSet\\Control\\GraphicsDrivers", "HwSchMode"),
}.items():
    ck("owned key still allowed via %s" % label, A("hags", h, p, n) is True, (h, p, n))

print()
print("[5] ...and the same tricks cannot be used to smuggle a DISALLOWED key in")
for label, (h, p, n) in {
    "lowercase Run key": ("HKLM", "software\\microsoft\\windows nt\\currentversion\\run", "evil"),
    "doubled separators": ("HKLM", "SOFTWARE\\\\Microsoft\\\\Windows NT\\\\CurrentVersion\\\\Run", "evil"),
    "trailing backslash": ("HKLM", "SOFTWARE\\Microsoft\\Windows NT\\CurrentVersion\\Run\\", "evil"),
    "forward slashes": ("HKLM", "SOFTWARE/Microsoft/Windows NT/CurrentVersion/Run", "evil"),
}.items():
    ck("disallowed Run key refused via %s" % label, A("hags", h, p, n) is False, (h, p, n))

print()
print("[6] the enforcement is actually WIRED into the restore path")
_src = io.open(r"app\tasks\tweak_tasks.py", encoding="utf-8", newline="").read()
_fn = ast.get_source_segment(_src, next(
    n for n in ast.walk(ast.parse(_src))
    if isinstance(n, ast.FunctionDef) and n.name == "_restore_reg_values")) or ""
ck("_restore_reg_values calls the allowlist check", "_spec_allowed_for_task(" in _fn)
ck("...and RAISES on a disallowed spec (fails closed, no partial write)",
   "Refusing to restore" in _fn and "raise RuntimeError" in _fn)
ck("the denylist is RETAINED underneath as defence in depth",
   "_is_persistence_location(path)" in _fn)
ck("SEC-001 is documented at the enforcement site", "SEC-001" in _fn)

print()
print("[7] DRIFT GUARD: the table must match what the task code actually declares")
# Recompute the allowlist from this module's own source, the same way it was
# generated, and compare. Without this, a tweak could gain a new registry
# location, apply it, and quietly have no Undo - or worse, be Undo-able at a
# location the table never sanctioned.
_src_tree = ast.parse(_src)

_expected = {}


def _val(node, depth=0):
    """Resolve a spec node, following module constants.

    Four shapes occur in this module and all four had to be handled:
      * a plain literal / a name bound to a literal list;
      * a Name INSIDE an inline list - [("HKCU", _ADV, "TaskbarAl")];
      * a list comprehension over a constant -
        [w + ("REG_SZ",) for w in _ACCESSIBILITY_FLAGS];
      * a subscript of a constant - _SENSE[0], _SENSE[1].
    A resolver that handles only the first misses 14 of the tasks.
    """
    if depth > 8:
        return None
    if isinstance(node, ast.Constant):
        return node.value
    if isinstance(node, ast.Name):
        return _C.get(node.id)
    if isinstance(node, (ast.List, ast.Tuple)):
        return [_val(e, depth + 1) for e in node.elts]
    if isinstance(node, ast.Subscript):
        base = _val(node.value, depth + 1)
        idx = _val(node.slice, depth + 1)
        try:
            return base[idx]
        except Exception:
            return None
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        # _STOP_ADS_SPECS is `[...] + [...]`, a list concat of two resolvable
        # halves.
        _l = _val(node.left, depth + 1)
        _r = _val(node.right, depth + 1)
        if isinstance(_l, list) and isinstance(_r, list):
            return _l + _r
        if isinstance(_l, list):
            return _l
        return _r
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Mod):
        # classic_context_menu builds its path with %-formatting:
        #   "Software\\Classes\\CLSID\\%s\\InprocServer32" % CONTEXT_MENU_CLSID
        _f = _val(node.left, depth + 1)
        if isinstance(_f, str):
            try:
                return _f % _val(node.right, depth + 1)
            except Exception:
                return None
        return None
    if isinstance(node, ast.ListComp):
        # Evaluate the comprehension properly: bind the loop variable, then
        # evaluate the ELEMENT expression for each item.
        #
        # Returning the iterable's contents instead - the earlier version - is
        # wrong for [("HKCU", _ADS_CDM, n) for n in _ADS_CDM_NAMES], which
        # yields a list of bare value NAMES rather than the (hive, path, name)
        # triples the comprehension builds. That made stop_windows_ads (17
        # specs) look dynamically built when it is a plain module constant.
        #
        # `ifs` is deliberately IGNORED: taskbar_cleanup filters on the running
        # Windows version, and honouring it would make a derived table depend
        # on the machine it was generated on. The unfiltered superset is the
        # right shape - a snapshot can only hold a subset of what apply wrote.
        _out = []
        for _g in node.generators:
            _it = _val(_g.iter, depth + 1)
            if not isinstance(_it, (list, tuple)):
                return None
            for _item in _it:
                if isinstance(_g.target, ast.Name):
                    _names = [(_g.target.id, _item)]
                elif isinstance(_g.target, (ast.Tuple, ast.List)):
                    # taskbar_cleanup unpacks 5-tuples:
                    #   [(h, p, n) for h, p, n, _v, _w in active]
                    if not isinstance(_item, (list, tuple)):
                        return None
                    _names = []
                    for _i, _t in enumerate(_g.target.elts):
                        if not isinstance(_t, ast.Name):
                            return None
                        _names.append((_t.id,
                                       _item[_i] if _i < len(_item) else None))
                else:
                    return None
                _saved = {k: _C.get(k) for k, _ in _names}
                try:
                    for _k, _v in _names:
                        _C[_k] = _v
                    _out.append(_val(node.elt, depth + 1))
                finally:
                    for _k, _v in _saved.items():
                        _C[_k] = _v
        return _out
    return None


_C = {}


def _build_consts(tree):
    """Resolve module-level constants to a FIXPOINT.

    One literal_eval pass is not enough. _IPV4_SPECS is

        [("HKLM", _IPV4_PATH, "DisabledComponents", "REG_DWORD")]

    which is perfectly static, but it REFERENCES another constant, so
    literal_eval raises and the task looks dynamically-built when it is not.
    Iterating the resolver over the module's own assignments resolves those
    chains - and it is why prefer_ipv4 and disable_sticky_keys are statically
    allowlistable at all.
    """
    # AnnAssign matters: `name: list[tuple] = [...]` is an AnnAssign, NOT an
    # Assign, so collecting only Assign silently skipped _IPV4_SPECS - which is
    # annotated - and made prefer_ipv4 look dynamically built. The drift guard
    # then correctly complained that the table declared an entry no apply
    # function seemed to write.
    global _C
    assigns = []
    for _n in tree.body:
        if isinstance(_n, ast.AnnAssign) and isinstance(_n.target, ast.Name):
            assigns.append(_n)
        elif (isinstance(_n, ast.Assign) and len(_n.targets) == 1
                and isinstance(_n.targets[0], ast.Name)):
            assigns.append(_n)
    out = {}
    for _ in range(6):
        before = len(out)
        # Publish incrementally: _val resolves Names out of the GLOBAL _C, so
        # a purely local `out` means no Name ever resolves and the fixpoint
        # silently degrades to plain literals. That is why _IPV4_SPECS (which
        # just references _IPV4_PATH) looked dynamically built.
        _C = out
        for _n in assigns:
            name = (_n.target.id if isinstance(_n, ast.AnnAssign)
                    else _n.targets[0].id)
            if name in out:
                continue
            try:
                v = _val(_n.value)
            except Exception:
                v = None
            if v is not None:
                out[name] = v
        if len(out) == before:
            break
    return out


_C = _build_consts(_src_tree)


for _fn2 in ast.walk(_src_tree):
    if not (isinstance(_fn2, ast.FunctionDef) and _fn2.name.startswith("apply_")):
        continue
    _tid = None
    _specs = None
    for _c in ast.walk(_fn2):
        if (isinstance(_c, ast.Call) and isinstance(_c.func, ast.Name)
                and _c.func.id in ("_regn_apply", "_snap_reg_values")):
            _args = list(_c.args) + [k.value for k in _c.keywords]
            for _a in _args:
                if isinstance(_a, ast.Constant) and isinstance(_a.value, str) and _tid is None:
                    _tid = _a.value
            for _a in _args:
                if isinstance(_a, ast.Constant) or (isinstance(_a, ast.Name)
                                                    and _a.id in ("ctx", "task_id")):
                    continue
                _v = _val(_a)
                if isinstance(_v, list) and _v and isinstance(_v[0], (list, tuple)):
                    _specs = _v
    if _tid and _specs is not None:
        _ok = [tuple(str(x) for x in _s[:3]) for _s in _specs
               if isinstance(_s, (list, tuple)) and len(_s) >= 3
               and all(isinstance(x, str) for x in _s[:3])]
        if _ok:
            _expected[_tid] = set(_ok)

_actual = {k: {tuple(s) for s in v} for k, v in TABLE.items()}
ck("every table entry is declared by a real apply function",
   set(_actual) <= set(_expected),
   sorted(set(_actual) - set(_expected))[:5])
_missing = {k: _expected[k] - _actual.get(k, set()) for k in _expected
            if _expected[k] - _actual.get(k, set())}
ck("the table declares EVERY location its apply function writes", not _missing,
   {k: sorted(v)[:2] for k, v in list(_missing.items())[:3]})
ck("no table entry invents a location no apply function writes",
   not (set(_actual) - set(_expected)))
ck("recomputing the table yields an identical result", _expected == _actual,
   "table and source disagree")

print()
print("[6b] BEHAVIOUR: a tampered snapshot is actually REFUSED, and no write happens")
# The text checks in [6] are not enough on their own: `raise RuntimeError`
# also appears in this function for malformed specs and bad value types, so a
# sabotage that downgrades ONLY the allowlist refusal to a warning still
# contains the string. This drives the real function instead.
from tools._config_guard import ConfigGuard  # noqa: E402


class _Ctx:
    def __init__(self):
        self.logs = []

    def log(self, m):
        self.logs.append(str(m))


try:
    from app.config_persist import save_tweak_snapshot
    with ConfigGuard():
        # a snapshot that names a Run key for the `hags` task
        save_tweak_snapshot("hags", {
            "specs": [["HKLM", "SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\Run",
                       "Evil"]],
            "0:type": "REG_SZ",
        })
        _ctx = _Ctx()
        _refused = None
        try:
            tt._restore_reg_values(_ctx, "hags")
        except RuntimeError as exc:
            _refused = str(exc)
        except Exception as exc:                      # noqa: BLE001
            _refused = "WRONG-TYPE: %s: %s" % (type(exc).__name__, exc)
        ck("a tampered snapshot RAISES", _refused is not None, _refused)
        ck("...with the allowlist refusal, not some unrelated error",
           bool(_refused) and "not defined to" in _refused, _refused)

        # positive control: the task's OWN key must NOT be refused by the
        # allowlist (it may still fail for access reasons, which is fine - what
        # must not happen is the allowlist refusal).
        # save_tweak_snapshot only writes when NO snapshot exists - it keeps
        # the ORIGINAL pre-tweak record on purpose, so a re-apply cannot
        # overwrite it with the tweak's own output. That makes a second
        # save_tweak_snapshot() for the same task a silent NO-OP, which is
        # why this block looked like the allowlist was refusing a legitimate
        # key. Clear first, then save.
        from app.config_persist import clear_tweak_snapshot
        clear_tweak_snapshot("hags")
        save_tweak_snapshot("hags", {
            "specs": [["HKLM", "SYSTEM\\CurrentControlSet\\Control\\GraphicsDrivers",
                       "HwSchMode"]],
            "0:type": "REG_DWORD",
        })
        _ctx2 = _Ctx()
        # config_persist.load_config() serves a CACHE, so the snapshot written
        # above is not visible until it is dropped. Without this the positive
        # control re-read the TAMPERED snapshot from the previous block and
        # failed for entirely the wrong reason. (Same caching behaviour that
        # made tools/verify_prior004_defender_exclusion.py sections 3-7 fail.)
        try:
            import app.config_persist as _cp
            _cp._config_cache = None
        except Exception:
            pass
        try:
            tt._restore_reg_values(_ctx2, "hags")
            _msg = None
        except RuntimeError as exc:
            _msg = str(exc)
        except Exception:                             # noqa: BLE001
            _msg = None
        ck("the task's own key is NOT refused by the allowlist",
           not (_msg and "not defined to" in _msg), _msg)
finally:
    pass

print()
print("[8] PREFIX rules: disable_nagle writes a per-interface subkey")
# disable_nagle enumerates network interfaces and writes a DIFFERENT subkey per
# interface GUID, so no fixed path can be allowlisted. _TASK_REG_ALLOW_PREFIX
# expresses it as hive + path prefix + value name. The two halves must both
# hold, which is what the REFUSED cases below pin.
_I = r"SYSTEM\CurrentControlSet\Services\Tcpip\Parameters\Interfaces"
ck("the prefix table exists and covers disable_nagle",
   "disable_nagle" in tt._TASK_REG_ALLOW_PREFIX)
for label, (h, p, n), want in (
    ("a real per-interface GUID", ("HKLM", _I + r"\{1a2b3c}", "TcpAckFrequency"), True),
    ("the other value it writes", ("HKLM", _I + r"\{dead-beef}", "TCPNoDelay"), True),
    ("the prefix key itself", ("HKLM", _I, "TCPNoDelay"), True),
    ("lowercased + forward slashes",
     ("HKLM", _I.lower().replace("\\", "/") + "/{x}", "tcpackfrequency"), True),
    # ---- the interesting half ----
    ("a SIBLING value under the same key", ("HKLM", _I + r"\{x}", "SysctlIp"), False),
    ("the PARENT key, not under Interfaces",
     ("HKLM", r"SYSTEM\CurrentControlSet\Services\Tcpip\Parameters", "TcpAckFrequency"), False),
    ("segment-boundary bypass (InterfacesXyz)",
     ("HKLM", r"SYSTEM\CurrentControlSet\Services\Tcpip\Parameters\InterfacesXyz",
      "TcpAckFrequency"), False),
    ("traversal OUT of the prefix with ..",
     ("HKLM", _I + r"\..\..\evil", "TcpAckFrequency"), False),
    ("deep traversal out of the prefix",
     ("HKLM", _I + r"\..\..\..\..\evil", "TcpAckFrequency"), False),
    ("right path, WRONG HIVE", ("HKCU", _I + r"\{x}", "TcpAckFrequency"), False),
):
    ck("disable_nagle %-38s -> %s" % (label, want),
       A("disable_nagle", h, p, n) is want, A("disable_nagle", h, p, n))

print()
print("[8b] the normaliser resolves . and .. (that is what closes the bypass)")
ck("dotdot collapses lexically",
   tt._norm_reg_spec("HKLM", _I + r"\..\..\evil", "x")[1].endswith(r"\tcpip\evil"),
   tt._norm_reg_spec("HKLM", _I + r"\..\..\evil", "x")[1])
ck("single dot segments are dropped",
   tt._norm_reg_spec("HKLM", r"A\.\B", "x")[1] == "a\\b",
   tt._norm_reg_spec("HKLM", r"A\.\B", "x")[1])
ck("doubled separators collapse",
   tt._norm_reg_spec("HKLM", r"A\\\\B", "x")[1] == "a\\b")
ck("SEC-001 is documented at the prefix table", "SEC-001" in _src)
ck("the prefix table is a module literal, not config-derived",
   "_TASK_REG_ALLOW_PREFIX = {" in _src)

print()
print()
print("[9] the hand-transcribed entries are checked, and named as weaker")
# taskbar_cleanup's specs come from FUNCTION-LOCAL variables, so no
# module-constant derivation can reach them and they are transcribed by hand.
# The automatic drift guard cannot prove completeness for that one task, so it
# is excluded from the completeness check EXPLICITLY, and instead each entry is
# checked to appear literally in the owning apply function. Weaker - and
# labelled as such rather than quietly folded into the strong check.
MANUAL = tt._TASK_REG_ALLOWLIST_MANUAL
ck("there is exactly one hand-transcribed task", set(MANUAL) == {"taskbar_cleanup"},
   sorted(MANUAL))
_derive = set(_expected)
ck("the manual task is the only one outside the derivation",
   set(MANUAL) - _derive == {"taskbar_cleanup"}, sorted(set(MANUAL) - _derive))
ck("...and every other derived task is in the table",
   not (_derive - set(TABLE) - set(MANUAL)),
   sorted(_derive - set(TABLE) - set(MANUAL)))
_fn = next((n for n in ast.walk(_src_tree)
            if isinstance(n, ast.FunctionDef) and n.name == "apply_taskbar_cleanup"), None)
ck("the owning apply function exists", _fn is not None)
_body = (ast.get_source_segment(_src, _fn) or "") if _fn is not None else ""
for _h, _p, _n in sorted(MANUAL["taskbar_cleanup"]):
    ck("transcribed value %r appears literally in apply_taskbar_cleanup" % _n,
       _n in _body, _n)
ck("SEC-001 is documented at the manual table",
   "cannot reach" in _src and "UNFILTERED superset" in _src)
ck("a foreign target is refused for a manual task",
   A("taskbar_cleanup", "HKLM", r"SOFTWARE\Microsoft\Windows\CurrentVersion\Run",
     "Evil") is False)
ck("...a sibling value under its own key is refused too",
   A("taskbar_cleanup", "HKCU",
     r"Software\Microsoft\Windows\CurrentVersion\Explorer\Advanced",
     "TaskbarSi") is False)
ck("...while its own key is allowed",
   A("taskbar_cleanup", "HKCU",
     r"Software\Microsoft\Windows\CurrentVersion\Explorer\Advanced",
     "TaskbarDa") is True)


if FAILS:
    print("PRIOR SEC-001 VERIFY: %d/%d FAILED -> %s" % (FAILS, CHECKS, FAILS))
    sys.exit(1)
print("PRIOR SEC-001 VERIFY: ALL PASS (%d checks)" % CHECKS)
