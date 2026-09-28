"""MED-007: fourteen tweaks' Undo destroyed pre-existing user settings.

The finding: these tweaks Undo'd with a blind `reg_delete_value` (or by writing
a hardcoded "Windows default" that was not the user's value). Both destroy a
pre-existing different setting - a user who had already turned something off
loses that choice, and one who had it on gets it silently changed. It also
falsifies the README promise that every tweak's Undo is a full, verified revert.

Two examples the finding calls out specifically:
  * `disable_nagle` wrote TcpAckFrequency/TCPNoDelay on EVERY adapter and
    blind-deleted both from EVERY adapter, so a VPN interface deliberately set
    to TcpAckFrequency=2 (Windows' delayed-ACK default) lost that.
  * `hide_recent`'s legacy fallback wrote 1, "the documented Windows default" -
    but the default is 0/absent, so Undo ENABLED the very thing it was undoing.

The fix routes all fourteen through `_snap_reg_values` / `_restore_reg_values`
(via the new `_regn_apply` / `_regn_revert`), and for `classic_context_menu`
restores a single VALUE instead of deleting the whole CLSID key.

No real registry or config is touched: winreg and the config-persist seams are
faked, and the helpers' local `from app.config_persist import ...` is
neutralised too.
"""

from __future__ import annotations

import sys

sys.path.insert(0, ".")

import app.config_persist as cp          # noqa: E402
from app.tasks import tweak_tasks as tt  # noqa: E402

FAILS = 0
CHECKS = 0

class Ctx:
    def __init__(self):
        self.lines = []

    def log(self, m, *a, **k):
        self.lines.append(str(m))

    def set_status(self, *a, **k):
        pass

    def cancelled(self):
        return False


# ---------------------------------------------------------------------------
# Mine each tweak's ACTUAL (hive, path, name, value) from its _regn_apply
# call, rather than hardcoding a guess. My first version of this table had
# four wrong value NAMES (StartupDelay vs StartupDelayInMSec,
# DoNotUseDeliveryOptimization vs DODownloadMode, ...) and every one of those
# guesses turned into a FAIL that looked like a code bug. The registry names
# are now read out of the code under test, so the table cannot drift.
# ---------------------------------------------------------------------------
import ast  # noqa: E402
import inspect  # noqa: E402

_MODULE_TREE = ast.parse(inspect.getsource(tt))


def _mine(base):
    """(task_id, [(hive, path, name, value), ...]) from apply_<base>."""
    fn = next((n for n in _MODULE_TREE.body
               if isinstance(n, ast.FunctionDef)
               and n.name == "apply_" + base), None)
    assert fn is not None, "no apply_%s" % base
    call = next((n for n in ast.walk(fn)
                 if isinstance(n, ast.Call)
                 and getattr(n.func, "id", "") == "_regn_apply"), None)
    assert call is not None, "apply_%s does not call _regn_apply" % base
    task_id = ast.literal_eval(call.args[1])
    entries = []
    for elt in ast.literal_eval(call.args[2]):
        hive, path, name, value = elt[0], elt[1], elt[2], elt[3]
        entries.append((hive, path, name, value))
    return task_id, entries


BASES = ["limit_telemetry", "startup_delay", "background_apps_disable",
         "consumer_features_disable", "delivery_optimization_disable",
         "suppress_crash_popups", "no_update_reboot", "mpo_fix"]

print("  mined specs:")
for _b in BASES:
    _tid, _ents = _mine(_b)
    print("    %-30s %-26s %s" % (_b, _tid,
                                   ", ".join(e[2] for e in _ents)))


def ck(label, cond, detail=""):
    global FAILS, CHECKS
    CHECKS += 1
    if cond:
        print("  PASS  %s" % label)
    else:
        FAILS += 1
        print("  FAIL  %s%s" % (label, ("  <- " + str(detail)) if detail else ""))


class Ctx:
    def __init__(self):
        self.lines = []

    def log(self, m, *a, **k):
        self.lines.append(str(m))

    def set_status(self, *a, **k):
        pass

    def cancelled(self):
        return False


