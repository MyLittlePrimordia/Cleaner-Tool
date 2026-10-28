"""Inline status messages say the outcome once.

The status line, the tab footer caption and the StatusLine in a Tools page are
all one-line answers to "what just happened". When the label beside them - the
Drivers page header, the row that was clicked, a notice's title - is handed the
very same sentence, the window says it twice: the Drivers tab used to paint
"Installed 3 drivers" in its header and again in its footer, four hundred
pixels apart in the same glance.

`_install_done_copy` is the same rule for the one place a variable body made
the notice audit blind: the install notice is titled with its outcome and its
body carries only the counts, and a run the user stopped is never titled
"Install Done".
"""
from __future__ import annotations

import ast
import pathlib

import pytest

from types import SimpleNamespace

from app.ui.app import (_install_done_copy, _install_start_copy,
                       _install_task_status)

UI_ROOT = pathlib.Path(__file__).resolve().parents[2] / "app" / "ui"

#: Calls that write the inline one-line status.
_STATUS_ATTRS = {"_say", "_set_status", "set_status"}
#: The same, on a StatusLine / ProgressStrip held by the page.
_STATUS_OWNERS = {"status", "progress", "progress_strip"}
_STATUS_METHODS = {"stop", "start", "set_text"}

#: Calls that paint a visible label somewhere else on the same screen.
_LABEL_ATTRS = {"set_title", "set_sub", "config_text"}

#: The implementations of the status helpers themselves: there the same
#: expression in and out IS the plumbing, not a duplicate sentence.
_HELPER_DEFS = {"_say", "_set_status", "set_status", "stop", "start",
                "set_text", "_set_caption", "set_caption"}


def _owner_and_attr(call):
    func = call.func
    if isinstance(func, ast.Attribute):
        try:
            owner = ast.unparse(func.value)
        except Exception:
            owner = ""
        return owner.rsplit(".", 1)[-1], func.attr
    if isinstance(func, ast.Name):
        return "", func.id
    return None, None


def _arg_text(call):
    """The sentence this call writes, as source, or None."""
    args = list(call.args) + [k.value for k in call.keywords
                              if k.arg in ("text", "message", "title", None)]
    if not args:
        return None
    node = args[0]
    if isinstance(node, ast.Constant) and node.value in ("", None):
        return None
    try:
        return ast.unparse(node)
    except Exception:
        return None


def _status_and_labels(func):
    """(status texts, label texts) written directly inside one function."""
    status, labels = [], []
    stack = list(func.body)
    while stack:
        node = stack.pop()
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node is not func:
            continue            # its own scope; scanned separately
        if isinstance(node, ast.Call):
            owner, attr = _owner_and_attr(node)
            text = _arg_text(node)
            if text is not None:
                if attr in _STATUS_ATTRS:
                    status.append((text, node.lineno))
                elif attr in _STATUS_METHODS and owner in _STATUS_OWNERS:
                    status.append((text, node.lineno))
                elif attr in _LABEL_ATTRS:
                    labels.append((text, node.lineno))
                elif attr == "config" and any(
                        k.arg == "text" for k in node.keywords):
                    text_node = next(k.value for k in node.keywords
                                     if k.arg == "text")
                    if not (isinstance(text_node, ast.Constant)
                            and text_node.value in ("", None)):
                        labels.append((ast.unparse(text_node), node.lineno))
        stack.extend(ast.iter_child_nodes(node))
    return status, labels


