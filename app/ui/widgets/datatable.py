"""DataTable: the one list the redesigned Tools popups share.

Why this exists
---------------
The Tools popups used to build a list by creating one `ToolRow` (a rounded
Canvas + a Frame + four Labels + a tooltip) PER ITEM and packing them into a
scroll panel. Services alone is ~290 rows, so a filter keystroke destroyed and
rebuilt ~1,500 widgets, and every row was a card with its own padding, so only
six or seven items fit on screen. Seen next to Task Manager or Explorer it
read as a settings page, not as a list you can scan.

DataTable is a real table: a header row with sortable columns, fixed-height
rows, and ONE canvas that paints only the rows currently in view. Cost per
repaint is ~20 rows however long the data is, so 300 services or 5,000 files
scroll and filter instantly. It is still hand-drawn Tk with no ttk and no
third-party code, like every other widget in this package.

Cells
-----
`cell(row)` for a column returns a plain string or a dict:

    text    the string                  fg       colour override
    bold    bool                        mono     monospace face
    sub     second, smaller muted line  dot      colour of a status dot
    pill    (bg, fg) draws text as a pill
    bar     0..1 draws a small bar beside the text
    mid     True: cut the MIDDLE of a long path, keep both ends
    icon    a tk.PhotoImage drawn in the column's reserved icon slot
            (see the DataTable(icon_col=...) argument)

Rows are dicts. A column's content comes from its `cell(row)` callable, else
from row["_cells"][column_key], else from row[column_key]. Reserved keys (all
optional):

    _check    True/False: draws a checkbox before the first column; a click
              on it calls on_check(row) (the owner flips the value)
    _key      unique id (selection, scroll anchor)   _depth  indent level
    _chev     "open" | "closed" | None (disclosure)  _tint   left-edge colour
    _head     True: a group header row               _dim    True: muted row
    _actions  [{"id", "text", "fg", "enabled"}]      drawn as small buttons
              at the right edge; clicks arrive in on_action(row, action_id).

Depends only on app.ui.theme, like every other widget.
"""
from __future__ import annotations

import tkinter as tk

from app.ui.theme import (COLORS, F, FM, ICON_ROW_PX, _hex_lerp,
                          _measure_font, _round_rect_points, ellipsize)

PAD = 10                 # horizontal inset inside a cell
SB_W = 14                # reserved strip for the scrollbar on the right
CHECK_W = 28             # room taken by a row checkbox
CHIP_H = 22              # inline action button height
CHIP_GAP = 6

FONT = (F, 9)
FONT_B = (F, 9, "bold")
FONT_SUB = (F, 8)
FONT_HEAD = (F, 8, "bold")
FONT_PILL = (F, 8, "bold")
FONT_MONO = (FM, 9)


def ellipsize_mid(text, font, room):
    """`text` cut in the MIDDLE to fit `room` px, keeping both ends.

    A path is only recognisable by its first folder and its file name, so a
    trailing ellipsis (which keeps the folders and loses the name) is the
    wrong cut for it.
    """
    text = str(text or "")
    try:
        room = int(room)
        if room < 24 or font.measure(text) <= room:
            return text
        lo, hi = 1, len(text) - 1
        best = "…"
        while lo <= hi:
            keep = (lo + hi) // 2
            head = (keep + 1) // 2
            tail = keep - head
            cand = text[:head] + "…" + (text[-tail:] if tail else "")
            if font.measure(cand) <= room:
                best = cand
                lo = keep + 1
            else:
                hi = keep - 1
        return best
    except Exception:
        return text


def _sort_key(v):
    """Numbers before text, text case-insensitive; None sorts as empty."""
    if v is None:
        return (1, "")
    if isinstance(v, bool):
        return (0, int(v))
    if isinstance(v, (int, float)):
        return (0, v)
    return (1, str(v).lower())


