"""N-002: the ordered plan, walked one step at a time.

`app.reclaim.build_plan` answers "what should I do first". This walks that
answer: it keeps the index, the steps already addressed and the measured bytes
they account for, so a user who hands off to a tool and comes back resumes at
the next step instead of re-reading the list.

The plan's own honesty rules carry over: a step with no measurement is still a
step (it is a decision, not a byte count), and `addressed_bytes` counts only
measured bytes from steps actually marked done. Pure and Tk-free.
"""
from __future__ import annotations


class Guide:
    """Cursor + completion state over a plan's steps."""

    def __init__(self, steps, done=None):
        self.steps = [s for s in (steps or ())
                      if isinstance(s, dict) and str(s.get("id") or "").strip()]
        self.done = {str(s) for s in (done or ())}
        self.index = 0
        while (self.index < len(self.steps)
               and str(self.steps[self.index].get("id")) in self.done):
            self.index += 1
        self.index = min(self.index, max(0, len(self.steps) - 1))

    def current(self):
        if not self.steps:
            return None
        return self.steps[max(0, min(self.index, len(self.steps) - 1))]

    def finished(self) -> bool:
        return bool(self.steps) and all(
            str(s.get("id")) in self.done for s in self.steps)

    def next(self):
        if self.index < len(self.steps) - 1:
            self.index += 1
        return self.current()

    def back(self):
        if self.index > 0:
            self.index -= 1
        return self.current()

    def mark_done(self):
        step = self.current()
        if step is not None:
            self.done.add(str(step.get("id")))
        self.next()

    def progress(self):
        counted = sum(1 for s in self.steps
                      if str(s.get("id")) in self.done)
        return counted, len(self.steps)

    def addressed_bytes(self) -> int:
        """Measured bytes from steps marked done. Never an estimate."""
        total = 0
        for step in self.steps:
            if str(step.get("id")) not in self.done:
                continue
            size = step.get("bytes")
            if isinstance(size, int) and not isinstance(size, bool) and size > 0:
                total += size
        return total


__all__ = ["Guide"]
