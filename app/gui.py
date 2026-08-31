"""
GUI layer — compact, symmetrical, Windows 11 dark, laymen labels with hover tooltips.
Thread-safe: worker thread never touches Tk widgets directly; all UI updates via root.after.
3-column no-scroll layout, centered colored tab bar, centered equal-width header buttons.
"""

import pathlib
import subprocess
import sys
import threading
import time
import tkinter as tk
from tkinter import ttk, scrolledtext, messagebox

from app.config import APP_NAME, WINDOW_SIZE, WINDOW_MIN_SIZE, COLORS, FONT_FAMILY, MONO_FONT_FAMILY
from app.elevation import is_admin, relaunch_as_admin
from app.utils import TaskContext, format_bytes
from app.tasks import clean_tasks, repair_tasks, tweak_tasks, game_tasks
from app.toast import notify_clean_complete  # noqa: E402
from app.warnings import check_dangerous_combos  # noqa: E402


def _set_window_icon(root: tk.Tk):
    """Set colored 🧼 window icon next to title Cleaner — no black square, no feather."""
    try:
        candidates = []
        if getattr(sys, "_MEIPASS", None):
            candidates.append(pathlib.Path(sys._MEIPASS) / "app" / "assets" / "icon.ico")
            candidates.append(pathlib.Path(sys._MEIPASS) / "assets" / "icon.ico")
        candidates.append(pathlib.Path(__file__).with_name("assets") / "icon.ico")
        for p in candidates:
            if p.is_file():
                # iconbitmap is the correct way to set the title-bar icon on Windows (color, no scaling)
                root.iconbitmap(str(p))
                return
    except Exception:
        pass
    # Fallback: leave default if ico not found (no transparent 1x1 to avoid black square)


