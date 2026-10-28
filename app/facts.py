"""One Fact Store: the machine's nouns, measured once and shared by tools.

AUDIT TX-001 (2026-10). Before this module every tool measured the machine
for itself: PC Specs read the registry and WMI, Hardware Monitor read the
same registry again, Drive Toolkit walked the logical drives, Storage Insight
had its own walker, and Health had a fourth set of sensors. The same fact -
"C: has 91 GB free", "this is an RTX 4070" - could be measured four times in
one session, disagree, and cost four separate stalls.

This is the Tk-free half of the ecosystem: tools ask for a fact, the store
remembers how it was measured and when. It does not decide what a fact means
(that is the findings bus, TX-002) and it does not draw anything.

    from app import facts
    f = facts.get("disks.usage")
    if f.ok:
        for volume in f.value:
            ...

The contract:

  * one provider per key. Registering a second reader for the same key is a
    programming error and raises, because two readers is how the old
    disagreement started;
  * `Fact.ok` is first-class. A failed read is a result with a reason, not an
    exception and not a silent `None`, so "unknown" stays honest everywhere;
  * providers are lazy imports of the existing engines. No engine's public
    function changes and no tool is forced to move its read onto another
    thread: each adopter keeps the threading model it already had;
  * `get()` is synchronous and single-flight per key. A dialog that already
    reads on a worker keeps doing so; a cheap registry read can stay on the
    Tk thread without a second read starting behind it;
  * `invalidate()` drops the cached Fact and clears the engine's own memo, so
    a driver install does not leave yesterday's GPU in the store;
  * `subscribe()` tells an open surface when another tool refreshed a fact it
    is showing (Storage's disk bar after an uninstall, for example). The
    callback runs on the measuring thread, so a widget subscriber must hop to
    Tk with its dialog's dispatcher; the store never touches the UI itself.

TTLs are declared with the provider. Cheap identity facts live for a day;
capacity and startup state are minutes; live memory is seconds. The store
never invents a value: when a provider fails, the Fact carries the reason and
the previous success is not passed off as current.

Providers live below the UI layer (`pc_specs`, `hw_monitor`, `startup_manager`)
and are imported inside the readers, so importing this module is cheap and
does not pull in Tk.
"""
from __future__ import annotations

import inspect
import logging
import threading
import time
import weakref
from dataclasses import dataclass
from typing import Any, Callable

log = logging.getLogger(__name__)

#: Static hardware identity. A driver install invalidates it explicitly;
#: the TTL only bounds how long a machine that changed underneath the app
#: (a RAM stick added while it was open) can go unnoticed.
HARDWARE_TTL = 24 * 60 * 60

#: Free space is a moving number. Volume labels and filesystems rarely
#: change, but capacity does, and the same Fact feeds every drive picker.
DISK_TTL = 60.0

#: GlobalMemoryStatusEx is a ctypes read measured in microseconds.
MEMORY_TTL = 5.0

#: Registry Run keys and Startup-folder shortcuts change only when a user or
#: an installer changes them. A toggle invalidates this immediately.
STARTUP_TTL = 5 * 60.0

#: A failure is cached just long enough that opening three tools does not
#: spawn the same failing read three times, and short enough that a retry is
#: not punished for the rest of the session.
FAILURE_TTL = 5.0


@dataclass(frozen=True)
class Fact:
    """One measured value, with its provenance and honesty.

    `value` is the engine's own shape, normalised only where two tools need
    the same field (a tuple becomes a dict, a list of tuples becomes a list of
    dicts). It is shared by reference; callers must treat it as read-only.
    """

    key: str
    value: Any
    source: str
    as_of: float
    ok: bool = True
    error: str = ""

    @property
    def age(self) -> float:
        """Seconds since this reading, never negative on a clock skew."""
        return max(0.0, time.time() - float(self.as_of or 0.0))


