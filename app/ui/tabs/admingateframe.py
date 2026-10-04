"""AdminGateFrame — extracted verbatim from app/gui.py (H13).

A feature tab page. May use the widget set and the modal base, and
opens dialogs, but never imports another tab.
"""
from __future__ import annotations

import sys
import threading

import tkinter as tk
from tkinter import ttk, messagebox
from app.elevation import relaunch_as_admin
from app.ui.theme import COLORS, F
from app.ui.widgets import AnimatedButton
from app.ui.tkdispatch import TkDispatcher


class AdminGateFrame(tk.Frame):
    def __init__(self, root, on_continue_limited):
        super().__init__(root, bg=COLORS["bg"])
        self.root = root
        self.on_continue_limited = on_continue_limited
        self.pack(fill="both", expand=True)
        wrapper = tk.Frame(self, bg=COLORS["bg"])
        wrapper.place(relx=0.5, rely=0.5, anchor="center")
        tk.Label(wrapper, text="🛡️", font=("Segoe UI Emoji", 40), bg=COLORS["bg"],
                 fg=COLORS["accent_yellow"]).pack(pady=(0, 10))
        tk.Label(wrapper, text="Admin Needed", font=(F, 15, "bold"),
                 bg=COLORS["bg"], fg=COLORS["text"]).pack()
        tk.Label(
            wrapper,
            text="Repairs and tweaks need admin.",
            font=(F, 10), bg=COLORS["bg"], fg=COLORS["subtext"], justify="center",
        ).pack(pady=(8, 18))
        btn_row = tk.Frame(wrapper, bg=COLORS["bg"])
        btn_row.pack()
        AnimatedButton(
            btn_row, text="Restart as Admin", command=self._restart_elevated,
            bg=COLORS["accent_green"], fg=COLORS["black"], font=(F, 10, "bold"),
        ).pack(side="left", padx=6)
        AnimatedButton(
            btn_row, text="Skip", command=self._continue_limited,
            bg=COLORS["surface"], fg=COLORS["text"], font=(F, 10),
        ).pack(side="left", padx=6)
        # auto-elevate ("stop asking me") + remember-limited + Defender
        # exclusion prefs. Saved on either button click (see
        # _save_gate_prefs); auto_elevate wins on read paths when both
        # end up set. Defender exclusion defaults to ON so users don't
        # have to fight false positives by hand.
        try:
            from app.config_persist import load_config as _load_cfg
            _cfg = _load_cfg()
            _auto0 = bool(_cfg.get("auto_elevate", False))
            _lim0 = bool(_cfg.get("remember_limited", False))
            _def0 = bool(_cfg.get("add_defender_exclusion", False))
        except Exception:
            _auto0, _lim0, _def0 = False, False, True
        self._auto_elevate_var = tk.BooleanVar(value=_auto0)
        self._remember_limited_var = tk.BooleanVar(value=_lim0)
        self._defender_excl_var = tk.BooleanVar(value=_def0)
        try:
            from app import elevated_launch as _el
            _notice = _el.notice_for_gate()
        except Exception:
            _notice = None
        if _notice:
            try:
                tk.Label(wrapper, text=_notice, font=(F, 9),
                         bg=COLORS["bg"], fg=COLORS["accent_yellow"],
                         wraplength=420, justify="center").pack(pady=(12, 0))
            except Exception:
                pass
        for _text, _var in (
                ("Always start as Admin",
                 self._auto_elevate_var),
                ("Exclude this app's .exe from Defender (opt-in)",
                 self._defender_excl_var),
                ("Don't show this again",
                 self._remember_limited_var)):
            try:
                _pady = (10 if _var is self._auto_elevate_var else 2, 0)
                tk.Checkbutton(wrapper, text=_text, variable=_var,
                               font=(F, 9), bg=COLORS["bg"], fg=COLORS["subtext"],
                               selectcolor=COLORS["surface"],
                               activebackground=COLORS["bg"],
                               activeforeground=COLORS["text"],
                               anchor="w", justify="left",
                               wraplength=420).pack(fill="x", pady=_pady,
                                                    padx=20)
            except Exception:
                pass

        # SEC-004 migration: an older build defaulted this exclusion ON and
        # applied it to the executable's WHOLE PARENT FOLDER. That entry is not
        # in our record (the record did not exist then), so the normal removal
        # path deliberately ignores it - and a portable copy would keep its
        # entire folder un scanned indefinitely. Offered here, once, next to
        # the checkbox that caused it. Never removed automatically: the app
        # cannot distinguish an exclusion it added from one the user added on
        # purpose, and silently deleting a security setting is worse than
        # asking.
        try:
            from app.config_persist import load_config, update_config
            _cfgd = load_config() or {}
            if not _cfgd.get("defender_migration_notice_shown") and not _def0:
                from app.elevation import find_stale_folder_exclusion
                _stale = find_stale_folder_exclusion()
                if _stale:
                    tk.Label(
                        wrapper,
                        text=("An older version of Cleaner Tool excluded this whole "
                              "folder from Defender:\n%s\n\nNothing else in it is "
                              "scanned. Remove that exclusion?" % _stale),
                        font=(F, 9), bg=COLORS["bg"], fg=COLORS["accent_yellow"],
                        wraplength=420, justify="left").pack(pady=(12, 4), padx=20)

                    def _do_remove(_p=_stale):
                        try:
                            from app.elevation import remove_path_exclusion
                            _ok, _msg = remove_path_exclusion(_p)
                            if not _ok and "administrator" not in (_msg or "").lower():
                                self._set_elevate_buttons_state("normal")
                        except Exception:
                            pass
                        finally:
                            # Ask once only, either way. Re-asking on every
                            # launch for something the user declined would be
                            # nagging, and this is a security setting they may
                            # legitimately want to keep.
                            def _mark(c):
                                c["defender_migration_notice_shown"] = True
                            try:
                                update_config(_mark)
                            except Exception:
                                pass

                    tk.Button(
                        wrapper, text="Remove the folder exclusion",
                        font=(F, 9, "bold"), bg=COLORS["surface"],
                        fg=COLORS["text"], activebackground=COLORS["surface_hover"],
                        activeforeground=COLORS["text"], relief="flat", bd=0,
                        cursor="hand2", command=_do_remove).pack(pady=(0, 4))
        except Exception:
            pass

    def _save_gate_prefs(self):
        try:
            from app import elevated_launch as _el
            _el.save_gate_prefs(
                bool(self._auto_elevate_var.get()),
                bool(self._remember_limited_var.get()),
                bool(self._defender_excl_var.get()))
        except Exception:
            pass

    def _restart_elevated(self):
        self._save_gate_prefs()
        if relaunch_as_admin():
            self._set_elevate_buttons_state("disabled")
            self._wait_status = tk.Label(self, text="Waiting for elevation — click Yes on the UAC prompt, then wait...", font=(F, 9),
                                         bg=COLORS["bg"], fg=COLORS["accent_blue"])
            self._wait_status.pack(pady=(10, 0))

            # BUG-001: the hand-back below used to be
            # `self.root.after(0, _on_done)` from inside this worker thread --
            # a Tk call on a foreign thread, which Python 3.14 rejects
            # outright. The gate has no Application to borrow a dispatcher
            # from (it is built before one exists), so it owns a short-lived
            # one for the duration of this wait and stops it in _on_done.
            _disp = TkDispatcher(self.root)
            _disp.start()

            def _wait_thread():
                from app.elevation import wait_for_elevated_process
                success = wait_for_elevated_process(timeout=60.0)

                def _on_done():
                    # the hand-back is arriving; this pump has done its job
                    try:
                        _disp.stop()
                    except Exception:
                        pass
                    if success:
                        try:
                            self.root.destroy()
                        except Exception:
                            pass
                        sys.exit(0)
                    else:
                        try:
                            if hasattr(self, "_wait_status") and self._wait_status.winfo_exists():
                                self._wait_status.destroy()
                        except Exception:
                            pass
                        self._set_elevate_buttons_state("normal")
                        # Don't auto-open limited mode here: the elevated
                        # window is often already running (old bug created
                        # two main windows). Let the user decide.
                        try:
                            messagebox.showwarning("Elevation Cancelled",
                                                   "Administrator elevation was cancelled or timed out.\n\n"
                                                   "If an elevated window opened, use that one and close this.\n"
                                                   "Otherwise click 'Continue Without Admin' to proceed in limited mode.")
                        except Exception:
                            pass

                try:
                    _disp.post(_on_done)
                except Exception:
                    pass

            threading.Thread(target=_wait_thread, daemon=True).start()
        else:
            messagebox.showerror("Elevation Failed",
                                 "Could not request administrator rights. You can still continue in limited mode.")

    def _set_elevate_buttons_state(self, state: str):
        try:
            for wrapper in self.winfo_children():
                for child in wrapper.winfo_children():
                    if isinstance(child, tk.Frame):
                        for btn in child.winfo_children():
                            if isinstance(btn, AnimatedButton):
                                btn.set_enabled(state == "normal")
        except Exception:
            pass

    def _continue_limited(self):
        self._save_gate_prefs()
        self.destroy()
        self.on_continue_limited()
