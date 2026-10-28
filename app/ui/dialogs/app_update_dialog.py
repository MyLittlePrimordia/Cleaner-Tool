"""AppUpdateDialog - fetch and apply a new build, or open the release page.

The banner that says "Update available" opens this instead of a bare
messagebox. It offers the two honest paths and never pretends one worked:

    Update automatically   download, verify against the published SHA-256,
                           and swap the exe in on restart (see
                           app/self_update.py). Only offered when the app is a
                           frozen build in a writable folder; otherwise the
                           reason is printed and the button stays off.
    Open download page     the original browser hand-off, always available.

If a verified build is already staged, the dialog opens in a "ready" state and
the primary button becomes "Restart to finish" -- it does not download twice.
"""
from __future__ import annotations

import tkinter as tk

from app.ui.dialogs._tool_page import ToolPage
from app.ui.theme import COLORS, RADIUS_SURFACE, TYPE, SPACE_3
from app.ui.metrics import COMPACT_INSET, GROUP_GAP, ROW_PAD, TEXT_NUDGE
from app.ui.widgets import RoundedCard

_NOTES_CAP = 1500


class AppUpdateDialog(ToolPage):
    def __init__(self, parent, app):
        from app import self_update
        self.app = app
        self._repo = self_update.UPDATE_REPO
        self._update = dict(getattr(app, "_update_info", None) or {})
        self._busy = False
        self._state = "offer"
        self._stop_token = [False]

        super().__init__(
            parent, title="Update",
            scope="Downloads the new build, checks it against the published "
                  "SHA-256, and installs it when you restart. Read-only until "
                  "you choose to update.",
            scroll=True)

        self._auto_btn = self.add_button("Update automatically",
                                         self._on_primary, kind="primary")
        self._browser_btn = self.add_button("Open download page",
                                            self._open_browser)
        self._close_btn = self.add_button("Close", self.close)
        self.on_close(self._stop)
        self._render()

    # ------------------------------------------------------------- lifecycle
    def _stop(self):
        self._stop_token[0] = True

    def set_busy(self, busy):
        for b in (self._auto_btn, self._browser_btn, self._close_btn):
            try:
                b.set_enabled(not busy)
            except Exception:
                pass

    def _clear(self):
        for w in list(self.content.winfo_children()):
            w.destroy()

    def _section(self, title):
        card = RoundedCard(self.content, fill=COLORS["surface"],
                           bg=COLORS["bg"], radius=RADIUS_SURFACE)
        card.pack(fill="x", padx=TEXT_NUDGE, pady=COMPACT_INSET)
        tk.Label(card.body, text=title, font=TYPE["label_large"],
                 bg=COLORS["surface"], fg=COLORS["text"], anchor="w").pack(
            fill="x", padx=SPACE_3, pady=(ROW_PAD, TEXT_NUDGE))
        return card.body

    # ---------------------------------------------------------------- render
    def _render(self):
        from app import self_update

        self._clear()
        latest = self._update.get("latest_version") or "a new build"
        current = self._update.get("current_version") or "?"
        staged = self_update.staged_ready()
        if staged:
            self._state = "ready"
        elif self._state != "ready":
            self._state = "offer"

        if self._state == "ready":
            self.banner.set_state("\u2713", COLORS["accent_green"])
            self.banner.set_title("Ready to install %s." % latest,
                                  COLORS["accent_green"])
            self.banner.set_sub("The new build is downloaded and verified. "
                                "Restart to finish.")
        else:
            self.banner.set_state("\U0001F504", COLORS["accent_yellow"])
            self.banner.set_title("Version %s is available." % latest,
                                  COLORS["accent_yellow"])
            self.banner.set_sub("You have %s." % current)

        # What's new
        notes = (self._update.get("release_notes") or "").strip()
        if notes:
            if len(notes) > _NOTES_CAP:
                notes = notes[:_NOTES_CAP].rstrip() + "\u2026"
            body = self._section("What's new")
            tk.Label(body, text=notes, font=TYPE["caption"], bg=COLORS["surface"],
                     fg=COLORS["text"], justify="left", anchor="w",
                     wraplength=520).pack(fill="x", padx=SPACE_3, pady=(0, GROUP_GAP))

        # How the automatic path works (or why it is off)
        ok, why = self_update.can_self_update()
        body = self._section("Update automatically")
        if ok:
            text = ("This build can update itself: it downloads the new "
                    "version, checks it against the SHA-256 published with the "
                    "release, and replaces the app when you restart. If the "
                    "check or the swap fails, nothing is changed.")
        else:
            text = ("Automatic updates are not available here. %s" % why)
        tk.Label(body, text=text, font=TYPE["caption"], bg=COLORS["surface"],
                 fg=COLORS["text"] if ok else COLORS["subtext"],
                 justify="left", anchor="w", wraplength=520).pack(
            fill="x", padx=SPACE_3, pady=(0, GROUP_GAP))

        # Buttons
        try:
            if self._state == "ready":
                self._auto_btn.config_text("Restart to finish")
                self._auto_btn.set_enabled(not self._busy)
            else:
                self._auto_btn.config_text("Update automatically")
                self._auto_btn.set_enabled(bool(ok) and not self._busy)
            self._browser_btn.set_enabled(not self._busy)
            self._close_btn.set_enabled(not self._busy)
        except Exception:
            pass

    # --------------------------------------------------------------- actions
    def _on_primary(self):
        if self._busy:
            return
        if self._state == "ready":
            self._restart_now()
        else:
            self._start_auto()

    def _prepare(self):
        """Worker body: fetch the release, resolve its checksum, stage the
        build. Returns {"ok": bool, "error": str}; never raises (run_worker
        also guards, but this is the whole point of the method)."""
        from app import self_update
        from app.update_checker import fetch_latest_release_info

        repo, token = self._repo, self._stop_token
        # Re-fetch the release rather than trusting the cached banner dict:
        # check_for_updates validated the download URL but its result carries
        # no asset list, and resolving the SHA-256 needs one. Fetching fresh
        # also handles the release changing between the banner and the click.
        release = fetch_latest_release_info(repo, timeout=20)
        if not release:
            return {"ok": False, "error": (
                "Could not reach the release page to confirm the new build. "
                "Try again, or use the download page.")}
        resolved = self_update.resolve_update(release, repo, timeout=20)
        if not resolved:
            return {"ok": False, "error": (
                "The release did not publish a checksum to verify the "
                "download against, so the app will not update itself. "
                "Use the download page.")}

        def prog(got, total):
            if total:
                self._ui(self.status.set_text,
                         "Downloading the new build... %d%%"
                         % int(got * 100 / total))
            else:
                self._ui(self.status.set_text, "Downloading the new build...")

        ok, msg = self_update.stage_download(
            resolved["url"], resolved["sha256"], resolved.get("size", 0),
            on_progress=prog, cancelled=lambda: token[0])
        return {"ok": ok, "error": msg,
                "cancelled": (not ok and self_update.is_cancellation(msg))}

    def _start_auto(self):
        from app import self_update

        ok, why = self_update.can_self_update()
        if not ok:
            self.status.stop(why, COLORS["accent_red"])
            return
        self._busy = True
        self.set_busy(True)
        self._stop_token[0] = False
        self.status.start("Preparing the update...")
        self.run_worker(self._prepare, self._land_auto)

    def _land_auto(self, res):
        self._busy = False
        self.set_busy(False)
        if not res.get("ok"):
            if res.get("cancelled"):
                # B-004: a Stop is not a failure - it is not red, it does not
                # blame the network, and the offer is left exactly as it was.
                self.status.stop("Update cancelled \u2014 nothing was changed.",
                                 COLORS["subtext"])
            else:
                self.status.stop(res.get("error") or "The update could not be "
                                 "prepared.", COLORS["accent_red"])
            self._render()
            return
        self._state = "ready"
        self.status.stop("Downloaded and verified.", COLORS["accent_green"])
        self._render()

    def _restart_now(self):
        from app import self_update

        staged = self_update.staged_ready()
        if not staged:
            self.status.stop("The update is no longer ready \u2014 try again.",
                             COLORS["accent_red"])
            self._state = "offer"
            self._render()
            return
        ok, msg = self_update.schedule_swap(self_update.current_exe(), staged,
                                            relaunch=True)
        if not ok:
            self.status.stop(msg, COLORS["accent_red"])
            return
        try:
            self.app.log("Update ready \u2014 restarting to finish "
                         "installing it.")
        except Exception:
            pass
        # Close this modal first, then quit for real so the swap helper can
        # replace the exe (it relaunches the new build).
        self.close()
        try:
            self.app.root.after(80,
                                lambda: self.app._on_close(force_quit=True))
        except Exception:
            pass

    def _open_browser(self):
        from app.update_checker import _trusted_release_url

        url = self._update.get("download_url") or ""
        safe = _trusted_release_url(url, self._repo)
        if not safe:
            self.status.stop("The release did not offer a safe download link; "
                             "nothing was opened.", COLORS["accent_red"])
            return
        try:
            import webbrowser
            webbrowser.open(safe)
            self.status.stop("Opened the download page in your browser.",
                             COLORS["accent_green"])
        except Exception as exc:
            self.status.stop("Could not open the browser: %s" % exc,
                             COLORS["accent_red"])