@dataclass(frozen=True)
class Provider:
    """How one key is measured. Registered once per key."""

    key: str
    fn: Callable[[], Any]
    ttl: float
    source: str
    #: Called by invalidate()/refresh() before the next measurement, so a
    #: provider that wraps a memoising engine (pc_specs) can drop its memo.
    on_invalidate: Callable[[], None] | None = None


@dataclass
class _Subscription:
    prefix: str
    ref: Any          # weakref.WeakMethod or a zero-arg callable returning fn


def _as_ref(callback):
    try:
        if inspect.ismethod(callback):
            return weakref.WeakMethod(callback)
    except Exception:
        pass
    return lambda: callback


class Facts:
    """A fact cache with one provider per key. Safe to instantiate in tests."""

    def __init__(self, clock: Callable[[], float] | None = None):
        self._clock = clock or time.time
        self._providers: dict[str, Provider] = {}
        self._cache: dict[str, Fact] = {}
        self._locks: dict[str, threading.Lock] = {}
        self._lock = threading.RLock()
        self._subs: list[_Subscription] = []

    # ---- registration -------------------------------------------------- #

    def register(self, key, fn, ttl, *, source="", on_invalidate=None):
        """Register the one provider for `key`.

        A duplicate key is refused rather than silently overwritten: two
        readers for one fact is exactly the disagreement this module exists
        to end.
        """
        key = str(key or "").strip()
        if not key or "." not in key:
            raise ValueError("fact keys are dotted names, e.g. 'hardware.cpu'")
        if not callable(fn):
            raise TypeError("fact provider must be callable")
        ttl = float(ttl)
        if ttl <= 0:
            raise ValueError("fact TTL must be positive")
        with self._lock:
            if key in self._providers:
                raise ValueError("a provider is already registered for %r" % key)
            self._providers[key] = Provider(
                key=key, fn=fn, ttl=ttl,
                source=str(source or getattr(fn, "__qualname__", "provider")),
                on_invalidate=on_invalidate)
        return self

    def providers(self) -> tuple[str, ...]:
        with self._lock:
            return tuple(sorted(self._providers))

    def provider(self, key) -> Provider | None:
        with self._lock:
            return self._providers.get(str(key))

    # ---- reading ------------------------------------------------------- #

    def get(self, key, *, max_age=None) -> Fact:
        """The current Fact for `key`.

        `max_age=None` uses the provider's TTL. A smaller number asks for a
        younger reading; `0` measures now (and clears the engine's memo via
        the provider's invalidation hook). Unknown keys and provider failures
        both come back as `ok=False`; this method does not raise.
        """
        key = str(key or "")
        now = self._clock()
        with self._lock:
            provider = self._providers.get(key)
        if provider is None:
            return Fact(key=key, value=None, source="facts", as_of=now,
                        ok=False,
                        error="No reader is registered for %r." % key)

        ttl = provider.ttl if max_age is None else max(0.0, float(max_age))
        if ttl > 0:
            cached = self._fresh(key, ttl, now)
            if cached is not None:
                return cached

        with self._key_lock(key):
            now = self._clock()
            if ttl > 0:
                cached = self._fresh(key, ttl, now)
                if cached is not None:
                    return cached
            fact = self._measure(provider, fresh=(ttl == 0))
            with self._lock:
                self._cache[key] = fact
        self._notify(key, fact)
        return fact

    def refresh(self, *keys) -> dict[str, Fact]:
        """Measure now, bypassing both the Fact cache and engine memos.

        With no keys, every registered provider is refreshed. Returns the new
        Facts keyed by name, for callers that want the value immediately.
        """
        wanted = [str(k) for k in keys if str(k)] or list(self.providers())
        # One invalidation pass clears every engine memo these keys share;
        # measuring each key must not clear it again per key.
        self.invalidate(*wanted)
        out = {}
        for key in wanted:
            with self._lock:
                provider = self._providers.get(key)
            if provider is None:
                continue
            with self._key_lock(key):
                fact = self._measure(provider)
                with self._lock:
                    self._cache[key] = fact
            self._notify(key, fact)
            out[key] = fact
        return out

    def clear(self) -> None:
        """Forget cached Facts but keep the registered readers."""
        with self._lock:
            self._cache.clear()

    def invalidate(self, *keys) -> None:
        """Drop cached Facts and let each engine drop its own memo.

        No notification: nothing new was measured. Use refresh() when an open
        surface should be told about a new value.
        """
        wanted = [str(k) for k in keys if str(k)] or list(self.providers())
        hooks = []
        with self._lock:
            for key in wanted:
                self._cache.pop(key, None)
                provider = self._providers.get(key)
                if provider is not None and provider.on_invalidate is not None:
                    hooks.append(provider.on_invalidate)
        for hook in dict.fromkeys(hooks):
            try:
                hook()
            except Exception:
                log.debug("fact invalidation hook failed", exc_info=True)

    # ---- subscriptions ------------------------------------------------- #

    def subscribe(self, prefix, callback):
        """Call `callback(fact)` whenever a key under `prefix` is measured.

        `prefix=""` subscribes to every fact. Bound methods are held weakly,
        so a closed dialog is not kept alive by the store. The callback runs
        on whichever thread measured the fact; UI code must dispatch it to
        Tk. Returns an unsubscribe callable.
        """
        if not callable(callback):
            raise TypeError("subscription callback must be callable")
        sub = _Subscription(prefix=str(prefix or ""), ref=_as_ref(callback))
        with self._lock:
            self._subs.append(sub)

        def _off():
            with self._lock:
                try:
                    self._subs.remove(sub)
                except ValueError:
                    pass

        return _off

    def _notify(self, key, fact):
        # Invoke callbacks OUTSIDE the lock: a subscriber is UI code and may
        # call back into the store, and a slow paint must never block a fact
        # read on another thread. Remove only dead refs, not the whole list:
        # a subscriber added during this pass must survive.
        callbacks = []
        dead = []
        with self._lock:
            for sub in list(self._subs):
                if not (sub.prefix == "" or key == sub.prefix
                        or key.startswith(sub.prefix + ".")):
                    continue
                fn = sub.ref()
                if fn is None:
                    dead.append(sub)
                    continue
                callbacks.append(fn)
            for sub in dead:
                try:
                    self._subs.remove(sub)
                except ValueError:
                    pass
        for fn in callbacks:
            try:
                fn(fact)
            except Exception:
                log.debug("fact subscriber failed for %s", key, exc_info=True)

    # ---- internals ----------------------------------------------------- #

    def _key_lock(self, key) -> threading.Lock:
        with self._lock:
            lock = self._locks.get(key)
            if lock is None:
                lock = threading.Lock()
                self._locks[key] = lock
            return lock

    def _fresh(self, key, ttl, now):
        with self._lock:
            fact = self._cache.get(key)
        if fact is None:
            return None
        limit = ttl if fact.ok else min(ttl, FAILURE_TTL)
        return fact if max(0.0, now - fact.as_of) <= limit else None

    def _measure(self, provider, *, fresh=False) -> Fact:
        if fresh and provider.on_invalidate is not None:
            try:
                provider.on_invalidate()
            except Exception:
                log.debug("fact %s could not clear its engine memo", provider.key,
                          exc_info=True)
        try:
            value = provider.fn()
        except Exception as exc:
            detail = str(exc).strip() or type(exc).__name__
            return Fact(key=provider.key, value=None, source=provider.source,
                        as_of=self._clock(), ok=False, error=detail)
        return Fact(key=provider.key, value=value, source=provider.source,
                    as_of=self._clock())