def test_an_inline_status_does_not_repeat_the_label_beside_it():
    """One sentence, one place: a status call and a label setter in the same
    function must not be handed the same expression."""
    offenders = []
    for path in sorted(UI_ROOT.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if node.name in _HELPER_DEFS:
                continue
            status, labels = _status_and_labels(node)
            shown = {}
            for text, line in labels:
                shown.setdefault(text, line)
            for text, line in status:
                if text in shown:
                    offenders.append(
                        "%s:%d %s() says %r, which %s:%d also puts on screen"
                        % (path.relative_to(UI_ROOT.parents[2]), line,
                           node.name, text,
                           path.relative_to(UI_ROOT.parents[2]), shown[text]))
    assert not offenders, "\n  ".join(offenders)


def test_a_finished_install_names_its_outcome_once():
    status, title, body = _install_done_copy(3, 0, 2, stopped=False)
    assert title == "Install Done"
    # the strip carries the news; the body carries only what is new
    assert "done" not in body.lower()
    assert "complete" not in body.lower()
    assert body.startswith("3 succeeded")
    assert "2 skipped" in body
    # the status line keeps the whole sentence - it outlives the dialog
    assert status.startswith("Install finished: ")


def test_the_update_job_names_winget_not_its_button_label():
    """"Running Update Everything..." named a button and left the user
    guessing; the job being waited on is `winget upgrade --all`."""
    from app.tasks.install_tasks import UPDATE_ALL_STATUS

    update = SimpleNamespace(label="Update Everything", key="update_all")
    assert _install_task_status(update) == UPDATE_ALL_STATUS
    assert "winget" in UPDATE_ALL_STATUS

    # every other Install-tab job keeps the label it is logged under
    store = SimpleNamespace(label="Install Microsoft Store", key="store")
    assert _install_task_status(store) == "Running Install Microsoft Store..."


def test_a_run_opens_with_the_word_for_its_job():
    status, banner = _install_start_copy(3)
    assert status == "Installing 3 item(s)..."
    assert banner == "===== Installing 3 selected item(s) ====="


def test_an_update_run_opens_as_an_update():
    """`total` is one TASK for an update run, not one app - printing
    "Installing 1 item(s)" described the wrong job and a number nobody asked
    for."""
    status, banner = _install_start_copy(1, kind="update")
    assert status == "Updating installed apps..."
    assert banner == "===== Updating all apps ====="
    # the "1 item(s)" of the install copy is a task count here, not an app
    assert "item(s)" not in (status + banner)
    assert not status.startswith("Installing")


def test_an_update_run_is_not_titled_an_install():
    """`winget upgrade --all` upgrades apps; it installs none. The card that
    said "Install Done" over it named the wrong job."""
    status, title, body = _install_done_copy(1, 0, 0, stopped=False,
                                             kind="update")
    assert title == "Update Done"
    assert status == "Update finished: 1 succeeded, 0 failed."
    assert body == "1 succeeded, 0 failed."
    assert "install" not in (status + title + body).lower()


def test_a_stopped_or_failed_update_says_update():
    _status, title, _body = _install_done_copy(0, 1, 0, stopped=False,
                                               kind="update")
    assert title == "Update Failed"
    _status, title, _body = _install_done_copy(1, 1, 0, stopped=False,
                                               kind="update")
    assert title == "Update Finished With Errors"
    status, title, body = _install_done_copy(0, 0, 0, stopped=True,
                                             kind="update")
    assert title == "Update Stopped"
    assert body == "Nothing was updated."
    assert "installed" not in status.lower()


def test_an_unknown_kind_falls_back_to_the_install_copy():
    assert _install_done_copy(1, 0, 0, stopped=False, kind="nonsense")[1] \
        == "Install Done"


def test_a_failed_install_is_not_titled_done():
    # "0 succeeded, 2 failed." under a green "Install Done" was the old copy
    _status, title, body = _install_done_copy(0, 2, 0, stopped=False)
    assert title == "Install Failed"
    assert "failed" in body
    _status, title, _body = _install_done_copy(3, 1, 0, stopped=False)
    assert title == "Install Finished With Errors"
    assert "done" not in title.lower()


def test_a_stopped_install_is_not_titled_done():
    status, title, body = _install_done_copy(2, 0, 0, stopped=True)
    assert title == "Install Stopped"
    assert "done" not in title.lower()
    assert "stopped" not in body.lower()
    assert "2 succeeded" in body
    assert status.startswith("Install stopped")


def test_stopping_before_anything_finished_says_that():
    status, title, body = _install_done_copy(0, 0, 0, stopped=True)
    assert title == "Install Stopped"
    assert "0 finished" not in status
    assert body == "Nothing was installed."


@pytest.mark.parametrize("ok_n,fail_n,skipped_n", [(0, 1, 0), (1, 0, 0),
                                                   (4, 2, 3)])
def test_the_install_notice_body_never_opens_with_its_title(ok_n, fail_n,
                                                            skipped_n):
    _status, title, body = _install_done_copy(ok_n, fail_n, skipped_n,
                                              stopped=False)
    first = body.strip().split(",")[0].lower()
    for word in ("install", "done", "complete"):
        assert word not in first, (title, body)