class Reg:
    """In-memory registry + snapshot store, mirroring the real
    save_tweak_snapshot guard (only write when no snapshot exists)."""

    def __init__(self, pre=None, set_ok=True):
        self.reg = dict(pre or {})
        self.set_ok = set_ok
        self.snap = {}
        self.deleted = []
        self.written = []

    def get_typed(self, ctx, hive, path, name):
        k = (hive, path, name)
        if k in self.reg:
            return self.reg[k], "REG_DWORD"
        return None, None

    def set_value(self, ctx, hive, path, name, value, value_type="REG_DWORD"):
        if not self.set_ok:
            return False
        self.reg[(hive, path, name)] = value
        self.written.append((hive, path, name, value))
        return True

    def set_checked(self, ctx, hive, path, name, value, value_type="REG_DWORD"):
        if not self.set_ok:
            raise RuntimeError("simulated write failure")
        self.reg[(hive, path, name)] = value
        self.written.append((hive, path, name, value))

    def delete(self, ctx, hive, path, name):
        self.deleted.append((hive, path, name))
        # Must actually REMOVE the value: the whole point of the absent-prior
        # case is that Undo puts it back to absent. A delete that only logged
        # left the value in place and made every [2] check fail for a reason
        # that had nothing to do with the code under test.
        self.reg.pop((hive, path, name), None)
        return True

    def save(self, task_id, values):
        if not values or self.snap.get(task_id):
            return
        self.snap[task_id] = dict(values)

    def get(self, task_id):
        return self.snap.get(task_id) or {}

    def clear(self, task_id):
        self.snap.pop(task_id, None)

    def __enter__(self):
        self._s = (tt.reg_set_value, tt.reg_set_value_checked,
                   tt.reg_delete_value, tt.save_tweak_snapshot,
                   tt.get_tweak_snapshot, tt.clear_tweak_snapshot,
                   cp.save_tweak_snapshot, cp.get_tweak_snapshot,
                   cp.clear_tweak_snapshot)
        import app.utils as u
        self._u = (u.reg_get_value_typed,)
        tt.reg_set_value = self.set_value
        tt.reg_set_value_checked = self.set_checked
        tt.reg_delete_value = self.delete
        tt.save_tweak_snapshot = self.save
        tt.get_tweak_snapshot = self.get
        tt.clear_tweak_snapshot = self.clear
        cp.save_tweak_snapshot = self.save
        cp.get_tweak_snapshot = self.get
        cp.clear_tweak_snapshot = self.clear
        u.reg_get_value_typed = self.get_typed
        return self

    def __exit__(self, *e):
        (tt.reg_set_value, tt.reg_set_value_checked, tt.reg_delete_value,
         tt.save_tweak_snapshot, tt.get_tweak_snapshot, tt.clear_tweak_snapshot,
         cp.save_tweak_snapshot, cp.get_tweak_snapshot,
         cp.clear_tweak_snapshot) = self._s
        import app.utils as u
        u.reg_get_value_typed = self._u[0]
        return False



print()
print("[1] a pre-existing value SURVIVES apply -> undo (the whole point)")
for base in BASES:
    task_id, entries = _mine(base)
    PRIOR = 7          # a value the user chose, which is NOT the Windows default
    pre = {(h, p, n): PRIOR for h, p, n, _v in entries}
    with Reg(pre=pre) as r:
        getattr(tt, "apply_" + base)(Ctx())
        ck("%-30s apply overwrote the user's value" % base,
           all(r.reg.get((h, p, n)) == v for h, p, n, v in entries), r.reg)
        getattr(tt, "revert_" + base)(Ctx())
        ck("%-30s Undo put the USER'S value back" % base,
           all(r.reg.get((h, p, n)) == PRIOR for h, p, n, _v in entries), r.reg)
        ck("%-30s Undo did not blind-delete" % base,
           not [d for d in r.deleted if d in {(h, p, n) for h, p, n, _v in entries}],
           r.deleted)

print()
print()
print("[2] a previously-ABSENT value comes back absent, not invented")
for base in BASES:
    task_id, entries = _mine(base)
    with Reg(pre={}) as r:
        getattr(tt, "apply_" + base)(Ctx())
        ck("%-30s apply created the value(s)" % base,
           all((h, p, n) in r.reg for h, p, n, _v in entries), sorted(r.reg))
        getattr(tt, "revert_" + base)(Ctx())
        ck("%-30s Undo removed them again (absent -> absent)" % base,
           all((h, p, n) not in r.reg for h, p, n, _v in entries), sorted(r.reg))

print()
print()
print("[3] no snapshot is left behind after a clean Undo")
for base in BASES:
    task_id, _ = _mine(base)
    with Reg(pre={}) as r:
        getattr(tt, "apply_" + base)(Ctx())
        getattr(tt, "revert_" + base)(Ctx())
        ck("%-30s snapshot cleared" % base, task_id not in r.snap, sorted(r.snap))