# --------------------------------------------------------------------------- #
# Readers. Each wraps one existing engine and normalises only the shape two
# tools need to agree on; it never measures anything the engine already does.
# --------------------------------------------------------------------------- #
def _clear_pc_specs_memo():
    from app import pc_specs
    pc_specs.clear_cache()


def _read_os():
    from app import pc_specs
    caption, build = pc_specs.os_info()
    return {"caption": caption, "build": build}


def _read_cpu():
    from app import pc_specs
    name, detail = pc_specs.cpu_info()
    return {"name": name, "detail": detail}


def _read_ram():
    from app import pc_specs
    headline, detail = pc_specs.ram_info()
    return {"headline": headline, "detail": detail}


def _read_board():
    from app import pc_specs
    name, firmware = pc_specs.board_info()
    return {"name": name, "firmware": firmware}


def _read_gpu():
    from app import hw_monitor
    return [dict(entry) for entry in (hw_monitor.gpu_list() or [])]


def _read_disks():
    from app import pc_specs
    return [dict(row) for row in (pc_specs.storage_info() or [])]


def _read_memory():
    from app import hw_monitor
    sample = hw_monitor.sample_ram()
    if not sample:
        raise RuntimeError("Memory usage could not be read.")
    total = int(sample.get("total_bytes") or 0)
    used = int(sample.get("used_bytes") or 0)
    return {
        "total": total,
        "used": used,
        "available": max(0, total - used),
        "percent": float(sample.get("percent") or 0.0),
    }


