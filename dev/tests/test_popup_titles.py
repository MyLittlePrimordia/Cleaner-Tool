"""Each popup names itself once (design audit C-002).

Every Tools popup used to show two headings 40px apart: the chrome strip's
16pt dialog title ("Speaker Test") and the banner's 14pt headline ("Tap a
direction to hear it."). They were both bold, both near the top, and neither
was clearly a title - so the window had no title hierarchy at all.

The fix is structural, not cosmetic: a popup that draws its own headline tells
the chrome strip so (`headline_below=True`) and the strip then carries the
window's NAME as an overline. A popup with no headline of its own keeps the
full title in the strip, exactly as before.

The other half is the popups that never had a headline of their own: My
Setups, the catalog picker, the scorecard, the two Tweak browsers and the
confirm/notice prompts built by `_themed_askyesno` and friends. Those keep the
strip's 16pt dialog title, and that title must be the only thing in the window
drawn at title weight - no body heading, and no copy of the name in the body.
Their lead sentence is a caption or 10pt label, never a second title.

These tests read the rendered canvas, so they fail if either half of that
contract comes apart - not just if a source string changes.
"""
from __future__ import annotations

import ast
import pathlib
import re

import pytest

from app.ui import theme

tk = pytest.importorskip("tkinter")

from app.ui.base import ThemedModal          # noqa: E402
from app.ui.dialogs._tool_page import ToolPage  # noqa: E402
from dev.tests._tkhelpers import make_root        # noqa: E402


class _Stub:
    def __getattr__(self, name):
        return lambda *a, **k: None


def _settle(root, times=8):
    for _ in range(times):
        root.update()


def _canvas_texts(modal):
    """[(text, font-string, y)] for every text item in the dialog CARD.

    Scoped to the chrome card, not every canvas: the speaker tester's keypad
    is drawn on child canvases whose item coordinates restart at y=0, so a
    window-wide walk cannot tell a keycap from a title.
    """
    out = []
    card = getattr(modal, "_card", None)
    if card is None:
        return out
    for item in card.find_all():
        try:
            if card.type(item) != "text":
                continue
            out.append((card.itemcget(item, "text"),
                        card.itemcget(item, "font"),
                        card.coords(item)[1]))
        except Exception:
            pass
    return out


def _font_size(root, spec, fallback=None):
    import tkinter.font as tkfont
    try:
        return tkfont.Font(root=root, font=spec).actual("size")
    except Exception:
        return fallback


def _label_texts(widget):
    out = []
    stack = [widget]
    while stack:
        w = stack.pop()
        try:
            text = str(w.cget("text") or "")
        except Exception:
            text = ""
        if text:
            out.append(text)
        try:
            stack.extend(w.winfo_children())
        except Exception:
            pass
    return out


# 2026-10: this file used to prove the label-vs-title hierarchy on the
# Speaker Tester, which its keypad glyphs made the hardest case. The tester was
# removed with the other unreachable peripherals, so the same contract is
# covered by the dialogs below and by the shared ThemedModal tests.


def test_a_popup_without_its_own_headline_keeps_the_full_title():
    root = make_root()
    try:
        root.geometry("1280x800")
        modal = ThemedModal(root, title="Delete setup?", accent="#F28B82")
        _settle(root)
        texts = _canvas_texts(modal)
        title = [t for t in texts if t[0] == "Delete setup?"]
        assert title, [t[0] for t in texts]
        assert _font_size(root, title[0][1]) == theme.TYPE["dialog_title"][1]
        assert modal._headline_below is False
    finally:
        root.destroy()


def test_tool_page_tells_the_chrome_it_draws_its_own_headline():
    root = make_root()
    try:
        root.geometry("1280x800")

        class _Page(ToolPage):
            def __init__(self, parent):
                super().__init__(parent, title="Services", scope="Read only.")
                self.banner.set_state("⚙", theme.COLORS["accent_sky"])
                self.banner.set_title("What is running right now?")

        page = _Page(root)
        _settle(root)
        assert page._headline_below is True
        texts = _canvas_texts(page)
        assert any(t[0] == "SERVICES" for t in texts)
        # One title: the headline, not the name.
        assert not any(t[0] == "Services" for t in texts)
        assert "What is running right now?" in _label_texts(page._dlg)
    finally:
        root.destroy()


