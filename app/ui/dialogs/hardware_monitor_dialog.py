"""HardwareMonitorDialog — extracted verbatim from app/gui.py (H12, batch 3).

One dialog per module, mirroring app/ui/widgets. Depends only on
app.ui below it, plus the app.utils helpers it already used; it
knows nothing
about any other dialog except where noted.
"""
from __future__ import annotations

import os

import tkinter as tk
from tkinter import ttk, messagebox
from app.ui.base import ThemedModal, Tooltip
from app.ui.theme import COLORS, F, TAB_ACCENTS
from app.ui.widgets import AnimatedProgressBar


class HardwareMonitorDialog(ThemedModal):
    """Hardware Monitor (Tools tab card): Task-Manager-style live stats —
    CPU/RAM, GPU/VRAM, Disk/Network in a 2-column x 3-row grid —
    read-only, ~1s refresh. Same on-tick-directly pattern MicCheckDialog
    uses for its level meter (the underlying Win32/PDH calls are fast
    synchronous reads, not I/O, so no worker thread is needed).

    Cards match the Health report's card language exactly (hairline
    outer + bg_alt inner, fixed heights, AnimatedProgressBar) — not a
    one-off style — with everything CENTERED (title, big value, detail
    lines) for symmetry. No Close button: the popup X already closes
    it, and the freed row funds taller cards. Device names live ON
    their cards; there is no separate spec row. Multi-GPU / multi-NIC
    rigs get a small ▾ arrow in the card title that pops a dark menu
    (no bulky combobox duplicating the name). GPU temperature is an
    Nvidia-only bonus via nvml.dll and is rendered inline on the GPU
    card ONLY when an Nvidia driver actually answers — on AMD/Intel
    there is no temp UI at all, not even a placeholder line. Disk
    (active-time + throughput + C: free/total) and Network (busiest
    interface by default, link-scaled bar) come from the same OS
    counters Task Manager graphs use."""

    _TICK_MS = 1000
    _GPU_REFRESH_EVERY = 15  # ticks between GPU counter instance re-enum
    _CARD_H = 144            # fixed inner height (frozen layout, Health-style)

    def __init__(self, parent, app):
        self.app = app
        self._tick_after = None
        self._tick_count = 0
        self._cpu_sampler = None
        self._gpu_sampler = None
        self._nvml = None
        self._disk_sampler = None
        self._net_sampler = None
        self._gpu_list = []
        self._gpu_sel = 0
        self._cpu_info = {"name": "Unknown CPU", "logical_cores": 0}
        super().__init__(parent, title="Hardware Monitor",
                         accent=TAB_ACCENTS.get("Tweak", COLORS["accent_blue"]))
        body = self.body

        try:
            from app.hw_monitor import cpu_static_info, gpu_list
            self._cpu_info = cpu_static_info()
            self._gpu_list = gpu_list() or []
        except Exception:
            pass
        if not self._gpu_list:
            self._gpu_list = [{"name": "Unknown GPU",
                               "total_vram_bytes": None,
                               "total_vram_approx": False}]

        # 2x3 uniform grid (Health-card geometry: uniform columns AND
        # rows with a minsize floor so live paints never move layout).
        # Two wide columns (~370px each) so device names fit on one
        # line; GPU sits above VRAM so the pair reads top-to-bottom.
        grid = self._grid = tk.Frame(body, bg=COLORS["bg"])
        grid.pack(fill="x", pady=(2, 0))
        for c in (0, 1):
            grid.columnconfigure(c, weight=1, uniform="hwcol")
        for r in (0, 1, 2):
            grid.rowconfigure(r, weight=1, uniform="hwrow",
                              minsize=self._CARD_H + 10)

        # Single-GPU rigs get no GPU arrow (a one-entry menu is
        # chrome); the network arrow always exists (Auto + NICs).
        _gpu_picker = "gpu" if len(self._gpu_list) > 1 else None
        self._cpu = self._make_card(grid, 0, 0, "CPU")
        self._ram = self._make_card(grid, 0, 1, "RAM")
        self._gpu = self._make_card(grid, 1, 0, "GPU", picker=_gpu_picker)
        self._vram = self._make_card(grid, 1, 1, "VRAM")
        self._disk = self._make_card(grid, 2, 0, "Disk")
        self._net = self._make_card(grid, 2, 1, "Network", picker="net")

        # Picker state: GPU index into gpu_list; network None = auto
        # (busiest interface each tick) or a pinned interface name.
        self._net_sel = None
        self._open_menu = None

        # Honesty note only (empty = zero-height): GPU counters missing
        # entirely. No Close button — the popup X already closes this.
        self._note_lbl = tk.Label(
            body, text="", font=(F, 8), bg=COLORS["bg"], fg=COLORS["subtext"],
            anchor="center", justify="center")
        self._note_lbl.pack(fill="x")

        try:
            from app.hw_monitor import (CpuSampler, DiskSampler, GpuSampler,
                                        NetworkSampler, NvmlTemp)
            self._cpu_sampler = CpuSampler()
            self._gpu_sampler = GpuSampler()
            self._disk_sampler = DiskSampler()
            self._net_sampler = NetworkSampler()
            self._nvml = NvmlTemp()
        except Exception:
            pass

        notes = []
        try:
            gpu_ok = (self._gpu_sampler is not None
                      and getattr(self._gpu_sampler, "_ok", False))
        except Exception:
            gpu_ok = False
        if not gpu_ok:
            notes.append(
                "GPU counters unavailable on this PC — GPU/VRAM will show \"N/A\".")
        if notes:
            self._note_lbl.config(text=" ".join(notes))

        self.on_close(self._stop_tick)
        self._tick()

    def _make_card(self, parent, row, col, title, picker=None):
        """One stat card in the Health report's card language: hairline
        outer + bg_alt inner at a FIXED height (pack_propagate off, so
        live paints can never resize the grid) — everything CENTERED
        (title, big value, detail lines) for symmetry. Title on its own
        line and the value below it, so long values (e.g. "0.01 Mbps")
        can never collide with the title. An optional picker ("gpu" /
        "net") adds a small ▾ arrow beside the title that pops a dark
        menu; the card face itself only ever shows the SELECTED name.
        Returns a dict of handles ("pick" = arrow label or None)."""
        outer = tk.Frame(parent, bg=COLORS["hairline"], bd=0)
        outer.grid(row=row, column=col, sticky="nsew", padx=5, pady=5)
        inner = tk.Frame(outer, bg=COLORS["bg_alt"], height=self._CARD_H)
        inner.pack(fill="x", padx=1, pady=1)
        try:
            inner.pack_propagate(False)
        except Exception:
            pass
        trow = tk.Frame(inner, bg=COLORS["bg_alt"])
        trow.pack(anchor="center", pady=(10, 0))
        title_lbl = tk.Label(trow, text=title, font=(F, 9, "bold"),
                             bg=COLORS["bg_alt"], fg=COLORS["text"])
        title_lbl.pack(side="left")
        pick_lbl = None
        if picker in ("gpu", "net"):
            # (F, 11): the 8pt glyph was easy to miss next to the 9pt
            # bold title — one step up so the picker reads as clickable.
            pick_lbl = tk.Label(trow, text="▾", font=(F, 11),
                                bg=COLORS["bg_alt"], fg=COLORS["subtext"],
                                cursor="hand2")
            pick_lbl.pack(side="left", padx=(6, 0))
            try:
                # Tooltip FIRST: it binds <Enter>/<Leave> without add="+"
                # (replacing), so the hover brightening below must come
                # after with add="+" — same trap as AnimatedButton.
                if picker == "gpu":
                    Tooltip(pick_lbl, "Choose which GPU these numbers describe.")
                    pick_lbl.bind("<Button-1>", self._open_gpu_menu, add="+")
                else:
                    Tooltip(pick_lbl, "Choose which network interface to watch.")
                    pick_lbl.bind("<Button-1>", self._open_net_menu, add="+")
                pick_lbl.bind("<Enter>", lambda _e, w=pick_lbl:
                              w.config(fg=COLORS["text"]), add="+")
                pick_lbl.bind("<Leave>", lambda _e, w=pick_lbl:
                              w.config(fg=COLORS["subtext"]), add="+")
            except Exception:
                pass
        val_lbl = tk.Label(inner, text="—", font=(F, 16, "bold"),
                           bg=COLORS["bg_alt"], fg=COLORS["text"],
                           anchor="center", justify="center")
        val_lbl.pack(anchor="center", pady=(2, 0))
        bar = AnimatedProgressBar(inner, accent=COLORS["accent_green"])
        bar.pack(fill="x", padx=14, pady=(8, 6))
        name_lbl = tk.Label(inner, text="", font=(F, 8),
                            bg=COLORS["bg_alt"], fg=COLORS["subtext"],
                            anchor="center", justify="center")
        name_lbl.pack(fill="x", padx=12)
        sub_lbl = tk.Label(inner, text="", font=(F, 8),
                           bg=COLORS["bg_alt"], fg=COLORS["subtext"],
                           anchor="center", justify="center")
        sub_lbl.pack(fill="x", padx=12)
        return {"outer": outer, "inner": inner, "title": title_lbl,
                "val": val_lbl, "bar": bar, "name": name_lbl,
                "sub": sub_lbl, "pick": pick_lbl}

    def _paint_card(self, card, percent, value_text, name_text,
                    sub_text, name_tip=None):
        """Paint one card. percent None = gap (muted bar, no fake fill);
        0..100 colors green→amber→red like every other meter here."""
        try:
            card["val"].config(text=value_text)
            bar = card["bar"]
            if percent is None:
                bar._accent = COLORS["subtext"]
                bar.set_fraction(0.0)
            else:
                bar._accent = (COLORS["accent_green"] if percent < 70 else
                               COLORS["accent_yellow"] if percent < 90 else
                               COLORS["accent_red"])
                bar.set_fraction(max(0.0, min(1.0, percent / 100.0)))
            card["name"].config(text=name_text or "")
            card["sub"].config(text=sub_text or "")
            # Tooltip once per distinct text (this paints every second —
            # re-binding each tick would churn hover hooks forever).
            if name_tip and card.get("_tip_for") != name_tip:
                try:
                    Tooltip(card["name"], name_tip)
                    card["_tip_for"] = name_tip
                except Exception:
                    pass
        except Exception:
            pass

    # ---- pickers (▾ arrow menus, no combobox chrome) --------------------- #

    @staticmethod
    def _short(text, limit=44):
        try:
            t = str(text or "")
            return t if len(t) <= limit else t[:limit - 1] + "…"
        except Exception:
            return ""

    def _gpu_label(self, i):
        try:
            entry = self._gpu_list[i]
            name = entry.get("name") or "Unknown GPU"
            if entry.get("total_vram_bytes"):
                gb = entry["total_vram_bytes"] / 1024 ** 3
                return "%s — %.0f GB" % (name, gb)
            return str(name)
        except Exception:
            return "GPU %d" % (i + 1)

    def _gpu_menu_labels(self):
        """Menu rows for the GPU arrow (also the smoke hook)."""
        try:
            return [self._gpu_label(i) for i in range(len(self._gpu_list))]
        except Exception:
            return []

    def _set_gpu(self, i):
        """Pin the GPU selection (clamped — never raises). Live numbers
        follow on the next tick."""
        try:
            self._gpu_sel = max(
                0, min(int(i), len(self._gpu_list) - 1))
        except Exception:
            self._gpu_sel = 0

    def _net_iface_names(self):
        """Usable NIC names for the network arrow (also the smoke hook)."""
        try:
            if self._net_sampler is not None:
                return list(self._net_sampler.interfaces() or [])
        except Exception:
            pass
        return []

    def _net_menu_labels(self):
        """Menu rows: Auto first, then every NIC."""
        try:
            return ["Auto (busiest)"] + self._net_iface_names()
        except Exception:
            return ["Auto (busiest)"]

    def _set_net(self, name_or_none):
        """Pin a NIC (None/"" = auto-busiest). Unknown names fall back
        to auto — never raises, never sticks on a dead interface."""
        try:
            if not name_or_none:
                self._net_sel = None
            elif name_or_none in self._net_iface_names():
                self._net_sel = name_or_none
            else:
                self._net_sel = None
            if self._net_sampler is not None:
                self._net_sampler.select(self._net_sel)
        except Exception:
            pass

    def _dark_menu(self):
        """One dark popup menu in dialog colors (never raises)."""
        try:
            return tk.Menu(self._dlg, tearoff=0, bg=COLORS["surface"],
                           fg=COLORS["text"],
                           activebackground=COLORS["surface_hover"],
                           activeforeground=COLORS["text"],
                           selectcolor=COLORS["text"], font=(F, 9))
        except Exception:
            return None

    def _post_menu(self, menu, widget):
        """Pop a menu under a ▾ arrow (best-effort, never raises)."""
        if menu is None or widget is None:
            return
        try:
            self._open_menu = menu  # anti-GC while posted
            x = widget.winfo_rootx()
            y = widget.winfo_rooty() + widget.winfo_height() + 2
            menu.tk_popup(x, y)
        except Exception:
            pass

    def _open_gpu_menu(self, _event=None):
        """GPU ▾ arrow: radio menu with a check on the live selection."""
        if len(self._gpu_list) < 2:
            return
        try:
            menu = self._dark_menu()
            if menu is None:
                return
            var = tk.IntVar(value=int(self._gpu_sel or 0))
            for i, label in enumerate(self._gpu_menu_labels()):
                menu.add_radiobutton(label=label, variable=var, value=i,
                                     command=lambda i=i: self._set_gpu(i))
            self._post_menu(menu, self._gpu.get("pick"))
        except Exception:
            pass

    def _open_net_menu(self, _event=None):
        """Network ▾ arrow: Auto (busiest) + every NIC, check on live."""
        try:
            menu = self._dark_menu()
            if menu is None:
                return
            cur = self._net_sel or ""
            var = tk.StringVar(value=cur)
            menu.add_radiobutton(label="Auto (busiest)", variable=var,
                                 value="", command=lambda: self._set_net(None))
            for name in self._net_iface_names():
                menu.add_radiobutton(label=name, variable=var, value=name,
                                     command=lambda n=name: self._set_net(n))
            self._post_menu(menu, self._net.get("pick"))
        except Exception:
            pass

    def _selected_gpu(self):
        """(registry entry, phys_index|None): positional pairing of the
        picked registry GPU to the PDH phys bucket (best-effort — the
        two enumerations have no shared key; counts that disagree fall
        back to totals rather than a wrong mapping)."""
        try:
            entry = self._gpu_list[self._gpu_sel]
        except Exception:
            entry = {"name": "Unknown GPU", "total_vram_bytes": None,
                     "total_vram_approx": False}
        phys = None
        try:
            idx = sorted(getattr(self._gpu_sampler, "phys_indices", []) or [])
            if self._gpu_sel < len(idx):
                phys = idx[self._gpu_sel]
            elif len(idx) == 1 and len(self._gpu_list) == 1:
                phys = idx[0]
        except Exception:
            phys = None
        return entry, phys

    def _selected_temp(self):
        """Nvidia temp for the picked GPU, or None (AMD/Intel/unreadable
        all read None — the caller renders NOTHING in that case, not
        even a placeholder)."""
        try:
            if self._nvml is None or not getattr(
                    self._nvml, "available", False):
                return None
            t = self._nvml.sample_at(self._gpu_sel)
            if t is None and int(getattr(self._nvml, "device_count", 0)) == 1:
                t = self._nvml.sample_at(0)
            return t
        except Exception:
            return None

    def _tick(self):
        self._tick_after = None
        self._tick_count += 1
        try:
            from app.hw_monitor import sample_ram
        except Exception:
            sample_ram = None

        # ---- RAM ---- #
        ram = sample_ram() if sample_ram else None
        if ram:
            used_gb = ram["used_bytes"] / 1024 ** 3
            total_gb = ram["total_bytes"] / 1024 ** 3
            self._paint_card(
                self._ram, ram["percent"], f"{ram['percent']:.0f}%",
                f"{used_gb:.1f} / {total_gb:.1f} GB", "physical memory")
        else:
            self._paint_card(self._ram, None, "N/A", "", "")

        # ---- CPU (name lives on the card) ---- #
        cpu_pct = self._cpu_sampler.sample() if self._cpu_sampler else None
        try:
            cores = int(self._cpu_info.get("logical_cores") or 0)
        except Exception:
            cores = 0
        cpu_name = self._cpu_info.get("name") or "Unknown CPU"
        threads_txt = (f"{cores} threads" if cores else "")
        if cpu_pct is not None:
            self._paint_card(self._cpu, cpu_pct, f"{cpu_pct:.0f}%",
                             self._short(cpu_name), threads_txt, cpu_name)
        else:
            self._paint_card(self._cpu, None, "…",
                             self._short(cpu_name),
                             "warming up" if threads_txt else "", cpu_name)

        # ---- GPU (+ picker, + inline Nvidia temp) ---- #
        # NOTE: exactly ONE CollectQueryData per tick (inside
        # sample_per_adapter). A second collect in the same tick would
        # read ~0 out of the rate counters (they need wall-clock
        # between collections), so totals are summed from the same
        # per-adapter pass, never re-collected.
        per = {}
        totals = {"gpu_percent": None, "vram_used_bytes": None}
        if self._gpu_sampler is not None:
            if self._tick_count % self._GPU_REFRESH_EVERY == 0:
                try:
                    self._gpu_sampler.refresh_instances()
                except Exception:
                    pass
            try:
                per = self._gpu_sampler.sample_per_adapter() or {}
            except Exception:
                per = {}
            try:
                pcts = [v.get("gpu_percent") for v in per.values()]
                pcts = [p for p in pcts if p is not None]
                if pcts:
                    totals["gpu_percent"] = max(0.0, min(100.0, sum(pcts)))
                vrams = [v.get("vram_used_bytes") for v in per.values()]
                vrams = [b for b in vrams if b is not None]
                if vrams:
                    totals["vram_used_bytes"] = int(sum(vrams))
            except Exception:
                pass
        entry, phys = self._selected_gpu()
        gpu_name = entry.get("name") or "Unknown GPU"
        # Name always on the card (the ▾ menu is transient — nothing is
        # duplicated); detail carries VRAM size + Nvidia temp when known.
        gpu_name_line = self._short(gpu_name)
        try:
            _tb = entry.get("total_vram_bytes")
            vram_size_txt = ("%.0f GB" % (_tb / 1024 ** 3) if _tb else "")
        except Exception:
            vram_size_txt = ""
        temp = self._selected_temp()
        if temp is not None:
            gpu_detail = ((vram_size_txt + " · ") if vram_size_txt
                          else "") + f"{temp}°C"
        else:
            gpu_detail = vram_size_txt
        gpu_ok = bool(getattr(self._gpu_sampler, "_ok", False))
        if phys is not None and phys in per:
            gp = (per[phys] or {}).get("gpu_percent")
            if gp is not None:
                self._paint_card(
                    self._gpu, gp, f"{gp:.0f}%", gpu_name_line,
                    gpu_detail, gpu_name)
            else:
                detail = (gpu_detail if not gpu_ok or gpu_detail
                          else "warming up")
                if gpu_ok and not gpu_detail:
                    detail = "warming up"
                self._paint_card(
                    self._gpu, None, "…" if gpu_ok else "N/A",
                    gpu_name_line, detail, gpu_name)
        elif len(self._gpu_list) > 1:
            # picked GPU has no engine counters this pass (idle card) —
            # Task Manager reads 0% here, not a gap.
            _core = ((vram_size_txt + " · ") if vram_size_txt else "")
            if temp is not None:
                idle_detail = _core + f"{temp}°C · idle"
            else:
                idle_detail = _core + "idle"
            self._paint_card(self._gpu, 0.0, "0%", gpu_name_line,
                             idle_detail, gpu_name)
        else:
            gp = totals.get("gpu_percent")
            if gp is not None:
                self._paint_card(self._gpu, gp, f"{gp:.0f}%",
                                 gpu_name_line, gpu_detail, gpu_name)
            else:
                detail = (gpu_detail if not gpu_ok or gpu_detail
                          else "warming up")
                if gpu_ok and not gpu_detail:
                    detail = "warming up"
                self._paint_card(
                    self._gpu, None, "…" if gpu_ok else "N/A",
                    gpu_name_line, detail, gpu_name)

        # ---- VRAM (follows the GPU picker) ---- #
        total_b = entry.get("total_vram_bytes")
        vram_used = None
        vram_idle = False
        if phys is not None and phys in per:
            vram_used = (per[phys] or {}).get("vram_used_bytes")
        elif not (len(self._gpu_list) > 1):
            vram_used = totals.get("vram_used_bytes")
        elif self._tick_count > 1:
            # picked GPU exposes no adapter counters (idle card on a
            # multi-GPU rig) — "idle", not a gap and not a fake number.
            vram_idle = True
        if vram_used is not None and total_b:
            used_gb = vram_used / 1024 ** 3
            total_gb = total_b / 1024 ** 3
            pct = max(0.0, min(100.0, vram_used / total_b * 100.0))
            self._paint_card(self._vram, pct, f"{used_gb:.1f} GB",
                             f"of {total_gb:.1f} GB",
                             self._short(gpu_name), gpu_name)
        elif vram_used is not None:
            self._paint_card(self._vram, None,
                             f"{vram_used / 1024 ** 3:.1f} GB", "used",
                             self._short(gpu_name), gpu_name)
        elif vram_idle:
            self._paint_card(self._vram, None, "—",
                             self._short(gpu_name), "idle", gpu_name)
        else:
            self._paint_card(self._vram, None,
                             "N/A" if not gpu_ok else "…",
                             self._short(gpu_name), "", gpu_name)

        # ---- Disk ---- #
        ds = None
        try:
            ds = self._disk_sampler.sample() if self._disk_sampler else None
        except Exception:
            ds = None
        disk_free_txt, disk_sub2 = "", ""
        try:
            from app.hw_monitor import drive_free_total
            pair = drive_free_total(
                (os.environ.get("SystemDrive", "C:") or "C:") + "\\")
            if pair:
                free_gb, total_gb = pair[0] / 1024 ** 3, pair[1] / 1024 ** 3
                disk_free_txt = f"C: {free_gb:.0f} / {total_gb:.0f} GB free"
        except Exception:
            pass
        if ds:
            try:
                r = (ds.get("read_bps") or 0.0) / 1024 ** 2
                w = (ds.get("write_bps") or 0.0) / 1024 ** 2
                disk_sub2 = f"R {r:.1f} · W {w:.1f} MB/s"
            except Exception:
                pass
            ap = ds.get("active_percent")
            if ap is not None:
                self._paint_card(self._disk, ap, f"{ap:.0f}%",
                                 disk_free_txt, disk_sub2)
            else:
                disk_ok = bool(getattr(self._disk_sampler, "_ok", False))
                self._paint_card(self._disk, None,
                                 "…" if disk_ok else "N/A",
                                 disk_free_txt,
                                 disk_sub2 or ("warming up" if disk_ok else ""))
        else:
            self._paint_card(self._disk, None, "N/A", disk_free_txt, "")

        # ---- Network ---- #
        ns = None
        try:
            ns = self._net_sampler.sample() if self._net_sampler else None
        except Exception:
            ns = None
        if ns and ns.get("total_mbps") is not None:
            try:
                total = float(ns["total_mbps"])
            except Exception:
                total = 0.0
            link = ns.get("link_mbps")
            if link and link > 0:
                pct = max(0.0, min(100.0, total / link * 100.0))
                scale_txt = f"of {link:.0f} Mbps link"
            else:
                try:
                    peak = float(self._net_sampler.max_seen_mbps or 0.0)
                except Exception:
                    peak = 0.0
                denom = max(peak, total, 1.0)
                pct = max(0.0, min(100.0, total / denom * 100.0))
                scale_txt = "auto-scaled"
            try:
                up = float(ns.get("up_bps") or 0.0) * 8.0 / 1_000_000.0
                down = float(ns.get("down_bps") or 0.0) * 8.0 / 1_000_000.0
                io_txt = f"↓ {down:.1f} · ↑ {up:.1f} Mbps"
            except Exception:
                io_txt = scale_txt
                scale_txt = ""
            iface = self._short(ns.get("iface") or "")
            self._paint_card(
                self._net, pct,
                f"{total:.1f} Mbps" if total >= 10 else f"{total:.2f} Mbps",
                iface, io_txt,
                ns.get("iface") or None)
        else:
            net_ok = bool(getattr(self._net_sampler, "_ok", False))
            self._paint_card(self._net, None, "…" if net_ok else "N/A",
                             "", "warming up" if net_ok else "")

        try:
            self._tick_after = self._dlg.after(self._TICK_MS, self._tick)
        except Exception:
            self._tick_after = None

    def _stop_tick(self):
        try:
            if self._tick_after is not None:
                self._dlg.after_cancel(self._tick_after)
        except Exception:
            pass
        self._tick_after = None
        for sampler in (self._gpu_sampler, self._disk_sampler,
                        self._net_sampler):
            try:
                if sampler is not None:
                    sampler.close()
            except Exception:
                pass
        try:
            if self._nvml is not None:
                self._nvml.close()
        except Exception:
            pass