def _read_startup():
    from app import startup_manager
    return list(startup_manager.list_startup_items() or [])


# --------------------------------------------------------------------------- #
# The process-wide store. This is what tools import.
# --------------------------------------------------------------------------- #
_FACTS = Facts()


def _register_defaults(store: Facts):
    store.register("hardware.os", _read_os, HARDWARE_TTL,
                   source="pc_specs.os_info",
                   on_invalidate=_clear_pc_specs_memo)
    store.register("hardware.cpu", _read_cpu, HARDWARE_TTL,
                   source="pc_specs.cpu_info",
                   on_invalidate=_clear_pc_specs_memo)
    store.register("hardware.ram", _read_ram, HARDWARE_TTL,
                   source="pc_specs.ram_info",
                   on_invalidate=_clear_pc_specs_memo)
    store.register("hardware.board", _read_board, HARDWARE_TTL,
                   source="pc_specs.board_info",
                   on_invalidate=_clear_pc_specs_memo)
    # hw_monitor.gpu_list() is a registry read with no memo of its own.
    store.register("hardware.gpu", _read_gpu, HARDWARE_TTL,
                   source="hw_monitor.gpu_list")
    store.register("disks.usage", _read_disks, DISK_TTL,
                   source="pc_specs.storage_info",
                   on_invalidate=_clear_pc_specs_memo)
    store.register("memory.usage", _read_memory, MEMORY_TTL,
                   source="hw_monitor.sample_ram")
    store.register("system.startup", _read_startup, STARTUP_TTL,
                   source="startup_manager.list_startup_items")


_register_defaults(_FACTS)


def get(key, *, max_age=None):
    """The shared Fact for `key`. Never raises; check `fact.ok`."""
    return _FACTS.get(key, max_age=max_age)


def refresh(*keys):
    """Measure keys again and return the new Facts."""
    return _FACTS.refresh(*keys)


def invalidate(*keys):
    """Forget cached facts and their engines' memos."""
    return _FACTS.invalidate(*keys)


def subscribe(prefix, callback):
    """Notify `callback(fact)` when a fact under `prefix` is measured."""
    return _FACTS.subscribe(prefix, callback)


def clear():
    """Drop the shared cache (tests and a full diagnostic reset)."""
    return _FACTS.clear()


def providers():
    """The registered fact keys, sorted (diagnostics and tests)."""
    return _FACTS.providers()


__all__ = [
    "Facts", "Fact", "Provider", "FAILURE_TTL", "HARDWARE_TTL", "DISK_TTL",
    "MEMORY_TTL", "STARTUP_TTL", "get", "refresh", "invalidate", "subscribe",
    "clear", "providers",
]