def _body_texts(modal):
    """[(text, font-spec)] for everything drawn inside the dialog BODY.

    Labels AND canvas-drawn text are collected: empty states, gauges and the
    scorecard's hero number are painted on child canvases rather than packed,
    so a label-only walk would miss them.
    """
    out = []
    stack = [modal.body]
    while stack:
        w = stack.pop()
        try:
            text = w.cget("text")
        except Exception:
            text = ""
        if isinstance(text, str) and text:
            try:
                out.append((text, w.cget("font")))
            except Exception:
                pass
        if isinstance(w, tk.Canvas):
            for item in w.find_all():
                if w.type(item) == "text":
                    out.append((w.itemcget(item, "text"),
                                w.itemcget(item, "font")))
        try:
            stack.extend(w.winfo_children())
        except Exception:
            pass
    return out


def _plain_dialogs():
    """(id, chrome title, builder) for the plain modals that build headless.

    `show_tweak_preview` and `show_tweak_snapshots` are the same shape but end
    in a modal `wait()` their callers need, and Auto Maintenance is built
    inline in app.py; all three are covered by the source scan below (they
    open with a caption subtitle / form labels, never a heading).
    """
    from app.ui.dialogs.catalog_picker_dialog import _CatalogPickerDialog
    from app.ui.dialogs.install_profiles_dialog import InstallProfilesDialog
    from app.ui.dialogs.scorecard_dialog import ScorecardDialog

    return [
        ("catalog picker", "Pick a game",
         lambda root: _CatalogPickerDialog(
             root, "Pick a game",
             entries=[{"name": "Steam", "launcher": "S",
                       "folder": "C:/Steam"}])),
        ("my setups", "My Setups",
         lambda root: InstallProfilesDialog(root, _Stub())),
        ("scorecard", "Clean Complete",
         lambda root: ScorecardDialog(
             root, tab_name="Clean", mode="clean", cancelled=False,
             results=[("Empty temp files", "ok", 1024)], total_bytes=1024)),
    ]


@pytest.mark.parametrize("_label, title, build", _plain_dialogs(),
                         ids=[d[0] for d in _plain_dialogs()])
def test_a_plain_dialog_names_itself_only_in_the_strip(_label, title, build):
    root = make_root()
    try:
        root.geometry("1280x800")
        root.update()
        dlg = build(root)
        _settle(root)
        title_px = _font_size(root, theme.TYPE["dialog_title"])
        strip = [(t, f) for t, f, y in _canvas_texts(dlg) if y < 48]
        assert [t for t, f in strip
                if _font_size(root, f) == title_px] == [title]
        body = _body_texts(dlg)
        # the name appears once, in the strip - never again in the body
        assert not [t for t, _f in body
                    if t.strip().lower() == title.strip().lower()], title
        # and no body heading competes at the same rung
        assert not [t for t, f in body
                    if _font_size(root, f) == title_px], title
    finally:
        root.destroy()


def test_confirm_and_notice_prompts_keep_one_title(monkeypatch):
    """The five prompt helpers: strip title, caption message, nothing else."""
    from app.ui import base as uibase

    built = []
    monkeypatch.setattr(uibase.ThemedModal, "wait",
                        lambda self, *a, **k: built.append(self))
    cases = [
        ("askyesno", "Delete setup?", lambda root: uibase._themed_askyesno(
            root, "Delete setup?", "Delete 'Work'? This cannot be undone.")),
        ("showwarning", "Could not empty",
         lambda root: uibase._themed_showwarning(
             root, "Could not empty", "Another program is using the bin.")),
        ("showerror", "Elevation Failed",
         lambda root: uibase._themed_showerror(
             root, "Elevation Failed", "The helper did not start.")),
    ]
    root = make_root()
    try:
        root.geometry("1280x800")
        root.update()
        title_px = _font_size(root, theme.TYPE["dialog_title"])
        for label, title, call in cases:
            call(root)
            assert built, label
            modal = built.pop()
            _settle(root)
            strip = [(t, f) for t, f, y in _canvas_texts(modal) if y < 48]
            assert [t for t, f in strip
                    if _font_size(root, f) == title_px] == [title], label
            body = _body_texts(modal)
            assert not [t for t, _f in body
                        if t.strip().lower() == title.strip().lower()], label
            assert not [t for t, f in body
                        if _font_size(root, f) == title_px], label
            modal.close()
            root.update()
    finally:
        root.destroy()