def _load_tab_images():
    """Load 🧹 🔧 ⚙️ 🎮 PNGs from app/assets for tab buttons."""
    images = {}
    mapping = {"Clean": "clean.png", "Repair": "repair.png", "Tweak": "tweak.png", "Games": "game.png"}
    for name, fname in mapping.items():
        candidates = []
        if getattr(sys, "_MEIPASS", None):
            candidates.append(pathlib.Path(sys._MEIPASS) / "app" / "assets" / fname)
            candidates.append(pathlib.Path(sys._MEIPASS) / "assets" / fname)
        candidates.append(pathlib.Path(__file__).with_name("assets") / fname)
        candidates.append(pathlib.Path(__file__).resolve().parents[1] / "app" / "assets" / fname)
        for p in candidates:
            if p.is_file():
                try:
                    img = tk.PhotoImage(file=str(p))
                    # 32px source → subsample to ~16 for button
                    if img.width() > 20:
                        factor = max(1, img.width() // 16)
                        if factor > 1:
                            img = img.subsample(factor, factor)
                    images[name] = img
                    break
                except Exception:
                    continue
    return images

TAB_COLORS = {
    "Clean": COLORS["accent_green"],    # green
    "Repair": COLORS["accent_yellow"],  # yellow
    "Tweak": COLORS["accent_blue"],     # blue
    "Games": COLORS["accent_teal"],     # teal
}
TAB_COLORS_INACTIVE_BG = COLORS["surface"]
TAB_COLORS_ACTIVE_FG = "#111111"


# --------------------------------------------------------------------------- #
# Tooltip
# --------------------------------------------------------------------------- #

class Tooltip:
    _shared_tip = None  # Class-level shared tooltip window
    _shared_label = None
    _current_widget = None

    def __init__(self, widget, text: str):
        self.widget = widget
        self.text = text
        widget.bind("<Enter>", self._show)
        widget.bind("<Leave>", self._hide)

    def _show(self, event=None):
        if not self.text:
            return
        # If another widget's tooltip is showing, hide it first
        if Tooltip._current_widget is not None and Tooltip._current_widget != self.widget:
            Tooltip._hide_shared()
        
        Tooltip._current_widget = self.widget
        x = self.widget.winfo_rootx() + 20
        y = self.widget.winfo_rooty() + self.widget.winfo_height() + 6
        
        if Tooltip._shared_tip is None:
            Tooltip._shared_tip = tw = tk.Toplevel(self.widget)
            tw.wm_overrideredirect(True)
            tw.configure(bg=COLORS["surface"])
            Tooltip._shared_label = tk.Label(tw, text="", bg="#2d2d2d", fg="#f0f0f0",
                                           font=(FONT_FAMILY, 8), wraplength=320, justify="left",
                                           padx=8, pady=6, bd=0)
            Tooltip._shared_label.pack()
        else:
            tw = Tooltip._shared_tip
        
        Tooltip._shared_label.config(text=self.text)
        tw.wm_geometry(f"+{x}+{y}")
        tw.deiconify()

    def _hide(self, event=None):
        if Tooltip._current_widget == self.widget:
            Tooltip._hide_shared()

    @classmethod
    def _hide_shared(cls):
        if cls._shared_tip:
            cls._shared_tip.withdraw()
        cls._current_widget = None


# --------------------------------------------------------------------------- #
# Admin gate
# --------------------------------------------------------------------------- #

class AdminGateFrame(tk.Frame):
    def __init__(self, root, on_continue_limited):
        super().__init__(root, bg=COLORS["bg"])
        self.root = root
        self.on_continue_limited = on_continue_limited
        self.pack(fill="both", expand=True)
        wrapper = tk.Frame(self, bg=COLORS["bg"])
        wrapper.place(relx=0.5, rely=0.5, anchor="center")
        tk.Label(wrapper, text="🛡️", font=("Segoe UI", 36), bg=COLORS["bg"],
                 fg=COLORS["accent_yellow"]).pack(pady=(0, 8))
        tk.Label(wrapper, text="Administrator Privileges Required", font=(FONT_FAMILY, 14, "bold"),
                 bg=COLORS["bg"], fg=COLORS["text"]).pack()
        tk.Label(
            wrapper,
            text=("Most cleaning works without admin, but repairs and tweaks need it.\n"
                  "Click below to restart as Administrator (UAC will appear)."),
            font=(FONT_FAMILY, 9), bg=COLORS["bg"], fg=COLORS["subtext"], justify="center",
        ).pack(pady=(6, 16))
        btn_row = tk.Frame(wrapper, bg=COLORS["bg"])
        btn_row.pack()
        tk.Button(
            btn_row, text="Restart as Administrator", font=(FONT_FAMILY, 10, "bold"),
            bg=COLORS["accent_green"], fg=COLORS["black"], activebackground=COLORS["accent_teal"],
            relief="flat", padx=18, pady=7, command=self._restart_elevated,
        ).pack(side="left", padx=5)
        tk.Button(
            btn_row, text="Continue Without Admin", font=(FONT_FAMILY, 9),
            bg=COLORS["surface"], fg=COLORS["text"], activebackground=COLORS["surface_hover"],
            relief="flat", padx=14, pady=7, command=self._continue_limited,
        ).pack(side="left", padx=5)

    def _restart_elevated(self):
        if relaunch_as_admin():
            # Disable buttons and show waiting state without blocking the main thread
            for child in self.winfo_children():
                try:
                    for btn in child.winfo_children():
                        for sub in btn.winfo_children():
                            if isinstance(sub, tk.Button):
                                sub.config(state="disabled")
                except Exception:
                    pass
            # Also disable direct buttons in btn_row if found
            self._set_elevate_buttons_state("disabled")
            self._wait_status = tk.Label(self, text="Waiting for elevation (UAC)...", font=(FONT_FAMILY, 8),
                                         bg=COLORS["bg"], fg=COLORS["accent_blue"])
            self._wait_status.pack(pady=(8, 0))

            def _wait_thread():
                from app.elevation import wait_for_elevated_process
                success = wait_for_elevated_process(timeout=15.0)
                # All UI work must happen on main thread via after
                def _on_done():
                    if success:
                        try:
                            self.root.destroy()
                        except Exception:
                            pass
                        sys.exit(0)
                    else:
                        # Elevated process didn't start in time (user cancelled UAC)
                        try:
                            if hasattr(self, "_wait_status") and self._wait_status.winfo_exists():
                                self._wait_status.destroy()
                        except Exception:
                            pass
                        self._set_elevate_buttons_state("normal")
                        try:
                            messagebox.showwarning("Elevation Cancelled", "Administrator elevation was cancelled or timed out. Continuing in limited mode.")
                        except Exception:
                            pass
                        self._continue_limited()
                # Schedule on Tk main loop (after may fail if root destroyed)
                try:
                    self.root.after(0, _on_done)
                except Exception:
                    # Root already destroyed
                    pass

            threading.Thread(target=_wait_thread, daemon=True).start()
        else:
            messagebox.showerror("Elevation Failed", "Could not request administrator rights. You can still continue in limited mode.")

    def _set_elevate_buttons_state(self, state: str):
        # Find buttons in this frame and set state
        try:
            for wrapper in self.winfo_children():
                for child in wrapper.winfo_children():
                    if isinstance(child, tk.Frame):
                        for btn in child.winfo_children():
                            if isinstance(btn, tk.Button):
                                try:
                                    btn.config(state=state)
                                except Exception:
                                    pass
        except Exception:
            pass

    def _continue_limited(self):
        self.destroy()
        self.on_continue_limited()


# --------------------------------------------------------------------------- #
# TaskTab — 3 columns, no scroll, centered equal-width header buttons
# --------------------------------------------------------------------------- #

class TaskTab(tk.Frame):
    def __init__(self, parent, app, tab_name, tasks, supports_revert=False):
        super().__init__(parent, bg=COLORS["bg"])
        self.app = app
        self.tab_name = tab_name
        self.tasks = tasks
        self.supports_revert = supports_revert
        self.vars = {}
        self._build_header()
        self._build_task_grid()
        self._build_footer()

    def _build_header(self):
        header = tk.Frame(self, bg=COLORS["bg"])
        header.pack(fill="x", padx=14, pady=(8, 4))
        center = tk.Frame(header, bg=COLORS["bg"])
        center.pack(anchor="center")
        btn_style = dict(font=(FONT_FAMILY, 8), bg=COLORS["surface"], fg=COLORS["text"],
                         activebackground=COLORS["surface_hover"], activeforeground=COLORS["text"],
                         relief="flat", width=12, pady=3)
        tk.Button(center, text="Select All", command=self.select_all, **btn_style).pack(side="left", padx=3)
        tk.Button(center, text="Clear", command=self.deselect_all, **btn_style).pack(side="left", padx=3)
        tk.Button(center, text="Default", command=self.select_defaults, **btn_style).pack(side="left", padx=3)
        if self.supports_revert:
            tk.Button(center, text="Undo Tweaks", command=self._revert_selected,
                      font=(FONT_FAMILY, 8), bg=COLORS["surface"], fg=COLORS["text"],
                      activebackground=COLORS["surface_hover"], activeforeground=COLORS["text"],
                      relief="flat", width=12, pady=3).pack(side="left", padx=3)

    def _build_task_grid(self):
        container = tk.Frame(self, bg=COLORS["bg_alt"], bd=0, relief="flat")
        container.pack(fill="x", padx=14, pady=4)

        # No scroll — 3 equal columns — fill x only so no extra vertical space
        inner = tk.Frame(container, bg=COLORS["bg_alt"])
        inner.pack(fill="x", padx=6, pady=6)

        # equal weight columns
        inner.grid_columnconfigure(0, weight=1, uniform="col")
        inner.grid_columnconfigure(1, weight=1, uniform="col")
        inner.grid_columnconfigure(2, weight=1, uniform="col")

        risk_colors = {
            "SAFE": COLORS["accent_green"],
            "ADVANCED": COLORS["accent_yellow"],
            "REBOOT REQUIRED": COLORS["accent_mauve"],
        }

        # distribute tasks round-robin across 3 columns to eliminate vertical scroll
        # Note: Task.column is legacy/deprecated — layout is auto-balanced via round-robin
        col_rows = {0: 0, 1: 0, 2: 0}
        for idx, task in enumerate(self.tasks):
            # Honor explicit column if it was intentionally set to 2, otherwise round-robin for balance
            if hasattr(task, "column") and task.column == 2 and len(self.tasks) > 6:
                col = 2
            else:
                col = idx % 3
            row = col_rows[col]
            col_rows[col] += 1
            var = tk.BooleanVar(value=task.default)
            self.vars[task.key] = var

            col_frame = tk.Frame(inner, bg=COLORS["bg_alt"])
            col_frame.grid(row=row, column=col, sticky="nw", padx=4, pady=2)

            cb = tk.Checkbutton(
                col_frame, variable=var, bg=COLORS["bg_alt"], fg=COLORS["text"],
                activebackground=COLORS["bg_alt"], selectcolor=COLORS["surface"],
                onvalue=True, offvalue=False,
            )
            cb.pack(side="left", anchor="n")

            label_row = tk.Frame(col_frame, bg=COLORS["bg_alt"])
            label_row.pack(side="left", anchor="w")
            lbl = tk.Label(label_row, text=task.label, font=(FONT_FAMILY, 8),
                           bg=COLORS["bg_alt"], fg=COLORS["text"], anchor="w", justify="left")
            lbl.pack(side="left")
            Tooltip(lbl, task.description)
            Tooltip(cb, task.description)
            if task.risk != "SAFE":
                tk.Label(label_row, text=f" [{task.risk}]", font=(FONT_FAMILY, 6, "bold"),
                         bg=COLORS["bg_alt"], fg=risk_colors.get(task.risk, COLORS["subtext"])).pack(side="left")

    def _build_footer(self):
        footer = tk.Frame(self, bg=COLORS["bg"])
        footer.pack(fill="x", padx=14, pady=(4, 8))
        center = tk.Frame(footer, bg=COLORS["bg"])
        center.pack(anchor="center")
        run_label = f"▶ Run {self.tab_name}" if self.tab_name != "Tweak" else "▶ Apply Tweaks"
        tab_color = TAB_COLORS.get(self.tab_name, COLORS["accent_green"])
        self.run_btn = tk.Button(
            center, text=run_label, font=(FONT_FAMILY, 10, "bold"),
            bg=tab_color, fg=COLORS["black"], activebackground=COLORS["accent_teal"],
            relief="flat", padx=18, pady=6, command=self._run_selected,
        )
        self.run_btn.pack(side="left", padx=4)

    def select_all(self):
        for v in self.vars.values():
            v.set(True)

    def deselect_all(self):
        for v in self.vars.values():
            v.set(False)

    def select_defaults(self):
        for task in self.tasks:
            self.vars[task.key].set(task.default)

    def set_buttons_enabled(self, enabled: bool):
        state = "normal" if enabled else "disabled"
        self.run_btn.config(state=state)

    def _selected_tasks(self):
        return [t for t in self.tasks if self.vars[t.key].get()]

    def _run_selected(self):
        selected = self._selected_tasks()
        if not selected:
            messagebox.showinfo("Nothing Selected", "Select at least one task first.")
            return
        self.app.run_tasks(self.tab_name, selected, mode="run")

    def _revert_selected(self):
        selected = [t for t in self._selected_tasks() if t.revert]
        if not selected:
            messagebox.showinfo("Nothing to Undo", "Select at least one reversible tweak first.")
            return
        if not messagebox.askyesno("Confirm Undo", f"Undo {len(selected)} tweak(s) back to defaults?"):
            return
        self.app.run_tasks(self.tab_name, selected, mode="revert")


# --------------------------------------------------------------------------- #
# Main window — centered colored tab bar, no ttk.Notebook tabs
# --------------------------------------------------------------------------- #

class Application:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("Cleaner")  # show as Cleaner with 🧼 icon next to it
        self.root.geometry(WINDOW_SIZE)
        self.root.minsize(*WINDOW_MIN_SIZE)
        
        # Handle window close
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        
        # Load saved theme
        self.root.configure(bg=COLORS["bg"])
        _set_window_icon(self.root)
        self._busy = False
        self._busy_lock = threading.Lock()
        self._cancel_requested = False
        self._build_style()
        if is_admin():
            self._build_main_ui()
        else:
            AdminGateFrame(self.root, on_continue_limited=self._build_main_ui)

    def _on_close(self):
        self._stop_disk_monitor()
        self.root.destroy()

    def _build_style(self):
        style = ttk.Style()
        style.theme_use("default")
        style.configure(".", background=COLORS["bg"], foreground=COLORS["text"])
        style.configure("TNotebook", background=COLORS["bg"], borderwidth=0)
        style.configure("TNotebook.Tab", background=COLORS["surface"], foreground=COLORS["text"],
                        padding=[14, 6], font=(FONT_FAMILY, 9, "bold"))
        style.map("TNotebook.Tab", background=[("selected", COLORS["bg_alt"])],
                  foreground=[("selected", COLORS["accent_green"])])
        style.configure("TLabel", background=COLORS["bg"], foreground=COLORS["text"], font=(FONT_FAMILY, 9))
        style.configure("Status.TLabel", background=COLORS["bg"], foreground=COLORS["accent_blue"],
                        font=(FONT_FAMILY, 9, "bold"))
        style.configure("TProgressbar", thickness=10, troughcolor=COLORS["surface"],
                        background=COLORS["accent_green"])
        style.configure("TCheckbutton", background=COLORS["bg"], foreground=COLORS["text"])
        # Combobox dark theme — fixes white-on-white popup reported in Auto Maintenance dialog
        style.configure("TCombobox",
                        fieldbackground=COLORS["surface"],
                        background=COLORS["surface"],
                        foreground=COLORS["text"],
                        arrowcolor=COLORS["text"],
                        selectbackground=COLORS["surface_hover"],
                        selectforeground=COLORS["text"],
                        bordercolor=COLORS["surface"],
                        lightcolor=COLORS["surface"],
                        darkcolor=COLORS["surface"])
        style.map("TCombobox",
                  fieldbackground=[("readonly", COLORS["surface"]), ("disabled", COLORS["surface"])],
                  background=[("readonly", COLORS["surface"])],
                  foreground=[("readonly", COLORS["text"]), ("disabled", COLORS["subtext"])],
                  selectbackground=[("readonly", COLORS["surface_hover"])],
                  selectforeground=[("readonly", COLORS["text"])],
                  arrowcolor=[("readonly", COLORS["text"])])
        # Listbox popup for Combobox (the dropdown) — default is white
        self.root.option_add("*TCombobox*Listbox.background", COLORS["surface"])
        self.root.option_add("*TCombobox*Listbox.foreground", COLORS["text"])
        self.root.option_add("*TCombobox*Listbox.selectBackground", COLORS["surface_hover"])
        self.root.option_add("*TCombobox*Listbox.selectForeground", COLORS["text"])
        self.root.option_add("*TCombobox*Listbox.font", (FONT_FAMILY, 9))



    def _build_main_ui(self):
        for widget in self.root.winfo_children():
            widget.destroy()
        self._build_menu()
        title_frame = tk.Frame(self.root, bg=COLORS["bg"])
        title_frame.pack(fill="x")
        # Header: Cleaner with colored 🧼 image next to it
        header_row = tk.Frame(title_frame, bg=COLORS["bg"])
        header_row.pack(pady=(8, 0))
        try:
            hdr_candidates = []
            if getattr(sys, "_MEIPASS", None):
                hdr_candidates.append(pathlib.Path(sys._MEIPASS) / "app" / "assets" / "soap_header.png")
                hdr_candidates.append(pathlib.Path(sys._MEIPASS) / "assets" / "soap_header.png")
            hdr_candidates.append(pathlib.Path(__file__).with_name("assets") / "soap_header.png")
            hdr_img = None
            for hp in hdr_candidates:
                if hp.is_file():
                    hdr_img = tk.PhotoImage(file=str(hp))
                    break
            if hdr_img is not None:
                lbl_img = tk.Label(header_row, image=hdr_img, bg=COLORS["bg"])
                lbl_img.image = hdr_img  # type: ignore
                lbl_img.pack(side="left", padx=(0, 6))
                self._header_soap_img = hdr_img  # keep ref
        except Exception:
            pass
        tk.Label(header_row, text="Cleaner", font=(FONT_FAMILY, 12, "bold"),
                 bg=COLORS["bg"], fg=COLORS["accent_blue"]).pack(side="left")
        
        mode = "Administrator Mode" if is_admin() else "Limited Mode — Clean only"
        ttk.Label(title_frame, text=mode, font=(FONT_FAMILY, 8)).pack(pady=(0, 4))

        # Centered colored tab bar
        tab_bar = tk.Frame(self.root, bg=COLORS["bg"])
        tab_bar.pack(fill="x", pady=(2, 0))
        tab_bar_inner = tk.Frame(tab_bar, bg=COLORS["bg"])
        tab_bar_inner.pack(anchor="center")

        self.tab_names = ["Clean", "Repair", "Tweak", "Games"]
        self.tab_buttons = {}
        self.active_tab = tk.StringVar(value="Clean")
        # load emoji images for tabs
        self.tab_images = _load_tab_images()
        for name in self.tab_names:
            color = TAB_COLORS.get(name, COLORS["surface"])
            img = self.tab_images.get(name)
            # No fixed width — image + text needs natural size, otherwise truncates to "Cl"/"Re"
            btn = tk.Button(
                tab_bar_inner, image=img, text=f" {name}", compound="left",
                font=(FONT_FAMILY, 9, "bold"),
                padx=16, pady=4, relief="flat", bd=0,
                bg=color if name == "Clean" else TAB_COLORS_INACTIVE_BG,
                fg=TAB_COLORS_ACTIVE_FG if name == "Clean" else COLORS["text"],
                activebackground=color, activeforeground=TAB_COLORS_ACTIVE_FG,
                command=lambda n=name: self._switch_tab(n),
            )
            if img is not None:
                btn.image = img  # type: ignore
            btn.pack(side="left", padx=4)
            self.tab_buttons[name] = btn

        # Content container — fill x so height hugs checkboxes instead of stretching
        self.page_container = tk.Frame(self.root, bg=COLORS["bg"])
        self.page_container.pack(fill="x", padx=10, pady=4)

        self.tabs = {}
        tab_tasks = {
            "Clean": clean_tasks.TASKS,
            "Repair": repair_tasks.TASKS,
            "Tweak": tweak_tasks.TASKS,
            "Games": game_tasks.TASKS,
        }
        for name in self.tab_names:
            supports_revert = (name == "Tweak")
            page = TaskTab(self.page_container, self, name, tab_tasks[name], supports_revert=supports_revert)
            self.tabs[name] = page

        self._show_tab("Clean")

        bottom = tk.Frame(self.root, bg=COLORS["bg"])
        bottom.pack(fill="both", padx=14, pady=(0, 8))

        self.lbl_status = ttk.Label(bottom, text="Status: Ready.", style="Status.TLabel")
        self.lbl_status.pack(anchor="w", pady=(2, 2))

        # Control row: Preview (dry run) toggle + Cancel button
        ctrl_row = tk.Frame(bottom, bg=COLORS["bg"])
        ctrl_row.pack(fill="x", pady=(0, 4))
        self.preview_var = tk.BooleanVar(value=False)
        preview_cb = tk.Checkbutton(
            ctrl_row, text="Preview Mode (dry run — don't change anything)",
            variable=self.preview_var, bg=COLORS["bg"], fg=COLORS["subtext"],
            activebackground=COLORS["bg"], selectcolor=COLORS["surface"],
            font=(FONT_FAMILY, 8), anchor="w",
        )
        preview_cb.pack(side="left")

        # Disk space monitor
        self.disk_space_var = tk.StringVar(value="Disk: checking...")
        disk_lbl = tk.Label(ctrl_row, textvariable=self.disk_space_var,
                            bg=COLORS["bg"], fg=COLORS["accent_teal"],
                            font=(FONT_FAMILY, 8, "bold"))
        disk_lbl.pack(side="left", padx=(16, 8))

        self.cancel_btn = tk.Button(
            ctrl_row, text="✕ Cancel", font=(FONT_FAMILY, 8, "bold"),
            bg=COLORS["surface"], fg=COLORS["accent_red"], activebackground=COLORS["surface_hover"],
            activeforeground=COLORS["accent_red"], relief="flat", padx=12, pady=3,
            command=self._request_cancel, state="disabled",
        )
        self.cancel_btn.pack(side="right")

        self.progress = ttk.Progressbar(bottom, orient="horizontal", mode="determinate")
        self.progress.pack(fill="x", pady=(0, 4))

        self.log_area = scrolledtext.ScrolledText(
            bottom, wrap=tk.WORD, bg=COLORS["bg_widget"], fg=COLORS["subtext"],
            insertbackground=COLORS["text"], selectbackground=COLORS["surface"],
            selectforeground=COLORS["text"], font=(MONO_FONT_FAMILY, 8), height=7,
        )
        self.log_area.pack(fill="both", expand=True)
        self.log("Welcome. Hover over any option for details. Click Run when ready.")
        if not is_admin():
            self.log("Limited Mode: Repair and Tweak need Administrator. Restart as Administrator to unlock.")

        # Start disk space monitor
        self._start_disk_monitor()

    def _start_disk_monitor(self):
        import ctypes
        self._disk_monitor_running = True

        def update_disk():
            if not getattr(self, "_disk_monitor_running", False):
                return
            try:
                free_bytes = ctypes.c_ulonglong(0)
                total_bytes = ctypes.c_ulonglong(0)
                avail_bytes = ctypes.c_ulonglong(0)
                res = ctypes.windll.kernel32.GetDiskFreeSpaceExW(
                    ctypes.c_wchar_p("C:\\"),
                    ctypes.byref(avail_bytes),
                    ctypes.byref(total_bytes),
                    ctypes.byref(free_bytes),
                )
                if res != 0:
                    gb = free_bytes.value / (1024 ** 3)
                    try:
                        self.disk_space_var.set(f"C: {gb:.1f} GB free")
                    except Exception:
                        pass
            except Exception:
                pass
            # Schedule next update (30 seconds)
            try:
                if getattr(self, "_disk_monitor_running", False) and self.root.winfo_exists():
                    self.root.after(30000, update_disk)
            except Exception:
                pass

        # Start the first update
        try:
            self.root.after(0, update_disk)
        except Exception:
            pass

    def _stop_disk_monitor(self):
        self._disk_monitor_running = False

    def _switch_tab(self, name: str):
        self.active_tab.set(name)
        # update button colors
        for n, btn in self.tab_buttons.items():
            if n == name:
                btn.configure(bg=TAB_COLORS.get(n, COLORS["accent_green"]), fg=TAB_COLORS_ACTIVE_FG)
            else:
                btn.configure(bg=TAB_COLORS_INACTIVE_BG, fg=COLORS["text"])
        self._show_tab(name)

    def _show_tab(self, name: str):
        for n, page in self.tabs.items():
            page.pack_forget()
        self.tabs[name].pack(fill="both", expand=True)

    def _build_menu(self):
        menubar = tk.Menu(self.root)
        tools_menu = tk.Menu(menubar, tearoff=0)
        tools_menu.add_command(label="Task Manager (Startup tab)", command=lambda: self._launch("taskmgr"))
        tools_menu.add_command(label="Disk Cleanup", command=lambda: self._launch("cleanmgr"))
        tools_menu.add_command(label="Reliability Monitor", command=lambda: self._launch("perfmon /rel"))
        tools_menu.add_command(label="Event Viewer", command=lambda: self._launch("eventvwr"))
        tools_menu.add_command(label="System Restore", command=lambda: self._launch("rstrui"))
        tools_menu.add_command(label="Power Options", command=lambda: self._launch("powercfg.cpl"))
        tools_menu.add_separator()
        tools_menu.add_command(label="Auto Maintenance...", command=self._show_schedule_dialog)
        menubar.add_cascade(label="Quick Tools", menu=tools_menu)
        help_menu = tk.Menu(menubar, tearoff=0)
        help_menu.add_command(label="About", command=self._show_about)
        menubar.add_cascade(label="Help", menu=help_menu)
        self.root.config(menu=menubar)

    def _launch(self, command):
        try:
            subprocess.Popen(command, shell=True)
        except Exception as exc:
            messagebox.showerror("Could Not Launch", str(exc))

    def _show_about(self):
        messagebox.showinfo("About", f"{APP_NAME}\n\nSafe, reversible Windows cleaning & tuning.")

    def _show_schedule_dialog(self):
        from app.config_persist import load_config, save_config
        from app.scheduler import enable_schedule, disable_schedule, get_schedule_status

        config = load_config()
        enabled = config.get("schedule_enabled", False)
        freq = config.get("schedule_frequency", "weekly")
        time_str = config.get("schedule_time", "03:00")

        dlg = tk.Toplevel(self.root)
        dlg.title("Auto Maintenance")
        dlg.geometry("360x280")
        dlg.resizable(False, False)
        dlg.configure(bg=COLORS["bg"])
        dlg.transient(self.root)
        dlg.grab_set()

        # Center on parent
        dlg.update_idletasks()
        x = self.root.winfo_rootx() + (self.root.winfo_width() - dlg.winfo_width()) // 2
        y = self.root.winfo_rooty() + (self.root.winfo_height() - dlg.winfo_height()) // 2
        dlg.geometry(f"+{x}+{y}")

        # Enable checkbox
        enabled_var = tk.BooleanVar(value=enabled)
        cb = tk.Checkbutton(
            dlg, text="Enable automatic maintenance",
            variable=enabled_var, bg=COLORS["bg"], fg=COLORS["text"],
            activebackground=COLORS["bg"], selectcolor=COLORS["surface"],
            font=(FONT_FAMILY, 9), anchor="w",
        )
        cb.pack(anchor="w", padx=16, pady=(16, 8))

        # Frequency — use tk.OptionMenu with explicit dark styling (ttk.Combobox is white-on-white on Windows dark theme)
        freq_frame = tk.Frame(dlg, bg=COLORS["bg"])
        freq_frame.pack(fill="x", padx=16, pady=4)
        tk.Label(freq_frame, text="Frequency:", bg=COLORS["bg"], fg=COLORS["text"],
                 font=(FONT_FAMILY, 9)).pack(side="left")
        freq_var = tk.StringVar(value=freq)
        # Dark OptionMenu to replace ttk.Combobox white popup bug
        freq_combo = tk.OptionMenu(freq_frame, freq_var, "daily", "weekly", "monthly")
        freq_combo.config(bg=COLORS["surface"], fg=COLORS["text"],
                          activebackground=COLORS["surface_hover"], activeforeground=COLORS["text"],
                          highlightthickness=0, bd=0, relief="flat",
                          font=(FONT_FAMILY, 9), width=12, indicatoron=True)
        # Style the dropdown Menu itself (otherwise it stays white)
        try:
            menu = freq_combo["menu"]
            menu.config(bg=COLORS["surface"], fg=COLORS["text"],
                        activebackground=COLORS["surface_hover"], activeforeground=COLORS["text"],
                        bd=0, relief="flat", font=(FONT_FAMILY, 9))
        except Exception:
            pass
        freq_combo.pack(side="right")

        # Time
        time_frame = tk.Frame(dlg, bg=COLORS["bg"])
        time_frame.pack(fill="x", padx=16, pady=4)
        tk.Label(time_frame, text="Time (24h):", bg=COLORS["bg"], fg=COLORS["text"],
                 font=(FONT_FAMILY, 9)).pack(side="left")
        time_var = tk.StringVar(value=time_str)
        time_entry = tk.Entry(time_frame, textvariable=time_var, width=10,
                               bg=COLORS["surface"], fg=COLORS["text"],
                               insertbackground=COLORS["text"], font=(FONT_FAMILY, 9))
        time_entry.pack(side="right")

        # Tasks info
        task_frame = tk.Frame(dlg, bg=COLORS["bg"])
        task_frame.pack(fill="x", padx=16, pady=8)
        total_selected = sum(len(v) for v in config.get("selected_tasks", {}).values())
        tk.Label(task_frame, text=f"Tasks to run: {total_selected}",
                 bg=COLORS["bg"], fg=COLORS["subtext"], font=(FONT_FAMILY, 8)).pack(anchor="w")
        tk.Label(task_frame, text="(Select tasks in each tab, then click Run to save)",
                 bg=COLORS["bg"], fg=COLORS["subtext"], font=(FONT_FAMILY, 8)).pack(anchor="w")

        # Status
        status_var = tk.StringVar(value="")
        status_lbl = tk.Label(dlg, textvariable=status_var, bg=COLORS["bg"],
                              fg=COLORS["accent_blue"], font=(FONT_FAMILY, 8), wraplength=320, justify="left")
        status_lbl.pack(anchor="w", padx=16, pady=(4, 8))

        def refresh_status():
            ok, out = get_schedule_status()
            if ok:
                status_var.set(f"Scheduled task exists:\n{out[:200]}")
            else:
                status_var.set("No scheduled task found")

        def apply():
            if enabled_var.get():
                ok, msg = enable_schedule(freq_var.get(), time_var.get())
                if ok:
                    config["schedule_enabled"] = True
                    config["schedule_frequency"] = freq_var.get()
                    config["schedule_time"] = time_var.get()
                    save_config(config)
                    status_var.set("Auto maintenance enabled")
                    self.log("Auto maintenance enabled")
                else:
                    messagebox.showerror("Failed", f"Could not create task:\n{msg}", parent=dlg)
            else:
                ok, msg = disable_schedule()
                if ok:
                    config["schedule_enabled"] = False
                    save_config(config)
                    status_var.set("Auto maintenance disabled")
                    self.log("Auto maintenance disabled")
                else:
                    messagebox.showerror("Failed", f"Could not remove task:\n{msg}", parent=dlg)
            refresh_status()

        # Buttons
        btn_frame = tk.Frame(dlg, bg=COLORS["bg"])
        btn_frame.pack(fill="x", padx=16, pady=(8, 16))
        tk.Button(btn_frame, text="Apply", command=apply,
                  bg=COLORS["accent_green"], fg=COLORS["black"],
                  activebackground=COLORS["accent_teal"], relief="flat",
                  padx=20, pady=6, font=(FONT_FAMILY, 9)).pack(side="right", padx=4)
        tk.Button(btn_frame, text="Close", command=dlg.destroy,
                  bg=COLORS["surface"], fg=COLORS["text"],
                  activebackground=COLORS["surface_hover"], relief="flat",
                  padx=20, pady=6, font=(FONT_FAMILY, 9)).pack(side="right")

        refresh_status()

    # -- thread-safe helpers via after -------------------------------------- #

    def log(self, text):
        def _do():
            try:
                if not self.log_area.winfo_exists():
                    return
                self.log_area.config(state="normal")
                self.log_area.insert(tk.END, f"[{time.strftime('%H:%M:%S')}] {text}\n")
                self.log_area.see(tk.END)
                self.log_area.config(state="disabled")
            except Exception:
                pass
        try:
            if threading.current_thread() is threading.main_thread():
                _do()
            else:
                self.root.after(0, _do)
        except Exception:
            pass

    def set_status(self, text):
        def _do():
            try:
                if hasattr(self, "lbl_status") and self.lbl_status.winfo_exists():
                    self.lbl_status.config(text=f"Status: {text}")
            except Exception:
                pass
        try:
            if threading.current_thread() is threading.main_thread():
                _do()
            else:
                self.root.after(0, _do)
        except Exception:
            pass

    def _set_progress(self, value, maximum=None):
        def _do():
            try:
                if maximum is not None:
                    self.progress["maximum"] = maximum
                self.progress["value"] = value
            except Exception:
                pass
        try:
            self.root.after(0, _do)
        except Exception:
            pass

    def _set_buttons_enabled(self, enabled: bool):
        def _do():
            try:
                for tab in self.tabs.values():
                    tab.set_buttons_enabled(enabled)
                if getattr(self, "cancel_btn", None) is not None:
                    self.cancel_btn.config(state="normal" if not enabled else "disabled")
            except Exception:
                pass
        try:
            self.root.after(0, _do)
        except Exception:
            pass

    # -- task execution ----------------------------------------------------- #

    def run_tasks(self, tab_name, tasks, mode="run"):
        # In limited (non-admin) mode, run only the tasks that don't need admin
        # instead of refusing the whole run. This keeps "Clean only" usable.
        if not is_admin():
            admin_tasks = [t for t in tasks if t.admin_required]
            if admin_tasks:
                names = ", ".join(t.label for t in admin_tasks)
                self.log(f"Limited mode: skipping {len(admin_tasks)} admin-only task(s): {names}")
                tasks = [t for t in tasks if not t.admin_required]
                messagebox.showwarning(
                    "Administrator Required",
                    f"Skipped {len(admin_tasks)} admin-only task(s):\n{names}\n\n"
                    "Running the rest in limited mode.",
                )
            if not tasks:
                messagebox.showinfo(
                    "Nothing to Run",
                    "All selected tasks require Administrator rights. Restart as Administrator to run them.",
                )
                return

        with self._busy_lock:
            if self._busy:
                messagebox.showinfo("Busy", "Please wait for the current operation to finish.")
                return
            self._busy = True

        # Check for dangerous task combinations — per-tab only (Run button is per-tab by design)
        # Cross-tab combos are checked in scheduler.py for --auto-clean which runs all tabs together
        warnings = check_dangerous_combos(tasks)
        if warnings:
            # Deduplicate identical warnings (reboot bundle overlaps)
            warnings = list(dict.fromkeys(warnings))
            msg = "⚠️ Potentially problematic task combinations detected:\n\n" + "\n\n".join(warnings)
            msg += "\n\nDo you want to continue?"
            if not messagebox.askyesno("Confirm Task Combination", msg, icon="warning"):
                with self._busy_lock:
                    self._busy = False
                return

        # Save selected tasks to config for scheduler
        try:
            from app.config_persist import load_config, save_config
            config = load_config()
            config.setdefault("selected_tasks", {})[tab_name] = [t.key for t in tasks]
            save_config(config)
        except Exception:
            pass

        self._cancel_requested = False
        for tab in self.tabs.values():
            tab.set_buttons_enabled(False)

        thread = threading.Thread(target=self._run_tasks_worker, args=(tab_name, tasks, mode), daemon=True)
        thread.start()

    def _request_cancel(self):
        if not self._busy:
            return
        self._cancel_requested = True
        # Kill the command currently running (if any) so long tasks stop promptly.
        try:
            from app.utils import cancel_current_command
            cancel_current_command()
        except Exception:
            pass
        self.log("Cancel requested — finishing the current task, then stopping.")

    def _run_tasks_worker(self, tab_name, tasks, mode):
        verb = "Reverting" if mode == "revert" else "Running"
        preview_tag = " (PREVIEW / dry run)" if self.preview_var.get() else ""
        self.log(f"===== {verb} {len(tasks)} {tab_name} task(s){preview_tag} =====")

        ctx = TaskContext(log=self.log, set_status=self.set_status,
                          dry_run=getattr(self, "preview_var", None) is not None and self.preview_var.get(),
                          cancelled=lambda: self._cancel_requested)

        total_bytes = 0
        completed, failed = 0, 0
        self._set_progress(0, len(tasks))

        for idx, task in enumerate(tasks):
            if ctx.cancelled():
                self.log("Cancelled by user — remaining tasks were skipped.")
                break
            self.set_status(f"{verb}: {task.label}")
            func = task.revert if mode == "revert" else task.run
            
            if func is None:
                self.log(f"  ! No {'revert' if mode=='revert' else 'run'} for '{task.label}'")
                failed += 1
                self._set_progress(idx + 1)
                continue
            try:
                result = func(ctx)
                # Handle return values consistently:
                # - int (not bool): bytes freed (cleaning tasks)
                # - bool: success/failure (e.g., create_restore_point)
                # - str: output text (e.g., dism_analyze)
                # - None: no meaningful return value
                if isinstance(result, int) and not isinstance(result, bool):
                    total_bytes += result
                elif isinstance(result, bool) and not result:
                    # Explicit False return = failure
                    raise RuntimeError(f"Task returned False")
                completed += 1
            except Exception as exc:
                failed += 1
                self.log(f"  ! ERROR in '{task.label}': {exc}")
            self._set_progress(idx + 1)

        summary_lines = [f"{verb} complete: {completed} succeeded, {failed} failed."]
        if total_bytes > 0:
            summary_lines.append(f"Disk space freed: {format_bytes(total_bytes)}")
        summary = "\n".join(summary_lines)
        self.log("=" * 48)
        self.log(summary)
        self.log("=" * 48)
        self.set_status(summary_lines[0])

        with self._busy_lock:
            self._busy = False
        self._cancel_requested = False
        self._set_buttons_enabled(True)
        self.root.after(0, lambda: messagebox.showinfo("Done", summary))
        # Toast notification — must not block Tk thread (PowerShell can cold-start ~2s)
        if total_bytes > 0 or completed > 0:
            try:
                threading.Thread(target=notify_clean_complete, args=(total_bytes, completed), daemon=True).start()
            except Exception:
                pass


def launch():
    root = tk.Tk()
    Application(root)
    root.mainloop()
