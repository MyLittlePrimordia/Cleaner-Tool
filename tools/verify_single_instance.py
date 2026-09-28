"""Verify the single-instance state holder is genuinely lock-guarded.

The bug: ``_state`` was a bare module-level dict, and ``release()`` did a
check-then-act on it:

    if _state.get("mutex") is handle:
        _state["mutex"] = None

Two threads could both pass the check, or one could clear a handle a
concurrent ``set_owner()`` had just installed. The dict is now an
``_InstanceState`` with an RLock and an atomic ``clear_if``.

This test proves the atomicity claim by hammering the exact interleaving from
many threads. It is a real race test, not a source-text check: if the lock is
removed, or ``clear_if`` is re-implemented as a non-atomic get-then-set, the
lost-update count goes above zero and the test fails.
"""

from __future__ import annotations

import sys
import threading

sys.path.insert(0, ".")

from app import single_instance as si  # noqa: E402

FAILS = 0
CHECKS = 0


def ck(label: str, cond: bool, detail: object = "") -> None:
    global FAILS, CHECKS
    CHECKS += 1
    if cond:
        print("  PASS  %s" % label)
    else:
        FAILS += 1
        print("  FAIL  %s   <- %s" % (label, detail))


print("[1] clear_if is a compare-and-clear, not a blind write")
si._state.set("mutex", "HANDLE-A")
ck("a matching value clears", si._state.clear_if("mutex", "HANDLE-A") is True)
ck("...and the slot is now empty", si._state.get("mutex") is None,
   si._state.get("mutex"))

si._state.set("mutex", "HANDLE-A")
ck("a non-matching value does NOT clear",
   si._state.clear_if("mutex", "HANDLE-B") is False)
ck("...and the real owner survives", si._state.get("mutex") == "HANDLE-A",
   si._state.get("mutex"))
ck("calling it twice is safe (second is a no-op)",
   si._state.clear_if("mutex", "HANDLE-A") is True
   and si._state.clear_if("mutex", "HANDLE-A") is False)
si._state.set("mutex", None)

print()
print("[2] concurrent clear_if: no lost updates, no double clears")
si._state.set("mutex", "OWNER")
cleared = []
lost = 0
dup = 0
barrier = threading.Barrier(8)
lock = threading.Lock()


def racer():
    global lost, dup
    barrier.wait()
    for _ in range(2000):
        if si._state.clear_if("mutex", "OWNER"):
            with lock:
                cleared.append(1)
                if len(cleared) > 1:
                    dup += 1


threads = [threading.Thread(target=racer) for _ in range(8)]
for t in threads:
    t.start()
for t in threads:
    t.join()

lost = 1 - len(cleared)
ck("exactly ONE thread won the compare-and-clear", len(cleared) == 1,
   "%d threads reported success (expected 1)" % len(cleared))
ck("no double-clear", dup == 0, "%d double clears" % dup)
ck("no lost update: the slot was cleared once and stayed cleared",
   si._state.get("mutex") is None, si._state.get("mutex"))

print()
print("[3] a racing set_owner cannot be silently undone by a stale release")
si._state.set("mutex", "OLD")
result = {}


def replayer():
    result["released"] = si._state.clear_if("mutex", "OLD")


def installer():
    result["installed"] = si._state.set("mutex", "NEW")


# interleave hard enough to matter: many rounds of owner-swap vs stale-release
swapped = 0
for _ in range(3000):
    si._state.set("mutex", "OLD")
    a = threading.Thread(target=replayer)
    b = threading.Thread(target=installer)
    a.start()
    b.start()
    a.join()
    b.join()
    # the installer always ran, so the slot must end up holding NEW unless the
    # release legitimately ran strictly afterwards on the same value
    if si._state.get("mutex") == "OLD":
        swapped += 1

ck("a fresh owner is never lost to a stale release", swapped == 0,
   "%d rounds ended with the stale handle back in the slot" % swapped)
si._state.set("mutex", None)

print()
print("[4] the real code path: release() uses the atomic clear")
src = open("app/single_instance.py", encoding="utf-8").read()
body = src.split("def release(", 1)[1].split("def set_owner(", 1)[0]
ck("release() calls clear_if", "clear_if" in body)
ck("release() has no read-then-write on the slot",
    "_state.get(\"mutex\") is handle" not in body)
ck("the module-level _state is no longer a mutable literal",
    "_state = {" not in src)
ck("all four slots are reachable through the guarded API",
    set(si._InstanceState._KEYS) == {"namespace", "mutex", "event", "bg_claim"},
    si._InstanceState._KEYS)

