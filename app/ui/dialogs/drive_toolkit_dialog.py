"""DriveToolkitDialog — extracted verbatim from app/gui.py (H12, batch 4).

One dialog per module, mirroring app/ui/widgets. Depends only on
app.ui below it, plus the app.utils helpers it already used; it
knows nothing
about any other dialog except where noted.
"""
from __future__ import annotations

import tkinter as tk
from tkinter import ttk, messagebox
from app.ui.base import ThemedModal, _themed_askyesno
from app.ui.theme import COLORS, F, TAB_ACCENTS
from app.ui.widgets import AnimatedButton, AnimatedProgressBar, ScrollableRoundedPanel, SegmentedTabs


class DriveToolkitDialog(ThemedModal):
    """Drive Toolkit (Tools tab card): 3-in-1 — Health, Speed, Capacity.
    Same internal tab-switch pattern as MouseTesterDialog (Buttons/Aim):
    two or three AnimatedButton tabs swap self._content wholesale.

    Every check here is read-only or self-cleaning:
      * Health only reads Get-PhysicalDisk — never writes anything
      * Speed writes one temp file (app.storage_scan.write_benchmark
        already guarantees its own cleanup) then deletes it
      * Capacity writes temporary test files ONLY inside free space on
        a REMOVABLE drive the user explicitly picked, and always
        removes them — see app/drive_toolkit.py for the safety gates

    All three run on a background thread with a stop token so closing
    the dialog mid-check can never leave a thread writing to a drive
    the dialog no longer has a handle open on.
    """

    _VIEWS = (("health", "Drive Health"), ("speed", "Speed Test"),
             ("capacity", "USB/SD Check"))

    def __init__(self, parent, app):
        self.app = app
        self._stop_token = [False]
        self._view = "health"
        super().__init__(parent, title="Drive Toolkit",
                         accent=TAB_ACCENTS["Clean"])
        body = self.body
        # One centered pill (same language as the main Clean/Repair/Tweak
        # switcher) — replaces the old three separate left-packed buttons.
        tabs = tk.Frame(body, bg=COLORS["bg"])
        tabs.pack(fill="x", pady=(0, 10))
        self._tab_btns = {}  # kept for harness compat (no longer styled)
        self._seg_tabs = SegmentedTabs(
            tabs, labels=[label for _, label in self._VIEWS],
            keys=[key for key, _ in self._VIEWS],
            initial="health", accent=TAB_ACCENTS["Clean"],
            command=self._switch)
        self._seg_tabs.pack(anchor="center")
        self._content = tk.Frame(body, bg=COLORS["bg"])
        self._content.pack(fill="both", expand=True)
        self.on_close(self._stop)
        self._switch("health")

    # ---- tab switching ------------------------------------------------- #

    def _switch(self, key):
        self._stop_token[0] = True    # abandon whatever the old view was doing
        self._stop_token = [False]
        self._view = key
        for child in self._content.winfo_children():
            try:
                child.destroy()
            except Exception:
                pass
        # Sync the pill without re-firing command (click already fired it;
        # programmatic _switch("health") on open must still move the thumb).
        try:
            if getattr(self, "_seg_tabs", None) is not None and \
                    self._seg_tabs.selected_key != key:
                self._seg_tabs.select(key, _fire=False)
        except Exception:
            pass
        if key == "health":
            self._build_health()
        elif key == "speed":
            self._build_speed()
        else:
            self._build_capacity()

    def _stop(self):
        self._stop_token[0] = True

    # ==================================================================== #
    # Health
    # ==================================================================== #

    def _build_health(self):
        c = self._content
        # UI fix: shortened to one plain-language row (was wrapping to
        # two lines) — same simpler-is-better pass as Speed/Capacity below.
        tk.Label(c, text="Checks each drive's health — the same signal "
                         "Windows uses to warn before it fails.",
                 font=(F, 9), bg=COLORS["bg"], fg=COLORS["subtext"],
                 wraplength=700, justify="left").pack(anchor="w", pady=(0, 8))
        self._health_panel = ScrollableRoundedPanel(c)
        self._health_panel.config(height=280)
        self._health_panel.pack(fill="both", expand=True)
        brow = tk.Frame(c, bg=COLORS["bg"])
        brow.pack(fill="x", pady=(8, 0))
        self._health_status = tk.Label(brow, text="Checking…", font=(F, 9),
                                       bg=COLORS["bg"], fg=COLORS["subtext"])
        self._health_status.pack(side="left")
        AnimatedButton(brow, text="Refresh", command=self._run_health,
                       bg=COLORS["surface"], fg=COLORS["text"],
                       font=(F, 9, "bold"), padx=16,
                       pady=6).pack(side="right")
        self._run_health()

    def _run_health(self):
        token = self._stop_token
        self._health_status.config(text="Checking…")
        for w in list(self._health_panel.inner.winfo_children()):
            try:
                w.destroy()
            except Exception:
                pass

        def worker():
            try:
                from app.drive_toolkit import list_physical_disks
                disks = list_physical_disks()
            except Exception:
                disks = []
            if not token[0]:
                self._dlg.after(0, lambda: self._health_done(disks, token))

        import threading
        threading.Thread(target=worker, daemon=True).start()

    def _health_done(self, disks, token):
        if token[0] or self._view != "health":
            return
        try:
            if not self._health_panel.winfo_exists():
                return
        except Exception:
            return
        if not disks:
            tk.Label(self._health_panel.inner,
                     text="Couldn't read drive health on this PC.",
                     font=(F, 9), bg=COLORS["bg_alt"],
                     fg=COLORS["subtext"]).pack(pady=16)
            self._health_status.config(text="")
            return
        chip = {"Healthy": ("✅", COLORS["accent_green"]),
               "Warning": ("⚠️", COLORS["accent_yellow"]),
               "Unhealthy": ("❌", COLORS["accent_red"])}
        worst = "Healthy"
        for d in disks:
            row = tk.Frame(self._health_panel.inner, bg=COLORS["bg_alt"])
            glyph, color = chip.get(d["health"], ("•", COLORS["subtext"]))
            if d["health"] == "Unhealthy":
                worst = "Unhealthy"
            elif d["health"] == "Warning" and worst != "Unhealthy":
                worst = "Warning"
            tk.Label(row, text=glyph, font=("Segoe UI Emoji", 12),
                     bg=COLORS["bg_alt"]).pack(side="left", padx=(10, 8), pady=8)
            info = tk.Frame(row, bg=COLORS["bg_alt"])
            info.pack(side="left", fill="x", expand=True)
            size_gb = d["size_bytes"] / (1024 ** 3) if d["size_bytes"] else 0
            title = d["name"]
            if size_gb:
                title += f"  ·  {size_gb:.0f} GB  ·  {d['media_type']}"
            else:
                title += f"  ·  {d['media_type']}"
            tk.Label(info, text=title, font=(F, 9, "bold"), bg=COLORS["bg_alt"],
                     fg=COLORS["text"], anchor="w").pack(fill="x")
            words = {"Healthy": "Working normally",
                    "Warning": "Showing early warning signs — back up your files soon",
                    "Unhealthy": "Failing — back up your files now"}
            tk.Label(info, text=words.get(d["health"], d["health"]),
                     font=(F, 8), bg=COLORS["bg_alt"], fg=color,
                     anchor="w").pack(fill="x")
            row.pack(fill="x", pady=2)
        self._health_status.config(
            text=("All drives look healthy." if worst == "Healthy"
                 else "One or more drives need attention — see above."))

    # ==================================================================== #
    # Speed
    # ==================================================================== #

    def _build_speed(self):
        c = self._content
        # UI fix: shortened further to a plain one-row line, matching
        # Health/Capacity's condensed style.
        tk.Label(c, text="Tests real-world read/write speed to spot a slowdown.",
                 font=(F, 9), bg=COLORS["bg"], fg=COLORS["subtext"],
                 wraplength=700, justify="left").pack(anchor="w", pady=(0, 10))

        row = tk.Frame(c, bg=COLORS["bg"])
        row.pack(fill="x")
        tk.Label(row, text="Drive:", font=(F, 9), bg=COLORS["bg"],
                 fg=COLORS["subtext"]).pack(side="left")
        try:
            from app.drive_toolkit import list_fixed_drives
            drives = list_fixed_drives()
        except Exception:
            drives = []
        names = [f"{letter}:" for letter, *_ in drives] or ["C:"]
        self._speed_var = tk.StringVar(value=names[0])
        import tkinter.ttk as _ttk
        combo = _ttk.Combobox(row, textvariable=self._speed_var, values=names,
                              state="readonly", width=8, font=(F, 9))
        combo.pack(side="left", padx=(6, 12))
        self._speed_btn = AnimatedButton(
            row, text="Test This Drive", command=self._run_speed,
            bg=TAB_ACCENTS["Clean"], fg=COLORS["black"],
            font=(F, 9, "bold"), padx=18, pady=7)
        self._speed_btn.pack(side="left")
        if not drives:
            self._speed_btn.set_enabled(False)

        results = tk.Frame(c, bg=COLORS["bg"])
        results.pack(fill="x", pady=(20, 0))
        results.grid_columnconfigure(0, weight=1, uniform="s")
        results.grid_columnconfigure(1, weight=1, uniform="s")
        w_cell = tk.Frame(results, bg=COLORS["bg_alt"])
        w_cell.grid(row=0, column=0, sticky="ew", padx=(0, 6))
        tk.Label(w_cell, text="WRITE", font=(F, 8, "bold"), bg=COLORS["bg_alt"],
                 fg=COLORS["subtext"]).pack(pady=(12, 0))
        self._write_lbl = tk.Label(w_cell, text="—", font=(F, 22, "bold"),
                                   bg=COLORS["bg_alt"], fg=COLORS["text"])
        self._write_lbl.pack(pady=(0, 12))
        r_cell = tk.Frame(results, bg=COLORS["bg_alt"])
        r_cell.grid(row=0, column=1, sticky="ew", padx=(6, 0))
        tk.Label(r_cell, text="READ", font=(F, 8, "bold"), bg=COLORS["bg_alt"],
                 fg=COLORS["subtext"]).pack(pady=(12, 0))
        self._read_lbl = tk.Label(r_cell, text="—", font=(F, 22, "bold"),
                                  bg=COLORS["bg_alt"], fg=COLORS["text"])
        self._read_lbl.pack(pady=(0, 12))

        self._speed_bar = AnimatedProgressBar(c, accent=TAB_ACCENTS["Clean"])
        self._speed_bar.pack(fill="x", pady=(16, 0))
        self._speed_status = tk.Label(c, text="", font=(F, 9), bg=COLORS["bg"],
                                      fg=COLORS["subtext"])
        self._speed_status.pack(pady=(6, 0))

    def _run_speed(self):
        token = self._stop_token
        root = self._speed_var.get().strip() + "\\"
        self._write_lbl.config(text="—")
        self._read_lbl.config(text="—")
        self._speed_btn.set_enabled(False)
        self._speed_bar.set_fraction(0.0)
        self._speed_status.config(text=f"Testing {root} …")

        def progress(frac):
            if not token[0]:
                self._dlg.after(0, lambda: self._speed_bar.set_fraction(frac))

        def worker():
            try:
                from app.drive_toolkit import benchmark_drive
                w, r = benchmark_drive(root, cancelled=lambda: token[0],
                                       progress_cb=progress)
            except Exception:
                w, r = None, None
            if not token[0]:
                self._dlg.after(0, lambda: self._speed_done(w, r, root, token))

        import threading
        threading.Thread(target=worker, daemon=True).start()

    def _speed_done(self, w, r, root, token):
        if token[0] or self._view != "speed":
            return
        try:
            self._speed_btn.set_enabled(True)
        except Exception:
            return
        if w is None or r is None:
            self._speed_status.config(text=f"Couldn't test {root} — it may "
                                           "be write-protected or out of space.")
            return
        self._write_lbl.config(text=f"{w:.0f}")
        self._read_lbl.config(text=f"{r:.0f}")
        self._speed_status.config(text=f"{root}  —  MB/s (megabytes per second)")

    # ==================================================================== #
    # Capacity
    # ==================================================================== #

    def _build_capacity(self):
        c = self._content
        # UI fix: condensed to a single row (was two stacked lines) —
        # same one-row treatment as Health/Speed above. The "don't unplug"
        # safety note now rides the same line instead of its own row.
        tk.Label(c, text="Checks if a USB drive or SD card is fake — "
                         "your files are never touched.",
                  font=(F, 9), bg=COLORS["bg"], fg=COLORS["subtext"],
                  wraplength=700, justify="left").pack(anchor="w", pady=(0, 10))

        self._cap_drives = []
        row = tk.Frame(c, bg=COLORS["bg"])
        row.pack(fill="x")
        tk.Label(row, text="Drive:", font=(F, 9), bg=COLORS["bg"],
                 fg=COLORS["subtext"]).pack(side="left")
        self._cap_var = tk.StringVar(value="")
        import tkinter.ttk as _ttk
        self._cap_combo = _ttk.Combobox(row, textvariable=self._cap_var,
                                        values=[], state="readonly",
                                        width=28, font=(F, 9))
        self._cap_combo.pack(side="left", padx=(6, 8))
        AnimatedButton(row, text="Refresh", command=self._refresh_cap_drives,
                       bg=COLORS["surface"], fg=COLORS["text"],
                       font=(F, 9, "bold"), padx=12,
                       pady=6).pack(side="left")

        mrow = tk.Frame(c, bg=COLORS["bg"])
        mrow.pack(fill="x", pady=(10, 0))
        self._cap_mode = tk.StringVar(value="quick")
        for val, label in (("quick", "Quick check (a minute or two)"),
                          ("full", "Full check (can take hours — most thorough)")):
            rb = tk.Radiobutton(
                mrow, text=label, value=val, variable=self._cap_mode,
                font=(F, 9), bg=COLORS["bg"], fg=COLORS["text"],
                selectcolor=COLORS["bg_alt"], activebackground=COLORS["bg"],
                anchor="w")
            rb.pack(anchor="w")

        self._cap_btn = AnimatedButton(
            c, text="Start Test", command=self._start_capacity,
            bg=TAB_ACCENTS["Clean"], fg=COLORS["black"],
            font=(F, 10, "bold"), padx=22, pady=8)
        self._cap_btn.pack(pady=(12, 0))

        self._cap_bar = AnimatedProgressBar(c, accent=TAB_ACCENTS["Clean"])
        self._cap_bar.pack(fill="x", pady=(14, 0))
        self._cap_result = tk.Label(c, text="", font=(F, 9), bg=COLORS["bg"],
                                    fg=COLORS["subtext"], wraplength=700,
                                    justify="left")
        self._cap_result.pack(pady=(8, 0), anchor="w")
        self._refresh_cap_drives()

    def _refresh_cap_drives(self):
        try:
            from app.drive_toolkit import list_removable_drives
            drives = list_removable_drives()
        except Exception:
            drives = []
        self._cap_drives = drives
        if not drives:
            self._cap_combo.config(values=[])
            self._cap_var.set("")
            self._cap_result.config(
                text="No USB drive or SD card detected. Plug one in, then "
                     "click Refresh.")
            self._cap_btn.set_enabled(False)
            return
        labels = []
        for letter, root, free_b, total_b, label in drives:
            gb = total_b / (1024 ** 3)
            name = f"{letter}: — {label} ({gb:.1f} GB)" if label else \
                  f"{letter}: ({gb:.1f} GB)"
            labels.append(name)
        self._cap_combo.config(values=labels)
        self._cap_var.set(labels[0])
        self._cap_result.config(text="")
        self._cap_btn.set_enabled(True)

    def _start_capacity(self):
        idx = 0
        try:
            sel = self._cap_var.get()
            labels = list(self._cap_combo["values"])
            idx = labels.index(sel) if sel in labels else 0
        except Exception:
            idx = 0
        if not self._cap_drives or idx >= len(self._cap_drives):
            return
        letter, root, free_b, total_b, label = self._cap_drives[idx]
        mode = self._cap_mode.get()
        if mode == "full":
            gb = total_b / (1024 ** 3)
            if not _themed_askyesno(
                    self._dlg, "Full Check",
                    f"A full check writes to all of {letter}:'s free space "
                    f"(about {gb:.1f} GB) and can take a long time on a "
                    "large drive. It won't erase your existing files.\n\n"
                    "Continue?", accent=TAB_ACCENTS["Clean"]):
                return
        token = self._stop_token
        self._cap_btn.set_enabled(False)
        self._cap_bar.set_fraction(0.0)
        self._cap_result.config(
            text=f"Testing {letter}: — you can keep using your PC, just "
                 "don't remove the drive.", fg=COLORS["subtext"])

        def progress(frac):
            if not token[0]:
                self._dlg.after(0, lambda: self._cap_bar.set_fraction(frac))

        def worker():
            try:
                from app.drive_toolkit import capacity_test
                result = capacity_test(root, mode=mode,
                                       cancelled=lambda: token[0],
                                       progress_cb=progress)
            except Exception as exc:
                result = {"ok": False, "error": str(exc), "declared_bytes": 0,
                          "good_bytes": 0, "tested_bytes": 0}
            if not token[0]:
                self._dlg.after(0, lambda: self._capacity_done(result, letter, token))

        import threading
        threading.Thread(target=worker, daemon=True).start()

    def _capacity_done(self, result, letter, token):
        if token[0] or self._view != "capacity":
            return
        try:
            self._cap_btn.set_enabled(True)
        except Exception:
            return
        if result.get("error"):
            self._cap_result.config(
                text=f"Test stopped: {result['error']}", fg=COLORS["accent_yellow"])
            return
        gb = lambda b: b / (1024 ** 3)
        if result["ok"]:
            self._cap_result.config(
                text=f"✅ {letter}: checks out — every part tested holds "
                     f"real data ({gb(result['tested_bytes']):.2f} GB tested"
                     + (", quick sample" if result.get("mode") == "quick" else "")
                     + ").",
                fg=COLORS["accent_green"])
        else:
            self._cap_result.config(
                text=(f"⚠️ {letter}: is NOT what it claims. It reports "
                     f"{gb(result['declared_bytes']):.1f} GB, but only "
                     f"about {gb(result['good_bytes']):.2f} GB of the part "
                     "we tested was genuine. The rest will corrupt your "
                     "files once you fill past the real capacity — this is "
                     "very likely a counterfeit or damaged drive."),
                fg=COLORS["accent_red"])