print()
print("[4] a snapshot-less Undo RAISES instead of guessing (legacy installs)")
for base in BASES[:4]:
    with Reg(pre={}) as r:
        err = None
        try:
            getattr(tt, "revert_" + base)(Ctx())
        except Exception as exc:
            err = exc
        ck("%-30s raised" % base, err is not None, "returned normally")
        ck("%-30s ...and wrote nothing" % base, r.written == [], r.written)

print()
print("[5] a FAILED apply leaves no Undo record for a change that never happened")
for base in BASES[:4]:
    task_id, _ = _mine(base)
    with Reg(pre={}, set_ok=False) as r:
        err = None
        try:
            getattr(tt, "apply_" + base)(Ctx())
        except Exception as exc:
            err = exc
        ck("%-30s apply raised" % base, err is not None, "returned normally")
        ck("%-30s snapshot dropped" % base, task_id not in r.snap, sorted(r.snap))

print()
print("[6] disable_nagle: per-adapter priors, the worst case in the finding")
NAGLE_BASE = "SYSTEM\\CurrentControlSet\\Services\\Tcpip\\Parameters\\Interfaces"
ADAPTERS = ["{A}", "{B}", "{C}"]
# Adapter B deliberately has the Windows delayed-ACK default; C has Nagle on.
PRE = {}
for a in ADAPTERS:
    PRE[("HKLM", "%s\\%s" % (NAGLE_BASE, a), "TcpAckFrequency")] = 2
    PRE[("HKLM", "%s\\%s" % (NAGLE_BASE, a), "TCPNoDelay")] = 0
with Reg(pre=PRE) as r:
    tt._network_interface_subs = lambda: list(ADAPTERS)
    try:
        tt.apply_disable_nagle(Ctx())
        for a in ADAPTERS:
            ck("apply set %s TcpAckFrequency=1" % a,
               r.reg[("HKLM", "%s\\%s" % (NAGLE_BASE, a), "TcpAckFrequency")] == 1)
        tt.revert_disable_nagle(Ctx())
        for a in ADAPTERS:
            for vn, want in (("TcpAckFrequency", 2), ("TCPNoDelay", 0)):
                got = r.reg.get(("HKLM", "%s\\%s" % (NAGLE_BASE, a), vn))
                ck("Undo restored %s %s to %s (per-adapter)" % (a, vn, want),
                   got == want, "got %r" % (got,))
        ck("nothing was blind-deleted on any adapter", r.deleted == [], r.deleted)
    finally:
        del tt._network_interface_subs

print()
print("[7] classic_context_menu: restores a VALUE, never deletes the CLSID")
CLSID = "{86ca1aa0-34aa-4e8b-a509-50c905bae2a2}"
KEY = "Software\\Classes\\CLSID\\%s" % CLSID
VAL = "Software\\Classes\\CLSID\\%s\\InprocServer32" % CLSID
rev_src = inspect.getsource(tt.revert_classic_context_menu)
ck("revert no longer calls reg_delete_key",
   "reg_delete_key(" not in rev_src.replace("reg_delete_key on the WHOLE", "")
   and "reg_delete_key(ctx" not in rev_src, rev_src[:200])
ck("revert restores the snapshot", "_regn_revert" in rev_src)
ck("revert prunes the key only if empty", "_prune_key_if_empty" in rev_src)
prune = inspect.getsource(tt._prune_key_if_empty)
ck("the prune refuses to touch a key that still holds data",
   "QueryValueEx" in prune and "still holds data" in prune)
ck("the prune refuses to touch a key that still has subkeys",
   "QueryInfoKey" in prune and "still has subkeys" in prune)

print()
print("[8] hide_recent's legacy fallback no longer writes 1 (the harmful guess)")
hr = inspect.getsource(tt.revert_hide_recent)
ck('it no longer writes ShowRecent = 1', '"ShowRecent", 1' not in hr)
ck('it no longer writes ShowFrequent = 1', '"ShowFrequent", 1' not in hr)
ck('it writes 0 (the real Windows default) instead',
   '"ShowRecent", 0' in hr and '"ShowFrequent", 0' in hr)
ck("...and admits the prior value is unknown", "unknown" in hr)

print()
print("[9] structural: no blind delete / hardcoded default left in the 14")
REG = {k: v for k, v in tt.__dict__.items()
       if callable(v) and getattr(v, "__name__", "").startswith(
           ("apply_", "revert_"))}
REVERTS = ["limit_telemetry", "startup_delay", "background_apps_disable",
           "consumer_features_disable", "delivery_optimization_disable",
           "fullscreen_optimizations_disable", "suppress_crash_popups",
           "no_update_reboot", "mpo_fix", "disable_sticky_keys",
           "disable_nagle", "classic_context_menu", "usb_suspend", "hide_recent"]
