"""UninstallProgramsDialog — extracted verbatim from app/gui.py (H12, batch 4).

One dialog per module, mirroring app/ui/widgets. Depends only on
app.ui below it, plus the app.utils helpers it already used; it
knows nothing
about any other dialog except where noted.
"""
from __future__ import annotations

import os

import tkinter as tk
from tkinter import ttk, messagebox
from app.ui.base import ThemedModal, _themed_askyesno
from app.ui.theme import COLORS, F, TAB_ACCENTS, ICON_ROW_PX, icon_bg_rgb
from app.ui.widgets import AnimatedButton, RoundedEntry, ScrollableRoundedPanel
from app.ui.tkdispatch import TkDispatcher

# Icons are composited onto an opaque plate, so that plate must be the
# exact row background or the icon shows a square edge. This file's rows
# are COLORS["bg"] - it used to hardcode (30, 32, 36), which matched no
# theme color at all, which is why every program icon had a border.
_icon_bg = icon_bg_rgb(COLORS["bg"])


class UninstallProgramsDialog(ThemedModal):
    """Uninstall Programs (Tools tab card): list installed apps,
    run each program's official uninstaller, then optionally scan for
    leftover folders for the chosen name only.

    Safety model matches the rest of the app:
      * Official UninstallString / QuietUninstallString only — never a
        blind force-delete of Program Files trees.
      * Leftover scan is name-scoped and high-confidence (same residual
        heuristics as the Clean-tab orphan task).
      * User reviews leftover candidates with checkboxes before delete.
      * Elevation is requested only when the selected uninstall needs it.
    """

    def __init__(self, parent, app):
        self.app = app
        self._programs = []       # full list from list_installed_programs
        self._shown = []          # filtered view
        self._checked = {}        # program id -> BooleanVar (multi-select)
        self._leftover_vars = {}  # path (or REGKEY::/STARTUP::/SHORTCUT:: pseudo-path) -> BooleanVar
        self._icon_cache = {}     # DisplayIcon spec -> tk.PhotoImage (survives rebuilds)
        self._icon_waiters = {}   # DisplayIcon spec -> [Label, ...] awaiting extraction
        self._icon_default_img = None  # lazily built placeholder PhotoImage
        super().__init__(parent, title="Uninstall Programs",
                         accent=TAB_ACCENTS.get("Clean", COLORS["accent_green"]))
        body = self.body

        # One-way bridge onto the Tk thread (created after the base dialog
        # exists, so self._dlg is available).
        #
        # The scan result AND every icon swap used to be posted with
        # `self._dlg.after(0, ...)` FROM A WORKER THREAD. That is the
        # documented cross-thread Tcl race: the callback is dropped outright
        # when the main thread is not inside the event loop at that instant.
        # Measured on this dialog headlessly — the scan callback never arrived
        # at all and the program list stayed empty through 15s of pumping.
        # Users usually do see the list, which is exactly why this survived:
        # it is intermittent, so it reads as "the list takes a moment".
        # See app/ui/tkdispatch.py.
        self._dispatch = TkDispatcher(self._dlg)
        self._dispatch.start()

        tk.Label(body, text="Uninstall a program the official way, then "
                            "optionally clean leftovers it left behind.",
                 font=(F, 10, "bold"), bg=COLORS["bg"], fg=COLORS["text"],
                 anchor="w").pack(fill="x", pady=(0, 2))
        tk.Label(body, text="Only the program's own uninstaller runs. "
                            "Leftover folders are shown for review — nothing "
                            "is force-deleted without your say-so.",
                 font=(F, 9), bg=COLORS["bg"], fg=COLORS["subtext"],
                 anchor="w", wraplength=720).pack(fill="x", pady=(0, 8))

        # Search
        self._search_var = tk.StringVar()
        self._search_var.trace_add("write", lambda *_: self._apply_filter())
        entry = RoundedEntry(body, textvariable=self._search_var)
        entry.pack(fill="x", pady=(0, 6))
        self._search_entry = entry.entry
        self._search_placeholder = "Type to filter…"
        self._search_entry.insert(0, self._search_placeholder)
        self._search_focused = False
        self._search_entry.bind("<FocusIn>", self._filter_focus_in)
        self._search_entry.bind("<FocusOut>", self._filter_focus_out)

        # Program list
        self._list_panel = ScrollableRoundedPanel(body)
        self._list_panel.config(height=280)
        self._list_panel.pack(fill="both", expand=True, pady=(0, 6))
        self._list_body = self._list_panel.inner

        # Status + actions
        brow = tk.Frame(body, bg=COLORS["bg"])
        brow.pack(fill="x", pady=(4, 0))
        self._status = tk.Label(brow, text="Scanning…", font=(F, 9),
                                bg=COLORS["bg"], fg=COLORS["subtext"],
                                wraplength=420, justify="left", anchor="w")
        self._status.pack(side="left", fill="x", expand=True)
        self._uninstall_btn = AnimatedButton(
            brow, text="Uninstall Selected", command=self._do_uninstall,
            bg=TAB_ACCENTS.get("Clean", COLORS["accent_green"]),
            fg=COLORS["black"], font=(F, 9, "bold"), padx=14, pady=6)
        self._uninstall_btn.pack(side="right", padx=(6, 0))
        self._uninstall_btn.set_enabled(False)
        self._select_all_var = tk.BooleanVar(value=False)
        tk.Checkbutton(
            brow, text="Select all", variable=self._select_all_var,
            command=self._on_select_all, font=(F, 9), bg=COLORS["bg"],
            fg=COLORS["text"], selectcolor=COLORS["surface"],
            activebackground=COLORS["bg"], activeforeground=COLORS["text"]
        ).pack(side="right", padx=(6, 0))
        AnimatedButton(brow, text="Refresh", command=self._refresh,
                       bg=COLORS["surface"], fg=COLORS["text"],
                       font=(F, 9, "bold"), padx=12, pady=6).pack(
                           side="right", padx=(6, 0))
        AnimatedButton(brow, text="Close", command=self.close,
                       bg=COLORS["surface"], fg=COLORS["text"],
                       font=(F, 9, "bold"), padx=12, pady=6).pack(
                           side="right")

        # Leftover review panel (hidden until after uninstall)
        self._leftover_frame = tk.Frame(body, bg=COLORS["bg"])
        # packed on demand

        # Async enumerate so the dialog never freezes on open
        self._scan_token = [False]
        self._start_scan()

    # ---- search placeholder helpers (same pattern as CatalogPicker) ---- #

    def _filter_focus_in(self, _e=None):
        self._search_focused = True
        if self._search_entry.get() == self._search_placeholder:
            self._search_entry.delete(0, "end")
            self._search_entry.config(fg=COLORS["text"])

    def _filter_focus_out(self, _e=None):
        self._search_focused = False
        if not self._search_entry.get().strip():
            self._search_entry.delete(0, "end")
            self._search_entry.insert(0, self._search_placeholder)
            self._search_entry.config(fg=COLORS["subtext"])

    def _query(self):
        q = self._search_var.get().strip()
        if q == self._search_placeholder:
            return ""
        return q.lower()

    def _apply_filter(self):
        if not hasattr(self, "_list_body"):
            return   # trace can fire while RoundedEntry wires up the
                     # variable, before the list panel exists yet
        q = self._query()
        if not q:
            self._shown = list(self._programs)
        else:
            self._shown = [
                p for p in self._programs
                if q in p["name"].lower()
                or q in (p.get("publisher") or "").lower()
            ]
        self._rebuild_list()

    def _start_scan(self):
        self._status.config(text="Scanning installed programs…")
        token = self._scan_token
        token[0] = False

        def work():
            try:
                from app.game_catalog import list_installed_programs
                programs = list_installed_programs()
            except Exception:
                programs = []
            if token[0]:
                return
            self._dispatch.post(lambda p=programs: self._on_scan_done(p))

        import threading
        threading.Thread(target=work, daemon=True).start()

    def _on_scan_done(self, programs):
        if self._scan_token[0]:
            return
        self._programs = programs or []
        self._shown = list(self._programs)
        self._status.config(
            text=f"{len(self._programs)} program(s) found. Select one to uninstall.")
        self._rebuild_list()

    def _refresh(self):
        self._checked = {}
        self._select_all_var.set(False)
        self._uninstall_btn.set_enabled(False)
        self._clear_leftover_panel()
        self._start_scan()

    # ---- multi-select helpers ------------------------------------------ #

    @staticmethod
    def _program_id(prog):
        """Stable id for a program dict — key_path+hive when we have a
        registry location (the common case), else fall back to the name
        (still unique: list_installed_programs() dedups by DisplayName)."""
        hive = prog.get("hive") or ""
        key_path = prog.get("key_path") or ""
        if hive and key_path:
            return f"{hive}|{key_path}"
        return f"name|{(prog.get('name') or '').lower()}"

    def _is_checked(self, prog):
        var = self._checked.get(self._program_id(prog))
        return bool(var and var.get())

    def _checked_count(self):
        return sum(1 for v in self._checked.values() if v.get())

    def _update_uninstall_btn(self):
        n = self._checked_count()
        self._uninstall_btn.config_text(
            f"Uninstall Selected ({n})" if n else "Uninstall Selected")
        self._uninstall_btn.set_enabled(n > 0)

    def _on_select_all(self):
        want = self._select_all_var.get()
        for prog in self._shown:
            pid = self._program_id(prog)
            var = self._checked.get(pid)
            if var is None:
                var = tk.BooleanVar(value=False)
                self._checked[pid] = var
            var.set(want)
        self._rebuild_list()
        self._update_uninstall_btn()

    def _toggle_row(self, prog):
        pid = self._program_id(prog)
        var = self._checked.get(pid)
        if var is None:
            var = tk.BooleanVar(value=False)
            self._checked[pid] = var
        var.set(not var.get())
        self._update_uninstall_btn()

    def _rebuild_list(self):
        for child in self._list_body.winfo_children():
            try:
                child.destroy()
            except Exception:
                pass
        if not self._shown:
            tk.Label(self._list_body,
                     text="No matching programs.",
                     font=(F, 10), bg=COLORS["bg"], fg=COLORS["subtext"],
                     anchor="w").pack(fill="x", padx=8, pady=12)
            try:
                self._list_panel.refresh_scroll()
            except Exception:
                pass
            return
        for prog in self._shown:
            self._add_row(prog)
        try:
            self._list_panel.refresh_scroll()
        except Exception:
            pass

    def _add_row(self, prog):
        pid = self._program_id(prog)
        var = self._checked.get(pid)
        if var is None:
            var = tk.BooleanVar(value=False)
            self._checked[pid] = var

        row = tk.Frame(self._list_body, bg=COLORS["bg"], cursor="hand2")
        row.pack(fill="x", padx=4, pady=1)

        cb = tk.Checkbutton(
            row, variable=var, command=lambda p=prog: self._update_uninstall_btn(),
            bg=COLORS["bg"], activebackground=COLORS["bg"],
            selectcolor=COLORS["surface"])
        cb.pack(side="left", padx=(2, 2), pady=2)

        icon_lbl = tk.Label(row, bg=COLORS["bg"])
        icon_lbl.pack(side="left", padx=(0, 6), pady=2)
        self._set_row_icon(icon_lbl, prog)

        name = prog.get("name") or "(unknown)"
        publisher = prog.get("publisher") or ""
        size_kb = prog.get("size_kb") or 0
        size_txt = ""
        if size_kb >= 1024:
            size_txt = f"  ·  {size_kb / 1024:.1f} MB"
        elif size_kb > 0:
            size_txt = f"  ·  {size_kb} KB"
        left = f"{name}"
        if publisher:
            left += f"  —  {publisher}"
        left += size_txt
        lbl = tk.Label(row, text=left, font=(F, 9), bg=COLORS["bg"],
                       fg=COLORS["text"], anchor="w", justify="left")
        lbl.pack(side="left", fill="x", expand=True, padx=6, pady=4)

        # clicking anywhere on the row (not just the checkbox hitbox)
        # toggles selection, same reachable-target size the rest of the
        # app uses for clickable rows
        def _pick(_e=None, p=prog):
            self._toggle_row(p)
        row.bind("<Button-1>", _pick)
        icon_lbl.bind("<Button-1>", _pick)
        lbl.bind("<Button-1>", _pick)
        row._prog = prog

    def _icon_default_photo(self):
        if self._icon_default_img is None:
            try:
                # Was a flat grey swatch, which reads as "this row is broken"
                # rather than "this program ships no icon". icon_resolve draws
                # a small application-window glyph instead, so the handful of
                # console/JVM binaries with no icon resource look deliberate.
                from app.icon_resolve import generic_app_icon_ppm
                _ppm = generic_app_icon_ppm(ICON_ROW_PX, _icon_bg)
                if _ppm is None:
                    from app.icon_extract import default_icon_ppm
                    _ppm = default_icon_ppm(ICON_ROW_PX, _icon_bg)
                self._icon_default_img = tk.PhotoImage(data=_ppm)
            except Exception:
                self._icon_default_img = None
        return self._icon_default_img

    def _set_row_icon(self, label, prog):
        """Show a placeholder immediately; if this program has a
        DisplayIcon, extract the real one on a background thread (GDI
        calls are pure computation, no Tk involved) and swap it in once
        ready. Cached by icon spec so re-filtering never re-extracts the
        same icon twice, and an in-flight guard stops duplicate threads
        firing on every keystroke while a search filters the list."""
        placeholder = self._icon_default_photo()
        if placeholder is not None:
            label.config(image=placeholder)
            label.image = placeholder

        icon_spec = prog.get("icon") or ""
        if not icon_spec:
            return
        cached = self._icon_cache.get(icon_spec)
        if cached is not None:
            label.config(image=cached)
            label.image = cached
            return
        # Register this label as waiting for icon_spec. If a rebuild (e.g.
        # a search keystroke) destroys this row before extraction lands,
        # the widget-existence check in apply() below skips just that
        # stale entry — the new row for the same program registers its
        # own label here too, so it still gets the icon once ready.
        waiters = self._icon_waiters.setdefault(icon_spec, [])
        waiters.append(label)
        if len(waiters) > 1:
            return   # extraction already in flight for this icon

        def work():
            ppm = None
            try:
                from app.icon_extract import icon_ppm_bytes
                ppm = icon_ppm_bytes(
                    icon_spec, size=ICON_ROW_PX, bg_rgb=_icon_bg)
            except Exception:
                ppm = None

            def apply():
                pending = self._icon_waiters.pop(icon_spec, [])
                photo = None
                if ppm:
                    try:
                        photo = tk.PhotoImage(data=ppm)
                        self._icon_cache[icon_spec] = photo
                    except Exception:
                        photo = None
                if photo is None:
                    # The spec existed but yielded no pixels — a console or
                    # JVM helper binary with no icon resource. The generic
                    # glyph is already on the label, so there is nothing to
                    # swap; record it so a re-filter does not re-extract.
                    if self._icon_default_img is not None:
                        self._icon_cache.setdefault(
                            icon_spec, self._icon_default_img)
                    return
                for lbl in pending:
                    try:
                        if lbl.winfo_exists():
                            lbl.config(image=photo)
                            lbl.image = photo
                    except Exception:
                        pass
            self._dispatch.post(apply)

        import threading
        threading.Thread(target=work, daemon=True).start()

    def _do_uninstall(self):
        checked = [p for p in self._programs if self._is_checked(p)]
        if not checked:
            return
        names = ", ".join((p.get("name") or "?") for p in checked[:5])
        more = f", and {len(checked) - 5} more" if len(checked) > 5 else ""
        plural = "1 program" if len(checked) == 1 else f"{len(checked)} programs"
        if not _themed_askyesno(
                self._dlg, "Confirm Uninstall",
                f"Uninstall {plural}?\n\n{names}{more}\n\n"
                "A System Restore point is attempted first as a safety "
                "net, then each program's own uninstaller runs in turn. "
                "After they finish you can review any leftovers.",
                yes_text="Uninstall", no_text="Cancel"):
            return
        self._uninstall_btn.set_enabled(False)
        self._queue = list(checked)
        self._queue_total = len(checked)
        self._queue_results = []   # [(prog, rc_or_None, err_str), ...]
        self._create_pre_uninstall_restore_point()

    def _create_pre_uninstall_restore_point(self):
        """Best-effort safety net before a batch that can uninstall
        multiple programs and delete registry/startup/shortcut leftovers.
        Reuses the same create_restore_point() the Repair tab's own
        'Safety Checkpoint' task uses — it never raises, just logs and
        returns False on failure (System Protection off, Windows'
        built-in once-per-~24h throttling, etc.), so this can't block or
        fail the uninstall itself; it only delays starting it by however
        long the checkpoint takes."""
        self._status.config(text="Creating a System Restore point (safety net)…")
        token = self._scan_token

        def work():
            ok = False
            try:
                from app.utils import TaskContext, create_restore_point
                ctx = TaskContext(
                    log=lambda m: None,
                    set_status=lambda m: None,
                    cancelled=lambda: False,
                )
                ok = create_restore_point(
                    ctx, description="Cleaner Tool - before uninstall")
            except Exception:
                ok = False
            if token[0]:
                return
            try:
                self._dlg.after(
                    0, lambda: self._after_restore_point(ok))
            except Exception:
                pass

        import threading
        threading.Thread(target=work, daemon=True).start()

    def _after_restore_point(self, ok):
        self._status.config(
            text="Restore point created." if ok
            else "Restore point skipped (System Protection may be off) — continuing.")
        self._run_next_in_queue()

    def _run_next_in_queue(self):
        if not self._queue:
            self._on_queue_done()
            return
        prog = self._queue.pop(0)
        name = prog.get("name") or "this program"
        step = self._queue_total - len(self._queue)
        self._status.config(
            text=f"Uninstalling {name}… ({step}/{self._queue_total})")
        cmd = prog.get("quiet") or prog.get("uninstall") or ""
        if not cmd:
            self._queue_results.append((prog, None, "No uninstall command found"))
            self._run_next_in_queue()
            return
        token = self._scan_token

        def work():
            rc = -1
            err = ""
            try:
                import subprocess
                import shlex
                # Prefer QuietUninstallString when present; otherwise run
                # the normal UninstallString. Parse to argv and run
                # shell=False so registry metacharacters cannot inject
                # extra commands (SEC-001). Fall closed on malformed strings.
                try:
                    argv = shlex.split(cmd, posix=False)
                except ValueError as ve:
                    err = f"Refusing to run malformed uninstall command: {ve}"
                    argv = None
                if argv:
                    completed = subprocess.run(
                        argv, shell=False, timeout=600,
                        capture_output=True, text=True,
                        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                    rc = completed.returncode
            except Exception as exc:
                err = str(exc)
            if token[0]:
                return
            try:
                self._dlg.after(
                    0, lambda: self._on_item_done(prog, rc, err))
            except Exception:
                pass

        import threading
        threading.Thread(target=work, daemon=True).start()

    def _on_item_done(self, prog, rc, err):
        self._queue_results.append((prog, rc, err))
        self._run_next_in_queue()

    def _on_queue_done(self):
        results = self._queue_results
        n = len(results)
        # Only programs whose uninstaller actually started get a leftover
        # scan — one that failed to launch left nothing new behind.
        started_ok = [p for p, rc, err in results if not err]
        failed = n - len(started_ok)
        self._status.config(text=f"Finished {n} uninstall(s). Scanning leftovers…")

        token = self._scan_token

        # Registry enumeration + filesystem walks per item add up for a
        # multi-item batch — run the whole scan off the Tk thread so the
        # dialog never freezes mid-batch, same async-then-hop-back pattern
        # _start_scan() already uses for the initial program list.
        def work():
            try:
                from app.tasks.clean_tasks import _normalize_product_token
            except Exception:
                def _normalize_product_token(s):
                    return (s or "").lower().strip()

            items = []
            seen_paths = set()

            def _add(it, owner_name):
                if it["path"] in seen_paths:
                    return
                seen_paths.add(it["path"])
                it["label"] = f"{it['label']}  —  {owner_name}"
                items.append(it)

            for prog in started_ok:
                if token[0]:
                    return
                name = prog.get("name") or "program"
                pt = _normalize_product_token(name)
                for folder in self._scan_leftovers_for(
                        name, prog.get("location") or ""):
                    _add({"path": folder, "label": folder, "kind": "folder"}, name)
                ghost = self._check_ghost_registry_key(prog)
                if ghost:
                    _add(ghost, name)
                for it in self._scan_startup_leftovers_for(pt):
                    _add(it, name)
                for it in self._scan_shortcut_leftovers_for(pt):
                    _add(it, name)

            if token[0]:
                return
            try:
                self._dlg.after(
                    0, lambda: self._on_leftover_scan_done(items, n, failed))
            except Exception:
                pass

        import threading
        threading.Thread(target=work, daemon=True).start()

    def _on_leftover_scan_done(self, items, n, failed):
        if not items:
            msg = f"Finished {n} uninstall(s). No high-confidence leftovers found."
            if failed:
                msg += f" ({failed} failed to start.)"
            self._status.config(text=msg)
            self._refresh()
            return
        summary = f"{n} uninstalled program(s)" if n != 1 else "1 program"
        self._show_leftover_review(summary, items)

    def _check_ghost_registry_key(self, prog):
        """If prog's own Uninstall registry key is still present after its
        uninstaller ran, AND list_installed_programs() no longer sees it
        (proof the uninstall actually took, not just that the key opens),
        return a reviewable REGKEY:: item. Never offered for a program
        that a fresh scan still considers installed."""
        key_path = prog.get("key_path") or ""
        hive_label = prog.get("hive") or ""
        if not key_path or hive_label not in ("HKLM", "HKCU"):
            return None
        try:
            import winreg
            root = (winreg.HKEY_LOCAL_MACHINE if hive_label == "HKLM"
                    else winreg.HKEY_CURRENT_USER)
            try:
                k = winreg.OpenKey(root, key_path)
                winreg.CloseKey(k)
            except FileNotFoundError:
                return None   # uninstaller already cleaned its own key
            except OSError:
                return None
        except Exception:
            return None
        try:
            from app.game_catalog import list_installed_programs
            fresh = list_installed_programs()
        except Exception:
            return None
        still_there = any(
            p.get("hive") == hive_label and p.get("key_path") == key_path
            for p in fresh)
        if still_there:
            return None
        leaf = key_path.rsplit("\\", 1)[-1]
        return {
            "path": f"REGKEY::{hive_label}::{key_path}",
            "label": f"Leftover registry entry: {hive_label}\\…\\{leaf}",
            "kind": "regkey",
        }

    @staticmethod
    def _startup_target_missing(cmd):
        """True only when we can confidently say the target no longer
        exists. Any ambiguity (empty command, env var we can't resolve,
        can't stat) returns False — never flag a startup entry we're not
        sure about, since removing one changes what runs at sign-in."""
        cmd = (cmd or "").strip()
        if not cmd:
            return False
        if cmd.startswith('"'):
            end = cmd.find('"', 1)
            path = cmd[1:end] if end != -1 else cmd[1:]
        else:
            path = cmd.split(" ", 1)[0]
        path = os.path.expandvars(path).strip()
        if not path:
            return False
        try:
            return not os.path.exists(path)
        except Exception:
            return False

    def _scan_startup_leftovers_for(self, token):
        """Startup Run-key / Startup-folder entries whose target no longer
        exists and whose name matches the just-uninstalled product. Fully
        reuses app.startup_manager (already-tested enumerate/toggle code,
        same undo-capable removal the Startup Manager dialog uses)."""
        if not token or len(token) < 3:
            return []
        try:
            from app.startup_manager import list_startup_items
            from app.tasks.clean_tasks import _normalize_product_token
        except Exception:
            return []
        hits = []
        for it in list_startup_items():
            if not it.get("enabled"):
                continue   # already disabled/held — nothing to clean
            if not self._startup_target_missing(it.get("command") or ""):
                continue
            nname = _normalize_product_token(it.get("name") or "")
            related = (
                nname == token or token in nname or nname in token
                or (len(set(nname.split()) & set(token.split())) >= 2))
            if not related:
                continue
            hits.append({
                "path": f"STARTUP::{it['id']}",
                "label": f"Startup entry (target missing): {it.get('name')} "
                         f"({it.get('source_label', '')})",
                "kind": "startup",
            })
        return hits

    def _scan_shortcut_leftovers_for(self, token):
        """Desktop / Start Menu .lnk/.url files whose filename matches the
        just-uninstalled product. Name-matched only (no shell-link target
        resolution, same conservative heuristic the folder scan uses) —
        review-only, never auto-deleted."""
        if not token or len(token) < 3:
            return []
        try:
            from app.tasks.clean_tasks import _normalize_product_token
        except Exception:
            return []
        roots = []
        userprofile = os.environ.get("USERPROFILE", "")
        if userprofile:
            roots.append(os.path.join(userprofile, "Desktop"))
        public = os.environ.get("PUBLIC", "")
        if public:
            roots.append(os.path.join(public, "Desktop"))
        appdata = os.environ.get("APPDATA", "")
        if appdata:
            roots.append(os.path.join(
                appdata, "Microsoft", "Windows", "Start Menu", "Programs"))
        programdata = os.environ.get("PROGRAMDATA", "")
        if programdata:
            roots.append(os.path.join(
                programdata, "Microsoft", "Windows", "Start Menu", "Programs"))

        hits = []
        seen = set()
        for root in roots:
            if not root or not os.path.isdir(root):
                continue
            try:
                entries = os.listdir(root)
            except OSError:
                continue
            for entry in entries:
                low = entry.lower()
                if not (low.endswith(".lnk") or low.endswith(".url")):
                    continue
                stem = os.path.splitext(entry)[0]
                nname = _normalize_product_token(stem)
                related = (
                    nname == token or token in nname or nname in token
                    or (len(set(nname.split()) & set(token.split())) >= 2))
                if not related:
                    continue
                full = os.path.join(root, entry)
                if full in seen:
                    continue
                seen.add(full)
                hits.append({
                    "path": f"SHORTCUT::{full}",
                    "label": f"Shortcut: {full}",
                    "kind": "shortcut",
                })
        return hits

    def _scan_leftovers_for(self, display_name, install_location):
        """Name-scoped leftover scan. Reuses orphan residual heuristics."""
        try:
            from app.tasks.clean_tasks import (
                _orphan_scan_roots, _looks_like_residual_folder,
                _normalize_product_token, _ORPHAN_DENY_NAMES,
            )
        except Exception:
            return []
        token = _normalize_product_token(display_name)
        if not token or len(token) < 3:
            return []
        hits = []
        # Always include InstallLocation if it still exists and looks residual
        if install_location and os.path.isdir(install_location):
            base = os.path.basename(install_location.rstrip("\\/"))
            if (base.lower() not in _ORPHAN_DENY_NAMES
                    and _looks_like_residual_folder(install_location)):
                hits.append(install_location)
        roots = _orphan_scan_roots()
        for root in roots:
            try:
                children = os.listdir(root)
            except OSError:
                continue
            for name in children:
                folder = os.path.join(root, name)
                if not os.path.isdir(folder):
                    continue
                if folder in hits:
                    continue
                lname = name.lower().strip()
                if lname in _ORPHAN_DENY_NAMES:
                    continue
                nname = _normalize_product_token(name)
                # must relate to the uninstalled product name
                related = (
                    nname == token
                    or token in nname
                    or nname in token
                    or (len(set(nname.split()) & set(token.split())) >= 2)
                )
                if not related:
                    continue
                if not _looks_like_residual_folder(folder):
                    continue
                hits.append(folder)
        return hits

    def _clear_leftover_panel(self):
        try:
            self._leftover_frame.pack_forget()
            for child in self._leftover_frame.winfo_children():
                child.destroy()
        except Exception:
            pass
        self._leftover_vars = {}

    def _show_leftover_review(self, summary_name, items):
        self._clear_leftover_panel()
        self._leftover_frame.pack(fill="x", pady=(10, 0))
        tk.Label(self._leftover_frame,
                 text=f"Possible leftovers for {summary_name} — check what to remove:",
                 font=(F, 9, "bold"), bg=COLORS["bg"], fg=COLORS["text"],
                 anchor="w").pack(fill="x", pady=(0, 4))
        for item in items:
            var = tk.BooleanVar(value=True)
            self._leftover_vars[item["path"]] = var
            cb = tk.Checkbutton(
                self._leftover_frame, text=item["label"], variable=var,
                font=(F, 8), bg=COLORS["bg"], fg=COLORS["text"],
                selectcolor=COLORS["surface"], activebackground=COLORS["bg"],
                activeforeground=COLORS["text"], anchor="w",
                wraplength=700, justify="left")
            cb.pack(fill="x", padx=4, pady=1)
        brow = tk.Frame(self._leftover_frame, bg=COLORS["bg"])
        brow.pack(fill="x", pady=(6, 0))
        AnimatedButton(
            brow, text="Delete Selected Leftovers",
            command=lambda: self._delete_leftovers(summary_name),
            bg=COLORS.get("accent_red", "#c44"),
            fg=COLORS["black"], font=(F, 9, "bold"),
            padx=12, pady=6).pack(side="right")
        AnimatedButton(
            brow, text="Skip",
            command=self._skip_leftovers,
            bg=COLORS["surface"], fg=COLORS["text"],
            font=(F, 9, "bold"), padx=12, pady=6).pack(side="right", padx=(0, 6))

    def _skip_leftovers(self):
        self._clear_leftover_panel()
        self._refresh()

    def _delete_leftovers(self, summary_name):
        paths = [p for p, v in self._leftover_vars.items() if v.get()]
        if not paths:
            self._status.config(text="Nothing selected to delete.")
            return
        if not _themed_askyesno(
                self._dlg, "Confirm Delete",
                f"Delete {len(paths)} selected leftover item(s) for {summary_name}?\n\n"
                "This cannot be undone. (A removed startup entry can still "
                "be restored later from Startup Manager.)",
                yes_text="Delete", no_text="Cancel"):
            return

        def work():
            freed = 0
            ok_count = 0
            try:
                from app.utils import (
                    TaskContext, clean_folder_contents, reg_delete_key)
                ctx = TaskContext(
                    log=lambda m: None,
                    set_status=lambda m: None,
                    cancelled=lambda: False,
                )
                for p in paths:
                    try:
                        if p.startswith("REGKEY::"):
                            _tag, hive, key_path = p.split("::", 2)
                            if reg_delete_key(ctx, hive, key_path):
                                ok_count += 1
                        elif p.startswith("STARTUP::"):
                            item_id = p[len("STARTUP::"):]
                            from app.startup_manager import set_item_enabled
                            okd, _msg = set_item_enabled(item_id, False)
                            if okd:
                                ok_count += 1
                        elif p.startswith("SHORTCUT::"):
                            target = p[len("SHORTCUT::"):]
                            try:
                                os.remove(target)
                                ok_count += 1
                            except OSError:
                                pass
                        else:
                            freed += clean_folder_contents(
                                ctx, p, remove_root=True)
                            ok_count += 1
                    except Exception:
                        pass
            except Exception:
                pass
            try:
                self._dlg.after(
                    0, lambda: self._after_leftover_delete(ok_count, freed))
            except Exception:
                pass

        import threading
        threading.Thread(target=work, daemon=True).start()
        self._status.config(text="Deleting selected leftovers…")

    def _after_leftover_delete(self, ok_count, freed):
        mb = freed / (1024 * 1024) if freed else 0
        self._status.config(
            text=f"Removed {ok_count} leftover item(s)"
                 + (f" (~{mb:.1f} MB freed)." if mb > 0 else "."))
        self._clear_leftover_panel()
        self._refresh()

    def close(self):
        self._scan_token[0] = True
        super().close()
