"""MetricStrip: one row of measured values, the same way in every tool.

AUDIT TV-004 (2026-10). "92% full" in Storage, "433 GB used" in Specs and
"Disk 91%" in Health were three unrelated visual treatments of the same kind
of fact; Speed Test drew four RoundedCards inside the rounded dialog, which
is a card in a card. The audit asked for one stat component: label, value,
optional unit and optional delta, with M3's stat hierarchy (quiet label, loud
number) and no surface of its own.

The strip owns equal columns so four measurements line up across tools even
when their labels differ in length. It paints on the page background: the
number is the thing the user reads, not another panel.

    strip = MetricStrip(body, [("ping", "Ping", "ms"),
                               ("down", "Download", "Mbps")])
    strip.pack(fill="x")
    strip.set("down", "94.2")            # updates just that cell

Speed Test still sets complete strings (it formats "23 ms" as one honest
readout, including an em dash when a leg failed), so `set` takes the value
the tool computed and leaves the unit slot empty.

Depends only on app.ui.theme.
"""
from __future__ import annotations

import tkinter as tk

from app.ui.metrics import TEXT_NUDGE
from app.ui.theme import COLORS, SPACE_2, SPACE_3, TYPE

#: Value rungs. `lg` is for a dashboard's headline row; `md` is the size a
#: detail page uses beside prose.
SIZES = {"lg": TYPE["metric_small"], "md": TYPE["readout_large"]}

_TONE = {
    "neutral": COLORS["subtext"],
    "good": COLORS["accent_green"],
    "warn": COLORS["accent_yellow"],
    "bad": COLORS["accent_red"],
}


class _MetricCell(tk.Frame):
    """Label over value (+ unit), with an optional change line."""

    def __init__(self, parent, label, unit="", *, bg, align="left", size="md"):
        super().__init__(parent, bg=bg, bd=0)
        self._bg = bg
        self._anchor = "w" if align == "left" else "center"
        self._align = align
        self.label_lbl = tk.Label(
            self, text=str(label or ""), font=TYPE["caption"], bg=bg,
            fg=COLORS["subtext"], anchor=self._anchor, justify="left", bd=0)
        self.label_lbl.pack(fill="x")

        self._row = tk.Frame(self, bg=bg, bd=0)
        if align == "left":
            self._row.pack(fill="x", pady=(TEXT_NUDGE, 0))
        else:
            self._row.pack(anchor="center", pady=(TEXT_NUDGE, 0))
        self.value_lbl = tk.Label(
            self._row, text="—", font=SIZES.get(size, SIZES["md"]), bg=bg,
            fg=COLORS["text"], anchor=self._anchor, justify="left", bd=0)
        self.value_lbl.pack(side="left")
        self.unit_lbl = tk.Label(
            self._row, text=str(unit or ""), font=TYPE["caption"], bg=bg,
            fg=COLORS["subtext"], anchor="w", bd=0)
        if unit:
            self.unit_lbl.pack(side="left", padx=(SPACE_2, 0))
        else:
            self.unit_lbl.pack_forget()
        self.delta_lbl = tk.Label(
            self, text="", font=TYPE["micro_regular"], bg=bg,
            fg=COLORS["subtext"], anchor=self._anchor, bd=0)

    def set(self, value, unit=None, tone=None, delta=None, delta_tone="neutral"):
        try:
            self.value_lbl.config(text=str(value), fg=_TONE.get(tone,
                                                               COLORS["text"]))
        except Exception:
            pass
        if unit is not None:
            try:
                self.unit_lbl.config(text=str(unit))
                if unit:
                    if not self.unit_lbl.winfo_manager():
                        self.unit_lbl.pack(side="left", padx=(SPACE_2, 0))
                else:
                    self.unit_lbl.pack_forget()
            except Exception:
                pass
        if delta is not None:
            try:
                text = str(delta)
                self.delta_lbl.config(text=text,
                                      fg=_TONE.get(delta_tone, COLORS["subtext"]))
                if text:
                    if not self.delta_lbl.winfo_manager():
                        self.delta_lbl.pack(fill="x")
                else:
                    self.delta_lbl.pack_forget()
            except Exception:
                pass


class MetricStrip(tk.Frame):
    """Equal-width metric cells on the page surface."""

    def __init__(self, parent, metrics, bg=None, align="left", size="md",
                 accent=None, **kw):
        page_bg = bg or COLORS["bg"]
        super().__init__(parent, bg=page_bg, bd=0, **kw)
        self._cells = {}
        for index, spec in enumerate(metrics):
            key, label = spec[0], spec[1]
            unit = spec[2] if len(spec) > 2 else ""
            cell = _MetricCell(self, label, unit, bg=page_bg, align=align,
                               size=size)
            pad = (0, SPACE_3) if index < len(metrics) - 1 else (0, 0)
            cell.grid(row=0, column=index, sticky="nsew", padx=pad)
            self.grid_columnconfigure(index, weight=1, uniform="metric")
            self._cells[key] = cell
        self._accent = accent

    # ---- api ----------------------------------------------------------- #
    def set(self, key, value, unit=None, tone=None, delta=None,
            delta_tone="neutral"):
        cell = self._cells.get(key)
        if cell is not None:
            cell.set(value, unit=unit, tone=tone, delta=delta,
                     delta_tone=delta_tone)
        return cell

    def value_label(self, key):
        """The raw value Label, for callers that repaint it directly."""
        cell = self._cells.get(key)
        return cell.value_lbl if cell is not None else None


    def keys(self):
        return tuple(self._cells)
