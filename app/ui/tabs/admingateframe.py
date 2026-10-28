"""AdminGateFrame — extracted verbatim from app/gui.py (H13).

A feature tab page. May use the widget set and the modal base, and
opens dialogs, but never imports another tab.
"""
from __future__ import annotations

import sys
import threading

import tkinter as tk

from app.ui.widgets.checkbox import M3Checkbutton
from app.elevation import relaunch_as_admin
from app.ui.theme import COLORS, EMOJI, TYPE, SPACE_1, SPACE_2, SPACE_3
from app.ui.metrics import CONTROL_GAP, GROUP_GAP, PAGE_INSET, SECTION_BREAK
from app.ui.widgets import AnimatedButton
from app.ui.tkdispatch import TkDispatcher
from app.ui.base import _themed_showerror, _themed_showinfo, _themed_showwarning


class AdminGateFrame(tk.Frame):
    def __init__(self, root, on_continue_limited):
        super().__init__(root, bg=COLORS["bg"])
        self.root = root
        self.on_continue_limited = on_continue_limited
        # SEC-004: the folder the one-time migration notice offers to remove,
        # or "" when no notice was shown. Set only when the notice is actually
        # built, so dismissing a gate that offered nothing records nothing.
        self._stale_folder_notice = ""
        # The offer's two widgets, so an answered offer can be taken down.
        # `_stale_folder_notice` stays set after a successful removal: it is
        # the record that this gate showed the notice at all, which
        # _mark_defender_notice_seen keys off. Only the widgets go.
        self._stale_notice_label = None
        self._stale_remove_button = None
        self.pack(fill="both", expand=True)
        wrapper = tk.Frame(self, bg=COLORS["bg"])
        wrapper.place(relx=0.5, rely=0.5, anchor="center")
        tk.Label(wrapper, text="🛡️", font=EMOJI["hero"], bg=COLORS["bg"],
                 fg=COLORS["accent_yellow"]).pack(pady=(0, GROUP_GAP))
        tk.Label(wrapper, text="Admin Needed", font=TYPE["readout_bold"],
                 bg=COLORS["bg"], fg=COLORS["text"]).pack()
        tk.Label(
            wrapper,
            text="Repairs and tweaks need admin.",
            font=TYPE["body_medium"], bg=COLORS["bg"], fg=COLORS["subtext"], justify="center",
        ).pack(pady=(SPACE_2, PAGE_INSET))
        btn_row = tk.Frame(wrapper, bg=COLORS["bg"])
        btn_row.pack()
        AnimatedButton(
            btn_row, text="Restart as Admin", command=self._restart_elevated,
            role="filled", size="md", accent=COLORS["accent_green"],
        ).pack(side="left", padx=CONTROL_GAP)
        AnimatedButton(
            btn_row, text="Skip", command=self._continue_limited,
            role="secondary", size="md",
        ).pack(side="left", padx=CONTROL_GAP)
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
                tk.Label(wrapper, text=_notice, font=TYPE["caption"],
                         bg=COLORS["bg"], fg=COLORS["accent_yellow"],
                         wraplength=420, justify="center").pack(pady=(SPACE_3, 0))
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
                M3Checkbutton(wrapper, text=_text, variable=_var,
                               font=TYPE["caption"], bg=COLORS["bg"], fg=COLORS["subtext"],
                               selectcolor=COLORS["surface"],
                               activebackground=COLORS["bg"],
                               activeforeground=COLORS["text"],
                               anchor="w", justify="left",
                               wraplength=420).pack(fill="x", pady=_pady,
                                                    padx=SECTION_BREAK)
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
                    self._stale_folder_notice = _stale
                    self._stale_notice_label = tk.Label(
                        wrapper,
                        text=("An older version of Cleaner Tool excluded this whole "
                              "folder from Defender:\n%s\n\nNothing else in it is "
                              "scanned. Remove that exclusion?" % _stale),
                        font=TYPE["caption"], bg=COLORS["bg"], fg=COLORS["accent_yellow"],
                        wraplength=420, justify="left")
                    self._stale_notice_label.pack(pady=(SPACE_3, SPACE_1), padx=SECTION_BREAK)

                    def _do_remove(_p=_stale):
                        # One click at a time. AnimatedButton defers the
                        # command by 60 ms, so two releases inside that window
                        # queue two _fire calls and would run the removal (and
                        # report it) twice. Disabling here is checked by _fire
                        # before it runs the command, so the second queued call
                        # becomes a no-op instead of a second PowerShell run.
                        self._set_remove_button_enabled(False)
                        _ok, _msg = False, ""
                        try:
                            from app.elevation import remove_path_exclusion
                            _ok, _msg = remove_path_exclusion(_p)
                            if not _ok and "administrator" not in (_msg or "").lower():
                                self._set_elevate_buttons_state("normal")
                        except Exception as exc:
                            # Nothing used to reach the user from here: the
                            # result was dropped and this swallowed too, so the
                            # notice sat on screen looking unanswered while the
                            # folder kept its exclusion. There is no return
                            # value to report, so report the exception.
                            _msg = "%s: %s" % (type(exc).__name__, exc)
                            try:
                                from app.utils import log_or_swallow
                                log_or_swallow(
                                    None, "could not remove the Defender folder "
                                    "exclusion", exc)
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
                        if _ok:
                            self._clear_stale_notice_offer()
                        else:
                            # The offer is still open (the folder is still
                            # excluded), so the button has to work again for
                            # the retry.
                            self._set_remove_button_enabled(True)
                        self._report_removal_result(_p, _ok, _msg)

                    self._stale_remove_button = AnimatedButton(
                        wrapper, text="Remove the folder exclusion",
                        command=_do_remove, role="secondary", size="md")
                    self._stale_remove_button.pack(pady=(0, SPACE_1))
        except Exception:
            pass

    def _set_remove_button_enabled(self, enabled: bool) -> None:
        """Enable/disable the offer's button; a button already gone is a no-op.

        Disabled rather than hidden while the removal runs: the button is the
        only thing on the gate that says what is happening, and the removal
        blocks the UI for the length of a PowerShell call, so a blank space
        would read as a glitch.
        """
        button = getattr(self, "_stale_remove_button", None)
        if button is None:
            return
        try:
            button.set_enabled(enabled)
        except Exception as exc:
            try:
                from app.utils import log_or_swallow
                log_or_swallow(None, "could not change the state of the "
                               "Defender exclusion button", exc)
            except Exception:
                pass

    def _clear_stale_notice_offer(self) -> None:
        """Take the answered offer off the gate, once Defender no longer
        excludes the folder.

        The confirmation said the exclusion was gone, but the label underneath
        still said nothing in that folder was scanned and the button still
        invited a click: the two statements contradicted each other, and a
        second click ran Remove-MpPreference (and confirmed) all over again.
        With the offer destroyed the gate shows only what is still true. The
        widgets are dropped even if destroying one fails, so a stuck label
        cannot make the gate claim an offer it can no longer honour; the
        failure is logged rather than raised, because a stale label is not
        worth losing the confirmation over.
        """
        for attr in ("_stale_notice_label", "_stale_remove_button"):
            widget = getattr(self, attr, None)
            setattr(self, attr, None)
            if widget is None:
                continue
            try:
                widget.destroy()
            except Exception as exc:
                try:
                    from app.utils import log_or_swallow
                    log_or_swallow(None, "could not clear the answered Defender "
                                   "folder exclusion offer", exc)
                except Exception:
                    pass

    def _report_removal_result(self, path: str, ok: bool, msg: str) -> None:
        """SEC-004: say what happened to the folder exclusion, either way.

        `remove_path_exclusion` returns (ok, message), and the gate used to
        throw the message away: a policy-blocked `Remove-MpPreference` or an
        unelevated click produced no dialog, no log line and no visible change,
        so a failed removal looked exactly like a successful one. Success gets
        a confirmation for the same reason — a security setting that silently
        stayed, or silently went, leaves the user guessing, and this is the one
        click where guessing about Defender is expensive.

        The notice still counts as answered (it is not shown again) either way.
        """
        if ok:
            self._show_result_dialog(
                _themed_showinfo, "Exclusion Removed",
                "The Defender exclusion for:\n%s\n\n"
                "Everything in that folder is scanned again." % path)
            return
        reason = (msg or "").strip()
        if "administrator" in reason.lower():
            detail = ("Cleaner Tool is not running as administrator, so it cannot "
                      "change Defender's settings.\n\nRestart as Admin and try "
                      "again, or remove the exclusion by hand in Windows Security.")
        else:
            detail = ("Everything in that folder is still not being "
                      "scanned.\n\n%s"
                      % (reason
                         or "The Defender command failed without saying why."))
        self._show_result_dialog(
            _themed_showwarning, "Exclusion Still In Place",
            "The Defender exclusion for:\n%s\n\n%s" % (path, detail))

    def _show_result_dialog(self, dialog, title: str, message: str) -> None:
        """Best-effort modal. The dialog failing must not take the gate down,
        but it is the only channel left for this answer, so it cannot vanish."""
        try:
            dialog(self, title, message)
        except Exception as exc:
            try:
                from app.utils import log_or_swallow
                log_or_swallow(None, "could not show the Defender exclusion "
                               "notice", exc)
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
        self._mark_defender_notice_seen()

    def _mark_defender_notice_seen(self):
        """SEC-004: the one-time folder notice has had its answer.

        Both gate buttons call this, because dismissing the gate IS the answer
        when the notice is on screen and the user does not click Remove: there
        is no "keep it" button, so Skip is the decline. Marking only inside
        _do_remove left the flag clear for exactly that user, and the notice
        came back on every launch - the nag the code means to avoid. A gate
        that showed no notice marks nothing, so a stale folder that appears
        later (the app copied into an old excluded folder) is still offered.
        """
        if not getattr(self, "_stale_folder_notice", ""):
            return
        try:
            from app.config_persist import update_config

            def _mark(c):
                c["defender_migration_notice_shown"] = True
            update_config(_mark)
        except Exception:
            pass

    def _restart_elevated(self):
        self._save_gate_prefs()
        if relaunch_as_admin():
            self._set_elevate_buttons_state("disabled")
            self._wait_status = tk.Label(self, text="Waiting for elevation — click Yes on the UAC prompt, then wait...", font=TYPE["caption"],
                                         bg=COLORS["bg"], fg=COLORS["accent_blue"])
            self._wait_status.pack(pady=(GROUP_GAP, 0))

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
                            _themed_showwarning(
                                self, "Elevation Cancelled",
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
            _themed_showerror(self, "Elevation Failed",
                              "Nothing has changed. You can still continue in limited mode.")

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