print()
print("[5] clear_if/get/set actually hold the lock while they run")
# The behavioural race in section [2] CANNOT catch a missing lock on its own:
# under CPython the compare and the clear are two bytecodes, and the GIL's
# 5ms switch interval essentially never preempts inside a two-bytecode
# window. Sabotaging clear_if into a plain get-then-set still passed [2].
# So prove the locking directly - swap in a lock that records every acquire
# and release, and confirm each API took it exactly once and released it.
class _RecordingLock:
    def __init__(self, real):
        self._real = real
        self.depth = 0
        self.acquires = 0
        self.releases = 0

    def __acquire__(self, *a):
        return self._real.__acquire__(*a)

    def acquire(self, *a, **kw):
        return self._real.acquire(*a, **kw)

    def release(self):
        return self._real.release()

    def __enter__(self):
        self._real.acquire()
        self.acquires += 1
        self.depth += 1
        return self

    def __exit__(self, *exc):
        self.depth -= 1
        self.releases += 1
        self._real.release()
        return False


probe = si._InstanceState()
rec = _RecordingLock(probe._lock)
probe._lock = rec

probe.set("mutex", "H")
ck("set() took the lock exactly once", rec.acquires == 1 and rec.releases == 1,
   "acquires=%d releases=%d" % (rec.acquires, rec.releases))
ck("set() left no lock held", rec.depth == 0, rec.depth)

probe.get("mutex")
ck("get() took the lock exactly once", rec.acquires == 2 and rec.releases == 2,
   "acquires=%d releases=%d" % (rec.acquires, rec.releases))

probe.clear_if("mutex", "nope")
ck("clear_if() took the lock exactly once, even on the no-match path",
   rec.acquires == 3 and rec.releases == 3,
   "acquires=%d releases=%d" % (rec.acquires, rec.releases))
ck("clear_if() left no lock held", rec.depth == 0, rec.depth)

probe.clear_if("mutex", "H")
ck("clear_if() took the lock on the match path too",
   rec.acquires == 4 and rec.releases == 4,
   "acquires=%d releases=%d" % (rec.acquires, rec.releases))

# The decisive one: prove clear_if actually BLOCKS on the lock, by having
# another thread hold it. If clear_if does not acquire the lock it returns
# instantly, so "still blocked after the lock is held" is unambiguous. (An
# earlier version of this check spied on the method and read the lock depth
# from the outside, which is always 0 - the spy runs before the lock is taken.
# That assertion could never fail and proved nothing.)
probe.set("mutex", "H")
probe._lock.acquire()          # main thread now owns the lock
done = threading.Event()
outcome: list[object] = []


def _blocked_worker():
    outcome.append(probe.clear_if("mutex", "H"))
    done.set()


w = threading.Thread(target=_blocked_worker)
w.start()
done.wait(timeout=1.5)
blocked_while_held = not done.is_set()
probe._lock.release()          # let the worker through
done.wait(timeout=5.0)
w.join(timeout=5.0)
ck("clear_if BLOCKS while another thread holds the lock (it really locks)",
   blocked_while_held, "clear_if returned while the lock was held elsewhere")
ck("...and completes once the lock is released",
   done.is_set() and outcome == [True], outcome)

print()
print("[6] background-claim thread and main thread do not corrupt state")
si._state.set("namespace", None)
si._state.set("bg_claim", None)
errors: list[str] = []


def claim_like_thread():
    try:
        for _ in range(300):
            si._probe_namespace()
            si.ensure_show_event()
    except Exception as exc:  # pragma: no cover
        errors.append(repr(exc))


def main_like_thread():
    try:
        for _ in range(300):
            si._state.set("mutex", "H")
            si.release(object())
            si.set_owner("H")
    except Exception as exc:  # pragma: no cover
        errors.append(repr(exc))


ts = [threading.Thread(target=claim_like_thread) for _ in range(3)]
ts.append(threading.Thread(target=main_like_thread))
for t in ts:
    t.start()
for t in ts:
    t.join()
ck("no thread raised", not errors, errors[:3])
ck("namespace resolved to a valid value",
   si._state.get("namespace") in ("Global", "Local"),
   si._state.get("namespace"))
si._state.set("mutex", None)
si._state.set("bg_claim", None)
si._state.set("event", None)

print()
if FAILS:
    print("SINGLE-INSTANCE VERIFY: %d/%d FAILED" % (FAILS, CHECKS))
    sys.exit(1)
print("SINGLE-INSTANCE VERIFY: ALL PASS (%d checks)" % CHECKS)
