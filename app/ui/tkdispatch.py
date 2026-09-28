"""Deliver work to the Tk thread from any thread, reliably.

Why this exists. Tkinter is not thread-safe, and `widget.after(0, fn)` called
from a worker thread is the classic way to get away with it *most of the
time* — which is worse than not doing it at all. Whether the call lands
depends on whether the main thread happens to be inside Tcl's event loop when
the cross-thread notifier fires. A measured case: the Uninstall Programs
dialog scans installed programs on a worker and posts the result back with
`self._dlg.after(0, ...)`. Driving that dialog headlessly, the callback was
dropped every single time — the list stayed empty for 15 seconds of pumping.
The user usually sees the list, which is exactly why this survived: it is
intermittent, so it looks like "sometimes the list takes a moment".

The codebase already learned this lesson elsewhere. The Game Session pilot
used to call `root.after()` straight from its watcher thread; that "occasional
cross-thread Tcl notifier race could drop that call and leave the
apply/revert pending forever", so the watcher was changed to touch nothing but
a `queue.Queue` drained by a Tk-thread pump (see app/ui/session.py). This
module is that same pattern, generalised, because the dialogs needed it too.

Contract:
  * `post(fn)` — callable from ANY thread. Never touches Tk.
  * the pump runs on the Tk thread only, and reschedules itself
  * `stop()` cancels the pending `after`; safe to call twice
  * an exception in a posted callback is reported once, and never breaks the
    pump or the other queued work
"""
from __future__ import annotations

import queue
import threading


class TkDispatcher:
    """A one-way bridge: any thread in, the Tk thread out."""

    #: pump cadence in ms. 30ms is imperceptible for icon swaps and costs
    #: nothing — the queue is empty almost always and get_nowait() on an
    #: empty queue is a few microseconds.
    PUMP_MS = 30

    def __init__(self, widget, on_error=None):
        # `widget` is the toplevel that owns the event loop. It is captured
        # for after()/after_cancel() ONLY — never touched from post().
        self._widget = widget
        self._queue = queue.Queue()
        self._lock = threading.Lock()
        self._running = False
        self._after_id = None
        self._on_error = on_error
        self._reported = set()
        self.delivered = 0
        self.pumps = 0
        self.inline = 0
        # The thread that constructed this IS the Tk thread (Tk widgets may
        # only be touched from it). Recorded so post() can skip the queue
        # entirely when the caller is already there.
        self._tk_thread = threading.current_thread()

    # ------------------------------------------------------------------ #
    def post(self, fn):
        """Run `fn` on the Tk thread. Safe from any thread. Never raises.

        If the caller is ALREADY on the Tk thread, `fn` runs inline. That is
        not just an optimisation: these hops replaced `widget.after(0, fn)`,
        and a 0 ms timer is serviced by the very next `update()`, while the
        pump here only fires every PUMP_MS. Deferring work that is already on
        the right thread would make it strictly slower than the code it
        replaced — which is exactly what broke a smoke check that calls
        _set_buttons_enabled and then asserts after a single update().
        """
        if threading.current_thread() is self._tk_thread:
            self.delivered += 1
            self.inline += 1
            try:
                fn()
            except Exception as exc:
                self._report("post(inline)", exc)
            return
        try:
            self._queue.put(fn)
        except Exception:
            pass

    def post_result(self, fn, on_done):
        """Queue `fn`, then `on_done(result_or_None)` on the Tk thread.

        The two-step shape every caller here actually wants: do the slow work
        off-thread, hand the result back. A failure in `fn` still calls
        `on_done` (with None) so the UI never waits forever for a reply.
        """

        def _wrapped():
            try:
                value = fn()
            except Exception as exc:
                self._report("post_result", exc)
                value = None
            try:
                on_done(value)
            except Exception as exc:
                self._report("on_done", exc)

        self.post(_wrapped)

    # ------------------------------------------------------------------ #
    def start(self):
        """Begin draining. Idempotent."""
        with self._lock:
            if self._running:
                return
            self._running = True
        self._pump()

    def stop(self):
        with self._lock:
            self._running = False
            aid = self._after_id
            self._after_id = None
        if aid is not None:
            try:
                self._widget.after_cancel(aid)
            except Exception:
                pass

    @property
    def running(self):
        return self._running

    def pending(self):
        return self._queue.qsize()

    # ------------------------------------------------------------------ #
    def _pump(self):
        """Tk thread only."""
        self.pumps += 1
        while True:
            try:
                fn = self._queue.get_nowait()
            except queue.Empty:
                break
            except Exception:
                break
            self.delivered += 1
            try:
                fn()
            except Exception as exc:
                self._report("callback", exc)
        with self._lock:
            running = self._running
        if not running:
            return
        try:
            if self._widget.winfo_exists():
                self._after_id = self._widget.after(self.PUMP_MS, self._pump)
            else:
                with self._lock:
                    self._running = False
        except Exception:
            # a destroyed root (app closing) — stop cleanly rather than
            # rescheduling onto a dead interpreter
            with self._lock:
                self._running = False

    def _report(self, where, exc):
        key = "%s:%s" % (where, type(exc).__name__)
        if key in self._reported:
            return
        self._reported.add(key)
        if self._on_error is None:
            return
        try:
            self._on_error("TkDispatcher %s failed (%s: %s)"
                           % (where, type(exc).__name__, exc))
        except Exception:
            pass
