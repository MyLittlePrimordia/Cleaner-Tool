"""DriveToolkitDialog — extracted verbatim from app/gui.py (H12, batch 4).

One dialog per module, mirroring app/ui/widgets. Depends only on
app.ui below it, plus the app.utils helpers it already used; it
knows nothing
about any other dialog except where noted.
"""
from __future__ import annotations

import tkinter as tk

from app import facts
from app.ui.widgets.dropdown import M3Dropdown

from app.ui.widgets.status_icon import StatusIcon
from app.ui.base import _themed_askyesno
from app.ui.dialogs._tool_page import ToolPage
from app.ui.theme import (
    COLORS, ICON_CONTROL, SPACE_2, SPACE_3, SPACE_4, TAB_ACCENTS, TYPE
)
from app.ui.metrics import (
    CARD_BODY_INSET, CONTROL_GAP, GROUP_GAP, SECTION_BREAK, TEXT_NUDGE,
)
from app.ui.widgets import AnimatedButton, AnimatedProgressBar, ScrollableRoundedPanel, SegmentedTabs


class DriveToolkitDialog(ToolPage):
    """Drive Toolkit (Tools tab card): 3-in-1 — Health, Speed, Capacity.
    The internal tab switch (two or three AnimatedButton tabs swapping
    self._content wholesale) is the same pattern the other multi-view
    tools use.

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
                         accent=TAB_ACCENTS["Clean"],
                         scope="Read-only checks; the speed test writes one "
                               "temp file and removes it.",
                         scroll=False, fit=True)
        # REDESIGN 2026-10: on the shared ToolPage shell; the per-tab actions
        # stay in the content zone and the Header banner opens the dialog.
        body = self.content
        self.banner.set_state("\u25A4", TAB_ACCENTS["Clean"])
        self.banner.set_title("How healthy is this drive?")
        # A-003: this used to read "Health, speed and a fake USB/SD card
        # check", which reads as "a pretend check" under a health headline.
        # The test behind it is the most serious thing in the dialog - it
        # writes the drive's full claimed capacity and reads every block
        # back, the only way to catch a controller that wraps around - so
        # the word is "counterfeit", not "fake".
        self.banner.set_sub("Health, speed and a counterfeit USB/SD card "
                            "test.")
        # One centered pill (same language as the main Clean/Repair/Tweak
        # switcher) — replaces the old three separate left-packed buttons.
        tabs = tk.Frame(body, bg=COLORS["bg"])
        tabs.pack(fill="x", pady=(0, GROUP_GAP))
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
                 font=TYPE["caption"], bg=COLORS["bg"], fg=COLORS["subtext"],
                 wraplength=self.body_width(28), justify="left").pack(anchor="w", pady=(0, SPACE_2))
        self._health_panel = ScrollableRoundedPanel(c)
        self._health_panel.config(height=280)
        self._health_panel.pack(fill="both", expand=True)
        brow = tk.Frame(c, bg=COLORS["bg"])
        brow.pack(fill="x", pady=(SPACE_2, 0))
        self._health_status = tk.Label(brow, text="Checking…", font=TYPE["caption"],
                                       bg=COLORS["bg"], fg=COLORS["subtext"])
        self._health_status.pack(side="left")
        AnimatedButton(brow, text="Refresh", command=self._run_health,
                       role="secondary", size="sm").pack(side="right")
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
                from app.drive_toolkit import (list_physical_disks,
                                               publish_health_findings)
                disks = list_physical_disks()
            except Exception:
                disks = []
            else:
                # Off the Tk thread: the bus writes config, and a warning
                # found here is what Health and the hub should see next.
                try:
                    publish_health_findings(disks)
                except Exception:
                    pass
            if not token[0]:
                self._dispatch.post(lambda: self._health_done(disks, token))

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
                     font=TYPE["caption"], bg=COLORS["bg_alt"],
                     fg=COLORS["subtext"]).pack(pady=SPACE_4)
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
            _kind = {"Healthy": "ok", "Warning": "warn",
                     "Unhealthy": "bad"}.get(d["health"])
            if _kind:
                StatusIcon(row, _kind, bg=COLORS["bg_alt"],
                            size=ICON_CONTROL).pack(
                    side="left", padx=(SPACE_3, GROUP_GAP), pady=GROUP_GAP)
            else:
                tk.Label(row, text=glyph, font=TYPE["body_xlarge"], bg=COLORS["bg_alt"],
                         fg=color).pack(side="left", padx=(SPACE_3, GROUP_GAP), pady=GROUP_GAP)
            info = tk.Frame(row, bg=COLORS["bg_alt"])
            info.pack(side="left", fill="x", expand=True)
            size_gb = d["size_bytes"] / (1024 ** 3) if d["size_bytes"] else 0
            title = d["name"]
            if size_gb:
                title += f"  ·  {size_gb:.0f} GB  ·  {d['media_type']}"
            else:
                title += f"  ·  {d['media_type']}"
            tk.Label(info, text=title, font=TYPE["label_medium"], bg=COLORS["bg_alt"],
                     fg=COLORS["text"], anchor="w").pack(fill="x")
            words = {"Healthy": "Working normally",
                    "Warning": "Showing early warning signs — back up your files soon",
                    "Unhealthy": "Failing — back up your files now"}
            tk.Label(info, text=words.get(d["health"], d["health"]),
                     font=TYPE["caption"], bg=COLORS["bg_alt"], fg=color,
                     anchor="w").pack(fill="x")
            row.pack(fill="x", pady=TEXT_NUDGE)
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
                 font=TYPE["caption"], bg=COLORS["bg"], fg=COLORS["subtext"],
                 wraplength=self.body_width(28), justify="left").pack(anchor="w", pady=(0, GROUP_GAP))

        row = tk.Frame(c, bg=COLORS["bg"])
        row.pack(fill="x")
        tk.Label(row, text="Drive:", font=TYPE["caption"], bg=COLORS["bg"],
                 fg=COLORS["subtext"]).pack(side="left")
        # AUDIT TX-001: the picker reads the shared `disks.usage` fact, the
        # same volumes PC Specs shows. This dialog and PC Specs used to walk
        # the logical drives independently with two near-identical readers.
        try:
            disk_fact = facts.get("disks.usage")
            drives = [d for d in (disk_fact.value or [])
                      if isinstance(d, dict)] if disk_fact.ok else []
        except Exception:
            drives = []
        names = [str(d.get("drive") or "").strip() for d in drives
                 if d.get("drive")] or ["C:"]
        self._speed_var = tk.StringVar(value=names[0])
        combo = M3Dropdown(row, textvariable=self._speed_var, values=names,
                              state="readonly", width=8, font=TYPE["caption"])
        combo.pack(side="left", padx=(CONTROL_GAP, SPACE_3))
        self._speed_btn = AnimatedButton(
            row, text="Test This Drive", command=self._run_speed,
            role="filled", size="sm", accent=TAB_ACCENTS["Clean"])
        self._speed_btn.pack(side="left")
        if not drives:
            self._speed_btn.set_enabled(False)

        results = tk.Frame(c, bg=COLORS["bg"])
        results.pack(fill="x", pady=(SECTION_BREAK, 0))
        results.grid_columnconfigure(0, weight=1, uniform="s")
        results.grid_columnconfigure(1, weight=1, uniform="s")
        w_cell = tk.Frame(results, bg=COLORS["bg_alt"])
        w_cell.grid(row=0, column=0, sticky="ew", padx=(0, CONTROL_GAP))
        tk.Label(w_cell, text="WRITE", font=TYPE["label_medium"], bg=COLORS["bg_alt"],
                 fg=COLORS["subtext"]).pack(pady=(SPACE_3, 0))
        self._write_lbl = tk.Label(w_cell, text="—", font=TYPE["metric_medium"],
                                   bg=COLORS["bg_alt"], fg=COLORS["text"])
        self._write_lbl.pack(pady=(0, SPACE_3))
        r_cell = tk.Frame(results, bg=COLORS["bg_alt"])
        r_cell.grid(row=0, column=1, sticky="ew", padx=(CONTROL_GAP, 0))
        tk.Label(r_cell, text="READ", font=TYPE["label_medium"], bg=COLORS["bg_alt"],
                 fg=COLORS["subtext"]).pack(pady=(SPACE_3, 0))
        self._read_lbl = tk.Label(r_cell, text="—", font=TYPE["metric_medium"],
                                  bg=COLORS["bg_alt"], fg=COLORS["text"])
        self._read_lbl.pack(pady=(0, SPACE_3))

        self._speed_bar = AnimatedProgressBar(c, accent=TAB_ACCENTS["Clean"])
        self._speed_bar.pack(fill="x", pady=(SPACE_4, 0))
        self._speed_status = tk.Label(c, text="", font=TYPE["caption"], bg=COLORS["bg"],
                                      fg=COLORS["subtext"])
        self._speed_status.pack(pady=(CONTROL_GAP, 0))

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
                self._dispatch.post(lambda: self._speed_bar.set_fraction(frac))

        def worker():
            try:
                from app.drive_toolkit import benchmark_drive
                w, r = benchmark_drive(root, cancelled=lambda: token[0],
                                       progress_cb=progress)
            except Exception:
                w, r = None, None
            if not token[0]:
                self._dispatch.post(lambda: self._speed_done(w, r, root, token))

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
                  font=TYPE["caption"], bg=COLORS["bg"], fg=COLORS["subtext"],
                  wraplength=self.body_width(28), justify="left").pack(anchor="w", pady=(0, GROUP_GAP))

        self._cap_drives = []
        row = tk.Frame(c, bg=COLORS["bg"])
        row.pack(fill="x")
        tk.Label(row, text="Drive:", font=TYPE["caption"], bg=COLORS["bg"],
                 fg=COLORS["subtext"]).pack(side="left")
        self._cap_var = tk.StringVar(value="")
        self._cap_combo = M3Dropdown(row, textvariable=self._cap_var,
                                        values=[], state="readonly",
                                        width=28, font=TYPE["caption"])
        self._cap_combo.pack(side="left", padx=(CONTROL_GAP, SPACE_2))
        AnimatedButton(row, text="Refresh", command=self._refresh_cap_drives,
                       role="secondary", size="sm").pack(side="left")

        mrow = tk.Frame(c, bg=COLORS["bg"])
        mrow.pack(fill="x", pady=(GROUP_GAP, 0))
        self._cap_mode = tk.StringVar(value="quick")
        self._cap_hint = tk.Label(c, text="", font=TYPE["micro_regular"], bg=COLORS["bg"],
                                  fg=COLORS["subtext"],
                                  wraplength=self.body_width(28), justify="left")

        def _cap_mode_changed(mode):
            self._cap_mode.set(mode)
            self._cap_hint.config(text={
                "quick": "Quick check — a handful of reads; usually a minute or two.",
                "full": "Full check — the most thorough scan; can take hours on a large drive.",
            }.get(mode, ""))

        # Themed segmented control (same language as this dialog's own
        # Health/Speed/Capacity pill) instead of a pair of native radios.
        self._cap_tabs = SegmentedTabs(
            mrow, labels=["Quick check", "Full check"],
            keys=["quick", "full"], initial="quick",
            accent=TAB_ACCENTS["Clean"], font=TYPE["caption"],
            command=_cap_mode_changed)
        self._cap_tabs.pack(anchor="w")
        self._cap_hint.pack(anchor="w", pady=(CONTROL_GAP, 0))
        _cap_mode_changed("quick")

        self._cap_btn = AnimatedButton(
            c, text="Start Test", command=self._start_capacity,
            role="filled", size="md", accent=TAB_ACCENTS["Clean"])
        self._cap_btn.pack(pady=(SPACE_3, 0))

        self._cap_bar = AnimatedProgressBar(c, accent=TAB_ACCENTS["Clean"])
        self._cap_bar.pack(fill="x", pady=(CARD_BODY_INSET, 0))
        self._cap_result = tk.Label(c, text="", font=TYPE["caption"], bg=COLORS["bg"],
                                    fg=COLORS["subtext"], wraplength=self.body_width(28),
                                    justify="left")
        self._cap_result.pack(pady=(SPACE_2, 0), anchor="w")
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
                    self._dlg, "Run a Full Check?",
                    f"It writes to all the free space on {letter} "
                    f"(about {gb:.1f} GB) and can take a long time on a "
                    "large drive. It won't erase your existing files.",
                    accent=TAB_ACCENTS["Clean"]):
                return
        token = self._stop_token
        self._cap_btn.set_enabled(False)
        self._cap_bar.set_fraction(0.0)
        self._cap_result.config(
            text=f"Testing {letter}: — you can keep using your PC, just "
                 "don't remove the drive.", fg=COLORS["subtext"])

        def progress(frac):
            if not token[0]:
                self._dispatch.post(lambda: self._cap_bar.set_fraction(frac))

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
                self._dispatch.post(lambda: self._capacity_done(result, letter, token))

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