for b in REVERTS:
    fn = getattr(tt, "revert_" + b, None)
    ck("revert_%s exists" % b, fn is not None)
    if fn is None:
        continue
    calls = set()
    for n in ast.walk(ast.parse(inspect.getsource(fn))):
        if isinstance(n, ast.Call):
            f = n.func
            calls.add(f.id if isinstance(f, ast.Name) else getattr(f, "attr", ""))
    ck("revert_%-30s has no reg_delete_* call" % b,
       not (calls & {"reg_delete_value", "reg_delete_key"}), sorted(calls))
    ck("revert_%-30s restores from a snapshot" % b,
       bool(calls & {"_regn_revert", "_restore_reg_values",
                     "_restore_powercfg_pairs"}), sorted(calls))
    ap = getattr(tt, "apply_" + b, None)
    acalls = set()
    for n in ast.walk(ast.parse(inspect.getsource(ap))):
        if isinstance(n, ast.Call):
            f = n.func
            acalls.add(f.id if isinstance(f, ast.Name) else getattr(f, "attr", ""))
    ck("apply_%-31s snapshots first" % b,
       bool(acalls & {"_regn_apply", "_snap_reg_values",
                      "_snapshot_powercfg_pairs"}), sorted(acalls))

print()
print("[10] the persistence guard still blocks what it must")
# This section exists because MED-007 had to NARROW that guard: the old rule
# blocked any path containing "\services", which also matched
# Services\Tcpip\Parameters\Interfaces - so disable_nagle's Undo raised
# "Refusing to restore ... persistence location" every time. Narrowing a
# security guard with no test on either side of the change is how you turn a
# fix into a vulnerability, so both halves are pinned here.
MUST_BLOCK = [
    ("HKLM", "SYSTEM\\CurrentControlSet\\Services\\Tcpip\\Start", "Start"),
    ("HKLM", "SYSTEM\\CurrentControlSet\\Services\\Tcpip", "ImagePath"),
    ("HKLM", "SYSTEM\\CurrentControlSet\\Services\\AnySvc\\Parameters",
     "ServiceDll"),          # no Parameters *subtree* -> still a service key
    ("HKCU", "Software\\Microsoft\\Windows\\CurrentVersion\\Run", "Backdoor"),
    ("HKCU", "Software\\Microsoft\\Windows\\CurrentVersion\\RunOnce", "X"),
    ("HKLM", "SYSTEM\\CurrentControlSet\\Control\\Session Manager\\"
             "AppInit_DLLs", "AppInit_DLLs"),
    ("HKLM", "SOFTWARE\\Microsoft\\Windows NT\\CurrentVersion\\Image File "
             "Execution Options\\foo.exe", "Debugger"),
    ("HKLM", "SYSTEM\\CurrentControlSet\\Control\\Winlogon", "Shell"),
    ("HKLM", "SECURITY\\Policy\\System\\LSA", "RunAsPPL"),
]
MUST_ALLOW = [
    ("HKLM", "SYSTEM\\CurrentControlSet\\Services\\Tcpip\\Parameters\\"
             "Interfaces\\{A}", "TcpAckFrequency"),
    ("HKLM", "SYSTEM\\CurrentControlSet\\Services\\Tcpip\\Parameters\\"
             "Interfaces\\{A}", "TCPNoDelay"),
]


def _restore_with_spec(spec):
    """Drive _restore_reg_values with a hand-built snapshot for `spec`."""
    hive, path, name = spec
    snap = {"specs": [[hive, path, name]], "0:present": True,
            "0:type": "REG_DWORD", "0:value": 1}
    with Reg() as r:
        r.snap["_guardtest"] = snap
        try:
            tt._restore_reg_values(Ctx(), "_guardtest")
            return None
        except Exception as exc:
            return exc


for spec in MUST_BLOCK:
    err = _restore_with_spec(spec)
    ck("BLOCKED  %s" % spec[2], err is not None and "persistence" in str(err),
       "allowed through: %r" % (err,))
for spec in MUST_ALLOW:
    err = _restore_with_spec(spec)
    ck("ALLOWED  %s\\%s" % (spec[1].rsplit("\\", 1)[-1], spec[2]),
       err is None, "wrongly blocked: %r" % (err,))

print()
if FAILS:
    print("MED-007 VERIFY: %d/%d FAILED" % (FAILS, CHECKS))
    sys.exit(1)
print("MED-007 VERIFY: ALL PASS (%d checks)" % CHECKS)
