"""What the Storage Insight hero says when a cleaner declines to look.

The scan engine reports a refusal as unknown (None), not 0. That contract is
only worth anything if the dialog carries it to the screen: a category whose
Office cache was skipped while Office was open must not read "All clean", and
must not silently drop five measured numbers either.

`_refresh_estimate` is the whole decision - it is exercised here on a bare
instance with stubbed widgets, so no Tk root or display is needed and the
assertions are about the words the user actually sees.
"""
from __future__ import annotations

from app.ui.dialogs.storage_insight_dialog import StorageInsightDialog
from app.utils import format_bytes


class _Banner:
    def __init__(self):
        self.title = None
        self.color = None

    def set_title(self, text, color=None):
        self.title = text
        self.color = color


class _Button:
    def __init__(self):
        self.text = None

    def config_text(self, text):
        self.text = text


class _Var:
    def __init__(self, value):
        self.value = value

    def get(self):
        return self.value


class _Label:
    def __init__(self):
        self.text = None
        self.color = None

    def winfo_exists(self):
        return True

    def config(self, text=None, fg=None):
        self.text = text
        self.color = fg


def _dialog(sizes, unknown, checked=None):
    """A bare instance: the three attributes _refresh_estimate reads."""
    d = object.__new__(StorageInsightDialog)
    d._cat_vars = {t: _Var(True) for t in sizes}
    d._cat_sizes = dict(sizes)
    d._cat_unknown = dict(unknown)
    d.banner = _Banner()
    d._clean_btn = _Button()
    if checked is not None:
        for title, on in checked.items():
            d._cat_vars[title] = _Var(on)
    return d


def test_a_refused_category_shows_its_known_bytes_plus_a_question_mark():
    d = _dialog({"Browser & app caches": 4096, "Game files & caches": 8192},
                {"Browser & app caches": 1, "Game files & caches": 0})
    d._refresh_estimate()
    assert d.banner.title == format_bytes(12288) + " + ?"
    assert d._clean_btn.text == "Clean Selected (%s + ?)" % format_bytes(12288)


def test_a_refused_category_with_nothing_measured_says_unknown_not_clean():
    """The lie this exists to stop: 0 bytes happened to be "All clean"."""
    d = _dialog({"Browser & app caches": 0, "Game files & caches": 0},
                {"Browser & app caches": 1, "Game files & caches": 0})
    d._refresh_estimate()
    assert d.banner.title == "? unknown"
    assert "All clean" not in d.banner.title


def test_a_build_with_no_refusals_still_reads_all_clean():
    d = _dialog({"Browser & app caches": 0, "Game files & caches": 0},
                {"Browser & app caches": 0, "Game files & caches": 0})
    d._refresh_estimate()
    assert d.banner.title == "All clean"
    assert d._clean_btn.text == "Clean Selected"


def test_a_pending_scan_still_says_scanning():
    d = _dialog({"Browser & app caches": None, "Game files & caches": 4096},
                {"Browser & app caches": 0, "Game files & caches": 0})
    d._refresh_estimate()
    assert d.banner.title == "Scanning…"


def test_the_category_pill_says_how_much_is_unknown():
    """The row is where a user sees one skipped cleaner: the bytes it did
    measure plus a marker, never a 0 that reads as an empty folder."""
    title = "Browser & app caches"
    d = _dialog({title: 4096}, {title: 1})
    d._cat_size_lbls = {title: _Label()}
    d._cat_refits = {}
    d._paint_category(title, 4096, 1)
    assert d._cat_size_lbls[title].text == format_bytes(4096) + " + ?"
    d._paint_category(title, 0, 1)
    assert d._cat_size_lbls[title].text == "? unknown"
    d._paint_category(title, 0, 0)
    assert d._cat_size_lbls[title].text == "✓ clean"


def test_an_unchecked_refused_category_does_not_taint_the_headline():
    """The user opted that category out of the clean, so its unknown size is
    not part of the estimate - and the number is a plain total again."""
    d = _dialog({"Browser & app caches": 4096, "Game files & caches": 0},
                {"Browser & app caches": 1, "Game files & caches": 0},
                checked={"Browser & app caches": False})
    d._refresh_estimate()
    assert d.banner.title == "All clean"
