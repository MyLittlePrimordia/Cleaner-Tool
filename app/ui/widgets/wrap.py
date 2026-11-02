from __future__ import annotations

import time

_COOLDOWN_S = 1.5
_HISTORY_KEEP = 6


def fit_wrap(widget, container, reserve=0):
    state = {"room": -1, "recent": []}

    def _apply(event=None):
        try:
            width = (int(event.width) if event is not None
                     else int(container.winfo_width()))
        except Exception:
            return
        if width <= 1:
            return
        try:
            room = int(reserve() if callable(reserve) else reserve)
        except Exception:
            room = 0
        room = width - room
        if room <= 80 or room == state["room"]:
            return
        now = time.monotonic()
        state["recent"] = [entry for entry in state["recent"]
                           if now - entry[0] < _COOLDOWN_S]
        if any(room == r for _t, r in state["recent"]):
            return
        state["recent"].append((now, room))
        del state["recent"][:-_HISTORY_KEEP]
        state["room"] = room
        try:
            widget.config(wraplength=room)
        except Exception:
            pass

    try:
        container.bind("<Configure>", _apply, add="+")
        container.after_idle(_apply)
    except Exception:
        pass
    return widget