#: Titles that name a mood or a feature rather than what just happened.
_GENERIC_TITLES = {"Failed", "Done", "Stopped", "Busy", "Quarantine",
                   "Error", "Warning", "Notice", "Info",
                   "Unexpected Error", "Something Went Wrong"}

_PROMPT_FUNCS = {"_themed_askyesno", "_themed_showinfo",
                 "_themed_showwarning", "_themed_showerror"}


def _flatten(node):
    """A title/message as text, with `{}` where the interpolation goes."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):             # f"Undo {n} tweaks?"
        return "".join(v.value if isinstance(v, ast.Constant) else "{}"
                       for v in node.values)
    if (isinstance(node, ast.BinOp) and isinstance(node.op, ast.Mod)
            and isinstance(node.left, ast.Constant)
            and isinstance(node.left.value, str)):  # "Undo %d tweaks?" % n
        return node.left.value
    return None


def _prompt_calls():
    """[(path, line, title, message, helper)] for every prompt call.

    Parsed with `ast` rather than a regex because the title is the SECOND
    argument (the parent comes first) and the message that follows it is full
    of commas and brackets. f-strings and `%` templates are flattened with
    `{}` placeholders for their interpolated parts: a count in the title is
    exactly what makes it specific, and the words around it still have to
    name the outcome. `message` is None when it is a bare variable (an error
    string handed straight through), which no scan can read.
    """
    out = []
    for path in sorted(pathlib.Path("app").rglob("*.py")):
        # A file that will not parse must FAIL here, not be skipped: a scan
        # that quietly drops a module looks exactly like a clean one.
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or len(node.args) < 2:
                continue
            func = node.func
            name = getattr(func, "id", None) or getattr(func, "attr", None)
            if name not in _PROMPT_FUNCS:
                continue
            title = _flatten(node.args[1])
            message = _flatten(node.args[2]) if len(node.args) > 2 else None
            if title is not None:
                out.append((path, node.lineno, title, message, name))
    return out


def _prompt_titles():
    """[(path, line, title, helper)] - `_prompt_calls` without the messages."""
    return [(path, line, title, kind)
            for path, line, title, _msg, kind in _prompt_calls()]


def test_prompt_titles_name_the_outcome_not_the_mood():
    """The strip says what happened; the message says why.

    "Failed", "Busy", "Done" and "Quarantine" made the reader hunt through
    the body to learn what the popup was about - the same failure as two
    competing headings, one rung lower. A one-word title cannot name an
    outcome, and the few two-word ones that are just as content-free are
    listed explicitly.
    """
    offenders = []
    for path, line, title, _kind in _prompt_titles():
        if title in _GENERIC_TITLES or len(title.split()) < 2:
            offenders.append("%s:%d %r" % (path, line, title))
    assert not offenders, "\n  ".join(offenders)


def test_every_confirmation_title_is_a_question():
    """An ask/confirm strip asks; the body supplies the facts.

    "Full Check" and "Pre-flight Check" named the dialog, and the question
    the buttons answer was the last line of the body - so the reader met the
    question twice and the title never said what for.
    """
    offenders = ["%s:%d %r" % (path, line, title)
                 for path, line, title, kind in _prompt_titles()
                 if kind == "_themed_askyesno"
                 and not title.rstrip().endswith("?")]
    assert not offenders, "\n  ".join(offenders)


def test_no_prompt_title_is_a_confirm_label():
    """A confirmation's strip carries the question; its body the specifics.

    "Confirm Uninstall" named the dialog's category, so the reader still had
    to read the body to find out what was about to happen. "Uninstall 3
    programs?" is the question the two buttons answer.
    """
    offenders = ["%s:%d %r" % (path, line, title)
                 for path, line, title, _kind in _prompt_titles()
                 if title.lower().startswith("confirm ")]
    assert not offenders, "\n  ".join(offenders)


#: Notice helpers: one button, so the strip names what just happened and the
#: body carries the specifics. There is no question for the body to answer.
_NOTICE_FUNCS = {"_themed_showinfo", "_themed_showwarning", "_themed_showerror"}

#: Phrases that already announce a failure or an absence in a title.
_FAILURE_TITLES = ("could not", "cannot", "can't", "failed", "cancelled",
                   "unavailable", "not scheduled", "not removed",
                   "not writable", "not a program", "already empty",
                   "nothing")

#: Ways a body announces one again, before saying anything new. "is not"
#: is deliberately absent: "That link is not a web address" is the reason
#: the link failed, not a second copy of the news. "Nothing was" is handled
#: by _EMPTY_OPENERS instead - see there.
_FAILURE_OPENERS = ("could not", "couldn't", "cannot", "can't", "failed",
                    "failure", "did not", "was not", "were not",
                    "unable to", "something went wrong")

#: A body that opens by saying nothing happened, under a title that already
#: says so ("Bin Already Empty" / "Nothing was deleted."). Separate from
#: _FAILURE_OPENERS because "Nothing was added." is a real fact under "That
#: File Is Not a Program" - it is not repeating the title.
_EMPTY_OPENERS = ("nothing was", "nothing has", "nothing is")
_EMPTY_TITLES = ("nothing", "empty")

_STOPWORDS = {"the", "a", "an", "to", "of", "in", "on", "for", "and",
              "or", "is", "are", "was", "were", "be", "been", "that",
              "this", "it", "you", "your", "with", "as", "at", "by",
              "from", "so", "there", "than"}
_WORD_RE = re.compile(r"[a-z0-9']+")


def _content(text):
    """The words that carry meaning, stopwords dropped."""
    return {w for w in _WORD_RE.findall(text.lower()) if w not in _STOPWORDS}


def _phrases(text, n=2):
    """The n-word phrases of `text`, stopwords dropped first."""
    words = [w for w in _WORD_RE.findall(text.lower()) if w not in _STOPWORDS]
    return {" ".join(words[i:i + n]) for i in range(len(words) - n + 1)}


def test_a_notice_body_does_not_repeat_its_title():
    """The strip names what happened; the body must not name it again.

    Every notice used to open by saying the title back: "System Tray
    Unavailable" over "Could not create the system-tray icon.", "Could Not
    Empty" over "The bin could not be emptied: [WinError 5]". The reader got
    the news twice and the reason - the part they cannot guess - last. Three
    nets, because restating takes three shapes: announce the failure again,
    reuse the title's own two-word phrase, or report that nothing happened
    under a title that already says so.
    """
    offenders = []
    for path, line, title, message, kind in _prompt_calls():
        if kind not in _NOTICE_FUNCS or not message or not title:
            continue
        first = message.strip().split("\n", 1)[0].strip().lower()
        said_again = [m for m in _FAILURE_OPENERS if m in first]
        if any(marker in title.lower() for marker in _EMPTY_TITLES):
            said_again += [m for m in _EMPTY_OPENERS if m in first]
        # A failure sentence belongs to the title when the title already
        # announces one, or when both are plainly about the same subject
        # ("Exclusion Still In Place" / "Windows did not remove the
        # exclusion." - no shared failure word, same news).
        about_the_title = (any(marker in title.lower()
                                for marker in _FAILURE_TITLES)
                           or _content(title) & _content(first))
        if said_again and about_the_title:
            offenders.append("%s:%d %r says %r again in its body: %r"
                             % (path, line, title, said_again[0], first[:70]))
            continue
        shared = _phrases(title) & _phrases(message)
        if shared:
            offenders.append("%s:%d %r repeats %s from the title in its body"
                             % (path, line, title, ", ".join(sorted(shared))))
    assert not offenders, "\n  ".join(offenders)


def test_no_dialog_module_declares_a_title_size_heading():
    """The 16/18pt rung belongs to the chrome, for every dialog.

    A Tools popup answers in the banner and labels itself with an overline; a
    plain dialog keeps the strip title. Either way a module that reaches for
    the title rung has reintroduced the second heading C-002 was about.
    """
    title_rung = re.compile(r'TYPE\["(dialog_title|screen_title)"\]')
    offenders = []
    for path in pathlib.Path("app/ui/dialogs").glob("*.py"):
        for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(),
                                 1):
            if title_rung.search(line):
                offenders.append("%s:%d %s" % (path.name, n,
                                                line.strip()[:80]))
    # ...and the one plain dialog built outside that folder (Auto Maintenance,
    # which shares app.py with the main window's 18pt app-bar destination
    # title and so has to be scanned by function rather than by module).
    src = pathlib.Path("app/ui/app.py").read_text(encoding="utf-8")
    marker = "def _show_schedule_dialog"
    if marker in src:
        body = src.split(marker, 1)[1].split("\n    def ", 1)[0]
        for n, line in enumerate(body.splitlines(), 1):
            if title_rung.search(line):
                offenders.append("app.py (auto maintenance):%d %s"
                                 % (n, line.strip()[:80]))
    assert not offenders, "\n  ".join(offenders)