class DataTable(tk.Frame):
    """Sortable, virtualised table. See the module docstring for the row and
    cell contract."""

    def __init__(self, parent, columns, *, row_h=34, head_h=28, accent=None,
                 on_select=None, on_action=None, on_activate=None,
                 show_header=True, bg=None, two_line=False, on_check=None,
                 icon_col=None):
        self._page_bg = bg or COLORS["bg"]
        super().__init__(parent, bg=self._page_bg, bd=0)
        self._accent = accent or COLORS["accent_pink"]
        self._cols = list(columns)
        self._row_h_req = int(row_h)
        self._row_h = int(row_h)
        self._two_line = bool(two_line)
        self._head_h_req = int(head_h)
        self._show_header = bool(show_header)
        self._head_h = int(head_h) if show_header else 0
        self._on_select = on_select
        self._on_action = on_action
        self._on_activate = on_activate
        self._on_check = on_check
        self._has_checks = False
        # Optional per-row app icon (user request: Services and Network
        # Connections showed no program icons). `icon_col` is the key of the
        # column that reserves a leading icon slot; a cell's "icon" (a
        # tk.PhotoImage) or the row's "_icon" fills it. The slot is reserved
        # for EVERY row of that column, so text stays aligned whether or not
        # an icon has loaded yet.
        self._icon_col = icon_col
        self._icon_w = 0

        self._src = []           # rows as given
        self._rows = []          # rows as shown (sorted)
        self._sort = None        # (col_key, descending)
        self._top = 0            # scroll offset in px
        self._sel_key = None
        self._hover = -1
        self._hits = []          # (x0, y0, x1, y1, kind, payload)
        self._xs = []            # column left edges
        self._ws = []            # column widths
        self._action_w = 0
        self._scale = 1.0
        self._empty = None       # dict(glyph, text, detail, action)
        self._sb_drag = None
        self._sb_thumb = None
        # M3 overlay scrollbar: shown while scrolling/hovering, faded out
        # ~1.4s after the user stops (a permanently-visible bar read as a
        # scrollbar nested inside the popup - user report).
        self._sb_show = True
        self._sb_after = None

        self.grid_rowconfigure(1, weight=1)
        self.grid_columnconfigure(0, weight=1)

        self._head = tk.Canvas(self, height=self._head_h, bg=self._page_bg,
                               highlightthickness=0, bd=0)
        if self._head_h:
            self._head.grid(row=0, column=0, sticky="ew")
        self._cv = tk.Canvas(self, bg=self._page_bg, highlightthickness=0,
                             bd=0, takefocus=1)
        self._cv.grid(row=1, column=0, sticky="nsew")

        cv = self._cv
        cv.bind("<Configure>", self._on_configure)
        cv.bind("<MouseWheel>", self._on_wheel)
        cv.bind("<Button-4>", lambda e: self._scroll_by(-self._row_h * 3))
        cv.bind("<Button-5>", lambda e: self._scroll_by(self._row_h * 3))
        cv.bind("<Button-1>", self._on_press)
        cv.bind("<B1-Motion>", self._on_drag)
        cv.bind("<ButtonRelease-1>", self._on_release)
        cv.bind("<Double-Button-1>", self._on_double)
        cv.bind("<Motion>", self._on_motion)
        cv.bind("<Enter>", lambda e: self._sb_poke())
        cv.bind("<Leave>", self._on_leave)
        # Cancel the scrollbar's idle fade on teardown (see _on_destroy).
        cv.bind("<Destroy>", self._on_destroy, add="+")
        cv.bind("<Up>", lambda e: self._step(-1))
        cv.bind("<Down>", lambda e: self._step(1))
        cv.bind("<Prior>", lambda e: self._step(-8))
        cv.bind("<Next>", lambda e: self._step(8))
        cv.bind("<Home>", lambda e: self._step(-10 ** 9))
        cv.bind("<End>", lambda e: self._step(10 ** 9))
        cv.bind("<Return>", self._on_enter_key)
        self._head.bind("<Button-1>", self._on_head_press)
        self._head.bind("<Configure>", lambda e: self._draw_head())

    # ------------------------------------------------------------- api --
    @property
    def rows(self):
        return list(self._rows)

    @property
    def selected(self):
        """The selected row dict, or None."""
        if self._sel_key is None:
            return None
        for r in self._rows:
            if self._key_of(r) == self._sel_key:
                return r
        return None

    def set_rows(self, rows):
        """Replace the data. Selection and scroll position survive when the
        selected row is still present."""
        self._src = list(rows or [])
        self._has_checks = any("_check" in r for r in self._src)
        self._empty_rows_changed()
        self._apply_sort()
        widest = 0
        for r in self._rows:
            acts = r.get("_actions") or ()
            if acts:
                widest = max(widest, self._actions_width(acts))
        self._action_w = widest + (PAD if widest else 0)
        keys = {self._key_of(r) for r in self._rows}
        if self._sel_key not in keys:
            self._sel_key = None
        self._clamp()
        self._layout()
        self._draw_head()
        self._redraw()
        self._sb_poke()

    def set_empty(self, text="", detail="", glyph="", action=None):
        """What to say when there are no rows. `action` = (label, callback)."""
        self._empty = {"text": text, "detail": detail, "glyph": glyph,
                       "action": action}
        self._redraw()

    def apply_icons(self, key_to_img):
        """Attach icons to rows and repaint.

        `key_to_img` maps a row's key (see the `_key` row field) to a
        tk.PhotoImage. Called on the Tk thread once a dialog's worker has
        finished extracting icon PPM bytes, so the icon column fills in
        without rebuilding the whole row set. Callers keep the images
        alive; this only points the cells at them.
        """
        if not self._icon_col or not key_to_img:
            return
        for r in self._rows:
            img = key_to_img.get(self._key_of(r))
            if img is None:
                continue
            cells = r.setdefault("_cells", {})
            cell = cells.get(self._icon_col)
            if isinstance(cell, dict):
                cell["icon"] = img
            else:
                cells[self._icon_col] = {
                    "text": "" if cell is None else str(cell), "icon": img}
        self._redraw()

    # ---- M3 overlay scrollbar ---------------------------------------- #
    def _sb_poke(self, ms=1400):
        """Reveal the floating scrollbar and (re)arm its idle fade."""
        if not self._sb_show:
            self._sb_show = True
            try:
                self._redraw()
            except Exception:
                pass
        if self._sb_after is not None:
            try:
                self._cv.after_cancel(self._sb_after)
            except Exception:
                pass
        try:
            self._sb_after = self._cv.after(ms, self._sb_conceal)
        except Exception:
            self._sb_after = None

    def _sb_conceal(self):
        self._sb_after = None
        self._sb_show = False
        try:
            self._redraw()
        except Exception:
            pass

    def _on_destroy(self, e):
        """Drop the pending scrollbar fade when the canvas goes away."""
        if getattr(e, "widget", None) is not self._cv:
            return
        if self._sb_after is not None:
            try:
                self._cv.after_cancel(self._sb_after)
            except Exception:
                pass
            self._sb_after = None

    def set_columns(self, columns):
        """Swap the column set (e.g. two views of one dialog). Clears sort."""
        self._cols = list(columns)
        self._sort = None
        self._apply_sort()
        self._layout()
        self._draw_head()
        self._redraw()

    def set_sort(self, col_key, descending=False):
        self._sort = (col_key, bool(descending)) if col_key else None
        self._apply_sort()
        self._draw_head()
        self._redraw()

    def select(self, key, notify=False, reveal=True):
        self._sel_key = key
        if reveal:
            self.see(key)
        self._redraw()
        if notify and self._on_select is not None:
            row = self.selected
            if row is not None:
                self._on_select(row)

    def see(self, key):
        for i, r in enumerate(self._rows):
            if self._key_of(r) == key:
                y0 = i * self._row_h
                y1 = y0 + self._row_h
                h = self._view_h()
                if y0 < self._top:
                    self._top = y0
                elif y1 > self._top + h:
                    self._top = y1 - h
                self._clamp()
                return

    def refresh(self):
        self._layout()
        self._draw_head()
        self._redraw()

    # ---------------------------------------------------------- helpers --
    def _empty_rows_changed(self):
        pass

    def _fit_metrics(self):
        """Row / header / button heights follow the REAL font size.

        Fixed pixel heights looked right at 100% display scaling and clipped
        the second line of every row at 125-150%, because Tk fonts scale and
        pixel constants do not. Measure the fonts and take the larger.
        """
        global CHIP_H
        try:
            ls = lambda f: self._measure(f).metrics("linespace")
            need = (ls(FONT_B) + ls(FONT_SUB) + 16) if self._two_line \
                else (ls(FONT_B) + 16)
            self._row_h = max(self._row_h_req, need)
            self._scale = max(1.0, ls(FONT) / 16.0)
            self._icon_w = (int((ICON_ROW_PX + 8) * self._scale)
                            if self._icon_col else 0)
            CHIP_H = max(22, ls(FONT_SUB) + 10)
            if self._show_header:
                self._head_h = max(self._head_h_req, ls(FONT_HEAD) + 14)
                self._head.configure(height=self._head_h)
        except Exception:
            pass

    def _key_of(self, row):
        k = row.get("_key")
        return k if k is not None else id(row)

    def _icon_index(self):
        """Index of the icon column, or -1 when there is none."""
        if self._icon_col is None:
            return -1
        for j, c in enumerate(self._cols):
            if c.get("key") == self._icon_col:
                return j
        return -1

    def _view_h(self):
        return max(1, self._cv.winfo_height())

    def _content_h(self):
        return len(self._rows) * self._row_h

    def _clamp(self):
        top = max(0, min(self._top, self._content_h() - self._view_h()))
        self._top = top

    def _apply_sort(self):
        rows = list(self._src)
        if self._sort:
            ck, desc = self._sort
            col = next((c for c in self._cols if c["key"] == ck), None)
            if col is not None:
                fn = col.get("sort")
                rows.sort(key=lambda r: _sort_key(
                    fn(r) if fn else r.get(ck)), reverse=desc)
        self._rows = rows

    def _measure(self, font):
        return _measure_font(self.winfo_toplevel(), font)

    def _actions_width(self, acts):
        f = self._measure(FONT_SUB)
        return sum(f.measure(a.get("text", "")) + 18 for a in acts) \
            + CHIP_GAP * max(0, len(acts) - 1)

    def _layout(self):
        w = max(0, self._cv.winfo_width() - SB_W - self._action_w)
        cols = self._cols
        mins = [int(int(c.get("w", 80)) * self._scale) for c in cols]
        weights = [float(c.get("weight", 0)) for c in cols]
        widths = list(mins)
        extra = w - sum(mins)
        if extra > 0 and sum(weights) > 0:
            tot = sum(weights)
            for i, wt in enumerate(weights):
                widths[i] += int(extra * wt / tot)
        elif extra < 0 and sum(weights) > 0:
            # shrink the stretchy columns first, never below their floor
            for _ in range(3):
                deficit = sum(widths) - w
                if deficit <= 0:
                    break
                flex = [i for i, wt in enumerate(weights)
                        if wt > 0 and widths[i] > int(int(cols[i].get("min", 48))
                                                      * self._scale)]
                if not flex:
                    break
                share = max(1, deficit // len(flex))
                for i in flex:
                    floor = int(int(cols[i].get("min", 48)) * self._scale)
                    widths[i] = max(floor, widths[i] - share)
        x = 0
        xs = []
        for wd in widths:
            xs.append(x)
            x += wd
        self._xs, self._ws = xs, widths

    # ------------------------------------------------------------ paint --
    def _draw_head(self):
        if not self._head_h:
            return
        hc = self._head
        hc.delete("all")
        f = self._measure(FONT_HEAD)
        for i, c in enumerate(self._cols):
            x0, wd = (self._xs[i], self._ws[i]) if self._xs else (0, 80)
            title = c.get("title", "")
            anchor = c.get("anchor", "w")
            tx = x0 + PAD if anchor == "w" else (
                x0 + wd - PAD if anchor == "e" else x0 + wd / 2)
            if i == 0 and self._has_checks and anchor == "w":
                tx += CHECK_W
            if (self._icon_col is not None and anchor == "w"
                    and c.get("key") == self._icon_col):
                tx += self._icon_w
            arrow = ""
            if self._sort and self._sort[0] == c["key"]:
                arrow = " ▼" if self._sort[1] else " ▲"
            txt = ellipsize(title.upper() + arrow, f, wd - PAD * 2)
            hc.create_text(tx, self._head_h / 2, text=txt, font=FONT_HEAD,
                           fill=(COLORS["text"] if arrow
                                 else COLORS["subtext"]),
                           anchor={"w": "w", "e": "e",
                                   "center": "center"}[anchor])

    def _redraw(self):
        cv = self._cv
        cv.delete("all")
        self._hits = []
        W = cv.winfo_width()
        H = cv.winfo_height()
        if W < 20 or H < 20:
            return
        if not self._rows:
            self._draw_empty(W, H)
            return
        rh = self._row_h
        first = max(0, self._top // rh)
        last = min(len(self._rows), (self._top + H) // rh + 2)
        for i in range(first, last):
            self._draw_row(i, i * rh - self._top, W)
        self._draw_scrollbar(W, H)

    def _draw_row(self, i, y, W):
        cv = self._cv
        row = self._rows[i]
        rh = self._row_h
        key = self._key_of(row)
        selected = key == self._sel_key
        hover = i == self._hover
        x_l, x_r = 4, W - SB_W + 4
        if selected:
            fill = COLORS["surface"]
        elif row.get("_head"):
            fill = COLORS["bg_alt"]
        elif hover:
            fill = COLORS["bg_alt"]
        else:
            fill = None
        if fill:
            cv.create_polygon(
                _round_rect_points(x_l, y + 1, x_r, y + rh - 1, 7),
                smooth=True, fill=fill, outline="")
        tint = row.get("_tint")
        if selected or tint:
            cv.create_rectangle(x_l, y + 8, x_l + 2, y + rh - 8,
                                fill=tint or self._accent, outline="")
        dim = bool(row.get("_dim"))
        depth = int(row.get("_depth") or 0)
        checked = row.get("_check")
        if checked is not None:
            by = y + rh / 2
            bx = x_l + 8
            cv.create_polygon(
                _round_rect_points(bx, by - 8, bx + 16, by + 8, 4),
                smooth=True, outline=self._accent if checked
                else COLORS["muted"], fill=self._accent if checked
                else COLORS["bg"], width=1)
            if checked:
                cv.create_line(bx + 4, by, bx + 7, by + 3, bx + 12, by - 4,
                               fill=COLORS["black"], width=2)
            self._hits.append((x_l, y, x_l + CHECK_W + 4, y + rh, "check",
                               key))
        for ci, col in enumerate(self._cols):
            x0, wd = self._xs[ci], self._ws[ci]
            lead = depth * 20 if ci == 0 else 0
            if ci == 0 and checked is not None:
                lead += CHECK_W
            self._draw_cell(col, row, x0 + lead, wd - lead, y, dim, ci == 0,
                            bool(row.get("_head")),
                            ci == self._icon_index())
        acts = row.get("_actions") or ()
        if acts:
            ax = W - SB_W - PAD
            f = self._measure(FONT_SUB)
            for a in reversed(acts):
                cw = f.measure(a.get("text", "")) + 18
                x1, x0 = ax, ax - cw
                self._draw_chip(x0, y + (rh - CHIP_H) / 2, x1,
                                y + (rh + CHIP_H) / 2, a, key)
                ax = x0 - CHIP_GAP

    def _draw_chip(self, x0, y0, x1, y1, a, key):
        cv = self._cv
        enabled = a.get("enabled", True)
        fg = a.get("fg") or COLORS["text"]
        cv.create_polygon(_round_rect_points(x0, y0, x1, y1, 6), smooth=True,
                          fill=COLORS["surface"] if enabled
                          else COLORS["bg_alt"], outline="")
        cv.create_text((x0 + x1) / 2, (y0 + y1) / 2, text=a.get("text", ""),
                       font=FONT_SUB, fill=fg if enabled else COLORS["muted"])
        if enabled:
            self._hits.append((x0, y0, x1, y1, "action", (key, a.get("id"))))

    def _draw_cell(self, col, row, x0, wd, y, dim, first, is_head,
                   is_icon=False):
        cv = self._cv
        rh = self._row_h
        if col.get("cell"):
            v = col["cell"](row)
        elif col["key"] in (row.get("_cells") or ()):
            v = row["_cells"][col["key"]]
        else:
            v = row.get(col["key"])
        spec = v if isinstance(v, dict) else {
            "text": "" if v is None else str(v)}
        text = str(spec.get("text", ""))
        anchor = col.get("anchor", "w")
        font = FONT_MONO if spec.get("mono") else (
            FONT_B if (spec.get("bold") or is_head) else FONT)
        fg = spec.get("fg") or (COLORS["muted"] if dim else COLORS["text"])
        sub = spec.get("sub")
        room = wd - PAD * 2
        x = x0 + PAD
        cy = y + rh / 2
        main_y = y + rh * 0.36 if sub else cy

        # App icon slot (Services / Network Connections). The slot is
        # reserved whether or not an image has loaded yet, so the text
        # never jumps sideways when icons arrive late.
        if is_icon and self._icon_w:
            img = spec.get("icon") or row.get("_icon")
            if img is not None:
                try:
                    cv.create_image(x, cy, image=img, anchor="w")
                except Exception:
                    pass
            x += self._icon_w
            room -= self._icon_w

        chev = row.get("_chev") if first else None
        if chev:
            cv.create_text(x + 4, cy, text="⌄" if chev == "open" else "›",
                           font=(F, 11, "bold"), fill=COLORS["subtext"])
            x += 16
            room -= 16
        if spec.get("dot"):
            cv.create_oval(x, cy - 4, x + 8, cy + 4, fill=spec["dot"],
                           outline="")
            x += 15
            room -= 15

        if spec.get("bar") is not None:
            frac = max(0.0, min(1.0, float(spec["bar"])))
            tw = self._measure(FONT_SUB).measure(text) + 8 if text else 0
            bw = max(20, room - tw)
            by = cy
            cv.create_polygon(
                _round_rect_points(x, by - 3, x + bw, by + 3, 3),
                smooth=True, fill=COLORS["surface"], outline="")
            if frac > 0:
                cv.create_polygon(
                    _round_rect_points(x, by - 3, x + max(6, bw * frac),
                                       by + 3, 3),
                    smooth=True, fill=spec.get("barcolor") or self._accent,
                    outline="")
            if text:
                cv.create_text(x0 + wd - PAD, cy, text=text, font=FONT_SUB,
                               fill=COLORS["subtext"], anchor="e")
            return

        fobj = self._measure(font)
        if spec.get("pill"):
            bgc, fgc = spec["pill"]
            fp = self._measure(FONT_PILL)
            t = ellipsize(text, fp, max(20, room - 14))
            pw = fp.measure(t) + 14
            px = x if anchor == "w" else (
                x0 + wd - PAD - pw if anchor == "e" else x0 + wd / 2 - pw / 2)
            cv.create_polygon(
                _round_rect_points(px, cy - 10, px + pw, cy + 10, 10),
                smooth=True, fill=bgc, outline="")
            cv.create_text(px + pw / 2, cy, text=t, font=FONT_PILL, fill=fgc)
            return

        shown = (ellipsize_mid(text, fobj, room) if spec.get("mid")
                 else ellipsize(text, fobj, room))
        tx = x if anchor == "w" else (
            x0 + wd - PAD if anchor == "e" else x0 + wd / 2)
        cv.create_text(tx, main_y, text=shown, font=font, fill=fg,
                       anchor={"w": "w", "e": "e", "center": "center"}[anchor])
        if sub:
            fs = self._measure(FONT_SUB)
            ss = (ellipsize_mid(str(sub), fs, room) if spec.get("mid")
                  else ellipsize(str(sub), fs, room))
            cv.create_text(tx, y + rh * 0.70, text=ss, font=FONT_SUB,
                           fill=COLORS["muted"] if not dim else COLORS["muted"],
                           anchor={"w": "w", "e": "e",
                                   "center": "center"}[anchor])

    def _draw_empty(self, W, H):
        cv = self._cv
        e = self._empty or {}
        if not e:
            return
        cx = W / 2
        y = max(30, H / 2 - 46)
        if e.get("glyph"):
            cv.create_text(cx, y, text=e["glyph"], font=(F, 22),
                           fill=self._accent)
            y += 34
        cv.create_text(cx, y, text=e.get("text", ""), font=FONT_B,
                       fill=COLORS["text"], width=max(160, W - 80),
                       justify="center")
        y += 22
        if e.get("detail"):
            cv.create_text(cx, y, text=e["detail"], font=FONT_SUB,
                           fill=COLORS["subtext"], width=max(160, W - 100),
                           justify="center", anchor="n")
            y += 36
        act = e.get("action")
        if act:
            f = self._measure(FONT_B)
            bw = f.measure(act[0]) + 34
            y += 8
            cv.create_polygon(
                _round_rect_points(cx - bw / 2, y, cx + bw / 2, y + 32, 9),
                smooth=True, fill=self._accent, outline="")
            cv.create_text(cx, y + 16, text=act[0], font=FONT_B,
                           fill=COLORS["black"])
            self._hits.append((cx - bw / 2, y, cx + bw / 2, y + 32,
                               "empty_action", act[1]))

    def _draw_scrollbar(self, W, H):
        ch = self._content_h()
        self._sb_thumb = None
        if ch <= H or not self._sb_show:
            return
        th = max(28, int(H * H / ch))
        span = H - th
        ty = int(span * self._top / max(1, ch - H))
        x1 = W - 3
        x0 = x1 - (6 if self._sb_drag is not None else 4)
        # M3 overlay: a slim floating thumb, no track, quiet until dragged.
        self._cv.create_polygon(
            _round_rect_points(x0, ty + 2, x1, ty + th - 2, 3), smooth=True,
            fill=COLORS["muted"] if self._sb_drag is not None
            else _hex_lerp(COLORS["bg"], COLORS["muted"], 0.85),
            outline="")
        self._sb_thumb = (ty, ty + th)

    # ----------------------------------------------------------- events --
    def _on_configure(self, _e=None):
        self._fit_metrics()
        self._layout()
        self._clamp()
        self._draw_head()
        self._redraw()

    def _scroll_by(self, dy):
        self._sb_poke()
        old = self._top
        self._top += dy
        self._clamp()
        if self._top != old:
            self._redraw()

    def _on_wheel(self, e):
        d = -1 if e.delta > 0 else 1
        self._scroll_by(d * self._row_h * 3)

    def _row_at(self, y):
        i = int((y + self._top) // self._row_h)
        return i if 0 <= i < len(self._rows) else -1

    def _hit(self, x, y):
        for x0, y0, x1, y1, kind, payload in self._hits:
            if x0 <= x <= x1 and y0 <= y <= y1:
                return kind, payload
        return None, None

    def _on_press(self, e):
        self._cv.focus_set()
        W = self._cv.winfo_width()
        if self._sb_thumb and e.x >= W - SB_W:
            self._sb_poke()
            ty0, ty1 = self._sb_thumb
            if ty0 <= e.y <= ty1:
                self._sb_drag = (e.y, self._top)
            else:
                self._scroll_by((-1 if e.y < ty0 else 1) * self._view_h() * 0.9)
            self._redraw()
            return
        kind, payload = self._hit(e.x, e.y)
        if kind == "empty_action":
            try:
                payload()
            except Exception:
                pass
            return
        if kind == "check":
            row = next((r for r in self._rows
                        if self._key_of(r) == payload), None)
            if row is not None and self._on_check is not None:
                self._on_check(row)
            return
        if kind == "action":
            key, aid = payload
            row = next((r for r in self._rows if self._key_of(r) == key), None)
            if row is not None and self._on_action is not None:
                self._on_action(row, aid)
            return
        i = self._row_at(e.y)
        if i < 0:
            return
        row = self._rows[i]
        if row.get("_head") and not row.get("_chev") and not row.get("_key"):
            return
        self._sel_key = self._key_of(row)
        self._redraw()
        if self._on_select is not None:
            self._on_select(row)

    def _on_drag(self, e):
        if self._sb_drag is None:
            return
        self._sb_poke()
        y0, top0 = self._sb_drag
        H = self._view_h()
        ch = self._content_h()
        th = max(28, int(H * H / ch)) if ch else 28
        span = max(1, H - th)
        self._top = top0 + (e.y - y0) * (ch - H) / span
        self._clamp()
        self._redraw()

    def _on_release(self, _e):
        if self._sb_drag is not None:
            self._sb_drag = None
            self._redraw()

    def _on_double(self, e):
        i = self._row_at(e.y)
        if i >= 0 and self._on_activate is not None:
            self._on_activate(self._rows[i])

    def _on_motion(self, e):
        i = self._row_at(e.y)
        kind, _p = self._hit(e.x, e.y)
        try:
            self._cv.config(cursor="hand2" if (kind or i >= 0) else "")
        except Exception:
            pass
        if i != self._hover:
            self._hover = i
            self._redraw()

    def _on_leave(self, _e):
        if self._hover != -1:
            self._hover = -1
            self._redraw()

    def _step(self, n):
        if not self._rows:
            return "break"
        self._sb_poke()
        cur = next((i for i, r in enumerate(self._rows)
                    if self._key_of(r) == self._sel_key), -1)
        nxt = 0 if cur < 0 else max(0, min(len(self._rows) - 1, cur + n))
        row = self._rows[nxt]
        self._sel_key = self._key_of(row)
        self.see(self._sel_key)
        self._redraw()
        if self._on_select is not None:
            self._on_select(row)
        return "break"

    def _on_enter_key(self, _e):
        row = self.selected
        if row is not None and self._on_activate is not None:
            self._on_activate(row)

    def _on_head_press(self, e):
        for i, c in enumerate(self._cols):
            if not c.get("sortable", True) or not self._xs:
                continue
            if self._xs[i] <= e.x < self._xs[i] + self._ws[i]:
                if self._sort and self._sort[0] == c["key"]:
                    self._sort = (c["key"], not self._sort[1])
                else:
                    self._sort = (c["key"], bool(c.get("desc_first", False)))
                self._apply_sort()
                self._draw_head()
                self._redraw()
                return
